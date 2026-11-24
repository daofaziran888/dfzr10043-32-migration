"""执行版本结构化关联迁移领域。

把流程执行历史上只保存的版本字符串，升级为「模板 + 版本号 + 发布记录」三元组
的结构化版本关联，并提供迁移断点续跑、在途回执核对、归集消费校验和下游交接能力。

模块划分：
- models.py   关联结构、核对清单、检查点与状态常量
- resolver.py 模板+版本号精确匹配（字符串原样比较，1.1 与 1.10 永不相等）
- migration.py 带检查点的迁移引擎，中断恢复时保留已确认依据
- receipts.py 在途采集回执核对与切换期双读查询
- handoff.py  归集消费执行的实际绑定版本校验与交接包
"""
from __future__ import annotations

from app.version_binding.models import (
    BINDING_CONFIRMED,
    BINDING_PENDING,
    PENDING_NO_BINDING,
    PENDING_NO_TEMPLATE,
    PENDING_NO_VERSION,
    CUTOVER_DUAL_READ,
    CUTOVER_STRUCTURED,
    VersionBinding,
)
from app.version_binding.resolver import VersionResolver
from app.version_binding.migration import MigrationEngine, MigrationInterrupted
from app.version_binding.receipts import ReceiptReconciler
from app.version_binding.handoff import build_handoff_pack, aggregate_bound_versions

__all__ = [
    "BINDING_CONFIRMED",
    "BINDING_PENDING",
    "PENDING_NO_BINDING",
    "PENDING_NO_TEMPLATE",
    "PENDING_NO_VERSION",
    "CUTOVER_DUAL_READ",
    "CUTOVER_STRUCTURED",
    "VersionBinding",
    "VersionResolver",
    "MigrationEngine",
    "MigrationInterrupted",
    "ReceiptReconciler",
    "build_handoff_pack",
    "aggregate_bound_versions",
]
