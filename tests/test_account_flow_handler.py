"""P3 注册与登录可恢复状态机测试。"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from types import SimpleNamespace

from sqlalchemy import select

from gopay_app.db.models import Account, AccountSecret, SmsActivation, utc_now
from gopay_app.services.account_flow_defaults import AccountFlowDefaultsStore
from gopay_app.services.sms_settings import HeroSmsSettingsStore, SmsSettingsStore
from gopay_app.tasks.errors import PermanentTaskError
from gopay_app.tasks.handlers.account_flow import AccountFlowHandler, _expect
from gopay_app.tasks.registry import HandlerRegistry
from gopay_app.tasks.repository import TaskRepository
from gopay_app.tasks.worker_pool import WorkerPool


def _auth() -> SimpleNamespace:
    return SimpleNamespace(
        transaction_id="tx",
        verification_id="",
        otp_token="",
        otp_length=6,
        otp_channel="",
        verification_token="",
        onefa_token="",
        account_id="",
        access_token="",
        refresh_token="",
        twofa_token="",
        twofa_methods=[],
        user_registered=True,
        methods=[],
        pin_otp_auth_token="",
        pin_challenge_id="",
        pin_client_id="",
        pin_token="",
    )


class FakeRegisterClient:
    def __init__(self, phone: str) -> None:
        self.phone = phone
        self.auth = _auth()
        self.user_uuid = ""
        self.session_id = "session-register"
        self.device_token = "device-token"
        self.uniqueid = "device-unique"

    def get_login_methods(self, _country_code, _phone):
        return {"status": 401, "body": {"errors": [{"code": "user:not_found"}]}}

    def signup_request_otp(self, _phone, _country_code):
        self.auth.verification_id = "signup-verification"
        self.auth.otp_token = "signup-otp-token"
        return {"status": 200, "body": {}}

    def signup_verify_otp(self, otp, _phone):
        assert otp == "123456"
        self.auth.verification_token = "signup-verified"
        return {"status": 200, "body": {}}

    def signup_create_account(self, **_kwargs):
        self.user_uuid = "customer-1"
        self.auth.account_id = "remote-account-1"
        self.auth.access_token = "access-1"
        self.auth.refresh_token = "refresh-1"
        return {"status": 201, "body": {}}

    def refresh_token(self):
        self.auth.access_token = "access-refreshed"
        return {"status": 201, "body": {}}

    def gopay_init(self):
        return {"status": 200, "body": {}}

    def get_user_profile(self):
        return {"status": 200, "body": {"data": {"is_pin_setup": False}}}

    def pin_check_allowed(self, _pin):
        return {"status": 200, "body": {}}

    def pin_request_otp(self):
        self.auth.verification_id = "pin-verification"
        self.auth.otp_token = "pin-otp-token"
        return {"status": 200, "body": {}}

    def pin_verify_otp(self, otp):
        assert otp == "654321"
        self.auth.verification_token = "pin-verified"
        return {"status": 200, "body": {}}

    def pin_setup(self, pin):
        assert pin == "147258"
        return {"status": 201, "body": {}}

    def get_balance(self):
        return {"status": 200, "body": {"data": [{"balance": {"value": 12500}}]}}


class FakeAdapter:
    def new_gojek_client(self, phone: str, **_kwargs):
        return FakeRegisterClient(phone)


class FakeRegister206Client(FakeRegisterClient):
    def get_login_methods(self, _country_code, _phone):
        if self.auth.verification_token != "signup-verified":
            return {"status": 401, "body": {"errors": [{"code": "user:not_found"}]}}
        self.auth.verification_id = "signup-relogin-verification"
        self.auth.methods = ["otp_sms"]
        return {"status": 200, "body": {}}

    def signup_create_account(self, **_kwargs):
        self.user_uuid = "customer-206"
        return {
            "status": 206,
            "body": {
                "success": True,
                "data": {
                    "customer": {"active": True, "phone_verified": True},
                },
            },
        }

    def initiate_otp(self, _country_code, _phone, *, method, flow):
        assert method == "otp_sms"
        assert flow == "login_1fa"
        self.auth.otp_token = "signup-relogin-otp-token"
        return {"status": 200, "body": {}}

    def verify_otp(self, otp, *, flow):
        assert otp == "246810"
        assert flow == "login_1fa"
        self.auth.verification_token = "signup-relogin-verified"
        return {"status": 200, "body": {}}

    def get_account_list(self):
        self.auth.onefa_token = "signup-relogin-onefa-token"
        self.auth.account_id = "remote-account-206"
        return {"status": 200, "body": {}}

    def issue_token(self, *, grant_type, token_value):
        assert grant_type == "cvs"
        assert token_value == "signup-relogin-onefa-token"
        self.auth.access_token = "access-206"
        self.auth.refresh_token = "refresh-206"
        return {"status": 201, "body": {}}


class FakeRegister206Adapter:
    def new_gojek_client(self, phone: str, **_kwargs):
        return FakeRegister206Client(phone)


class FakeLoginClient:
    def __init__(self, phone: str) -> None:
        self.phone = phone
        self.auth = _auth()
        self.user_uuid = ""
        self.session_id = "session-login"
        self.device_token = "device-token-login"
        self.uniqueid = "device-unique-login"

    def get_login_methods(self, _country_code, _phone):
        self.auth.verification_id = "login-verification"
        self.auth.methods = ["goto_pin", "otp_sms"]
        return {"status": 200, "body": {}}

    def initiate_otp(self, _country_code, _phone, *, method, flow):
        if method == "goto_pin":
            assert flow == "login_1fa"
            self.auth.pin_challenge_id = "login-pin-challenge"
        else:
            assert method == "otp_sms"
            assert flow == "login_2fa"
            self.auth.otp_token = "login-twofa-otp-token"
        return {"status": 200, "body": {}}

    def login_pin_verify(self, pin):
        assert pin == "147258"
        self.auth.pin_token = "login-pin-token"
        return {"status": 200, "body": {}}

    def verify_pin_via_cvs(self):
        self.auth.verification_token = "login-pin-verified"
        return {"status": 200, "body": {}}

    def get_account_list(self):
        self.auth.onefa_token = "login-onefa-token"
        self.auth.account_id = "remote-account-login"
        self.user_uuid = "customer-login"
        return {"status": 200, "body": {}}

    def issue_token(self, *, grant_type, token_value):
        if grant_type == "cvs":
            assert token_value == "login-onefa-token"
            self.auth.twofa_token = "login-twofa-token"
            return {"status": 403, "body": {"code": "twofa_required"}}
        assert grant_type == "challenge"
        assert token_value == "login-twofa-token"
        self.auth.access_token = "login-access-token"
        self.auth.refresh_token = "login-refresh-token"
        return {"status": 201, "body": {}}

    def verify_otp(self, otp, *, flow):
        assert otp == "246810"
        assert flow == "login_2fa"
        self.auth.verification_token = "login-twofa-verified"
        return {"status": 200, "body": {}}

    def gopay_init(self):
        return {"status": 200, "body": {}}

    def get_user_profile(self):
        return {"status": 200, "body": {"data": {"is_pin_setup": True}}}

    def pin_create_challenge(self, *, flow):
        assert flow == "UPDATE_PIN"
        self.auth.pin_challenge_id = "change-pin-challenge"
        self.auth.pin_client_id = "change-pin-client"
        return {"status": 200, "body": {}}

    def pin_verify(self, pin):
        assert pin == "147258"
        self.auth.pin_token = "change-pin-token"
        return {"status": 200, "body": {}}

    def pin_update_v3(self, new_pin):
        assert new_pin == "369258"
        return {"status": 200, "body": {}}

    def get_balance(self):
        return {"status": 200, "body": {"data": [{"balance": {"value": 23600}}]}}


class FakeLoginAdapter:
    def new_gojek_client(self, phone: str, **_kwargs):
        return FakeLoginClient(phone)


class FakeOtpLoginClient:
    def __init__(self, phone: str) -> None:
        self.phone = phone
        self.auth = _auth()
        self.user_uuid = "customer-otp-login"
        self.session_id = "session-otp-login"
        self.device_token = "device-token-otp-login"
        self.uniqueid = "device-unique-otp-login"

    def get_login_methods(self, _country_code, _phone):
        self.auth.verification_id = "otp-login-verification"
        self.auth.methods = ["otp_sms"]
        return {"status": 200, "body": {}}

    def initiate_otp(self, _country_code, _phone, *, method, flow):
        assert method == "otp_sms"
        assert flow == "login_1fa"
        self.auth.otp_token = "otp-login-token"
        return {"status": 200, "body": {}}

    def verify_otp(self, otp, *, flow):
        assert otp == "135790"
        assert flow == "login_1fa"
        self.auth.verification_token = "otp-login-verified"
        return {"status": 200, "body": {}}

    def get_account_list(self):
        self.auth.onefa_token = "otp-login-onefa"
        self.auth.account_id = "remote-otp-login"
        return {"status": 200, "body": {}}

    def issue_token(self, *, grant_type, token_value):
        assert grant_type == "cvs"
        assert token_value == "otp-login-onefa"
        self.auth.access_token = "otp-login-access"
        self.auth.refresh_token = "otp-login-refresh"
        return {"status": 201, "body": {}}

    def gopay_init(self):
        return {"status": 200, "body": {}}

    def get_user_profile(self):
        return {"status": 200, "body": {"data": {"is_pin_setup": True}}}

    def pin_create_challenge(self, *, flow):
        assert flow == "UPDATE_PIN"
        self.auth.pin_challenge_id = "otp-login-change-pin-challenge"
        return {"status": 200, "body": {}}

    def pin_verify(self, pin):
        assert pin == "147258"
        self.auth.pin_token = "otp-login-change-pin-token"
        return {"status": 200, "body": {}}

    def pin_update_v3(self, new_pin):
        assert new_pin == "369258"
        return {"status": 200, "body": {}}

    def get_balance(self):
        return {"status": 200, "body": {"data": [{"balance": {"value": 34500}}]}}


class FakeOtpLoginAdapter:
    def new_gojek_client(self, phone: str, **_kwargs):
        return FakeOtpLoginClient(phone)


class FakeNoPinLoginClient(FakeOtpLoginClient):
    def __init__(self, phone: str, adapter: FakeNoPinLoginAdapter) -> None:
        super().__init__(phone)
        self.adapter = adapter

    def get_user_profile(self):
        self.adapter.profile_reads += 1
        return {
            "status": 200,
            "body": {"data": {"is_pin_setup": self.adapter.pin_configured}},
        }

    def pin_check_allowed(self, pin):
        assert pin == "369258"
        return {"status": 200, "body": {}}

    def pin_request_otp(self):
        self.auth.otp_token = "pin-setup-otp-token"
        return {"status": 200, "body": {}}

    def pin_verify_otp(self, otp):
        assert otp == "246810"
        self.auth.verification_token = "pin-setup-verified"
        return {"status": 200, "body": {}}

    def pin_setup(self, pin):
        assert pin == "369258"
        if self.adapter.confirm_setup:
            self.adapter.pin_configured = True
        return {"status": 201, "body": {}}


class FakeNoPinLoginAdapter:
    def __init__(self, *, confirm_setup: bool = True) -> None:
        self.confirm_setup = confirm_setup
        self.pin_configured = False
        self.profile_reads = 0

    def new_gojek_client(self, phone: str, **_kwargs):
        return FakeNoPinLoginClient(phone, self)


class PrepareContext:
    def __init__(self) -> None:
        self.saved: dict = {}

    def progress(self, _value, _message):
        return None

    def save_checkpoint(self, checkpoint):
        self.saved = checkpoint


class OtpContext:
    task_id = "task-otp"

    def __init__(self) -> None:
        self.repository = SimpleNamespace(get_task=lambda _task_id: SimpleNamespace(progress=0.2))
        self.messages: list[str] = []
        self.wait_timeout = 0
        self.wait_message = ""

    def ensure_active(self) -> None:
        return None

    def consume_input(self, _input_type: str) -> None:
        return None

    def progress(self, _value: float, message: str) -> None:
        self.messages.append(message)

    def wait_for_input(self, _input_type: str, *, timeout_seconds: int, checkpoint, message: str):
        self.wait_timeout = timeout_seconds
        self.wait_message = message
        return "112233"


def _wait(repository: TaskRepository, task_id: str, status: str, *, attempt: int = 0) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        task = repository.get_task(task_id)
        if task.status == status and task.attempt >= attempt:
            return
        time.sleep(0.02)
    raise AssertionError(f"等待任务状态超时：{status}")


def test_rate_limit_is_classified_without_exposing_remote_english_message():
    try:
        _expect(
            {
                "status": 429,
                "body": {"errors": [{"code": "auth:error:ratelimited"}]},
            },
            "查询登录方式",
        )
    except PermanentTaskError as exc:
        assert exc.code == "gopay_rate_limited"
        assert "当前代理出口请求过多" in str(exc)
        assert "auth:error:ratelimited" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("HTTP 429 应当结束当前任务")


def test_rate_limit_error_code_is_recognized_even_when_http_status_is_400():
    try:
        _expect(
            {
                "status": 400,
                "body": {
                    "errors": [
                        {"code": "scp-cvs:error:ratelimit:init_verification"}
                    ]
                },
            },
            "注册 OTP 申请",
        )
    except PermanentTaskError as exc:
        assert exc.code == "gopay_rate_limited"
        assert "当前代理出口请求过多" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("GoPay 限频错误码应当触发代理切换")


def test_frs_rejection_is_reported_in_chinese_without_unsafe_retry():
    try:
        _expect(
            {
                "status": 400,
                "body": {
                    "errors": [
                        {
                            "code": "CO:CUST:frs_failure_generic",
                            "message": "Worry not, we're working on it.",
                        }
                    ]
                },
            },
            "创建 GoPay 账号",
            side_effect=True,
        )
    except PermanentTaskError as exc:
        assert exc.code == "gopay_frs_rejected"
        assert "GoPay 风控服务拒绝创建账号" in str(exc)
        assert "不会自动重复提交" in str(exc)
        assert "Worry not" not in str(exc)
    else:  # pragma: no cover
        raise AssertionError("FRS 拒绝应当结束当前创建请求")


def test_register_rate_limit_retries_only_otp_with_same_device_session(database):
    _engine, session_factory, codec = database
    first_proxy = "http://user-region-ID-sid-first:secret@proxy.example:3010"
    second_proxy = "http://user-region-ID-sid-second:secret@proxy.example:3010"
    sms_store = SmsSettingsStore(session_factory, codec)
    sms_store.save(
        api_key="sms-test-key",
        base_url="https://sms.example.test",
        service="ni",
        country="6",
    )
    AccountFlowDefaultsStore(session_factory, codec).save(
        register_pin=None,
        login_pin=None,
        new_pin=None,
        task_count=1,
        concurrency=1,
        sms_otp_timeout_seconds=60,
        manual_otp_timeout_seconds=300,
        change_pin_enabled=True,
        default_proxy_region="ID",
        proxy_pool=f"{first_proxy}\n{second_proxy}",
    )

    class RateLimitedClient(FakeRegisterClient):
        def signup_request_otp(self, _phone, _country_code):
            return {
                "status": 429,
                "body": {
                    "errors": [
                        {"code": "scp-cvs:error:ratelimit:init_verification"}
                    ]
                },
            }

    class RotatingAdapter:
        def __init__(self) -> None:
            self.rent_count = 0
            self.client_proxies: list[str] = []

        def configure_sms(self, **_values) -> None:
            return None

        def probe_proxy(self, proxy):
            ip = "203.0.113.20" if proxy == first_proxy else "203.0.113.21"
            return {"ok": True, "status": 200, "ip": ip}

        def sms_get_number(self, _api_key):
            self.rent_count += 1
            return "+628777770010", "activation-proxy-switch"

        def sms_wait_code(self, _api_key, _activation_id, **_kwargs):
            return None

        def new_gojek_client(self, phone, *, proxy):
            self.client_proxies.append(proxy)
            client_number = len(self.client_proxies)
            client = RateLimitedClient(phone) if proxy == first_proxy else FakeRegisterClient(phone)
            client.uniqueid = f"generated-device-{client_number}"
            return client

    adapter = RotatingAdapter()
    repository = TaskRepository(session_factory, codec, lease_seconds=10, retry_base_seconds=0.01)
    handler = AccountFlowHandler("register", session_factory, codec, adapter, sms_store)
    registry = HandlerRegistry()
    registry.register("account.register", handler, safe_to_retry=False)
    task, _created = repository.create_task(
        "account.register",
        {
            "phone_source": "smsbower",
            "phone": "",
            "pin": "147258",
            "country_code": "+62",
            "proxy": first_proxy,
            "proxy_region": "ID",
        },
        max_attempts=1,
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
    try:
        _wait(repository, task.id, "waiting_input", attempt=1)
        checkpoint = repository.get_execution(task.id).checkpoint
        assert checkpoint["proxy"] == second_proxy
        assert checkpoint["proxy_switch_count"] == 1
        assert checkpoint["signup_otp_request_count"] == 2
        assert checkpoint["client"]["uniqueid"] == "generated-device-1"
        assert adapter.rent_count == 1
        assert adapter.client_proxies == [first_proxy, second_proxy]
        messages = [event["message"] for event in repository.list_events(task.id, limit=200)]
        assert messages.count(
            "Support SDK 启动步骤已处理，开始检测手机号是否已有 GoPay 账号"
        ) == 1
        assert sum("保留原设备会话并直接重试注册 OTP" in message for message in messages) == 1
        assert all("重新执行安全的注册前置流程" not in message for message in messages)
    finally:
        pool.stop()


def test_register_skips_proxy_with_same_rate_limited_egress_without_sending_otp(database):
    _engine, session_factory, codec = database
    proxies = [
        f"http://user-region-ID-sid-{name}:secret@proxy.example:3010"
        for name in ("first", "same-egress", "fresh-egress")
    ]
    sms_store = SmsSettingsStore(session_factory, codec)
    sms_store.save(
        api_key="sms-test-key",
        base_url="https://sms.example.test",
        service="ni",
        country="6",
    )
    AccountFlowDefaultsStore(session_factory, codec).save(
        register_pin=None,
        login_pin=None,
        new_pin=None,
        task_count=1,
        concurrency=1,
        sms_otp_timeout_seconds=60,
        manual_otp_timeout_seconds=60,
        change_pin_enabled=True,
        default_proxy_region="ID",
        proxy_pool="\n".join(proxies),
    )

    class SharedClient(FakeRegisterClient):
        def __init__(self, phone: str, adapter) -> None:
            super().__init__(phone)
            self.adapter = adapter

        def support_customer_initiate(self):
            self.adapter.support_initiate_calls += 1
            return {"status": 200, "body": {}}

        def support_customer_actions(self):
            self.adapter.support_action_calls += 1
            return {"status": 200, "body": {}}

        def get_login_methods(self, country_code, phone):
            self.adapter.login_method_calls += 1
            return super().get_login_methods(country_code, phone)

        def signup_request_otp(self, phone, country_code):
            self.adapter.otp_request_calls += 1
            if self.adapter.otp_request_calls == 1:
                return {
                    "status": 429,
                    "body": {
                        "errors": [
                            {"code": "scp-cvs:error:ratelimit:init_verification"}
                        ]
                    },
                }
            return super().signup_request_otp(phone, country_code)

    class SameEgressAdapter:
        def __init__(self) -> None:
            self.rent_count = 0
            self.support_initiate_calls = 0
            self.support_action_calls = 0
            self.login_method_calls = 0
            self.otp_request_calls = 0
            self.client_proxies: list[str] = []

        def configure_sms(self, **_values) -> None:
            return None

        def probe_proxy(self, proxy):
            ip = "198.51.100.10" if proxy in proxies[:2] else "198.51.100.11"
            return {"ok": True, "status": 200, "ip": ip}

        def sms_get_number(self, _api_key):
            self.rent_count += 1
            return "+628777770011", "activation-same-egress"

        def sms_wait_code(self, _api_key, _activation_id, **_kwargs):
            return None

        def new_gojek_client(self, phone, *, proxy):
            self.client_proxies.append(proxy)
            client = SharedClient(phone, self)
            client.uniqueid = f"generated-device-{len(self.client_proxies)}"
            return client

    adapter = SameEgressAdapter()
    repository = TaskRepository(session_factory, codec, lease_seconds=10, retry_base_seconds=0.01)
    registry = HandlerRegistry()
    registry.register(
        "account.register",
        AccountFlowHandler("register", session_factory, codec, adapter, sms_store),
        safe_to_retry=False,
    )
    task, _created = repository.create_task(
        "account.register",
        {
            "phone_source": "smsbower",
            "phone": "",
            "pin": "147258",
            "country_code": "+62",
            "proxy": proxies[0],
            "proxy_region": "ID",
        },
        max_attempts=1,
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
    try:
        _wait(repository, task.id, "waiting_input", attempt=1)
        checkpoint = repository.get_execution(task.id).checkpoint
        assert checkpoint["proxy"] == proxies[2]
        assert checkpoint["proxy_switch_count"] == 2
        assert checkpoint["signup_otp_request_count"] == 2
        assert checkpoint["client"]["uniqueid"] == "generated-device-1"
        assert adapter.rent_count == 1
        assert adapter.support_initiate_calls == 1
        assert adapter.support_action_calls == 1
        assert adapter.login_method_calls == 1
        assert adapter.otp_request_calls == 2
        assert adapter.client_proxies == proxies
        messages = [event["message"] for event in repository.list_events(task.id, limit=200)]
        assert any("跳过该代理且不发送注册 OTP" in message for message in messages)
    finally:
        pool.stop()


def test_register_stops_after_proxy_pool_is_exhausted(database):
    _engine, session_factory, codec = database
    proxies = [
        f"http://user-region-ID-sid-{name}:secret@proxy.example:3010"
        for name in ("first", "second")
    ]
    sms_store = SmsSettingsStore(session_factory, codec)
    sms_store.save(
        api_key="sms-test-key",
        base_url="https://sms.example.test",
        service="ni",
        country="6",
    )
    AccountFlowDefaultsStore(session_factory, codec).save(
        register_pin=None,
        login_pin=None,
        new_pin=None,
        task_count=1,
        concurrency=1,
        sms_otp_timeout_seconds=60,
        manual_otp_timeout_seconds=60,
        change_pin_enabled=True,
        default_proxy_region="ID",
        proxy_pool="\n".join(proxies),
    )

    class AlwaysLimitedClient(FakeRegisterClient):
        def __init__(self, phone: str, adapter) -> None:
            super().__init__(phone)
            self.adapter = adapter

        def get_login_methods(self, country_code, phone):
            self.adapter.login_method_calls += 1
            return super().get_login_methods(country_code, phone)

        def signup_request_otp(self, _phone, _country_code):
            self.adapter.otp_request_calls += 1
            return {
                "status": 429,
                "body": {
                    "errors": [
                        {"code": "scp-cvs:error:ratelimit:init_verification"}
                    ]
                },
            }

    class ExhaustedAdapter:
        def __init__(self) -> None:
            self.rent_count = 0
            self.login_method_calls = 0
            self.otp_request_calls = 0

        def configure_sms(self, **_values) -> None:
            return None

        def probe_proxy(self, proxy):
            return {
                "ok": True,
                "status": 200,
                "ip": "198.51.100.20" if proxy == proxies[0] else "198.51.100.21",
            }

        def sms_get_number(self, _api_key):
            self.rent_count += 1
            return "+628777770012", "activation-exhausted"

        def sms_cancel(self, _api_key, _activation_id):
            return True

        def new_gojek_client(self, phone, *, proxy):
            return AlwaysLimitedClient(phone, self)

    adapter = ExhaustedAdapter()
    repository = TaskRepository(session_factory, codec, lease_seconds=10, retry_base_seconds=0.01)
    registry = HandlerRegistry()
    registry.register(
        "account.register",
        AccountFlowHandler("register", session_factory, codec, adapter, sms_store),
        safe_to_retry=False,
    )
    task, _created = repository.create_task(
        "account.register",
        {
            "phone_source": "smsbower",
            "phone": "",
            "pin": "147258",
            "country_code": "+62",
            "proxy": proxies[0],
            "proxy_region": "ID",
        },
        max_attempts=1,
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
    try:
        _wait(repository, task.id, "failed", attempt=1)
        snapshot = repository.get_task(task.id)
        assert snapshot.last_error_code == "gopay_rate_limit_exhausted"
        assert "当前区域代理池已无新的可用出口" in snapshot.last_error_message
        assert "不再重复请求" in snapshot.last_error_message
        assert adapter.rent_count == 1
        assert adapter.login_method_calls == 1
        assert adapter.otp_request_calls == 2
    finally:
        pool.stop()


def test_same_proxy_is_shareable_between_account_tasks(database):
    _engine, session_factory, codec = database
    handler = AccountFlowHandler(
        "register",
        session_factory,
        codec,
        FakeAdapter(),
        SmsSettingsStore(session_factory, codec),
    )

    class LeaseContext:
        def __init__(self) -> None:
            self.resources: list[tuple[str, str]] = []

        def acquire_resource(self, resource_type, resource_key, **_kwargs):
            self.resources.append((resource_type, resource_key))

    first = LeaseContext()
    second = LeaseContext()
    handler._acquire_resources(
        first,
        {"phone_normalized": "628111111111", "proxy": "http://proxy.example:3010"},
    )
    handler._acquire_resources(
        second,
        {"phone_normalized": "628222222222", "proxy": "http://proxy.example:3010"},
    )

    assert first.resources == [("phone", "628111111111")]
    assert second.resources == [("phone", "628222222222")]


def test_sms_otp_uses_one_short_polling_round(database):
    _engine, session_factory, codec = database
    sms_store = SmsSettingsStore(session_factory, codec)
    sms_store.save(
        api_key="sms-test-key",
        base_url="https://sms.example.test",
        service="ni",
        country="6",
    )
    AccountFlowDefaultsStore(session_factory, codec).save(
        register_pin=None,
        login_pin=None,
        new_pin=None,
        task_count=1,
        concurrency=2,
        sms_otp_timeout_seconds=60,
        manual_otp_timeout_seconds=480,
        change_pin_enabled=True,
        default_proxy_region="",
        proxy_pool=None,
    )

    class SmsAdapter:
        def __init__(self) -> None:
            self.timeouts: list[int] = []

        def configure_sms(self, **_values) -> None:
            return None

        def sms_wait_code(self, _api_key, _activation_id, *, timeout, ignore_code_hashes):
            assert ignore_code_hashes == set()
            self.timeouts.append(timeout)
            return "246810"

        def sms_request_another(self, _api_key, _activation_id):
            return True

    adapter = SmsAdapter()
    handler = AccountFlowHandler("register", session_factory, codec, adapter, sms_store)
    context = OtpContext()
    checkpoint = {
        "activation_id": "activation-three-rounds",
        "sms_provider": "smsbower",
        "consumed_code_hashes": [],
    }

    assert handler._obtain_otp(context, checkpoint, "注册") == "246810"
    assert adapter.timeouts == [60]
    assert any("最多等待 60 秒" in message for message in context.messages)
    assert all("轮" not in message for message in context.messages)


def test_sms_otp_falls_back_to_configured_manual_timeout(database):
    _engine, session_factory, codec = database
    sms_store = SmsSettingsStore(session_factory, codec)
    sms_store.save(
        api_key="sms-test-key",
        base_url="https://sms.example.test",
        service="ni",
        country="6",
    )

    class SmsAdapter:
        def __init__(self) -> None:
            self.calls = 0
            self.timeouts: list[int] = []

        def configure_sms(self, **_values) -> None:
            return None

        def sms_wait_code(self, _api_key, _activation_id, **kwargs):
            self.calls += 1
            self.timeouts.append(kwargs["timeout"])
            return None

    adapter = SmsAdapter()
    handler = AccountFlowHandler("register", session_factory, codec, adapter, sms_store)
    context = OtpContext()
    checkpoint = {
        "activation_id": "activation-manual-fallback",
        "sms_provider": "smsbower",
        "consumed_code_hashes": [],
    }

    assert handler._obtain_otp(context, checkpoint, "注册") == "112233"
    assert adapter.calls == 1
    assert adapter.timeouts == [60]
    assert context.wait_timeout == 300
    assert "自动获取结束" in context.wait_message
    assert "最多等待 300 秒" in context.wait_message


def test_login_with_smsbower_probes_proxy_then_rents_new_number(database):
    _engine, session_factory, codec = database
    sms_store = SmsSettingsStore(session_factory, codec)
    sms_store.save(
        api_key="sms-test-key",
        base_url="https://sms.example.test",
        service="ni",
        country="6",
    )

    class AutoLoginAdapter:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.probe_attempts = 0

        def configure_sms(self, **_values) -> None:
            self.calls.append("configure")

        def probe_proxy(self, proxy):
            assert proxy == "http://proxy.example:3010"
            self.calls.append("probe")
            self.probe_attempts += 1
            if self.probe_attempts == 1:
                return {"ok": False, "status": 502, "ip": ""}
            return {"ok": True, "ip": "203.0.113.20"}

        def sms_get_number(self, api_key):
            assert api_key == "sms-test-key"
            self.calls.append("rent")
            return "+628777777777", "activation-new-login"

    adapter = AutoLoginAdapter()

    handler = AccountFlowHandler(
        "login",
        session_factory,
        codec,
        adapter,
        sms_store,
    )
    context = PrepareContext()
    checkpoint = handler._prepare(
        context,
        {
            "phone_source": "smsbower",
            "phone": "",
            "pin": "147258",
            "country_code": "+62",
            "proxy": "http://proxy.example:3010",
        },
        {},
    )

    handler._preflight(context, checkpoint)

    assert adapter.calls == ["probe", "probe", "configure", "rent"]
    assert checkpoint["phone"] == "+628777777777"
    assert checkpoint["activation_id"] == "activation-new-login"
    assert checkpoint["proxy_preflight"] == {"ok": True, "ip": "203.0.113.20"}
    assert checkpoint["local_account_id"] == ""
    assert checkpoint["consumed_code_hashes"] == []
    with session_factory() as session:
        activation = session.scalar(
            select(SmsActivation).where(
                SmsActivation.provider == "smsbower",
                SmsActivation.provider_activation_id == "activation-new-login",
            )
        )
        assert activation is not None
        assert activation.account_id is None
        assert activation.status == "rented"


def test_register_with_hero_sms_uses_isolated_provider_client(database):
    _engine, session_factory, codec = database
    hero_store = HeroSmsSettingsStore(session_factory, codec)
    hero_store.save(
        api_key="hero-test-key",
        base_url="https://hero-sms.example/stubs/handler_api.php",
        service="ni",
        country="6",
    )

    class HeroAdapter:
        def __init__(self) -> None:
            self.cancelled = False

        def sms_get_number_for(
            self,
            api_key,
            *,
            base_url,
            service,
            country,
        ):
            assert api_key == "hero-test-key"
            assert base_url == "https://hero-sms.example/stubs/handler_api.php"
            assert service == "ni"
            assert country == "6"
            return "+628888888888", "hero-activation-1"

        def sms_cancel_for(
            self,
            api_key,
            activation_id,
            *,
            base_url,
            service,
            country,
        ):
            assert api_key == "hero-test-key"
            assert activation_id == "hero-activation-1"
            assert base_url == "https://hero-sms.example/stubs/handler_api.php"
            assert service == "ni"
            assert country == "6"
            self.cancelled = True
            return True

    adapter = HeroAdapter()
    handler = AccountFlowHandler(
        "register",
        session_factory,
        codec,
        adapter,
        SmsSettingsStore(session_factory, codec),
        hero_store,
    )
    checkpoint = handler._prepare(
        PrepareContext(),
        {
            "phone_source": "hero_sms",
            "phone": "",
            "pin": "147258",
            "country_code": "+62",
            "proxy": "",
        },
        {},
    )

    assert checkpoint["phone"] == "+628888888888"
    assert checkpoint["sms_provider"] == "hero_sms"
    assert checkpoint["activation_id"] == "hero-activation-1"
    with session_factory() as session:
        activation = session.scalar(
            select(SmsActivation).where(
                SmsActivation.provider == "hero_sms",
                SmsActivation.provider_activation_id == "hero-activation-1",
            )
        )
        assert activation is not None
        assert activation.status == "rented"

    handler._cancel_unused_activation(checkpoint)
    assert adapter.cancelled is True
    with session_factory() as session:
        activation = session.scalar(
            select(SmsActivation).where(
                SmsActivation.provider == "hero_sms",
                SmsActivation.provider_activation_id == "hero-activation-1",
            )
        )
        assert activation is not None
        assert activation.status == "cancelled"


def test_unused_sms_activation_uses_persistent_delayed_release(database):
    _engine, session_factory, codec = database
    sms_store = SmsSettingsStore(session_factory, codec)
    sms_store.save(
        api_key="sms-test-key",
        base_url="https://sms.example.test",
        service="ni",
        country="6",
    )

    class SmsAdapter:
        def configure_sms(self, **_values) -> None:
            return None

        def sms_cancel(self, _api_key, activation_id):
            assert activation_id == "activation-delayed-release"
            return False

    class Repository:
        def __init__(self) -> None:
            self.created: list[tuple[str, dict, dict]] = []

        def create_task(self, task_type, payload, **kwargs):
            self.created.append((task_type, payload, kwargs))
            return SimpleNamespace(), True

    handler = AccountFlowHandler("login", session_factory, codec, SmsAdapter(), sms_store)
    phone_id = handler._upsert_phone("+628777770001", "628777770001", "smsbower")
    handler._upsert_activation("smsbower", "activation-delayed-release", phone_id)
    repository = Repository()
    context = SimpleNamespace(repository=repository)
    status = handler._cancel_unused_activation(
        {
            "phase": "prepared",
            "phone_source": "smsbower",
            "sms_provider": "smsbower",
            "activation_id": "activation-delayed-release",
        },
        context,
    )

    assert status == "release_pending"
    assert len(repository.created) == 1
    task_type, payload, options = repository.created[0]
    assert task_type == "sms.cancel_activation"
    assert payload == {
        "provider": "smsbower",
        "activation_id": "activation-delayed-release",
    }
    assert options["max_attempts"] == 3
    assert options["idempotency_key"] == "sms-cancel:smsbower:activation-delayed-release"
    assert options["run_after"] > utc_now()
    with session_factory() as session:
        activation = session.scalar(
            select(SmsActivation).where(
                SmsActivation.provider_activation_id == "activation-delayed-release"
            )
        )
        assert activation is not None
        assert activation.status == "release_pending"


def test_register_flow_pauses_twice_and_persists_account(database):
    _engine, session_factory, codec = database
    repository = TaskRepository(session_factory, codec, lease_seconds=10, retry_base_seconds=0.01)
    handler = AccountFlowHandler(
        "register",
        session_factory,
        codec,
        FakeAdapter(),
        SmsSettingsStore(session_factory, codec),
    )
    registry = HandlerRegistry()
    registry.register("account.register", handler, safe_to_retry=False)
    task, _created = repository.create_task(
        "account.register",
        {
            "phone_source": "manual",
            "phone": "+628123456789",
            "pin": "147258",
            "country_code": "+62",
            "proxy": "",
        },
        max_attempts=8,
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
    try:
        _wait(repository, task.id, "waiting_input", attempt=1)
        first = repository.get_execution(task.id).checkpoint
        assert first["phase"] == "signup_otp_sent"
        assert first["otp_purpose"] == "注册"
        repository.submit_input(task.id, "otp", "123456")

        _wait(repository, task.id, "waiting_input", attempt=2)
        second = repository.get_execution(task.id).checkpoint
        assert second["phase"] == "pin_otp_sent"
        assert second["otp_purpose"] == "PIN 设置"
        repository.submit_input(task.id, "otp", "654321")

        _wait(repository, task.id, "succeeded", attempt=3)
        result = repository.consume_result(task.id)
        assert result["core_registration_succeeded"] is True
        assert result["post_registration_task_id"]
        _post_tasks, post_total = repository.list_tasks(
            task_type="account.post_register",
            limit=20,
        )
        assert post_total == 1
        with session_factory() as session:
            account = session.scalar(select(Account).where(Account.phone_normalized == "628123456789"))
            assert account is not None
            assert account.balance == 12500
            assert account.pin_setup_status == "configured"
            secret_row = session.get(AccountSecret, account.id)
            secret = json.loads(
                codec.decrypt(secret_row.secret_payload_ciphertext, context=f"account:{account.id}")
            )
            assert secret["pin"] == "147258"
            assert secret["access_token"] == "access-refreshed"
    finally:
        pool.stop()


def test_register_206_relogs_in_for_token_then_finishes_pin_setup(database):
    _engine, session_factory, codec = database
    repository = TaskRepository(session_factory, codec, lease_seconds=10, retry_base_seconds=0.01)
    handler = AccountFlowHandler(
        "register",
        session_factory,
        codec,
        FakeRegister206Adapter(),
        SmsSettingsStore(session_factory, codec),
    )
    registry = HandlerRegistry()
    registry.register("account.register", handler, safe_to_retry=False)
    task, _created = repository.create_task(
        "account.register",
        {
            "phone_source": "manual",
            "phone": "+628123450206",
            "pin": "147258",
            "country_code": "+62",
            "proxy": "",
        },
        max_attempts=1,
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
    try:
        _wait(repository, task.id, "waiting_input", attempt=1)
        first = repository.get_execution(task.id).checkpoint
        assert first["phase"] == "signup_otp_sent"
        repository.submit_input(task.id, "otp", "123456")

        _wait(repository, task.id, "waiting_input", attempt=2)
        second = repository.get_execution(task.id).checkpoint
        assert second["phase"] == "signup_relogin_1fa_otp_sent"
        assert second["signup_missing_token"] is True
        repository.submit_input(task.id, "otp", "246810")

        _wait(repository, task.id, "waiting_input", attempt=3)
        third = repository.get_execution(task.id).checkpoint
        assert third["phase"] == "pin_otp_sent"
        assert third["signup_missing_token"] is False
        repository.submit_input(task.id, "otp", "654321")

        _wait(repository, task.id, "succeeded", attempt=4)
        with session_factory() as session:
            account = session.scalar(select(Account).where(Account.phone_normalized == "628123450206"))
            assert account is not None
            assert account.customer_id == "customer-206"
            assert account.remote_account_id == "remote-account-206"
            assert account.pin_setup_status == "configured"
            secret_row = session.get(AccountSecret, account.id)
            secret = json.loads(
                codec.decrypt(secret_row.secret_payload_ciphertext, context=f"account:{account.id}")
            )
            assert secret["pin"] == "147258"
            assert secret["access_token"] == "access-206"
            assert secret["refresh_token"] == "refresh-206"
    finally:
        pool.stop()


def test_register_stops_when_old_flow_detects_existing_account(database):
    _engine, session_factory, codec = database

    class ExistingClient(FakeRegisterClient):
        def get_login_methods(self, _country_code, _phone):
            return {"status": 201, "body": {"success": True}}

    class ExistingAdapter:
        def new_gojek_client(self, phone: str, **_kwargs):
            return ExistingClient(phone)

    repository = TaskRepository(session_factory, codec, lease_seconds=10, retry_base_seconds=0.01)
    registry = HandlerRegistry()
    registry.register(
        "account.register",
        AccountFlowHandler(
            "register",
            session_factory,
            codec,
            ExistingAdapter(),
            SmsSettingsStore(session_factory, codec),
        ),
        safe_to_retry=False,
    )
    task, _created = repository.create_task(
        "account.register",
        {
            "phone_source": "manual",
            "phone": "+628123451201",
            "pin": "147258",
            "country_code": "+62",
            "proxy": "",
        },
        max_attempts=1,
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
    try:
        _wait(repository, task.id, "failed", attempt=1)
        snapshot = repository.get_task(task.id)
        assert snapshot.last_error_code == "already_registered"
        assert snapshot.last_error_message == "号码已注册，不能作为新号注册"
        messages = [event["message"] for event in repository.list_events(task.id, limit=100)]
        assert "手机号账号检测返回 HTTP 201，继续判断注册分支" in messages
    finally:
        pool.stop()


def test_register_preserves_review_state_when_create_account_times_out_after_otp(database):
    _engine, session_factory, codec = database

    class TimeoutClient(FakeRegisterClient):
        def signup_create_account(self, **_kwargs):
            raise TimeoutError("测试代理超时")

    class TimeoutAdapter:
        def new_gojek_client(self, phone: str, **_kwargs):
            return TimeoutClient(phone)

    repository = TaskRepository(session_factory, codec, lease_seconds=10, retry_base_seconds=0.01)
    registry = HandlerRegistry()
    registry.register(
        "account.register",
        AccountFlowHandler(
            "register",
            session_factory,
            codec,
            TimeoutAdapter(),
            SmsSettingsStore(session_factory, codec),
        ),
        safe_to_retry=False,
    )
    task, _created = repository.create_task(
        "account.register",
        {
            "phone_source": "manual",
            "phone": "+628123451202",
            "pin": "147258",
            "country_code": "+62",
            "proxy": "",
        },
        max_attempts=1,
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
    try:
        _wait(repository, task.id, "waiting_input", attempt=1)
        repository.submit_input(task.id, "otp", "123456")
        _wait(repository, task.id, "needs_review", attempt=2)
        snapshot = repository.get_task(task.id)
        assert snapshot.last_error_code == "account_create_result_unknown"
        assert "本次不会自动重复提交创建请求" in snapshot.last_error_message
        with session_factory() as session:
            assert session.scalar(
                select(Account).where(Account.phone_normalized == "628123451202")
            ) is None
    finally:
        pool.stop()


def test_register_frs_rejection_never_enters_post_registration_flow(database):
    _engine, session_factory, codec = database

    class FrsClient(FakeRegisterClient):
        def signup_create_account(self, **_kwargs):
            return {
                "status": 400,
                "body": {
                    "errors": [
                        {
                            "code": "CO:CUST:frs_failure_generic",
                            "message": "远端测试消息",
                        }
                    ]
                },
            }

    class FrsAdapter:
        def new_gojek_client(self, phone: str, **_kwargs):
            return FrsClient(phone)

    repository = TaskRepository(session_factory, codec, lease_seconds=10, retry_base_seconds=0.01)
    registry = HandlerRegistry()
    registry.register(
        "account.register",
        AccountFlowHandler(
            "register",
            session_factory,
            codec,
            FrsAdapter(),
            SmsSettingsStore(session_factory, codec),
        ),
        safe_to_retry=False,
    )
    task, _created = repository.create_task(
        "account.register",
        {
            "phone_source": "manual",
            "phone": "+628123451203",
            "pin": "147258",
            "country_code": "+62",
            "proxy": "",
        },
        max_attempts=1,
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
    try:
        _wait(repository, task.id, "waiting_input", attempt=1)
        repository.submit_input(task.id, "otp", "123456")
        _wait(repository, task.id, "failed", attempt=2)
        snapshot = repository.get_task(task.id)
        assert snapshot.last_error_code == "gopay_frs_rejected"
        assert "GoPay 风控服务拒绝创建账号" in snapshot.last_error_message
        _items, total = repository.list_tasks(task_type="account.post_register", limit=20)
        assert total == 0
    finally:
        pool.stop()


def test_login_flow_restores_twofa_and_persists_changed_pin(database):
    _engine, session_factory, codec = database
    repository = TaskRepository(session_factory, codec, lease_seconds=10, retry_base_seconds=0.01)
    handler = AccountFlowHandler(
        "login",
        session_factory,
        codec,
        FakeLoginAdapter(),
        SmsSettingsStore(session_factory, codec),
    )
    registry = HandlerRegistry()
    registry.register("account.login", handler, safe_to_retry=False)
    task, _created = repository.create_task(
        "account.login",
        {
            "phone_source": "manual",
            "phone": "+628987654321",
            "pin": "147258",
            "change_pin": True,
            "new_pin": "369258",
            "country_code": "+62",
            "proxy": "",
        },
        max_attempts=8,
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
    try:
        _wait(repository, task.id, "waiting_input", attempt=1)
        checkpoint = repository.get_execution(task.id).checkpoint
        assert checkpoint["phase"] == "login_2fa_otp_sent"
        assert checkpoint["otp_purpose"] == "登录二阶段"
        assert checkpoint["client"]["auth"]["twofa_token"] == "login-twofa-token"
        repository.submit_input(task.id, "otp", "246810")

        _wait(repository, task.id, "succeeded", attempt=2)
        with session_factory() as session:
            account = session.scalar(select(Account).where(Account.phone_normalized == "628987654321"))
            assert account is not None
            assert account.balance == 23600
            assert account.pin_setup_status == "configured"
            assert account.pin_change_status == "changed_unconfirmed"
            secret_row = session.get(AccountSecret, account.id)
            secret = json.loads(
                codec.decrypt(secret_row.secret_payload_ciphertext, context=f"account:{account.id}")
            )
            assert secret["pin"] == "369258"
            assert secret["access_token"] == "login-access-token"
            assert secret["refresh_token"] == "login-refresh-token"
    finally:
        pool.stop()


def test_otp_login_without_pin_sets_new_pin_and_confirms_profile(database):
    _engine, session_factory, codec = database
    repository = TaskRepository(session_factory, codec, lease_seconds=10, retry_base_seconds=0.01)
    adapter = FakeNoPinLoginAdapter()
    handler = AccountFlowHandler(
        "login",
        session_factory,
        codec,
        adapter,
        SmsSettingsStore(session_factory, codec),
    )
    registry = HandlerRegistry()
    registry.register("account.login", handler, safe_to_retry=False)
    task, _created = repository.create_task(
        "account.login",
        {
            "phone_source": "manual",
            "phone": "+628555666779",
            "pin": "147258",
            "change_pin": True,
            "new_pin": "369258",
            "country_code": "+62",
            "proxy": "",
        },
        max_attempts=8,
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
    try:
        _wait(repository, task.id, "waiting_input", attempt=1)
        first = repository.get_execution(task.id).checkpoint
        assert first["phase"] == "login_1fa_otp_sent"
        repository.submit_input(task.id, "otp", "135790")

        _wait(repository, task.id, "waiting_input", attempt=2)
        second = repository.get_execution(task.id).checkpoint
        assert second["phase"] == "pin_otp_sent"
        assert second["setup_pin"] == "369258"
        repository.submit_input(task.id, "otp", "246810")

        _wait(repository, task.id, "succeeded", attempt=3)
        assert adapter.pin_configured is True
        assert adapter.profile_reads >= 2
        with session_factory() as session:
            account = session.scalar(
                select(Account).where(Account.phone_normalized == "628555666779")
            )
            assert account is not None
            assert account.pin_setup_status == "configured"
            secret_row = session.get(AccountSecret, account.id)
            secret = json.loads(
                codec.decrypt(
                    secret_row.secret_payload_ciphertext,
                    context=f"account:{account.id}",
                )
            )
            assert secret["pin"] == "369258"

        messages = [event["message"] for event in repository.list_events(task.id, limit=200)]
        expected = [
            "开始已有账号登录：自动检测 PIN 状态",
            "登录步骤 1/10：查询账号登录方式",
            "登录步骤 2/10：账号未要求 PIN，发送一阶段 OTP",
            "登录 OTP 已发送，等待新的验证码",
            "登录 OTP 已提交，开始验证",
            "登录步骤 5/10：读取账号列表",
            "登录步骤 6/10：申请登录 token",
            "已检测到账号没有 PIN，登录成功后开始设置新 PIN",
            "PIN OTP 已发送，等待新的验证码",
            "PIN OTP 已提交，开始验证",
            "PIN 设置完成，并已重新检测确认",
            "已有账号登录成功，PIN 状态已确认，保存账号",
        ]
        positions = [messages.index(message) for message in expected]
        assert positions == sorted(positions)
    finally:
        pool.stop()


def test_otp_login_preserves_account_when_pin_setup_is_unconfirmed(database):
    _engine, session_factory, codec = database
    repository = TaskRepository(session_factory, codec, lease_seconds=10, retry_base_seconds=0.01)
    adapter = FakeNoPinLoginAdapter(confirm_setup=False)
    handler = AccountFlowHandler(
        "login",
        session_factory,
        codec,
        adapter,
        SmsSettingsStore(session_factory, codec),
    )
    registry = HandlerRegistry()
    registry.register("account.login", handler, safe_to_retry=False)
    task, _created = repository.create_task(
        "account.login",
        {
            "phone_source": "manual",
            "phone": "+628555666780",
            "pin": "147258",
            "change_pin": True,
            "new_pin": "369258",
            "country_code": "+62",
            "proxy": "",
        },
        max_attempts=8,
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
    try:
        _wait(repository, task.id, "waiting_input", attempt=1)
        repository.submit_input(task.id, "otp", "135790")
        _wait(repository, task.id, "waiting_input", attempt=2)
        repository.submit_input(task.id, "otp", "246810")
        _wait(repository, task.id, "needs_review", attempt=3)

        assert adapter.pin_configured is False
        assert adapter.profile_reads == 4
        with session_factory() as session:
            account = session.scalar(
                select(Account).where(Account.phone_normalized == "628555666780")
            )
            assert account is not None
            assert account.pin_setup_status == "unknown"
            assert account.pin_change_status == "setup_unconfirmed"
            assert "账号资料尚未确认" in account.pin_change_message
            secret_row = session.get(AccountSecret, account.id)
            secret = json.loads(
                codec.decrypt(
                    secret_row.secret_payload_ciphertext,
                    context=f"account:{account.id}",
                )
            )
            assert secret["pin"] == "369258"
            assert secret["access_token"] == "otp-login-access"
    finally:
        pool.stop()


def test_otp_login_preserves_existing_pin_without_pin_verification(database):
    _engine, session_factory, codec = database
    account_id = str(uuid.uuid4())
    now = utc_now()
    with session_factory() as session, session.begin():
        session.add(
            Account(
                id=account_id,
                phone="+628555666777",
                phone_normalized="628555666777",
                local_phone="8555666777",
                pin_setup_status="configured",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            AccountSecret(
                account_id=account_id,
                secret_payload_ciphertext=codec.encrypt(
                    json.dumps({"pin": "147258", "access_token": "old-access"}),
                    context=f"account:{account_id}",
                ),
                updated_at=now,
            )
        )

    repository = TaskRepository(session_factory, codec, lease_seconds=10, retry_base_seconds=0.01)
    handler = AccountFlowHandler(
        "login",
        session_factory,
        codec,
        FakeOtpLoginAdapter(),
        SmsSettingsStore(session_factory, codec),
    )
    registry = HandlerRegistry()
    registry.register("account.login", handler, safe_to_retry=False)
    task, _created = repository.create_task(
        "account.login",
        {
            "phone_source": "manual",
            "phone": "+628555666777",
            "pin": "000000",
            "change_pin": False,
            "new_pin": "",
            "country_code": "+62",
            "proxy": "",
        },
        max_attempts=8,
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
    try:
        _wait(repository, task.id, "waiting_input", attempt=1)
        repository.submit_input(task.id, "otp", "135790")
        _wait(repository, task.id, "succeeded", attempt=2)

        with session_factory() as session:
            account = session.get(Account, account_id)
            assert account is not None
            assert account.pin_setup_status == "configured"
            secret_row = session.get(AccountSecret, account_id)
            secret = json.loads(
                codec.decrypt(secret_row.secret_payload_ciphertext, context=f"account:{account_id}")
            )
            assert secret["pin"] == "147258"
            assert secret["access_token"] == "otp-login-access"
    finally:
        pool.stop()


def test_otp_login_verifies_old_pin_during_requested_pin_change(database):
    _engine, session_factory, codec = database
    repository = TaskRepository(session_factory, codec, lease_seconds=10, retry_base_seconds=0.01)
    handler = AccountFlowHandler(
        "login",
        session_factory,
        codec,
        FakeOtpLoginAdapter(),
        SmsSettingsStore(session_factory, codec),
    )
    registry = HandlerRegistry()
    registry.register("account.login", handler, safe_to_retry=False)
    task, _created = repository.create_task(
        "account.login",
        {
            "phone_source": "manual",
            "phone": "+628555666778",
            "pin": "147258",
            "change_pin": True,
            "new_pin": "369258",
            "country_code": "+62",
            "proxy": "",
        },
        max_attempts=1,
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
    try:
        _wait(repository, task.id, "waiting_input", attempt=1)
        repository.submit_input(task.id, "otp", "135790")
        _wait(repository, task.id, "succeeded", attempt=2)

        result = repository.consume_result(task.id)
        assert result["pin_changed_now"] is True
        with session_factory() as session:
            account = session.scalar(
                select(Account).where(Account.phone_normalized == "628555666778")
            )
            assert account is not None
            assert account.pin_change_status == "changed_unconfirmed"
            secret_row = session.get(AccountSecret, account.id)
            secret = json.loads(
                codec.decrypt(
                    secret_row.secret_payload_ciphertext,
                    context=f"account:{account.id}",
                )
            )
            assert secret["pin"] == "369258"
    finally:
        pool.stop()


def test_reused_sms_activation_is_prepared_before_new_gopay_otp(database):
    _engine, session_factory, codec = database
    sms_store = SmsSettingsStore(session_factory, codec)
    sms_store.save(
        api_key="sms-test-key",
        base_url="https://sms.example.test",
        service="ni",
        country="6",
    )

    class SmsAdapter:
        def __init__(self) -> None:
            self.requested: list[tuple[str, str]] = []

        def configure_sms(self, **_values) -> None:
            return None

        def sms_status(self, api_key, activation_id):
            assert api_key == "sms-test-key"
            assert activation_id == "activation-reused"
            return "code_received", "112233"

        def sms_request_another(self, api_key, activation_id):
            self.requested.append((api_key, activation_id))
            return True

    class PrepareSmsContext:
        task_id = "task-sms-prepare"

        def __init__(self) -> None:
            self.repository = SimpleNamespace(
                get_task=lambda _task_id: SimpleNamespace(progress=0.16)
            )
            self.messages: list[str] = []
            self.saved: dict = {}

        def progress(self, _value, message):
            self.messages.append(message)

        def save_checkpoint(self, checkpoint):
            self.saved = dict(checkpoint)

    adapter = SmsAdapter()
    handler = AccountFlowHandler("login", session_factory, codec, adapter, sms_store)
    context = PrepareSmsContext()
    checkpoint = {
        "activation_id": "activation-reused",
        "consumed_code_hashes": [],
    }

    handler._prepare_sms_for_next_otp(context, checkpoint, "登录一阶段")

    assert adapter.requested == [("sms-test-key", "activation-reused")]
    assert checkpoint["sms_retry_ready"] is True
    assert hashlib.sha256(b"112233").hexdigest() in checkpoint["consumed_code_hashes"]
    assert any("已识别并忽略" in message for message in context.messages)
    assert any("已准备接收" in message for message in context.messages)


def test_login_stops_when_success_response_has_no_otp_token(database):
    _engine, session_factory, codec = database

    class MissingOtpTokenClient:
        def __init__(self) -> None:
            self.auth = _auth()

        def get_login_methods(self, _country_code, _phone):
            self.auth.verification_id = "verification-without-token"
            self.auth.methods = [{"method": "otp_sms"}]
            return {"status": 200, "body": {}}

        def initiate_otp(self, _country_code, _phone, *, method, flow):
            assert method == "otp_sms"
            assert flow == "login_1fa"
            return {"status": 200, "body": {}}

    class MissingOtpTokenAdapter:
        def new_gojek_client(self, _phone, **_kwargs):
            return MissingOtpTokenClient()

    repository = TaskRepository(session_factory, codec, lease_seconds=10, retry_base_seconds=0.01)
    handler = AccountFlowHandler(
        "login",
        session_factory,
        codec,
        MissingOtpTokenAdapter(),
        SmsSettingsStore(session_factory, codec),
    )
    registry = HandlerRegistry()
    registry.register("account.login", handler, safe_to_retry=False)
    task, _created = repository.create_task(
        "account.login",
        {
            "phone_source": "manual",
            "phone": "+628123400001",
            "pin": "147258",
            "change_pin": False,
            "new_pin": "",
            "country_code": "+62",
            "proxy": "",
        },
        max_attempts=1,
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
    try:
        _wait(repository, task.id, "failed", attempt=1)
        failed = repository.get_task(task.id)
        assert failed.last_error_code == "login_otp_token_missing"
        assert "otp_token" in failed.last_error_message
    finally:
        pool.stop()
