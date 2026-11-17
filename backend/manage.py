"""迁移运维命令：本地开发、构建校验和上线流水线共用同一入口。

用法（在 backend 目录下）：
    python3 manage.py version           # 打印迁移版本与容量口径
    python3 manage.py check             # 校验环境变量与代码版本一致
    python3 manage.py preview           # 只计算不落库
    python3 manage.py migrate           # 断点续跑迁移（默认）
    python3 manage.py migrate --mode redo --region 华东
    python3 manage.py verify            # 校验规则字段、结果唯一性和容量口径
    python3 manage.py backfill          # 只回填历史记录，不重算结果

所有命令都使用 `services.migration` 的同一套逻辑，没有单独的迁移实现。
"""
from __future__ import annotations

import argparse
import json
import sys

from app.migrations import LATEST_MIGRATION, MIGRATION_BATCH, MIGRATION_VERSION, SCHEMA_VERSION
from app.services import capacity as cap
from app.services.migration import (
    CALIBER,
    MIGRATED,
    RESULT_SUPERSEDED,
    migration_service,
)
from app.store import store
from app.versioning import assert_supported_version


def _print(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_version(_: argparse.Namespace) -> int:
    _print({
        "迁移版本": MIGRATION_VERSION,
        "结构版本": SCHEMA_VERSION,
        "迁移批次": MIGRATION_BATCH,
        "迁移标题": LATEST_MIGRATION["title"],
        "容量口径": CALIBER,
        "规则版本": f"{SCHEMA_VERSION}.0",
        "运行环境": assert_supported_version(None),
    })
    return 0


def cmd_check(_: argparse.Namespace) -> int:
    try:
        version = assert_supported_version(None)
    except ValueError as exc:
        print(f"[失败] {exc}", file=sys.stderr)
        return 2
    print(f"[通过] 迁移版本 {version}，结构版本 {SCHEMA_VERSION}，批次 {MIGRATION_BATCH}")
    return 0


def cmd_preview(args: argparse.Namespace) -> int:
    _print(migration_service.run_migration(
        mode=args.mode, region=args.region, dry_run=True, trigger="命令行预览"
    ))
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    run = migration_service.run_migration(
        mode=args.mode, region=args.region, actor=args.actor, trigger="命令行迁移"
    )
    _print(run)
    if run["失败规则数"] == 0:
        return 0
    if args.allow_failures:
        # 示例数据里有一条故意无法迁移的规则，演示和流水线允许带失败继续，
        # 但失败明细仍在输出里，不能被忽略。
        print(
            f"[提示] {run['失败规则数']} 条规则迁移失败，已保留原频率结果，详见失败明细",
            file=sys.stderr,
        )
        return 0
    return 1


def cmd_backfill(args: argparse.Namespace) -> int:
    """对已迁移规则补齐历史记录；不重新发布频率结果。"""
    rules = [row for row in store.rows("ratelimit") if row.get("迁移状态") == MIGRATED]
    if args.region:
        rules = [row for row in rules if row.get("区域标签") == args.region]
    count = 0
    for rule in rules:
        result = migration_service._active_result(str(rule.get("目标站点") or ""))
        if result is None:
            continue
        migration_service._append_history({
            "类型": "历史回填",
            "规则编号": rule.get("规则编号"),
            "站点": rule.get("目标站点"),
            "迁移版本": MIGRATION_VERSION,
            "迁移批次": MIGRATION_BATCH,
            "结果版本": result["结果版本"],
            "规则版本": rule.get("规则版本"),
            "生效并发上限": rule.get("并发上限"),
            "处理字段": sorted((rule.get("处理明细") or {}).keys()),
            "操作人": args.actor,
        })
        count += 1
    _print({"回填规则数": count, "迁移版本": MIGRATION_VERSION, "迁移批次": MIGRATION_BATCH})
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    # 仓库是进程内示例数据，单独起一个进程时看不到上一个进程的迁移状态，
    # 所以校验前先在当前进程里续跑一遍迁移（幂等，不会产生第二份结果）。
    run = None
    if not getattr(args, "no_migrate", False):
        run = migration_service.run_migration(mode="resume", trigger="命令行校验前续跑")
    problems: list[str] = []
    rules = [row for row in store.rows("ratelimit") if row.get("规则编号")]

    for rule in rules:
        if rule.get("迁移状态") != MIGRATED:
            continue
        detail = rule.get("处理明细")
        if not isinstance(detail, dict) or not detail:
            problems.append(f"{rule['规则编号']} 缺少处理后字段记录，无法复核")
            continue
        for field in LATEST_MIGRATION["rule_fields"]:
            if field not in detail:
                problems.append(f"{rule['规则编号']} 处理明细缺字段：{field}")
        interval = cap.parse_number(rule.get("请求间隔"), None)
        original_interval = cap.parse_number(rule.get("原始请求间隔"), None)
        if interval is None or original_interval is None:
            problems.append(f"{rule['规则编号']} 请求间隔无法解析")
        elif interval < original_interval:
            problems.append(f"{rule['规则编号']} 请求间隔被缩短：{original_interval} → {interval}")
        quota = cap.parse_number(rule.get("日配额上限"), None)
        if quota is None or interval is None:
            problems.append(f"{rule['规则编号']} 日配额无法解析")
        elif cap.projected_daily_requests(interval, int(cap.parse_number(rule.get("并发上限"), 1) or 1)) > quota:
            problems.append(f"{rule['规则编号']} 迁移后并发超出日配额")

    active = {}
    for row in store.state_rows("frequency_results"):
        if not row.get("是否当前版本"):
            continue
        site = row.get("站点")
        if site in active:
            problems.append(f"站点 {site} 出现两份当前版本结果：{active[site]} 与 {row.get('结果版本')}")
        active[site] = row.get("结果版本")
        if row.get("容量口径") != CALIBER:
            problems.append(f"站点 {site} 结果容量口径不一致：{row.get('容量口径')}")
        if row.get("规则版本") != f"{SCHEMA_VERSION}.0":
            problems.append(f"站点 {site} 结果规则版本不一致：{row.get('规则版本')}")

    superseded = [
        row for row in store.state_rows("frequency_results")
        if row.get("状态") == RESULT_SUPERSEDED and row.get("是否当前版本")
    ]
    if superseded:
        problems.append(f"有 {len(superseded)} 份已覆盖结果仍被标为当前版本")

    for proxy in store.rows("proxy"):
        detail = cap.proxy_capacity(proxy)
        if detail["cap_missing"] and detail["units"] > 1:
            problems.append(f"{proxy.get('配置编号')} 缺并发上限却算出了 {detail['units']} 个容量")

    _print({
        "迁移版本": MIGRATION_VERSION,
        "结构版本": SCHEMA_VERSION,
        "检查规则数": len(rules),
        "当前结果站点数": len(active),
        "续跑状态": (run or {}).get("状态", "未续跑"),
        "问题数": len(problems),
        "问题": problems,
    })
    return 0 if not problems else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="频率规则迁移运维命令")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("version", help="打印迁移版本与容量口径")
    sub.add_parser("check", help="校验环境变量与代码版本一致")

    preview = sub.add_parser("preview", help="只计算不落库")
    preview.add_argument("--mode", default="resume", choices=["resume", "restart", "redo"])
    preview.add_argument("--region", default=None)

    migrate = sub.add_parser("migrate", help="执行迁移（默认断点续跑）")
    migrate.add_argument("--mode", default="resume", choices=["resume", "restart", "redo"])
    migrate.add_argument("--region", default=None)
    migrate.add_argument("--actor", default="命令行")
    migrate.add_argument(
        "--allow-failures",
        action="store_true",
        help="有规则无法迁移时仍返回 0（失败明细照常输出）",
    )

    backfill = sub.add_parser("backfill", help="只回填历史记录")
    backfill.add_argument("--region", default=None)
    backfill.add_argument("--actor", default="命令行")

    verify = sub.add_parser("verify", help="校验迁移结果")
    verify.add_argument("--no-migrate", action="store_true", help="不先续跑迁移，只校验当前进程状态")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "version": cmd_version,
        "check": cmd_check,
        "preview": cmd_preview,
        "migrate": cmd_migrate,
        "backfill": cmd_backfill,
        "verify": cmd_verify,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
