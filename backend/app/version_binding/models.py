"""版本关联领域模型与状态常量。

关键约定：
- 版本号一律按原始字符串保存和比较，不做 float/int 归一化，
  因此 ``"1.1"`` 与 ``"1.10"`` 是两个不同版本；
- 存量执行的 ``历史版本字符串`` 原样保留，迁移只在新字段 ``版本关联`` 写回结构化结果；
- 关联不到模板或发布记录时不猜测，标记为待核实并写明原因。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 执行核对清单上的关联状态
BINDING_CONFIRMED = "已确认"
BINDING_PENDING = "待核实"

# 待核实原因码
PENDING_NO_BINDING = "NO_HISTORICAL_BINDING"      # 执行没有历史版本绑定
PENDING_NO_TEMPLATE = "TEMPLATE_NOT_FOUND"       # 模板编号在模板表中不存在
PENDING_NO_VERSION = "VERSION_RELEASE_NOT_FOUND"  # 模板下找不到该版本号发布记录

# 切换模式：dual_read 切换中断期允许按旧回执/旧字符串查询；structured 只认结构化关联
CUTOVER_DUAL_READ = "dual_read"
CUTOVER_STRUCTURED = "structured"

ALL_PENDING_REASONS = (PENDING_NO_BINDING, PENDING_NO_TEMPLATE, PENDING_NO_VERSION)


@dataclass(frozen=True)
class VersionBinding:
    """结构化版本关联：定位到具体模板与具体发布记录的三元组。"""

    template_code: str          # 模板编号，如 FLOW-T01
    template_id: int | None     # flow_template 表主键，待核实时为 None
    version_label: str          # 版本号原字符串，如 "1.10"（不做数值化）
    release_id: int | None      # version_release 表主键，待核实时为 None
    release_code: str           # 发布编号，如 VERS-T01-110
    status: str                 # BINDING_CONFIRMED / BINDING_PENDING
    source_version_string: str  # 执行上保存的原始版本字符串，原样留档
    pending_reason: str = ""
    receipt_codes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "template_code": self.template_code,
            "template_id": self.template_id,
            "version_label": self.version_label,
            "release_id": self.release_id,
            "release_code": self.release_code,
            "status": self.status,
            "source_version_string": self.source_version_string,
            "pending_reason": self.pending_reason,
            "receipt_codes": list(self.receipt_codes),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "VersionBinding":
        return cls(
            template_code=str(payload.get("template_code", "")),
            template_id=payload.get("template_id"),
            version_label=str(payload.get("version_label", "")),
            release_id=payload.get("release_id"),
            release_code=str(payload.get("release_code", "")),
            status=str(payload.get("status", BINDING_PENDING)),
            source_version_string=str(payload.get("source_version_string", "")),
            pending_reason=str(payload.get("pending_reason", "")),
            receipt_codes=tuple(payload.get("receipt_codes", [])),
        )


def pending_binding(template_code: str, source_version_string: str,
                    reason: str, receipt_codes: tuple[str, ...] = ()) -> VersionBinding:
    """构造待核实关联：模板/发布记录 id 留空，原因码必填。"""
    return VersionBinding(
        template_code=template_code,
        template_id=None,
        version_label=source_version_string,
        release_id=None,
        release_code="",
        status=BINDING_PENDING,
        source_version_string=source_version_string,
        pending_reason=reason,
        receipt_codes=receipt_codes,
    )
