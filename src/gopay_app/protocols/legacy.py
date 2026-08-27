"""旧 GoPay、Midtrans 与 SMSBower 纯协议模块的隔离适配层。"""

from __future__ import annotations

import importlib
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

from gopay_app.protocols.sms_activate import SmsActivateClient


class ProtocolUnavailableError(RuntimeError):
    """旧协议模块路径或依赖不可用。"""


class LegacyProtocolAdapter:
    """只加载无 JSON 状态写入的底层协议对象。"""

    _import_lock = threading.Lock()
    _envelope_lock = threading.Lock()

    def __init__(self, legacy_app_path: Path) -> None:
        self._source_path = legacy_app_path.expanduser().resolve() / "src"

    def _module(self, name: str):
        if not self._source_path.is_dir():
            raise ProtocolUnavailableError(f"旧协议源码目录不存在：{self._source_path}")
        with self._import_lock:
            source = str(self._source_path)
            if source not in sys.path:
                sys.path.insert(0, source)
            try:
                return importlib.import_module(name)
            except Exception as exc:
                raise ProtocolUnavailableError(f"加载旧协议模块失败：{name}") from exc

    def new_gojek_client(self, phone: str, *, proxy: str = "", device_seed: str = ""):
        module = self._module("opai.core.gojek_client")
        return module.GojekClient.from_phone(phone, proxy=proxy, device_seed=device_seed)

    def new_payment(self, *, proxy: str = "", payment_fingerprint: dict[str, Any] | None = None):
        module = self._module("opai.core.gopay_payment_protocol")
        return module.GoPayPayment(proxy=proxy, payment_fingerprint=payment_fingerprint)

    def normalize_payment_fingerprint(
        self,
        payment_fingerprint: dict[str, Any] | None,
        *,
        phone: str,
        local: str,
        account_id: str,
    ) -> dict[str, Any]:
        module = self._module("opai.core.payment_fingerprint")
        return dict(
            module.normalize_payment_fingerprint(
                payment_fingerprint,
                phone=phone,
                local=local,
                account_id=account_id,
            )
        )

    @staticmethod
    def payment_warm_verification_page(payment: Any, url: str) -> dict[str, Any]:
        """访问支付 challenge 页面以建立与后续 GWA 请求一致的 Cookie 会话。"""
        headers = payment._request_headers({"Referer": "https://app.midtrans.com/"})
        response = payment._session.get(url, headers=headers, timeout_seconds=15)
        return {
            "status": int(getattr(response, "status_code", 0) or 0),
            "body": {"content_length": len(str(getattr(response, "text", "") or ""))},
        }

    @staticmethod
    def payment_read_midtrans_transaction(payment: Any, snap: str) -> dict[str, Any]:
        """按旧支付编排的无签名请求读取 Midtrans 交易元数据。"""
        response = payment._session.get(
            f"https://app.midtrans.com/snap/v1/transactions/{snap}",
            headers=payment._request_headers(),
            timeout_seconds=30,
        )
        try:
            body = response.json()
        except Exception:
            body = {"raw": str(getattr(response, "text", "") or "")[:500]}
        return {
            "status": int(getattr(response, "status_code", 0) or 0),
            "body": body if isinstance(body, dict) else {},
        }

    def payment_pin_verify(
        self,
        payment: Any,
        challenge_id: str,
        pin: str,
        *,
        purpose: str,
    ) -> str:
        module = self._module("opai.core.gopay_payment_protocol")
        client_id = module.PIN_CLIENT_LINKING if purpose == "linking" else module.PIN_CLIENT_PAYMENT
        return str(payment._pin_verify(challenge_id, pin, client_id))

    def sms_get_number(self, api_key: str) -> tuple[str | None, str | None]:
        return self._module("opai.core.sms_helpers").sms_get_number(api_key)

    @staticmethod
    def _sms_client(
        api_key: str,
        *,
        base_url: str,
        service: str,
        country: str,
    ) -> SmsActivateClient:
        label = "Hero-SMS" if "hero-sms" in base_url.lower() else "SMSBower"
        return SmsActivateClient(
            api_key=api_key,
            base_url=base_url,
            service=service,
            country=country,
            provider_label=label,
        )

    def sms_balance_for(
        self,
        api_key: str,
        *,
        base_url: str,
        service: str,
        country: str,
    ) -> str:
        return self._sms_client(
            api_key,
            base_url=base_url,
            service=service,
            country=country,
        ).get_balance()

    def sms_get_number_for(
        self,
        api_key: str,
        *,
        base_url: str,
        service: str,
        country: str,
    ) -> tuple[str | None, str | None]:
        return self._sms_client(
            api_key,
            base_url=base_url,
            service=service,
            country=country,
        ).get_number()

    def sms_wait_code(
        self,
        api_key: str,
        activation_id: str,
        *,
        timeout: int,
        ignore_code_hashes: set[str] | None = None,
    ) -> str | None:
        return self._module("opai.core.sms_helpers").sms_wait_code(
            api_key,
            activation_id,
            timeout=timeout,
            ignore_code_hashes=ignore_code_hashes or set(),
        )

    def sms_wait_code_for(
        self,
        api_key: str,
        activation_id: str,
        *,
        base_url: str,
        service: str,
        country: str,
        timeout: int,
        ignore_code_hashes: set[str] | None = None,
    ) -> str | None:
        return self._sms_client(
            api_key,
            base_url=base_url,
            service=service,
            country=country,
        ).wait_code(
            activation_id,
            timeout=timeout,
            ignore_code_hashes=ignore_code_hashes,
        )

    def configure_sms(
        self,
        *,
        api_key: str,
        base_url: str,
        service: str,
        country: str,
    ) -> None:
        values = {
            "OPAI_SMSBOWER_API_KEY": api_key,
            "OPAI_SMSBOWER_API_BASE_URL": base_url,
            "OPAI_SMSBOWER_SERVICE": service,
            "OPAI_SMSBOWER_COUNTRY": country,
        }
        with self._import_lock:
            for key, value in values.items():
                os.environ[key] = value

    def sms_request_another(self, api_key: str, activation_id: str) -> bool:
        return bool(self._module("opai.core.sms_helpers").sms_request_another(api_key, activation_id))

    def sms_request_another_for(
        self,
        api_key: str,
        activation_id: str,
        *,
        base_url: str,
        service: str,
        country: str,
    ) -> bool:
        return self._sms_client(
            api_key,
            base_url=base_url,
            service=service,
            country=country,
        ).request_another(activation_id)

    def sms_status(self, api_key: str, activation_id: str) -> tuple[str, str]:
        """读取一次激活状态，同时提取服务端当前保存的验证码。"""
        module = self._module("opai.core.sms_helpers")
        response = str(module.sms_api(api_key, "getStatus", {"id": activation_id}) or "").strip()
        upper = response.upper()
        state = "unknown"
        if upper.startswith("STATUS_WAIT_RETRY"):
            state = "waiting_retry"
        elif upper.startswith("STATUS_WAIT_CODE"):
            state = "waiting_code"
        elif upper.startswith("STATUS_OK"):
            state = "code_received"
        elif upper.startswith("STATUS_CANCEL") or "NO_ACTIVATION" in upper:
            state = "cancelled"

        values: list[str] = [response]
        try:
            payload = json.loads(response)
        except (TypeError, ValueError):
            payload = None

        def collect(value: Any) -> None:
            if isinstance(value, dict):
                for item in value.values():
                    collect(item)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    collect(item)
            elif value is not None:
                values.append(str(value))

        if payload is not None:
            collect(payload)
            joined = " ".join(values).upper()
            if "STATUS_WAIT_RETRY" in joined or "WAIT_RETRY" in joined:
                state = "waiting_retry"
            elif "STATUS_WAIT_CODE" in joined or "WAIT_CODE" in joined:
                state = "waiting_code"
            elif "STATUS_OK" in joined:
                state = "code_received"
            elif "STATUS_CANCEL" in joined or "NO_ACTIVATION" in joined:
                state = "cancelled"

        for value in values:
            match = re.search(r"(?<!\d)(\d{4,8})(?!\d)", value)
            if match:
                return state, match.group(1)
        return state, ""

    def sms_status_for(
        self,
        api_key: str,
        activation_id: str,
        *,
        base_url: str,
        service: str,
        country: str,
    ) -> tuple[str, str]:
        return self._sms_client(
            api_key,
            base_url=base_url,
            service=service,
            country=country,
        ).status(activation_id)

    def sms_cancel(self, api_key: str, activation_id: str) -> bool:
        return bool(self._module("opai.core.sms_helpers").sms_cancel(api_key, activation_id))

    def sms_cancel_for(
        self,
        api_key: str,
        activation_id: str,
        *,
        base_url: str,
        service: str,
        country: str,
    ) -> bool:
        return self._sms_client(
            api_key,
            base_url=base_url,
            service=service,
            country=country,
        ).cancel(activation_id)

    def sms_done(self, api_key: str, activation_id: str) -> bool:
        return bool(self._module("opai.core.sms_helpers").sms_done(api_key, activation_id))

    def sms_done_for(
        self,
        api_key: str,
        activation_id: str,
        *,
        base_url: str,
        service: str,
        country: str,
    ) -> bool:
        return self._sms_client(
            api_key,
            base_url=base_url,
            service=service,
            country=country,
        ).done(activation_id)

    def probe_proxy(self, proxy: str, *, timeout_sec: float | None = None) -> dict[str, Any]:
        return self._module("opai.core.gojek_client").probe_proxy_egress(
            proxy,
            timeout_sec=timeout_sec,
        )

    def claim_configured_envelope(self, client: Any) -> dict[str, Any]:
        """读取旧项目的红包配置并串行领取，返回适合新任务状态机保存的结果。"""
        configured_path = str(os.environ.get("OPAI_GOPAY_ENVELOPE_STORE") or "").strip()
        store_path = Path(configured_path) if configured_path else self._source_path.parent.parent / "config" / "envelope_links.json"
        module = self._module("opai.core.envelope_manager")
        with self._envelope_lock:
            manager = module.EnvelopeManager(store_path)
            active = manager.get_active()
            if not active:
                return {
                    "status": "not_configured" if not manager.links else "unavailable",
                    "http_status": 0,
                }
            result = manager.claim_one(client)
        if not isinstance(result, dict):
            return {"status": "unavailable", "http_status": 0}
        http_status = int(result.get("status") or 0)
        body = result.get("body")
        if http_status in {200, 201} and isinstance(body, dict) and body.get("success") is True:
            return {"status": "claimed", "http_status": http_status}
        text = str(body or "")
        if "GoPay-36006" in text:
            return {"status": "expired", "http_status": http_status}
        if "GoPay-36009" in text:
            return {"status": "already_claimed", "http_status": http_status}
        return {"status": "pending", "http_status": http_status}

    @staticmethod
    def account_request_pause(seconds: float) -> None:
        """按原流程在连续 GoPay 请求之间留出节奏间隔。"""
        time.sleep(max(0.0, min(float(seconds), 10.0)))
