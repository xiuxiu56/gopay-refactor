"""SMS-Activate 兼容协议的并发安全客户端。"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Collection, Iterator
from typing import Any

import tls_client


class SmsActivateProtocolError(RuntimeError):
    """短信平台请求或响应不正确。"""


def sms_code_hash(code: str) -> str:
    normalized = str(code or "").strip()
    return hashlib.sha256(normalized.encode()).hexdigest() if normalized else ""


def _json_values(value: object) -> Iterator[str]:
    if isinstance(value, dict):
        for item in value.values():
            yield from _json_values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _json_values(item)
    elif value is not None:
        yield str(value)


def _json_payload(value: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(value)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


class SmsActivateClient:
    """每个实例固定一套平台配置，避免 Worker 之间串用密钥。"""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        service: str,
        country: str,
        provider_label: str,
    ) -> None:
        self._api_key = api_key.strip()
        self._base_url = base_url.strip().rstrip("/")
        self._service = service.strip()
        self._country = country.strip()
        self._provider_label = provider_label.strip() or "短信平台"
        if not self._api_key:
            raise SmsActivateProtocolError(f"{self._provider_label} API Key 未配置")
        if not self._base_url.startswith(("https://", "http://")):
            raise SmsActivateProtocolError(f"{self._provider_label} 服务地址格式不正确")

    @property
    def endpoint(self) -> str:
        if self._base_url.lower().endswith(("handler_api.php", "handler_api_internal.php")):
            return self._base_url
        return f"{self._base_url}/stubs/handler_api.php"

    def _request(
        self,
        action: str,
        params: dict[str, str] | None = None,
        *,
        retries: int = 3,
    ) -> str:
        values = {"api_key": self._api_key, "action": action, **(params or {})}
        last_message = ""
        for attempt in range(1, max(1, retries) + 1):
            try:
                session = tls_client.Session(client_identifier="chrome_120")
                response = session.get(self.endpoint, params=values, timeout_seconds=30)
                status = int(getattr(response, "status_code", 0) or 0)
                body = str(getattr(response, "text", "") or "").strip()
                if 200 <= status < 300:
                    return body
                last_message = f"HTTP {status}：{body[:180]}"
                if status not in {408, 425, 429} and status < 500:
                    break
            except Exception as exc:
                last_message = str(exc or "连接失败")[:180].replace(self._api_key, "***")
            if attempt < retries:
                time.sleep(1.5 * attempt)
        raise SmsActivateProtocolError(
            f"{self._provider_label} {action} 请求失败：{last_message or '未知错误'}"
        )

    def get_balance(self) -> str:
        response = self._request("getBalance", retries=1)
        if response.startswith("ACCESS_BALANCE:"):
            return response.split(":", 1)[1].strip()
        payload = _json_payload(response)
        if payload:
            values = list(_json_values(payload.get("data", payload)))
            balance = next((item for item in values if re.fullmatch(r"-?\d+(?:\.\d+)?", item)), "")
            if balance:
                return balance
        raise SmsActivateProtocolError(f"{self._provider_label} 余额响应不正确：{response[:120]}")

    def get_number(self) -> tuple[str | None, str | None]:
        response = self._request(
            "getNumber",
            {"service": self._service, "country": self._country},
        )
        if response.startswith("ACCESS_NUMBER:"):
            parts = response.split(":", 2)
            if len(parts) == 3 and parts[1].strip() and parts[2].strip():
                phone = parts[2].strip()
                return (phone if phone.startswith("+") else f"+{phone}"), parts[1].strip()
        payload = _json_payload(response)
        if payload:
            values = list(_json_values(payload.get("data", payload)))
            activation_id = next((item for item in values if re.fullmatch(r"\d+", item)), "")
            phone = next(
                (item for item in values if item != activation_id and re.search(r"\d{7,}", item)),
                "",
            )
            if activation_id and phone:
                return (phone if phone.startswith("+") else f"+{phone}"), activation_id
        return None, None

    def status(self, activation_id: str) -> tuple[str, str]:
        response = self._request("getStatus", {"id": activation_id})
        upper = response.upper()
        state = "unknown"
        if upper.startswith("STATUS_WAIT_RETRY"):
            state = "waiting_retry"
        elif upper.startswith(("STATUS_WAIT_CODE", "STATUS_WAIT_RESEND")):
            state = "waiting_code"
        elif upper.startswith("STATUS_OK"):
            state = "code_received"
        elif upper.startswith("STATUS_CANCEL") or "NO_ACTIVATION" in upper:
            state = "cancelled"

        values = [response]
        payload = _json_payload(response)
        if payload:
            values.extend(_json_values(payload))
            joined = " ".join(values).upper()
            if "STATUS_WAIT_RETRY" in joined or "WAIT_RETRY" in joined:
                state = "waiting_retry"
            elif "STATUS_WAIT_CODE" in joined or "WAIT_CODE" in joined or "WAIT_RESEND" in joined:
                state = "waiting_code"
            elif "STATUS_OK" in joined:
                state = "code_received"
            elif "STATUS_CANCEL" in joined or "NO_ACTIVATION" in joined:
                state = "cancelled"

        for value in values:
            matched = re.search(r"(?<!\d)(\d{4,8})(?!\d)", value)
            if matched:
                return state, matched.group(1)
        return state, ""

    def wait_code(
        self,
        activation_id: str,
        *,
        timeout: int,
        ignore_code_hashes: Collection[str] | None = None,
    ) -> str | None:
        ignored = {
            str(item).strip().lower()
            for item in (ignore_code_hashes or ())
            if re.fullmatch(r"[0-9a-fA-F]{64}", str(item).strip())
        }
        deadline = time.monotonic() + max(1, timeout)
        while time.monotonic() < deadline:
            try:
                state, code = self.status(activation_id)
            except SmsActivateProtocolError:
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(5, remaining))
                continue
            if code and sms_code_hash(code) not in ignored:
                return code
            if state == "cancelled":
                return None
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(5, remaining))
        return None

    def request_another(self, activation_id: str) -> bool:
        response = self._request("setStatus", {"id": activation_id, "status": "3"})
        return "ACCESS_RETRY_GET" in response.upper()

    def cancel(self, activation_id: str) -> bool:
        response = self._request("setStatus", {"id": activation_id, "status": "8"})
        upper = response.upper()
        return "ACCESS_CANCEL" in upper or "STATUS_CANCEL" in upper

    def done(self, activation_id: str) -> bool:
        response = self._request("setStatus", {"id": activation_id, "status": "6"})
        upper = response.upper()
        return "ACCESS_ACTIVATION" in upper or "ACCESS_READY" in upper
