"""本地业务配置接口。"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, SecretStr

from gopay_app.api.dependencies import require_admin, require_csrf
from gopay_app.api.responses import success
from gopay_app.services.account_flow_defaults import ProxyEntry, parse_proxy_pool

router = APIRouter(prefix="/api/v1/settings", tags=["设置"])


class SmsSettingsUpdate(BaseModel):
    api_key: SecretStr | None = Field(default=None, max_length=512)
    clear_api_key: bool = False
    base_url: str = Field(default="https://smsbower.page", min_length=8, max_length=500)
    service: str = Field(default="ni", min_length=1, max_length=32)
    country: str = Field(default="6", min_length=1, max_length=16)


class HeroSmsSettingsUpdate(BaseModel):
    api_key: SecretStr | None = Field(default=None, max_length=512)
    clear_api_key: bool = False
    base_url: str = Field(
        default="https://hero-sms.com/stubs/handler_api.php",
        min_length=8,
        max_length=500,
    )
    service: str = Field(default="ni", min_length=1, max_length=32)
    country: str = Field(default="6", min_length=1, max_length=16)


class HeroSmsConnectionTest(BaseModel):
    api_key: SecretStr | None = Field(default=None, max_length=512)
    base_url: str | None = Field(default=None, min_length=8, max_length=500)
    service: str | None = Field(default=None, min_length=1, max_length=32)
    country: str | None = Field(default=None, min_length=1, max_length=16)


class AccountFlowDefaultsUpdate(BaseModel):
    register_pin: SecretStr | None = Field(default=None, max_length=6)
    login_pin: SecretStr | None = Field(default=None, max_length=6)
    new_pin: SecretStr | None = Field(default=None, max_length=6)
    task_count: int = Field(default=1, ge=1, le=50)
    concurrency: int = Field(default=2, ge=1, le=50)
    sms_otp_timeout_seconds: int = Field(default=60, ge=30, le=60)
    manual_otp_timeout_seconds: int = Field(default=300, ge=60, le=1800)
    change_pin_enabled: bool = True
    default_proxy_region: str = Field(default="", pattern=r"^(|[A-Za-z0-9]{2,12})$")
    proxy_pool: SecretStr | None = Field(default=None, max_length=50_000)
    clear_proxy_pool: bool = False


class ProxyPoolTestAndAdd(BaseModel):
    proxy_pool: SecretStr = Field(min_length=1, max_length=50_000)


def _test_proxy(adapter: Any, entry: ProxyEntry, index: int) -> dict[str, object]:
    try:
        result = adapter.probe_proxy(entry.url, timeout_sec=8)
    except Exception:
        result = {"ok": False, "status": 0}
    ok = bool(result.get("ok"))
    status = int(result.get("status") or 0)
    if ok:
        message = "测试通过"
    elif status:
        message = f"代理返回 HTTP {status}"
    else:
        message = "连接超时或代理不可用"
    return {
        "index": index,
        "region": entry.region,
        "proxy": "",
        "ok": ok,
        "ip": str(result.get("ip") or "")[:80] if ok else "",
        "status": status,
        "message": message,
    }


@router.get("/smsbower")
def get_sms_settings(request: Request, _admin=Depends(require_admin)):
    value = request.app.state.sms_settings_store.get()
    return success(request.app.state.sms_settings_store.public(value))


@router.put("/smsbower")
def update_sms_settings(
    request: Request,
    body: SmsSettingsUpdate,
    _admin=Depends(require_csrf),
):
    api_key = body.api_key.get_secret_value() if body.api_key is not None else None
    value = request.app.state.sms_settings_store.save(
        api_key=api_key,
        base_url=body.base_url,
        service=body.service,
        country=body.country,
        clear_api_key=body.clear_api_key,
    )
    return success(request.app.state.sms_settings_store.public(value))


@router.get("/hero-sms")
def get_hero_sms_settings(request: Request, _admin=Depends(require_admin)):
    value = request.app.state.hero_sms_settings_store.get()
    return success(request.app.state.hero_sms_settings_store.public(value))


@router.put("/hero-sms")
def update_hero_sms_settings(
    request: Request,
    body: HeroSmsSettingsUpdate,
    _admin=Depends(require_csrf),
):
    api_key = body.api_key.get_secret_value() if body.api_key is not None else None
    value = request.app.state.hero_sms_settings_store.save(
        api_key=api_key,
        base_url=body.base_url,
        service=body.service,
        country=body.country,
        clear_api_key=body.clear_api_key,
    )
    return success(request.app.state.hero_sms_settings_store.public(value))


@router.post("/hero-sms/test")
def test_hero_sms_connection(
    request: Request,
    body: HeroSmsConnectionTest,
    _admin=Depends(require_csrf),
):
    current = request.app.state.hero_sms_settings_store.get()
    api_key = body.api_key.get_secret_value().strip() if body.api_key is not None else current.api_key
    if not api_key:
        raise HTTPException(
            status_code=422,
            detail={"code": "hero_sms_api_key_missing", "message": "请先填写 Hero-SMS API Key"},
        )
    try:
        balance = request.app.state.protocol_adapter.sms_balance_for(
            api_key,
            base_url=(body.base_url or current.base_url).strip(),
            service=(body.service or current.service).strip(),
            country=(body.country or current.country).strip(),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail={"code": "hero_sms_test_failed", "message": f"Hero-SMS 连接测试失败：{exc}"},
        ) from exc
    return success(
        {
            "provider": "hero_sms",
            "balance": str(balance),
            "message": "Hero-SMS 连接正常",
        }
    )


@router.get("/account-flow")
def get_account_flow_defaults(request: Request, _admin=Depends(require_admin)):
    value = request.app.state.account_flow_defaults_store.get()
    return success(request.app.state.account_flow_defaults_store.public(value))


@router.put("/account-flow")
def update_account_flow_defaults(
    request: Request,
    body: AccountFlowDefaultsUpdate,
    _admin=Depends(require_csrf),
):
    secrets = {
        "register_pin": body.register_pin.get_secret_value() if body.register_pin is not None else None,
        "login_pin": body.login_pin.get_secret_value() if body.login_pin is not None else None,
        "new_pin": body.new_pin.get_secret_value() if body.new_pin is not None else None,
    }
    for label, value in (
        ("注册 PIN", secrets["register_pin"]),
        ("登录原 PIN", secrets["login_pin"]),
        ("登录新 PIN", secrets["new_pin"]),
    ):
        if value is not None and not re.fullmatch(r"\d{6}", value):
            raise HTTPException(
                status_code=422,
                detail={"code": "pin_invalid", "message": f"{label} 必须是 6 位数字"},
            )
    if (
        body.change_pin_enabled
        and secrets["login_pin"] is not None
        and secrets["new_pin"] is not None
        and secrets["login_pin"] == secrets["new_pin"]
    ):
        raise HTTPException(
            status_code=422,
            detail={"code": "new_pin_same", "message": "登录新 PIN 需要与原 PIN 不同"},
        )
    proxy_pool = body.proxy_pool.get_secret_value() if body.proxy_pool is not None else None
    try:
        value = request.app.state.account_flow_defaults_store.save(
            register_pin=secrets["register_pin"],
            login_pin=secrets["login_pin"],
            new_pin=secrets["new_pin"],
            task_count=body.task_count,
            concurrency=body.concurrency,
            sms_otp_timeout_seconds=body.sms_otp_timeout_seconds,
            manual_otp_timeout_seconds=body.manual_otp_timeout_seconds,
            change_pin_enabled=body.change_pin_enabled,
            default_proxy_region=body.default_proxy_region,
            proxy_pool=proxy_pool,
            clear_proxy_pool=body.clear_proxy_pool,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "proxy_pool_invalid", "message": str(exc)},
        ) from exc
    return success(request.app.state.account_flow_defaults_store.public(value))


@router.post("/account-flow/proxies/test-and-add")
def test_and_add_proxy_pool(
    request: Request,
    body: ProxyPoolTestAndAdd,
    _admin=Depends(require_csrf),
):
    try:
        entries = parse_proxy_pool(body.proxy_pool.get_secret_value())
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "proxy_pool_invalid", "message": str(exc)},
        ) from exc
    if not entries:
        raise HTTPException(
            status_code=422,
            detail={"code": "proxy_pool_empty", "message": "请粘贴至少一条代理"},
        )
    if len(entries) > 100:
        raise HTTPException(
            status_code=422,
            detail={"code": "proxy_test_limit", "message": "每次最多测试 100 条代理"},
        )

    worker_count = min(12, len(entries))
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="proxy-test") as pool:
        results = list(
            pool.map(
                lambda item: _test_proxy(request.app.state.protocol_adapter, item[1], item[0]),
                enumerate(entries, 1),
            )
        )

    store = request.app.state.account_flow_defaults_store
    for entry, result in zip(entries, results, strict=True):
        result["proxy"] = store.mask_proxy(entry.url)
    passed_entries = [entry for entry, result in zip(entries, results, strict=True) if result["ok"]]
    if passed_entries:
        value = store.add_proxy_pool("\n".join(entry.url for entry in passed_entries))
    else:
        value = store.get()
    data = store.public(value)
    data["proxy_test"] = {
        "tested": len(results),
        "passed": len(passed_entries),
        "failed": len(results) - len(passed_entries),
        "results": results,
    }
    return success(data)
