"""代理容量口径：迁移计算、代理路由、机器人读取共用这一套公式。

同一套口径同时被三处引用，避免迁移清单算一个数、代理路由又按另一个数下发：
- 单代理可用容量 = floor(并发上限 × 可用比例 × 延迟系数)
- 区域可用容量 = 区域内未关闭代理的可用容量之和
- 区域延迟取可用代理响应延迟的加权平均，基线取自 `响应延迟基线`
"""
from __future__ import annotations

import math
import re
from typing import Any

from app.store import store

DEFAULT_INTERVAL_SECONDS = 10.0
DEFAULT_DAILY_QUOTA = 1000
DEFAULT_PROXY_CAP = 1
LATENCY_FREE_MS = 300.0
LATENCY_FLOOR_MS = 3000.0
LATENCY_FACTOR_FLOOR = 0.2
LATENCY_SPIKE_RATIO = 1.5
LATENCY_SPIKE_DELTA_MS = 100.0

# 规则没有显式区域标签时按站点后缀归到代理区域；命中不到就走全区域口径。
SITE_REGION_MAP = {
    "example.com": "华东",
    "shop.example.com": "华东",
    "news.example.com": "华南",
    "job.example.com": "华北",
    "blocked.example.com": "华南",
}
SITE_REGION_SUFFIX = {
    "example.com": "华东",
}


def parse_number(value: Any, default: float | None = None) -> float | None:
    """把页面上的数字、带单位文本、百分比都转换成浮点数；空值返回默认值。"""
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text or text in {"-", "—", "--", "无", "null", "None"}:
        return default
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if match is None:
        return default
    return float(match.group())


def parse_ratio(value: Any, default: float = 0.0) -> float:
    """可用比例兼容 0.9、90%、90 三种写法，超出 0~1 时按百分比换算。"""
    if isinstance(value, str) and value.strip().endswith("%"):
        number = parse_number(value, None)
        ratio = (number or 0.0) / 100.0
    else:
        number = parse_number(value, None)
        if number is None:
            return default
        ratio = number / 100.0 if number > 1 else number
    return min(max(ratio, 0.0), 1.0)


def latency_factor(latency_ms: float) -> float:
    """延迟越高，单代理能承载的并发越小；300ms 内不惩罚，3000ms 降到 20%。"""
    if latency_ms <= LATENCY_FREE_MS:
        return 1.0
    if latency_ms >= LATENCY_FLOOR_MS:
        return LATENCY_FACTOR_FLOOR
    span = LATENCY_FLOOR_MS - LATENCY_FREE_MS
    return max(LATENCY_FACTOR_FLOOR, 1.0 - (latency_ms - LATENCY_FREE_MS) / span * 0.8)


def interval_from_latency(latency_ms: float) -> int:
    """当前延迟折算出的最小请求间隔（秒），只能把间隔拉长，不能压缩。"""
    if latency_ms <= 0:
        return 1
    return max(1, math.ceil(latency_ms / 250.0))


def proxy_capacity(proxy: dict[str, Any]) -> dict[str, Any]:
    """单个代理的可用容量；缺少 `并发上限` 的旧配置按 1 处理，不放大并发。"""
    cap_raw = parse_number(proxy.get("并发上限"), None)
    cap_missing = cap_raw is None
    cap = max(1, int(cap_raw)) if not cap_missing else DEFAULT_PROXY_CAP
    availability = parse_ratio(proxy.get("可用比例"), default=0.0)
    latency = parse_number(proxy.get("响应延迟"), 0.0) or 0.0
    baseline = parse_number(proxy.get("响应延迟基线"), latency) or latency
    units = int(math.floor(cap * availability * latency_factor(latency)))
    return {
        "units": max(units, 0),
        "cap": cap,
        "cap_missing": cap_missing,
        "availability": round(availability, 4),
        "latency_ms": round(latency, 1),
        "baseline_latency_ms": round(baseline, 1),
        "latency_factor": round(latency_factor(latency), 4),
    }


def region_capacity(
    region: str | None,
    *,
    proxies: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """汇总区域容量；区域为空时退化为全部代理，结果里标注口径来源。"""
    source = "region"
    candidates = list(proxies if proxies is not None else store.rows("proxy"))
    if region:
        candidates = [row for row in candidates if str(row.get("区域标签") or "") == region]
    else:
        source = "all_regions"
    active = [row for row in candidates if str(row.get("status") or "") != "已关闭"]
    details = [proxy_capacity(row) for row in active]
    total_units = sum(item["units"] for item in details)
    total_cap = sum(item["cap"] for item in details)
    if details:
        availability_avg = sum(item["availability"] for item in details) / len(details)
        latency_avg = sum(item["latency_ms"] for item in details) / len(details)
        baseline_avg = sum(item["baseline_latency_ms"] for item in details) / len(details)
    else:
        availability_avg = latency_avg = baseline_avg = 0.0
    latency_spike = bool(details) and (
        latency_avg >= baseline_avg * LATENCY_SPIKE_RATIO
        and latency_avg - baseline_avg >= LATENCY_SPIKE_DELTA_MS
    )
    return {
        "region": region or "",
        "source": source,
        "proxy_count": len(candidates),
        "active_count": len(active),
        "capacity": total_units,
        "cap_total": total_cap,
        "availability_avg": round(availability_avg, 4),
        "latency_ms": round(latency_avg, 1),
        "baseline_latency_ms": round(baseline_avg, 1),
        "latency_spike": latency_spike,
        "proxies": details,
    }


def quota_concurrency_cap(daily_quota: int, interval_seconds: float) -> int:
    """按日配额和请求间隔折算并发上限；至少保留 1，是否超限由调用方复核。"""
    if interval_seconds <= 0:
        return 1
    return max(1, int(math.floor(daily_quota * interval_seconds / 86400.0)))


def projected_daily_requests(interval_seconds: float, concurrency: int) -> int:
    if interval_seconds <= 0:
        return 0
    return int(math.floor(86400.0 / interval_seconds)) * max(concurrency, 0)


def fit_concurrency_to_quota(
    concurrency: int,
    *,
    interval_seconds: float,
    daily_quota: int,
) -> tuple[int, bool]:
    """在不超过日配额的前提下取最大并发；返回 (并发, 是否触到配额硬边界)。"""
    cap = max(concurrency, 1)
    bounded = False
    while cap > 1 and projected_daily_requests(interval_seconds, cap) > daily_quota:
        cap -= 1
        bounded = True
    exceeded = projected_daily_requests(interval_seconds, cap) > daily_quota
    return cap, bounded or exceeded
