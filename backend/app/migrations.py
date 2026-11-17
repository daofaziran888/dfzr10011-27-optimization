"""迁移注册表：所有版本的字段变更和容量口径都登记在这里。

`versioning`、迁移服务、构建脚本和容器镜像都从这一份读取，避免出现
「代码是新口径、部署还带旧版本」的情况。新增一版时只在末尾追加，不改历史项。
"""
from __future__ import annotations

from typing import Any

CAPACITY_CALIBER = "capacity-v2:floor(并发上限×可用比例×延迟系数)"

MIGRATION_REGISTRY: list[dict[str, Any]] = [
    {
        "version": "2026.11.17-proxy-capacity-v2",
        "schema_version": 2,
        "title": "频率规则接入代理容量与延迟系数",
        "description": (
            "把采集频率规则按站点迁移到代理容量口径：补齐规则版本、结构版本、区域标签、"
            "代理容量和迁移状态字段，按可用比例与响应延迟重算并发上限，保留站点间隔和"
            "日配额硬边界，并回填迁移历史。"
        ),
        # 频率规则需要补齐/回填的字段
        "rule_fields": [
            "规则版本",
            "结构版本",
            "区域标签",
            "并发上限",
            "代理容量",
            "迁移状态",
        ],
        # 代理配置需要补齐的字段（旧数据没有并发上限时不得放大并发）
        "proxy_fields": ["并发上限", "响应延迟基线"],
        "capacity_caliber": CAPACITY_CALIBER,
        "backfill": "迁移存量频率规则，按规则逐条回填历史记录",
    }
]

LATEST_MIGRATION: dict[str, Any] = MIGRATION_REGISTRY[-1]
MIGRATION_VERSION: str = LATEST_MIGRATION["version"]
SCHEMA_VERSION: int = int(LATEST_MIGRATION["schema_version"])
MIGRATION_BATCH: str = f"batch-{MIGRATION_VERSION}"


def known_versions() -> list[str]:
    return [item["version"] for item in MIGRATION_REGISTRY]


def lookup(version: str) -> dict[str, Any] | None:
    for item in MIGRATION_REGISTRY:
        if item["version"] == version:
            return item
    return None
