"""强类型保留模型的 SQLite 持久层与表结构（DDL）。

设计要点：
- 只用标准库 sqlite3，克隆下来即可演练，不引入新依赖。
- retention_rule 对规则编号建 UNIQUE 索引：重复执行迁移 / 两个批次并发写
  同一条规则时，数据库层直接拒绝第二次写入（INSERT OR IGNORE + 行锁），
  保证只有首次成功的迁移结果继续。
- migration_ledger 留存每条规则的迁移结果、批次与原值快照；无法识别的值
  落 PENDING，而不是静默丢弃。
- migration_lock 是单例行锁，配合 BEGIN IMMEDIATE 实现跨进程/跨线程的
  批次互斥。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS retention_rule (
    id                           INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_no                      TEXT NOT NULL UNIQUE,
    dataset_scope                TEXT NOT NULL,
    retention_days               INTEGER,
    duration_value               INTEGER,
    duration_unit                TEXT,
    expire_action                TEXT,
    exceptions_json              TEXT NOT NULL DEFAULT '[]',
    compliance_ref               TEXT,
    compliance_version           TEXT,
    compliance_version_inferred  INTEGER NOT NULL DEFAULT 0,
    execution_cycle              TEXT,
    status                       TEXT NOT NULL,
    migrated                     INTEGER NOT NULL DEFAULT 0,
    migration_status             TEXT NOT NULL,
    raw_snapshot_json            TEXT NOT NULL DEFAULT '{}',
    created_at                   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at                   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS migration_ledger (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_no            TEXT NOT NULL UNIQUE,
    source_id          INTEGER,
    batch_id           TEXT,
    result             TEXT NOT NULL,
    pending_reasons    TEXT NOT NULL DEFAULT '[]',
    normalized_json    TEXT NOT NULL DEFAULT '[]',
    raw_snapshot_json  TEXT NOT NULL DEFAULT '{}',
    migrated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS migration_batch (
    id              TEXT PRIMARY KEY,
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at     TEXT,
    batch_size      INTEGER NOT NULL,
    total_seen      INTEGER NOT NULL DEFAULT 0,
    migrated_count  INTEGER NOT NULL DEFAULT 0,
    pending_count   INTEGER NOT NULL DEFAULT 0,
    skipped_count   INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'RUNNING'
);

CREATE TABLE IF NOT EXISTS migration_lock (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    holder      TEXT NOT NULL,
    acquired_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS dataset_link (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_no         TEXT NOT NULL,
    lifecycle_id    INTEGER,
    lifecycle_code  TEXT,
    recycle_id      INTEGER,
    recycle_code    TEXT,
    note            TEXT,
    UNIQUE(rule_no, lifecycle_id, recycle_id)
);

CREATE INDEX IF NOT EXISTS idx_retention_rule_status ON retention_rule(status);
CREATE INDEX IF NOT EXISTS idx_retention_rule_migration ON retention_rule(migration_status);
CREATE INDEX IF NOT EXISTS idx_ledger_result ON migration_ledger(result);
CREATE INDEX IF NOT EXISTS idx_dataset_link_lifecycle ON dataset_link(lifecycle_id);
CREATE INDEX IF NOT EXISTS idx_dataset_link_recycle ON dataset_link(recycle_id);
"""


def connect(db_path: str) -> sqlite3.Connection:
    """打开一个连接。

    isolation_level=None 切到 autocommit，事务由 immediate_transaction()
    显式管理，否则驱动会在 BEGIN IMMEDIATE 前偷偷开事务导致嵌套报错。
    """
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        db_path, timeout=30, check_same_thread=False, isolation_level=None
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if db_path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """数据库结构演练入口：幂等建表，可重复执行。"""
    conn.executescript(SCHEMA_SQL)
    conn.commit()


@contextmanager
def immediate_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE：立即拿写锁，把并发写入冲突提前到事务开始处。

    锁竞争（busy_timeout 内拿不到写锁）统一抛 MigrationLocked，由迁移引擎
    按“后到批次不继续”处理，而不是把驱动层 OperationalError 泄露给调用方。
    """
    from app.retention_migration import MigrationLocked

    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower():
            raise MigrationLocked(f"迁移写锁竞争失败：{exc}") from exc
        raise
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def loads(text: str | None) -> Any:
    if not text:
        return None
    return json.loads(text)
