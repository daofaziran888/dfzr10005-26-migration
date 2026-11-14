#!/usr/bin/env python3
"""存量保留规则迁移命令行工具。

典型流程（对应上线步骤）：
  1. 隔离环境演练（不碰线上库，落临时 SQLite）：
       python -m scripts.migrate_retention rehearse --batch-size 3
  2. 数据库结构演练（只建表）：
       python -m scripts.migrate_retention init-db
  3. 分批回填上线：
       python -m scripts.migrate_retention migrate --batch-size 500
  4. 查看结果 / 待处理清单：
       python -m scripts.migrate_retention status
       python -m scripts.migrate_retention pending
  5. 回退（按批次或全部）：
       python -m scripts.migrate_retention rollback [--batch-id BATCH-xxxx]

所有命令默认读取 app.config.settings.retention_db_path，可用 --db 覆盖；
内存演练使用独立临时文件，与本地开发库完全隔离。
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

# 允许 `python scripts/migrate_retention.py` 与 `python -m scripts.migrate_retention` 两种调用。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.legacy_data import DATASET_LINKS, LEGACY_RETENTION_ROWS  # noqa: E402
from app.retention_bootstrap import reset_context  # noqa: E402
from app.retention_db import connect, init_schema  # noqa: E402
from app.retention_migration import RetentionMigration  # noqa: E402
from app.retention_repo import RetentionRepository  # noqa: E402


def _print(data: object) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _open(db_path: str):
    conn = connect(db_path)
    init_schema(conn)
    repo = RetentionRepository(conn)
    repo.seed_links(DATASET_LINKS)
    return conn, RetentionMigration(conn)


def cmd_init_db(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    init_schema(conn)
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    ]
    print(f"[init-db] {args.db} 结构演练完成，共 {len(tables)} 张表：")
    for name in tables:
        print(f"  - {name}")
    conn.close()
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    conn, migration = _open(args.db)
    try:
        report = migration.run_batch(
            LEGACY_RETENTION_ROWS, batch_size=args.batch_size
        )
        _print(report.as_dict())
        print("\n[migrate] 迁移后汇总：")
        _print(migration.summary())
    finally:
        conn.close()
    return 0


def cmd_rehearse(args: argparse.Namespace) -> int:
    """隔离演练：临时库 + 小批次，跑完自动清理，不触碰本地/线上库。"""
    tmp_dir = tempfile.mkdtemp(prefix="retn-rehearse-")
    tmp_db = str(Path(tmp_dir) / "rehearse.db")
    print(f"[rehearse] 隔离演练库：{tmp_db}（批次大小 {args.batch_size}）")
    conn, migration = _open(tmp_db)
    try:
        report = migration.run_batch(
            LEGACY_RETENTION_ROWS, batch_size=args.batch_size
        )
        _print(report.as_dict())
        # 幂等演练：再跑一遍，应当全部 SKIPPED，不产生重复规则。
        again = migration.run_batch(
            LEGACY_RETENTION_ROWS, batch_size=args.batch_size
        )
        print("\n[rehearse] 重复执行结果（应全部 skipped，规则数不变）：")
        _print(again.as_dict())
        rule_count = conn.execute("SELECT COUNT(*) AS c FROM retention_rule").fetchone()["c"]
        ledger_count = conn.execute("SELECT COUNT(*) AS c FROM migration_ledger").fetchone()["c"]
        print(f"\n[rehearse] 强类型规则 {rule_count} 条 / 台账 {ledger_count} 条（应等于样例数 {len(LEGACY_RETENTION_ROWS)}）")
        assert rule_count == len(LEGACY_RETENTION_ROWS), "重复迁移生成了重复规则！"
        assert again.skipped_count == len(LEGACY_RETENTION_ROWS), "幂等校验失败！"
        print("[rehearse] 幂等与去重校验通过 ✔")
    finally:
        conn.close()
    reset_context()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    conn, migration = _open(args.db)
    try:
        _print(migration.summary())
    finally:
        conn.close()
    return 0


def cmd_pending(args: argparse.Namespace) -> int:
    conn, migration = _open(args.db)
    try:
        _print({"total": len(migration.pending_entries()), "items": migration.pending_entries()})
    finally:
        conn.close()
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    conn, migration = _open(args.db)
    try:
        result = migration.rollback(batch_id=args.batch_id)
        _print(result)
    finally:
        conn.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    from app.config import settings

    parser = argparse.ArgumentParser(description="存量保留规则 -> 强类型保留模型迁移工具")
    parser.add_argument("--db", default=settings.retention_db_path, help="SQLite 路径，默认取配置")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="数据库结构演练（幂等建表）").set_defaults(func=cmd_init_db)

    p_migrate = sub.add_parser("migrate", help="分批回填上线")
    p_migrate.add_argument("--batch-size", type=int, default=settings.migration_batch_size)
    p_migrate.set_defaults(func=cmd_migrate)

    p_rehearse = sub.add_parser("rehearse", help="隔离环境演练（临时库，含幂等校验）")
    p_rehearse.add_argument("--batch-size", type=int, default=3)
    p_rehearse.set_defaults(func=cmd_rehearse)

    sub.add_parser("status", help="查看迁移汇总").set_defaults(func=cmd_status)
    sub.add_parser("pending", help="查看待处理清单").set_defaults(func=cmd_pending)

    p_rb = sub.add_parser("rollback", help="回退迁移")
    p_rb.add_argument("--batch-id", default=None, help="只回退指定批次；缺省回退全部")
    p_rb.set_defaults(func=cmd_rollback)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
