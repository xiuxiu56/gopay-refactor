"""P5 页面配套设置、账号操作与流程日志接口测试。"""

from __future__ import annotations

import json
import uuid

from fastapi.testclient import TestClient

from gopay_app.api.app import create_app
from gopay_app.auth.service import CSRF_COOKIE
from gopay_app.config import Settings
from gopay_app.db.models import (
    Account,
    AccountSecret,
    PaymentIntent,
    Setting,
    SmsActivation,
    Task,
    utc_now,
)

PROXY_POOL = """testuser-region-ID-sid-session0001-t-5:testpass@proxy.example:3010
testuser-region-ID-sid-session0002-t-5:testpass@proxy.example:3010
testuser-region-ID-sid-session0003-t-5:testpass@proxy.example:3010
testuser-region-ID-sid-session0004-t-5:testpass@proxy.example:3010
testuser-region-ID-sid-session0005-t-5:testpass@proxy.example:3010"""


def _setup(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/setup",
        json={"username": "admin", "password": "测试-password-123"},
        headers={"Origin": "http://testserver"},
    )
    assert response.status_code == 201
    return {"Origin": "http://testserver", "X-CSRF-Token": client.cookies.get(CSRF_COOKIE)}


def _create_account(client: TestClient, phone: str = "+628123456789") -> str:
    account_id = str(uuid.uuid4())
    now = utc_now()
    phone_normalized = phone.removeprefix("+")
    is_default = phone == "+628123456789"
    with client.app.state.session_factory() as session, session.begin():
        session.add(
            Account(
                id=account_id,
                phone=phone,
                phone_normalized=phone_normalized,
                local_phone=phone_normalized.removeprefix("62"),
                customer_id="customer-p5" if is_default else f"customer-{phone_normalized}",
                balance=50000,
                pin_setup_status="configured",
                sms_activation_status="active",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            AccountSecret(
                account_id=account_id,
                secret_payload_ciphertext=client.app.state.secret_codec.encrypt(
                    json.dumps(
                        {
                            "pin": "147258",
                            "proxy": "http://saved.example:8080",
                            "access_token": "saved-access-token",
                        }
                    ),
                    context=f"account:{account_id}",
                ),
                updated_at=now,
            )
        )
        session.add(
            SmsActivation(
                id=str(uuid.uuid4()),
                account_id=account_id,
                provider="smsbower",
                provider_activation_id="activation-p5" if is_default else f"activation-{phone_normalized}",
                status="active",
                created_at=now,
                updated_at=now,
            )
        )
    return account_id


def test_account_flow_defaults_encrypt_proxy_profiles(settings: Settings):
    with TestClient(create_app(settings)) as client:
        headers = _setup(client)
        client.app.state.worker_pool.stop()
        saved = client.put(
            "/api/v1/settings/account-flow",
            json={
                "register_pin": "147258",
                "login_pin": "147258",
                "new_pin": "258369",
                "task_count": 1000,
                "concurrency": 3,
                "sms_otp_timeout_seconds": 60,
                "manual_otp_timeout_seconds": 480,
                "change_pin_enabled": True,
                "default_proxy_region": "",
                "proxy_pool": PROXY_POOL,
                "clear_proxy_pool": False,
            },
            headers=headers,
        )
        assert saved.status_code == 200
        data = saved.json()["data"]
        assert data["task_count"] == 1000
        assert data["concurrency"] == 3
        assert data["sms_otp_timeout_seconds"] == 60
        assert "sms_otp_attempts" not in data
        assert data["manual_otp_timeout_seconds"] == 480
        assert data["default_proxy_region"] == "ID"
        assert data["proxy_count"] == 5
        assert data["proxy_profiles"] == [
            {
                "region": "ID",
                "label": "ID 自动分配",
                "description": "印度尼西亚",
                "configured": True,
                "count": 5,
                "masked": "***@proxy.example:3010",
            }
        ]
        assert "testpass" not in saved.text

        with client.app.state.session_factory() as session:
            row = session.get(Setting, "account_flow.proxy_pool")
            assert row is not None
            assert row.is_secret is True
            assert row.value_text == ""
            assert "testpass" not in row.value_ciphertext

        created = client.post(
            "/api/v1/account-flows",
            json={
                "mode": "register",
                "phone_source": "smsbower",
                "phone": "",
                "proxy_region": "ID",
                "count": 5,
                "concurrency": 2,
            },
            headers=headers,
        )
        assert created.status_code == 201
        tasks = created.json()["data"]["tasks"]
        assigned = {
            client.app.state.task_repository.get_execution(item["id"]).payload["proxy"] for item in tasks
        }
        assert len(assigned) == 2
        assert all(value.startswith("http://") for value in assigned)


def test_single_explicit_proxy_does_not_limit_account_flow_concurrency(settings: Settings):
    with TestClient(create_app(settings)) as client:
        headers = _setup(client)
        client.app.state.worker_pool.stop()
        response = client.post(
            "/api/v1/account-flows",
            json={
                "mode": "register",
                "phone_source": "smsbower",
                "pin": "147258",
                "proxy": "http://user:secret@proxy.example:3010",
                "count": 2,
                "concurrency": 2,
            },
            headers=headers,
        )

        assert response.status_code == 201
        data = response.json()["data"]
        assert data["batch"]["desired_concurrency"] == 2
        assert len(data["tasks"]) == 2


def test_proxy_pool_tests_connectivity_before_saving(settings: Settings):
    with TestClient(create_app(settings)) as client:
        headers = _setup(client)
        client.app.state.worker_pool.stop()

        def probe_proxy(proxy: str, *, timeout_sec: float | None = None):
            assert timeout_sec == 8
            if "working" in proxy:
                return {"ok": True, "status": 200, "ip": "203.0.113.8"}
            return {"ok": False, "status": 407, "ip": ""}

        client.app.state.protocol_adapter.probe_proxy = probe_proxy
        response = client.post(
            "/api/v1/settings/account-flow/proxies/test-and-add",
            json={
                "proxy_pool": (
                    "user-region-ID-sid-working:secret@proxy.example:3010\n"
                    "user-region-US-sid-failed:secret@proxy.example:3011"
                )
            },
            headers=headers,
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["proxy_count"] == 1
        assert data["default_proxy_region"] == "ID"
        assert data["proxy_test"]["tested"] == 2
        assert data["proxy_test"]["passed"] == 1
        assert data["proxy_test"]["failed"] == 1
        assert data["proxy_test"]["results"] == [
            {
                "index": 1,
                "region": "ID",
                "proxy": "***@proxy.example:3010",
                "ok": True,
                "ip": "203.0.113.8",
                "status": 200,
                "message": "测试通过",
            },
            {
                "index": 2,
                "region": "US",
                "proxy": "***@proxy.example:3011",
                "ok": False,
                "ip": "",
                "status": 407,
                "message": "代理返回 HTTP 407",
            },
        ]
        assert "secret" not in response.text


def test_account_flow_runs_can_queue_append_and_be_stopped(settings: Settings):
    with TestClient(create_app(settings)) as client:
        headers = _setup(client)
        client.app.state.worker_pool.stop()
        _create_account(client)

        response = client.post(
            "/api/v1/account-flows",
            json={
                "mode": "register",
                "phone_source": "smsbower",
                "pin": "147258",
                "change_pin": False,
                "count": 3,
                "concurrency": 1,
            },
            headers=headers,
        )
        assert response.status_code == 201
        data = response.json()["data"]
        assert len(data["tasks"]) == 1
        assert data["batch"]["total"] == 3

        login = client.post(
            "/api/v1/account-flows",
            json={
                "mode": "login",
                "phone_source": "smsbower",
                "pin": "147258",
                "change_pin": False,
                "count": 1,
                "concurrency": 1,
            },
            headers=headers,
        )
        assert login.status_code == 201
        login_data = login.json()["data"]
        assert login_data["batch"]["id"] != data["batch"]["id"]
        assert login_data["batch"]["total"] == 1
        login_task_id = login_data["tasks"][0]["id"]
        login_payload = client.app.state.task_repository.get_execution(login_task_id).payload
        assert login_payload["phone_source"] == "smsbower"
        assert login_payload["phone"] == ""

        appended = client.post(
            "/api/v1/account-flows",
            json={
                "mode": "register",
                "phone_source": "smsbower",
                "pin": "147258",
                "change_pin": False,
                "count": 2,
                "concurrency": 2,
            },
            headers=headers,
        )
        assert appended.status_code == 201
        appended_data = appended.json()["data"]
        assert appended_data["batch"]["id"] == data["batch"]["id"]
        assert appended_data["batch"]["appended"] == 2
        assert appended_data["batch"]["total"] == 5
        assert appended_data["batch"]["desired_concurrency"] == 2
        assert len(appended_data["tasks"]) == 1

        listed = client.get("/api/v1/account-flows/logs?all=true").json()["data"]
        assert listed["runs"]["current"]["id"] == login_data["batch"]["id"]
        assert listed["runs"]["current"]["remaining"] == 1
        assert listed["runs"]["latest"]["register"]["target"] == 5
        assert listed["runs"]["latest"]["register"]["created"] == 2
        assert listed["runs"]["latest"]["register"]["remaining"] == 5
        assert listed["runs"]["latest"]["login"]["target"] == 1

        stopped = client.post(
            f"/api/v1/account-flows/runs/{data['batch']['id']}/stop",
            json={},
            headers=headers,
        )
        assert stopped.status_code == 200
        assert stopped.json()["data"]["status"] == "stopped"
        assert stopped.json()["data"]["stopped_tasks"] == 2
        current = client.get("/api/v1/account-flows/logs").json()["data"]["runs"]["current"]
        assert current["id"] == login_data["batch"]["id"]

        stopped_login = client.post(
            f"/api/v1/account-flows/runs/{login_data['batch']['id']}/stop",
            json={},
            headers=headers,
        )
        assert stopped_login.status_code == 200
        assert stopped_login.json()["data"]["stopped_tasks"] == 1
        assert client.get("/api/v1/account-flows/logs").json()["data"]["runs"]["current"] is None


def test_account_flow_single_additions_fill_requested_concurrency(settings: Settings):
    runtime_settings = settings.model_copy(update={"worker_count": 8})
    with TestClient(create_app(runtime_settings)) as client:
        headers = _setup(client)
        client.app.state.worker_pool.stop()

        batches = []
        for _index in range(3):
            response = client.post(
                "/api/v1/account-flows",
                json={
                    "mode": "register",
                    "phone_source": "smsbower",
                    "pin": "147258",
                    "count": 1,
                    "concurrency": 5,
                },
                headers=headers,
            )
            assert response.status_code == 201
            batches.append(response.json()["data"])

        batch_id = batches[0]["batch"]["id"]
        assert all(item["batch"]["id"] == batch_id for item in batches)
        assert [item["batch"]["created"] for item in batches] == [1, 1, 1]
        assert batches[-1]["batch"]["total"] == 3
        assert batches[-1]["batch"]["desired_concurrency"] == 5
        listed = client.get("/api/v1/account-flows/logs?all=true").json()["data"]
        assert listed["runs"]["current"]["target"] == 3
        assert listed["runs"]["current"]["created"] == 3
        assert listed["runs"]["current"]["active"] == 3


def test_account_flow_logs_show_phone_and_clear_all_tasks(settings: Settings):
    with TestClient(create_app(settings)) as client:
        headers = _setup(client)
        client.app.state.worker_pool.stop()
        first, _created = client.app.state.task_repository.create_task(
            "account.register",
            {"phone": "+628111111111", "phone_source": "smsbower", "pin": "147258"},
        )
        second, _created = client.app.state.task_repository.create_task(
            "account.login",
            {"phone": "+628222222222", "phone_source": "hero_sms", "pin": "147258"},
        )
        now = utc_now()
        with client.app.state.session_factory() as session, session.begin():
            terminal = session.get(Task, first.id)
            terminal.status = "succeeded"
            terminal.progress = 1
            terminal.finished_at = now
            terminal.updated_at = now

        execution = client.app.state.task_repository.claim_next("test-account-flow-worker")
        assert execution is not None
        assert execution.snapshot.id == second.id
        assert client.app.state.task_repository.update_progress(
            second.id,
            "test-account-flow-worker",
            0.2,
            "正在发送登录 OTP",
        )

        listed = client.get("/api/v1/account-flows/logs?limit=20")
        assert listed.status_code == 200
        data = listed.json()["data"]
        assert data["total"] == 2
        assert data["active"] == {"register": 0, "login": 1}
        assert {item["phone"] for item in data["items"]} == {
            "+628111111111",
            "+628222222222",
        }
        by_id = {item["id"]: item for item in data["items"]}
        assert by_id[first.id]["phone_source"] == "smsbower"
        assert by_id[second.id]["phone_source"] == "hero_sms"
        assert by_id[second.id]["latest_event_message"] == "正在发送登录 OTP"
        assert "147258" not in listed.text
        listed_all = client.get("/api/v1/account-flows/logs?all=true&limit=1").json()["data"]
        assert listed_all["total"] == 2
        assert len(listed_all["items"]) == 2

        cleared = client.delete("/api/v1/account-flows/logs", headers=headers)
        assert cleared.status_code == 200
        assert cleared.json()["data"]["removed"] == 2
        assert cleared.json()["data"]["active_removed"] == 1
        assert cleared.json()["data"]["active_retained"] == 0
        remaining = client.get("/api/v1/account-flows/logs").json()["data"]
        assert remaining == {
            "items": [],
            "total": 0,
            "active": {"register": 0, "login": 0},
            "runs": {"current": None, "latest": {}},
        }


def test_account_more_actions_create_tasks_and_delete_account(settings: Settings):
    with TestClient(create_app(settings)) as client:
        headers = _setup(client)
        client.app.state.worker_pool.stop()
        account_id = _create_account(client)
        second_account_id = _create_account(client, "+628123456780")

        paged = client.get("/api/v1/accounts?limit=1").json()["data"]
        assert paged["total"] == 2
        assert len(paged["items"]) == 1
        listed_response = client.get("/api/v1/accounts?all=true&limit=1")
        listed_all = listed_response.json()["data"]
        assert listed_all["total"] == 2
        assert {item["id"] for item in listed_all["items"]} == {account_id, second_account_id}
        assert {item["pin"] for item in listed_all["items"]} == {"147258"}
        assert {item["account_status"] for item in listed_all["items"]} == {"available"}
        assert {item["account_status_label"] for item in listed_all["items"]} == {"可用"}
        assert {item["phone_source"] for item in listed_all["items"]} == {"smsbower"}
        assert "saved.example" not in listed_response.text
        assert "saved-access-token" not in listed_response.text
        assert '"proxy"' not in listed_response.text

        detail = client.get(f"/api/v1/accounts/{account_id}")
        assert detail.status_code == 200
        assert detail.json()["data"]["pin"] == "147258"
        assert "saved.example" not in detail.text

        pool = client.get(f"/api/v1/accounts/{account_id}/pool-format")
        assert pool.status_code == 200
        assert pool.json()["data"]["value"] == "+628123456789----smsbower://activation-p5"

        expected = {
            "refresh": "account.refresh",
            "check-pin": "account.check_pin",
            "release-number": "account.release_number",
            "refresh-sms-code": "account.refresh_sms_code",
            "relogin": "account.login",
        }
        latest_code_task_id = ""
        for action, task_type in expected.items():
            response = client.post(
                f"/api/v1/accounts/{account_id}/actions/{action}",
                json={},
                headers=headers,
            )
            assert response.status_code == 201
            assert response.json()["data"]["task"]["task_type"] == task_type
            if action == "refresh-sms-code":
                latest_code_task_id = response.json()["data"]["task"]["id"]

        with client.app.state.session_factory() as session, session.begin():
            task = session.get(Task, latest_code_task_id)
            task.status = "succeeded"
            task.result_ciphertext = client.app.state.secret_codec.encrypt(
                json.dumps(
                    {
                        "account_id": account_id,
                        "activation_id": "activation-p5",
                        "code": "246810",
                    }
                ),
                context=f"task:{latest_code_task_id}:result",
            )
            task.finished_at = utc_now()
            task.updated_at = utc_now()

        task_detail = client.get(f"/api/v1/tasks/{latest_code_task_id}")
        assert task_detail.status_code == 200
        assert "246810" not in task_detail.text
        code_result = client.post(
            f"/api/v1/accounts/{account_id}/actions/refresh-sms-code/{latest_code_task_id}/result",
            json={},
            headers=headers,
        )
        assert code_result.status_code == 200
        assert code_result.json()["data"]["code"] == "246810"
        consumed = client.post(
            f"/api/v1/accounts/{account_id}/actions/refresh-sms-code/{latest_code_task_id}/result",
            json={},
            headers=headers,
        )
        assert consumed.status_code == 409
        assert consumed.json()["code"] == "sms_code_consumed"

        changed = client.post(
            f"/api/v1/accounts/{account_id}/actions/change-pin",
            json={"old_pin": "147258", "new_pin": "258369"},
            headers=headers,
        )
        assert changed.status_code == 201
        assert changed.json()["data"]["task"]["task_type"] == "account.login"
        assert "258369" not in changed.text

        deleted = client.delete(f"/api/v1/accounts/{account_id}", headers=headers)
        assert deleted.status_code == 200
        assert deleted.json()["data"]["deleted"] is True
        assert client.get(f"/api/v1/accounts/{account_id}").status_code == 404


def test_account_status_uses_latest_payment_state_with_chinese_labels(settings: Settings):
    with TestClient(create_app(settings)) as client:
        _setup(client)
        client.app.state.worker_pool.stop()
        account_id = _create_account(client)
        now = utc_now()
        payment_id = str(uuid.uuid4())
        with client.app.state.session_factory() as session, session.begin():
            session.add(
                PaymentIntent(
                    id=payment_id,
                    snap_token_hash="status-payment-token-hash",
                    order_id="status-order",
                    account_id=account_id,
                    task_id=None,
                    status="running",
                    amount=1,
                    currency="IDR",
                    transaction_status="",
                    last_error_message="",
                    midtrans_url_ciphertext="",
                    raw_state_ciphertext="",
                    created_at=now,
                    updated_at=now,
                )
            )

        running = client.get(f"/api/v1/accounts/{account_id}").json()["data"]
        assert (running["account_status"], running["account_status_label"]) == ("reserved", "使用中")

        with client.app.state.session_factory() as session, session.begin():
            payment = session.get(PaymentIntent, payment_id)
            payment.status = "succeeded"
            payment.updated_at = utc_now()
        succeeded = client.get(f"/api/v1/accounts/{account_id}").json()["data"]
        assert (succeeded["account_status"], succeeded["account_status_label"]) == (
            "payment_success",
            "支付成功",
        )

        with client.app.state.session_factory() as session, session.begin():
            payment = session.get(PaymentIntent, payment_id)
            payment.status = "failed"
            payment.last_error_message = "支付验证码校验失败"
            payment.updated_at = utc_now()
        failed = client.get(f"/api/v1/accounts/{account_id}").json()["data"]
        assert (failed["account_status"], failed["account_status_label"]) == (
            "payment_failed",
            "支付失败",
        )
        assert failed["account_status_message"] == "支付验证码校验失败"
