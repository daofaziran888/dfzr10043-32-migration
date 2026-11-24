"""在途采集回执核对与切换期查询。

采集批次（capture_batch）是执行产出的采集回执，旧回执只带 ``回执版本字符串``。
切换到结构化版本关联期间分两种读取模式：

- dual_read（切换中断/迁移未完成）：优先按执行的结构化关联查；
  关联缺失或待核实时，回退到回执自身携带的版本字符串，保证旧执行
  仍能按回执查询到来源版本；
- structured（切换完成）：只认执行核对清单上的结构化关联，
  待核实执行不得当作已确认依据消费。

回执核对：用执行实际绑定的模板+版本号，校验在途回执声称的版本是否一致。
"""
from __future__ import annotations

from typing import Any

from app.services import service_for
from app.version_binding.models import (
    BINDING_CONFIRMED,
    BINDING_PENDING,
    CUTOVER_DUAL_READ,
    CUTOVER_STRUCTURED,
    VersionBinding,
)

# 核对结论
VERDICT_MATCH = "版本一致"
VERDICT_MISMATCH = "版本不符"
VERDICT_PENDING = "待核实"
VERDICT_NO_RECEIPT = "无回执"


class ReceiptReconciler:
    def __init__(self, cutover_mode: str = CUTOVER_DUAL_READ) -> None:
        self.cutover_mode = cutover_mode
        self._run_svc = service_for("flow_run")
        self._batch_svc = service_for("capture_batch")

    def _run_by_code(self, run_code: str) -> dict[str, Any] | None:
        for row in self._run_svc.list_entries(size=10000)[0]:
            if row.get("执行编号") == run_code:
                return row
        return None

    def receipts_for_run(self, run_code: str) -> list[dict[str, Any]]:
        return [
            row for row in self._batch_svc.list_entries(size=10000)[0]
            if row.get("关联执行") == run_code
        ]

    def query_source_version(self, run_code: str) -> dict[str, Any]:
        """查询某条执行当前可核对的来源版本（切换期双读）。"""
        run = self._run_by_code(run_code)
        if run is None:
            return {"run_code": run_code, "queryable": False, "reason": "执行不存在"}

        binding_payload = run.get("版本关联")
        binding = VersionBinding.from_dict(binding_payload) if binding_payload else None

        if binding is not None and binding.status == BINDING_CONFIRMED:
            return {
                "run_code": run_code,
                "queryable": True,
                "mode": "structured",
                "template_code": binding.template_code,
                "version_label": binding.version_label,
                "release_code": binding.release_code,
                "source_version_string": binding.source_version_string,
            }

        if self.cutover_mode == CUTOVER_STRUCTURED:
            return {
                "run_code": run_code,
                "queryable": False,
                "mode": "structured",
                "reason": "结构化关联缺失或待核实，结构化模式下不提供来源版本",
            }

        # dual_read：回退到该执行在途回执自带的旧版本字符串
        receipts = self.receipts_for_run(run_code)
        fallback_versions = sorted({
            str(r.get("回执版本字符串", "")) for r in receipts
            if r.get("回执版本字符串")
        })
        if fallback_versions:
            return {
                "run_code": run_code,
                "queryable": True,
                "mode": "legacy_receipt_fallback",
                "template_code": receipts[0].get("回执模板编号", ""),
                "version_label": fallback_versions[0],
                "release_code": "",
                "source_version_string": fallback_versions[0],
            }

        # 连回执都没有时，最后兜底执行自身的历史字符串（仅作留档查询）
        legacy = str(run.get("历史版本字符串", ""))
        if legacy:
            return {
                "run_code": run_code,
                "queryable": True,
                "mode": "legacy_string_fallback",
                "template_code": run.get("关联模板", ""),
                "version_label": legacy,
                "release_code": "",
                "source_version_string": legacy,
            }
        return {
            "run_code": run_code,
            "queryable": False,
            "mode": "legacy_receipt_fallback",
            "reason": "无结构化关联且无在途回执，保持待核实",
        }

    def verify_receipt(self, receipt: dict[str, Any]) -> dict[str, Any]:
        """用执行实际绑定版本核对单条在途采集回执。"""
        run_code = str(receipt.get("关联执行", ""))
        run = self._run_by_code(run_code)
        receipt_version = str(receipt.get("回执版本字符串", ""))
        receipt_template = str(receipt.get("回执模板编号", ""))
        base = {
            "receipt_code": receipt.get("批次编号", ""),
            "run_code": run_code,
            "receipt_version": receipt_version,
            "receipt_template": receipt_template,
        }

        if run is None:
            return {**base, "verdict": VERDICT_PENDING, "reason": "关联执行不存在"}

        binding_payload = run.get("版本关联")
        binding = VersionBinding.from_dict(binding_payload) if binding_payload else None

        if binding is None or binding.status == BINDING_PENDING:
            # 切换中断期：结构化依据缺失，按旧回执仍可登记，但结论只能是待核实
            if self.cutover_mode == CUTOVER_STRUCTURED:
                return {**base, "verdict": VERDICT_PENDING,
                        "reason": "执行版本关联待核实，结构化模式暂缓消费"}
            return {**base, "verdict": VERDICT_PENDING,
                    "reason": "执行版本关联待核实，按旧回执留档待人工确认"}

        same_template = binding.template_code == receipt_template
        same_version = binding.version_label == receipt_version  # 字符串精确比较
        if same_template and same_version:
            return {**base, "verdict": VERDICT_MATCH,
                    "bound_template": binding.template_code,
                    "bound_version": binding.version_label,
                    "release_code": binding.release_code}
        return {**base, "verdict": VERDICT_MISMATCH,
                "bound_template": binding.template_code,
                "bound_version": binding.version_label,
                "release_code": binding.release_code,
                "reason": "回执模板或版本与执行实际绑定不一致"}

    def verify_all_inflight(self) -> list[dict[str, Any]]:
        """核对全部在途（未确认）采集回执。"""
        inflight = [
            row for row in self._batch_svc.list_entries(size=10000)[0]
            if row.get("status") in ("归集中", "校验中")
        ]
        return [self.verify_receipt(row) for row in inflight]
