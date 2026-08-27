"""P2 Vue 静态托管与支付公开摘要接口测试。"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from gopay_app.api.app import create_app
from gopay_app.config import Settings
from gopay_app.db.models import PaymentIntent, utc_now


def _setup(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/setup",
        json={"username": "admin", "password": "测试-password-123"},
        headers={"Origin": "http://testserver"},
    )
    assert response.status_code == 201


def test_spa_index_history_fallback_and_api_404(settings: Settings):
    with TestClient(create_app(settings)) as client:
        index = client.get("/")
        assert index.status_code == 200
        assert index.headers["content-type"].startswith("text/html")
        assert '<div id="app"></div>' in index.text
        assert "/assets/index-" in index.text

        login = client.get("/login")
        assert login.status_code == 200
        assert login.text == index.text

        missing_api = client.get("/api/v1/not-present")
        assert missing_api.status_code == 404
        assert missing_api.json()["code"] == "route_not_found"


def test_payment_api_never_returns_encrypted_or_hashed_secrets(settings: Settings):
    with TestClient(create_app(settings)) as client:
        _setup(client)
        payment_id = str(uuid.uuid4())
        now = utc_now()
        with client.app.state.session_factory() as session, session.begin():
            session.add(
                PaymentIntent(
                    id=payment_id,
                    snap_token_hash="a" * 64,
                    order_id="order-public-1",
                    account_id=None,
                    status="pending",
                    midtrans_url_ciphertext="enc:v1:secret-url",
                    raw_state_ciphertext="enc:v1:secret-state",
                    created_at=now,
                    updated_at=now,
                )
            )

        response = client.get("/api/v1/payments")
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 1
        assert data["items"][0]["id"] == payment_id
        assert data["items"][0]["has_midtrans_url"] is True
        assert "snap_token_hash" not in data["items"][0]
        assert "secret-url" not in response.text

        detail = client.get(f"/api/v1/payments/{payment_id}")
        assert detail.status_code == 200
        assert detail.json()["data"]["order_id"] == "order-public-1"
