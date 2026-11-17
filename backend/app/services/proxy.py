"""采集代理路由业务规则：状态流转、字段校验与筛选口径都收在这里。

代理容量口径与频率规则、机器人下发共用 `services.capacity`：
代理变化后按区域联动重算采集频率结果，并按结果输出可用容量。
"""
from __future__ import annotations

from typing import Any

from app.services import capacity as cap
from app.services.migration import migration_service
from app.store import store

MODULE = "proxy"
REQUIRED_FIELDS = ["配置编号", "代理地址", "代理协议"]
STATUS_ORDER = ["草稿", "已启用", "已降级", "已关闭"]
ACTION_RULES = {"启用代理": "草稿", "降级代理": "已启用", "关闭代理": "已降级"}
NEGATIVE_ACTIONS = []
OPTIONAL_FIELDS = ["区域标签", "可用比例", "切换阈值", "响应延迟", "并发上限"]


class ProxyService:
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
            rows = [row for row in rows if keyword in str(row.get("配置编号", ""))]
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
        config_no = str(values.get("配置编号") or "").strip()
        if any(str(row.get("配置编号") or "") == config_no for row in store.rows(MODULE)):
            return None, [f"配置编号 {config_no}（已存在）"]
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for field in OPTIONAL_FIELDS:
            if values.get(field) is not None:
                entry[field] = values.get(field)
        if entry.get("并发上限") is None:
            # 旧配置没有新字段时按 1 计，宁可少发也不放开并发。
            entry["并发上限"] = 1
        latency = cap.parse_number(entry.get("响应延迟"), 0.0) or 0.0
        if entry.get("响应延迟基线") is None:
            entry["响应延迟基线"] = latency
        entry["status"] = STATUS_ORDER[0]
        entry["配置状态"] = STATUS_ORDER[0]
        entry["可用容量"] = cap.proxy_capacity(entry)["units"]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        self._recalculate_region(entry.get("区域标签"), trigger="代理配置登记")
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"代理配置 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于采集代理路由可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        region = entry.get("区域标签")
        entry["status"] = target
        entry["配置状态"] = target
        entry["可用容量"] = cap.proxy_capacity(entry)["units"]
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        recalced = self._recalculate_region(region, trigger=f"代理动作：{action}")
        suffix = f"，联动重算 {recalced['影响规则数']} 条频率规则" if recalced else ""
        return entry, f"代理配置已{action}{suffix}"

    def _recalculate_region(self, region: Any, *, trigger: str) -> dict[str, Any] | None:
        name = str(region or "").strip()
        if not name:
            return None
        return migration_service.recalculate_region(name, trigger=trigger)

    def capacity_review(self) -> dict[str, Any]:
        """代理容量清单：展示新规则版本、并发上限和复核所需的处理字段。"""
        review = migration_service.capacity_review()
        proxies = []
        for row in store.rows(MODULE):
            detail = cap.proxy_capacity(row)
            proxies.append({
                "配置编号": row.get("配置编号"),
                "区域标签": row.get("区域标签"),
                "可用比例": detail["availability"],
                "响应延迟": detail["latency_ms"],
                "并发上限": row.get("并发上限") if not detail["cap_missing"] else "未配置（按 1）",
                "可用容量": detail["units"],
                "配置状态": row.get("配置状态") or row.get("status"),
                "容量口径": review["容量口径"],
            })
        return {
            "迁移版本": review["迁移版本"],
            "容量口径": review["容量口径"],
            "规则复核": review["items"],
            "代理配置": proxies,
            "待执行计划": migration_service.capacity_queue()["按站点"],
        }

    def routing(self) -> dict[str, Any]:
        return migration_service.routing()

    def capacity_queue(self) -> dict[str, Any]:
        return migration_service.capacity_queue()

    def robot_dispatch(self) -> dict[str, Any]:
        return migration_service.robot_dispatch()

