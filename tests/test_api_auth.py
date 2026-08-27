"""认证接口与基础安全边界测试。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from gopay_app.api.app import create_app
from gopay_app.auth.service import CSRF_COOKIE
from gopay_app.config import Settings


def test_setup_login_protection_and_logout(settings: Settings):
    with TestClient(create_app(settings)) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["data"]["stage"] == "P4"

        initial = client.get("/api/v1/auth/status").json()["data"]
        assert initial == {"setup_required": True, "authenticated": False, "admin": None}

        protected = client.get("/api/v1/system/status")
        assert protected.status_code == 401

        setup = client.post(
            "/api/v1/auth/setup",
            json={"username": "admin", "password": "测试-password-123"},
            headers={"Origin": "http://testserver"},
        )
        assert setup.status_code == 201
        assert setup.json()["data"]["admin"]["username"] == "admin"

        system = client.get("/api/v1/system/status")
        assert system.status_code == 200
        assert system.json()["data"]["journal_mode"] == "wal"

        rejected_logout = client.post("/api/v1/auth/logout", headers={"Origin": "http://testserver"})
        assert rejected_logout.status_code == 403

        csrf_token = client.cookies.get(CSRF_COOKIE)
        logout = client.post(
            "/api/v1/auth/logout",
            headers={"Origin": "http://testserver", "X-CSRF-Token": csrf_token},
        )
        assert logout.status_code == 200
        assert client.get("/api/v1/system/status").status_code == 401


def test_rejects_untrusted_origin_and_large_body(settings: Settings):
    with TestClient(create_app(settings)) as client:
        rejected = client.post(
            "/api/v1/auth/setup",
            json={"username": "admin", "password": "测试-password-123"},
            headers={"Origin": "https://example.invalid"},
        )
        assert rejected.status_code == 403
        assert rejected.json()["code"] == "origin_rejected"

        oversized = client.post(
            "/api/v1/auth/setup",
            content=b"x" * (settings.max_request_bytes + 1),
            headers={"Content-Type": "application/json", "Origin": "http://testserver"},
        )
        assert oversized.status_code == 413
