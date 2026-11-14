"""强类型保留规则仓储：负责 retention_rule / dataset_link 的读写与视图投影。"""
from __future__ import annotations

import sqlite3
from typing import Any

from app.retention_db import dumps, loads
from app.retention_model import (
    DurationUnit,
    ExpireAction,
    MigrationStatus,
    NormalizedRule,
    RuleStatus,
)

# 列表视图给前端的强类型列（同时保留旧口径列，见 to_view_dict）。
TYPED_COLUMNS = [
    "规则编号", "数据集范围", "规范保留天数", "规范期限", "规范过期动作",
    "例外清单", "合规依据", "依据版本", "执行周期", "迁移状态", "规则状态",
]


class RuleAlreadyExists(Exception):
    """规则编号已存在：并发迁移的第二次写入会命中它。"""


class RetentionRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ---------- 写入 ----------
    def upsert_rule(self, rule: NormalizedRule, *, migrated: bool) -> bool:
        """插入一条强类型规则。

        返回 True 表示本次调用完成插入；False 表示规则编号已存在（重复迁移
        或并发批次后到），调用方应跳过且不得覆盖首次迁移结果。
        """
        params = (
            rule.rule_no,
            rule.dataset_scope,
            rule.retention_days,
            rule.duration_value,
            rule.duration_unit.value if rule.duration_unit else None,
            rule.expire_action.value if rule.expire_action else None,
            dumps(rule.exceptions),
            rule.compliance_ref,
            rule.compliance_version,
            int(rule.compliance_version_inferred),
            rule.execution_cycle,
            rule.status.value,
            int(migrated),
            (MigrationStatus.MIGRATED if not rule.is_pending else MigrationStatus.PENDING).value,
            dumps(rule.raw_snapshot),
        )
        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO retention_rule (
                rule_no, dataset_scope, retention_days, duration_value,
                duration_unit, expire_action, exceptions_json, compliance_ref,
                compliance_version, compliance_version_inferred, execution_cycle,
                status, migrated, migration_status, raw_snapshot_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            params,
        )
        return cur.rowcount == 1

    def list_rules(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        migration_status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        where: list[str] = []
        args: list[Any] = []
        if keyword:
            where.append("rule_no LIKE ?")
            args.append(f"%{keyword}%")
        if status:
            where.append("status = ?")
            args.append(status)
        if migration_status:
            where.append("migration_status = ?")
            args.append(migration_status)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        total = int(
            self.conn.execute(
                f"SELECT COUNT(*) AS c FROM retention_rule {clause}", args
            ).fetchone()["c"]
        )
        offset = max(page - 1, 0) * size
        rows = self.conn.execute(
            f"SELECT * FROM retention_rule {clause} ORDER BY id LIMIT ? OFFSET ?",
            [*args, size, offset],
        ).fetchall()
        return [self.to_view_dict(row) for row in rows], total

    def get_by_rule_no(self, rule_no: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM retention_rule WHERE rule_no = ?", (rule_no,)
        ).fetchone()
        return self.to_view_dict(row) if row else None

    def get_by_id(self, entry_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM retention_rule WHERE id = ?", (entry_id,)
        ).fetchone()
        return self.to_view_dict(row) if row else None

    def update_status(self, rule_no: str, status: RuleStatus) -> None:
        self.conn.execute(
            "UPDATE retention_rule SET status = ?, updated_at = datetime('now') WHERE rule_no = ?",
            (status.value, rule_no),
        )

    def link_lifecycle_days(self, lifecycle_id: int) -> int | None:
        row = self.conn.execute(
            "SELECT r.retention_days FROM dataset_link l "
            "JOIN retention_rule r ON r.rule_no = l.rule_no "
            "WHERE l.lifecycle_id = ? AND r.retention_days IS NOT NULL "
            "ORDER BY r.id LIMIT 1",
            (lifecycle_id,),
        ).fetchone()
        return int(row["retention_days"]) if row else None

    def link_recycle_days(self, recycle_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT r.rule_no, r.retention_days, r.status, r.expire_action "
            "FROM dataset_link l JOIN retention_rule r ON r.rule_no = l.rule_no "
            "WHERE l.recycle_id = ? ORDER BY r.id LIMIT 1",
            (recycle_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "rule_no": row["rule_no"],
            "retention_days": int(row["retention_days"]) if row["retention_days"] is not None else None,
            "rule_status": row["status"],
            "expire_action": row["expire_action"],
        }

    def seed_links(self, links: list[dict[str, Any]]) -> int:
        count = 0
        for link in links:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO dataset_link "
                "(rule_no, lifecycle_id, lifecycle_code, recycle_id, recycle_code, note) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    link["rule_no"], link.get("lifecycle_id"), link.get("lifecycle_code"),
                    link.get("recycle_id"), link.get("recycle_code"), link.get("note"),
                ),
            )
            count += cur.rowcount
        return count

    # ---------- 视图投影：强类型列 + 兼容旧口径列 ----------
    @staticmethod
    def to_view_dict(row: sqlite3.Row) -> dict[str, Any]:
        exceptions = loads(row["exceptions_json"]) or []
        unit = row["duration_unit"]
        duration_label = None
        if row["duration_value"] is not None and unit:
            unit_label = {
                DurationUnit.DAY.value: "天",
                DurationUnit.WEEK.value: "周",
                DurationUnit.MONTH.value: "个月",
                DurationUnit.QUARTER.value: "季度",
                DurationUnit.YEAR.value: "年",
            }.get(unit, unit)
            duration_label = f"{row['duration_value']}{unit_label}"
        action_label = None
        if row["expire_action"]:
            action_label = {
                ExpireAction.DELETE.value: "删除",
                ExpireAction.ARCHIVE.value: "归档",
                ExpireAction.ANONYMIZE.value: "脱敏",
            }.get(row["expire_action"], row["expire_action"])
        snapshot = loads(row["raw_snapshot_json"]) or {}
        return {
            "id": row["id"],
            "规则编号": row["rule_no"],
            "数据集范围": row["dataset_scope"],
            "规范保留天数": row["retention_days"],
            "规范期限": duration_label,
            "规范过期动作": action_label,
            "过期动作枚举": row["expire_action"],
            "例外清单": "、".join(exceptions),
            "合规依据": row["compliance_ref"],
            "依据版本": row["compliance_version"],
            "依据版本推断": bool(row["compliance_version_inferred"]),
            "执行周期": row["execution_cycle"],
            "迁移状态": row["migration_status"],
            "规则状态": row["status"],
            "status": row["status"],
            "migrated": bool(row["migrated"]),
            # 旧口径列原样回填，保证既有页面/导出不崩。
            "保留期限": duration_label or snapshot.get("保留期限"),
            "过期动作": action_label or snapshot.get("过期动作"),
            "原值快照": snapshot,
        }
