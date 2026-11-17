"""构建期自检：镜像构建、流水线和本地构建都跑这一份。

检查三件事：
1. 环境变量 `MIGRATION_VERSION` 和代码里的注册表版本一致；
2. 迁移所需的字段变更能在示例数据上算通（不落库）；
3. 频率规则/代理/机器人的容量口径来自同一处定义。
"""
from __future__ import annotations

import json
import os
import sys

from app.migrations import LATEST_MIGRATION, MIGRATION_BATCH, MIGRATION_VERSION, SCHEMA_VERSION
from app.services.migration import CALIBER, migration_service
from app.versioning import assert_supported_version


def main() -> int:
    try:
        version = assert_supported_version(os.getenv("MIGRATION_VERSION"))
    except ValueError as exc:
        print(f"[构建失败] {exc}", file=sys.stderr)
        return 1

    preview = migration_service.run_migration(mode="resume", dry_run=True, trigger="构建自检")
    failures = [row for row in preview["预览"] if row.get("失败原因")]
    payload = {
        "迁移版本": version,
        "结构版本": SCHEMA_VERSION,
        "迁移批次": MIGRATION_BATCH,
        "容量口径": CALIBER,
        "字段": LATEST_MIGRATION["rule_fields"] + LATEST_MIGRATION["proxy_fields"],
        "预览规则数": preview["待处理规则数"],
        "可迁移规则数": preview["待处理规则数"] - len(failures),
        "不可迁移规则": [
            {"规则编号": row.get("规则编号"), "原因": row.get("失败原因")} for row in failures
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))

    if CALIBER != LATEST_MIGRATION["capacity_caliber"]:
        print("[构建失败] 容量口径与迁移注册表不一致", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
