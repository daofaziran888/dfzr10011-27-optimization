"""迁移与采集频率重算的行为测试。

覆盖：版本口径、断点续跑、失败保留原结果、重复提交、硬边界、冲突两种处理、
容量清单与代理路由/机器人下发的一致性、业务入口联动。
"""
from __future__ import annotations

import os
import unittest

from app.migrations import MIGRATION_BATCH, MIGRATION_VERSION, SCHEMA_VERSION
from app.services import capacity as cap
from app.services.migration import (
    CALIBER,
    DECISION_ADOPT_NEW,
    DECISION_KEEP_OLD,
    MIGRATED,
    RESULT_PENDING,
    RESULT_REVIEWED,
    migration_service,
)
from app.services.proxy import ProxyService
from app.services.ratelimit import RatelimitService
from app.services.robot import RobotService
from app.store import store
from app.versioning import assert_supported_version


class MigrationTestBase(unittest.TestCase):
    def setUp(self) -> None:
        store.reset()
        self.service = migration_service

    def rule(self, rule_no: str) -> dict:
        for row in store.rows("ratelimit"):
            if row.get("规则编号") == rule_no:
                return row
        raise AssertionError(f"找不到规则 {rule_no}")

    def active(self, site: str) -> dict | None:
        return self.service._active_result(site)

    def active_count(self) -> int:
        return len(self.service.list_results(page=1, size=500)[0])


class VersionTest(unittest.TestCase):
    def test_version_matches_registry(self) -> None:
        self.assertEqual(assert_supported_version(MIGRATION_VERSION), MIGRATION_VERSION)
        self.assertEqual(MIGRATION_BATCH, f"batch-{MIGRATION_VERSION}")

    def test_other_version_rejected(self) -> None:
        with self.assertRaises(ValueError):
            assert_supported_version("2026.01.01-old")

    def test_env_only_used_for_comparison(self) -> None:
        os.environ["MIGRATION_VERSION"] = MIGRATION_VERSION
        try:
            self.assertEqual(assert_supported_version(MIGRATION_VERSION), MIGRATION_VERSION)
        finally:
            os.environ.pop("MIGRATION_VERSION", None)


class MigrationRunTest(MigrationTestBase):
    def test_migrate_publishes_one_result_per_site(self) -> None:
        run = self.service.run_migration(mode="resume")
        self.assertEqual(run["处理规则数"], 4)
        self.assertEqual(run["失败规则数"], 1)
        self.assertEqual(run["回填历史数"], 5)
        sites = [row["站点"] for row in self.service.list_results(page=1, size=100)[0]]
        self.assertEqual(len(sites), len(set(sites)))

    def test_resume_does_not_republish(self) -> None:
        self.service.run_migration(mode="resume")
        before = self.active_count()
        again = self.service.run_migration(mode="resume")
        self.assertEqual(again["处理规则数"], 0)
        self.assertEqual(self.active_count(), before)
        restart = self.service.run_migration(mode="restart")
        self.assertEqual(restart["处理规则数"], 0)
        self.assertEqual(self.active_count(), before)

    def test_restart_continues_after_processed_rules(self) -> None:
        # 先只迁一个区域，模拟中途重启
        first = self.service.run_migration(mode="resume", region="华东")
        self.assertEqual(first["处理规则数"], 2)
        self.assertEqual(self.active_count(), 2)
        rest = self.service.run_migration(mode="restart")
        self.assertGreater(rest["跳过规则数"], 0)
        self.assertEqual(self.active_count(), 4)

    def test_redo_replaces_current_version(self) -> None:
        self.service.run_migration(mode="resume")
        version_before = self.active("example.com")["结果版本"]
        self.service.run_migration(mode="redo", region="华东")
        version_after = self.active("example.com")["结果版本"]
        self.assertNotEqual(version_before, version_after)
        self.assertEqual(self.active_count(), 4)

    def test_failed_rule_keeps_original_result(self) -> None:
        self.service.run_migration(mode="resume")
        rule = self.rule("RATE-0001")
        original_version = self.active("example.com")["结果版本"]
        original_interval = rule.get("原始请求间隔")
        rule["请求间隔"] = ""
        result, message = self.service.recalculate(rule["id"])
        self.assertIsNone(result)
        self.assertIn("请求间隔", message)
        self.assertEqual(rule["迁移状态"], "迁移失败")
        self.assertEqual(self.active("example.com")["结果版本"], original_version)
        self.assertEqual(rule.get("原始请求间隔"), original_interval)
        self.assertEqual(rule.get("迁移批次"), MIGRATION_BATCH)

    def test_repeated_failure_not_counted_twice(self) -> None:
        self.service.run_migration(mode="resume")
        rule = self.rule("RATE-0005")
        self.service.run_migration(mode="redo")
        self.assertEqual(rule.get("失败次数"), 1)
        history, _ = self.service.list_history(page=1, size=200)
        failures = [row for row in history if row["类型"] == "迁移失败"]
        self.assertEqual(len(failures), 1)


class HardBoundaryTest(MigrationTestBase):
    def test_interval_never_shortened(self) -> None:
        self.service.run_migration(mode="resume")
        for rule in store.rows("ratelimit"):
            if rule.get("迁移状态") != MIGRATED:
                continue
            original = cap.parse_number(rule.get("原始请求间隔"))
            current = cap.parse_number(rule.get("请求间隔"))
            self.assertLessEqual(original, current)

    def test_quota_not_exceeded(self) -> None:
        self.service.run_migration(mode="resume")
        for result in self.service.list_results(page=1, size=100)[0]:
            requests = cap.projected_daily_requests(
                result["请求间隔"], result["生效并发上限"]
            )
            self.assertLessEqual(requests, result["日配额上限"], result["站点"])

    def test_missing_concurrency_does_not_open_parallelism(self) -> None:
        rule = self.rule("RATE-0003")
        self.assertIsNone(rule.get("并发上限"))
        self.service.run_migration(mode="resume")
        result = self.active("news.example.com")
        self.assertTrue(result["并发上限缺失"])
        self.assertEqual(result["生效并发上限"], 1)
        self.assertEqual(rule["并发上限"], 1)

    def test_new_rule_without_cap_defaults_to_one(self) -> None:
        service = RatelimitService()
        entry, missing = service.create_entry(
            {
                "规则编号": "RATE-9001",
                "目标站点": "example.com",
                "请求间隔": "15",
                "日配额上限": "20000",
            }
        )
        self.assertEqual(missing, [])
        assert entry is not None
        self.assertEqual(entry["并发上限"], 1)
        result, message = self.service.recalculate(entry["id"])
        self.assertIsNotNone(result, message)
        self.assertIn("缺并发上限", result["备注"])

    def test_default_quota_still_checked(self) -> None:
        service = RatelimitService()
        entry, _ = service.create_entry(
            {"规则编号": "RATE-9002", "目标站点": "example.com", "请求间隔": "15"}
        )
        assert entry is not None
        result, message = self.service.recalculate(entry["id"])
        self.assertIsNone(result)
        self.assertIn("日配额", message)
        self.assertEqual(entry["迁移状态"], "迁移失败")


class DedupTest(MigrationTestBase):
    def test_same_submission_publishes_once(self) -> None:
        self.service.run_migration(mode="resume")
        first, _ = self.service.recalculate(1, submission_id="submit-1")
        count = self.active_count()
        second, message = self.service.recalculate(1, submission_id="submit-1")
        self.assertEqual(first["id"], second["id"])
        self.assertIn("重复提交", message)
        self.assertEqual(self.active_count(), count)

    def test_second_recalculation_supersedes_first(self) -> None:
        self.service.run_migration(mode="resume")
        first, _ = self.service.recalculate(1, submission_id="a")
        second, _ = self.service.recalculate(1, submission_id="b")
        self.assertGreater(second["版本序号"], first["版本序号"])
        rows = store.state_rows("frequency_results")
        stale = [row for row in rows if row["id"] == first["id"]]
        self.assertFalse(stale[0]["是否当前版本"])
        self.assertEqual(stale[0]["状态"], "已覆盖")
        current = self.active("example.com")
        self.assertEqual(current["id"], second["id"])


class ConflictTest(MigrationTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.service.run_migration(mode="resume")
        self.proxy_service = ProxyService()

    def conflict_result(self) -> dict:
        result = self.active("job.example.com")
        assert result is not None
        self.assertTrue(result["冲突"])
        self.assertEqual(result["状态"], RESULT_PENDING)
        return result

    def test_conflict_lists_both_options(self) -> None:
        review = self.service.capacity_review(site="job.example.com")
        row = review["items"][0]
        self.assertEqual(len(row["处理方案选项"]), 2)
        codes = {option["代码"] for option in row["处理方案选项"]}
        self.assertEqual(codes, {DECISION_KEEP_OLD, DECISION_ADOPT_NEW})

    def test_keep_old_reports_old_cap_and_more_retries(self) -> None:
        result = self.conflict_result()
        old_cap, new_cap = result["旧并发上限"], result["新并发上限"]
        retries_before = result["失败重试数"]
        entry, message = self.service.decide_conflict(4, DECISION_KEEP_OLD)
        self.assertIsNotNone(entry, message)
        review = self.service.capacity_review(site="job.example.com")["items"][0]
        self.assertEqual(review["生效并发上限"], old_cap)
        self.assertEqual(review["状态"], RESULT_REVIEWED)
        self.assertGreater(review["失败重试数"], retries_before)
        self.assertEqual(review["待执行计划数量"], 0)
        self.assertEqual(review["处理方案"], "保留旧并发上限")
        self.assertEqual(review["规则版本"], f"{SCHEMA_VERSION}.0")

    def test_adopt_new_reports_new_cap_and_pending_plans(self) -> None:
        result = self.conflict_result()
        old_cap, new_cap = result["旧并发上限"], result["新并发上限"]
        entry, message = self.service.decide_conflict(4, DECISION_ADOPT_NEW)
        self.assertIsNotNone(entry, message)
        review = self.service.capacity_review(site="job.example.com")["items"][0]
        self.assertEqual(review["生效并发上限"], new_cap)
        self.assertEqual(review["状态"], RESULT_REVIEWED)
        self.assertEqual(review["待执行计划数量"], max(0, old_cap - new_cap))
        self.assertEqual(review["失败重试数"], result["失败重试数"])
        self.assertEqual(review["处理方案"], "采用新代理容量")

    def test_decision_is_repeatable_but_not_switchable(self) -> None:
        self.service.decide_conflict(4, DECISION_ADOPT_NEW)
        again, message = self.service.decide_conflict(4, DECISION_ADOPT_NEW)
        self.assertIsNotNone(again)
        self.assertIn("无需重复提交", message)
        switched, message = self.service.decide_conflict(4, DECISION_KEEP_OLD)
        self.assertIsNone(switched)
        self.assertIn("已按", message)

    def test_conflict_without_latency_spike_rejected(self) -> None:
        entry, message = self.service.decide_conflict(1, DECISION_KEEP_OLD)
        self.assertIsNone(entry)
        self.assertIn("没有待决策", message)

    def test_decision_survives_business_recalculation(self) -> None:
        self.service.decide_conflict(4, DECISION_KEEP_OLD)
        before = self.active("job.example.com")
        RobotService().create_entry(
            {"记录编号": "ROBO-9001", "流程模板": "job.example.com 采集", "执行节点": "node-1"}
        )
        after = self.active("job.example.com")
        self.assertGreater(after["版本序号"], before["版本序号"])
        self.assertEqual(after["状态"], RESULT_REVIEWED)
        self.assertEqual(after["处理方案"], "保留旧并发上限")
        self.assertEqual(after["生效并发上限"], before["生效并发上限"])


class CapacityCaliberTest(MigrationTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.service.run_migration(mode="resume")
        self.proxy_service = ProxyService()

    def test_capacity_review_matches_proxy_routing(self) -> None:
        review = {row["站点"]: row for row in self.service.capacity_review()["items"]}
        routing = {row["站点"]: row for row in self.service.routing()["items"]}
        dispatch = {row["站点"]: row for row in self.service.robot_dispatch()["items"]}
        self.assertEqual(set(review), set(routing))
        self.assertEqual(set(review), set(dispatch))
        for site, row in review.items():
            self.assertEqual(row["生效并发上限"], routing[site]["生效并发上限"])
            self.assertEqual(dispatch[site]["并发上限"], row["生效并发上限"])
            self.assertEqual(dispatch[site]["可用容量"], routing[site]["可用容量"])
            self.assertLessEqual(routing[site]["可用容量"], row["代理容量"])
            self.assertEqual(dispatch[site]["容量口径"], row["容量口径"])

    def test_review_exposes_rule_version_and_cap(self) -> None:
        payload = self.proxy_service.capacity_review()
        self.assertEqual(payload["容量口径"], CALIBER)
        for row in payload["规则复核"]:
            self.assertEqual(row["规则版本"], f"{SCHEMA_VERSION}.0")
            self.assertEqual(row["结构版本"], SCHEMA_VERSION)
            self.assertIn("并发上限", row["可复核字段"])
            self.assertIn("失败重试数", row["可复核字段"])
            self.assertIn("待执行计划数量", row["可复核字段"])

    def test_capacity_review_endpoint_shapes(self) -> None:
        payload = self.proxy_service.capacity_review()
        for key in ["迁移版本", "容量口径", "规则复核", "代理配置", "待执行计划"]:
            self.assertIn(key, payload)

    def test_proxy_without_cap_keeps_single_unit(self) -> None:
        proxies = store.rows("proxy")
        proxies.append({
            "id": 99,
            "status": "已启用",
            "配置编号": "PROX-9999",
            "代理地址": "10.9.9.9:8080",
            "代理协议": "HTTP",
            "区域标签": "华东",
            "可用比例": "1.0",
            "响应延迟": "100",
        })
        detail = cap.proxy_capacity(proxies[-1])
        self.assertTrue(detail["cap_missing"])
        self.assertEqual(detail["units"], 1)


class BusinessHookTest(MigrationTestBase):
    def test_robot_entry_triggers_recalculation(self) -> None:
        self.service.run_migration(mode="resume")
        before = self.active("example.com")["版本序号"]
        RobotService().create_entry(
            {"记录编号": "ROBO-9100", "流程模板": "example.com 采集", "执行节点": "node-2"}
        )
        after = self.active("example.com")["版本序号"]
        self.assertGreater(after, before)
        triggers = self.service.triggers()["items"]
        self.assertEqual(triggers[0]["业务模块"], "robot")
        self.assertIn("example.com", triggers[0]["影响站点"])

    def test_proxy_action_recalculates_region(self) -> None:
        self.service.run_migration(mode="resume")
        before = self.active("shop.example.com")["版本序号"]
        entry, message = ProxyService().run_action(1, "降级代理")
        self.assertIsNotNone(entry, message)
        after = self.active("shop.example.com")["版本序号"]
        self.assertGreater(after, before)

    def test_unrelated_business_entry_does_not_touch_results(self) -> None:
        self.service.run_migration(mode="resume")
        before = self.active_count()
        from app.services.cluster import ClusterService

        ClusterService().create_entry(
            {"节点编号": "CLUS-9999", "节点地址": "10.0.0.9", "节点角色": "采集"}
        )
        self.assertEqual(self.active_count(), before)

    def test_capacity_queue_has_no_duplicate_plans(self) -> None:
        self.service.run_migration(mode="resume")
        queue_before = self.service.capacity_queue()["待执行总数"]
        self.service.run_migration(mode="redo")
        queue_after = self.service.capacity_queue()["待执行总数"]
        self.assertEqual(queue_before, queue_after)
        plans = [
            row["计划编号"] for row in self.service.capacity_queue()["items"]
            if row["状态"] == "待执行"
        ]
        self.assertEqual(len(plans), len(set(plans)))


if __name__ == "__main__":
    unittest.main()
