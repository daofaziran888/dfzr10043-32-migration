"""归集消费的实际绑定版本校验与下游交接包。

归集侧消费采集批次前，先读取产出执行核对清单上的「版本关联」，确认执行
实际绑定的模板与版本，再用它去验证旧回执；交接包里必须保留每个执行引用的
来源版本字符串与回执编号，下游才能回溯。
"""
from __future__ import annotations

from typing import Any

from app.services import service_for
from app.version_binding.models import BINDING_CONFIRMED, BINDING_PENDING, VersionBinding
from app.version_binding.receipts import (
    VERDICT_MATCH,
    VERDICT_MISMATCH,
    VERDICT_NO_RECEIPT,
    VERDICT_PENDING,
    ReceiptReconciler,
)


def _binding_of(run: dict[str, Any]) -> VersionBinding | None:
    payload = run.get("版本关联")
    return VersionBinding.from_dict(payload) if payload else None


def aggregate_bound_versions(run_codes: list[str]) -> dict[str, Any]:
    """归集消费前汇总各执行实际绑定版本，并验证其在途旧回执。

    返回每个执行的绑定依据、回执核对结论，以及总体可否消费标记。
    任一执行待核实或回执版本不符，整体不得放行消费。
    """
    run_svc = service_for("flow_run")
    reconciler = ReceiptReconciler()
    runs_by_code = {
        str(row.get("执行编号", "")): row
        for row in run_svc.list_entries(size=10000)[0]
    }

    items: list[dict[str, Any]] = []
    consumable = True
    for run_code in run_codes:
        run = runs_by_code.get(run_code)
        if run is None:
            consumable = False
            items.append({"run_code": run_code, "consumable": False,
                          "reason": "执行不存在"})
            continue

        binding = _binding_of(run)
        receipts = reconciler.receipts_for_run(run_code)
        receipt_results = [reconciler.verify_receipt(r) for r in receipts]
        verdicts = {r["verdict"] for r in receipt_results}

        if binding is None or binding.status == BINDING_PENDING:
            item_status = BINDING_PENDING
            ok = False
        elif VERDICT_MISMATCH in verdicts or VERDICT_PENDING in verdicts:
            item_status = "回执异常"
            ok = False
        elif not receipt_results:
            item_status = VERDICT_NO_RECEIPT
            ok = False
        else:
            item_status = BINDING_CONFIRMED
            ok = VERDICT_MATCH in verdicts and verdicts <= {VERDICT_MATCH}

        consumable = consumable and ok
        items.append({
            "run_code": run_code,
            "consumable": ok,
            "binding": binding.to_dict() if binding else None,
            "receipt_verdicts": receipt_results,
            "status": item_status,
        })

    return {
        "consumable": consumable,
        "total_runs": len(run_codes),
        "confirmed": sum(1 for i in items if i.get("status") == BINDING_CONFIRMED),
        "pending": sum(1 for i in items if i.get("status") == BINDING_PENDING),
        "items": items,
    }


def build_handoff_pack(run_codes: list[str], target_system: str = "datalake_downstream") -> dict[str, Any]:
    """生成交接包：保留引用的来源版本字符串、发布记录与回执清单。

    待核实或回执不符时拒绝出包，避免把没有确认来源的执行交给下游。
    """
    summary = aggregate_bound_versions(run_codes)
    if not summary["consumable"]:
        return {
            "ok": False,
            "reason": "存在待核实执行或回执版本不符，交接包不予生成",
            "summary": summary,
        }

    references = []
    for item in summary["items"]:
        binding = item["binding"]
        references.append({
            "run_code": item["run_code"],
            "template_code": binding["template_code"],
            "release_code": binding["release_code"],
            "version_label": binding["version_label"],
            # 来源版本字符串原样留档，供下游按旧口径回溯
            "source_version_string": binding["source_version_string"],
            "receipt_codes": binding["receipt_codes"],
        })

    return {
        "ok": True,
        "target_system": target_system,
        "total_runs": len(run_codes),
        "references": references,
        "summary": summary,
    }
