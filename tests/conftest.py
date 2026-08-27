"""P0 测试公共夹具。"""

from __future__ import annotations

from pathlib import Path

import pytest

from gopay_app.config import Settings
from gopay_app.db.engine import build_session_factory, create_database_engine, upgrade_database
from gopay_app.security.codec import SecretCodec


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_path=tmp_path / "data" / "app.db",
        allowed_hosts=["testserver", "127.0.0.1", "localhost"],
        allowed_origins=["http://testserver", "http://127.0.0.1:19081"],
        session_ttl_hours=24,
        worker_count=2,
        worker_lease_seconds=10,
        worker_heartbeat_seconds=1,
        worker_poll_seconds=0.05,
        worker_shutdown_seconds=5,
        task_retry_base_seconds=0.1,
        sse_poll_seconds=0.1,
        sse_heartbeat_seconds=1,
    )


@pytest.fixture
def database(settings: Settings):
    upgrade_database(settings)
    engine = create_database_engine(settings)
    session_factory = build_session_factory(engine)
    codec = SecretCodec.load(settings.database_key_path)
    try:
        yield engine, session_factory, codec
    finally:
        engine.dispose()
