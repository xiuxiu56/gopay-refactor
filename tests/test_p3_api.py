"""P3 账号流程与 SMSBower 配置接口测试。"""

from __future__ import annotations

from pathlib import Path

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


def test_sms_settings_are_masked_and_account_batch_is_created(settings: Settings, tmp_path: Path):
    isolated = settings.model_copy(update={"legacy_app_path": tmp_path / "missing-protocol"})
    with TestClient(create_app(isolated)) as client:
        headers = _setup(client)
        client.app.state.worker_pool.stop()
        sources = client.get("/api/v1/account-flows/sources")
        assert sources.status_code == 200
        assert sources.json()["data"] == [
            {
                "value": "smsbower",
                "label": "SMSBower 自动取号",
                "description": "注册和已有账号登录都会自动申请新号码并取码",
                "modes": ["register", "login"],
                "available": False,
            },
            {
                "value": "hero_sms",
                "label": "Hero-SMS 自动取号",
                "description": "使用 Hero-SMS 的印度尼西亚 Gojek 号码自动取号并取码",
                "modes": ["register", "login"],
                "available": False,
            },
        ]
        created = client.post(
            "/api/v1/account-flows",
            json={
                "mode": "register",
                "phone_source": "smsbower",
                "phone": "",
                "pin": "147258",
                "proxy": None,
                "count": 3,
                "concurrency": 2,
            },
            headers=headers,
        )
        assert created.status_code == 201
        data = created.json()["data"]
        assert data["batch"]["total"] == 3
        assert data["batch"]["desired_concurrency"] == 2
        assert data["batch"]["strategy"] == "rolling"
        assert data["batch"]["created"] == 2
        assert len(data["tasks"]) == 2
        assert all(item["max_attempts"] == 1 for item in data["tasks"])
        stopped = client.post(
            f"/api/v1/account-flows/runs/{data['batch']['id']}/stop",
            json={},
            headers=headers,
        )
        assert stopped.status_code == 200

        # 停止测试 Worker 后验证批次持久化，避免访问短信网络。
        saved = client.put(
            "/api/v1/settings/smsbower",
            json={
                "api_key": "p3-secret-api-key",
                "base_url": "https://sms.example.test",
                "service": "ni",
                "country": "6",
            },
            headers=headers,
        )
        assert saved.status_code == 200
        assert saved.json()["data"]["api_key_configured"] is True
        assert "p3-secret-api-key" not in saved.text
        configured_sources = client.get("/api/v1/account-flows/sources").json()["data"]
        assert configured_sources[0]["available"] is True

        saved_hero = client.put(
            "/api/v1/settings/hero-sms",
            json={
                "api_key": "hero-p3-secret-key",
                "base_url": "https://hero-sms.example/stubs/handler_api.php",
                "service": "ni",
                "country": "6",
            },
            headers=headers,
        )
        assert saved_hero.status_code == 200
        assert saved_hero.json()["data"]["api_key_configured"] is True
        assert "hero-p3-secret-key" not in saved_hero.text
        configured_sources = client.get("/api/v1/account-flows/sources").json()["data"]
        assert configured_sources[1]["available"] is True

        hero_login = client.post(
            "/api/v1/account-flows",
            json={
                "mode": "login",
                "phone_source": "hero_sms",
                "phone": "",
                "pin": "147258",
                "change_pin": False,
                "count": 2,
                "concurrency": 2,
            },
            headers=headers,
        )
        assert hero_login.status_code == 201
        hero_tasks = hero_login.json()["data"]["tasks"]
        assert len(hero_tasks) == 2
        for task in hero_tasks:
            execution = client.app.state.task_repository.get_execution(task["id"])
            assert execution.payload["phone_source"] == "hero_sms"
            assert execution.payload["phone"] == ""

        client.app.state.protocol_adapter.sms_balance_for = lambda *_args, **_kwargs: "12.75"
        tested_hero = client.post(
            "/api/v1/settings/hero-sms/test",
            json={
                "api_key": None,
                "base_url": "https://hero-sms.example/stubs/handler_api.php",
                "service": "ni",
                "country": "6",
            },
            headers=headers,
        )
        assert tested_hero.status_code == 200
        assert tested_hero.json()["data"] == {
            "provider": "hero_sms",
            "balance": "12.75",
            "message": "Hero-SMS 连接正常",
        }

        loaded = client.get("/api/v1/settings/smsbower")
        assert loaded.status_code == 200
        assert loaded.json()["data"]["api_key_masked"].startswith("p3-")
        assert "p3-secret-api-key" not in loaded.text

        types = client.get("/api/v1/tasks/types").json()["data"]
        names = {item["task_type"] for item in types}
        assert {"account.register", "account.login"} <= names
