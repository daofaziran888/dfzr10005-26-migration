"""强类型保留模型的启动装配：建表、关联回填、本地兼容样例自动迁移。

服务层通过 get_retention_context() 拿到共享连接；任何环节失败（目录不可写、
磁盘异常等）都回退到内存仓库并标记 available=False，保证本地开发和既有
接口不被数据库问题拖垮。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

from app.config import settings
from app.legacy_data import DATASET_LINKS, LEGACY_RETENTION_ROWS
from app.retention_db import connect, init_schema
from app.retention_migration import RetentionMigration
from app.retention_repo import RetentionRepository


@dataclass
class RetentionContext:
    conn: object | None
    repo: RetentionRepository | None
    migration: RetentionMigration | None
    available: bool
    reason: str | None = None


_lock = threading.Lock()
_context: RetentionContext | None = None


def bootstrap(
    *,
    db_path: str | None = None,
    run_migration: bool = True,
    batch_size: int | None = None,
) -> RetentionContext:
    """建库演练 + 首次回填。重复调用安全：已迁移的规则会被跳过。"""
    path = db_path or settings.retention_db_path
    conn = connect(path)
    init_schema(conn)
    repo = RetentionRepository(conn)
    repo.seed_links(DATASET_LINKS)
    migration = RetentionMigration(conn)
    if run_migration:
        migration.run_batch(
            LEGACY_RETENTION_ROWS,
            batch_size=batch_size or settings.migration_batch_size,
        )
    return RetentionContext(conn=conn, repo=repo, migration=migration, available=True)


def get_retention_context() -> RetentionContext:
    """进程级单例；首次调用按配置决定是否自动迁移本地兼容样例。"""
    global _context
    if _context is not None:
        return _context
    with _lock:
        if _context is None:
            try:
                _context = bootstrap(run_migration=settings.retention_auto_bootstrap)
            except Exception as exc:  # 本地开发兜底：退回纯内存模式
                _context = RetentionContext(
                    conn=None, repo=None, migration=None,
                    available=False, reason=f"{type(exc).__name__}: {exc}",
                )
    return _context


def reset_context() -> None:
    """测试钩子：丢弃单例，便于用 :memory: 重新装配。"""
    global _context
    with _lock:
        if _context and _context.conn is not None:
            try:
                _context.conn.close()
            except Exception:
                pass
        _context = None
