"""应用配置与路径解析。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _split_csv(value: object) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


class Settings(BaseSettings):
    """GoPay 本地服务配置。"""

    model_config = SettingsConfigDict(
        env_prefix="GOPAY_",
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "127.0.0.1"
    port: int = Field(default=19081, ge=1, le=65535)
    database_path: Path = Path("data/app.db")
    session_ttl_hours: int = Field(default=168, ge=1, le=24 * 365)
    cookie_secure: bool = False
    allowed_hosts: list[str] = ["127.0.0.1", "localhost", "testserver"]
    allowed_origins: list[str] = ["http://127.0.0.1:19081", "http://localhost:19081"]
    worker_count: int = Field(default=8, ge=1, le=64)
    worker_lease_seconds: int = Field(default=60, ge=10, le=3600)
    worker_heartbeat_seconds: float = Field(default=10.0, ge=1.0, le=300.0)
    worker_poll_seconds: float = Field(default=0.25, ge=0.05, le=10.0)
    worker_shutdown_seconds: float = Field(default=30.0, ge=1.0, le=300.0)
    task_retry_base_seconds: float = Field(default=2.0, ge=0.1, le=3600.0)
    change_log_limit: int = Field(default=10_000, ge=100, le=1_000_000)
    sse_poll_seconds: float = Field(default=0.5, ge=0.1, le=10.0)
    sse_heartbeat_seconds: float = Field(default=15.0, ge=1.0, le=120.0)
    legacy_app_path: Path = PROJECT_ROOT.parent / "app"
    max_request_bytes: int = Field(default=1_048_576, ge=1024, le=16 * 1024 * 1024)
    log_level: str = "INFO"

    @field_validator("allowed_hosts", "allowed_origins", mode="before")
    @classmethod
    def parse_csv(cls, value: object) -> list[str]:
        return _split_csv(value)

    @field_validator("database_path", "legacy_app_path", mode="after")
    @classmethod
    def resolve_database_path(cls, value: Path) -> Path:
        expanded = value.expanduser()
        if not expanded.is_absolute():
            expanded = PROJECT_ROOT / expanded
        return expanded.resolve()

    @field_validator("log_level", mode="after")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        normalized = value.strip().upper()
        return normalized if normalized in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"} else "INFO"

    @property
    def database_key_path(self) -> Path:
        return self.database_path.with_suffix(self.database_path.suffix + ".key")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
