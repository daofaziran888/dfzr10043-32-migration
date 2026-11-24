"""带检查点的版本关联迁移引擎。

迁移语义：
- 每条 flow_run 独立解析、独立写回「版本关联」到执行核对清单，
  原 ``历史版本字符串`` 字段永不修改（存量数据保留原字符）；
- 每确认一条就在检查点落一条已确认依据；中断后重跑时，已确认的执行
  直接复用检查点依据，不重新解析、不被后续数据变化改写；
- 待核实记录同样进入检查点，重跑保持待核实，直到外部补齐模板/发布记录；
- ``fail_after`` 用于演练切换中断：处理完指定条数后抛出 MigrationInterrupted，
  此时已确认条目已经写回并持久化在检查点中。
"""
from __future__ import annotations

from typing import Any

from app.services import service_for
from app.version_binding.models import (
    BINDING_CONFIRMED,
    BINDING_PENDING,
    VersionBinding,
)
from app.version_binding.resolver import VersionResolver


class MigrationInterrupted(RuntimeError):
    """迁移在处理中途被打断（演练用），已确认依据已在检查点中。"""

    def __init__(self, processed: int, confirmed: int, pending: int) -> None:
        super().__init__(
            f"迁移中断：已处理 {processed} 条，确认 {confirmed} 条，待核实 {pending} 条"
        )
        self.processed = processed
        self.confirmed = confirmed
        self.pending = pending


class MigrationEngine:
    def __init__(self, resolver: VersionResolver | None = None,
                 checkpoint: dict[str, Any] | None = None) -> None:
        self._run_svc = service_for("flow_run")
        self._batch_svc = service_for("capture_batch")
        self.resolver = resolver or VersionResolver()
        # 检查点：run_id(str) -> 关联 dict；由调用方持有，恢复时原样传回
        self.checkpoint: dict[str, dict[str, Any]] = checkpoint or {}

    # ── 检查点与清单 ─────────────────────────────────────────────

    def _receipts_for_run(self, run_code: str) -> tuple[str, ...]:
        return tuple(
            str(row.get("批次编号", ""))
            for row in self._batch_svc.list_entries(size=10000)[0]
            if row.get("关联执行") == run_code
        )

    def checkpoint_snapshot(self) -> dict[str, dict[str, Any]]:
        return {key: dict(value) for key, value in self.checkpoint.items()}

    def confirmed_run_ids(self) -> list[int]:
        return [
            int(key) for key, value in self.checkpoint.items()
            if value.get("status") == BINDING_CONFIRMED
        ]

    def pending_run_ids(self) -> list[int]:
        return [
            int(key) for key, value in self.checkpoint.items()
            if value.get("status") == BINDING_PENDING
        ]

    def _write_back(self, run: dict[str, Any], binding: VersionBinding) -> None:
        """把结构化关联写回执行核对清单，历史版本字符串保持原样。"""
        run["版本关联"] = binding.to_dict()

    # ── 迁移主流程 ───────────────────────────────────────────────

    def _targets(self) -> list[dict[str, Any]]:
        """只迁移演练/新链路产生的执行：关联模板属于现有模板表的记录。"""
        known_codes = {
            row.get("模板编号")
            for row in service_for("flow_template").list_entries(size=10000)[0]
        }
        return [
            row for row in self._run_svc.list_entries(size=10000)[0]
            if row.get("关联模板") in known_codes
        ]

    def migrate(self, fail_after: int | None = None) -> dict[str, Any]:
        """执行（或恢复执行）迁移。

        fail_after: 处理满该条数后抛 MigrationInterrupted，模拟切换中断；
                    None 表示跑到全部完成。
        """
        processed = 0
        reused = 0
        results: list[dict[str, Any]] = []

        for run in self._targets():
            run_id = str(run["id"])

            # 恢复路径：检查点里已有结论的，原样复用，不再重新解析
            if run_id in self.checkpoint:
                binding = VersionBinding.from_dict(self.checkpoint[run_id])
                self._write_back(run, binding)
                reused += 1
                processed += 1
                results.append(binding.to_dict())
                if fail_after is not None and processed >= fail_after:
                    raise MigrationInterrupted(processed, len(self.confirmed_run_ids()),
                                               len(self.pending_run_ids()))
                continue

            receipts = self._receipts_for_run(str(run.get("执行编号", "")))
            binding = self.resolver.resolve(
                template_code=str(run.get("关联模板", "")),
                source_version_string=str(run.get("历史版本字符串", "")),
                receipt_codes=receipts,
            )
            self._write_back(run, binding)
            # 每条确认后立即落检查点：中断也不丢已确认依据
            self.checkpoint[run_id] = binding.to_dict()
            processed += 1
            results.append(binding.to_dict())

            if fail_after is not None and processed >= fail_after:
                raise MigrationInterrupted(processed, len(self.confirmed_run_ids()),
                                           len(self.pending_run_ids()))

        return {
            "status": "completed",
            "total": len(self._targets()),
            "processed": processed,
            "reused_from_checkpoint": reused,
            "confirmed": len(self.confirmed_run_ids()),
            "pending": len(self.pending_run_ids()),
            "items": results,
        }
