"""SQLite 引擎、WAL 配置与迁移入口。"""

from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path
from typing import Any

from alembic.config import Config
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from gopay_app.config import PROJECT_ROOT, Settings


def sqlite_url(path: Path) -> str:
    return f"sqlite+pysqlite:///{path.as_posix()}"


def _prepare_data_path(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with suppress(OSError):
        os.chmod(path.parent, 0o700)


def create_database_engine(settings: Settings) -> Engine:
    _prepare_data_path(settings.database_path)
    engine = create_engine(
        sqlite_url(settings.database_path),
        connect_args={"check_same_thread": False, "timeout": 5.0},
        pool_pre_ping=True,
    )

    @event.listens_for(engine, "connect")
    def apply_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode = WAL")
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.execute("PRAGMA busy_timeout = 5000")
            cursor.execute("PRAGMA synchronous = NORMAL")
            cursor.execute("PRAGMA wal_autocheckpoint = 1000")
        finally:
            cursor.close()

    return engine


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def upgrade_database(settings: Settings) -> None:
    _prepare_data_path(settings.database_path)
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("prepend_sys_path", str(PROJECT_ROOT / "src"))
    config.set_main_option("sqlalchemy.url", sqlite_url(settings.database_path))
    command.upgrade(config, "head")
    if settings.database_path.exists():
        with suppress(OSError):
            os.chmod(settings.database_path, 0o600)


def database_status(engine: Engine, path: Path) -> dict[str, Any]:
    with engine.connect() as connection:
        journal_mode = connection.execute(text("PRAGMA journal_mode")).scalar_one()
        foreign_keys = int(connection.execute(text("PRAGMA foreign_keys")).scalar_one())
        busy_timeout = int(connection.execute(text("PRAGMA busy_timeout")).scalar_one())
        schema_version = connection.execute(
            text("SELECT version_num FROM alembic_version LIMIT 1")
        ).scalar_one_or_none()
        quick_check = connection.execute(text("PRAGMA quick_check")).scalar_one()
    wal_path = Path(str(path) + "-wal")
    return {
        "path": str(path),
        "schema_version": schema_version or "",
        "journal_mode": str(journal_mode).lower(),
        "foreign_keys": bool(foreign_keys),
        "busy_timeout_ms": busy_timeout,
        "database_bytes": path.stat().st_size if path.exists() else 0,
        "wal_bytes": wal_path.stat().st_size if wal_path.exists() else 0,
        "quick_check": quick_check,
    }
