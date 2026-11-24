"""版本关联解析器：在模板作用域内按版本号原字符串精确匹配发布记录。

匹配规则（顺序即优先级）：
1. 执行没有历史版本字符串 → 待核实（NO_HISTORICAL_BINDING）；
2. 模板编号在 flow_template 中不存在 → 待核实（TEMPLATE_NOT_FOUND）；
3. 在该模板的 version_release 记录里按版本号 **原字符串相等** 查找：
   - 命中唯一记录 → 确认关联；
   - 命中多条（同模板同版本号重复发布）→ 不猜测，待核实；
   - 一条都没有 → 待核实（VERSION_RELEASE_NOT_FOUND）。

严禁把版本号转成 float/double 比较：float("1.1") == float("1.10")，
会把两个不同版本错误合并。模板编号先把作用域隔离开，因此两个模板
各自的 "1.10" 不会互相串入。
"""
from __future__ import annotations

from app.services import service_for
from app.version_binding.models import (
    BINDING_CONFIRMED,
    PENDING_NO_BINDING,
    PENDING_NO_TEMPLATE,
    PENDING_NO_VERSION,
    VersionBinding,
    pending_binding,
)


class VersionResolver:
    def __init__(self) -> None:
        # 解析次数统计：用于测试断言「恢复时已确认依据不被重新解析」
        self.resolve_calls = 0

    def find_template(self, template_code: str) -> dict | None:
        tpl_svc = service_for("flow_template")
        for row in tpl_svc.list_entries(size=10000)[0]:
            if row.get("模板编号") == template_code:
                return row
        return None

    def find_release(self, template_code: str, version_label: str) -> dict | None:
        """在指定模板作用域内按版本号原字符串精确查找唯一发布记录。"""
        ver_svc = service_for("version_release")
        matches = [
            row for row in ver_svc.list_entries(size=10000)[0]
            if row.get("关联模板") == template_code
            and str(row.get("版本号", "")) == version_label
        ]
        return matches[0] if len(matches) == 1 else None

    def resolve(self, template_code: str, source_version_string: str,
                receipt_codes: tuple[str, ...] = ()) -> VersionBinding:
        """把一条执行的历史版本字符串解析为结构化版本关联。"""
        self.resolve_calls += 1
        legacy = (source_version_string or "").strip()

        if not legacy:
            return pending_binding(template_code, legacy, PENDING_NO_BINDING, receipt_codes)

        template = self.find_template(template_code)
        if template is None:
            return pending_binding(template_code, legacy, PENDING_NO_TEMPLATE, receipt_codes)

        release = self.find_release(template_code, legacy)
        if release is None:
            return pending_binding(template_code, legacy, PENDING_NO_VERSION, receipt_codes)

        return VersionBinding(
            template_code=template_code,
            template_id=int(template["id"]),
            version_label=legacy,
            release_id=int(release["id"]),
            release_code=str(release.get("发布编号", "")),
            status=BINDING_CONFIRMED,
            source_version_string=legacy,
            receipt_codes=receipt_codes,
        )
