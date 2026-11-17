"""版本一致性自检：所有构建/部署入口必须声明同一个迁移版本。

CI、本地和容器都跑这个脚本，任何一处漏改都会在合并前失败，而不是上线后才发现
镜像、compose 和代码各跑一套口径。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from app.migrations import MIGRATION_VERSION

ROOT = Path(__file__).resolve().parents[2]

FILES = [
    ROOT / "Makefile",
    ROOT / "docker-compose.yml",
    ROOT / ".env.example",
    ROOT / "backend" / "Dockerfile",
    ROOT / "frontend" / "Dockerfile",
    ROOT / ".github" / "workflows" / "ci.yml",
]

# 每个文件允许出现的写法：`${MIGRATION_VERSION:-版本}`、`${MIGRATION_VERSION}`、
# `KEY=版本`、`KEY: 版本`、`--build-arg MIGRATION_VERSION=版本`。
PLACEHOLDER = re.compile(r"\$\{MIGRATION_VERSION(?::-[^}]*)?\}")
DECLARED = re.compile(
    r"MIGRATION_VERSION\s*(?:\?=|:=|=|:|:-)\s*\"?'?([0-9][0-9A-Za-z.\-]*)\"?'?"
)


def scan(path: Path) -> list[str]:
    problems: list[str] = []
    if not path.exists():
        return [f"缺少文件：{path.relative_to(ROOT)}"]
    text = path.read_text(encoding="utf-8")
    declared = sorted(set(DECLARED.findall(text)))
    for value in declared:
        if value != MIGRATION_VERSION:
            problems.append(
                f"{path.relative_to(ROOT)} 声明 {value}，代码注册表是 {MIGRATION_VERSION}"
            )
    if not declared and not PLACEHOLDER.search(text) and "MIGRATION_VERSION" in text:
        problems.append(f"{path.relative_to(ROOT)} 引用了 MIGRATION_VERSION 但没有固定版本")
    return problems


def main() -> int:
    problems: list[str] = []
    for path in FILES:
        problems.extend(scan(path))
    print(json.dumps({
        "迁移版本": MIGRATION_VERSION,
        "检查文件": [str(path.relative_to(ROOT)) for path in FILES],
        "问题数": len(problems),
        "问题": problems,
    }, ensure_ascii=False, indent=2))
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
