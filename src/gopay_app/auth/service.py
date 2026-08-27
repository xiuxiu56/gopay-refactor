"""单管理员设置、登录和会话服务。"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from gopay_app.db.models import Admin, AuditEvent, WebSession, utc_now

from .passwords import hash_password, verify_password

SESSION_COOKIE = "gopay_v2_session"
CSRF_COOKIE = "gopay_v2_csrf"
USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.@-]{2,63}$")


class AuthError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class AuthenticatedAdmin:
    id: str
    username: str


@dataclass(slots=True)
class SessionResult:
    admin: AuthenticatedAdmin
    token: str
    csrf_token: str
    expires_at: datetime


def token_hash(value: str) -> str:
    return hashlib.sha256(value.strip().encode()).hexdigest()


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def validate_credentials(username: str, password: str) -> tuple[str, str]:
    normalized = username.strip().lower()
    if not USERNAME_PATTERN.fullmatch(normalized):
        raise AuthError("invalid_username", "账号需要 3-64 位，只能包含字母、数字、点、下划线、短横线或 @")
    if len(password) < 8:
        raise AuthError("invalid_password", "密码至少需要 8 位")
    if len(password) > 256:
        raise AuthError("invalid_password", "密码长度超过限制")
    return normalized, password


class AuthService:
    def __init__(self, session_factory: sessionmaker[Session], *, session_ttl_hours: int):
        self._session_factory = session_factory
        self._session_ttl = timedelta(hours=session_ttl_hours)

    def setup_required(self) -> bool:
        with self._session_factory() as session:
            return int(session.scalar(select(func.count()).select_from(Admin)) or 0) == 0

    def setup(self, username: str, password: str) -> SessionResult:
        normalized, password = validate_credentials(username, password)
        with self._session_factory() as session:
            if int(session.scalar(select(func.count()).select_from(Admin)) or 0) > 0:
                raise AuthError("setup_completed", "本地管理员已经创建")
            admin = Admin(
                id=str(uuid.uuid4()),
                singleton_key=1,
                username=normalized,
                password_hash=hash_password(password),
            )
            session.add(admin)
            try:
                session.flush()
            except IntegrityError as exc:
                raise AuthError("setup_completed", "本地管理员已经创建") from exc
            result = self._create_session(session, admin)
            self._audit(session, "auth.setup", admin.id)
            session.commit()
            return result

    def login(self, username: str, password: str) -> SessionResult:
        normalized, password = validate_credentials(username, password)
        with self._session_factory() as session:
            admin = session.scalar(select(Admin).where(func.lower(Admin.username) == normalized))
            if admin is None:
                raise AuthError("invalid_credentials", "账号或密码错误")
            valid, needs_rehash = verify_password(password, admin.password_hash)
            if not valid:
                self._audit(session, "auth.login_failed", admin.id)
                session.commit()
                raise AuthError("invalid_credentials", "账号或密码错误")
            if needs_rehash:
                admin.password_hash = hash_password(password)
            admin.last_login_at = utc_now()
            admin.updated_at = utc_now()
            session.execute(delete(WebSession).where(WebSession.expires_at <= utc_now()))
            result = self._create_session(session, admin)
            self._audit(session, "auth.login", admin.id)
            session.commit()
            return result

    def authenticate(self, token: str) -> AuthenticatedAdmin | None:
        if not token.strip():
            return None
        now = utc_now()
        with self._session_factory() as session:
            web_session = session.scalar(select(WebSession).where(WebSession.token_hash == token_hash(token)))
            if web_session is None or _as_utc(web_session.expires_at) <= now:
                return None
            admin = session.get(Admin, web_session.admin_id)
            if admin is None:
                return None
            web_session.last_seen_at = now
            session.commit()
            return AuthenticatedAdmin(id=admin.id, username=admin.username)

    def verify_csrf(self, token: str, csrf_token: str) -> bool:
        if not token.strip() or not csrf_token.strip():
            return False
        with self._session_factory() as session:
            stored = session.scalar(
                select(WebSession.csrf_token_hash).where(WebSession.token_hash == token_hash(token))
            )
            return bool(stored and secrets.compare_digest(stored, token_hash(csrf_token)))

    def logout(self, token: str, actor_id: str = "") -> None:
        if not token.strip():
            return
        with self._session_factory() as session:
            session.execute(delete(WebSession).where(WebSession.token_hash == token_hash(token)))
            self._audit(session, "auth.logout", actor_id)
            session.commit()

    def _create_session(self, session: Session, admin: Admin) -> SessionResult:
        token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(24)
        expires_at = utc_now() + self._session_ttl
        session.add(
            WebSession(
                id=str(uuid.uuid4()),
                admin_id=admin.id,
                token_hash=token_hash(token),
                csrf_token_hash=token_hash(csrf_token),
                expires_at=expires_at,
            )
        )
        return SessionResult(
            admin=AuthenticatedAdmin(id=admin.id, username=admin.username),
            token=token,
            csrf_token=csrf_token,
            expires_at=expires_at,
        )

    @staticmethod
    def _audit(session: Session, event_type: str, actor_id: str) -> None:
        session.add(
            AuditEvent(
                id=str(uuid.uuid4()),
                event_type=event_type,
                actor_id=actor_id,
                detail_json=json.dumps({}, ensure_ascii=False),
            )
        )
