"""P4 支付创建与复核接口测试。"""

from __future__ import annotations

import json
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from gopay_app.api.app import create_app
from gopay_app.auth.service import CSRF_COOKIE
from gopay_app.config import Settings
from gopay_app.db.models import Account, AccountSecret, PaymentIntent, Task, utc_now

SNAP = "11111111-2222-3333-4444-555555555555"
MIDTRANS_URL = f"https://app.midtrans.com/snap/v4/redirection/{SNAP}"


def _setup(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/setup",
        json={"username": "admin", "password": "测试-password-123"},
        headers={"Origin": "http://testserver"},
    )
    assert response.status_code == 201
    return {"Origin": "http://testserver", "X-CSRF-Token": client.cookies.get(CSRF_COOKIE)}


def test_payment_intent_and_encrypted_task_are_created(settings: Settings):
    with TestClient(create_app(settings)) as client:
        headers = _setup(client)
        client.app.state.worker_pool.stop()
        account_id = str(uuid.uuid4())
        now = utc_now()
        with client.app.state.session_factory() as session, session.begin():
            session.add(
                Account(
                    id=account_id,
                    phone="+628123456789",
                    phone_normalized="628123456789",
                    local_phone="8123456789",
                    balance=100,
                    pin_setup_status="configured",
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                AccountSecret(
                    account_id=account_id,
                    secret_payload_ciphertext=client.app.state.secret_codec.encrypt(
                        json.dumps({"pin": "147258"}),
                        context=f"account:{account_id}",
                    ),
                    updated_at=now,
                )
            )

        response = client.post(
            "/api/v1/payments",
            json={
                "midtrans_url": MIDTRANS_URL,
                "account_id": account_id,
                "pin": "",
                "proxy": "http://payment-user:payment-pass@proxy.example:8080",
            },
            headers=headers,
        )
        assert response.status_code == 201
        data = response.json()["data"]
        assert data["created"] is True
        assert data["task"]["task_type"] == "payment.execute"
        assert data["payment"]["task_id"] == data["task"]["id"]
        assert SNAP not in response.text
        assert "147258" not in response.text
        assert "payment-pass" not in response.text

        with client.app.state.session_factory() as session:
            intent = session.get(PaymentIntent, data["payment"]["id"])
            assert intent is not None
            assert SNAP not in intent.midtrans_url_ciphertext
            assert intent.status == "queued"

        types = client.get("/api/v1/tasks/types").json()["data"]
        names = {item["task_type"] for item in types}
        assert {"payment.execute", "payment.reconcile"} <= names

        reconcile = client.post(
            f"/api/v1/payments/{data['payment']['id']}/reconcile",
            json={},
            headers=headers,
        )
        assert reconcile.status_code == 201
        assert reconcile.json()["data"]["task"]["task_type"] == "payment.reconcile"

        cleared = client.delete("/api/v1/payments", headers=headers)
        assert cleared.status_code == 200
        cleared_data = cleared.json()["data"]
        assert cleared_data["removed"] == 1
        assert cleared_data["tasks_removed"] == 2
        assert cleared_data["active_tasks_removed"] == 2
        assert client.get("/api/v1/payments").json()["data"]["total"] == 0

        with client.app.state.session_factory() as session:
            assert session.get(PaymentIntent, data["payment"]["id"]) is None
            payment_tasks = session.scalars(
                select(Task).where(Task.task_type.in_(("payment.execute", "payment.reconcile")))
            ).all()
            assert payment_tasks == []
