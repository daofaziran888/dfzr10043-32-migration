"""版本结构化关联迁移演练测试。

演练素材（seed.py 中维护）：
- 两个模板：FLOW-T01 订单采集模板、FLOW-T02 对账采集模板；
- 三条版本发布记录：VERS-T01-11(1.1)、VERS-T01-110(1.10)、VERS-T02-110(1.10)；
- 四条执行：RUN-T01-OLD-01(历史串 1.1)、RUN-T01-OLD-02(历史串 1.10)、
  RUN-T02-OLD-01(历史串 1.10)、RUN-T02-NEW-01(无历史绑定)；
- 三条在途采集回执 RC-T01-11-01 / RC-T01-110-01 / RC-T02-110-01。
"""
from __future__ import annotations

import pytest

from app.services import service_for
from app.version_binding import (
    BINDING_CONFIRMED,
    BINDING_PENDING,
    CUTOVER_DUAL_READ,
    CUTOVER_STRUCTURED,
    MigrationEngine,
    MigrationInterrupted,
    ReceiptReconciler,
    VersionResolver,
    aggregate_bound_versions,
    build_handoff_pack,
)
from app.version_binding.models import (
    PENDING_NO_BINDING,
    PENDING_NO_TEMPLATE,
    PENDING_NO_VERSION,
)

RUN_CODES = ["RUN-T01-OLD-01", "RUN-T01-OLD-02", "RUN-T02-OLD-01", "RUN-T02-NEW-01"]


def _run_by_code(code: str) -> dict:
    svc = service_for("flow_run")
    return next(row for row in svc.list_entries(size=10000)[0]
                if row.get("执行编号") == code)


class TestVersionResolver:
    """解析器：模板作用域 + 版本号原字符串精确匹配。"""

    def test_1_1_and_1_10_are_distinct_versions(self):
        resolver = VersionResolver()
        binding_11 = resolver.resolve("FLOW-T01", "1.1")
        binding_110 = resolver.resolve("FLOW-T01", "1.10")
        assert binding_11.status == BINDING_CONFIRMED
        assert binding_110.status == BINDING_CONFIRMED
        assert binding_11.release_id == 4
        assert binding_110.release_id == 5
        # 关键红线：字符串原样比较，1.1 与 1.10 绝不能指向同一条发布记录
        assert binding_11.release_id != binding_110.release_id
        assert binding_11.version_label == "1.1"
        assert binding_110.version_label == "1.10"

    def test_float_normalization_would_collapse_versions(self):
        # 证明为什么禁止数值化：float 会把两个版本错误合并
        assert float("1.1") == float("1.10")

    def test_same_version_label_does_not_leak_across_templates(self):
        resolver = VersionResolver()
        t01_binding = resolver.resolve("FLOW-T01", "1.10")
        t02_binding = resolver.resolve("FLOW-T02", "1.10")
        assert t01_binding.release_id == 5
        assert t02_binding.release_id == 6
        assert t01_binding.template_code == "FLOW-T01"
        assert t02_binding.template_code == "FLOW-T02"

    def test_missing_template_marked_pending(self):
        binding = VersionResolver().resolve("FLOW-UNKNOWN", "1.1")
        assert binding.status == BINDING_PENDING
        assert binding.pending_reason == PENDING_NO_TEMPLATE

    def test_missing_version_release_marked_pending(self):
        binding = VersionResolver().resolve("FLOW-T01", "9.99")
        assert binding.status == BINDING_PENDING
        assert binding.pending_reason == PENDING_NO_VERSION

    def test_empty_legacy_string_marked_pending(self):
        binding = VersionResolver().resolve("FLOW-T02", "")
        assert binding.status == BINDING_PENDING
        assert binding.pending_reason == PENDING_NO_BINDING


class TestMigrationChecklist:
    """迁移写回执行核对清单：存量原字符保留、待核实标注。"""

    def test_full_migration_writes_structured_bindings(self):
        engine = MigrationEngine()
        result = engine.migrate()
        assert result["status"] == "completed"
        assert result["total"] == 4
        assert result["confirmed"] == 3
        assert result["pending"] == 1

        r1 = _run_by_code("RUN-T01-OLD-01")["版本关联"]
        r2 = _run_by_code("RUN-T01-OLD-02")["版本关联"]
        r3 = _run_by_code("RUN-T02-OLD-01")["版本关联"]
        r4 = _run_by_code("RUN-T02-NEW-01")["版本关联"]

        assert r1["status"] == BINDING_CONFIRMED
        assert (r1["template_code"], r1["version_label"], r1["release_id"]) == ("FLOW-T01", "1.1", 4)
        assert (r2["template_code"], r2["version_label"], r2["release_id"]) == ("FLOW-T01", "1.10", 5)
        assert (r3["template_code"], r3["version_label"], r3["release_id"]) == ("FLOW-T02", "1.10", 6)
        assert r4["status"] == BINDING_PENDING
        assert r4["pending_reason"] == PENDING_NO_BINDING

    def test_legacy_version_string_is_never_modified(self):
        before = {code: _run_by_code(code).get("历史版本字符串") for code in RUN_CODES}
        MigrationEngine().migrate()
        after = {code: _run_by_code(code).get("历史版本字符串") for code in RUN_CODES}
        assert before == after
        assert before["RUN-T01-OLD-01"] == "1.1"
        assert before["RUN-T02-NEW-01"] == ""

    def test_binding_carries_receipt_references(self):
        MigrationEngine().migrate()
        binding = _run_by_code("RUN-T01-OLD-01")["版本关联"]
        assert "RC-T01-11-01" in binding["receipt_codes"]

    def test_new_execution_can_register_structured_binding_directly(self):
        """升级后新执行：直接写结构化关联，不依赖历史字符串。"""
        run_svc = service_for("flow_run")
        entry, missing = run_svc.create_entry({
            "执行编号": "RUN-T01-NEW-02",
            "关联模板": "FLOW-T01",
            "执行节点": "ROBO-0002",
        })
        assert not missing
        resolver = VersionResolver()
        binding = resolver.resolve("FLOW-T01", "1.10")
        entry["版本关联"] = binding.to_dict()
        stored = run_svc.get_entry(entry["id"])["版本关联"]
        assert stored["release_id"] == 5
        assert stored["status"] == BINDING_CONFIRMED


class TestMigrationRecovery:
    """切换中断与恢复：已确认执行依据保留，待核实保持待核实。"""

    def test_interrupted_migration_keeps_confirmed_checkpoint(self):
        resolver = VersionResolver()
        engine = MigrationEngine(resolver=resolver)
        with pytest.raises(MigrationInterrupted) as exc:
            engine.migrate(fail_after=2)
        assert exc.value.processed == 2
        assert exc.value.confirmed == 2

        # 中断时刻：前两条已确认并写回，后两条尚未处理
        assert _run_by_code("RUN-T01-OLD-01")["版本关联"]["status"] == BINDING_CONFIRMED
        assert _run_by_code("RUN-T01-OLD-02")["版本关联"]["status"] == BINDING_CONFIRMED
        assert _run_by_code("RUN-T02-OLD-01").get("版本关联") is None
        assert _run_by_code("RUN-T02-NEW-01").get("版本关联") is None

        # 模拟引擎重启：用持久化下来的检查点构造新引擎
        checkpoint = engine.checkpoint_snapshot()
        resolver2 = VersionResolver()
        engine2 = MigrationEngine(resolver=resolver2, checkpoint=checkpoint)
        result = engine2.migrate()
        assert result["status"] == "completed"
        assert result["confirmed"] == 3
        assert result["pending"] == 1

        # 恢复时两条已确认依据直接复用，未重新解析
        assert resolver2.resolve_calls == 2
        # 历史字符串在中断-恢复全程未被改动
        assert _run_by_code("RUN-T01-OLD-01")["历史版本字符串"] == "1.1"

    def test_recovered_confirmed_binding_is_byte_identical(self):
        engine = MigrationEngine()
        with pytest.raises(MigrationInterrupted):
            engine.migrate(fail_after=1)
        first = dict(engine.checkpoint_snapshot()["4"])

        engine2 = MigrationEngine(checkpoint=engine.checkpoint_snapshot())
        engine2.migrate()
        second = _run_by_code("RUN-T01-OLD-01")["版本关联"]
        assert first == second

    def test_pending_record_stays_pending_after_rerun(self):
        engine = MigrationEngine()
        engine.migrate()
        pending_id = str(_run_by_code("RUN-T02-NEW-01")["id"])
        assert engine.checkpoint[pending_id]["status"] == BINDING_PENDING

        # 再跑一轮：待核实结论原样保留，不会被强行确认
        engine_rerun = MigrationEngine(checkpoint=engine.checkpoint_snapshot())
        engine_rerun.migrate()
        assert _run_by_code("RUN-T02-NEW-01")["版本关联"]["pending_reason"] == PENDING_NO_BINDING


class TestDualReadAndReceipts:
    """切换中断期双读查询与在途回执核对。"""

    def test_legacy_receipt_query_works_after_interruption(self):
        engine = MigrationEngine()
        with pytest.raises(MigrationInterrupted):
            engine.migrate(fail_after=2)

        # 中断期（dual_read）：尚未迁移的旧执行仍可按在途回执查询来源版本
        reconciler = ReceiptReconciler(cutover_mode=CUTOVER_DUAL_READ)
        answer = reconciler.query_source_version("RUN-T02-OLD-01")
        assert answer["queryable"] is True
        assert answer["mode"] == "legacy_receipt_fallback"
        assert answer["template_code"] == "FLOW-T02"
        assert answer["version_label"] == "1.10"

    def test_unbound_execution_remains_pending_in_dual_read(self):
        MigrationEngine().migrate()
        reconciler = ReceiptReconciler(cutover_mode=CUTOVER_DUAL_READ)
        answer = reconciler.query_source_version("RUN-T02-NEW-01")
        # 无绑定、无回执：不可作为已确认来源消费
        assert answer["queryable"] is False

    def test_structured_mode_rejects_pending_source(self):
        MigrationEngine().migrate()
        reconciler = ReceiptReconciler(cutover_mode=CUTOVER_STRUCTURED)
        answer = reconciler.query_source_version("RUN-T02-NEW-01")
        assert answer["queryable"] is False
        assert answer["mode"] == "structured"

    def test_structured_mode_serves_confirmed_binding(self):
        MigrationEngine().migrate()
        reconciler = ReceiptReconciler(cutover_mode=CUTOVER_STRUCTURED)
        answer = reconciler.query_source_version("RUN-T01-OLD-01")
        assert answer["queryable"] is True
        assert answer["mode"] == "structured"
        assert answer["release_code"] == "VERS-T01-11"

    def test_inflight_receipts_verify_against_bound_versions(self):
        MigrationEngine().migrate()
        reconciler = ReceiptReconciler()
        verdicts = {v["receipt_code"]: v["verdict"]
                    for v in reconciler.verify_all_inflight()
                    if v["receipt_code"].startswith("RC-")}
        assert verdicts == {
            "RC-T01-11-01": "版本一致",
            "RC-T01-110-01": "版本一致",
            "RC-T02-110-01": "版本一致",
        }

    def test_legacy_seed_receipts_without_binding_stay_pending(self):
        # 原有种子在途批次没有结构化版本关联，切换期只能待核实，不得冒充已确认
        MigrationEngine().migrate()
        reconciler = ReceiptReconciler()
        verdicts = {v["receipt_code"]: v["verdict"]
                    for v in reconciler.verify_all_inflight()}
        assert verdicts["CAPT-0001"] == "待核实"
        assert verdicts["CAPT-0002"] == "待核实"

    def test_receipt_mismatch_is_detected(self):
        MigrationEngine().migrate()
        # 人为把在途回执的版本字符串改成同模板的另一个版本
        batch_svc = service_for("capture_batch")
        receipt = next(row for row in batch_svc.list_entries(size=10000)[0]
                       if row.get("批次编号") == "RC-T01-11-01")
        receipt["回执版本字符串"] = "1.10"
        reconciler = ReceiptReconciler()
        verdict = reconciler.verify_receipt(receipt)
        assert verdict["verdict"] == "版本不符"
        assert verdict["bound_version"] == "1.1"

    def test_cross_template_receipt_does_not_satisfy_binding(self):
        MigrationEngine().migrate()
        batch_svc = service_for("capture_batch")
        receipt = next(row for row in batch_svc.list_entries(size=10000)[0]
                       if row.get("批次编号") == "RC-T02-110-01")
        # 版本号同为 1.10，但模板串成 T01：必须判不符，防止跨模板串号
        receipt["回执模板编号"] = "FLOW-T01"
        verdict = ReceiptReconciler().verify_receipt(receipt)
        assert verdict["verdict"] == "版本不符"


class TestAggregateAndHandoff:
    """归集消费实际绑定版本验证与交接保留来源版本、回执。"""

    def test_aggregate_consumes_bound_versions(self):
        MigrationEngine().migrate()
        summary = aggregate_bound_versions([
            "RUN-T01-OLD-01", "RUN-T01-OLD-02", "RUN-T02-OLD-01",
        ])
        assert summary["consumable"] is True
        assert summary["confirmed"] == 3
        assert all(
            all(v["verdict"] == "版本一致" for v in item["receipt_verdicts"])
            for item in summary["items"]
        )

    def test_pending_execution_blocks_consumption(self):
        MigrationEngine().migrate()
        summary = aggregate_bound_versions(["RUN-T02-NEW-01"])
        assert summary["consumable"] is False
        assert summary["pending"] == 1

    def test_handoff_pack_retains_source_versions_and_receipts(self):
        MigrationEngine().migrate()
        pack = build_handoff_pack([
            "RUN-T01-OLD-01", "RUN-T01-OLD-02", "RUN-T02-OLD-01",
        ])
        assert pack["ok"] is True
        refs = {r["run_code"]: r for r in pack["references"]}
        assert refs["RUN-T01-OLD-01"]["source_version_string"] == "1.1"
        assert refs["RUN-T01-OLD-01"]["release_code"] == "VERS-T01-11"
        assert refs["RUN-T01-OLD-01"]["receipt_codes"] == ["RC-T01-11-01"]
        assert refs["RUN-T02-OLD-01"]["template_code"] == "FLOW-T02"
        assert refs["RUN-T02-OLD-01"]["release_code"] == "VERS-T02-110"

    def test_handoff_refused_when_any_pending(self):
        MigrationEngine().migrate()
        pack = build_handoff_pack(["RUN-T01-OLD-01", "RUN-T02-NEW-01"])
        assert pack["ok"] is False
        assert "待核实" in pack["reason"]
