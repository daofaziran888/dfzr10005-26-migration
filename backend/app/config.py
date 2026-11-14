"""运行配置：端口、跨域、运行环境，以及强类型保留模型的存储位置。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _default_db_path() -> str:
    """本地开发默认把 SQLite 文件放在 backend/data/ 下；该目录已在 .gitignore 忽略。"""
    data_dir = Path(__file__).resolve().parent.parent / "data"
    return str(data_dir / "retention.db")


@dataclass(frozen=True)
class Settings:
    app_name: str = "数仓治理与运维平台"
    env: str = os.environ.get("APP_ENV", "local")
    port: int = 8000
    allowed_origins: list[str] = field(
        default_factory=lambda: [
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ]
    )
    page_size_default: int = 20
    page_size_max: int = 200
    # 强类型保留模型的 SQLite 路径；测试里传 ":memory:" 做完全隔离的演练。
    retention_db_path: str = os.environ.get("RETN_DB_PATH", _default_db_path())
    # 应用启动时是否自动建表并迁移本地兼容样例；测试与显式演练时关掉。
    retention_auto_bootstrap: bool = os.environ.get("RETN_AUTO_BOOTSTRAP", "1") == "1"
    # 单批回填大小，可被迁移命令行参数覆盖。
    migration_batch_size: int = int(os.environ.get("RETN_BATCH_SIZE", "500"))


settings = Settings()
