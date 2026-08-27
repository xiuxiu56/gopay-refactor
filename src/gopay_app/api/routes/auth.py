"""管理员认证接口。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from gopay_app.auth.limiter import SlidingWindowLimiter
from gopay_app.auth.schemas import Credentials
from gopay_app.auth.service import CSRF_COOKIE, SESSION_COOKIE, AuthService, SessionResult
from gopay_app.config import Settings

from ..dependencies import auth_service, optional_admin, require_csrf
from ..responses import failure, success

router = APIRouter(prefix="/api/v1/auth", tags=["认证"])


def _client_key(request: Request, username: str = "") -> str:
    host = request.client.host if request.client else "unknown"
    return f"{host}:{username.strip().lower()}"


def _set_session_cookies(response: Response, result: SessionResult, settings: Settings) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        result.token,
        max_age=max(1, settings.session_ttl_hours * 3600),
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        result.csrf_token,
        max_age=max(1, settings.session_ttl_hours * 3600),
        httponly=False,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
    )


@router.get("/status")
def status(request: Request, admin=Depends(optional_admin), service: AuthService = Depends(auth_service)):
    return success(
        {
            "setup_required": service.setup_required(),
            "authenticated": admin is not None,
            "admin": {"id": admin.id, "username": admin.username} if admin else None,
        }
    )


@router.post("/setup", status_code=201)
def setup(request: Request, credentials: Credentials):
    limiter: SlidingWindowLimiter = request.app.state.login_limiter
    key = _client_key(request, credentials.username)
    allowed, retry_after = limiter.allow(key)
    if not allowed:
        response = failure(429, "rate_limited", "操作过于频繁，请稍后重试")
        response.headers["Retry-After"] = str(retry_after)
        return response
    result = request.app.state.auth_service.setup(credentials.username, credentials.password)
    response = success({"admin": {"id": result.admin.id, "username": result.admin.username}}, status_code=201)
    _set_session_cookies(response, result, request.app.state.settings)
    limiter.reset(key)
    return response


@router.post("/login")
def login(request: Request, credentials: Credentials):
    limiter: SlidingWindowLimiter = request.app.state.login_limiter
    key = _client_key(request, credentials.username)
    allowed, retry_after = limiter.allow(key)
    if not allowed:
        response = failure(429, "rate_limited", "登录尝试过于频繁，请稍后重试")
        response.headers["Retry-After"] = str(retry_after)
        return response
    result = request.app.state.auth_service.login(credentials.username, credentials.password)
    response = success({"admin": {"id": result.admin.id, "username": result.admin.username}})
    _set_session_cookies(response, result, request.app.state.settings)
    limiter.reset(key)
    return response


@router.post("/logout")
def logout(request: Request, admin=Depends(require_csrf)):
    request.app.state.auth_service.logout(request.cookies.get(SESSION_COOKIE, ""), admin.id)
    response = success({"logged_out": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return response
