"""首批直接读写 SQLite 的 GoPay 业务 Handler。"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from gopay_app.db.models import (
    Account,
    AccountSecret,
    ChangeLog,
    PhoneNumber,
    SmsActivation,
    utc_now,
)
from gopay_app.protocols.legacy import LegacyProtocolAdapter, ProtocolUnavailableError
from gopay_app.security.codec import SecretCodec
from gopay_app.services.sms_settings import SmsSettingsStore

from ..context import TaskContext
from ..errors import PermanentTaskError, RetryableTaskError
from .sms_provider import build_sms_stores, call_sms, get_sms_settings, provider_label


def _find_balance(value: Any) -> int | None:
    if isinstance(value, dict):
        for key in ("balance", "amount", "value"):
            candidate = value.get(key)
            if isinstance(candidate, (int, float)):
                return int(candidate)
            if isinstance(candidate, str) and candidate.replace(".", "", 1).isdigit():
                return int(float(candidate))
        for nested in value.values():
            result = _find_balance(nested)
            if result is not None:
                return result
    if isinstance(value, list):
        for nested in value:
            result = _find_balance(nested)
            if result is not None:
                return result
    return None


class AccountRefreshHandler:
    """使用旧纯协议客户端刷新令牌和余额，状态只写入新数据库。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        codec: SecretCodec,
        adapter: LegacyProtocolAdapter,
    ) -> None:
        self._session_factory = session_factory
        self._codec = codec
        self._adapter = adapter

    def __call__(self, context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
        account_id = str(payload.get("account_id") or "").strip()
        if not account_id:
            raise PermanentTaskError("缺少 account_id", code="account_id_required")

        with self._session_factory() as session:
            account = session.get(Account, account_id)
            secret_row = session.get(AccountSecret, account_id)
            if account is None or secret_row is None:
                raise PermanentTaskError("账号或账号密钥记录不存在", code="account_not_found")
            secret = json.loads(
                self._codec.decrypt(secret_row.secret_payload_ciphertext, context=f"account:{account_id}")
            )
            phone = account.phone

        context.acquire_resource("account", account_id)
        proxy = str(secret.get("proxy") or "")
        context.progress(0.1, "正在建立账号协议会话")

        try:
            client = self._adapter.new_gojek_client(phone, proxy=proxy)
        except ProtocolUnavailableError as exc:
            raise PermanentTaskError(str(exc), code="protocol_unavailable") from exc

        client.auth.access_token = str(secret.get("access_token") or "")
        client.auth.refresh_token = str(secret.get("refresh_token") or "")
        client.user_uuid = str(secret.get("customer_id") or "")
        client.uniqueid = str(secret.get("device_uniqueid") or client.uniqueid)
        client.session_id = str(secret.get("device_session_id") or client.session_id)
        client.device_token = str(secret.get("device_token") or client.device_token)
        if not client.auth.refresh_token and not client.auth.access_token:
            raise PermanentTaskError("账号缺少可用登录令牌", code="account_token_missing")

        context.progress(0.3, "正在刷新账号令牌")
        refresh = client.refresh_token() if client.auth.refresh_token else {"status": 200, "body": {}}
        refresh_status = int(refresh.get("status") or 0)
        if refresh_status not in {200, 201}:
            if refresh_status == 0 or refresh_status >= 500:
                raise RetryableTaskError("刷新账号令牌时服务暂时不可用", code="refresh_temporary")
            raise PermanentTaskError("账号令牌已失效，需要重新登录", code="refresh_rejected")

        context.progress(0.65, "正在读取 GoPay 余额")
        balance_response = client.get_balance()
        balance_status = int(balance_response.get("status") or 0)
        if balance_status not in {200, 201}:
            raise RetryableTaskError("读取 GoPay 余额失败", code="balance_query_failed")
        balance = _find_balance(balance_response.get("body"))
        if balance is None:
            raise RetryableTaskError("GoPay 余额响应格式发生变化", code="balance_parse_failed")

        secret["access_token"] = client.auth.access_token
        secret["refresh_token"] = client.auth.refresh_token
        secret["customer_id"] = client.user_uuid or secret.get("customer_id", "")
        now = utc_now()
        with self._session_factory() as session, session.begin():
            account = session.get(Account, account_id)
            secret_row = session.get(AccountSecret, account_id)
            if account is None or secret_row is None:
                raise PermanentTaskError("账号在执行期间被移除", code="account_removed")
            account.balance = balance
            account.customer_id = str(secret.get("customer_id") or account.customer_id)
            account.updated_at = now
            account.version += 1
            secret_row.secret_payload_ciphertext = self._codec.encrypt(
                json.dumps(secret, ensure_ascii=False, separators=(",", ":")),
                context=f"account:{account_id}",
            )
            secret_row.updated_at = now
            session.add(
                ChangeLog(
                    event_type="account.updated",
                    resource="account",
                    resource_id=account_id,
                    operation="refresh",
                    payload_json=json.dumps(
                        {"id": account_id, "balance": balance, "version": account.version},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    created_at=now,
                )
            )
        context.progress(1.0, "账号令牌和余额已更新")
        return {"account_id": account_id, "balance": balance}


def _find_pin_setup(value: Any) -> bool | None:
    if isinstance(value, dict):
        candidate = value.get("is_pin_setup")
        if isinstance(candidate, bool):
            return candidate
        for nested in value.values():
            result = _find_pin_setup(nested)
            if result is not None:
                return result
    if isinstance(value, list):
        for nested in value:
            result = _find_pin_setup(nested)
            if result is not None:
                return result
    return None


class AccountPinStatusHandler:
    """使用账号令牌读取官方 PIN 状态并回写 SQLite。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        codec: SecretCodec,
        adapter: LegacyProtocolAdapter,
    ) -> None:
        self._session_factory = session_factory
        self._codec = codec
        self._adapter = adapter

    def __call__(self, context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
        account_id = str(payload.get("account_id") or "").strip()
        with self._session_factory() as session:
            account = session.get(Account, account_id)
            secret_row = session.get(AccountSecret, account_id)
            if account is None or secret_row is None:
                raise PermanentTaskError("账号或账号密钥记录不存在", code="account_not_found")
            secret = json.loads(
                self._codec.decrypt(
                    secret_row.secret_payload_ciphertext,
                    context=f"account:{account_id}",
                )
            )
            phone = account.phone

        context.acquire_resource("account", account_id)
        proxy = str(secret.get("proxy") or "")
        try:
            client = self._adapter.new_gojek_client(phone, proxy=proxy)
        except ProtocolUnavailableError as exc:
            raise PermanentTaskError(str(exc), code="protocol_unavailable") from exc
        client.auth.access_token = str(secret.get("access_token") or "")
        client.auth.refresh_token = str(secret.get("refresh_token") or "")
        client.user_uuid = str(secret.get("customer_id") or "")
        client.uniqueid = str(secret.get("device_uniqueid") or client.uniqueid)
        client.session_id = str(secret.get("device_session_id") or client.session_id)
        client.device_token = str(secret.get("device_token") or client.device_token)
        if not client.auth.access_token and not client.auth.refresh_token:
            raise PermanentTaskError("账号缺少可用登录令牌", code="account_token_missing")

        context.progress(0.25, "正在刷新账号令牌")
        if client.auth.refresh_token:
            refresh = client.refresh_token()
            if int(refresh.get("status") or 0) not in {200, 201}:
                raise RetryableTaskError("刷新账号令牌失败", code="refresh_failed")
        context.progress(0.6, "正在读取 GoPay PIN 状态")
        profile = client.get_user_profile()
        if int(profile.get("status") or 0) not in {200, 201}:
            raise RetryableTaskError("读取 PIN 状态失败", code="pin_status_failed")
        configured = _find_pin_setup(profile.get("body"))
        if configured is None:
            raise PermanentTaskError("PIN 状态响应中缺少 is_pin_setup", code="pin_status_invalid")

        secret["access_token"] = client.auth.access_token
        secret["refresh_token"] = client.auth.refresh_token
        now = utc_now()
        status = "configured" if configured else "missing"
        with self._session_factory() as session, session.begin():
            account = session.get(Account, account_id)
            secret_row = session.get(AccountSecret, account_id)
            if account is None or secret_row is None:
                raise PermanentTaskError("账号在执行期间被移除", code="account_removed")
            account.pin_setup_status = status
            account.updated_at = now
            account.version += 1
            secret_row.secret_payload_ciphertext = self._codec.encrypt(
                json.dumps(secret, ensure_ascii=False, separators=(",", ":")),
                context=f"account:{account_id}",
            )
            secret_row.updated_at = now
            session.add(
                ChangeLog(
                    event_type="account.updated",
                    resource="account",
                    resource_id=account_id,
                    operation="check_pin",
                    payload_json=json.dumps(
                        {"id": account_id, "pin_setup_status": status, "version": account.version},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    created_at=now,
                )
            )
        context.progress(1.0, "PIN 状态已更新")
        return {"account_id": account_id, "pin_setup_status": status}


class AccountReleaseNumberHandler:
    """将账号对应的短信平台租号标记为已完成。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        adapter: LegacyProtocolAdapter,
        sms_store: SmsSettingsStore,
        hero_sms_store: SmsSettingsStore | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._adapter = adapter
        self._sms_stores = build_sms_stores(sms_store, hero_sms_store)

    def __call__(self, context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
        account_id = str(payload.get("account_id") or "").strip()
        with self._session_factory() as session:
            account = session.get(Account, account_id)
            activation = session.scalar(
                select(SmsActivation)
                .where(
                    SmsActivation.account_id == account_id,
                    SmsActivation.status.in_(("active", "unknown", "rented")),
                )
                .order_by(SmsActivation.updated_at.desc())
                .limit(1)
            )
            if account is None:
                raise PermanentTaskError("账号不存在", code="account_not_found")
            if activation is None:
                raise PermanentTaskError("该账号没有可释放的短信号码", code="sms_activation_missing")
            activation_id = activation.provider_activation_id
            provider = activation.provider
            phone_normalized = account.phone_normalized

        settings = get_sms_settings(self._sms_stores, provider)
        label = provider_label(provider)
        if settings is None or not settings.api_key:
            raise PermanentTaskError(f"{label} API Key 尚未配置", code="sms_settings_missing")
        context.acquire_resource("account", account_id)
        context.acquire_resource("sms", f"{provider}:{activation_id}")
        context.progress(0.35, f"正在向 {label} 提交号码释放")
        try:
            released = call_sms(self._adapter, "sms_done", settings, activation_id)
        except Exception as exc:
            raise RetryableTaskError("释放短信号码时服务暂时不可用", code="sms_release_failed") from exc
        if not released:
            raise RetryableTaskError("短信号码释放未成功", code="sms_release_rejected")

        now = utc_now()
        with self._session_factory() as session, session.begin():
            account = session.get(Account, account_id)
            activation = session.scalar(
                select(SmsActivation).where(
                    SmsActivation.provider == provider,
                    SmsActivation.provider_activation_id == activation_id,
                )
            )
            if account is not None:
                account.sms_activation_status = "completed"
                account.updated_at = now
                account.version += 1
            if activation is not None:
                activation.status = "completed"
                activation.updated_at = now
            phone = session.scalar(
                select(PhoneNumber).where(PhoneNumber.phone_normalized == phone_normalized)
            )
            if phone is not None:
                phone.status = "released"
                phone.updated_at = now
            session.add(
                ChangeLog(
                    event_type="account.updated",
                    resource="account",
                    resource_id=account_id,
                    operation="release_number",
                    payload_json=json.dumps(
                        {"id": account_id, "sms_activation_status": "completed"},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    created_at=now,
                )
            )
        context.progress(1.0, "短信号码已释放")
        return {"account_id": account_id, "sms_activation_status": "completed"}


class SmsActivationCancelHandler:
    """延迟取消尚未绑定账号的短信激活，覆盖平台的早期取消窗口。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        adapter: LegacyProtocolAdapter,
        sms_store: SmsSettingsStore,
        hero_sms_store: SmsSettingsStore | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._adapter = adapter
        self._sms_stores = build_sms_stores(sms_store, hero_sms_store)

    def __call__(self, context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
        provider = str(payload.get("provider") or "smsbower").strip()
        activation_id = str(payload.get("activation_id") or "").strip()
        if not activation_id:
            raise PermanentTaskError("缺少短信激活 ID", code="sms_activation_id_required")
        settings = get_sms_settings(self._sms_stores, provider)
        label = provider_label(provider)
        if settings is None or not settings.api_key:
            raise PermanentTaskError(f"{label} API Key 尚未配置", code="sms_settings_missing")

        context.acquire_resource("sms", f"{provider}:{activation_id}")
        context.progress(0.35, f"正在延迟释放 {label} 未使用号码")
        try:
            cancelled = call_sms(
                self._adapter,
                "sms_cancel",
                settings,
                activation_id,
            )
        except Exception as exc:
            raise RetryableTaskError(
                f"{label} 延迟释放请求暂时失败",
                code="sms_cancel_failed",
            ) from exc
        if not cancelled:
            raise RetryableTaskError(
                f"{label} 暂未接受号码释放请求",
                code="sms_cancel_rejected",
            )

        now = utc_now()
        with self._session_factory() as session, session.begin():
            activation = session.scalar(
                select(SmsActivation).where(
                    SmsActivation.provider == provider,
                    SmsActivation.provider_activation_id == activation_id,
                )
            )
            if activation is not None:
                activation.status = "cancelled"
                activation.updated_at = now
                if activation.phone_number_id:
                    phone = session.get(PhoneNumber, activation.phone_number_id)
                    if phone is not None and activation.account_id is None:
                        phone.status = "released"
                        phone.updated_at = now
        context.progress(1.0, f"{label} 未使用号码已释放")
        return {"provider": provider, "activation_id": activation_id, "cancelled": True}


def _consumed_code_hashes(value: str) -> list[str]:
    try:
        rows = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(rows, list):
        return []
    return list(
        dict.fromkeys(
            str(item).strip().lower()
            for item in rows
            if re.fullmatch(r"[0-9a-fA-F]{64}", str(item).strip())
        )
    )[-50:]


class AccountRefreshSmsCodeHandler:
    """忽略当前旧码，并从同一短信平台激活记录获取下一条验证码。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        adapter: LegacyProtocolAdapter,
        sms_store: SmsSettingsStore,
        hero_sms_store: SmsSettingsStore | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._adapter = adapter
        self._sms_stores = build_sms_stores(sms_store, hero_sms_store)

    def _save_hashes(self, provider: str, activation_id: str, hashes: list[str]) -> None:
        with self._session_factory() as session, session.begin():
            activation = session.scalar(
                select(SmsActivation).where(
                    SmsActivation.provider == provider,
                    SmsActivation.provider_activation_id == activation_id,
                )
            )
            if activation is None:
                raise PermanentTaskError("短信激活记录已被移除", code="sms_activation_removed")
            activation.consumed_code_hashes_json = json.dumps(hashes[-50:], separators=(",", ":"))
            activation.updated_at = utc_now()

    def __call__(self, context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
        account_id = str(payload.get("account_id") or "").strip()
        if not account_id:
            raise PermanentTaskError("缺少 account_id", code="account_id_required")
        with self._session_factory() as session:
            account = session.get(Account, account_id)
            activation = session.scalar(
                select(SmsActivation)
                .where(
                    SmsActivation.account_id == account_id,
                    SmsActivation.status.in_(("active", "unknown", "rented")),
                )
                .order_by(SmsActivation.updated_at.desc())
                .limit(1)
            )
            if account is None:
                raise PermanentTaskError("账号不存在", code="account_not_found")
            if activation is None:
                raise PermanentTaskError(
                    "该账号没有可继续接码的短信平台激活记录",
                    code="sms_activation_missing",
                )
            activation_id = activation.provider_activation_id
            provider = activation.provider
            hashes = _consumed_code_hashes(activation.consumed_code_hashes_json)

        settings = get_sms_settings(self._sms_stores, provider)
        label = provider_label(provider)
        if settings is None or not settings.api_key:
            raise PermanentTaskError(f"{label} API Key 尚未配置", code="sms_settings_missing")
        context.acquire_resource("account", account_id)
        context.acquire_resource("sms", f"{provider}:{activation_id}")

        context.progress(0.15, f"正在读取 {label} 当前激活状态")
        try:
            state, current_code = call_sms(
                self._adapter,
                "sms_status",
                settings,
                activation_id,
            )
        except Exception as exc:
            raise RetryableTaskError(
                f"读取 {label} 激活状态时服务暂时不可用",
                code="sms_status_failed",
            ) from exc
        if state == "cancelled":
            raise PermanentTaskError(f"{label} 激活已经失效或取消", code="sms_activation_cancelled")
        if state == "unknown":
            raise RetryableTaskError(f"{label} 返回了未知激活状态", code="sms_status_unknown")

        if current_code:
            current_hash = hashlib.sha256(current_code.encode()).hexdigest()
            hashes = list(dict.fromkeys([*hashes, current_hash]))[-50:]
            self._save_hashes(provider, activation_id, hashes)

        if state == "code_received":
            context.progress(0.35, "已忽略旧验证码，正在请求下一条验证码")
            try:
                requested = call_sms(
                    self._adapter,
                    "sms_request_another",
                    settings,
                    activation_id,
                )
            except Exception as exc:
                raise RetryableTaskError(
                    "请求下一条验证码时服务暂时不可用",
                    code="sms_retry_failed",
                ) from exc
            if not requested:
                raise RetryableTaskError(
                    f"{label} 尚未接受下一条验证码请求",
                    code="sms_retry_rejected",
                )
        elif state == "waiting_retry":
            context.progress(0.35, f"{label} 已在等待下一条验证码")
        else:
            context.progress(0.35, f"{label} 正在等待最新验证码")

        context.progress(0.55, "正在轮询最新验证码，旧验证码不会再次返回")
        try:
            code = call_sms(
                self._adapter,
                "sms_wait_code",
                settings,
                activation_id,
                timeout=120,
                ignore_code_hashes=set(hashes),
            )
        except Exception as exc:
            raise RetryableTaskError(
                "获取最新验证码时服务暂时不可用",
                code="sms_latest_code_failed",
            ) from exc
        normalized = str(code or "").strip()
        if not normalized:
            raise RetryableTaskError("等待最新验证码超时，请稍后重试", code="sms_latest_code_timeout")
        if not re.fullmatch(r"\d{4,8}", normalized):
            raise PermanentTaskError(f"{label} 返回的验证码格式不正确", code="sms_latest_code_invalid")
        digest = hashlib.sha256(normalized.encode()).hexdigest()
        if digest in hashes:
            raise RetryableTaskError(f"{label} 仍返回旧验证码，请稍后重试", code="sms_code_stale")
        hashes = list(dict.fromkeys([*hashes, digest]))[-50:]
        self._save_hashes(provider, activation_id, hashes)

        now = utc_now()
        with self._session_factory() as session, session.begin():
            account = session.get(Account, account_id)
            if account is not None:
                account.sms_activation_status = "active"
                account.updated_at = now
                account.version += 1
            session.add(
                ChangeLog(
                    event_type="account.updated",
                    resource="account",
                    resource_id=account_id,
                    operation="refresh_sms_code",
                    payload_json=json.dumps(
                        {"id": account_id, "sms_activation_status": "active"},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    created_at=now,
                )
            )
        context.progress(1.0, "最新验证码已安全获取，等待账号页面读取")
        return {
            "account_id": account_id,
            "activation_id": activation_id,
            "provider": provider,
            "code": normalized,
        }
