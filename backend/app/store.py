"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

from typing import Any

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 迁移任务、采集频率结果、代理容量队列这些运行期状态不属于业务列表模块，
        # 单独放在 state 里，避免污染运营概览的模块统计。
        self._state: dict[str, Any] = {}

    def reset(self) -> None:
        """恢复成刚启动时的示例数据；迁移状态一并清空，供测试和本地演示使用。"""
        self._tables = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self._state = {}

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def state_rows(self, key: str) -> list[dict[str, Any]]:
        rows = self._state.setdefault(key, [])
        if not isinstance(rows, list):
            raise TypeError(f"状态 {key} 不是列表")
        return rows

    def state_value(self, key: str, default: Any = None) -> Any:
        return self._state.get(key, default)

    def set_state_value(self, key: str, value: Any) -> None:
        self._state[key] = value

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
