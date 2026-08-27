"""注册成功后的钱包激活、奖励余额等待与红包领取持久化任务。"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from gopay_app.db.models import Account, AccountSecret, ChangeLog, utc_now
from gopay_app.protocols.legacy import LegacyProtocolAdapter, ProtocolUnavailableError
from gopay_app.security.codec import SecretCodec

from ..context import TaskContext
from ..errors import PermanentTaskError
from .account_flow import _auth_fields, _client_state, _status
from .business import _find_balance


class AccountPostRegisterHandler:
    """处理不影响核心注册结论的注册后附加流程。"""

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

        account, secret = self._load_account(account_id)
        context.acquire_resource("account", account_id, ttl_seconds=600)
        client = self._restore_client(account.phone, secret)
        checkpoint = context.checkpoint()
        if not checkpoint:
            checkpoint = {
                "version": 1,
                "phase": "prepared",
                "account_id": account_id,
                "parent_task_id": str(payload.get("parent_task_id") or ""),
                "warnings": [],
                "warmup_index": 0,
                "balance_poll_count": 0,
                "client": _client_state(client),
            }
            context.save_checkpoint(checkpoint)
        else:
            client = self._restore_checkpoint_client(client, checkpoint)

        phase = str(checkpoint.get("phase") or "prepared")
        if phase == "prepared":
            context.progress(0.06, "核心注册已成功，开始执行独立的 PIN 后钱包激活任务")
            self._pause(context, 2.0)
            hook = self._optional_step(
                context,
                checkpoint,
                client,
                "GoPay 钱包首次激活",
                getattr(client, "pin_post_registration_hook", None),
                0.1,
            )
            checkpoint["first_hook_status"] = _status(hook)
            self._checkpoint(context, checkpoint, client, "initial_hook_done")
            phase = "initial_hook_done"

        if phase in {"initial_hook_done", "post_pin_warmup"}:
            self._run_post_pin_warmup(context, checkpoint, client)
            phase = "post_pin_warmup_done"

        if phase == "post_pin_warmup_done":
            first_status = int(checkpoint.get("first_hook_status") or 0)
            second_status = 0
            if first_status not in {200, 201}:
                context.progress(0.59, "首次钱包激活未通过，刷新令牌后执行一次补偿激活")
                self._optional_step(
                    context,
                    checkpoint,
                    client,
                    "补偿激活前令牌刷新",
                    getattr(client, "refresh_token", None),
                    0.6,
                )
                self._pause(context, 10.0)
                second = self._optional_step(
                    context,
                    checkpoint,
                    client,
                    "GoPay 钱包第二次激活",
                    getattr(client, "pin_post_registration_hook", None),
                    0.62,
                )
                second_status = _status(second)
                if second_status in {200, 201}:
                    for label, method_name, args, kwargs in (
                        ("支付方式余额补刷新", "gopay_get_balances", (), {}),
                        ("钱包余额组件补刷新", "wallet_card_balance", (), {}),
                        ("安全中心状态补刷新", "security_meter", ("security_meter",), {}),
                    ):
                        self._optional_step(
                            context,
                            checkpoint,
                            client,
                            label,
                            getattr(client, method_name, None),
                            0.64,
                            *args,
                            **kwargs,
                        )
            checkpoint["second_hook_status"] = second_status
            checkpoint["activation_status"] = (
                "activated" if first_status in {200, 201} or second_status in {200, 201} else "pending"
            )
            self._checkpoint(context, checkpoint, client, "activation_done")
            phase = "activation_done"

        if phase == "activation_done":
            self._claim_configured_envelope(context, payload, checkpoint, client)
            self._checkpoint(context, checkpoint, client, "envelope_done")
            phase = "envelope_done"

        if phase in {"envelope_done", "balance_waiting"}:
            balance = self._wait_reward_balance(context, checkpoint, client, account_id)
            checkpoint["final_balance"] = balance
            checkpoint["reward_status"] = "arrived" if balance > 0 else "pending"
            self._checkpoint(context, checkpoint, client, "post_registration_done")
            phase = "post_registration_done"

        if phase != "post_registration_done":
            raise PermanentTaskError("注册后附加任务检查点不正确", code="post_registration_checkpoint_invalid")

        warnings = list(dict.fromkeys(str(item) for item in checkpoint.get("warnings") or [] if item))
        if warnings:
            context.progress(1.0, f"注册后附加流程已完成，共记录 {len(warnings)} 条警告；核心注册仍保持成功")
        else:
            context.progress(1.0, "PIN 后钱包激活、奖励余额检查和红包流程已完成")
        return {
            "account_id": account_id,
            "parent_task_id": str(checkpoint.get("parent_task_id") or ""),
            "activation_status": str(checkpoint.get("activation_status") or "pending"),
            "reward_status": str(checkpoint.get("reward_status") or "pending"),
            "envelope_status": str(checkpoint.get("envelope_status") or "skipped"),
            "balance": int(checkpoint.get("final_balance") or 0),
            "warnings": warnings,
        }

    def _load_account(self, account_id: str) -> tuple[Account, dict[str, Any]]:
        with self._session_factory() as session:
            account = session.get(Account, account_id)
            secret_row = session.get(AccountSecret, account_id)
            if account is None or secret_row is None:
                raise PermanentTaskError("注册成功账号或账号密钥记录不存在", code="account_not_found")
            secret = json.loads(
                self._codec.decrypt(
                    secret_row.secret_payload_ciphertext,
                    context=f"account:{account_id}",
                )
            )
            session.expunge(account)
        if not isinstance(secret, dict):
            raise PermanentTaskError("账号密钥记录格式不正确", code="account_secret_invalid")
        return account, secret

    def _restore_client(self, phone: str, secret: dict[str, Any]):
        try:
            client = self._adapter.new_gojek_client(
                phone,
                proxy=str(secret.get("proxy") or ""),
            )
        except ProtocolUnavailableError as exc:
            raise PermanentTaskError(str(exc), code="protocol_unavailable") from exc
        saved = secret.get("protocol_client")
        if not isinstance(saved, dict):
            saved = {
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
        return self._apply_client_state(client, saved)

    def _restore_checkpoint_client(self, client: Any, checkpoint: dict[str, Any]):
        saved = checkpoint.get("client")
        return self._apply_client_state(client, saved if isinstance(saved, dict) else {})

    @staticmethod
    def _apply_client_state(client: Any, saved: dict[str, Any]):
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
            if field in saved and hasattr(client, field):
                setattr(client, field, saved[field])
        return client

    def _checkpoint(
        self,
        context: TaskContext,
        checkpoint: dict[str, Any],
        client: Any,
        phase: str,
    ) -> None:
        checkpoint["phase"] = phase
        checkpoint["client"] = _client_state(client)
        context.save_checkpoint(checkpoint)

    def _warning(self, checkpoint: dict[str, Any], message: str) -> None:
        warnings = [str(item) for item in checkpoint.get("warnings") or [] if item]
        warnings.append(message)
        checkpoint["warnings"] = list(dict.fromkeys(warnings))[-100:]

    def _optional_step(
        self,
        context: TaskContext,
        checkpoint: dict[str, Any],
        client: Any,
        label: str,
        callback: Callable[..., Any] | None,
        progress: float,
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """附加请求只记录结果，异常和非成功状态不会反向覆盖核心注册状态。"""
        context.ensure_active()
        if not callable(callback):
            message = f"{label}接口在当前协议版本中不存在，已跳过"
            self._warning(checkpoint, message)
            context.progress(progress, message)
            return {"status": 0, "body": {}}
        context.progress(progress, f"{label}请求中")
        try:
            response = callback(*args, **kwargs)
        except Exception:
            message = f"{label}请求异常，已记录待补偿状态"
            self._warning(checkpoint, message)
            context.progress(progress, message)
            return {"status": 0, "body": {}}
        if not isinstance(response, dict):
            response = {"status": 0, "body": {}}
        status = _status(response)
        if status in {200, 201, 204}:
            context.progress(progress, f"{label}请求完成（HTTP {status}）")
        else:
            message = f"{label}返回 HTTP {status}，已记录待补偿状态"
            self._warning(checkpoint, message)
            context.progress(progress, message)
        checkpoint["client"] = _client_state(client)
        context.save_checkpoint(checkpoint)
        return response

    def _run_post_pin_warmup(
        self,
        context: TaskContext,
        checkpoint: dict[str, Any],
        client: Any,
    ) -> None:
        steps: tuple[tuple[str, str, tuple[Any, ...], dict[str, Any]], ...] = (
            ("首页安全状态刷新", "security_meter", ("gopay_home",), {}),
            ("账号安全状态刷新", "security_meter", ("account_safety_home",), {}),
            ("安全中心状态刷新", "security_meter", ("security_meter",), {}),
            (
                "安全提示展示状态回传",
                "security_meter",
                ("security_meter",),
                {
                    "view_count": 1,
                    "click_count": 0,
                    "security_aware_identifier": "cyber_security_zero_policy",
                },
            ),
            ("用户资料刷新", "get_user_profile", (), {}),
            ("Gojek 用户资料补刷新", "gojek_customer_profile", (), {}),
            ("消息通道令牌补刷新", "courier_token", (), {}),
            ("公开实验配置补刷新", "litmus_public_experiments", (), {}),
            ("登录态实验配置补刷新", "litmus_experiments", (), {}),
            ("节日礼包资源补刷新", "festivals_assets", (), {}),
            ("支付方式余额补刷新", "gopay_get_balances", (), {}),
            ("消息角标补刷新", "red_badges", (), {}),
            ("支付方式资料补刷新", "gopay_get_profiles", (), {}),
            ("实名认证状态补刷新", "kyc_status", (), {}),
            ("Support SDK session 上报", "support_customer_session", (), {}),
            ("Support SDK activity 上报", "support_customer_activity", (), {}),
        )
        start = int(checkpoint.get("warmup_index") or 0)
        checkpoint["phase"] = "post_pin_warmup"
        context.save_checkpoint(checkpoint)
        context.progress(0.14, "开始执行独立的 PIN 后真机钱包初始化链路")
        for index in range(start, len(steps)):
            label, method_name, args, kwargs = steps[index]
            progress = 0.16 + (0.4 * (index + 1) / len(steps))
            self._optional_step(
                context,
                checkpoint,
                client,
                label,
                getattr(client, method_name, None),
                progress,
                *args,
                **kwargs,
            )
            checkpoint["warmup_index"] = index + 1
            checkpoint["client"] = _client_state(client)
            context.save_checkpoint(checkpoint)
        self._checkpoint(context, checkpoint, client, "post_pin_warmup_done")

    def _claim_configured_envelope(
        self,
        context: TaskContext,
        payload: dict[str, Any],
        checkpoint: dict[str, Any],
        client: Any,
    ) -> None:
        if not bool(payload.get("claim_configured_envelope", True)):
            checkpoint["envelope_status"] = "disabled"
            context.progress(0.68, "配置红包领取未开启，已跳过")
            return
        callback = getattr(self._adapter, "claim_configured_envelope", None)
        if not callable(callback):
            checkpoint["envelope_status"] = "unsupported"
            self._warning(checkpoint, "当前协议适配器未提供配置红包领取能力")
            context.progress(0.68, "当前协议适配器未提供配置红包领取能力，已跳过")
            return
        context.progress(0.68, "正在检查已配置的节日红包")
        try:
            result = callback(client)
        except Exception:
            checkpoint["envelope_status"] = "pending"
            self._warning(checkpoint, "节日红包领取请求异常，已保留待补偿状态")
            context.progress(0.7, "节日红包领取请求异常，核心注册状态不受影响")
            return
        status = str(result.get("status") or "pending") if isinstance(result, dict) else "pending"
        checkpoint["envelope_status"] = status
        messages = {
            "claimed": "节日红包领取完成",
            "not_configured": "未配置可领取的节日红包，已跳过",
            "unavailable": "当前没有可领取的节日红包",
            "expired": "配置的节日红包已过期",
            "already_claimed": "当前账号已经领取过配置红包",
        }
        message = messages.get(status, "节日红包领取结果待确认，核心注册状态不受影响")
        if status not in {"claimed", "not_configured", "unavailable", "expired", "already_claimed"}:
            self._warning(checkpoint, message)
        context.progress(0.7, message)

    def _wait_reward_balance(
        self,
        context: TaskContext,
        checkpoint: dict[str, Any],
        client: Any,
        account_id: str,
    ) -> int:
        wait_seconds = self._bounded_env_int(
            "GOPAY_POST_REGISTER_BALANCE_WAIT_SECONDS",
            fallback_name="OPAI_GOPAY_POST_PIN_BALANCE_WAIT_SEC",
            default=180,
            minimum=0,
            maximum=1800,
        )
        poll_seconds = self._bounded_env_int(
            "GOPAY_POST_REGISTER_BALANCE_POLL_SECONDS",
            fallback_name="OPAI_GOPAY_POST_PIN_BALANCE_POLL_SEC",
            default=10,
            minimum=1,
            maximum=120,
        )
        poll_target = math.ceil(wait_seconds / poll_seconds) if wait_seconds else 0
        poll_count = int(checkpoint.get("balance_poll_count") or 0)
        checkpoint["phase"] = "balance_waiting"
        context.save_checkpoint(checkpoint)

        balance = self._read_balance(context, checkpoint, client, 0.74)
        if balance >= 0:
            self._save_account_state(account_id, client, balance, checkpoint)
        if balance > 0:
            context.progress(0.98, f"奖励余额已到账：{balance} Rp")
            return balance
        if poll_count == 0 and wait_seconds:
            context.progress(0.76, f"当前余额为 {max(balance, 0)} Rp，最多等待 {wait_seconds} 秒检查异步奖励")

        while balance <= 0 and poll_count < poll_target:
            self._pause(context, poll_seconds)
            poll_count += 1
            checkpoint["balance_poll_count"] = poll_count
            progress = min(0.97, 0.76 + (0.2 * poll_count / max(1, poll_target)))
            balance = self._read_balance(context, checkpoint, client, progress)
            if balance >= 0:
                self._save_account_state(account_id, client, balance, checkpoint)
            checkpoint["last_balance"] = balance
            checkpoint["client"] = _client_state(client)
            context.save_checkpoint(checkpoint)
            if balance > 0:
                context.progress(progress, f"奖励余额已到账：{balance} Rp")
                break
            context.progress(progress, f"奖励余额检查第 {poll_count}/{poll_target} 次：暂未到账")

        if balance <= 0:
            context.progress(0.98, "奖励余额在等待时间内尚未到账，已记录待补偿状态")
        return max(balance, 0)

    def _read_balance(
        self,
        context: TaskContext,
        checkpoint: dict[str, Any],
        client: Any,
        progress: float,
    ) -> int:
        context.ensure_active()
        try:
            response = client.get_balance()
        except Exception:
            self._warning(checkpoint, "奖励余额查询请求异常")
            return -1
        if _status(response) not in {200, 201}:
            self._warning(checkpoint, f"奖励余额查询返回 HTTP {_status(response)}")
            return -1
        balance = _find_balance(response.get("body"))
        if balance is None:
            self._warning(checkpoint, "奖励余额响应中缺少余额字段")
            return -1
        context.progress(progress, f"已读取 GoPay 余额：{balance} Rp")
        return int(balance)

    def _save_account_state(
        self,
        account_id: str,
        client: Any,
        balance: int,
        checkpoint: dict[str, Any],
    ) -> None:
        now = utc_now()
        with self._session_factory() as session, session.begin():
            account = session.get(Account, account_id)
            secret_row = session.get(AccountSecret, account_id)
            if account is None or secret_row is None:
                raise PermanentTaskError("注册成功账号在附加流程中被移除", code="account_removed")
            secret = json.loads(
                self._codec.decrypt(
                    secret_row.secret_payload_ciphertext,
                    context=f"account:{account_id}",
                )
            )
            secret.update(
                {
                    "access_token": str(getattr(client.auth, "access_token", "") or ""),
                    "refresh_token": str(getattr(client.auth, "refresh_token", "") or ""),
                    "balance": balance,
                    "protocol_client": _client_state(client),
                    "post_registration": {
                        "activation_status": str(checkpoint.get("activation_status") or "pending"),
                        "envelope_status": str(checkpoint.get("envelope_status") or "skipped"),
                        "reward_status": "arrived" if balance > 0 else "pending",
                        "updated_at": now.isoformat().replace("+00:00", "Z"),
                    },
                }
            )
            account.balance = balance
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
                    operation="post_register",
                    payload_json=json.dumps(
                        {
                            "id": account_id,
                            "balance": balance,
                            "activation_status": str(checkpoint.get("activation_status") or "pending"),
                            "envelope_status": str(checkpoint.get("envelope_status") or "skipped"),
                            "version": account.version,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    created_at=now,
                )
            )

    def _pause(self, context: TaskContext, seconds: float) -> None:
        remaining = max(0.0, float(seconds))
        pause = getattr(self._adapter, "account_request_pause", None)
        while remaining > 0:
            context.ensure_active()
            chunk = min(10.0, remaining)
            if callable(pause):
                pause(chunk)
            remaining -= chunk
        context.ensure_active()

    @staticmethod
    def _bounded_env_int(
        name: str,
        *,
        fallback_name: str,
        default: int,
        minimum: int,
        maximum: int,
    ) -> int:
        value = os.environ.get(name, os.environ.get(fallback_name, str(default)))
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return min(maximum, max(minimum, parsed))
