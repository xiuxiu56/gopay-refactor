"""注册成功后的独立激活奖励任务测试。"""

from __future__ import annotations

import json
import time
import uuid
from types import SimpleNamespace

from sqlalchemy import select

from gopay_app.db.models import Account, AccountSecret, utc_now
from gopay_app.tasks.handlers.account_flow import _auth_fields
from gopay_app.tasks.handlers.post_registration import AccountPostRegisterHandler
from gopay_app.tasks.registry import HandlerRegistry
from gopay_app.tasks.repository import TaskRepository
from gopay_app.tasks.worker_pool import WorkerPool


def _auth() -> SimpleNamespace:
    values = {field: "" for field in _auth_fields}
    values.update({"twofa_methods": [], "methods": [], "otp_length": 6, "user_registered": True})
    return SimpleNamespace(**values)


class PostRegisterClient:
    def __init__(self, adapter) -> None:
        self.adapter = adapter
        self.auth = _auth()
        self.d1 = "d1-post"
        self.model = "设备型号"
        self.xm1_template = "xm1"
        self.phone_make = "设备品牌"
        self.os_info = "Android"
        self.appid = "应用标识"
        self.version = "1.0"
        self.user_uuid = "customer-post"
        self.session_id = "session-post"
        self.device_token = "device-token-post"
        self.uniqueid = "unique-post"
        self.balance_reads = 0

    def _ok(self, name: str):
        self.adapter.calls.append(name)
        return {"status": 200, "body": {"success": True}}

    def pin_post_registration_hook(self):
        return self._ok("pin_post_registration_hook")

    def security_meter(self, *_args, **_kwargs):
        return self._ok("security_meter")

    def get_user_profile(self):
        return self._ok("get_user_profile")

    def gojek_customer_profile(self):
        return self._ok("gojek_customer_profile")

    def courier_token(self):
        return self._ok("courier_token")

    def litmus_public_experiments(self):
        return self._ok("litmus_public_experiments")

    def litmus_experiments(self):
        return self._ok("litmus_experiments")

    def festivals_assets(self):
        return self._ok("festivals_assets")

    def gopay_get_balances(self):
        return self._ok("gopay_get_balances")

    def red_badges(self):
        return self._ok("red_badges")

    def gopay_get_profiles(self):
        return self._ok("gopay_get_profiles")

    def kyc_status(self):
        return self._ok("kyc_status")

    def support_customer_session(self):
        return self._ok("support_customer_session")

    def support_customer_activity(self):
        return self._ok("support_customer_activity")

    def refresh_token(self):
        self.auth.access_token = "post-access-refreshed"
        return self._ok("refresh_token")

    def wallet_card_balance(self):
        return self._ok("wallet_card_balance")

    def get_balance(self):
        self.balance_reads += 1
        balance = 0 if self.balance_reads == 1 else 9000
        return {"status": 200, "body": {"data": [{"balance": {"value": balance}}]}}


class PostRegisterAdapter:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def new_gojek_client(self, _phone: str, **_kwargs):
        return PostRegisterClient(self)

    def account_request_pause(self, _seconds: float) -> None:
        return None

    def claim_configured_envelope(self, _client):
        self.calls.append("claim_configured_envelope")
        return {"status": "claimed", "http_status": 200}


class WarningPostRegisterClient(PostRegisterClient):
    def _ok(self, name: str):
        self.adapter.calls.append(name)
        return {"status": 503, "body": {}}

    def get_balance(self):
        return {"status": 503, "body": {}}


class WarningPostRegisterAdapter(PostRegisterAdapter):
    def new_gojek_client(self, _phone: str, **_kwargs):
        return WarningPostRegisterClient(self)

    def claim_configured_envelope(self, _client):
        raise TimeoutError("测试超时")


def _create_account(session_factory, codec, phone: str) -> str:
    account_id = str(uuid.uuid4())
    now = utc_now()
    protocol_auth = {field: "" for field in _auth_fields}
    protocol_auth.update(
        {
            "access_token": "post-access",
            "refresh_token": "post-refresh",
            "account_id": "remote-post",
            "twofa_methods": [],
            "methods": [],
        }
    )
    secret = {
        "access_token": "post-access",
        "refresh_token": "post-refresh",
        "proxy": "",
        "protocol_client": {
            "auth": protocol_auth,
            "user_uuid": "customer-post",
            "session_id": "session-post",
            "device_token": "device-token-post",
            "uniqueid": "unique-post",
        },
    }
    with session_factory() as session, session.begin():
        session.add(
            Account(
                id=account_id,
                phone=phone,
                phone_normalized="".join(character for character in phone if character.isdigit()),
                local_phone=phone.removeprefix("+62"),
                balance=0,
                pin_setup_status="configured",
                registered_at=now.isoformat().replace("+00:00", "Z"),
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            AccountSecret(
                account_id=account_id,
                secret_payload_ciphertext=codec.encrypt(
                    json.dumps(secret, ensure_ascii=False),
                    context=f"account:{account_id}",
                ),
                updated_at=now,
            )
        )
    return account_id


def _wait(repository: TaskRepository, task_id: str, status: str) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if repository.get_task(task_id).status == status:
            return
        time.sleep(0.02)
    raise AssertionError(f"等待任务状态超时：{status}")


def _run_post_task(database, adapter, account_id: str):
    _engine, session_factory, codec = database
    repository = TaskRepository(session_factory, codec, lease_seconds=10, retry_base_seconds=0.01)
    registry = HandlerRegistry()
    registry.register(
        "account.post_register",
        AccountPostRegisterHandler(session_factory, codec, adapter),
        safe_to_retry=True,
    )
    task, _created = repository.create_task(
        "account.post_register",
        {
            "account_id": account_id,
            "parent_task_id": "parent-register-task",
            "claim_configured_envelope": True,
        },
        max_attempts=2,
    )
    pool = WorkerPool(
        repository,
        registry,
        worker_count=1,
        heartbeat_seconds=0.2,
        poll_seconds=0.01,
        shutdown_seconds=3,
    )
    pool.start()
    return repository, task, pool


def test_post_registration_runs_activation_warmup_envelope_and_reward(database):
    _engine, session_factory, codec = database
    account_id = _create_account(session_factory, codec, "+628111111101")
    adapter = PostRegisterAdapter()
    repository, task, pool = _run_post_task(database, adapter, account_id)
    try:
        _wait(repository, task.id, "succeeded")
        result = repository.consume_result(task.id)
        assert result["activation_status"] == "activated"
        assert result["reward_status"] == "arrived"
        assert result["envelope_status"] == "claimed"
        assert result["balance"] == 9000
        assert result["warnings"] == []
        assert "pin_post_registration_hook" in adapter.calls
        assert "festivals_assets" in adapter.calls
        assert "support_customer_session" in adapter.calls
        assert "support_customer_activity" in adapter.calls
        assert "claim_configured_envelope" in adapter.calls
        with session_factory() as session:
            account = session.get(Account, account_id)
            assert account is not None
            assert account.balance == 9000
            secret_row = session.get(AccountSecret, account_id)
            secret = json.loads(
                codec.decrypt(
                    secret_row.secret_payload_ciphertext,
                    context=f"account:{account_id}",
                )
            )
            assert secret["post_registration"]["activation_status"] == "activated"
            assert secret["post_registration"]["reward_status"] == "arrived"
    finally:
        pool.stop()


def test_post_registration_warnings_do_not_change_core_account_success(database, monkeypatch):
    monkeypatch.setenv("GOPAY_POST_REGISTER_BALANCE_WAIT_SECONDS", "0")
    _engine, session_factory, codec = database
    account_id = _create_account(session_factory, codec, "+628111111102")
    adapter = WarningPostRegisterAdapter()
    repository, task, pool = _run_post_task(database, adapter, account_id)
    try:
        _wait(repository, task.id, "succeeded")
        result = repository.consume_result(task.id)
        assert result["activation_status"] == "pending"
        assert result["reward_status"] == "pending"
        assert result["envelope_status"] == "pending"
        assert result["warnings"]
        with session_factory() as session:
            account = session.scalar(select(Account).where(Account.id == account_id))
            assert account is not None
            assert account.pin_setup_status == "configured"
            assert account.balance == 0
    finally:
        pool.stop()
