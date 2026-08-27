"""任务 REST 接口请求模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, SecretStr, field_validator


class TaskCreate(BaseModel):
    task_type: str = Field(min_length=1, max_length=48, pattern=r"^[a-z0-9_.-]+$")
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(default=0, ge=-100, le=100)
    max_attempts: int = Field(default=3, ge=1, le=20)
    idempotency_key: str | None = Field(default=None, max_length=160)
    run_after: datetime | None = None

    @field_validator("task_type", mode="after")
    @classmethod
    def normalize_task_type(cls, value: str) -> str:
        return value.strip().lower()


class TaskInputSubmit(BaseModel):
    input_type: str = Field(default="otp", min_length=1, max_length=32, pattern=r"^[a-z0-9_.-]+$")
    value: SecretStr = Field(min_length=1, max_length=2048)
    ttl_seconds: int = Field(default=300, ge=10, le=1800)

    @field_validator("input_type", mode="after")
    @classmethod
    def normalize_input_type(cls, value: str) -> str:
        return value.strip().lower()
