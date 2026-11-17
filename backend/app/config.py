"""运行配置：端口、跨域、运行环境、迁移版本。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from app.migrations import MIGRATION_VERSION, SCHEMA_VERSION


@dataclass(frozen=True)
class Settings:
    app_name: str = "分布式采集调度引擎"
    env: str = "local"
    port: int = 8000
    allowed_origins: list[str] = field(
        default_factory=lambda: [
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ]
    )
    page_size_default: int = 20
    page_size_max: int = 200
    # 迁移版本口径：构建、部署、本地示例和流水线都从同一环境变量读取。
    migration_version: str = MIGRATION_VERSION
    schema_version: int = SCHEMA_VERSION
    # 启动时自动续跑迁移：只处理还没处理的规则，重跑不会产生第二份结果。
    auto_migrate: bool = os.getenv("AUTO_MIGRATE", "0").strip() in {"1", "true", "True"}


settings = Settings()
