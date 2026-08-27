"""GoPay 注册、登录、短信接码、OTP 与 PIN 可恢复状态机。"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
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
from gopay_app.services.account_flow_defaults import AccountFlowDefaultsStore
from gopay_app.services.sms_settings import SmsSettings, SmsSettingsStore

from ..context import TaskContext
from ..errors import PermanentTaskError, RetryableTaskError, ReviewTaskError, TaskCancelled
from .business import _find_balance
from .sms_provider import build_sms_stores, call_sms, get_sms_settings, provider_label

_auth_fields = (
    "transaction_id",
    "verification_id",
    "otp_token",
    "otp_length",
    "otp_channel",
    "verification_token",
    "onefa_token",
    "account_id",
    "access_token",
    "refresh_token",
    "twofa_token",
    "twofa_methods",
    "user_registered",
    "methods",
    "pin_otp_auth_token",
    "pin_challenge_id",
    "pin_client_id",
    "pin_token",
)

_names = (
    "Budi Santoso",
    "Adi Pratama",
    "Siti Rahayu",
    "Dewi Lestari",
    "Rizky Ramadhan",
    "Putri Wulandari",
    "Agus Setiawan",
    "Rina Kusuma",
)
_MAX_PROXY_SWITCHES = 4


def _digits(value: object) -> str:
    return "".join(character for character in str(value or "") if character.isdigit())


def _stable_id(namespace: str, value: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"gopay-v2:{namespace}:{value}"))


def _normalize_phone(value: object, country_code: str = "+62") -> tuple[str, str, str]:
    country_digits = _digits(country_code) or "62"
    number = _digits(value)
    if number.startswith(country_digits):
        local = number[len(country_digits) :]
    elif number.startswith("0"):
        local = number[1:]
    else:
        local = number
    if len(local) < 8 or len(local) > 15:
        raise PermanentTaskError("手机号格式不正确", code="phone_invalid")
    return f"+{country_digits}{local}", local, f"+{country_digits}"


def _status(response: object) -> int:
    if not isinstance(response, dict):
        return 0
    try:
        return int(response.get("status") or 0)
    except (TypeError, ValueError):
        return 0


def _response_detail(response: object) -> str:
    if not isinstance(response, dict):
        return "响应格式不正确"
    body = response.get("body")
    if not isinstance(body, dict):
        return str(body or "")[:240]
    candidates: list[str] = []
    for key in ("code", "message", "description", "error"):
        value = body.get(key)
        if isinstance(value, str) and value:
            candidates.append(value)
        elif isinstance(value, dict):
            candidates.extend(str(value.get(name) or "") for name in ("code", "message", "description"))
    errors = body.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        candidates.extend(str(errors[0].get(name) or "") for name in ("code", "message"))
    return " ".join(item for item in candidates if item)[:240]


def _response_error_code(response: object) -> str:
    if not isinstance(response, dict):
        return ""
    body = response.get("body")
    if not isinstance(body, dict):
        return ""
    errors = body.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        return str(errors[0].get("code") or "").strip()[:120]
    return str(body.get("code") or "").strip()[:120]


def _expect(
    response: object,
    label: str,
    *,
    accepted: tuple[int, ...] = (200, 201),
    side_effect: bool = False,
) -> dict[str, Any]:
    code = _status(response)
    if code in accepted and isinstance(response, dict):
        return response
    detail = _response_detail(response)
    message = f"{label}失败：HTTP {code}" + (f" · {detail}" if detail else "")
    error_code = _response_error_code(response)
    rate_limit_text = f"{error_code} {detail}".lower()
    if code == 429 or "ratelimit" in rate_limit_text or "rate_limit" in rate_limit_text:
        raise PermanentTaskError(
            f"{label}被 GoPay 限频：当前代理出口请求过多"
            + (f" · 错误码 {error_code}" if error_code else ""),
            code="gopay_rate_limited",
        )
    if code == 403:
        raise PermanentTaskError(
            f"{label}被 GoPay 风控拒绝：请检查或更换代理出口"
            + (f" · {detail}" if detail else ""),
            code="gopay_proxy_blocked",
        )
    if side_effect and (code == 0 or code >= 500):
        raise ReviewTaskError(message, code="remote_side_effect_unknown")
    if label == "创建 GoPay 账号" and code == 400 and "frs_failure_generic" in detail.lower():
        raise PermanentTaskError(
            "GoPay 风控服务拒绝创建账号：当前号码、设备环境或代理出口未通过风险校验；"
            "OTP 已验证，本次不会自动重复提交创建请求"
            " · 错误码 CO:CUST:frs_failure_generic",
            code="gopay_frs_rejected",
        )
    if code in {0, 408, 425} or code >= 500:
        raise RetryableTaskError(message, code="remote_temporary_error")
    raise PermanentTaskError(message, code="remote_rejected")


def _profile_pin_state(response: object) -> bool | None:
    def visit(value: object) -> bool | None:
        if isinstance(value, dict):
            candidate = value.get("is_pin_setup")
            if isinstance(candidate, bool):
                return candidate
            for nested in value.values():
                found = visit(nested)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for nested in value:
                found = visit(nested)
                if found is not None:
                    return found
        return None

    return visit(response)


def _login_method_names(client: Any) -> list[str]:
    """统一解析 GoPay 登录方式，兼容字符串与对象两种响应形式。"""
    raw_methods = getattr(getattr(client, "auth", None), "methods", []) or []
    values = raw_methods if isinstance(raw_methods, (list, tuple, set)) else [raw_methods]
    methods: list[str] = []
    for value in values:
        if isinstance(value, str):
            candidate = value
        elif isinstance(value, dict):
            candidate = next(
                (
                    str(value.get(key) or "")
                    for key in ("method", "name", "type", "value", "id")
                    if value.get(key)
                ),
                "",
            )
        else:
            candidate = ""
        normalized = candidate.strip().lower()
        if normalized and normalized not in methods:
            methods.append(normalized)
    return methods


def _expect_existing_login_methods(response: object) -> dict[str, Any]:
    """将旧项目中的“用户不存在”分支还原为明确的终止原因。"""
    code = _status(response)
    detail = _response_detail(response)
    lowered = detail.lower()
    if code in {401, 404} and any(
        marker in lowered
        for marker in ("user:not_found", "user:not-found", "user_not_found", "not found", "invalid user")
    ):
        raise PermanentTaskError(
            "GoPay 未找到该手机号，当前号码不是可登录的已有账号"
            + (f"·{detail}" if detail else ""),
            code="remote_account_not_found",
        )
    return _expect(response, "查询登录方式")


def _expect_login_pin(response: object) -> dict[str, Any]:
    """保留旧项目的 PIN 错误语义，不将原 PIN 错误当作可重试网络故障。"""
    code = _status(response)
    if code in {200, 201} and isinstance(response, dict):
        return response
    if code not in {403, 429} and 400 <= code < 500:
        detail = _response_detail(response)
        raise PermanentTaskError(
            "账号已有 PIN，原 PIN 验证失败；请检查原 PIN，如已忘记请先在 GoPay 官方 App 重置"
            + (f"·HTTP {code} · {detail}" if detail else f"·HTTP {code}"),
            code="login_pin_invalid",
        )
    return _expect(response, "原 PIN 验证")


def _signup_created_without_token(response: object) -> bool:
    if _status(response) != 206 or not isinstance(response, dict):
        return False
    body = response.get("body")
    if not isinstance(body, dict):
        return False
    data = body.get("data", body)
    if not isinstance(data, dict):
        return False
    customer = data.get("customer")
    return bool(
        body.get("success") is True
        and isinstance(customer, dict)
        and customer.get("active") is True
        and customer.get("phone_verified") is True
        and not data.get("access_token")
        and not data.get("refresh_token")
    )


def _client_state(client: Any) -> dict[str, Any]:
    auth = {field: getattr(client.auth, field) for field in _auth_fields if hasattr(client.auth, field)}
    return {
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


class AccountFlowHandler:
    """一个任务只处理一个手机号，检查点保存完整协议会话状态。"""

    def __init__(
        self,
        mode: str,
        session_factory: sessionmaker[Session],
        codec: SecretCodec,
        adapter: LegacyProtocolAdapter,
        sms_store: SmsSettingsStore,
        hero_sms_store: SmsSettingsStore | None = None,
    ) -> None:
        self._mode = mode
        self._session_factory = session_factory
        self._codec = codec
        self._adapter = adapter
        self._sms_stores = build_sms_stores(sms_store, hero_sms_store)
        self._defaults_store = AccountFlowDefaultsStore(session_factory, codec)

    def __call__(self, context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
        checkpoint = context.checkpoint()
        try:
            checkpoint = self._prepare(context, payload, checkpoint)
            self._acquire_resources(context, checkpoint)
            client = self._restore_client(checkpoint)
            if self._mode == "register":
                return self._register(context, payload, checkpoint, client)
            return self._login(context, payload, checkpoint, client)
        except ProtocolUnavailableError as exc:
            if checkpoint.get("activation_id"):
                self._cancel_unused_activation(checkpoint, context)
            raise PermanentTaskError(str(exc), code="protocol_unavailable") from exc
        except PermanentTaskError as exc:
            terminal_rate_limit_error: PermanentTaskError | None = None
            if exc.code == "gopay_rate_limited":
                if self._switch_proxy_after_rate_limit(
                    context,
                    payload,
                    checkpoint,
                    str(exc),
                ):
                    return self(context, payload)
                switch_count = int(checkpoint.get("proxy_switch_count") or 0)
                attempted_count = len(
                    {
                        str(value)
                        for value in checkpoint.get("attempted_proxy_hashes") or []
                        if value
                    }
                )
                terminal_rate_limit_error = PermanentTaskError(
                    f"{exc}；已尝试 {max(1, attempted_count)} 个代理出口并切换 {switch_count} 次，"
                    "当前区域代理池已无新的可用出口，本次注册已停止，不再重复请求",
                    code="gopay_rate_limit_exhausted",
                )
            release_status = ""
            if checkpoint.get("activation_id"):
                release_status = self._cancel_unused_activation(checkpoint, context)
            if exc.code == "remote_account_not_found" and release_status:
                label = provider_label(self._checkpoint_sms_provider(checkpoint))
                release_message = (
                    "号码已释放"
                    if release_status == "cancelled"
                    else "号码已进入持久化延迟释放队列"
                )
                raise PermanentTaskError(
                    f"{exc}；{label} {release_message}",
                    code=exc.code,
                ) from exc
            if terminal_rate_limit_error is not None:
                raise terminal_rate_limit_error from exc
            raise
        except RetryableTaskError:
            task = context.repository.get_task(context.task_id)
            if checkpoint.get("activation_id") and task.attempt >= task.max_attempts:
                self._cancel_unused_activation(checkpoint, context)
            raise
        except TaskCancelled:
            if checkpoint.get("activation_id"):
                self._cancel_unused_activation(checkpoint, context)
            raise

    def _prepare(
        self,
        context: TaskContext,
        payload: dict[str, Any],
        checkpoint: dict[str, Any],
    ) -> dict[str, Any]:
        if checkpoint.get("phone"):
            return checkpoint

        source = str(payload.get("phone_source") or "manual").strip().lower()
        if source != "manual" and source not in self._sms_stores:
            raise PermanentTaskError("号码来源不正确", code="phone_source_invalid")
        pin = str(payload.get("pin") or "").strip()
        if not re.fullmatch(r"\d{6}", pin):
            raise PermanentTaskError("PIN 必须是 6 位数字", code="pin_invalid")
        country_code = str(payload.get("country_code") or "+62").strip()
        proxy = str(payload.get("proxy") or "").strip()
        proxy_preflight: dict[str, Any] = {}
        activation_id = ""
        consumed_code_hashes: list[str] = []
        sms_settings: SmsSettings | None = None

        if source in self._sms_stores:
            sms_settings = self._require_sms_settings(source)
            label = provider_label(source)
            if proxy:
                context.progress(0.01, "自动取号前正在检测代理出口")
                probe = self._probe_proxy(context, proxy, progress=0.01, stage="取号前")
                if not probe.get("ok"):
                    raise RetryableTaskError(
                        f"取号前代理出口不可用（{self._proxy_probe_detail(probe)}），"
                        f"未购买 {label} 号码",
                        code="proxy_unavailable",
                    )
                proxy_preflight = {
                    "ok": True,
                    "ip": str(probe.get("ip") or "")[:80],
                }
                context.progress(0.015, f"取号前代理预检通过：出口 IP {probe.get('ip') or '-'}")
            action = "已有账号登录" if self._mode == "login" else "注册"
            context.progress(0.02, f"正在向 {label} 申请{action}手机号")
            try:
                rented_phone, activation_id = call_sms(
                    self._adapter,
                    "sms_get_number",
                    sms_settings,
                )
            except Exception as exc:
                raise RetryableTaskError(f"{label} 取号暂时失败", code="sms_number_failed") from exc
            if not rented_phone or not activation_id:
                raise RetryableTaskError(f"{label} 暂无可用号码", code="sms_number_unavailable")
            phone, local, country_code = _normalize_phone(rented_phone, country_code)
        else:
            phone, local, country_code = _normalize_phone(payload.get("phone"), country_code)

        normalized = _digits(phone)
        existing_account_id = ""
        with self._session_factory() as session:
            existing = session.scalar(select(Account).where(Account.phone_normalized == normalized))
            if existing is not None:
                existing_account_id = existing.id
            if self._mode == "register" and existing is not None:
                raise PermanentTaskError("该手机号已在本地账号库，请使用已有账号登录", code="account_exists")

        phone_id = self._upsert_phone(phone, normalized, source)
        if activation_id:
            self._upsert_activation(source, activation_id, phone_id)
        checkpoint = {
            "version": 1,
            "mode": self._mode,
            "phase": "prepared",
            "phone_source": source,
            "sms_provider": source if source in self._sms_stores else "",
            "phone": phone,
            "phone_normalized": normalized,
            "local_phone": local,
            "country_code": country_code,
            "phone_id": phone_id,
            "local_account_id": existing_account_id,
            "activation_id": activation_id,
            "proxy": proxy,
            "proxy_region": str(payload.get("proxy_region") or "").strip().upper(),
            "proxy_preflight": proxy_preflight,
            "rate_limited_egress_ips": [],
            "proxy_switch_count": 0,
            "attempted_proxy_hashes": (
                [self._proxy_hash(proxy)] if proxy else []
            ),
            "consumed_code_hashes": consumed_code_hashes,
            "client": {},
        }
        context.save_checkpoint(checkpoint)
        context.progress(0.05, f"手机号已准备：{phone}")
        return checkpoint

    def _acquire_resources(self, context: TaskContext, checkpoint: dict[str, Any]) -> None:
        context.acquire_resource("phone", str(checkpoint["phone_normalized"]), ttl_seconds=600)
        account_id = str(checkpoint.get("local_account_id") or "")
        if account_id:
            context.acquire_resource("account", account_id, ttl_seconds=600)
        activation_id = str(checkpoint.get("activation_id") or "")
        if activation_id:
            provider = str(checkpoint.get("sms_provider") or checkpoint.get("phone_source") or "smsbower")
            context.acquire_resource("sms", f"{provider}:{activation_id}", ttl_seconds=600)

    def _restore_client(self, checkpoint: dict[str, Any]):
        client = self._adapter.new_gojek_client(
            str(checkpoint["phone"]),
            proxy=str(checkpoint.get("proxy") or ""),
        )
        saved = checkpoint.get("client")
        if not isinstance(saved, dict):
            return client
        auth = saved.get("auth")
        if isinstance(auth, dict):
            for field in _auth_fields:
                if field in auth and hasattr(client.auth, field):
                    setattr(client.auth, field, auth[field])
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
            if saved.get(field) is not None and hasattr(client, field):
                setattr(client, field, saved[field])
        return client

    def _proxy_hash(self, proxy: str) -> str:
        return self._codec.lookup_hash(proxy, namespace="account-flow:proxy-switch")

    def _switch_proxy_after_rate_limit(
        self,
        context: TaskContext,
        payload: dict[str, Any],
        checkpoint: dict[str, Any],
        reason: str,
    ) -> bool:
        """仅在尚未成功发送 OTP 的安全阶段切换代理。"""
        current_phase = str(checkpoint.get("phase") or "")
        if current_phase not in {"prepared", "signup_otp_ready", "signup_otp_requesting"}:
            return False
        switch_count = int(checkpoint.get("proxy_switch_count") or 0)
        if switch_count >= _MAX_PROXY_SWITCHES:
            return False
        defaults = self._defaults_store.get()
        region = str(
            checkpoint.get("proxy_region")
            or payload.get("proxy_region")
            or defaults.default_proxy_region
            or ""
        ).strip().upper()
        candidates = [
            entry.url
            for entry in defaults.proxy_pool
            if not region or entry.region == region
        ]
        if not candidates:
            return False

        current = str(checkpoint.get("proxy") or "")
        attempted = {
            str(value)
            for value in checkpoint.get("attempted_proxy_hashes") or []
            if value
        }
        if current:
            attempted.add(self._proxy_hash(current))
        current_index = next(
            (index for index, candidate in enumerate(candidates) if candidate == current),
            -1,
        )
        ordered = (
            candidates[current_index + 1 :] + candidates[: current_index + 1]
            if current_index >= 0
            else candidates
        )
        next_proxy = next(
            (candidate for candidate in ordered if self._proxy_hash(candidate) not in attempted),
            "",
        )
        if not next_proxy:
            return False

        attempted.add(self._proxy_hash(next_proxy))
        rate_limited_ips = {
            str(value)
            for value in checkpoint.get("rate_limited_egress_ips") or []
            if value
        }
        cached_probe = checkpoint.get("proxy_preflight")
        if isinstance(cached_probe, dict) and cached_probe.get("ip"):
            rate_limited_ips.add(str(cached_probe["ip"]))
        retry_otp_only = current_phase in {"signup_otp_ready", "signup_otp_requesting"}
        checkpoint.update(
            {
                "phase": "signup_otp_ready" if retry_otp_only else "prepared",
                "proxy": next_proxy,
                "proxy_preflight": {},
                "proxy_switch_count": switch_count + 1,
                "attempted_proxy_hashes": sorted(attempted),
                "rate_limited_egress_ips": sorted(rate_limited_ips),
            }
        )
        if not retry_otp_only:
            checkpoint["client"] = {}
        context.save_checkpoint(checkpoint)
        progress = max(0.05, context.repository.get_task(context.task_id).progress)
        if retry_otp_only:
            context.progress(
                progress,
                f"{reason}；已自动切换同区域代理，第 {switch_count + 1}/{_MAX_PROXY_SWITCHES} 次，"
                "保留原设备会话并直接重试注册 OTP，不重复执行 Support SDK 和账号检测",
            )
        else:
            context.progress(
                progress,
                f"{reason}；已自动切换同区域代理，第 {switch_count + 1}/{_MAX_PROXY_SWITCHES} 次，"
                "正在重新执行尚未完成的注册前置检测",
            )
        return True

    def _checkpoint(
        self,
        context: TaskContext,
        checkpoint: dict[str, Any],
        client: Any,
        phase: str,
    ) -> dict[str, Any]:
        checkpoint["phase"] = phase
        checkpoint["client"] = _client_state(client)
        checkpoint.pop("input_type", None)
        checkpoint.pop("otp_purpose", None)
        context.save_checkpoint(checkpoint)
        return checkpoint

    def _pause(self, context: TaskContext, seconds: float = 2.0) -> None:
        pause = getattr(self._adapter, "account_request_pause", None)
        if callable(pause):
            context.ensure_active()
            pause(seconds)
            context.ensure_active()

    def _registration_warmup(self, context: TaskContext, client: Any) -> None:
        """复用旧项目注册前的 Support SDK 启动顺序，失败不改变注册结论。"""
        for label, method_name in (
            ("Support SDK initiate 启动", "support_customer_initiate"),
            ("Support SDK actions 启动", "support_customer_actions"),
        ):
            self._optional_registration_step(context, client, label, method_name, 0.065)

    def _optional_registration_step(
        self,
        context: TaskContext,
        client: Any,
        label: str,
        method_name: str,
        progress: float,
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """执行附加初始化请求；记录真实结果，但不把附加步骤当成注册失败。"""
        method = getattr(client, method_name, None)
        if not callable(method):
            context.progress(progress, f"{label}接口在当前协议版本中不存在，已跳过")
            return {"status": 0, "body": {}}
        context.progress(progress, f"{label}请求中")
        try:
            response = method(*args, **kwargs)
        except Exception:
            context.progress(progress, f"{label}请求异常，已跳过且不影响核心注册流程")
            return {"status": 0, "body": {}}
        status = _status(response)
        if status in {200, 201, 204}:
            context.progress(progress, f"{label}请求完成（HTTP {status}），继续后续流程")
        else:
            context.progress(progress, f"{label}返回 HTTP {status}，已跳过且不影响核心注册流程")
        return response if isinstance(response, dict) else {"status": 0, "body": {}}

    def _pre_pin_warmup(self, context: TaskContext, client: Any) -> None:
        """按旧项目顺序执行 token 后、PIN 前的真机初始化链路。"""
        steps: tuple[tuple[str, str, tuple[Any, ...], dict[str, Any]], ...] = (
            ("Support SDK initiate 初始化", "support_customer_initiate", (), {}),
            ("Support SDK initiate 短包补发", "support_customer_initiate", (), {"prefer_shortest": True}),
            ("Support SDK actions 初始化", "support_customer_actions", (), {}),
            ("公开实验配置初始化", "litmus_public_experiments", (), {}),
            ("Gojek 用户资料初始化", "gojek_customer_profile", (), {}),
            ("登录态实验配置初始化", "litmus_experiments", (), {}),
            ("聊天资料初始化", "chat_profile", (), {}),
            ("消息通道令牌初始化", "courier_token", (), {}),
            ("金融通道令牌初始化", "gofin_token", (), {}),
            ("支付方式资料初始化", "gopay_get_profiles", (), {}),
            ("GoPay 首页初始化", "gopay_home_v3", (), {}),
            ("节日礼包资源初始化", "festivals_assets", (), {}),
            ("应用条款与隐私授权同步", "accept_signup_consents", (), {}),
            ("支付方式余额初始化", "gopay_get_balances", (), {}),
            ("用户资料刷新", "get_user_profile", (), {}),
            ("消息角标初始化", "red_badges", (), {}),
            ("交叉推荐配置初始化", "cross_sells", (), {}),
            ("实名认证状态初始化", "kyc_status", (), {}),
            ("先享后付资料初始化", "paylater_profile", (), {}),
            ("钱包余额组件初始化", "wallet_card_balance", (), {}),
            ("钱包卡片组件初始化", "wallet_card_widget", (), {}),
            ("推送令牌绑定", "update_push_token", (), {}),
            ("首页安全状态刷新", "security_meter", ("gopay_home",), {}),
            ("账号安全状态刷新", "security_meter", ("account_safety_home",), {}),
            ("安全中心状态刷新", "security_meter", ("security_meter",), {}),
        )
        context.progress(0.47, "开始执行 PIN 前附加初始化链路；其结果不改变核心注册结论")
        for label, method_name, args, kwargs in steps:
            self._optional_registration_step(context, client, label, method_name, 0.48, *args, **kwargs)

    def _require_login_methods(self, client: Any, response: object) -> list[str]:
        _expect_existing_login_methods(response)
        verification_id = str(getattr(client.auth, "verification_id", "") or "").strip()
        if not verification_id:
            raise PermanentTaskError(
                "查询登录方式成功，但 GoPay 未返回 verification_id，已停止后续验证",
                code="login_verification_id_missing",
            )
        methods = _login_method_names(client)
        if not methods:
            raise PermanentTaskError(
                "查询登录方式成功，但 GoPay 未返回可用登录方式",
                code="login_methods_missing",
            )
        if "goto_pin" not in methods and "otp_sms" not in methods:
            raise PermanentTaskError(
                f"GoPay 返回的登录方式暂未接入：{', '.join(methods)}",
                code="login_method_unsupported",
            )
        return methods

    def _require_login_artifact(self, client: Any, field: str, message: str, code: str) -> str:
        value = str(getattr(client.auth, field, "") or "").strip()
        if not value:
            raise PermanentTaskError(message, code=code)
        return value

    def _prepare_sms_for_next_otp(
        self,
        context: TaskContext,
        checkpoint: dict[str, Any],
        purpose: str,
    ) -> None:
        """复用号码时先清理旧码，并确认当前平台已进入接收下一条短信的状态。"""
        activation_id = str(checkpoint.get("activation_id") or "")
        if not activation_id:
            return
        if not any(
            callable(getattr(self._adapter, name, None))
            for name in ("sms_status_for", "sms_status")
        ):
            return
        provider = self._checkpoint_sms_provider(checkpoint)
        settings = self._require_sms_settings(provider)
        label = provider_label(provider)
        progress = max(0.05, context.repository.get_task(context.task_id).progress)
        context.progress(progress, f"正在确认 {label} 可接收{purpose}新验证码")
        try:
            state, current_code = call_sms(
                self._adapter,
                "sms_status",
                settings,
                activation_id,
            )
        except Exception:
            context.progress(progress, f"{label} 激活状态暂未读取到，继续执行 GoPay 验证")
            return
        if state == "cancelled":
            raise PermanentTaskError(
                f"{label} 激活已取消，该号码不再接收新验证码",
                code="sms_activation_cancelled",
            )

        stale_code = str(current_code or "").strip()
        if stale_code:
            self._record_consumed_code(checkpoint, stale_code)
            context.progress(progress, f"已识别并忽略 {label} 中的旧验证码")

        if state == "code_received":
            context.progress(progress, f"正在通知 {label} 接收下一条新验证码")
            try:
                ready = bool(
                    call_sms(
                        self._adapter,
                        "sms_request_another",
                        settings,
                        activation_id,
                    )
                )
            except Exception as exc:
                raise RetryableTaskError(
                    f"{label} 准备下一条验证码时请求失败",
                    code="sms_retry_prepare_failed",
                ) from exc
            if not ready:
                raise PermanentTaskError(
                    f"{label} 尚未接受下一条验证码请求，本次未向 GoPay 重复发送 OTP",
                    code="sms_retry_not_ready",
                )
            checkpoint["sms_retry_ready"] = True
            context.save_checkpoint(checkpoint)
            context.progress(progress, f"{label} 已准备接收{purpose}新验证码")
        elif state in {"waiting_code", "waiting_retry"}:
            checkpoint["sms_retry_ready"] = True
            context.save_checkpoint(checkpoint)
            context.progress(progress, f"{label} 已处于{purpose}新验证码等待状态")

    def _register(
        self,
        context: TaskContext,
        payload: dict[str, Any],
        checkpoint: dict[str, Any],
        client: Any,
    ) -> dict[str, Any]:
        phase = str(checkpoint.get("phase") or "prepared")
        pin = str(payload.get("pin") or "")

        if phase == "prepared":
            self._preflight(context, checkpoint)
            self._registration_warmup(context, client)
            self._pause(context)
            context.progress(0.08, "Support SDK 启动步骤已处理，开始检测手机号是否已有 GoPay 账号")
            methods = client.get_login_methods(checkpoint["country_code"], checkpoint["local_phone"])
            methods_status = _status(methods)
            detail = _response_detail(methods).lower()
            context.progress(0.09, f"手机号账号检测返回 HTTP {methods_status}，继续判断注册分支")
            if methods_status in {200, 201}:
                raise PermanentTaskError("号码已注册，不能作为新号注册", code="already_registered")
            if methods_status not in {401, 404} and "not_found" not in detail and "not found" not in detail:
                _expect(methods, "账号注册状态检测")
            self._checkpoint(context, checkpoint, client, "signup_otp_ready")
            phase = "signup_otp_ready"

        if phase == "signup_otp_ready":
            cached_probe = checkpoint.get("proxy_preflight")
            if not isinstance(cached_probe, dict) or not cached_probe.get("ok"):
                cached_probe = self._preflight(context, checkpoint)
            egress_ip = str((cached_probe or {}).get("ip") or "")
            rate_limited_ips = {
                str(value)
                for value in checkpoint.get("rate_limited_egress_ips") or []
                if value
            }
            if egress_ip and egress_ip in rate_limited_ips:
                raise PermanentTaskError(
                    f"新代理仍使用已被限频的出口 IP {egress_ip}，跳过该代理且不发送注册 OTP",
                    code="gopay_rate_limited",
                )
            self._pause(context)
            checkpoint["signup_otp_request_count"] = int(
                checkpoint.get("signup_otp_request_count") or 0
            ) + 1
            checkpoint["phase"] = "signup_otp_requesting"
            context.save_checkpoint(checkpoint)
            context.progress(0.12, "正在发送注册 OTP")
            _expect(
                client.signup_request_otp(checkpoint["phone"], checkpoint["country_code"]), "注册 OTP 申请"
            )
            self._checkpoint(context, checkpoint, client, "signup_otp_sent")
            phase = "signup_otp_sent"

        if phase == "signup_otp_sent":
            otp = self._obtain_otp(context, checkpoint, "注册")
            self._pause(context)
            context.progress(0.2, "正在验证注册 OTP")
            _expect(client.signup_verify_otp(otp, checkpoint["phone"]), "注册 OTP 验证")
            self._checkpoint(context, checkpoint, client, "signup_otp_verified")
            phase = "signup_otp_verified"

        if phase == "signup_otp_verified":
            self._pause(context)
            context.progress(0.28, "正在创建 GoPay 账号")
            name_index = int(hashlib.sha256(checkpoint["phone"].encode()).hexdigest()[:8], 16)
            try:
                response = client.signup_create_account(
                    name=_names[name_index % len(_names)],
                    phone=checkpoint["phone"],
                    email="",
                    country=_digits(checkpoint["country_code"]),
                )
            except Exception as exc:
                raise ReviewTaskError(
                    "注册 OTP 已验证，但创建 GoPay 账号时代理连接超时或远端结果未知；"
                    "本次不会自动重复提交创建请求，请保留号码并复核账号状态",
                    code="account_create_result_unknown",
                ) from exc
            if _signup_created_without_token(response):
                checkpoint["signup_missing_token"] = True
            else:
                _expect(response, "创建 GoPay 账号", side_effect=True)
            self._checkpoint(context, checkpoint, client, "account_created")
            phase = "account_created"

        if phase == "account_created":
            if getattr(client.auth, "refresh_token", ""):
                context.progress(0.38, "正在刷新账号令牌")
                refresh = client.refresh_token()
                if _status(refresh) not in {200, 201} and not getattr(client.auth, "access_token", ""):
                    _expect(refresh, "刷新账号令牌")
            if not getattr(client.auth, "access_token", "") and checkpoint.get("signup_missing_token"):
                context.progress(0.4, "账号已经创建，正在通过登录流程补充令牌")
                methods = client.get_login_methods(
                    checkpoint["country_code"], checkpoint["local_phone"]
                )
                login_methods = self._require_login_methods(client, methods)
                has_pin = "goto_pin" in login_methods
                checkpoint["signup_relogin_has_pin"] = has_pin
                if has_pin:
                    _expect(
                        client.initiate_otp(
                            checkpoint["country_code"],
                            checkpoint["local_phone"],
                            method="goto_pin",
                            flow="login_1fa",
                        ),
                        "注册后创建 PIN 登录 challenge",
                    )
                    self._require_login_artifact(
                        client,
                        "pin_challenge_id",
                        "注册后创建 PIN 登录 challenge 成功，但 GoPay 未返回 challenge_id",
                        "login_pin_challenge_missing",
                    )
                    _expect_login_pin(client.login_pin_verify(pin))
                    _expect(client.verify_pin_via_cvs(), "注册后换取 PIN 登录令牌")
                    self._checkpoint(context, checkpoint, client, "signup_relogin_1fa_verified")
                    phase = "signup_relogin_1fa_verified"
                else:
                    self._prepare_sms_for_next_otp(context, checkpoint, "注册后登录一阶段")
                    client.auth.otp_channel = "otp_sms"
                    _expect(
                        client.initiate_otp(
                            checkpoint["country_code"],
                            checkpoint["local_phone"],
                            method="otp_sms",
                            flow="login_1fa",
                        ),
                        "注册后登录 OTP 申请",
                    )
                    self._require_login_artifact(
                        client,
                        "otp_token",
                        "注册后登录 OTP 请求已返回成功，但 GoPay 未返回 otp_token",
                        "login_otp_token_missing",
                    )
                    self._checkpoint(context, checkpoint, client, "signup_relogin_1fa_otp_sent")
                    phase = "signup_relogin_1fa_otp_sent"
            elif not getattr(client.auth, "access_token", ""):
                raise ReviewTaskError(
                    "账号创建成功但未取得可用令牌，请人工复核后重新登录",
                    code="account_created_without_token",
                )

        if phase == "signup_relogin_1fa_otp_sent":
            otp = self._obtain_otp(context, checkpoint, "注册后登录")
            context.progress(0.42, "正在验证注册后登录验证码")
            _expect(client.verify_otp(otp, flow="login_1fa"), "注册后登录 OTP 验证")
            self._checkpoint(context, checkpoint, client, "signup_relogin_1fa_verified")
            phase = "signup_relogin_1fa_verified"

        if phase == "signup_relogin_1fa_verified":
            _expect(client.get_account_list(), "注册后读取账号列表")
            token = client.issue_token(grant_type="cvs", token_value=client.auth.onefa_token)
            if _status(token) in {200, 201}:
                self._checkpoint(context, checkpoint, client, "signup_relogin_authenticated")
                phase = "signup_relogin_authenticated"
            elif _status(token) == 403 and getattr(client.auth, "twofa_token", ""):
                client.auth.otp_channel = "otp_sms"
                self._prepare_sms_for_next_otp(context, checkpoint, "注册后登录二阶段")
                _expect(
                    client.initiate_otp(
                        checkpoint["country_code"],
                        checkpoint["local_phone"],
                        method="otp_sms",
                        flow="login_2fa",
                    ),
                    "注册后登录二阶段 OTP 申请",
                )
                self._require_login_artifact(
                    client,
                    "otp_token",
                    "注册后登录二阶段 OTP 请求已返回成功，但 GoPay 未返回 otp_token",
                    "login_otp_token_missing",
                )
                self._checkpoint(context, checkpoint, client, "signup_relogin_2fa_otp_sent")
                phase = "signup_relogin_2fa_otp_sent"
            else:
                _expect(token, "注册后申请登录令牌")

        if phase == "signup_relogin_2fa_otp_sent":
            otp = self._obtain_otp(context, checkpoint, "注册后登录二阶段")
            _expect(client.verify_otp(otp, flow="login_2fa"), "注册后二阶段 OTP 验证")
            _expect(
                client.issue_token(grant_type="challenge", token_value=client.auth.twofa_token),
                "注册后换取最终登录令牌",
            )
            self._checkpoint(context, checkpoint, client, "signup_relogin_authenticated")
            phase = "signup_relogin_authenticated"

        if phase == "signup_relogin_authenticated":
            checkpoint["signup_missing_token"] = False
            self._checkpoint(context, checkpoint, client, "account_created")
            phase = "account_created"

        if phase == "account_created":
            if not getattr(client.auth, "access_token", ""):
                raise ReviewTaskError(
                    "注册后登录仍未取得可用令牌，请人工复核",
                    code="account_created_without_token",
                )
            context.progress(0.46, "正在初始化 GoPay 钱包")
            _expect(client.gopay_init(), "初始化 GoPay 钱包")
            self._checkpoint(context, checkpoint, client, "wallet_initialized")
            phase = "wallet_initialized"

        if phase == "wallet_initialized":
            self._pre_pin_warmup(context, client)
            self._checkpoint(context, checkpoint, client, "pre_pin_warmup_done")
            phase = "pre_pin_warmup_done"

        if phase == "pre_pin_warmup_done":
            profile = client.get_user_profile()
            _expect(profile, "读取 PIN 状态")
            pin_state = _profile_pin_state(profile)
            if pin_state is None:
                raise RetryableTaskError("PIN 状态响应格式发生变化", code="pin_state_unknown")
            if pin_state:
                checkpoint["pin_configured"] = True
                self._checkpoint(context, checkpoint, client, "pin_ready")
                phase = "pin_ready"
            else:
                self._request_pin_otp(context, checkpoint, client, pin)
                phase = "pin_otp_sent"

        if phase == "pin_otp_sent":
            otp = self._obtain_otp(context, checkpoint, "PIN 设置")
            context.progress(0.68, "正在验证 PIN OTP")
            _expect(client.pin_verify_otp(otp), "PIN OTP 验证")
            context.progress(0.75, "正在设置 6 位 PIN")
            _expect(client.pin_setup(pin), "PIN 设置", side_effect=True)
            checkpoint["pin_verified"] = True
            checkpoint["pin_configured"] = True
            checkpoint["pin_set_now"] = True
            self._checkpoint(context, checkpoint, client, "pin_ready")
            phase = "pin_ready"

        if phase not in {"pin_ready", "account_persisted", "post_registration_queued"}:
            raise ReviewTaskError("注册检查点状态需要人工复核", code="checkpoint_unknown")
        persisted_pin = pin if checkpoint.get("pin_set_now") else ""
        return self._finish_registration(context, payload, checkpoint, client, persisted_pin)

    def _login(
        self,
        context: TaskContext,
        payload: dict[str, Any],
        checkpoint: dict[str, Any],
        client: Any,
    ) -> dict[str, Any]:
        phase = str(checkpoint.get("phase") or "prepared")
        pin = str(payload.get("pin") or "")
        change_pin = bool(payload.get("change_pin"))
        new_pin = str(payload.get("new_pin") or "").strip()
        if change_pin and (not re.fullmatch(r"\d{6}", new_pin) or new_pin == pin):
            raise PermanentTaskError("新 PIN 必须是不同的 6 位数字", code="new_pin_invalid")

        if phase == "prepared":
            self._preflight(context, checkpoint)
            context.progress(
                0.08,
                f"开始真实请求 GoPay：{checkpoint['phone']} "
                f"country_code={checkpoint['country_code']}",
            )
            context.progress(0.09, "开始已有账号登录：自动检测 PIN 状态")
            self._pause(context, 1.0)
            context.progress(0.1, "登录步骤 1/10：查询账号登录方式")
            methods_response = client.get_login_methods(
                checkpoint["country_code"], checkpoint["local_phone"]
            )
            login_methods = self._require_login_methods(client, methods_response)
            has_pin = "goto_pin" in login_methods
            checkpoint["login_methods_has_pin"] = has_pin
            checkpoint["login_methods"] = login_methods
            method_label = "PIN + 短信 OTP" if has_pin else "短信 OTP"
            context.progress(0.13, f"账号登录方式已确认：{method_label}")
            if has_pin:
                context.progress(0.16, "登录步骤 2/10：创建 PIN 验证 challenge")
                _expect(
                    client.initiate_otp(
                        checkpoint["country_code"],
                        checkpoint["local_phone"],
                        method="goto_pin",
                        flow="login_1fa",
                    ),
                    "创建 PIN 登录 challenge",
                )
                self._require_login_artifact(
                    client,
                    "pin_challenge_id",
                    "创建 PIN 验证 challenge 成功，但 GoPay 未返回 challenge_id",
                    "login_pin_challenge_missing",
                )
                context.progress(0.19, "登录步骤 3/10：提交原 PIN 验证")
                _expect_login_pin(client.login_pin_verify(pin))
                context.progress(0.22, "登录步骤 4/10：换取 PIN 验证令牌")
                _expect(client.verify_pin_via_cvs(), "换取 PIN 登录令牌")
                checkpoint["pin_verified"] = True
                self._checkpoint(context, checkpoint, client, "login_1fa_verified")
                phase = "login_1fa_verified"
            else:
                context.progress(0.16, "登录步骤 2/10：账号未要求 PIN，发送一阶段 OTP")
                self._prepare_sms_for_next_otp(context, checkpoint, "登录一阶段")
                client.auth.otp_channel = "otp_sms"
                _expect(
                    client.initiate_otp(
                        checkpoint["country_code"],
                        checkpoint["local_phone"],
                        method="otp_sms",
                        flow="login_1fa",
                    ),
                    "登录一阶段 OTP 申请",
                )
                self._require_login_artifact(
                    client,
                    "otp_token",
                    "登录一阶段 OTP 请求已返回成功，但 GoPay 未返回 otp_token",
                    "login_otp_token_missing",
                )
                self._checkpoint(context, checkpoint, client, "login_1fa_otp_sent")
                context.progress(0.2, "登录 OTP 已发送，等待新的验证码")
                phase = "login_1fa_otp_sent"

        if phase == "login_1fa_otp_sent":
            otp = self._obtain_otp(context, checkpoint, "登录一阶段")
            context.progress(0.25, "登录 OTP 已提交，开始验证")
            _expect(client.verify_otp(otp, flow="login_1fa"), "登录一阶段 OTP 验证")
            self._checkpoint(context, checkpoint, client, "login_1fa_verified")
            phase = "login_1fa_verified"

        if phase == "login_1fa_verified":
            context.progress(0.34, "登录步骤 5/10：读取账号列表")
            _expect(client.get_account_list(), "读取远端账号列表")
            context.progress(0.38, "登录步骤 6/10：申请登录 token")
            token = client.issue_token(grant_type="cvs", token_value=client.auth.onefa_token)
            if _status(token) in {200, 201}:
                self._checkpoint(context, checkpoint, client, "authenticated")
                phase = "authenticated"
            elif _status(token) == 403 and getattr(client.auth, "twofa_token", ""):
                client.auth.otp_channel = "otp_sms"
                context.progress(0.42, "登录步骤 7/10：发送登录二阶段 OTP")
                self._prepare_sms_for_next_otp(context, checkpoint, "登录二阶段")
                _expect(
                    client.initiate_otp(
                        checkpoint["country_code"],
                        checkpoint["local_phone"],
                        method="otp_sms",
                        flow="login_2fa",
                    ),
                    "登录二阶段 OTP 申请",
                )
                self._require_login_artifact(
                    client,
                    "otp_token",
                    "登录二阶段 OTP 请求已返回成功，但 GoPay 未返回 otp_token",
                    "login_otp_token_missing",
                )
                self._checkpoint(context, checkpoint, client, "login_2fa_otp_sent")
                phase = "login_2fa_otp_sent"
            else:
                _expect(token, "申请登录令牌")

        if phase == "login_2fa_otp_sent":
            context.progress(0.46, "登录步骤 8/10：等待登录二阶段 OTP")
            otp = self._obtain_otp(context, checkpoint, "登录二阶段")
            context.progress(0.5, "登录步骤 9/10：验证登录二阶段 OTP")
            _expect(client.verify_otp(otp, flow="login_2fa"), "登录二阶段 OTP 验证")
            context.progress(0.54, "登录步骤 10/10：换取最终访问令牌")
            _expect(
                client.issue_token(grant_type="challenge", token_value=client.auth.twofa_token),
                "换取最终登录令牌",
            )
            self._checkpoint(context, checkpoint, client, "authenticated")
            phase = "authenticated"

        if phase == "authenticated":
            context.progress(0.6, "登录完成，正在确认 PIN 状态")
            _expect(client.gopay_init(), "初始化 GoPay 钱包")
            profile = client.get_user_profile()
            _expect(profile, "读取 PIN 状态")
            pin_state = _profile_pin_state(profile)
            if pin_state is None:
                raise RetryableTaskError("PIN 状态响应格式发生变化", code="pin_state_unknown")
            if not pin_state:
                setup_pin = new_pin if change_pin else pin
                context.progress(0.62, "已检测到账号没有 PIN，登录成功后开始设置新 PIN")
                self._request_pin_otp(context, checkpoint, client, setup_pin)
                checkpoint["setup_pin"] = setup_pin
                context.save_checkpoint(checkpoint)
                phase = "pin_otp_sent"
            else:
                checkpoint["pin_configured"] = True
                if checkpoint.get("login_methods_has_pin"):
                    context.progress(0.62, "账号资料确认已设置 PIN，本次登录已验证原 PIN")
                else:
                    context.progress(
                        0.62,
                        "本次为 OTP 登录；账号资料确认已设置 PIN，但本次未验证原 PIN",
                    )
                if change_pin:
                    context.progress(0.64, "登录已经成功，正在先保存账号会话")
                    self._persist_account(
                        payload,
                        checkpoint,
                        client,
                        pin if checkpoint.get("pin_verified") else "",
                        0,
                    )
                    if not checkpoint.get("login_methods_has_pin"):
                        context.progress(
                            0.66,
                            "本次通过短信 OTP 登录，正在通过 PIN 修改 challenge 验证原 PIN",
                        )
                    self._change_pin(context, client, pin, new_pin)
                    checkpoint["pin_changed_now"] = True
                    checkpoint["pin_change_status"] = "changed_unconfirmed"
                    checkpoint["pin_verified"] = True
                self._checkpoint(context, checkpoint, client, "pin_ready")
                phase = "pin_ready"

        if phase == "pin_otp_sent":
            setup_pin = str(checkpoint.get("setup_pin") or pin)
            otp = self._obtain_otp(context, checkpoint, "PIN 设置")
            context.progress(0.72, "PIN OTP 已提交，开始验证")
            _expect(client.pin_verify_otp(otp), "PIN OTP 验证")
            context.progress(0.76, "PIN OTP 验证通过，正在设置新 PIN")
            _expect(client.pin_setup(setup_pin), "PIN 设置", side_effect=True)
            checkpoint["pin_set_now"] = True
            checkpoint["pin_verified"] = True
            self._checkpoint(context, checkpoint, client, "pin_setup_submitted")
            phase = "pin_setup_submitted"

        if phase == "pin_setup_submitted":
            try:
                self._confirm_pin_setup(context, client)
            except ReviewTaskError as exc:
                checkpoint["pin_setup_unconfirmed"] = True
                checkpoint["pin_change_status"] = "setup_unconfirmed"
                checkpoint["pin_change_message"] = str(exc)
                self._checkpoint(context, checkpoint, client, "pin_setup_submitted")
                setup_pin = str(checkpoint.get("setup_pin") or pin)
                self._persist_account(payload, checkpoint, client, setup_pin, 0)
                raise
            checkpoint["pin_configured"] = True
            checkpoint["pin_setup_unconfirmed"] = False
            self._checkpoint(context, checkpoint, client, "pin_ready")
            phase = "pin_ready"

        if phase != "pin_ready":
            raise ReviewTaskError("登录检查点状态需要人工复核", code="checkpoint_unknown")
        effective_pin = (
            new_pin if checkpoint.get("pin_changed_now") else str(checkpoint.get("setup_pin") or pin)
        )
        if not checkpoint.get("pin_verified"):
            effective_pin = ""
        context.progress(0.86, "已有账号登录成功，PIN 状态已确认，保存账号")
        return self._finish(context, payload, checkpoint, client, effective_pin, "已有账号登录完成")

    def _request_pin_otp(
        self,
        context: TaskContext,
        checkpoint: dict[str, Any],
        client: Any,
        pin: str,
    ) -> None:
        context.progress(0.63, "正在检查 PIN 并申请 PIN OTP")
        self._prepare_sms_for_next_otp(context, checkpoint, "PIN 设置")
        _expect(client.pin_check_allowed(pin), "PIN 可用性检查")
        response = client.pin_request_otp()
        if _status(response) == 401 and getattr(client.auth, "refresh_token", ""):
            _expect(client.refresh_token(), "刷新 PIN 会话令牌")
            response = client.pin_request_otp()
        _expect(response, "PIN OTP 申请")
        self._checkpoint(context, checkpoint, client, "pin_otp_sent")
        context.progress(0.64, "PIN OTP 已发送，等待新的验证码")

    def _confirm_pin_setup(self, context: TaskContext, client: Any) -> None:
        """按旧项目流程重新读取资料，确认 PIN 已真正生效。"""
        for attempt in range(1, 4):
            if attempt > 1:
                self._pause(context, 1.0)
            context.progress(0.82, f"正在重新检测 PIN 设置状态（{attempt}/3）")
            try:
                profile = client.get_user_profile()
            except Exception:
                continue
            if _status(profile) not in {200, 201}:
                continue
            state = _profile_pin_state(profile)
            if state is True:
                context.progress(0.84, "PIN 设置完成，并已重新检测确认")
                return
        raise ReviewTaskError(
            "PIN 设置接口已返回成功，但账号资料尚未确认 PIN 已生效；账号和号码已保留，等待复核",
            code="pin_setup_unconfirmed",
        )

    def _change_pin(self, context: TaskContext, client: Any, old_pin: str, new_pin: str) -> None:
        context.progress(0.68, "正在创建 PIN 修改 challenge")
        _expect(client.pin_create_challenge(flow="UPDATE_PIN"), "创建 PIN 修改 challenge")
        _expect(client.pin_verify(old_pin), "验证原 PIN")
        context.progress(0.75, "正在提交新 PIN")
        _expect(client.pin_update_v3(new_pin), "修改 PIN", side_effect=True)

    def _obtain_otp(
        self,
        context: TaskContext,
        checkpoint: dict[str, Any],
        purpose: str,
    ) -> str:
        defaults = self._defaults_store.get()
        sms_timeout = defaults.sms_otp_timeout_seconds
        manual_timeout = defaults.manual_otp_timeout_seconds
        value = context.consume_input("otp")
        activation_id = str(checkpoint.get("activation_id") or "")
        provider = self._checkpoint_sms_provider(checkpoint) if activation_id else ""
        label = provider_label(provider) if provider else "短信平台"
        timeout_detail = f"{label} 在自动取码时间内尚未收到 GoPay 的新验证码"
        if value is None and activation_id:
            settings = self._require_sms_settings(provider)
            progress = max(0.05, context.repository.get_task(context.task_id).progress)
            context.ensure_active()
            context.progress(
                progress,
                f"{label} 正在自动获取{purpose}验证码（最多等待 {sms_timeout} 秒）",
            )
            try:
                value = call_sms(
                    self._adapter,
                    "sms_wait_code",
                    settings,
                    activation_id,
                    timeout=sms_timeout,
                    ignore_code_hashes=set(checkpoint.get("consumed_code_hashes") or []),
                )
            except Exception:
                timeout_detail = f"{label} 自动取码请求失败"
                context.progress(
                    progress,
                    f"{label} 自动获取{purpose}验证码失败，已转为等待手动输入",
                )
        if value is not None:
            normalized = str(value).strip()
            if not re.fullmatch(r"\d{4,8}", normalized):
                raise PermanentTaskError("OTP 必须是 4 到 8 位数字", code="otp_invalid")
            self._record_consumed_code(checkpoint, normalized)
            if activation_id:
                settings = self._require_sms_settings(provider)
                try:
                    checkpoint["sms_retry_ready"] = bool(
                        call_sms(
                            self._adapter,
                            "sms_request_another",
                            settings,
                            activation_id,
                        )
                    )
                except Exception:
                    checkpoint["sms_retry_ready"] = False
            return normalized

        checkpoint["input_type"] = "otp"
        checkpoint["otp_purpose"] = purpose
        has_status_reader = any(
            callable(getattr(self._adapter, name, None))
            for name in ("sms_status_for", "sms_status")
        )
        if activation_id and has_status_reader:
            try:
                settings = self._require_sms_settings(provider)
                state, current_code = call_sms(
                    self._adapter,
                    "sms_status",
                    settings,
                    activation_id,
                )
                current_hash = (
                    hashlib.sha256(str(current_code).encode()).hexdigest() if current_code else ""
                )
                if state == "cancelled":
                    timeout_detail = f"{label} 激活已取消"
                elif current_hash and current_hash in set(
                    checkpoint.get("consumed_code_hashes") or []
                ):
                    timeout_detail = f"{label} 仍只返回已使用的旧验证码，未收到新验证码"
            except Exception:
                pass
        if activation_id:
            wait_message = (
                f"{purpose}验证码自动获取结束：{timeout_detail}；"
                f"正在等待手动输入，最多等待 {manual_timeout} 秒；"
                "该任务已释放 Worker 和批次并发名额"
            )
        else:
            wait_message = (
                f"正在等待手动输入{purpose}验证码，最多等待 {manual_timeout} 秒；"
                "该任务已释放 Worker 和批次并发名额"
            )
        return context.wait_for_input(
            "otp",
            timeout_seconds=manual_timeout,
            checkpoint=checkpoint,
            message=wait_message,
        )

    def _preflight(self, context: TaskContext, checkpoint: dict[str, Any]) -> dict[str, Any]:
        proxy = str(checkpoint.get("proxy") or "")
        if not proxy:
            return {}
        cached = checkpoint.get("proxy_preflight")
        if isinstance(cached, dict) and cached.get("ok"):
            context.progress(
                0.065,
                f"复用取号前代理预检结果：出口 IP {cached.get('ip') or '-'}",
            )
            return cached
        context.progress(0.06, "正在检测代理出口")
        result = self._probe_proxy(context, proxy, progress=0.06, stage="GoPay 请求前")
        if not result.get("ok"):
            raise RetryableTaskError(
                f"代理出口不可用（{self._proxy_probe_detail(result)}），请更换代理",
                code="proxy_unavailable",
            )
        cached_result = {
            "ok": True,
            "ip": str(result.get("ip") or "")[:80],
        }
        checkpoint["proxy_preflight"] = cached_result
        context.save_checkpoint(checkpoint)
        context.progress(0.065, f"代理预检通过：出口 IP {cached_result['ip'] or '-'}")
        return cached_result

    def _probe_proxy(
        self,
        context: TaskContext,
        proxy: str,
        *,
        progress: float,
        stage: str,
    ) -> dict[str, Any]:
        """代理出口短暂抖动时复检一次，不重复执行后续 GoPay 请求。"""
        result: dict[str, Any] = {}
        for attempt in range(1, 3):
            try:
                value = self._adapter.probe_proxy(proxy)
                result = value if isinstance(value, dict) else {"ok": False, "status": 0}
            except Exception:
                result = {"ok": False, "status": 0}
            if result.get("ok"):
                return result
            if attempt == 1:
                context.progress(
                    progress,
                    f"{stage}代理第 1/2 次检测未通过（{self._proxy_probe_detail(result)}），"
                    "正在复检",
                )
                self._pause(context, 1.0)
        return result

    @staticmethod
    def _proxy_probe_detail(result: dict[str, Any]) -> str:
        try:
            status = int(result.get("status") or 0)
        except (TypeError, ValueError):
            status = 0
        if status == 407:
            return "代理认证失败，HTTP 407"
        if status:
            return f"探测地址返回 HTTP {status}"
        return "连接超时或代理隧道暂时不可用"

    def _finish(
        self,
        context: TaskContext,
        payload: dict[str, Any],
        checkpoint: dict[str, Any],
        client: Any,
        pin: str,
        message: str,
    ) -> dict[str, Any]:
        context.progress(0.88, "正在保存账号令牌和余额")
        balance = self._current_balance(client)
        account_id = self._persist_account(payload, checkpoint, client, pin, balance)
        context.progress(1.0, message)
        return {
            "account_id": account_id,
            "phone": checkpoint["phone"],
            "balance": balance,
            "pin_set_now": bool(checkpoint.get("pin_set_now")),
            "pin_changed_now": bool(checkpoint.get("pin_changed_now")),
            "sms_activation_retained": bool(checkpoint.get("activation_id")),
        }

    @staticmethod
    def _current_balance(client: Any) -> int:
        try:
            response = client.get_balance()
            if _status(response) in {200, 201}:
                return _find_balance(response.get("body")) or 0
        except Exception:
            pass
        return 0

    def _finish_registration(
        self,
        context: TaskContext,
        payload: dict[str, Any],
        checkpoint: dict[str, Any],
        client: Any,
        pin: str,
    ) -> dict[str, Any]:
        """先完成核心注册落库，再把激活奖励等附加步骤交给独立持久化任务。"""
        phase = str(checkpoint.get("phase") or "pin_ready")
        account_id = str(checkpoint.get("account_id") or "")
        balance = int(checkpoint.get("initial_balance") or 0)
        if phase == "pin_ready":
            context.progress(0.86, "核心注册流程已完成，正在保存账号、令牌与 PIN 状态")
            balance = self._current_balance(client)
            account_id = self._persist_account(payload, checkpoint, client, pin, balance)
            checkpoint["account_id"] = account_id
            checkpoint["initial_balance"] = balance
            self._checkpoint(context, checkpoint, client, "account_persisted")
            phase = "account_persisted"

        if phase == "account_persisted":
            post_task, _created = context.repository.create_task(
                "account.post_register",
                {
                    "account_id": account_id,
                    "parent_task_id": context.task_id,
                    "claim_configured_envelope": True,
                },
                max_attempts=8,
                run_after=utc_now() + timedelta(seconds=1),
                idempotency_key=f"account-post-register:{context.task_id}",
            )
            checkpoint["post_registration_task_id"] = post_task.id
            checkpoint["phase"] = "post_registration_queued"
            context.save_checkpoint(checkpoint)
            phase = "post_registration_queued"

        if phase != "post_registration_queued":
            raise ReviewTaskError("注册落库检查点状态需要人工复核", code="checkpoint_unknown")
        context.progress(
            1.0,
            "GoPay 核心注册成功；PIN 后激活奖励、余额等待和红包领取已进入独立后台任务",
        )
        return {
            "account_id": account_id,
            "phone": checkpoint["phone"],
            "balance": balance,
            "pin_set_now": bool(checkpoint.get("pin_set_now")),
            "sms_activation_retained": bool(checkpoint.get("activation_id")),
            "post_registration_task_id": str(checkpoint.get("post_registration_task_id") or ""),
            "core_registration_succeeded": True,
        }

    def _persist_account(
        self,
        _payload: dict[str, Any],
        checkpoint: dict[str, Any],
        client: Any,
        pin: str,
        balance: int,
    ) -> str:
        now = utc_now()
        normalized = str(checkpoint["phone_normalized"])
        with self._session_factory() as session, session.begin():
            account = session.scalar(select(Account).where(Account.phone_normalized == normalized))
            account_created = account is None
            if account_created:
                account = Account(
                    id=_stable_id("account", normalized),
                    phone=checkpoint["phone"],
                    phone_normalized=normalized,
                    local_phone=checkpoint["local_phone"],
                    customer_id="",
                    remote_account_id="",
                    balance=0,
                    pin_setup_status="unknown",
                    pin_change_status="",
                    pin_change_message="",
                    sms_activation_status="unknown",
                    payment_fingerprint_json="{}",
                    registered_at=now.isoformat().replace("+00:00", "Z"),
                    created_at=now,
                    updated_at=now,
                    version=1,
                )
                session.add(account)
                session.flush()
            secret_row = session.get(AccountSecret, account.id)
            secret: dict[str, Any] = {}
            if secret_row is not None:
                try:
                    secret = json.loads(
                        self._codec.decrypt(
                            secret_row.secret_payload_ciphertext,
                            context=f"account:{account.id}",
                        )
                    )
                except (TypeError, ValueError):
                    secret = {}
            activation_id = str(checkpoint.get("activation_id") or secret.get("activation_id") or "")
            activation_provider = (
                str(
                    checkpoint.get("sms_provider")
                    or secret.get("activation_provider")
                    or "smsbower"
                )
                if activation_id
                else ""
            )
            secret.update(
                {
                    "phone": checkpoint["phone"],
                    "local": checkpoint["local_phone"],
                    "activation_id": activation_id,
                    "activation_provider": activation_provider,
                    "customer_id": str(getattr(client, "user_uuid", "") or ""),
                    "account_id": str(getattr(client.auth, "account_id", "") or ""),
                    "device_token": str(getattr(client, "device_token", "") or ""),
                    "device_uniqueid": str(getattr(client, "uniqueid", "") or ""),
                    "device_session_id": str(getattr(client, "session_id", "") or ""),
                    "access_token": str(getattr(client.auth, "access_token", "") or ""),
                    "refresh_token": str(getattr(client.auth, "refresh_token", "") or ""),
                    "proxy": str(checkpoint.get("proxy") or ""),
                    "registered_at": account.registered_at,
                    "balance": balance,
                    "protocol_client": _client_state(client),
                }
            )
            if pin:
                secret["pin"] = pin
            account.phone = checkpoint["phone"]
            account.local_phone = checkpoint["local_phone"]
            account.customer_id = str(getattr(client, "user_uuid", "") or "")
            account.remote_account_id = str(getattr(client.auth, "account_id", "") or "")
            account.balance = balance
            account.pin_setup_status = (
                "unknown"
                if checkpoint.get("pin_setup_unconfirmed")
                else (
                    "configured"
                    if checkpoint.get("pin_configured") or checkpoint.get("pin_verified")
                    else "unknown"
                )
            )
            account.pin_change_status = str(checkpoint.get("pin_change_status") or "")
            account.pin_change_message = (
                str(checkpoint.get("pin_change_message") or "")
                or (
                    "PIN 修改接口成功，等待下次登录确认"
                    if account.pin_change_status == "changed_unconfirmed"
                    else ""
                )
            )
            account.sms_activation_status = "active" if activation_id else "unavailable"
            account.updated_at = now
            if not account_created:
                account.version += 1
            encrypted = self._codec.encrypt(
                json.dumps(secret, ensure_ascii=False, separators=(",", ":")),
                context=f"account:{account.id}",
            )
            if secret_row is None:
                session.add(
                    AccountSecret(
                        account_id=account.id,
                        secret_payload_ciphertext=encrypted,
                        updated_at=now,
                    )
                )
            else:
                secret_row.secret_payload_ciphertext = encrypted
                secret_row.updated_at = now
            activation = None
            if activation_id:
                activation = session.scalar(
                    select(SmsActivation).where(
                        SmsActivation.provider == activation_provider,
                        SmsActivation.provider_activation_id == activation_id,
                    )
                )
            if activation is not None:
                activation.account_id = account.id
                activation.status = "active"
                activation.updated_at = now
            phone_row = session.get(PhoneNumber, checkpoint["phone_id"])
            if phone_row is not None:
                phone_row.status = "registered"
                phone_row.updated_at = now
            session.add(
                ChangeLog(
                    event_type="account.updated",
                    resource="account",
                    resource_id=account.id,
                    operation=self._mode,
                    payload_json=json.dumps(
                        {
                            "id": account.id,
                            "balance": balance,
                            "pin_setup_status": account.pin_setup_status,
                            "version": account.version,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    created_at=now,
                )
            )
            return account.id

    def _upsert_phone(self, phone: str, normalized: str, source: str) -> str:
        phone_id = _stable_id("phone", normalized)
        now = utc_now()
        statement = insert(PhoneNumber).values(
            id=phone_id,
            phone=phone,
            phone_normalized=normalized,
            source=source,
            status="rented" if source in self._sms_stores else "available",
            sms_url_ciphertext="",
            created_at=now,
            updated_at=now,
        )
        with self._session_factory() as session, session.begin():
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=[PhoneNumber.phone_normalized],
                    set_={"phone": phone, "source": source, "updated_at": now},
                )
            )
        return phone_id

    def _upsert_activation(self, provider: str, activation_id: str, phone_id: str) -> None:
        now = utc_now()
        statement = insert(SmsActivation).values(
            id=_stable_id("sms-activation", f"{provider}:{activation_id}"),
            account_id=None,
            phone_number_id=phone_id,
            provider=provider,
            provider_activation_id=activation_id,
            status="rented",
            consumed_code_hashes_json="[]",
            created_at=now,
            updated_at=now,
        )
        with self._session_factory() as session, session.begin():
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=[SmsActivation.provider, SmsActivation.provider_activation_id],
                    set_={"phone_number_id": phone_id, "status": "rented", "updated_at": now},
                )
            )

    def _record_consumed_code(self, checkpoint: dict[str, Any], code: str) -> None:
        digest = hashlib.sha256(code.encode()).hexdigest()
        hashes = list(dict.fromkeys([*(checkpoint.get("consumed_code_hashes") or []), digest]))
        checkpoint["consumed_code_hashes"] = hashes
        activation_id = str(checkpoint.get("activation_id") or "")
        if not activation_id:
            return
        provider = self._checkpoint_sms_provider(checkpoint)
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

    def _checkpoint_sms_provider(self, checkpoint: dict[str, Any]) -> str:
        provider = str(
            checkpoint.get("sms_provider")
            or checkpoint.get("phone_source")
            or "smsbower"
        ).strip()
        return provider if provider in self._sms_stores else "smsbower"

    def _require_sms_settings(self, provider: str = "smsbower") -> SmsSettings:
        value = get_sms_settings(self._sms_stores, provider)
        label = provider_label(provider)
        if value is None or not value.api_key:
            raise PermanentTaskError(
                f"请先在系统设置中配置 {label} API Key",
                code="sms_api_key_missing",
            )
        return value

    def _cancel_unused_activation(
        self,
        checkpoint: dict[str, Any],
        context: TaskContext | None = None,
    ) -> str:
        activation_id = str(checkpoint.get("activation_id") or "")
        if not activation_id or checkpoint.get("phase") in {
            "account_created",
            "authenticated",
            "pin_otp_sent",
            "pin_ready",
            "account_persisted",
            "post_registration_queued",
        }:
            return ""
        provider = self._checkpoint_sms_provider(checkpoint)
        status = "release_pending"
        try:
            settings = self._require_sms_settings(provider)
            if call_sms(
                self._adapter,
                "sms_cancel",
                settings,
                activation_id,
            ):
                status = "cancelled"
        except Exception:
            pass
        if status == "release_pending" and context is not None:
            context.repository.create_task(
                "sms.cancel_activation",
                {"provider": provider, "activation_id": activation_id},
                max_attempts=3,
                run_after=utc_now() + timedelta(seconds=135),
                idempotency_key=f"sms-cancel:{provider}:{activation_id}",
            )
        with self._session_factory() as session, session.begin():
            activation = session.scalar(
                select(SmsActivation).where(
                    SmsActivation.provider == provider,
                    SmsActivation.provider_activation_id == activation_id,
                )
            )
            if activation is not None:
                activation.status = status
                activation.updated_at = utc_now()
        return status
