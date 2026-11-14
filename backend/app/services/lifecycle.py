"""表生命周期管理业务规则：状态流转、字段校验与筛选口径都收在这里。

迁移后列表读取会通过 dataset_link 关联强类型保留规则，把规范化后的保留
天数同步到每条生命周期结果（新增「规范保留天数」列，原「保留天数」列保持
不变），让生命周期清理使用可比较的整数天数，同时不改变既有状态流转行为。
"""
from __future__ import annotations

from typing import Any

from app.retention_bootstrap import get_retention_context
from app.store import store

MODULE = "lifecycle"
REQUIRED_FIELDS = ["策略编号", "表名称", "创建日期"]
STATUS_ORDER = ["草稿", "生效中", "已到期", "已归档"]
ACTION_RULES = {"启用策略": "草稿", "归档表": "生效中", "删除表": "已到期"}
NEGATIVE_ACTIONS = []


class LifecycleService:
    def _enrich_retention(self, row: dict[str, Any]) -> dict[str, Any]:
        """回填强类型保留规则的规范期限；关联不到时不改动既有字段。"""
        ctx = get_retention_context()
        if ctx.available and ctx.repo is not None:
            days = ctx.repo.link_lifecycle_days(int(row.get("id", 0)))
            if days is not None:
                row["规范保留天数"] = days
        return row

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("策略编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        page_rows = [self._enrich_retention(dict(row)) for row in rows[start:start + size]]
        return page_rows, total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        return self._enrich_retention(dict(row)) if row else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"生命周期策略 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于表生命周期管理可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"生命周期策略已{action}"
