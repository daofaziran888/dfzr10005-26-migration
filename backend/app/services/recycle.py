"""数据回收站业务规则：状态流转、字段校验与筛选口径都收在这里。

迁移后回收记录清单会通过 dataset_link 关联强类型保留规则，把当前规则状态
与规范化可恢复期（天数）带到每条回收记录上（新增「规则状态」「规范可恢复期
天数」列，原「可恢复期」列保持不变），兼容既有回收站读取口径。
"""
from __future__ import annotations

from typing import Any

from app.retention_bootstrap import get_retention_context
from app.store import store

MODULE = "recycle"
REQUIRED_FIELDS = ["记录编号", "表名称", "删除时间"]
STATUS_ORDER = ["待恢复", "已恢复", "已清理", "已过期"]
ACTION_RULES = {"恢复数据": "待恢复", "永久清理": "已恢复", "延期保留": "已过期"}
NEGATIVE_ACTIONS = []


class RecycleService:
    def _enrich_recovery(self, row: dict[str, Any]) -> dict[str, Any]:
        """把关联保留规则的当前状态与规范可恢复期回填到回收记录。"""
        ctx = get_retention_context()
        if ctx.available and ctx.repo is not None:
            link = ctx.repo.link_recycle_days(int(row.get("id", 0)))
            if link is not None:
                row["规则状态"] = link["rule_status"]
                row["关联规则编号"] = link["rule_no"]
                if link["retention_days"] is not None:
                    row["规范可恢复期天数"] = link["retention_days"]
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
            rows = [row for row in rows if keyword in str(row.get("记录编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        page_rows = [self._enrich_recovery(dict(row)) for row in rows[start:start + size]]
        return page_rows, total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        return self._enrich_recovery(dict(row)) if row else None

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
            return None, f"回收记录 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据回收站可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"回收记录已{action}"
