"""存量规则 -> 强类型保留模型的分批迁移引擎。

保障口径（对应需求）：
1. 原值保留：每条规则的完整原始行进 raw_snapshot_json，并同步落 migration_ledger。
2. 不静默丢弃：规范化失败的字段累计成 pending_reasons，记录以 PENDING 落库，
   可通过待处理清单逐条复核。
3. 幂等：retention_rule.rule_no 与 ledger(rule_no,batch_id) 均为 UNIQUE；
   重复执行迁移时已存在的规则只计数 skipped，不生成重复规则。
4. 并发：migration_lock 单例行锁 + BEGIN IMMEDIATE。两个批次同时写同一条
   规则时，只有拿到锁的批次能提交；后到批次整体判定为“已有迁移在进行/
   已完成”，不会产生第二条迁移结果。
5. 可回退：rollback 按批次（或全部）删除本引擎写入的规则与台账，恢复到
   迁移前状态；原始行从不被覆盖，因此回退不依赖额外备份即可追溯。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.retention_db import dumps, immediate_transaction
from app.retention_model import (
    MigrationStatus,
    NormalizedRule,
    RuleStatus,
    normalize_legacy,
)
from app.retention_repo import RetentionRepository


class MigrationLocked(RuntimeError):
    """已有迁移批次持有写锁。"""


@dataclass
class BatchReport:
    batch_id: str
    batch_size: int
    total_seen: int = 0
    migrated_count: int = 0
    pending_count: int = 0
    skipped_count: int = 0
    details: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "batch_size": self.batch_size,
            "total_seen": self.total_seen,
            "migrated_count": self.migrated_count,
            "pending_count": self.pending_count,
            "skipped_count": self.skipped_count,
            "details": self.details,
        }


class RetentionMigration:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.repo = RetentionRepository(conn)

    # ---------- 锁 ----------
    def _acquire_lock(self, holder: str) -> None:
        try:
            self.conn.execute(
                "INSERT INTO migration_lock (id, holder) VALUES (1, ?)", (holder,)
            )
        except sqlite3.IntegrityError as exc:
            row = self.conn.execute(
                "SELECT holder, acquired_at FROM migration_lock WHERE id = 1"
            ).fetchone()
            raise MigrationLocked(
                f"迁移锁已被批次 {row['holder']} 于 {row['acquired_at']} 持有"
            ) from exc
        except sqlite3.OperationalError as exc:
            # 另一连接正在提交（database is locked / table is locked）。
            if "locked" in str(exc).lower():
                raise MigrationLocked(f"迁移写锁竞争失败：{exc}") from exc
            raise

    def _release_lock(self) -> None:
        self.conn.execute("DELETE FROM migration_lock WHERE id = 1")

    # ---------- 主流程 ----------
    def run_batch(
        self,
        legacy_rows: list[dict[str, Any]],
        *,
        batch_size: int = 500,
        batch_id: str | None = None,
    ) -> BatchReport:
        """迁移一个分片（调用方按 batch_size 切片后传入，或直接传全量由本方法切）。

        每个分片独立批次、独立事务；任一分片失败只回滚该分片，已完成分片
        不受影响（分批上线、故障可续跑）。
        """
        report = BatchReport(batch_id=batch_id or f"BATCH-{uuid.uuid4().hex[:12]}",
                             batch_size=batch_size)
        for start in range(0, len(legacy_rows), batch_size):
            chunk = legacy_rows[start:start + batch_size]
            self._run_chunk(chunk, report)
        return report

    def _run_chunk(self, chunk: list[dict[str, Any]], report: BatchReport) -> None:
        with immediate_transaction(self.conn):
            self._acquire_lock(report.batch_id)
            try:
                # 同一批次可能分多个 chunk，批次行只建一次。
                self.conn.execute(
                    "INSERT OR IGNORE INTO migration_batch (id, batch_size, status) "
                    "VALUES (?, ?, 'RUNNING')",
                    (report.batch_id, report.batch_size),
                )
                for legacy in chunk:
                    self._migrate_one(legacy, report)
                self.conn.execute(
                    "UPDATE migration_batch SET finished_at = datetime('now'), "
                    "total_seen = ?, migrated_count = ?, pending_count = ?, "
                    "skipped_count = ?, status = 'DONE' WHERE id = ?",
                    (report.total_seen, report.migrated_count, report.pending_count,
                     report.skipped_count, report.batch_id),
                )
            finally:
                self._release_lock()

    def _migrate_one(self, legacy: dict[str, Any], report: BatchReport) -> None:
        report.total_seen += 1
        normalized = normalize_legacy(legacy)
        result = MigrationStatus.PENDING if normalized.is_pending else MigrationStatus.MIGRATED

        # 先写规则。UNIQUE(rule_no) 保证重复/并发时后到者 rowcount=0。
        inserted = self.repo.upsert_rule(
            normalized, migrated=result is MigrationStatus.MIGRATED
        )

        # 台账同样唯一，重复执行不产生重复迁移记录。
        ledger_cur = self.conn.execute(
            "INSERT OR IGNORE INTO migration_ledger "
            "(rule_no, source_id, batch_id, result, pending_reasons, "
            " normalized_json, raw_snapshot_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                normalized.rule_no,
                legacy.get("source_id"),
                report.batch_id,
                result.value,
                dumps(normalized.pending_reasons),
                dumps(self._ledger_view(normalized)),
                dumps(normalized.raw_snapshot),
            ),
        )

        if not inserted or ledger_cur.rowcount == 0:
            report.skipped_count += 1
            report.details.append({
                "规则编号": normalized.rule_no, "结果": "SKIPPED",
                "说明": "规则已迁移或已存在，保留首次迁移结果",
            })
        elif result is MigrationStatus.MIGRATED:
            report.migrated_count += 1
            report.details.append({
                "规则编号": normalized.rule_no, "结果": "MIGRATED",
                "规范保留天数": normalized.retention_days,
                "过期动作": normalized.expire_action.value if normalized.expire_action else None,
                "依据版本": normalized.compliance_version,
            })
        else:
            report.pending_count += 1
            report.details.append({
                "规则编号": normalized.rule_no, "结果": "PENDING",
                "待处理原因": normalized.pending_reasons,
            })

    @staticmethod
    def _ledger_view(rule: NormalizedRule) -> dict[str, Any]:
        return {
            "rule_no": rule.rule_no,
            "dataset_scope": rule.dataset_scope,
            "retention_days": rule.retention_days,
            "duration_value": rule.duration_value,
            "duration_unit": rule.duration_unit.value if rule.duration_unit else None,
            "expire_action": rule.expire_action.value if rule.expire_action else None,
            "exceptions": rule.exceptions,
            "compliance_ref": rule.compliance_ref,
            "compliance_version": rule.compliance_version,
            "execution_cycle": rule.execution_cycle,
            "status": rule.status.value,
        }

    # ---------- 查询 ----------
    def pending_entries(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT rule_no, batch_id, pending_reasons, raw_snapshot_json "
            "FROM migration_ledger WHERE result = 'PENDING' ORDER BY id"
        ).fetchall()
        return [
            {
                "规则编号": row["rule_no"],
                "批次": row["batch_id"],
                "待处理原因": json.loads(row["pending_reasons"]),
                "原值快照": json.loads(row["raw_snapshot_json"]),
            }
            for row in rows
        ]

    def batches(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM migration_batch ORDER BY started_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]

    def summary(self) -> dict[str, Any]:
        rows = self.conn.execute(
            "SELECT result, COUNT(*) AS c FROM migration_ledger GROUP BY result"
        ).fetchall()
        counts = {row["result"]: row["c"] for row in rows}
        lock = self.conn.execute("SELECT holder FROM migration_lock WHERE id = 1").fetchone()
        return {
            "migrated": counts.get("MIGRATED", 0),
            "pending": counts.get("PENDING", 0),
            "failed": counts.get("FAILED", 0),
            "lock_holder": lock["holder"] if lock else None,
            "batches": len(self.batches()),
        }

    # ---------- 回退 ----------
    def rollback(self, *, batch_id: str | None = None) -> dict[str, int]:
        """按批次回退（不传 batch_id 则回退全部迁移数据）。

        只删除本引擎写入的强类型规则与台账，原始内存样例不受影响，
        因此随时可以重新执行迁移。
        """
        with immediate_transaction(self.conn):
            if batch_id:
                rule_nos = [
                    row["rule_no"]
                    for row in self.conn.execute(
                        "SELECT DISTINCT rule_no FROM migration_ledger WHERE batch_id = ?",
                        (batch_id,),
                    ).fetchall()
                ]
                ledger = self.conn.execute(
                    "DELETE FROM migration_ledger WHERE batch_id = ?", (batch_id,)
                ).rowcount
                batch = self.conn.execute(
                    "DELETE FROM migration_batch WHERE id = ?", (batch_id,)
                ).rowcount
                rules = 0
                for rule_no in rule_nos:
                    rules += self.conn.execute(
                        "DELETE FROM retention_rule WHERE rule_no = ?", (rule_no,)
                    ).rowcount
            else:
                # 只回退迁移引擎写入的规则（台账里有记录的），手工登记且未迁移的规则保留。
                rules = self.conn.execute(
                    "DELETE FROM retention_rule WHERE rule_no IN "
                    "(SELECT rule_no FROM migration_ledger)"
                ).rowcount
                ledger = self.conn.execute("DELETE FROM migration_ledger").rowcount
                batch = self.conn.execute("DELETE FROM migration_batch").rowcount
                self.conn.execute("DELETE FROM migration_lock")
            return {"rules_removed": rules, "ledger_removed": ledger, "batches_removed": batch}
