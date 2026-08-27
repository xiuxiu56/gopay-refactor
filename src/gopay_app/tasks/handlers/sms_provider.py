"""短信平台配置选择与协议调用转发。"""

from __future__ import annotations

from typing import Any

from gopay_app.services.sms_settings import SmsSettings, SmsSettingsStore

SMS_PROVIDER_LABELS = {
    "smsbower": "SMSBower",
    "hero_sms": "Hero-SMS",
}


def provider_label(provider: str) -> str:
    return SMS_PROVIDER_LABELS.get(provider, provider or "短信平台")


def build_sms_stores(
    sms_store: SmsSettingsStore,
    hero_sms_store: SmsSettingsStore | None = None,
) -> dict[str, SmsSettingsStore]:
    stores = {"smsbower": sms_store}
    if hero_sms_store is not None:
        stores["hero_sms"] = hero_sms_store
    return stores


def get_sms_settings(
    stores: dict[str, SmsSettingsStore],
    provider: str,
) -> SmsSettings | None:
    store = stores.get(provider)
    return store.get() if store is not None else None


def call_sms(
    adapter: Any,
    method: str,
    settings: SmsSettings,
    *args: object,
    **kwargs: object,
) -> Any:
    """优先调用不依赖全局环境变量的平台隔离方法。"""
    isolated = getattr(adapter, f"{method}_for", None)
    if callable(isolated):
        return isolated(
            settings.api_key,
            *args,
            base_url=settings.base_url,
            service=settings.service,
            country=settings.country,
            **kwargs,
        )
    adapter.configure_sms(
        api_key=settings.api_key,
        base_url=settings.base_url,
        service=settings.service,
        country=settings.country,
    )
    return getattr(adapter, method)(settings.api_key, *args, **kwargs)
