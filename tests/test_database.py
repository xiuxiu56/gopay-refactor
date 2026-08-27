"""SQLite WAL 与迁移测试。"""

from __future__ import annotations

from alembic.config import Config

from alembic import command
from gopay_app.config import PROJECT_ROOT, Settings
from gopay_app.db.engine import database_status


def test_database_uses_wal_and_current_schema(settings: Settings, database):
    engine, _session_factory, _codec = database
    status = database_status(engine, settings.database_path)

    assert status["schema_version"] == "0003_p6_rolling_account_flow"
    assert status["journal_mode"] == "wal"
    assert status["foreign_keys"] is True
    assert status["busy_timeout_ms"] == 5000
    assert status["quick_check"] == "ok"
    assert settings.database_path.stat().st_mode & 0o077 == 0
    assert settings.database_key_path.stat().st_mode & 0o077 == 0


def test_alembic_metadata_matches_database(settings: Settings, database):
    _engine, _session_factory, _codec = database
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("prepend_sys_path", str(PROJECT_ROOT / "src"))
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{settings.database_path.as_posix()}")
    command.check(config)
