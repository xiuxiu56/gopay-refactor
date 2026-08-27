"""首个 SQLite 业务 Handler 的隔离测试。"""

from __future__ import annotations

import hashlib
import json
import uuid

from sqlalchemy import select

from gopay_app.db.models import Account, AccountSecret, PhoneNumber, SmsActivation, utc_now
from gopay_app.services.sms_settings import SmsSettings
from gopay_app.tasks.handlers.business import (
    AccountRefreshHandler,
    AccountRefreshSmsCodeHandler,
    SmsActivationCancelHandler,
)


class FakeAuth:
    access_token = "旧-access-token"
    refresh_token = "旧-refresh-token"


class FakeClient:
    def __init__(self) -> None:
        self.auth = FakeAuth()
        self.user_uuid = "customer-1"
        self.uniqueid = "device-1"
        self.session_id = "session-1"
        self.device_token = "device-token-1"

    def refresh_token(self):
        self.auth.access_token = "新-access-token"
        self.auth.refresh_token = "新-refresh-token"
        return {"status": 200, "body": {}}

    def get_balance(self):
        return {"status": 200, "body": {"data": {"balance": 321}}}


class FakeAdapter:
    def new_gojek_client(self, _phone: str, *, proxy: str = ""):
        assert proxy == "http://user:password@127.0.0.1:8080"
        return FakeClient()


class FakeContext:
    def __init__(self) -> None:
        self.resources: list[tuple[str, str]] = []
        self.progress_values: list[float] = []

    def acquire_resource(self, resource_type: str, resource_key: str) -> None:
        self.resources.append((resource_type, resource_key))

    def progress(self, value: float, _message: str = "") -> None:
        self.progress_values.append(value)


class FakeSmsStore:
    def get(self):
        return SmsSettings(
            api_key="测试-api-key",
            base_url="https://smsbower.example",
            service="ni",
            country="6",
        )


class FakeSmsAdapter:
    def __init__(self) -> None:
        self.requested = False
        self.ignored_hashes: set[str] = set()

    def configure_sms(self, **values) -> None:
        assert values == {
            "api_key": "测试-api-key",
            "base_url": "https://smsbower.example",
            "service": "ni",
            "country": "6",
        }

    def sms_status(self, api_key: str, activation_id: str):
        assert api_key == "测试-api-key"
        assert activation_id == "activation-latest-code"
        return "code_received", "111111"

    def sms_request_another(self, api_key: str, activation_id: str) -> bool:
        assert api_key == "测试-api-key"
        assert activation_id == "activation-latest-code"
        self.requested = True
        return True

    def sms_wait_code(
        self,
        api_key: str,
        activation_id: str,
        *,
        timeout: int,
        ignore_code_hashes: set[str],
    ) -> str:
        assert api_key == "测试-api-key"
        assert activation_id == "activation-latest-code"
        assert timeout == 120
        self.ignored_hashes = ignore_code_hashes
        assert hashlib.sha256(b"111111").hexdigest() in ignore_code_hashes
        return "222222"


def test_account_refresh_updates_only_new_database(database):
    _engine, session_factory, codec = database
    account_id = str(uuid.uuid4())
    secret = {
        "phone": "+628123456789",
        "access_token": "旧-access-token",
        "refresh_token": "旧-refresh-token",
        "proxy": "http://user:password@127.0.0.1:8080",
    }
    now = utc_now()
    with session_factory() as session, session.begin():
        session.add(
            Account(
                id=account_id,
                phone=secret["phone"],
                phone_normalized="628123456789",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            AccountSecret(
                account_id=account_id,
                secret_payload_ciphertext=codec.encrypt(
                    json.dumps(secret, ensure_ascii=False), context=f"account:{account_id}"
                ),
                updated_at=now,
            )
        )

    context = FakeContext()
    handler = AccountRefreshHandler(session_factory, codec, FakeAdapter())
    result = handler(context, {"account_id": account_id})
    assert result == {"account_id": account_id, "balance": 321}
    assert context.progress_values[-1] == 1.0
    assert context.resources == [("account", account_id)]

    with session_factory() as session:
        account = session.get(Account, account_id)
        stored = session.get(AccountSecret, account_id)
        assert account is not None and account.balance == 321 and account.version == 2
        assert stored is not None
        updated_secret = json.loads(
            codec.decrypt(stored.secret_payload_ciphertext, context=f"account:{account_id}")
        )
        assert updated_secret["access_token"] == "新-access-token"
        assert updated_secret["refresh_token"] == "新-refresh-token"


def test_refresh_sms_code_ignores_old_code_and_persists_hashes(database):
    _engine, session_factory, _codec = database
    account_id = str(uuid.uuid4())
    now = utc_now()
    with session_factory() as session, session.begin():
        session.add(
            Account(
                id=account_id,
                phone="+628123456780",
                phone_normalized="628123456780",
                sms_activation_status="active",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            SmsActivation(
                id=str(uuid.uuid4()),
                account_id=account_id,
                provider="smsbower",
                provider_activation_id="activation-latest-code",
                status="active",
                consumed_code_hashes_json="[]",
                created_at=now,
                updated_at=now,
            )
        )

    adapter = FakeSmsAdapter()
    context = FakeContext()
    handler = AccountRefreshSmsCodeHandler(session_factory, adapter, FakeSmsStore())
    result = handler(context, {"account_id": account_id})

    assert result == {
        "account_id": account_id,
        "activation_id": "activation-latest-code",
        "code": "222222",
        "provider": "smsbower",
    }
    assert adapter.requested is True
    assert context.resources == [
        ("account", account_id),
        ("sms", "smsbower:activation-latest-code"),
    ]
    assert context.progress_values[-1] == 1.0

    old_hash = hashlib.sha256(b"111111").hexdigest()
    new_hash = hashlib.sha256(b"222222").hexdigest()
    with session_factory() as session:
        activation = session.scalar(
            select(SmsActivation).where(
                SmsActivation.provider_activation_id == "activation-latest-code"
            )
        )
        account = session.get(Account, account_id)
        hashes = json.loads(activation.consumed_code_hashes_json)
        assert hashes == [old_hash, new_hash]
        assert "111111" not in activation.consumed_code_hashes_json
        assert "222222" not in activation.consumed_code_hashes_json
        assert account.sms_activation_status == "active"


def test_delayed_sms_activation_handler_marks_number_released(database):
    _engine, session_factory, _codec = database
    phone_id = str(uuid.uuid4())
    now = utc_now()
    with session_factory() as session, session.begin():
        session.add(
            PhoneNumber(
                id=phone_id,
                phone="+628123456781",
                phone_normalized="628123456781",
                source="smsbower",
                status="rented",
                sms_url_ciphertext="",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            SmsActivation(
                id=str(uuid.uuid4()),
                account_id=None,
                phone_number_id=phone_id,
                provider="smsbower",
                provider_activation_id="activation-cancel-later",
                status="release_pending",
                consumed_code_hashes_json="[]",
                created_at=now,
                updated_at=now,
            )
        )

    class CancelAdapter:
        def configure_sms(self, **_values) -> None:
            return None

        def sms_cancel(self, api_key, activation_id):
            assert api_key == "测试-api-key"
            assert activation_id == "activation-cancel-later"
            return True

    context = FakeContext()
    handler = SmsActivationCancelHandler(session_factory, CancelAdapter(), FakeSmsStore())
    result = handler(
        context,
        {"provider": "smsbower", "activation_id": "activation-cancel-later"},
    )

    assert result["cancelled"] is True
    assert context.resources == [("sms", "smsbower:activation-cancel-later")]
    with session_factory() as session:
        activation = session.scalar(
            select(SmsActivation).where(
                SmsActivation.provider_activation_id == "activation-cancel-later"
            )
        )
        phone = session.get(PhoneNumber, phone_id)
        assert activation is not None and activation.status == "cancelled"
        assert phone is not None and phone.status == "released"
