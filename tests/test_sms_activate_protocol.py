"""Hero-SMS 的 SMS-Activate 兼容协议测试。"""

from __future__ import annotations

from types import SimpleNamespace

from gopay_app.protocols.sms_activate import SmsActivateClient


def test_hero_sms_compatible_actions(monkeypatch):
    calls: list[tuple[str, dict[str, str]]] = []

    class FakeSession:
        def get(self, url, *, params, timeout_seconds):
            assert url == "https://hero-sms.com/stubs/handler_api.php"
            assert timeout_seconds == 30
            assert params["api_key"] == "hero-secret-key"
            calls.append((params["action"], dict(params)))
            if params["action"] == "getBalance":
                body = "ACCESS_BALANCE:18.50"
            elif params["action"] == "getNumber":
                assert params["service"] == "ni"
                assert params["country"] == "6"
                body = "ACCESS_NUMBER:12345678:628123456789"
            elif params["action"] == "getStatus":
                body = "STATUS_OK:654321"
            elif params["status"] == "3":
                body = "ACCESS_RETRY_GET"
            elif params["status"] == "6":
                body = "ACCESS_ACTIVATION"
            else:
                body = "ACCESS_CANCEL"
            return SimpleNamespace(status_code=200, text=body)

    monkeypatch.setattr(
        "gopay_app.protocols.sms_activate.tls_client.Session",
        lambda **_kwargs: FakeSession(),
    )
    client = SmsActivateClient(
        api_key="hero-secret-key",
        base_url="https://hero-sms.com/stubs/handler_api.php",
        service="ni",
        country="6",
        provider_label="Hero-SMS",
    )

    assert client.get_balance() == "18.50"
    assert client.get_number() == ("+628123456789", "12345678")
    assert client.status("12345678") == ("code_received", "654321")
    assert client.wait_code("12345678", timeout=1) == "654321"
    assert client.request_another("12345678") is True
    assert client.done("12345678") is True
    assert client.cancel("12345678") is True
    assert [action for action, _params in calls] == [
        "getBalance",
        "getNumber",
        "getStatus",
        "getStatus",
        "setStatus",
        "setStatus",
        "setStatus",
    ]
