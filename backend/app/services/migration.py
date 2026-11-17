"""频率规则迁移与采集频率重算。

关键口径（与代理路由、机器人下发共用 `services.capacity`）：
- 站点请求间隔与日配额是硬边界：迁移不缩短间隔，并发按配额反推上限；
- 旧配置缺 `并发上限` 时按 1 处理，不因为缺字段而放开并发；
- 同一站点只保留一个当前版本的结果，重算/重复提交不会产生两份生效结果；
- 重算或迁移失败不覆盖原结果，失败规则留在待迁移状态，下次从已处理规则之后继续；
- 迁移完成时若代理延迟突增且新旧并发上限不同，进入冲突待决策，两个方案都可继续。
"""
from __future__ import annotations

import math
import threading
from datetime import datetime
from typing import Any

from app.migrations import LATEST_MIGRATION, MIGRATION_BATCH, MIGRATION_VERSION, SCHEMA_VERSION
from app.services import capacity as cap
from app.store import store
from app.versioning import assert_supported_version

RULE_VERSION = f"{SCHEMA_VERSION}.0"
CALIBER = LATEST_MIGRATION["capacity_caliber"]

RULE_MODULE = "ratelimit"
RESULT_PENDING = "待决策"
RESULT_PUBLISHED = "已发布"
RESULT_REVIEWED = "已复核"
RESULT_SUPERSEDED = "已覆盖"

MIGRATED = "已迁移"
MIGRATE_FAILED = "迁移失败"
MIGRATE_PENDING = "待迁移"

DECISION_KEEP_OLD = "keep_old"
DECISION_ADOPT_NEW = "adopt_new"
DECISION_LABELS = {
    DECISION_KEEP_OLD: "保留旧并发上限",
    DECISION_ADOPT_NEW: "采用新代理容量",
}

# 其他业务入口发生变更时，会按站点联动重算频率规则、容量队列与频率结果。
TRIGGER_MODULES = {
    "crawlconf": ("模板名称", "采集计划"),
    "datasource": ("数据源名称", "目标站点"),
    "verify": ("采集计划", "规则编号"),
    "audit": ("操作对象", "操作详情"),
    "robot": ("流程模板", "执行节点", "站点"),
}

DEFAULT_QUOTA = cap.DEFAULT_DAILY_QUOTA

# 同一站点的重算串行化：并发请求排队执行，只发布最后确认的版本。
_SITE_LOCKS: dict[str, threading.Lock] = {}
_SITE_LOCKS_GUARD = threading.Lock()


class MigrationError(ValueError):
    """规则当前数据无法迁移：记录原因并保留原频率结果。"""


def _site_lock(site: str) -> threading.Lock:
    with _SITE_LOCKS_GUARD:
        lock = _SITE_LOCKS.get(site)
        if lock is None:
            lock = threading.Lock()
            _SITE_LOCKS[site] = lock
        return lock


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


class MigrationService:
    # ---------------------------------------------------------------- 状态存取
    def _dict_state(self, key: str) -> dict[str, Any]:
        value = store.state_value(key)
        if not isinstance(value, dict):
            value = {}
            store.set_state_value(key, value)
        return value

    def _rules(self) -> list[dict[str, Any]]:
        return store.rows(RULE_MODULE)

    def _rule_by_id(self, rule_id: int) -> dict[str, Any] | None:
        for rule in self._rules():
            if int(rule.get("id", 0)) == rule_id and rule.get("规则编号"):
                return rule
        return None

    def _find_rule_by_no(self, rule_no: str) -> dict[str, Any] | None:
        for rule in self._rules():
            if str(rule.get("规则编号") or "") == rule_no:
                return rule
        return None

    def _active_results(self) -> list[dict[str, Any]]:
        return [row for row in store.state_rows("frequency_results") if row.get("是否当前版本")]

    def _active_result(self, site: str) -> dict[str, Any] | None:
        for row in self._active_results():
            if row.get("站点") == site:
                return row
        return None

    def _next_site_sequence(self, site: str) -> int:
        sequences = self._dict_state("site_sequences")
        value = int(sequences.get(site, 0)) + 1
        sequences[site] = value
        return value

    def _retries(self) -> dict[str, int]:
        return self._dict_state("capacity_retries")

    def _add_retries(self, site: str, amount: int) -> int:
        if amount <= 0:
            return int(self._retries().get(site, 0))
        retries = self._retries()
        retries[site] = int(retries.get(site, 0)) + amount
        return retries[site]

    # ---------------------------------------------------------------- 版本信息
    def version_info(self) -> dict[str, Any]:
        return {
            "迁移版本": MIGRATION_VERSION,
            "结构版本": SCHEMA_VERSION,
            "迁移批次": MIGRATION_BATCH,
            "迁移标题": LATEST_MIGRATION["title"],
            "容量口径": CALIBER,
            "规则版本": RULE_VERSION,
            "版本环境变量": assert_supported_version(None),
            "支持版本": [LATEST_MIGRATION["version"]],
        }

    # ---------------------------------------------------------------- 规则解析
    def _resolve_region(self, rule: dict[str, Any]) -> str:
        declared = str(rule.get("区域标签") or "").strip()
        if declared:
            return declared
        site = str(rule.get("目标站点") or "").strip()
        return cap.SITE_REGION_MAP.get(site, "")

    def _rule_plan(self, rule: dict[str, Any]) -> dict[str, Any]:
        """只计算不落库；所有硬边界都在这里校验。"""
        rule_no = str(rule.get("规则编号") or "").strip()
        site = str(rule.get("目标站点") or "").strip()
        if not rule_no:
            raise MigrationError("缺少规则编号，无法确定迁移对象")
        if not site:
            raise MigrationError("缺少目标站点，恢复范围无法按站点归并")

        interval = cap.parse_number(rule.get("请求间隔"), None)
        if interval is None or interval <= 0:
            raise MigrationError("请求间隔缺失或不是正数，不能放宽站点间隔")
        interval = float(interval)

        quota_raw = cap.parse_number(rule.get("日配额上限"), None)
        quota_defaulted = quota_raw is None or quota_raw <= 0
        quota = int(quota_raw) if not quota_defaulted else DEFAULT_QUOTA

        if cap.projected_daily_requests(interval, 1) > quota:
            raise MigrationError(
                f"日配额 {quota} 低于按站点间隔 {interval:g}s 单并发所需请求量，"
                "需先调整配额再迁移"
            )

        region = self._resolve_region(rule)
        region_info = cap.region_capacity(region)
        if region_info["capacity"] <= 0:
            label = region or "全部区域"
            raise MigrationError(f"区域「{label}」没有可用代理容量")

        cap_raw = cap.parse_number(rule.get("并发上限"), None)
        cap_missing = cap_raw is None or cap_raw < 1
        # 重算时以首次迁移前记录的原始上限作为「旧上限」，否则多轮重算会把
        # 上一轮写回的值当成旧口径，冲突再也判定不出来。
        baseline_raw = cap.parse_number(rule.get("原始并发上限"), None)
        if rule.get("原始并发上限缺失") or rule.get("并发上限未配置"):
            # 旧配置从来没有配过并发上限：按 1 计，即使重算写回了 1 也不放大。
            cap_raw = None
            cap_missing = True
        elif baseline_raw is not None and baseline_raw >= 1:
            cap_raw = baseline_raw
            cap_missing = False
        old_concurrency = max(1, int(cap_raw)) if not cap_missing else cap.DEFAULT_PROXY_CAP

        quota_cap = cap.quota_concurrency_cap(quota, interval)
        candidate = min(region_info["capacity"], quota_cap)
        new_concurrency, quota_bounded = cap.fit_concurrency_to_quota(
            candidate, interval_seconds=interval, daily_quota=quota
        )
        # 旧配置缺并发上限时只按 1 执行：迁移本身不放开并发，新容量只用于复核。
        effective_concurrency = 1 if cap_missing else new_concurrency

        latency_spike = bool(region_info["latency_spike"])
        conflict = latency_spike and old_concurrency != new_concurrency and not cap_missing
        if conflict:
            # 冲突未决策前按较小值执行，两种处理方案都保留。
            effective_concurrency = max(1, min(old_concurrency, new_concurrency))

        suggestions = {
            "建议请求间隔": max(int(math.ceil(interval)), cap.interval_from_latency(region_info["latency_ms"])),
            "日请求量预估": cap.projected_daily_requests(interval, effective_concurrency),
            "配额内": cap.projected_daily_requests(interval, effective_concurrency) <= quota,
        }
        return {
            "规则编号": rule_no,
            "站点": site,
            "区域标签": region,
            "区域口径": region_info,
            "请求间隔": interval,
            "日配额上限": quota,
            "配额来源": "默认值" if quota_defaulted else "规则",
            "日配额缺失": quota_defaulted,
            "旧并发上限": old_concurrency,
            "新并发上限": new_concurrency,
            "生效并发上限": effective_concurrency,
            "并发上限缺失": cap_missing,
            "配额约束": quota_bounded,
            "延迟突增": latency_spike,
            "冲突": conflict,
            **suggestions,
        }

    # ---------------------------------------------------------------- 发布结果
    def _publish(
        self,
        plan: dict[str, Any],
        *,
        rule_id: int,
        actor: str,
        trigger: str,
        preserve_decision: bool,
        submission_id: str | None,
    ) -> tuple[dict[str, Any], bool]:
        """写一条频率结果并切换当前版本；返回 (结果, 是否命中重复提交)。"""
        if submission_id:
            submissions = self._dict_state("recompute_submissions")
            existing_id = submissions.get(submission_id)
            if existing_id is not None:
                for row in store.state_rows("frequency_results"):
                    if int(row.get("id", 0)) == int(existing_id):
                        return row, True

        site = plan["站点"]
        sequence = self._next_site_sequence(site)
        prior = self._active_result(site)
        decision_code = ""
        decision_label = ""
        if preserve_decision and prior and prior.get("处理方案代码") in DECISION_LABELS:
            decision_code = str(prior["处理方案代码"])
            decision_label = DECISION_LABELS[decision_code]

        old_cap = int(plan["旧并发上限"])
        new_cap = int(plan["新并发上限"])
        effective = int(plan["生效并发上限"])
        status = RESULT_PENDING if plan["冲突"] else RESULT_PUBLISHED
        note_parts: list[str] = []

        if plan["并发上限缺失"]:
            note_parts.append("旧配置缺并发上限，按 1 执行，需人工复核")
        if plan["日配额缺失"]:
            note_parts.append(f"日配额缺失，按默认 {DEFAULT_QUOTA} 复核")
        if plan["配额约束"]:
            note_parts.append("并发已按日配额硬边界收敛")

        if decision_code:
            status = RESULT_REVIEWED
            effective = old_cap if decision_code == DECISION_KEEP_OLD else new_cap
            if plan["并发上限缺失"]:
                effective = 1
            note_parts.append(f"业务入口变更后沿用原复核方案：{decision_label}")
        elif plan["冲突"]:
            note_parts.append("延迟突增且新旧并发上限不同，暂按较小值执行，等待复核")
        else:
            note_parts.append("迁移完成，等待容量复核")

        if prior is not None:
            prior["是否当前版本"] = False
            prior["已确认"] = False
            prior["状态"] = RESULT_SUPERSEDED
            prior["status"] = RESULT_SUPERSEDED
            prior["被覆盖版本"] = f"v{sequence}"

        pending_plans = self._pending_plan_count(site)
        if status == RESULT_REVIEWED and decision_code == DECISION_ADOPT_NEW:
            self._ensure_plans(
                site, max(0, old_cap - effective), "采用新代理容量，排队下调并发"
            )
        elif status == RESULT_PENDING:
            self._ensure_plans(
                site, max(0, old_cap - effective), "冲突待决策，暂按较小值排队"
            )
        elif status != RESULT_REVIEWED:
            self._ensure_plans(
                site, max(0, old_cap - effective), "迁移按代理容量口径调整并发"
            )
        pending_plans = self._pending_plan_count(site)

        row: dict[str, Any] = {
            "id": _next_id(store.state_rows("frequency_results")),
            "站点": site,
            "site": site,
            "规则ID": rule_id,
            "规则编号": plan["规则编号"],
            "规则版本": RULE_VERSION,
            "rule_version": RULE_VERSION,
            "结构版本": SCHEMA_VERSION,
            "schema_version": SCHEMA_VERSION,
            "容量口径": CALIBER,
            "迁移批次": MIGRATION_BATCH,
            "结果版本": f"v{sequence}",
            "version": f"v{sequence}",
            "版本序号": sequence,
            "seq": sequence,
            "请求间隔": plan["请求间隔"],
            "interval_seconds": plan["请求间隔"],
            "日配额上限": plan["日配额上限"],
            "daily_quota": plan["日配额上限"],
            "配额来源": plan["配额来源"],
            "建议请求间隔": plan["建议请求间隔"],
            "日请求量预估": plan["日请求量预估"],
            "配额内": plan["配额内"],
            "代理容量": plan["区域口径"]["capacity"],
            "proxy_capacity": plan["区域口径"]["capacity"],
            "区域标签": plan["区域标签"] or "全部区域",
            "区域口径": plan["区域口径"]["source"],
            "旧并发上限": old_cap,
            "old_concurrency": old_cap,
            "新并发上限": new_cap,
            "new_concurrency": new_cap,
            "生效并发上限": effective,
            "effective_concurrency": effective,
            "并发上限缺失": plan["并发上限缺失"],
            "延迟系数": round(
                cap.latency_factor(plan["区域口径"]["latency_ms"]), 4
            ),
            "响应延迟": plan["区域口径"]["latency_ms"],
            "响应延迟基线": plan["区域口径"]["baseline_latency_ms"],
            "延迟突增": plan["延迟突增"],
            "冲突": plan["冲突"],
            "conflict": plan["冲突"],
            "冲突状态": status if plan["冲突"] else "无冲突",
            "处理方案": decision_label,
            "处理方案代码": decision_code,
            "decision": decision_code,
            "失败重试数": int(self._retries().get(site, 0)),
            "failure_retries": int(self._retries().get(site, 0)),
            "待执行计划数量": pending_plans,
            "pending_plans": pending_plans,
            "状态": status,
            "status": status,
            "已确认": status != RESULT_SUPERSEDED,
            "是否当前版本": True,
            "触发来源": trigger,
            "操作人": actor,
            "确认时间": _now(),
            "重算时间": _now(),
            "备注": "；".join(note_parts),
        }
        store.state_rows("frequency_results").append(row)
        if submission_id:
            self._dict_state("recompute_submissions")[submission_id] = row["id"]
        return row, False

    # ---------------------------------------------------------------- 队列
    def _queue(self) -> list[dict[str, Any]]:
        return store.state_rows("capacity_queue")

    def _pending_plan_count(self, site: str) -> int:
        return sum(
            1
            for row in self._queue()
            if row.get("站点") == site and row.get("状态") == "待执行"
        )

    def _ensure_plans(self, site: str, desired: int, reason: str) -> None:
        """把站点待执行计划数量校准到 desired；重复调用不会产生重复计划。"""
        desired = max(0, int(desired))
        pending = [
            row
            for row in self._queue()
            if row.get("站点") == site and row.get("状态") == "待执行"
        ]
        if len(pending) > desired:
            for row in pending[desired:]:
                row["状态"] = "已取消"
                row["备注"] = reason
                row["更新时间"] = _now()
        elif len(pending) < desired:
            queue = self._queue()
            existing = sum(1 for row in queue if row.get("站点") == site)
            for index in range(desired - len(pending)):
                queue.append({
                    "id": _next_id(queue),
                    "计划编号": f"PLAN-{site}-{existing + index + 1:04d}",
                    "站点": site,
                    "计划类型": "并发调整",
                    "计划并发": 1,
                    "状态": "待执行",
                    "触发来源": reason,
                    "创建时间": _now(),
                    "更新时间": _now(),
                })

    # ---------------------------------------------------------------- 规则落库
    def _apply_rule(self, rule: dict[str, Any], plan: dict[str, Any], result: dict[str, Any]) -> None:
        before = {
            "规则版本": rule.get("规则版本"),
            "结构版本": rule.get("结构版本"),
            "区域标签": rule.get("区域标签"),
            "并发上限": rule.get("并发上限"),
            "代理容量": rule.get("代理容量"),
            "迁移状态": rule.get("迁移状态"),
        }
        if rule.get("原始并发上限") is None:
            rule["原始并发上限"] = before["并发上限"]
            rule["原始并发上限缺失"] = cap.parse_number(before["并发上限"], None) is None
        if rule.get("原始请求间隔") is None:
            rule["原始请求间隔"] = rule.get("请求间隔")
        if rule.get("原始日配额上限") is None:
            rule["原始日配额上限"] = rule.get("日配额上限")

        rule["规则版本"] = RULE_VERSION
        rule["结构版本"] = SCHEMA_VERSION
        rule["区域标签"] = plan["区域标签"] or "全部区域"
        rule["代理容量"] = plan["区域口径"]["capacity"]
        rule["并发上限"] = result["生效并发上限"]
        rule["迁移状态"] = MIGRATED
        rule["迁移批次"] = MIGRATION_BATCH
        rule["迁移版本"] = MIGRATION_VERSION
        rule["最近迁移时间"] = _now()
        rule.pop("迁移失败原因", None)
        rule["处理明细"] = {
            "规则版本": {"原值": before["规则版本"], "新值": RULE_VERSION},
            "结构版本": {"原值": before["结构版本"], "新值": SCHEMA_VERSION},
            "区域标签": {"原值": before["区域标签"], "新值": rule["区域标签"]},
            "并发上限": {
                "原值": before["并发上限"],
                "新值": rule["并发上限"],
                "旧口径上限": plan["旧并发上限"],
                "代理容量上限": plan["新并发上限"],
            },
            "代理容量": {"原值": before["代理容量"], "新值": rule["代理容量"]},
            "迁移状态": {"原值": before["迁移状态"], "新值": MIGRATED},
            "请求间隔": {
                "原值": rule.get("原始请求间隔"),
                "新值": rule.get("请求间隔"),
                "口径": "保留站点间隔，不因延迟缩短",
            },
            "日配额上限": {
                "原值": rule.get("原始日配额上限"),
                "新值": rule.get("日配额上限"),
                "口径": "硬边界，按间隔反推并发",
            },
        }

    def _mark_failure(self, rule: dict[str, Any], reason: str, *, actor: str, trigger: str) -> None:
        already = (
            rule.get("迁移状态") == MIGRATE_FAILED
            and rule.get("迁移批次") == MIGRATION_BATCH
            and rule.get("迁移失败原因") == reason
        )
        rule["迁移状态"] = MIGRATE_FAILED
        rule["迁移失败原因"] = reason
        rule["迁移批次"] = MIGRATION_BATCH
        rule["最近迁移时间"] = _now()
        if already:
            # 同一批次同样原因重复失败不再累加失败次数和历史，避免重跑刷出多份记录。
            return
        rule["失败次数"] = int(rule.get("失败次数", 0)) + 1
        site = str(rule.get("目标站点") or "")
        if site:
            self._add_retries(site, 1)
        self._append_history({
            "类型": "迁移失败",
            "规则编号": rule.get("规则编号"),
            "站点": site,
            "迁移版本": MIGRATION_VERSION,
            "迁移批次": MIGRATION_BATCH,
            "失败原因": reason,
            "操作人": actor,
            "触发来源": trigger,
        })

    def _append_history(self, values: dict[str, Any]) -> dict[str, Any]:
        rows = store.state_rows("migration_history")
        row = {"id": _next_id(rows), "记录时间": _now()}
        row.update(values)
        rows.append(row)
        return row

    def _backfill(self, rule: dict[str, Any], plan: dict[str, Any], result: dict[str, Any], *, actor: str, redo: bool) -> None:
        self._append_history({
            "类型": "规则迁移",
            "规则编号": plan["规则编号"],
            "站点": plan["站点"],
            "迁移版本": MIGRATION_VERSION,
            "结构版本": SCHEMA_VERSION,
            "迁移批次": MIGRATION_BATCH,
            "结果版本": result["结果版本"],
            "规则版本": RULE_VERSION,
            "请求间隔": plan["请求间隔"],
            "日配额上限": plan["日配额上限"],
            "旧并发上限": plan["旧并发上限"],
            "新并发上限": plan["新并发上限"],
            "生效并发上限": result["生效并发上限"],
            "代理容量": plan["区域口径"]["capacity"],
            "区域标签": result["区域标签"],
            "处理字段": sorted(rule.get("处理明细", {}).keys()),
            "结果状态": result["状态"],
            "重做": redo,
            "操作人": actor,
        })

    # ---------------------------------------------------------------- 单规则迁移
    def migrate_rule(
        self,
        rule: dict[str, Any],
        *,
        actor: str = "值班管理员",
        trigger: str = "规则迁移",
        redo: bool = False,
        preserve_decision: bool = False,
        submission_id: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        site = str(rule.get("目标站点") or "")
        if not site:
            # 站点缺失时无法归并，直接在原规则上记失败，不进站点级锁。
            try:
                self._rule_plan(rule)
            except MigrationError as exc:
                self._mark_failure(rule, str(exc), actor=actor, trigger=trigger)
                return None, str(exc)
        with _site_lock(site or f"rule-{rule.get('id')}"):
            return self._migrate_rule_locked(
                rule,
                actor=actor,
                trigger=trigger,
                redo=redo,
                preserve_decision=preserve_decision,
                submission_id=submission_id,
            )

    def _migrate_rule_locked(
        self,
        rule: dict[str, Any],
        *,
        actor: str,
        trigger: str,
        redo: bool,
        preserve_decision: bool,
        submission_id: str | None,
    ) -> tuple[dict[str, Any] | None, str]:
        try:
            plan = self._rule_plan(rule)
        except MigrationError as exc:
            self._mark_failure(rule, str(exc), actor=actor, trigger=trigger)
            return None, str(exc)
        result, duplicate = self._publish(
            plan,
            rule_id=int(rule.get("id", 0)),
            actor=actor,
            trigger=trigger,
            preserve_decision=preserve_decision,
            submission_id=submission_id,
        )
        if duplicate:
            return result, f"重复提交已忽略，沿用结果 {result['结果版本']}"
        self._apply_rule(rule, plan, result)
        self._backfill(rule, plan, result, actor=actor, redo=redo)
        return result, f"频率规则已迁移，结果版本 {result['结果版本']}"

    # ---------------------------------------------------------------- 迁移运行
    def _is_processed(self, rule: dict[str, Any]) -> bool:
        return (
            rule.get("迁移状态") == MIGRATED
            and int(rule.get("结构版本") or 0) == SCHEMA_VERSION
            and rule.get("迁移批次") == MIGRATION_BATCH
        )

    def _is_settled(self, rule: dict[str, Any]) -> bool:
        """本批次已经处理过（成功或已记录失败）的规则，重启时不再重复处理。"""
        return (
            rule.get("迁移批次") == MIGRATION_BATCH
            and rule.get("迁移状态") in {MIGRATED, MIGRATE_FAILED}
        )

    def _run_scope(self, region: str | None) -> list[dict[str, Any]]:
        rules = [row for row in self._rules() if row.get("规则编号")]
        if region:
            rules = [row for row in rules if self._resolve_region(row) == region]
        return rules

    def run_migration(
        self,
        *,
        mode: str = "resume",
        actor: str = "值班管理员",
        region: str | None = None,
        dry_run: bool = False,
        trigger: str = "迁移任务",
    ) -> dict[str, Any]:
        if mode not in {"resume", "restart", "redo"}:
            raise ValueError(f"未知迁移模式：{mode}，只支持 resume、restart、redo")
        assert_supported_version(None)
        rules = self._run_scope(region)
        processed = [row for row in rules if self._is_processed(row)]
        settled = [row for row in rules if self._is_settled(row)]
        if mode == "redo":
            candidates = rules
        elif mode == "restart":
            # 重启不会把上次已记录失败的规则再跑一遍只是为了避免刷失败次数时，
            # 仍需要留出显式重试通道：失败规则用 redo 或单条重算处理。
            candidates = [row for row in rules if not self._is_settled(row)]
        else:
            candidates = [row for row in rules if not self._is_processed(row)]
        preview = []
        for rule in candidates if dry_run else []:
            try:
                plan = self._rule_plan(rule)
                preview.append({
                    "规则编号": plan["规则编号"],
                    "站点": plan["站点"],
                    "区域标签": plan["区域标签"] or "全部区域",
                    "旧并发上限": plan["旧并发上限"],
                    "新并发上限": plan["新并发上限"],
                    "生效并发上限": plan["生效并发上限"],
                    "冲突": plan["冲突"],
                    "失败原因": "",
                })
            except MigrationError as exc:
                preview.append({
                    "规则编号": rule.get("规则编号"),
                    "站点": rule.get("目标站点"),
                    "失败原因": str(exc),
                })
        if dry_run:
            return {
                "dry_run": True,
                "模式": mode,
                "迁移版本": MIGRATION_VERSION,
                "迁移批次": MIGRATION_BATCH,
                "范围规则数": len(rules),
                "待处理规则数": len(candidates),
                "已处理规则数": len(processed),
                "预览": preview,
            }

        run_rows = store.state_rows("migration_runs")
        run: dict[str, Any] = {
            "id": _next_id(run_rows),
            "运行编号": f"MIGR-{len(run_rows) + 1:04d}",
            "迁移版本": MIGRATION_VERSION,
            "结构版本": SCHEMA_VERSION,
            "迁移批次": MIGRATION_BATCH,
            "容量口径": CALIBER,
            "模式": mode,
            "阶段": "重算频率结果",
            "状态": "执行中",
            "开始时间": _now(),
            "结束时间": "",
            "范围规则数": len(rules),
            "处理规则数": 0,
            "跳过规则数": len(rules) - len(candidates),
            "失败规则数": 0,
            "生成结果数": 0,
            "回填历史数": 0,
            "待复核冲突数": 0,
            "已处理规则编号": [str(row.get("规则编号")) for row in processed],
            "最后处理规则ID": max(
                (int(row.get("id", 0)) for row in processed), default=0
            ),
            "失败明细": [],
            "触发来源": trigger,
            "操作人": actor,
        }
        run_rows.append(run)

        history_before = len(store.state_rows("migration_history"))
        for rule in candidates:
            result, message = self.migrate_rule(
                rule,
                actor=actor,
                trigger=trigger,
                redo=mode == "redo",
                submission_id=None,
            )
            if result is None:
                run["失败规则数"] += 1
                run["失败明细"].append({
                    "规则编号": rule.get("规则编号"),
                    "站点": rule.get("目标站点"),
                    "原因": message,
                })
            else:
                run["处理规则数"] += 1
                run["生成结果数"] += 1
                if result["冲突"] and result["状态"] == RESULT_PENDING:
                    run["待复核冲突数"] += 1
            run["已处理规则编号"].append(str(rule.get("规则编号")))
            run["最后处理规则ID"] = max(
                run["最后处理规则ID"], int(rule.get("id", 0))
            )

        run["阶段"] = "回填迁移历史"
        run["回填历史数"] = len(store.state_rows("migration_history")) - history_before
        run["结束时间"] = _now()
        if run["失败规则数"] == 0:
            run["状态"] = "成功" if run["处理规则数"] else "已是最新"
        elif run["处理规则数"]:
            run["状态"] = "部分完成，可继续迁移"
        else:
            run["状态"] = "失败，可重试"
        run["阶段"] = "完成"
        return run

    # ---------------------------------------------------------------- 重算
    def recalculate(
        self,
        rule_id: int,
        *,
        actor: str = "值班管理员",
        trigger: str = "手动重算",
        submission_id: str | None = None,
        preserve_decision: bool = True,
    ) -> tuple[dict[str, Any] | None, str]:
        rule = self._rule_by_id(rule_id)
        if rule is None:
            return None, f"频率规则 {rule_id} 不存在或已归档"
        return self.migrate_rule(
            rule,
            actor=actor,
            trigger=trigger,
            preserve_decision=preserve_decision,
            submission_id=submission_id,
        )

    # ---------------------------------------------------------------- 冲突决策
    def decide_conflict(
        self,
        rule_id: int,
        decision: str,
        *,
        actor: str = "值班管理员",
    ) -> tuple[dict[str, Any] | None, str]:
        if decision not in DECISION_LABELS:
            return None, f"未知处理方案：{decision}，只支持 {DECISION_KEEP_OLD} 或 {DECISION_ADOPT_NEW}"
        rule = self._rule_by_id(rule_id)
        if rule is None:
            return None, f"频率规则 {rule_id} 不存在或已归档"
        site = str(rule.get("目标站点") or "")
        result = self._active_result(site)
        if result is None:
            return None, f"站点 {site} 还没有已确认的频率结果，请先执行迁移"
        if result.get("处理方案代码") == decision:
            return result, f"站点 {site} 已按「{DECISION_LABELS[decision]}」处理，无需重复提交"
        if result.get("处理方案代码"):
            return None, (
                f"站点 {site} 已按「{result['处理方案']}」处理，"
                "如需换方案请先重做该站点的频率规则"
            )
        if not result.get("冲突"):
            return None, f"站点 {site} 当前没有待决策的容量冲突"

        old_cap = int(result["旧并发上限"])
        new_cap = int(result["新并发上限"])
        if decision == DECISION_KEEP_OLD:
            effective = 1 if result.get("并发上限缺失") else old_cap
            self._ensure_plans(site, 0, "保留旧并发上限，取消待执行计划")
            self._add_retries(site, max(1, old_cap - new_cap))
            note = "保留旧并发上限，失败重试数按新增并发差回填"
        else:
            effective = 1 if result.get("并发上限缺失") else new_cap
            self._ensure_plans(site, max(0, old_cap - effective), "采用新代理容量，排队下调并发")
            note = "采用新代理容量，按并发差生成待执行计划"

        result["生效并发上限"] = effective
        result["effective_concurrency"] = effective
        result["状态"] = RESULT_REVIEWED
        result["status"] = RESULT_REVIEWED
        result["冲突状态"] = RESULT_REVIEWED
        result["处理方案"] = DECISION_LABELS[decision]
        result["处理方案代码"] = decision
        result["decision"] = decision
        result["失败重试数"] = int(self._retries().get(site, 0))
        result["failure_retries"] = result["失败重试数"]
        result["待执行计划数量"] = self._pending_plan_count(site)
        result["pending_plans"] = result["待执行计划数量"]
        result["确认时间"] = _now()
        result["操作人"] = actor
        result["备注"] = f"{result.get('备注') or ''}；{note}".strip("；")

        rule["并发上限"] = effective
        details = rule.get("处理明细")
        if not isinstance(details, dict):
            details = {}
        details["冲突决策"] = {
            "原值": old_cap,
            "新值": effective,
            "方案": DECISION_LABELS[decision],
            "操作人": actor,
        }
        rule["处理明细"] = details
        rule["最近迁移时间"] = _now()

        self._append_history({
            "类型": "容量冲突复核",
            "规则编号": result["规则编号"],
            "站点": site,
            "迁移版本": MIGRATION_VERSION,
            "迁移批次": MIGRATION_BATCH,
            "结果版本": result["结果版本"],
            "旧并发上限": old_cap,
            "新并发上限": new_cap,
            "生效并发上限": effective,
            "处理方案": DECISION_LABELS[decision],
            "失败重试数": result["失败重试数"],
            "待执行计划数量": result["待执行计划数量"],
            "操作人": actor,
        })
        return result, f"站点 {site} 已{DECISION_LABELS[decision]}，生效并发上限 {effective}"

    def recalculate_region(
        self,
        region: str | None,
        *,
        actor: str = "系统联动",
        trigger: str = "代理容量变更",
    ) -> dict[str, Any]:
        """代理容量/延迟变化后，按区域重算受影响的规则，沿用既有复核方案。"""
        affected: list[dict[str, Any]] = []
        for rule in self._rules():
            if rule.get("迁移状态") != MIGRATED or not rule.get("规则编号"):
                continue
            if region and self._resolve_region(rule) != region:
                continue
            result, message = self.migrate_rule(
                rule, actor=actor, trigger=trigger, preserve_decision=True
            )
            affected.append({
                "规则编号": rule.get("规则编号"),
                "站点": rule.get("目标站点"),
                "结果版本": result["结果版本"] if result else "",
                "结果": "成功" if result else "失败",
                "说明": message,
            })
        self._append_history({
            "类型": "代理容量联动重算",
            "区域标签": region or "全部区域",
            "影响规则数": len(affected),
            "迁移版本": MIGRATION_VERSION,
            "迁移批次": MIGRATION_BATCH,
            "操作人": actor,
        })
        return {"区域标签": region or "全部区域", "影响规则数": len(affected), "明细": affected}

    # ---------------------------------------------------------------- 查询
    def list_results(
        self,
        *,
        site: str | None = None,
        status: str | None = None,
        active_only: bool = True,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = list(store.state_rows("frequency_results"))
        if active_only:
            rows = [row for row in rows if row.get("是否当前版本")]
        if site:
            rows = [row for row in rows if site in str(row.get("站点", ""))]
        if status:
            rows = [row for row in rows if row.get("状态") == status]
        rows.sort(key=lambda row: int(row.get("id", 0)), reverse=True)
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def list_history(
        self,
        *,
        site: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = list(store.state_rows("migration_history"))
        if site:
            rows = [row for row in rows if site in str(row.get("站点", ""))]
        rows.sort(key=lambda row: int(row.get("id", 0)), reverse=True)
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def list_runs(self, *, page: int = 1, size: int = 20) -> tuple[list[dict[str, Any]], int]:
        rows = sorted(
            store.state_rows("migration_runs"),
            key=lambda row: int(row.get("id", 0)),
            reverse=True,
        )
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def capacity_review(self, *, site: str | None = None) -> dict[str, Any]:
        rows = self._active_results()
        if site:
            rows = [row for row in rows if site in str(row.get("站点", ""))]
        items: list[dict[str, Any]] = []
        for result in sorted(rows, key=lambda row: str(row.get("站点", ""))):
            region = result.get("区域标签") or ""
            region_key = "" if region == "全部区域" else region
            region_info = cap.region_capacity(region_key)
            review_fields = [
                "规则版本",
                "结构版本",
                "并发上限",
                "代理容量",
                "区域标签",
                "失败重试数",
                "待执行计划数量",
            ]
            old_cap = int(result["旧并发上限"])
            new_cap = int(result["新并发上限"])
            current_retries = int(result["失败重试数"])
            current_pending = int(result["待执行计划数量"])
            pending_conflict = bool(result["冲突"]) and not result.get("处理方案代码")
            options = []
            if pending_conflict:
                # 两种处理都能继续：把各自落到的清单字段先算出来，复核时可直接对照。
                options = [
                    {
                        "方案": DECISION_LABELS[DECISION_KEEP_OLD],
                        "代码": DECISION_KEEP_OLD,
                        "生效并发上限": old_cap,
                        "并发上限": old_cap,
                        "失败重试数": current_retries + max(1, old_cap - new_cap),
                        "待执行计划数量": 0,
                        "说明": "保留旧并发上限，失败重试数增加",
                    },
                    {
                        "方案": DECISION_LABELS[DECISION_ADOPT_NEW],
                        "代码": DECISION_ADOPT_NEW,
                        "生效并发上限": new_cap,
                        "并发上限": new_cap,
                        "失败重试数": current_retries,
                        "待执行计划数量": max(0, old_cap - new_cap),
                        "说明": "采用新代理容量，生成待执行计划",
                    },
                ]
            items.append({
                "规则ID": result["规则ID"],
                "规则编号": result["规则编号"],
                "目标站点": result["站点"],
                "站点": result["站点"],
                "区域标签": region,
                "规则版本": result["规则版本"],
                "结构版本": result["结构版本"],
                "容量口径": result["容量口径"],
                "迁移批次": result["迁移批次"],
                "结果版本": result["结果版本"],
                "请求间隔": result["请求间隔"],
                "日配额上限": result["日配额上限"],
                "代理容量": region_info["capacity"],
                "可用代理数": region_info["active_count"],
                "可用比例": region_info["availability_avg"],
                "响应延迟": region_info["latency_ms"],
                "响应延迟基线": region_info["baseline_latency_ms"],
                "延迟突增": region_info["latency_spike"],
                "旧并发上限": result["旧并发上限"],
                "新并发上限": result["新并发上限"],
                "生效并发上限": result["生效并发上限"],
                "失败重试数": result["失败重试数"],
                "待执行计划数量": result["待执行计划数量"],
                "冲突": result["冲突"],
                "冲突状态": result["冲突状态"],
                "复核状态": result["状态"],
                "状态": result["状态"],
                "处理方案": result["处理方案"],
                "处理方案代码": result["处理方案代码"],
                "处理方案选项": options,
                "可复核字段": review_fields,
                "最近迁移时间": result["确认时间"],
                "old_concurrency": result["旧并发上限"],
                "new_concurrency": result["新并发上限"],
                "effective_concurrency": result["生效并发上限"],
                "rule_version": result["规则版本"],
                "status": result["状态"],
                "decision": result["处理方案代码"],
                "failure_retries": result["失败重试数"],
                "pending_plans": result["待执行计划数量"],
            })
        return {
            "迁移版本": MIGRATION_VERSION,
            "结构版本": SCHEMA_VERSION,
            "容量口径": CALIBER,
            "total": len(items),
            "items": items,
        }

    def routing(self) -> dict[str, Any]:
        """代理路由输出：按采集频率结果给出每个站点的可用容量，供机器人读取。"""
        items: list[dict[str, Any]] = []
        for result in sorted(self._active_results(), key=lambda row: str(row.get("站点", ""))):
            if result["状态"] == RESULT_SUPERSEDED:
                continue
            available = max(0, min(int(result["代理容量"]), int(result["生效并发上限"])))
            items.append({
                "站点": result["站点"],
                "site": result["站点"],
                "规则编号": result["规则编号"],
                "区域标签": result["区域标签"],
                "可用容量": available,
                "available_capacity": available,
                "生效并发上限": result["生效并发上限"],
                "effective_concurrency": result["生效并发上限"],
                "请求间隔": result["请求间隔"],
                "interval_seconds": result["请求间隔"],
                "日配额上限": result["日配额上限"],
                "daily_quota": result["日配额上限"],
                "结果版本": result["结果版本"],
                "version": result["结果版本"],
                "规则版本": result["规则版本"],
                "容量口径": result["容量口径"],
                "响应延迟": result["响应延迟"],
                "来源": "采集频率结果",
                "状态": result["状态"],
            })
        return {
            "迁移版本": MIGRATION_VERSION,
            "容量口径": CALIBER,
            "total": len(items),
            "items": items,
        }

    def capacity_queue(self, *, status: str | None = None) -> dict[str, Any]:
        rows = list(self._queue())
        if status:
            rows = [row for row in rows if row.get("状态") == status]
        pending_total = sum(1 for row in self._queue() if row.get("状态") == "待执行")
        sites: dict[str, int] = {}
        for row in self._queue():
            if row.get("状态") == "待执行":
                sites[row["站点"]] = sites.get(row["站点"], 0) + 1
        return {
            "迁移版本": MIGRATION_VERSION,
            "total": len(rows),
            "待执行总数": pending_total,
            "按站点": sites,
            "items": sorted(rows, key=lambda row: int(row.get("id", 0)), reverse=True),
        }

    def robot_dispatch(self) -> dict[str, Any]:
        routing = self.routing()
        pending_by_site = self.capacity_queue()["按站点"]
        items = []
        for route in routing["items"]:
            site = route["站点"]
            items.append({
                "计划编号": f"DISP-{site}",
                "站点": site,
                "区域标签": route["区域标签"],
                "并发上限": route["生效并发上限"],
                "可用容量": route["可用容量"],
                "请求间隔": route["请求间隔"],
                "日配额上限": route["日配额上限"],
                "结果版本": route["结果版本"],
                "规则版本": route["规则版本"],
                "待执行计划数量": pending_by_site.get(site, 0),
                "容量口径": CALIBER,
                "可执行": route["可用容量"] > 0 and not route["状态"] == RESULT_PENDING,
            })
        return {
            "迁移版本": MIGRATION_VERSION,
            "容量口径": CALIBER,
            "total": len(items),
            "items": items,
            "待执行计划总数": sum(pending_by_site.values()),
        }

    def trace_rule(self, rule_id: int) -> dict[str, Any] | None:
        rule = self._rule_by_id(rule_id)
        if rule is None:
            return None
        site = str(rule.get("目标站点") or "")
        active = self._active_result(site)
        history, _ = self.list_history(site=site, page=1, size=50)
        return {
            "规则编号": rule.get("规则编号"),
            "站点": site,
            "迁移状态": rule.get("迁移状态", MIGRATE_PENDING),
            "规则版本": rule.get("规则版本"),
            "结构版本": rule.get("结构版本"),
            "处理明细": rule.get("处理明细", {}),
            "当前结果": active,
            "历史记录": history,
            "迁移版本": MIGRATION_VERSION,
        }

    # ---------------------------------------------------------------- 业务联动
    def on_business_entry_changed(
        self,
        module: str,
        entry: dict[str, Any],
        *,
        action: str = "登记",
    ) -> dict[str, Any] | None:
        """其他业务入口变化后，联动重算频率规则、容量队列和采集频率结果。"""
        fields = TRIGGER_MODULES.get(module)
        if fields is None:
            return None
        hints = [str(entry.get(field) or "") for field in fields if entry.get(field)]
        targets = [
            rule
            for rule in self._rules()
            if rule.get("迁移状态") == MIGRATED
            and rule.get("规则编号")
            and (
                not hints
                or any(
                    hint in str(rule.get("目标站点") or "")
                    or hint in str(rule.get("规则编号") or "")
                    or str(rule.get("目标站点") or "") in hint
                    for hint in hints
                )
            )
        ]
        affected = []
        for rule in targets:
            result, message = self.migrate_rule(
                rule,
                actor="系统联动",
                trigger=f"{module}:{action}",
                preserve_decision=True,
            )
            affected.append({
                "规则编号": rule.get("规则编号"),
                "站点": rule.get("目标站点"),
                "结果版本": result["结果版本"] if result else "",
                "结果": "成功" if result else "失败",
                "说明": message,
            })
        event = self._append_history({
            "类型": "业务联动重算",
            "业务模块": module,
            "操作动作": action,
            "影响站点": [item["站点"] for item in affected],
            "影响规则数": len(affected),
            "迁移版本": MIGRATION_VERSION,
            "迁移批次": MIGRATION_BATCH,
            "操作人": "系统联动",
        })
        triggers = store.state_rows("migration_triggers")
        triggers.append({
            "id": _next_id(triggers),
            "业务模块": module,
            "操作动作": action,
            "影响规则数": len(affected),
            "影响站点": event["影响站点"],
            "结果明细": affected,
            "时间": _now(),
        })
        return {
            "业务模块": module,
            "操作动作": action,
            "影响规则数": len(affected),
            "影响站点": event["影响站点"],
            "明细": affected,
        }

    def triggers(self) -> dict[str, Any]:
        rows = sorted(
            store.state_rows("migration_triggers"),
            key=lambda row: int(row.get("id", 0)),
            reverse=True,
        )
        return {"total": len(rows), "items": rows}

    def status(self) -> dict[str, Any]:
        rules = [row for row in self._rules() if row.get("规则编号")]
        migrated = sum(1 for row in rules if row.get("迁移状态") == MIGRATED)
        failed = sum(1 for row in rules if row.get("迁移状态") == MIGRATE_FAILED)
        active = self._active_results()
        conflicts = sum(1 for row in active if row.get("冲突") and not row.get("处理方案代码"))
        runs, _ = self.list_runs(page=1, size=1)
        return {
            **self.version_info(),
            "规则总数": len(rules),
            "已迁移规则": migrated,
            "迁移失败规则": failed,
            "待迁移规则": len(rules) - migrated,
            "当前结果数": len(active),
            "待决策冲突": conflicts,
            "待执行计划": self.capacity_queue()["待执行总数"],
            "历史记录数": len(store.state_rows("migration_history")),
            "最近运行": runs[0] if runs else None,
            "结果": self.list_results(page=1, size=5)[0],
        }


migration_service = MigrationService()
