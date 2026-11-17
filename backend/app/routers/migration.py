"""频率规则迁移与代理容量复核接口。

路径统一放在 `/api/migration` 下，静态路径先于 `/{...}` 动态路径注册，
避免 `export`、`runs` 这类固定路径被当成整数 ID 解析。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.migration import DECISION_LABELS, migration_service
from app.versioning import assert_supported_version

router = APIRouter(prefix="/api/migration", tags=["频率规则迁移"])


def _page(items: list[dict[str, Any]], total: int, page: int = 1, size: int = 20) -> PageResult[dict]:
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/status", response_model=dict)
def status() -> dict[str, Any]:
    """迁移总览：版本、规则迁移进度、待决策冲突和最近一次运行。"""
    return migration_service.status()


@router.get("/versions", response_model=dict)
def versions() -> dict[str, Any]:
    """迁移版本口径：迁移版本、结构版本、容量口径与批次号。"""
    return migration_service.version_info()


@router.get("/preview", response_model=dict)
def preview(
    mode: str = Query(default="resume", description="resume、restart、redo"),
    region: str | None = Query(default=None, description="按区域缩小迁移范围"),
) -> dict[str, Any]:
    """只计算不落库：先看每条规则会得到什么并发上限、哪条会失败。"""
    try:
        return migration_service.run_migration(
            mode=mode, region=region, dry_run=True, trigger="迁移预览"
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/run", response_model=dict)
def run_migration(payload: EntryPayload) -> dict[str, Any]:
    """执行迁移。mode=resume 断点续跑，restart 从已处理规则之后继续，redo 全部重做。"""
    values = payload.values
    mode = str(values.get("mode") or "resume").strip()
    region = str(values.get("region") or "").strip() or None
    version = values.get("migration_version")
    actor = str(values.get("actor") or "值班管理员").strip() or "值班管理员"
    try:
        assert_supported_version(str(version) if version else None)
        run = migration_service.run_migration(mode=mode, region=region, actor=actor)
        return {"ok": True, "message": run["状态"], "entry": run}
    except ValueError as exc:
        return {"ok": False, "message": str(exc), "entry": None}


@router.get("/runs", response_model=PageResult[dict])
def list_runs(page: int = 1, size: int = 20) -> PageResult[dict]:
    """迁移运行记录：每次运行处理了多少条、失败明细和已处理规则。"""
    items, total = migration_service.list_runs(page=page, size=size)
    return _page(items, total, page, size)


@router.get("/results", response_model=PageResult[dict])
def list_results(
    site: str | None = Query(default=None, description="按站点过滤"),
    status: str | None = Query(default=None, description="已发布、待决策、已复核、已覆盖"),
    active_only: bool = Query(default=True, description="只看每个站点的当前版本"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """采集频率结果；默认只返回当前版本，同一站点不会同时出现两份结果。"""
    items, total = migration_service.list_results(
        site=site, status=status, active_only=active_only, page=page, size=size
    )
    return _page(items, total, page, size)


@router.get("/history", response_model=PageResult[dict])
def list_history(
    site: str | None = Query(default=None, description="按站点过滤"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """迁移历史与回填记录，处理后字段可逐条复核。"""
    items, total = migration_service.list_history(site=site, page=page, size=size)
    return _page(items, total, page, size)


@router.get("/capacity-review", response_model=dict)
def capacity_review(site: str | None = Query(default=None)) -> dict[str, Any]:
    """代理容量复核清单：规则版本、并发上限、失败重试数与待执行计划数量。"""
    return migration_service.capacity_review(site=site)


@router.get("/routing", response_model=dict)
def routing() -> dict[str, Any]:
    """代理路由输出：按采集频率结果给出每个站点的可用容量。"""
    return migration_service.routing()


@router.get("/robot-dispatch", response_model=dict)
def robot_dispatch() -> dict[str, Any]:
    """机器人执行读取的容量口径，与代理路由、容量复核清单同一份计算结果。"""
    return migration_service.robot_dispatch()


@router.get("/capacity-queue", response_model=dict)
def capacity_queue(status: str | None = Query(default=None)) -> dict[str, Any]:
    """代理容量队列：待执行/已取消的并发调整计划。"""
    return migration_service.capacity_queue(status=status)


@router.get("/triggers", response_model=dict)
def triggers() -> dict[str, Any]:
    """业务入口联动重算记录。"""
    return migration_service.triggers()


@router.get("/trace/{rule_id}", response_model=dict)
def trace(rule_id: int) -> dict[str, Any]:
    """追一条规则的数据流：原值/新值、当前结果版本和历史记录。"""
    entry = migration_service.trace_rule(rule_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"频率规则 {rule_id} 不存在或已归档")
    return entry


@router.post("/recalculate", response_model=ActionResult)
def recalculate(payload: EntryPayload) -> ActionResult:
    """重算单条规则的采集频率。submission_id 相同视为重复提交，只发布第一次的结果。"""
    values = payload.values
    try:
        rule_id = int(values.get("rule_id") or 0)
    except (TypeError, ValueError):
        return ActionResult(ok=False, message="rule_id 必须是整数")
    submission_id = str(values.get("submission_id") or "").strip() or None
    entry, message = migration_service.recalculate(
        rule_id, actor=str(values.get("actor") or "值班管理员"), submission_id=submission_id
    )
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/conflicts/{rule_id}/decide", response_model=ActionResult)
def decide_conflict(rule_id: int, payload: EntryPayload) -> ActionResult:
    """处理延迟突增导致的并发上限冲突：保留旧并发上限，或立即采用新代理容量。"""
    decision = str(payload.values.get("decision") or "").strip()
    if decision and decision not in DECISION_LABELS:
        return ActionResult(
            ok=False,
            message=f"未知处理方案：{decision}，可选 {', '.join(DECISION_LABELS)}",
        )
    entry, message = migration_service.decide_conflict(
        rule_id, decision, actor=str(payload.values.get("actor") or "值班管理员")
    )
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
