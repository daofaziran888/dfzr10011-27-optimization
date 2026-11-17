"""分布式采集调度引擎 后端服务入口。

启动：uvicorn app.main:app --host 127.0.0.1 --port 8000
健康检查：GET /api/health
"""
from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import ROUTERS
from app.services.migration import migration_service
from app.store import store

# 启动前先确认部署环境和代码是同一迁移版本，不一致直接起不来，
# 避免用旧口径把新代理容量算错。
from app.versioning import assert_supported_version

assert_supported_version(os.getenv("MIGRATION_VERSION"))

app = FastAPI(title="分布式采集调度引擎", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for module in ROUTERS:
    app.include_router(module.router)

if settings.auto_migrate:
    # 重启后从已处理规则之后继续；已处理完就直接跳过，不会重复发布结果。
    migration_service.run_migration(mode="restart", trigger="服务启动")


@app.get("/api/health")
def health() -> dict[str, object]:
    """健康检查：确认服务已经监听、示例数据已经就绪。"""
    return {
        "ok": True,
        "app": settings.app_name,
        "modules": len(store.module_names()),
        "migration_version": migration_service.version_info()["迁移版本"],
        "schema_version": settings.schema_version,
    }


@app.get("/api/overview")
def overview() -> dict[str, object]:
    """运营概览：把各业务模块的待处理量汇总成看板卡片。"""
    return store.overview()
