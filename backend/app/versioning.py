"""迁移版本口径：后端、构建、部署、本地示例和上线流水线共用这一份。

版本号和批次号统一从 `app.migrations` 的注册表读取；`MIGRATION_VERSION`
环境变量只用于确认部署环境和本地是同一版本，不一致直接报错而不是静默混跑。
"""
from __future__ import annotations

from app.migrations import LATEST_MIGRATION, MIGRATION_BATCH, MIGRATION_VERSION, SCHEMA_VERSION

__all__ = [
    "MIGRATION_VERSION",
    "MIGRATION_BATCH",
    "SCHEMA_VERSION",
    "MIGRATION_TITLE",
    "resolve_migration_version",
    "assert_supported_version",
]

MIGRATION_TITLE: str = LATEST_MIGRATION["title"]


def resolve_migration_version() -> str:
    """返回代码里登记的迁移版本；环境变量只用来比对，不覆盖代码版本。

    如果允许环境变量覆盖，旧环境设了 `MIGRATION_VERSION=旧值` 就会连校验一起骗过，
    变成静默用旧口径跑新代码。
    """
    return MIGRATION_VERSION


def assert_supported_version(version: str | None) -> str:
    """只接受当前版本；其他版本必须显式升级，不允许静默混用两套口径。"""
    current = resolve_migration_version()
    if version is None or not str(version).strip():
        return current
    if str(version).strip() != current:
        supported = ", ".join(_supported())
        raise ValueError(
            f"迁移版本不一致：当前 {current}，收到 {version}；支持的版本：{supported}。"
            "请先升级构建、部署和本地环境到同一 MIGRATION_VERSION。"
        )
    return current


def _supported() -> list[str]:
    from app.migrations import known_versions

    return known_versions()
