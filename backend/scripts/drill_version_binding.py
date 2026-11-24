#!/usr/bin/env python3
"""版本结构化关联迁移 · 升级演练脚本。

演练完整升级故事线：
  1. 素材：两个模板、三条版本发布记录、四条执行（一条无历史绑定）、三条在途回执；
  2. 迁移在处理 2 条后中断 → 验证检查点里已确认依据仍在、历史原字符未动；
  3. 中断期 dual_read：旧执行仍能按在途回执查询来源版本，未绑定执行保持待核实；
  4. 携带检查点恢复迁移直到完成；
  5. 核对同模板 1.1 / 1.10 分别定位、另一模板 1.10 不串号；
  6. 用执行实际绑定版本验证在途旧回执；
  7. 归集消费并生成交接包，确认来源版本字符串与回执被保留；
  8. 结构化模式下未绑定记录仍为待核实。

运行：cd backend && .venv/bin/python -m scripts.drill_version_binding
"""
from __future__ import annotations

from app.services import service_for
from app.store import store
from app.version_binding import (
    BINDING_CONFIRMED,
    BINDING_PENDING,
    CUTOVER_DUAL_READ,
    CUTOVER_STRUCTURED,
    MigrationEngine,
    MigrationInterrupted,
    ReceiptReconciler,
    aggregate_bound_versions,
    build_handoff_pack,
)

PASS = "✅"
FAIL = "❌"
_checks = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global _checks
    _checks += 1
    mark = PASS if condition else FAIL
    print(f"  {mark} {label}" + (f" —— {detail}" if detail else ""))
    if not condition:
        raise SystemExit(f"演练断言失败：{label}")


def section(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def main() -> None:
    store.reset()
    run_svc = service_for("flow_run")
    tpl_svc = service_for("flow_template")
    ver_svc = service_for("version_release")

    section("0. 演练素材盘点")
    templates = [r for r in tpl_svc.list_entries(size=10000)[0]
                 if r["模板编号"] in ("FLOW-T01", "FLOW-T02")]
    releases = [r for r in ver_svc.list_entries(size=10000)[0]
                if r["id"] in (4, 5, 6)]
    print(f"  模板 {len(templates)} 个：" + "、".join(t["模板编号"] for t in templates))
    for rel in releases:
        print(f"  发布记录 {rel['发布编号']}: 模板={rel['关联模板']} 版本号={rel['版本号']}")
    check("两个模板就位", len(templates) == 2)
    check("三条版本发布记录就位", len(releases) == 3)

    section("1. 迁移中断：处理 2 条后模拟切换中断")
    engine = MigrationEngine()
    try:
        engine.migrate(fail_after=2)
        check("中断应抛出 MigrationInterrupted", False)
    except MigrationInterrupted as exc:
        print(f"  中断信息：{exc}")
        check("中断时已处理 2 条", exc.processed == 2)
        check("中断时已确认 2 条", exc.confirmed == 2)

    r_old_01 = next(r for r in run_svc.list_entries(size=10000)[0]
                    if r["执行编号"] == "RUN-T01-OLD-01")
    r_old_02 = next(r for r in run_svc.list_entries(size=10000)[0]
                    if r["执行编号"] == "RUN-T01-OLD-02")
    r_t02 = next(r for r in run_svc.list_entries(size=10000)[0]
                 if r["执行编号"] == "RUN-T02-OLD-01")
    r_new = next(r for r in run_svc.list_entries(size=10000)[0]
                 if r["执行编号"] == "RUN-T02-NEW-01")
    check("已确认依据已写回执行核对清单",
          r_old_01["版本关联"]["status"] == BINDING_CONFIRMED
          and r_old_02["版本关联"]["status"] == BINDING_CONFIRMED)
    check("存量历史版本字符串原样保留",
          r_old_01["历史版本字符串"] == "1.1" and r_old_02["历史版本字符串"] == "1.10")
    check("未处理执行尚无结构化关联", r_t02.get("版本关联") is None)

    section("2. 中断期恢复读取：dual_read 双读")
    reconciler = ReceiptReconciler(cutover_mode=CUTOVER_DUAL_READ)
    ans = reconciler.query_source_version("RUN-T02-OLD-01")
    print(f"  旧执行按回执查询：{ans}")
    check("旧执行仍可按在途回执查询来源版本",
          ans["queryable"] and ans["mode"] == "legacy_receipt_fallback"
          and ans["version_label"] == "1.10" and ans["template_code"] == "FLOW-T02")
    ans_new = reconciler.query_source_version("RUN-T02-NEW-01")
    check("无历史绑定执行不可作为确认来源", ans_new["queryable"] is False)

    section("3. 携带检查点恢复迁移直到完成")
    checkpoint = engine.checkpoint_snapshot()
    engine2 = MigrationEngine(checkpoint=checkpoint)
    result = engine2.migrate()
    print(f"  恢复结果：{result['status']}，确认 {result['confirmed']} 条，"
          f"待核实 {result['pending']} 条，复用检查点 {result['reused_from_checkpoint']} 条")
    check("迁移完成", result["status"] == "completed")
    check("3 条确认 + 1 条待核实", result["confirmed"] == 3 and result["pending"] == 1)
    check("恢复时复用 2 条已确认依据", result["reused_from_checkpoint"] == 2)
    check("1.1 历史字符全程未改",
          next(r for r in run_svc.list_entries(size=10000)[0]
               if r["执行编号"] == "RUN-T01-OLD-01")["历史版本字符串"] == "1.1")

    section("4. 版本定位核对：1.1 与 1.10 不混淆、跨模板不串号")
    bindings = {
        r["执行编号"]: r["版本关联"]
        for r in run_svc.list_entries(size=10000)[0]
        if r.get("版本关联")
    }
    b_11 = bindings["RUN-T01-OLD-01"]
    b_110 = bindings["RUN-T01-OLD-02"]
    b_t02 = bindings["RUN-T02-OLD-01"]
    print(f"  T01/1.1  -> release_id={b_11['release_id']} {b_11['release_code']}")
    print(f"  T01/1.10 -> release_id={b_110['release_id']} {b_110['release_code']}")
    print(f"  T02/1.10 -> release_id={b_t02['release_id']} {b_t02['release_code']}")
    check("同模板 1.1 定位到 VERS-T01-11", b_11["release_code"] == "VERS-T01-11")
    check("同模板 1.10 定位到 VERS-T01-110", b_110["release_code"] == "VERS-T01-110")
    check("1.1 与 1.10 不是同一发布记录", b_11["release_id"] != b_110["release_id"])
    check("另一模板 1.10 未串入 T01",
          b_t02["release_code"] == "VERS-T02-110" and b_t02["template_code"] == "FLOW-T02")
    check("无绑定执行标记待核实",
          bindings["RUN-T02-NEW-01"]["status"] == BINDING_PENDING
          and bindings["RUN-T02-NEW-01"]["pending_reason"] == "NO_HISTORICAL_BINDING")

    section("5. 在途旧回执核对（按执行实际绑定版本）")
    verdicts = {v["receipt_code"]: v for v in reconciler.verify_all_inflight()
                if v["receipt_code"].startswith("RC-")}
    for code, verdict in verdicts.items():
        print(f"  {code}: {verdict['verdict']}（绑定 {verdict.get('bound_template')}"
              f"/{verdict.get('bound_version')}）")
    check("三条在途回执全部版本一致",
          all(v["verdict"] == "版本一致" for v in verdicts.values())
          and len(verdicts) == 3)

    section("6. 归集消费与交接包")
    consumer_codes = ["RUN-T01-OLD-01", "RUN-T01-OLD-02", "RUN-T02-OLD-01"]
    summary = aggregate_bound_versions(consumer_codes)
    check("三条已确认执行允许归集消费", summary["consumable"] is True)
    blocked = build_handoff_pack(consumer_codes + ["RUN-T02-NEW-01"])
    check("含待核实执行时拒绝出包", blocked["ok"] is False)
    pack = build_handoff_pack(consumer_codes)
    check("交接包生成成功", pack["ok"] is True)
    for ref in pack["references"]:
        print(f"  交接引用 {ref['run_code']}: 模板={ref['template_code']} "
              f"版本={ref['version_label']} 来源字符串={ref['source_version_string']!r} "
              f"回执={ref['receipt_codes']}")
    first_ref = pack["references"][0]
    check("交接保留来源版本字符串", first_ref["source_version_string"] == "1.1")
    check("交接保留回执引用", first_ref["receipt_codes"] == ["RC-T01-11-01"])

    section("7. 切换完成后的结构化模式")
    strict = ReceiptReconciler(cutover_mode=CUTOVER_STRUCTURED)
    check("结构化模式下确认依据可查",
          strict.query_source_version("RUN-T01-OLD-01")["queryable"] is True)
    check("结构化模式下未绑定记录仍待核实、不可查",
          strict.query_source_version("RUN-T02-NEW-01")["queryable"] is False)

    section(f"演练完成：{_checks} 项核对全部通过")


if __name__ == "__main__":
    main()
