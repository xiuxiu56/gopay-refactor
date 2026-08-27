"""认证依赖。"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from gopay_app.auth.service import CSRF_COOKIE, SESSION_COOKIE, AuthenticatedAdmin, AuthService


def auth_service(request: Request) -> AuthService:
    return request.app.state.auth_service


def optional_admin(
    request: Request,
    service: AuthService = Depends(auth_service),
) -> AuthenticatedAdmin | None:
    return service.authenticate(request.cookies.get(SESSION_COOKIE, ""))


def require_admin(admin: AuthenticatedAdmin | None = Depends(optional_admin)) -> AuthenticatedAdmin:
    if admin is None:
        raise HTTPException(
            status_code=401, detail={"code": "unauthenticated", "message": "请先登录本地控制台"}
        )
    return admin


def require_csrf(
    request: Request,
    admin: AuthenticatedAdmin = Depends(require_admin),
    service: AuthService = Depends(auth_service),
) -> AuthenticatedAdmin:
    session_token = request.cookies.get(SESSION_COOKIE, "")
    header_token = request.headers.get("X-CSRF-Token", "")
    cookie_token = request.cookies.get(CSRF_COOKIE, "")
    if (
        not header_token
        or header_token != cookie_token
        or not service.verify_csrf(session_token, header_token)
    ):
        raise HTTPException(
            status_code=403, detail={"code": "csrf_failed", "message": "请求校验已失效，请刷新页面后重试"}
        )
    return admin
