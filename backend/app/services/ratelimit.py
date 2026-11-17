"""采集频率控制业务规则：状态流转、字段校验与筛选口径都收在这里。

频率结果由 `services.migration` 统一发布：本服务只负责把规则写进仓库，
迁移/重算走同一套口径，避免同一站点出现两份生效结果。
"""
from __future__ import annotations

from typing import Any

from app.services.migration import MIGRATE_PENDING, migration_service
from app.store import store

MODULE = "ratelimit"
REQUIRED_FIELDS = ["规则编号", "目标站点", "请求间隔"]
STATUS_ORDER = ["草稿", "已生效", "已暂停", "已关闭"]
ACTION_RULES = {"启用规则": "草稿", "暂停规则": "已生效", "关闭规则": "已暂停"}
NEGATIVE_ACTIONS = []
# 新建规则时一并落库的可选字段，缺省值保证旧配置口径不被放大。
OPTIONAL_FIELDS = ["日配额上限", "并发上限", "优先级调整", "生效时段", "区域标签"]


class RatelimitService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rule_no = str(values.get("规则编号") or "").strip()
        if any(str(row.get("规则编号") or "") == rule_no for row in store.rows(MODULE)):
            return None, [f"规则编号 {rule_no}"]
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for field in OPTIONAL_FIELDS:
            if values.get(field) is not None:
                entry[field] = values.get(field)
        if entry.get("并发上限") is None:
            # 缺并发上限的规则按 1 迁移，不因为新字段缺失而放开并发。
            entry["并发上限"] = 1
            entry["并发上限未配置"] = True
        entry["status"] = STATUS_ORDER[0]
        entry["规则状态"] = STATUS_ORDER[0]
        entry["迁移状态"] = MIGRATE_PENDING
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"频率规则 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于采集频率控制可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["规则状态"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"频率规则已{action}"

    # ------------------------------------------------------------ 迁移/重算入口
    def migrate(self, *, mode: str, region: str | None, dry_run: bool, actor: str) -> dict[str, Any]:
        return migration_service.run_migration(
            mode=mode, region=region, dry_run=dry_run, actor=actor
        )

    def recalculate(self, entry_id: int, *, submission_id: str | None, actor: str) -> tuple[dict[str, Any] | None, str]:
        return migration_service.recalculate(
            entry_id, submission_id=submission_id, actor=actor
        )

    def trace(self, entry_id: int) -> dict[str, Any] | None:
        return migration_service.trace_rule(entry_id)

