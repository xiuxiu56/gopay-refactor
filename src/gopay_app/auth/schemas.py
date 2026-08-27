"""认证接口数据结构。"""

from pydantic import BaseModel, Field, field_validator


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=256)

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value: str) -> str:
        return value.strip().lower()


class AdminView(BaseModel):
    id: str
    username: str


class AuthStatusView(BaseModel):
    setup_required: bool
    authenticated: bool
    admin: AdminView | None = None
