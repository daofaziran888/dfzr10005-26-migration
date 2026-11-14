"""数据保留策略业务规则：状态流转、字段校验与筛选口径都收在这里。

迁移后读路径优先走强类型 SQLite 仓库（保留期限可按整数天数比较、动作与
依据版本均为枚举/独立字段）；数据库不可用时回退到内存样例仓库，保证本地
开发与既有接口口径不变。状态流转动作仍为 启用/废止/归档，行为与迁移前一致。
"""
from __future__ import annotations

from typing import Any

from app.retention_bootstrap import get_retention_context
from app.retention_model import RuleStatus
from app.store import store

MODULE = "retention"
REQUIRED_FIELDS = ["规则编号", "数据集范围", "保留期限"]
STATUS_ORDER = ["草稿", "生效中", "已到期", "已废止"]
ACTION_RULES = {"启用规则": "草稿", "废止规则": "生效中", "归档规则": "已到期"}
NEGATIVE_ACTIONS = []

# 动作 -> 强类型状态，保证迁移后已生效规则的流转行为不改变。
_ACTION_TO_STATUS = {
    "启用规则": RuleStatus.DRAFT,
    "废止规则": RuleStatus.ACTIVE,
    "归档规则": RuleStatus.EXPIRED,
}


class RetentionService:
    def __init__(self) -> None:
        self._ctx = get_retention_context()

    # ---------- 读：强类型仓库优先 ----------
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        if self._ctx.available and self._ctx.repo is not None:
            return self._ctx.repo.list_rules(
                keyword=keyword, status=status, page=page, size=size
            )
        return self._legacy_list(keyword=keyword, status=status, page=page, size=size)

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        if self._ctx.available and self._ctx.repo is not None:
            return self._ctx.repo.get_by_id(entry_id)
        return store.find(MODULE, entry_id)

    # ---------- 写：登记与状态流转 ----------
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        if not self._ctx.available or self._ctx.repo is None:
            return self._legacy_create(values)
        # 强类型路径：新登记规则同样先做规范化，识别不了的期限/动作直接拒收并说明。
        from app.retention_model import normalize_legacy

        legacy = {
            "规则编号": values.get("规则编号"),
            "数据集范围": values.get("数据集范围"),
            "保留期限": values.get("保留期限"),
            "过期动作": values.get("过期动作"),
            "例外清单": values.get("例外清单", ""),
            "合规依据": values.get("合规依据", ""),
            "执行周期": values.get("执行周期", ""),
            "status": "草稿",
        }
        normalized = normalize_legacy(legacy)
        if normalized.is_pending:
            return None, normalized.pending_reasons  # 路由层会原样回显，不静默丢弃
        inserted = self._ctx.repo.upsert_rule(normalized, migrated=False)
        if not inserted:
            return None, [f"规则编号 {normalized.rule_no} 已存在，不能重复登记"]
        entry = self._ctx.repo.get_by_rule_no(normalized.rule_no)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据保留策略可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"

        if self._ctx.available and self._ctx.repo is not None:
            entry = self._ctx.repo.get_by_id(entry_id)
            if entry is None:
                return None, f"保留规则 {entry_id} 不存在或已归档"
            self._ctx.repo.update_status(entry["规则编号"], _ACTION_TO_STATUS[action])
            entry = self._ctx.repo.get_by_id(entry_id)
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            return entry, f"保留规则已{action}"

        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"保留规则 {entry_id} 不存在或已归档"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"保留规则已{action}"

    # ---------- 迁移运维 ----------
    def migration_summary(self) -> dict[str, Any]:
        if self._ctx.available and self._ctx.migration is not None:
            return self._ctx.migration.summary()
        return {"available": False, "reason": self._ctx.reason}

    def pending_entries(self) -> list[dict[str, Any]]:
        if self._ctx.available and self._ctx.migration is not None:
            return self._ctx.migration.pending_entries()
        return []

    def migration_batches(self) -> list[dict[str, Any]]:
        if self._ctx.available and self._ctx.migration is not None:
            return self._ctx.migration.batches()
        return []

    # ---------- 内存兜底（迁移前口径，保持原行为） ----------
    def _legacy_list(
        self, *, keyword: str | None, status: str | None, page: int, size: int
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def _legacy_create(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []
