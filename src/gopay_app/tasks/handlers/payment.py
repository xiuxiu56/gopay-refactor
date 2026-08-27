"""可恢复的 Midtrans GoPay 支付与远端状态核验 Handler。"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
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

LINK_RETRY_LIMIT = 2
LINK_RETRY_SECONDS = 12.0


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


def _response_detail(response: dict[str, Any]) -> str:
    """只提取远端公开错误字段，避免把响应中的令牌写入任务日志。"""
    body = _body(response)
    values: list[str] = []
    for key in ("code", "message_title", "message", "description", "error", "status_message"):
        found = _extract_nested(body, {key}).strip()
        if found and found not in values:
            values.append(found)
    return " · ".join(values)[:360]


def _find_balance(value: Any) -> int | None:
    if isinstance(value, dict):
        for key in ("balance", "amount", "value"):
            candidate = value.get(key)
            if isinstance(candidate, (int, float)):
                return int(candidate)
            if isinstance(candidate, str):
                with suppress(InvalidOperation, ValueError):
                    return int(Decimal(candidate))
        for candidate in value.values():
            balance = _find_balance(candidate)
            if balance is not None:
                return balance
    elif isinstance(value, list):
        for candidate in value:
            balance = _find_balance(candidate)
            if balance is not None:
                return balance
    return None


def _client_state(client: Any) -> dict[str, Any]:
    auth_fields = (
        "access_token",
        "refresh_token",
        "account_id",
        "token_type",
        "expires_in",
        "expires_at",
    )
    auth = {
        field: getattr(client.auth, field)
        for field in auth_fields
        if hasattr(client.auth, field)
    }
    values = {
        "auth": auth,
        "d1": str(getattr(client, "d1", "") or ""),
        "model": str(getattr(client, "model", "") or ""),
        "xm1_template": str(getattr(client, "xm1_template", "") or ""),
        "phone_make": str(getattr(client, "phone_make", "") or ""),
        "os_info": str(getattr(client, "os_info", "") or ""),
        "appid": str(getattr(client, "appid", "") or ""),
        "version": str(getattr(client, "version", "") or ""),
        "user_uuid": str(getattr(client, "user_uuid", "") or ""),
        "session_id": str(getattr(client, "session_id", "") or ""),
        "device_token": str(getattr(client, "device_token", "") or ""),
        "uniqueid": str(getattr(client, "uniqueid", "") or ""),
    }
    return values


def _apply_client_state(client: Any, saved: dict[str, Any]) -> Any:
    auth = saved.get("auth")
    if isinstance(auth, dict):
        for field, value in auth.items():
            if hasattr(client.auth, field):
                setattr(client.auth, field, value)
    for field in (
        "d1",
        "model",
        "xm1_template",
        "phone_make",
        "os_info",
        "appid",
        "version",
        "user_uuid",
        "session_id",
        "device_token",
        "uniqueid",
    ):
        if field in saved and hasattr(client, field):
            setattr(client, field, saved[field])
    return client


def _midtrans_meta(body: dict[str, Any], snap: str) -> dict[str, Any]:
    details = body.get("transaction_details")
    details = details if isinstance(details, dict) else {}
    merchant = body.get("merchant")
    merchant = merchant if isinstance(merchant, dict) else {}
    accounts = body.get("accounts")
    accounts = accounts if isinstance(accounts, dict) else {}
    gopay = accounts.get("gopay")
    gopay = gopay if isinstance(gopay, dict) else {}
    order_id = str(details.get("order_id") or body.get("order_id") or "").strip()
    return {
        "snap_token": snap,
        "order_id": order_id,
        "gross_amount": str(details.get("gross_amount") or body.get("gross_amount") or "").strip(),
        "currency": str(details.get("currency") or body.get("currency") or "").strip().upper(),
        "midtrans_client_key": str(
            merchant.get("client_key") or body.get("client_key") or ""
        ).strip(),
        "expiry_time": str(body.get("expiry_time") or "").strip(),
        "account_status": str(gopay.get("account_status") or "").strip().upper(),
        "transaction_status": str(body.get("transaction_status") or "").strip().lower(),
        "is_setup_authorization": order_id.lower().startswith(("setatt_", "ufpi_")),
    }


def _parse_expiry(value: str) -> datetime | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    with suppress(ValueError):
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z"):
        with suppress(ValueError):
            return datetime.strptime(normalized, fmt)
    return None


def _validate_midtrans_meta(meta: dict[str, Any], *, balance: int) -> None:
    order_id = str(meta.get("order_id") or "")
    amount_text = str(meta.get("gross_amount") or "")
    currency = str(meta.get("currency") or "").upper()
    transaction_status = str(meta.get("transaction_status") or "").lower()
    account_status = str(meta.get("account_status") or "").upper()
    if not order_id:
        raise PermanentTaskError("Midtrans 链接读取不到订单 ID", code="midtrans_order_missing")
    try:
        amount = Decimal(amount_text)
    except InvalidOperation as exc:
        raise PermanentTaskError("Midtrans 链接金额格式不正确", code="midtrans_amount_invalid") from exc
    if amount != Decimal(1) or currency != "IDR":
        raise PermanentTaskError(
            f"已拦截非 1 IDR 授权链：订单 {order_id}，金额 {amount_text or '-'} {currency or '-'}",
            code="midtrans_not_setup_payment",
        )
    if not meta.get("is_setup_authorization"):
        raise PermanentTaskError(
            f"Midtrans 订单不是 1 IDR 授权链：{order_id}",
            code="midtrans_order_type_invalid",
        )
    if transaction_status in {*SUCCESS_TRANSACTION_STATUSES, *FAILED_TRANSACTION_STATUSES}:
        raise PermanentTaskError(
            f"Midtrans 链接状态不可支付：{transaction_status}",
            code="midtrans_status_invalid",
        )
    if account_status == "ENABLED":
        raise PermanentTaskError(
            "这条 Midtrans 链接已经绑定过 GoPay，请重新生成支付链接",
            code="midtrans_already_linked",
        )
    expiry = _parse_expiry(str(meta.get("expiry_time") or ""))
    if expiry is not None and expiry <= datetime.now(expiry.tzinfo or UTC):
        raise PermanentTaskError(
            f"Midtrans 链接已过期：{meta.get('expiry_time')}",
            code="midtrans_expired",
        )
    if balance < 1:
        raise PermanentTaskError(
            f"GoPay 账号余额不足：当前 {balance} Rp，需要 1 Rp",
            code="payment_balance_insufficient",
        )
    if not str(meta.get("midtrans_client_key") or "").strip():
        raise PermanentTaskError(
            "Midtrans 交易信息缺少 merchant.client_key，已停止创建 GoPay 绑定",
            code="midtrans_client_key_missing",
        )


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
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._session_factory = session_factory
        self._codec = codec
        self._adapter = adapter
        self._sms_stores = build_sms_stores(sms_store, hero_sms_store)
        self._state = PaymentStateStore(session_factory, codec)
        self._sleep = sleep

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
        with self._session_factory() as session, session.begin():
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
            try:
                fingerprint = json.loads(account.payment_fingerprint_json or "{}")
            except (TypeError, ValueError):
                fingerprint = {}
            try:
                fingerprint = self._adapter.normalize_payment_fingerprint(
                    fingerprint,
                    phone=account.phone,
                    local=account.local_phone,
                    account_id=account.id,
                )
            except ProtocolUnavailableError as exc:
                raise PermanentTaskError(str(exc), code="protocol_unavailable") from exc
            fingerprint_json = json.dumps(
                fingerprint,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            if account.payment_fingerprint_json != fingerprint_json:
                account.payment_fingerprint_json = fingerprint_json
                account.updated_at = utc_now()
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
            saved_client = secret.get("protocol_client")
            if not isinstance(saved_client, dict):
                saved_client = {
                    "auth": {
                        "access_token": str(secret.get("access_token") or ""),
                        "refresh_token": str(secret.get("refresh_token") or ""),
                        "account_id": str(secret.get("account_id") or ""),
                    },
                    "user_uuid": str(secret.get("customer_id") or ""),
                    "uniqueid": str(secret.get("device_uniqueid") or ""),
                    "session_id": str(secret.get("device_session_id") or ""),
                    "device_token": str(secret.get("device_token") or ""),
                }
            account_balance = int(account.balance or 0)

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
                    "protocol_client": saved_client,
                }
            )
            if (
                checkpoint.get("phase") == "linking_pending"
                and not checkpoint.get("midtrans_client_key")
            ):
                checkpoint["phase"] = "prepared"
                context.save_checkpoint(checkpoint)
                context.progress(
                    0.01,
                    "检测到旧版 Midtrans 绑定鉴权检查点，正在按新流程重新执行安全预检",
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
            "protocol_client": saved_client,
            "balance": account_balance,
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

    def _pause(self, context: TaskContext, seconds: float) -> None:
        remaining = max(0.0, seconds)
        while remaining > 0:
            context.heartbeat()
            interval = min(1.0, remaining)
            self._sleep(interval)
            remaining -= interval

    def _refresh_account(
        self,
        context: TaskContext,
        checkpoint: dict[str, Any],
        payment: Any,
    ) -> None:
        proxy = str(checkpoint.get("proxy") or "")
        if proxy:
            context.progress(0.02, "支付前正在复检代理出口")
            try:
                probe = self._adapter.probe_proxy(proxy)
            except ProtocolUnavailableError as exc:
                raise PermanentTaskError(str(exc), code="protocol_unavailable") from exc
            except Exception as exc:
                raise RetryableTaskError(
                    "支付前代理出口检测暂时失败",
                    code="payment_proxy_temporary",
                ) from exc
            if not bool(probe.get("ok")):
                detail = str(probe.get("error") or probe.get("message") or "出口不可用")[:240]
                raise RetryableTaskError(
                    f"支付前代理出口不可用：{detail}",
                    code="payment_proxy_unavailable",
                )
            context.progress(
                0.04,
                f"支付前代理复检通过：出口 IP {probe.get('ip') or '-'!s}",
            )

        context.progress(0.05, "正在刷新 GoPay 支付账号令牌与余额")
        try:
            client = self._adapter.new_gojek_client(
                str(checkpoint["phone"]),
                proxy=proxy,
            )
            saved = checkpoint.get("protocol_client")
            _apply_client_state(client, saved if isinstance(saved, dict) else {})
        except ProtocolUnavailableError as exc:
            raise PermanentTaskError(str(exc), code="protocol_unavailable") from exc
        except Exception as exc:
            raise RetryableTaskError(
                "建立 GoPay 支付账号会话暂时失败",
                code="payment_account_session_temporary",
            ) from exc

        access_token = str(getattr(client.auth, "access_token", "") or "")
        refresh_token = str(getattr(client.auth, "refresh_token", "") or "")
        if not access_token and not refresh_token:
            raise PermanentTaskError(
                "GoPay 支付账号缺少登录令牌，请先重新登录账号",
                code="payment_account_token_missing",
            )
        if refresh_token:
            try:
                refresh = client.refresh_token()
            except Exception:
                refresh = {"status": 0, "body": {}}
            if _status(refresh) not in {200, 201}:
                if not access_token:
                    self._expect(refresh, "刷新 GoPay 支付账号令牌", accepted={200, 201})
                context.progress(0.06, "支付账号令牌刷新未通过，正在尝试现有会话")

        try:
            balance_response = client.get_balance()
        except Exception as exc:
            raise RetryableTaskError(
                "读取 GoPay 支付账号余额暂时失败",
                code="payment_balance_temporary",
            ) from exc
        balance_body = self._expect(
            balance_response,
            "读取 GoPay 支付账号余额",
            accepted={200, 201},
        )
        balance = _find_balance(balance_body)
        if balance is None:
            raise RetryableTaskError(
                "GoPay 支付账号余额响应格式发生变化",
                code="payment_balance_parse_failed",
            )

        saved_client = _client_state(client)
        checkpoint["protocol_client"] = saved_client
        checkpoint["balance"] = balance
        now = utc_now()
        account_id = str(checkpoint["account_id"])
        with self._session_factory() as session, session.begin():
            account = session.get(Account, account_id)
            secret_row = session.get(AccountSecret, account_id)
            if account is None or secret_row is None:
                raise PermanentTaskError(
                    "支付账号在执行期间被移除",
                    code="payment_account_removed",
                )
            secret = json.loads(
                self._codec.decrypt(
                    secret_row.secret_payload_ciphertext,
                    context=f"account:{account_id}",
                )
            )
            auth = saved_client.get("auth")
            auth = auth if isinstance(auth, dict) else {}
            secret["access_token"] = str(auth.get("access_token") or "")
            secret["refresh_token"] = str(auth.get("refresh_token") or "")
            secret["protocol_client"] = saved_client
            secret["balance"] = balance
            account.balance = balance
            account.updated_at = now
            account.version += 1
            secret_row.secret_payload_ciphertext = self._codec.encrypt(
                json.dumps(secret, ensure_ascii=False, separators=(",", ":")),
                context=f"account:{account_id}",
            )
            secret_row.updated_at = now
        self._checkpoint(
            context,
            checkpoint,
            payment,
            "account_ready",
            balance=balance,
        )
        context.progress(0.07, f"GoPay 支付账号预检通过：余额 {balance} Rp")

    def _read_midtrans_meta(
        self,
        context: TaskContext,
        checkpoint: dict[str, Any],
        payment: Any,
    ) -> None:
        snap = str(checkpoint["snap"])
        context.progress(0.08, "正在读取并校验 Midtrans 交易信息")
        try:
            response = self._adapter.payment_read_midtrans_transaction(payment, snap)
        except Exception as exc:
            raise RetryableTaskError(
                "读取 Midtrans 交易信息暂时失败",
                code="midtrans_meta_temporary",
            ) from exc
        body = self._expect(response, "读取 Midtrans 交易信息")
        meta = _midtrans_meta(body, snap)
        _validate_midtrans_meta(meta, balance=int(checkpoint.get("balance") or 0))
        self._state.update(
            str(checkpoint["payment_id"]),
            status="running",
            transaction_status=str(meta.get("transaction_status") or ""),
            remote_state=meta,
        )
        self._checkpoint(
            context,
            checkpoint,
            payment,
            "preflight_ready",
            **meta,
        )
        context.progress(
            0.1,
            "Midtrans 支付链接预检通过："
            f"订单 {meta['order_id']}，金额 {meta['gross_amount']} {meta['currency']}",
        )

    def _prepare_sms_for_payment(
        self,
        context: TaskContext,
        checkpoint: dict[str, Any],
        payment: Any,
    ) -> None:
        if checkpoint.get("sms_wait_prepared"):
            return
        activation_id = str(checkpoint.get("activation_id") or "")
        provider = str(checkpoint.get("activation_provider") or "smsbower")
        settings = get_sms_settings(self._sms_stores, provider)
        if activation_id and settings is not None and settings.api_key:
            label = provider_label(provider)
            try:
                ready = bool(
                    call_sms(
                        self._adapter,
                        "sms_request_another",
                        settings,
                        activation_id,
                    )
                )
            except Exception:
                ready = False
            context.progress(
                0.27,
                (
                    f"{label} 已准备接收支付新验证码"
                    if ready
                    else f"{label} 新验证码等待状态确认失败，将继续发送 OTP"
                ),
            )
        self._checkpoint(
            context,
            checkpoint,
            payment,
            "consented",
            sms_wait_prepared=True,
        )

    def _create_linking(
        self,
        context: TaskContext,
        checkpoint: dict[str, Any],
        payment: Any,
    ) -> dict[str, Any]:
        snap = str(checkpoint["snap"])
        request_body = {
            "type": "gopay",
            "country_code": checkpoint["country_code"],
            "phone_number": checkpoint["local_phone"],
        }
        for attempt in range(1, LINK_RETRY_LIMIT + 2):
            self._pending(context, checkpoint, payment, "linking_pending")
            response = payment._midtrans_post(
                f"/snap/v3/accounts/{snap}/linking",
                request_body,
                auth_snap=str(checkpoint["midtrans_client_key"]),
            )
            status = _status(response)
            if status in {200, 201}:
                return _body(response)
            detail = _response_detail(response)
            self._checkpoint(context, checkpoint, payment, "preflight_ready")
            if status in {406, 429} and attempt <= LINK_RETRY_LIMIT:
                reason = "链接存在待完成绑定" if status == 406 else "Midtrans 请求限频"
                context.progress(
                    0.11,
                    f"{reason}，等待 {int(LINK_RETRY_SECONDS)} 秒后重试绑定 "
                    f"{attempt}/{LINK_RETRY_LIMIT}",
                )
                self._pause(context, LINK_RETRY_SECONDS)
                continue
            if status == 406:
                raise PermanentTaskError(
                    "Midtrans 链接已有未完成的 GoPay 绑定状态，请重新生成支付链接"
                    + (f" · {detail}" if detail else ""),
                    code="midtrans_linking_conflict",
                )
            if status == 429:
                raise RetryableTaskError(
                    "Midtrans 创建 GoPay 绑定被限频，请稍后重试"
                    + (f" · {detail}" if detail else ""),
                    code="midtrans_linking_rate_limited",
                )
            self._expect(
                response,
                "创建 GoPay 绑定",
                accepted={200, 201},
                side_effect=True,
            )
        raise ReviewTaskError("创建 GoPay 绑定结果需要复核", code="linking_result_unknown")

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
        side_effect: bool = False,
    ) -> dict[str, Any]:
        accepted = accepted or {200}
        status = _status(response)
        if status in accepted:
            return _body(response)
        detail = _response_detail(response)
        message = f"{label}失败：HTTP {status}" + (f" · {detail}" if detail else "")
        if side_effect and (status == 0 or status >= 500):
            raise ReviewTaskError(message, code="payment_side_effect_uncertain")
        if status == 0 or status == 429 or status >= 500:
            raise RetryableTaskError(message, code="payment_temporary")
        raise PermanentTaskError(message, code="payment_rejected")

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
            self._refresh_account(context, checkpoint, payment)
            phase = "account_ready"

        if phase == "account_ready":
            self._read_midtrans_meta(context, checkpoint, payment)
            phase = "preflight_ready"

        if phase == "preflight_ready":
            context.progress(0.12, "支付步骤 1/14：正在创建 Midtrans GoPay 绑定")
            body = self._create_linking(context, checkpoint, payment)
            reference = _reference_id(body)
            if not reference:
                raise ReviewTaskError("绑定已提交但缺少 reference", code="linking_reference_missing")
            self._checkpoint(context, checkpoint, payment, "linking_ready", reference=reference)
            self._pause(context, 1)
            phase = "linking_ready"

        if phase == "linking_ready":
            context.progress(0.16, "支付步骤 2/14：正在验证 GoPay 绑定 reference")
            self._pending(context, checkpoint, payment, "reference_validation_pending")
            self._expect(
                payment._gwa_post(
                    "/v1/linking/validate-reference",
                    {"reference_id": checkpoint["reference"]},
                ),
                "验证绑定 reference",
                side_effect=True,
            )
            self._checkpoint(context, checkpoint, payment, "reference_validated")
            self._pause(context, 1)
            phase = "reference_validated"

        if phase == "reference_validated":
            context.progress(0.21, "支付步骤 3/14：正在确认 GoPay 绑定授权")
            self._pending(context, checkpoint, payment, "consent_pending")
            self._expect(
                payment._gwa_post(
                    "/v1/linking/user-consent",
                    {"reference_id": checkpoint["reference"]},
                ),
                "确认绑定授权",
                side_effect=True,
            )
            self._checkpoint(context, checkpoint, payment, "consented")
            self._pause(context, 1)
            phase = "consented"

        if phase == "consented":
            self._prepare_sms_for_payment(context, checkpoint, payment)
            context.progress(0.29, "支付步骤 4/14：正在申请 GoPay 绑定短信验证码")
            self._pending(context, checkpoint, payment, "otp_request_pending")
            self._expect(
                payment._gwa_post(
                    "/v1/linking/resend-otp",
                    {"reference_id": checkpoint["reference"], "otp_channel": "SMS"},
                ),
                "申请支付 OTP",
                accepted={200, 201, 204},
                side_effect=True,
            )
            self._checkpoint(context, checkpoint, payment, "otp_requested")
            phase = "otp_requested"

        if phase == "otp_requested":
            otp = self._obtain_otp(context, checkpoint, payment)
            context.progress(0.37, "支付步骤 5/14：正在验证 GoPay 绑定短信验证码")
            self._pause(context, 1)
            self._pending(context, checkpoint, payment, "otp_validation_pending")
            body = self._expect(
                payment._gwa_post(
                    "/v1/linking/validate-otp",
                    {"reference_id": checkpoint["reference"], "otp": otp},
                ),
                "验证支付 OTP",
                side_effect=True,
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
            self._pause(context, 1)
            phase = "otp_validated"

        if phase == "otp_validated":
            context.progress(0.43, "支付步骤 6/14：正在验证 GoPay 绑定 PIN")
            pin_token = self._adapter.payment_pin_verify(
                payment,
                str(checkpoint["linking_challenge_id"]),
                str(checkpoint["pin"]),
                purpose="linking",
            )
            self._checkpoint(
                context,
                checkpoint,
                payment,
                "linking_pin_ready",
                linking_pin_token=pin_token,
            )
            self._pause(context, 1)
            phase = "linking_pin_ready"

        if phase == "linking_pin_ready":
            context.progress(0.47, "支付步骤 7/14：正在提交 GoPay 绑定 PIN 授权")
            self._pending(context, checkpoint, payment, "linking_pin_pending")
            self._expect(
                payment._gwa_post(
                    "/v1/linking/validate-pin",
                    {
                        "reference_id": checkpoint["reference"],
                        "token": checkpoint["linking_pin_token"],
                    },
                ),
                "验证绑定 PIN",
                side_effect=True,
            )
            self._checkpoint(context, checkpoint, payment, "linked")
            phase = "linked"

        if phase == "linked":
            context.progress(0.53, "支付步骤 8/14：正在轮询 GoPay 绑定状态")
            linked = False
            for index in range(10):
                self._pause(context, 2)
                response = payment._midtrans_get(f"/snap/v3/accounts/{snap}/gopay")
                if _status(response) == 200:
                    body = _body(response)
                    text = json.dumps(body, ensure_ascii=False).lower()
                    if body.get("account_status") == "ENABLED" or "linked" in text:
                        linked = True
                        break
                context.progress(
                    0.53,
                    f"GoPay 绑定状态尚未生效，正在继续轮询 {index + 1}/10",
                )
            if not linked:
                raise ReviewTaskError("GoPay 绑定状态尚未确认", code="linking_not_confirmed")
            self._checkpoint(context, checkpoint, payment, "link_confirmed")
            self._pause(context, 1)
            phase = "link_confirmed"

        if phase == "link_confirmed":
            context.progress(0.61, "支付步骤 9/14：正在向 Midtrans 提交扣款")
            self._pending(context, checkpoint, payment, "charge_pending")
            response = payment._midtrans_post(
                f"/snap/v2/transactions/{snap}/charge",
                {"payment_type": "gopay", "tokenization": "true", "promo_details": None},
            )
            body = _body(response)
            transaction_status = str(body.get("transaction_status") or "").lower()
            if str(body.get("fraud_status") or "").lower() == "deny" or transaction_status == "deny":
                raise PermanentTaskError("Midtrans 风控拒绝本次支付", code="payment_fraud_denied")
            self._expect(
                response,
                "提交 Midtrans 扣款",
                accepted={200, 201},
                side_effect=True,
            )
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
            verification_url = str(
                (checkpoint.get("charge_state") or {}).get("gopay_verification_link_url") or ""
            )
            if verification_url and not checkpoint.get("verification_page_warmed"):
                context.progress(0.68, "正在预热 GoPay 支付验证页面并建立会话")
                try:
                    warm = self._adapter.payment_warm_verification_page(payment, verification_url)
                    warmed = _status(warm) in set(range(200, 400))
                except Exception:
                    warmed = False
                self._checkpoint(
                    context,
                    checkpoint,
                    payment,
                    "charged",
                    verification_page_warmed=True,
                )
                context.progress(
                    0.69,
                    "GoPay 支付验证页面预热完成"
                    if warmed
                    else "GoPay 支付验证页面预热未完成，正在继续协议验证",
                )
            self._pause(context, 1)
            context.progress(0.72, "支付步骤 10/14：正在验证 Midtrans 支付 challenge")
            reference = str(checkpoint["charge_reference"])
            validate = self._expect(
                payment._gwa_get(f"/v1/payment/validate?reference_id={reference}"),
                "验证支付 challenge",
            )
            challenge = _challenge_id(validate)
            self._pause(context, 1)
            context.progress(0.77, "支付步骤 11/14：正在确认 Midtrans 支付 challenge")
            self._pending(context, checkpoint, payment, "payment_confirm_pending")
            confirm = self._expect(
                payment._gwa_post(
                    f"/v1/payment/confirm?reference_id={reference}",
                    {"payment_instructions": []},
                ),
                "确认支付 challenge",
                side_effect=True,
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
            self._pause(context, 1)
            phase = "payment_confirmed"

        if phase == "payment_confirmed":
            context.progress(0.84, "支付步骤 12/14：正在验证最终支付 PIN")
            pin_token = self._adapter.payment_pin_verify(
                payment,
                str(checkpoint["payment_challenge_id"]),
                str(checkpoint["pin"]),
                purpose="payment",
            )
            self._checkpoint(
                context,
                checkpoint,
                payment,
                "payment_pin_ready",
                payment_pin_token=pin_token,
            )
            self._pause(context, 1)
            phase = "payment_pin_ready"

        if phase == "payment_pin_ready":
            context.progress(0.9, "支付步骤 13/14：正在执行最终支付")
            self._pending(context, checkpoint, payment, "payment_process_pending")
            self._expect(
                payment._gwa_post(
                    f"/v1/payment/process?reference_id={checkpoint['charge_reference']}",
                    {
                        "challenge": {
                            "type": "GOPAY_PIN_CHALLENGE",
                            "value": {"pin_token": checkpoint["payment_pin_token"]},
                        }
                    },
                ),
                "执行最终支付",
                side_effect=True,
            )
            self._checkpoint(context, checkpoint, payment, "processed")
            self._pause(context, 2)
            phase = "processed"

        if phase in {"processed", "completed"}:
            context.progress(0.96, "支付步骤 14/14：正在核验 Midtrans 最终交易状态")
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
