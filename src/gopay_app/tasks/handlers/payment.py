"""可恢复的 Midtrans GoPay 支付与远端状态核验 Handler。"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import suppress
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from gopay_app.db.models import (
    Account,
    AccountSecret,
    ChangeLog,
    PaymentIntent,
    SmsActivation,
    utc_now,
)
from gopay_app.protocols.legacy import LegacyProtocolAdapter, ProtocolUnavailableError
from gopay_app.security.codec import SecretCodec
from gopay_app.services.payment_intents import extract_snap_token
from gopay_app.services.sms_settings import SmsSettingsStore

from ..context import TaskContext
from ..errors import (
    PermanentTaskError,
    RetryableTaskError,
    ReviewTaskError,
    TaskWaitingInput,
)
from .sms_provider import build_sms_stores, call_sms, get_sms_settings, provider_label

SUCCESS_TRANSACTION_STATUSES = {"settlement", "capture"}
FAILED_TRANSACTION_STATUSES = {"deny", "cancel", "expire", "failure"}
UNCERTAIN_PHASES = {
    "linking_pending",
    "reference_validation_pending",
    "consent_pending",
    "otp_request_pending",
    "otp_validation_pending",
    "linking_pin_pending",
    "charge_pending",
    "payment_confirm_pending",
    "payment_process_pending",
}


def _body(response: dict[str, Any]) -> dict[str, Any]:
    value = response.get("body")
    return value if isinstance(value, dict) else {}


def _status(response: dict[str, Any]) -> int:
    try:
        return int(response.get("status") or 0)
    except (TypeError, ValueError):
        return 0


def _extract_nested(value: Any, keys: set[str]) -> str:
    if isinstance(value, dict):
        for key, candidate in value.items():
            if key in keys and candidate not in {None, ""}:
                return str(candidate)
        for candidate in value.values():
            found = _extract_nested(candidate, keys)
            if found:
                return found
    elif isinstance(value, list):
        for candidate in value:
            found = _extract_nested(candidate, keys)
            if found:
                return found
    return ""


def _challenge_id(value: dict[str, Any]) -> str:
    found = _extract_nested(value, {"challenge_id"})
    if found:
        return found
    matched = re.search(
        r"challengeId=([0-9a-fA-F-]{36})",
        json.dumps(value, ensure_ascii=False),
    )
    return matched.group(1) if matched else ""


def _challenge_reference(value: dict[str, Any]) -> str:
    for action in value.get("actions") or []:
        if isinstance(action, dict):
            matched = re.search(r"reference=([A-Za-z0-9_-]+)", str(action.get("url") or ""))
            if matched:
                return matched.group(1)
    for key in ("gopay_verification_link_url", "redirect_url", "url", "deeplink_url"):
        matched = re.search(r"reference=([A-Za-z0-9_-]+)", str(value.get(key) or ""))
        if matched:
            return matched.group(1)
    return ""


def _reference_id(value: dict[str, Any]) -> str:
    matched = re.search(r"reference=([0-9a-fA-F-]{36})", str(value.get("activation_link_url") or ""))
    return matched.group(1) if matched else ""


def _account_phone_parts(account: Account) -> tuple[str, str]:
    full = re.sub(r"\D", "", account.phone)
    local = re.sub(r"\D", "", account.local_phone)
    if local and full.endswith(local):
        return full[: -len(local)] or "62", local
    if full.startswith("62"):
        return "62", full[2:]
    return "62", local or full


def _cookies(payment: Any) -> dict[str, str]:
    jar = getattr(getattr(payment, "_session", None), "cookies", None)
    if jar is None:
        return {}
    if hasattr(jar, "get_dict"):
        with suppress(Exception):
            return {str(key): str(value) for key, value in jar.get_dict().items()}
    if isinstance(jar, dict):
        return {str(key): str(value) for key, value in jar.items()}
    return {}


def _restore_cookies(payment: Any, values: dict[str, Any]) -> None:
    jar = getattr(getattr(payment, "_session", None), "cookies", None)
    if jar is not None and hasattr(jar, "update"):
        with suppress(Exception):
            jar.update({str(key): str(value) for key, value in values.items()})


class PaymentStateStore:
    """统一更新支付公开状态和加密远端摘要。"""

    def __init__(self, session_factory: sessionmaker[Session], codec: SecretCodec) -> None:
        self._session_factory = session_factory
        self._codec = codec

    def update(
        self,
        payment_id: str,
        *,
        status: str,
        transaction_status: str = "",
        message: str = "",
        remote_state: dict[str, Any] | None = None,
    ) -> None:
        now = utc_now()
        with self._session_factory() as session, session.begin():
            intent = session.get(PaymentIntent, payment_id)
            if intent is None:
                return
            intent.status = status
            if transaction_status:
                intent.transaction_status = transaction_status[:32]
            intent.last_error_message = message[:1000]
            if remote_state is not None:
                intent.raw_state_ciphertext = self._codec.encrypt(
                    json.dumps(remote_state, ensure_ascii=False, separators=(",", ":")),
                    context=f"payment:{payment_id}:state",
                )
                intent.order_id = _extract_nested(remote_state, {"order_id"})[:160] or intent.order_id
                amount = _extract_nested(remote_state, {"gross_amount", "amount"})
                with suppress(TypeError, ValueError):
                    intent.amount = int(float(amount))
                intent.currency = _extract_nested(remote_state, {"currency"})[:8] or intent.currency or "IDR"
            intent.updated_at = now
            session.add(
                ChangeLog(
                    event_type="payment.updated",
                    resource="payment",
                    resource_id=payment_id,
                    operation=status,
                    payload_json=json.dumps(
                        {
                            "id": payment_id,
                            "status": status,
                            "transaction_status": intent.transaction_status,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    created_at=now,
                )
            )


class PaymentExecutionHandler:
    """将支付远端副作用拆成加密检查点，并在不确定阶段转人工复核。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        codec: SecretCodec,
        adapter: LegacyProtocolAdapter,
        sms_store: SmsSettingsStore,
        hero_sms_store: SmsSettingsStore | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._codec = codec
        self._adapter = adapter
        self._sms_stores = build_sms_stores(sms_store, hero_sms_store)
        self._state = PaymentStateStore(session_factory, codec)

    def __call__(self, context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
        payment_id = str(payload.get("payment_id") or "").strip()
        if not payment_id:
            raise PermanentTaskError("缺少 payment_id", code="payment_id_required")
        self._state.update(payment_id, status="running")
        checkpoint = context.checkpoint()
        try:
            prepared = self._prepare(context, payment_id, payload, checkpoint)
            payment = self._new_payment(prepared)
            _restore_cookies(payment, prepared.get("cookies") or {})
            result = self._run(context, prepared, payment)
            self._state.update(
                payment_id,
                status="succeeded",
                transaction_status=str(result.get("transaction_status") or "settlement"),
                remote_state=result.get("remote_state") or {},
            )
            return {
                "payment_id": payment_id,
                "account_id": prepared["account_id"],
                "transaction_status": result.get("transaction_status") or "settlement",
            }
        except TaskWaitingInput:
            self._state.update(payment_id, status="waiting_otp")
            raise
        except RetryableTaskError as exc:
            self._state.update(payment_id, status="retry_wait", message=str(exc))
            raise
        except PermanentTaskError as exc:
            self._state.update(payment_id, status="failed", message=str(exc))
            raise
        except ReviewTaskError as exc:
            self._state.update(payment_id, status="needs_review", message=str(exc))
            raise
        except Exception as exc:
            message = "支付协议中断，远端副作用结果需要复核"
            self._state.update(payment_id, status="needs_review", message=message)
            raise ReviewTaskError(message, code="payment_interrupted") from exc

    def _prepare(
        self,
        context: TaskContext,
        payment_id: str,
        payload: dict[str, Any],
        checkpoint: dict[str, Any],
    ) -> dict[str, Any]:
        with self._session_factory() as session:
            intent = session.get(PaymentIntent, payment_id)
            if intent is None or not intent.account_id:
                raise PermanentTaskError("支付意图或支付账号不存在", code="payment_not_found")
            account = session.get(Account, intent.account_id)
            secret_row = session.get(AccountSecret, intent.account_id)
            if account is None or secret_row is None:
                raise PermanentTaskError("支付账号密钥不存在", code="payment_account_missing")
            secret = json.loads(
                self._codec.decrypt(
                    secret_row.secret_payload_ciphertext,
                    context=f"account:{intent.account_id}",
                )
            )
            midtrans_url = self._codec.decrypt(
                intent.midtrans_url_ciphertext,
                context=f"payment:{payment_id}:url",
            )
            fingerprint = json.loads(account.payment_fingerprint_json or "{}")
            country_code, local_phone = _account_phone_parts(account)
            activation = session.scalar(
                select(SmsActivation)
                .where(
                    SmsActivation.account_id == intent.account_id,
                    SmsActivation.status.in_(("active", "unknown", "rented")),
                )
                .order_by(SmsActivation.updated_at.desc())
                .limit(1)
            )
            activation_id = (
                activation.provider_activation_id
                if activation is not None
                else str(secret.get("activation_id") or "")
            )
            activation_provider = (
                activation.provider
                if activation is not None
                else str(secret.get("activation_provider") or "smsbower")
            )

        pin = str(payload.get("pin") or secret.get("pin") or "").strip()
        if not re.fullmatch(r"\d{6}", pin):
            raise PermanentTaskError("支付账号缺少有效的 6 位 PIN", code="payment_pin_missing")
        snap = extract_snap_token(midtrans_url)
        if not snap:
            raise PermanentTaskError("Midtrans 链接格式不正确", code="midtrans_url_invalid")
        account_id = str(intent.account_id)
        context.acquire_resource("account", account_id, ttl_seconds=600)
        context.acquire_resource("payment", hashlib.sha256(snap.encode()).hexdigest(), ttl_seconds=600)
        proxy = str(payload.get("proxy") or secret.get("proxy") or "")
        if checkpoint:
            checkpoint.update(
                {
                    "pin": pin,
                    "proxy": proxy,
                    "payment_fingerprint": fingerprint,
                    "activation_id": activation_id,
                    "activation_provider": activation_provider,
                }
            )
            return checkpoint
        prepared = {
            "phase": "prepared",
            "payment_id": payment_id,
            "account_id": account_id,
            "midtrans_url": midtrans_url,
            "snap": snap,
            "phone": account.phone,
            "country_code": country_code,
            "local_phone": local_phone,
            "pin": pin,
            "proxy": proxy,
            "payment_fingerprint": fingerprint,
            "activation_id": activation_id,
            "activation_provider": activation_provider,
            "consumed_code_hashes": [],
            "cookies": {},
        }
        context.save_checkpoint(prepared)
        self._state.update(payment_id, status="running")
        return prepared

    def _new_payment(self, checkpoint: dict[str, Any]):
        try:
            return self._adapter.new_payment(
                proxy=str(checkpoint.get("proxy") or ""),
                payment_fingerprint=checkpoint.get("payment_fingerprint") or {},
            )
        except ProtocolUnavailableError as exc:
            raise PermanentTaskError(str(exc), code="protocol_unavailable") from exc

    def _checkpoint(
        self,
        context: TaskContext,
        checkpoint: dict[str, Any],
        payment: Any,
        phase: str,
        **values: Any,
    ) -> None:
        checkpoint.update(values)
        checkpoint["phase"] = phase
        checkpoint["cookies"] = _cookies(payment)
        checkpoint.pop("input_type", None)
        checkpoint.pop("otp_purpose", None)
        context.save_checkpoint(checkpoint)

    def _pending(
        self,
        context: TaskContext,
        checkpoint: dict[str, Any],
        payment: Any,
        phase: str,
    ) -> None:
        self._checkpoint(context, checkpoint, payment, phase)

    def _expect(
        self,
        response: dict[str, Any],
        label: str,
        *,
        accepted: set[int] | None = None,
    ) -> dict[str, Any]:
        accepted = accepted or {200}
        status = _status(response)
        if status in accepted:
            return _body(response)
        if status == 0 or status == 429 or status >= 500:
            raise RetryableTaskError(f"{label}暂时失败：HTTP {status}", code="payment_temporary")
        raise PermanentTaskError(f"{label}失败：HTTP {status}", code="payment_rejected")

    def _run(
        self,
        context: TaskContext,
        checkpoint: dict[str, Any],
        payment: Any,
    ) -> dict[str, Any]:
        phase = str(checkpoint.get("phase") or "prepared")
        if phase in UNCERTAIN_PHASES:
            return self._reconcile_uncertain(context, checkpoint, payment)
        snap = str(checkpoint["snap"])

        if phase == "prepared":
            context.progress(0.08, "正在绑定 Midtrans 与 GoPay 账号")
            self._pending(context, checkpoint, payment, "linking_pending")
            response = payment._midtrans_post(
                f"/snap/v3/accounts/{snap}/linking",
                {
                    "type": "gopay",
                    "country_code": checkpoint["country_code"],
                    "phone_number": checkpoint["local_phone"],
                },
                auth_snap=snap,
            )
            body = self._expect(response, "创建 GoPay 绑定", accepted={200, 201})
            reference = _reference_id(body)
            if not reference:
                raise ReviewTaskError("绑定已提交但缺少 reference", code="linking_reference_missing")
            self._checkpoint(context, checkpoint, payment, "linking_ready", reference=reference)
            phase = "linking_ready"

        if phase == "linking_ready":
            context.progress(0.15, "正在验证支付绑定 reference")
            self._pending(context, checkpoint, payment, "reference_validation_pending")
            self._expect(
                payment._gwa_post(
                    "/v1/linking/validate-reference",
                    {"reference_id": checkpoint["reference"]},
                ),
                "验证绑定 reference",
            )
            self._checkpoint(context, checkpoint, payment, "reference_validated")
            phase = "reference_validated"

        if phase == "reference_validated":
            context.progress(0.22, "正在确认 GoPay 绑定授权")
            self._pending(context, checkpoint, payment, "consent_pending")
            self._expect(
                payment._gwa_post(
                    "/v1/linking/user-consent",
                    {"reference_id": checkpoint["reference"]},
                ),
                "确认绑定授权",
            )
            self._checkpoint(context, checkpoint, payment, "consented")
            phase = "consented"

        if phase == "consented":
            context.progress(0.28, "正在申请支付绑定 OTP")
            self._pending(context, checkpoint, payment, "otp_request_pending")
            self._expect(
                payment._gwa_post(
                    "/v1/linking/resend-otp",
                    {"reference_id": checkpoint["reference"], "otp_channel": "SMS"},
                ),
                "申请支付 OTP",
                accepted={200, 201, 204},
            )
            self._checkpoint(context, checkpoint, payment, "otp_requested")
            phase = "otp_requested"

        if phase == "otp_requested":
            otp = self._obtain_otp(context, checkpoint, payment)
            self._pending(context, checkpoint, payment, "otp_validation_pending")
            body = self._expect(
                payment._gwa_post(
                    "/v1/linking/validate-otp",
                    {"reference_id": checkpoint["reference"], "otp": otp},
                ),
                "验证支付 OTP",
            )
            challenge = _challenge_id(body)
            if not challenge:
                raise ReviewTaskError("OTP 已提交但缺少 PIN challenge", code="linking_challenge_missing")
            self._checkpoint(
                context,
                checkpoint,
                payment,
                "otp_validated",
                linking_challenge_id=challenge,
            )
            phase = "otp_validated"

        if phase == "otp_validated":
            context.progress(0.42, "正在进行 GoPay 绑定 PIN 授权")
            self._pending(context, checkpoint, payment, "linking_pin_pending")
            pin_token = self._adapter.payment_pin_verify(
                payment,
                str(checkpoint["linking_challenge_id"]),
                str(checkpoint["pin"]),
                purpose="linking",
            )
            self._expect(
                payment._gwa_post(
                    "/v1/linking/validate-pin",
                    {"reference_id": checkpoint["reference"], "token": pin_token},
                ),
                "验证绑定 PIN",
            )
            self._checkpoint(context, checkpoint, payment, "linked")
            phase = "linked"

        if phase == "linked":
            context.progress(0.55, "正在核验 GoPay 绑定状态")
            linked = False
            for _index in range(5):
                response = payment._midtrans_get(f"/snap/v3/accounts/{snap}/gopay")
                body = self._expect(response, "读取 GoPay 绑定状态")
                text = json.dumps(body, ensure_ascii=False).lower()
                if body.get("account_status") == "ENABLED" or "linked" in text:
                    linked = True
                    break
            if not linked:
                raise ReviewTaskError("GoPay 绑定状态尚未确认", code="linking_not_confirmed")
            self._checkpoint(context, checkpoint, payment, "link_confirmed")
            phase = "link_confirmed"

        if phase == "link_confirmed":
            context.progress(0.64, "正在向 Midtrans 提交扣款")
            self._pending(context, checkpoint, payment, "charge_pending")
            response = payment._midtrans_post(
                f"/snap/v2/transactions/{snap}/charge",
                {"payment_type": "gopay", "tokenization": "true", "promo_details": None},
            )
            body = _body(response)
            transaction_status = str(body.get("transaction_status") or "").lower()
            if str(body.get("fraud_status") or "").lower() == "deny" or transaction_status == "deny":
                raise PermanentTaskError("Midtrans 风控拒绝本次支付", code="payment_fraud_denied")
            self._expect(response, "提交 Midtrans 扣款", accepted={200, 201})
            if transaction_status in SUCCESS_TRANSACTION_STATUSES:
                self._checkpoint(context, checkpoint, payment, "completed")
                context.progress(1.0, "支付已完成并通过远端核验")
                return {"transaction_status": transaction_status, "remote_state": body}
            reference = _challenge_reference(body)
            if not reference:
                raise ReviewTaskError("扣款已提交但缺少支付 challenge", code="charge_reference_missing")
            self._checkpoint(
                context,
                checkpoint,
                payment,
                "charged",
                charge_reference=reference,
                charge_state=body,
            )
            phase = "charged"

        if phase == "charged":
            context.progress(0.75, "正在确认 Midtrans 支付 challenge")
            reference = str(checkpoint["charge_reference"])
            validate = self._expect(
                payment._gwa_get(f"/v1/payment/validate?reference_id={reference}"),
                "验证支付 challenge",
            )
            challenge = _challenge_id(validate)
            self._pending(context, checkpoint, payment, "payment_confirm_pending")
            confirm = self._expect(
                payment._gwa_post(
                    f"/v1/payment/confirm?reference_id={reference}",
                    {"payment_instructions": []},
                ),
                "确认支付 challenge",
            )
            challenge = challenge or _challenge_id(confirm)
            if not challenge:
                raise ReviewTaskError("支付确认后缺少 PIN challenge", code="payment_challenge_missing")
            self._checkpoint(
                context,
                checkpoint,
                payment,
                "payment_confirmed",
                payment_challenge_id=challenge,
            )
            phase = "payment_confirmed"

        if phase == "payment_confirmed":
            context.progress(0.86, "正在进行最终支付 PIN 授权")
            pin_token = self._adapter.payment_pin_verify(
                payment,
                str(checkpoint["payment_challenge_id"]),
                str(checkpoint["pin"]),
                purpose="payment",
            )
            self._pending(context, checkpoint, payment, "payment_process_pending")
            self._expect(
                payment._gwa_post(
                    f"/v1/payment/process?reference_id={checkpoint['charge_reference']}",
                    {
                        "challenge": {
                            "type": "GOPAY_PIN_CHALLENGE",
                            "value": {"pin_token": pin_token},
                        }
                    },
                ),
                "执行最终支付",
            )
            self._checkpoint(context, checkpoint, payment, "processed")
            phase = "processed"

        if phase in {"processed", "completed"}:
            result = self._remote_status(payment, snap)
            transaction_status = str(result.get("transaction_status") or "unknown").lower()
            if transaction_status in SUCCESS_TRANSACTION_STATUSES:
                context.progress(1.0, "支付已完成并通过远端核验")
                return {"transaction_status": transaction_status, "remote_state": result}
            if transaction_status in FAILED_TRANSACTION_STATUSES:
                raise PermanentTaskError(
                    f"远端支付状态为 {transaction_status}",
                    code="payment_remote_failed",
                )
            raise ReviewTaskError(
                f"远端支付状态仍为 {transaction_status}",
                code="payment_remote_pending",
            )

        raise ReviewTaskError("支付检查点状态需要人工复核", code="payment_checkpoint_unknown")

    def _remote_status(self, payment: Any, snap: str) -> dict[str, Any]:
        response = payment._midtrans_get(f"/snap/v1/transactions/{snap}/status")
        return self._expect(response, "读取 Midtrans 交易状态")

    def _reconcile_uncertain(
        self,
        context: TaskContext,
        checkpoint: dict[str, Any],
        payment: Any,
    ) -> dict[str, Any]:
        phase = str(checkpoint.get("phase") or "")
        if phase in {"charge_pending", "payment_process_pending"}:
            remote = self._remote_status(payment, str(checkpoint["snap"]))
            status = str(remote.get("transaction_status") or "unknown").lower()
            if status in SUCCESS_TRANSACTION_STATUSES:
                context.progress(1.0, "中断支付已通过远端状态确认成功")
                return {"transaction_status": status, "remote_state": remote}
            if status in FAILED_TRANSACTION_STATUSES:
                raise PermanentTaskError(
                    f"中断支付远端状态为 {status}",
                    code="payment_remote_failed",
                )
        raise ReviewTaskError(
            f"支付中断于 {phase}，需要执行远端复核",
            code="payment_side_effect_uncertain",
        )

    def _obtain_otp(
        self,
        context: TaskContext,
        checkpoint: dict[str, Any],
        payment: Any,
    ) -> str:
        value = context.consume_input("otp")
        activation_id = str(checkpoint.get("activation_id") or "")
        if value is None and activation_id:
            provider = str(checkpoint.get("activation_provider") or "smsbower")
            settings = get_sms_settings(self._sms_stores, provider)
            if settings is not None and settings.api_key:
                label = provider_label(provider)
                context.progress(0.34, f"{label} 正在自动获取支付 OTP")
                try:
                    value = call_sms(
                        self._adapter,
                        "sms_wait_code",
                        settings,
                        activation_id,
                        timeout=120,
                        ignore_code_hashes=set(checkpoint.get("consumed_code_hashes") or []),
                    )
                except Exception as exc:
                    raise RetryableTaskError(
                        f"{label} 获取支付 OTP 暂时失败",
                        code="payment_otp_temporary",
                    ) from exc
        if value is None:
            checkpoint["phase"] = "otp_requested"
            checkpoint["cookies"] = _cookies(payment)
            checkpoint["input_type"] = "otp"
            checkpoint["otp_purpose"] = "payment"
            return context.wait_for_input(
                "otp",
                timeout_seconds=300,
                checkpoint=checkpoint,
                message="支付 OTP 自动获取超时，正在等待手动输入",
            )
        normalized = str(value).strip()
        if not re.fullmatch(r"\d{4,8}", normalized):
            raise PermanentTaskError("支付 OTP 必须是 4 到 8 位数字", code="payment_otp_invalid")
        digest = hashlib.sha256(normalized.encode()).hexdigest()
        hashes = list(dict.fromkeys([*(checkpoint.get("consumed_code_hashes") or []), digest]))
        checkpoint["consumed_code_hashes"] = hashes
        if activation_id:
            provider = str(checkpoint.get("activation_provider") or "smsbower")
            with self._session_factory() as session, session.begin():
                activation = session.scalar(
                    select(SmsActivation).where(
                        SmsActivation.provider == provider,
                        SmsActivation.provider_activation_id == activation_id,
                    )
                )
                if activation is not None:
                    activation.consumed_code_hashes_json = json.dumps(hashes, separators=(",", ":"))
                    activation.updated_at = utc_now()
        return normalized


class PaymentReconcileHandler:
    """只读取 Midtrans 远端状态，不重复执行支付副作用。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        codec: SecretCodec,
        adapter: LegacyProtocolAdapter,
    ) -> None:
        self._session_factory = session_factory
        self._codec = codec
        self._adapter = adapter
        self._state = PaymentStateStore(session_factory, codec)

    def __call__(self, context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
        payment_id = str(payload.get("payment_id") or "").strip()
        with self._session_factory() as session:
            intent = session.get(PaymentIntent, payment_id)
            if intent is None:
                raise PermanentTaskError("支付意图不存在", code="payment_not_found")
            url = self._codec.decrypt(
                intent.midtrans_url_ciphertext,
                context=f"payment:{payment_id}:url",
            )
            account = session.get(Account, intent.account_id) if intent.account_id else None
            secret_row = session.get(AccountSecret, intent.account_id) if intent.account_id else None
            secret = {}
            if secret_row is not None:
                secret = json.loads(
                    self._codec.decrypt(
                        secret_row.secret_payload_ciphertext,
                        context=f"account:{intent.account_id}",
                    )
                )
        snap = extract_snap_token(url)
        if not snap:
            raise PermanentTaskError("Midtrans 链接格式不正确", code="midtrans_url_invalid")
        self._state.update(payment_id, status="running")
        context.acquire_resource("payment", hashlib.sha256(snap.encode()).hexdigest())
        context.progress(0.25, "正在读取 Midtrans 远端状态")
        try:
            payment = self._adapter.new_payment(
                proxy=str(secret.get("proxy") or ""),
                payment_fingerprint=(json.loads(account.payment_fingerprint_json or "{}") if account else {}),
            )
            response = payment._midtrans_get(f"/snap/v1/transactions/{snap}/status")
        except ProtocolUnavailableError as exc:
            raise PermanentTaskError(str(exc), code="protocol_unavailable") from exc
        except Exception as exc:
            raise RetryableTaskError("读取 Midtrans 状态暂时失败", code="reconcile_temporary") from exc
        if _status(response) != 200:
            raise RetryableTaskError(
                f"读取 Midtrans 状态失败：HTTP {_status(response)}",
                code="reconcile_temporary",
            )
        remote = _body(response)
        transaction_status = str(remote.get("transaction_status") or "unknown").lower()
        public_status = (
            "succeeded"
            if transaction_status in SUCCESS_TRANSACTION_STATUSES
            else "failed"
            if transaction_status in FAILED_TRANSACTION_STATUSES
            else "needs_review"
        )
        self._state.update(
            payment_id,
            status=public_status,
            transaction_status=transaction_status,
            message=("" if public_status == "succeeded" else f"远端状态：{transaction_status}"),
            remote_state=remote,
        )
        context.progress(1.0, f"远端支付状态已更新：{transaction_status}")
        return {
            "payment_id": payment_id,
            "status": public_status,
            "transaction_status": transaction_status,
        }
