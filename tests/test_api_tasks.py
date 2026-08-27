"""P1 任务、账号与 SSE 接口测试。"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from gopay_app.api.app import create_app
from gopay_app.auth.service import CSRF_COOKIE
from gopay_app.config import Settings


def _setup(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/setup",
        json={"username": "admin", "password": "测试-password-123"},
        headers={"Origin": "http://testserver"},
    )
    assert response.status_code == 201
    return {"Origin": "http://testserver", "X-CSRF-Token": client.cookies.get(CSRF_COOKIE)}


def _wait_status(client: TestClient, task_id: str, status: str) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        data = client.get(f"/api/v1/tasks/{task_id}").json()["data"]["task"]
        if data["status"] == status:
            return data
        time.sleep(0.02)
    raise AssertionError("等待接口任务状态超时")


def test_task_api_csrf_idempotency_input_and_sse(settings: Settings):
    with TestClient(create_app(settings)) as client:
        headers = _setup(client)
        rejected = client.post(
            "/api/v1/tasks",
            json={"task_type": "system.echo", "payload": {"value": "测试"}},
            headers={"Origin": "http://testserver"},
        )
        assert rejected.status_code == 403

        request_body = {
            "task_type": "system.wait_input",
            "payload": {"input_type": "otp", "timeout_seconds": 60},
            "idempotency_key": "api-otp-1",
        }
        created = client.post("/api/v1/tasks", json=request_body, headers=headers)
        assert created.status_code == 201
        task_id = created.json()["data"]["task"]["id"]
        duplicate = client.post("/api/v1/tasks", json=request_body, headers=headers)
        assert duplicate.status_code == 200
        assert duplicate.json()["data"] == {
            "task": duplicate.json()["data"]["task"],
            "created": False,
        }
        assert duplicate.json()["data"]["task"]["id"] == task_id

        _wait_status(client, task_id, "waiting_input")
        submitted = client.post(
            f"/api/v1/tasks/{task_id}/input",
            json={"input_type": "otp", "value": "246810"},
            headers=headers,
        )
        assert submitted.status_code == 200
        _wait_status(client, task_id, "succeeded")

        listing = client.get("/api/v1/tasks?status=succeeded")
        assert listing.status_code == 200
        assert listing.json()["data"]["total"] == 1

        realtime = client.get("/api/v1/realtime?after=0&once=true")
        assert realtime.status_code == 200
        assert realtime.headers["content-type"].startswith("text/event-stream")
        assert "event: task.updated" in realtime.text
        assert f'"resource_id":"{task_id}"' in realtime.text


def test_task_type_validation_and_account_list(settings: Settings):
    with TestClient(create_app(settings)) as client:
        headers = _setup(client)
        unknown = client.post(
            "/api/v1/tasks",
            json={"task_type": "unknown.task"},
            headers=headers,
        )
        assert unknown.status_code == 422
        assert client.get("/api/v1/tasks/types").status_code == 200
        accounts = client.get("/api/v1/accounts")
        assert accounts.status_code == 200
        assert accounts.json()["data"] == {"items": [], "total": 0}
