"""P4 可恢复支付状态机测试。"""

from __future__ import annotations

import hashlib
import json
import uuid
from types import SimpleNamespace

import pytest

from gopay_app.db.models import Account, AccountSecret, PaymentIntent, utc_now
from gopay_app.services.sms_settings import SmsSettingsStore
from gopay_app.tasks.errors import TaskWaitingInput
from gopay_app.tasks.handlers.payment import PaymentExecutionHandler

SNAP = "11111111-2222-3333-4444-555555555555"
MIDTRANS_URL = f"https://app.midtrans.com/snap/v4/redirection/{SNAP}#/gopay-tokenization/linking"


class FakeCookieJar(dict):
    def get_dict(self):
        return dict(self)


class FakePayment:
    def __init__(self) -> None:
        self._session = SimpleNamespace(cookies=FakeCookieJar({"payment-session": "cookie-1"}))

    def _midtrans_post(self, path, _body, **_kwargs):
        if path.endswith("/linking"):
            return {
                "status": 201,
                "body": {
                    "activation_link_url": (
                        "https://example.test/link?reference=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
                    )
                },
            }
        if path.endswith("/charge"):
            return {
                "status": 200,
                "body": {
                    "transaction_status": "pending",
                    "actions": [{"url": "https://example.test/pay?reference=charge-ref-1"}],
                },
            }
        raise AssertionError(path)

    def _midtrans_get(self, path):
        if path.endswith("/gopay"):
            return {"status": 200, "body": {"account_status": "ENABLED"}}
        if path.endswith("/status"):
            return {
                "status": 200,
                "body": {
                    "transaction_status": "settlement",
                    "order_id": "order-p4-1",
                    "gross_amount": "1",
                    "currency": "IDR",
                },
            }
        raise AssertionError(path)

    def _gwa_post(self, path, _body):
        if path == "/v1/linking/validate-otp":
            return {
                "status": 200,
                "body": {"challenge_id": "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"},
            }
        if path.startswith("/v1/payment/confirm"):
            return {
                "status": 200,
                "body": {"challenge_id": "cccccccc-dddd-eeee-ffff-000000000000"},
            }
        return {"status": 200, "body": {}}

    def _gwa_get(self, path):
        assert path.startswith("/v1/payment/validate")
        return {
            "status": 200,
            "body": {"challenge_id": "cccccccc-dddd-eeee-ffff-000000000000"},
        }


class FakeAdapter:
    def new_payment(self, *, proxy="", payment_fingerprint=None):
        assert proxy == ""
        assert payment_fingerprint == {"profile_id": "profile-p4"}
        return FakePayment()

    def payment_pin_verify(self, _payment, _challenge_id, pin, *, purpose):
        assert pin == "147258"
        return f"pin-token-{purpose}"


class FakeContext:
    def __init__(self, checkpoint=None, input_value=None) -> None:
        self._checkpoint = checkpoint or {}
        self._input_value = input_value
        self.progress_values: list[float] = []
        self.resources: list[tuple[str, str]] = []

    def checkpoint(self):
        return dict(self._checkpoint)

    def save_checkpoint(self, value):
        self._checkpoint = dict(value)

    def acquire_resource(self, resource_type, resource_key, **_kwargs):
        self.resources.append((resource_type, resource_key))

    def progress(self, value, _message=""):
        self.progress_values.append(value)

    def consume_input(self, input_type):
        assert input_type == "otp"
        value, self._input_value = self._input_value, None
        return value

    def wait_for_input(self, input_type, *, timeout_seconds, checkpoint, message):
        assert timeout_seconds == 300
        raise TaskWaitingInput(
            input_type,
            expires_at=utc_now(),
            checkpoint=checkpoint,
            message=message,
        )


def _seed_payment(database):
    _engine, session_factory, codec = database
    account_id = str(uuid.uuid4())
    payment_id = str(uuid.uuid4())
    now = utc_now()
    with session_factory() as session, session.begin():
        session.add(
            Account(
                id=account_id,
                phone="+628123456789",
                phone_normalized="628123456789",
                local_phone="8123456789",
                balance=100,
                pin_setup_status="configured",
                payment_fingerprint_json=json.dumps({"profile_id": "profile-p4"}),
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            AccountSecret(
                account_id=account_id,
                secret_payload_ciphertext=codec.encrypt(
                    json.dumps({"pin": "147258", "proxy": ""}),
                    context=f"account:{account_id}",
                ),
                updated_at=now,
            )
        )
        session.add(
            PaymentIntent(
                id=payment_id,
                snap_token_hash=hashlib.sha256(SNAP.encode()).hexdigest(),
                account_id=account_id,
                status="queued",
                midtrans_url_ciphertext=codec.encrypt(
                    MIDTRANS_URL,
                    context=f"payment:{payment_id}:url",
                ),
                raw_state_ciphertext=codec.encrypt(
                    "{}",
                    context=f"payment:{payment_id}:state",
                ),
                created_at=now,
                updated_at=now,
            )
        )
    return session_factory, codec, account_id, payment_id


def test_payment_resumes_after_manual_otp_and_verifies_remote_status(database):
    session_factory, codec, account_id, payment_id = _seed_payment(database)
    handler = PaymentExecutionHandler(
        session_factory,
        codec,
        FakeAdapter(),
        SmsSettingsStore(session_factory, codec),
    )
    first_context = FakeContext()
    with pytest.raises(TaskWaitingInput) as waiting:
        handler(first_context, {"payment_id": payment_id})
    checkpoint = waiting.value.checkpoint
    assert checkpoint["phase"] == "otp_requested"
    assert checkpoint["cookies"] == {"payment-session": "cookie-1"}

    second_context = FakeContext(checkpoint, input_value="123456")
    result = handler(second_context, {"payment_id": payment_id})
    assert result == {
        "payment_id": payment_id,
        "account_id": account_id,
        "transaction_status": "settlement",
    }
    assert second_context.progress_values[-1] == 1.0
    assert ("account", account_id) in second_context.resources

    with session_factory() as session:
        intent = session.get(PaymentIntent, payment_id)
        assert intent is not None
        assert intent.status == "succeeded"
        assert intent.transaction_status == "settlement"
        assert intent.order_id == "order-p4-1"
        assert intent.amount == 1
        state = json.loads(
            codec.decrypt(
                intent.raw_state_ciphertext,
                context=f"payment:{payment_id}:state",
            )
        )
        assert state["transaction_status"] == "settlement"
