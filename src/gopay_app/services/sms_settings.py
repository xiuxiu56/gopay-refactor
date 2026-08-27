"""短信接码平台配置的加密持久化服务。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session, sessionmaker

from gopay_app.db.models import Setting, utc_now
from gopay_app.security.codec import SecretCodec


@dataclass(frozen=True, slots=True)
class SmsSettings:
    api_key: str
    base_url: str
    service: str
    country: str
    provider: str = "smsbower"


class SmsSettingsStore:
    """统一读取新配置，并兼容 P0 导入的旧配置键。"""

    _defaults: ClassVar[dict[str, str]] = {
        "base_url": "https://smsbower.page",
        "service": "ni",
        "country": "6",
    }
    _keys: ClassVar[dict[str, str]] = {
        "api_key": "smsbower.api_key",
        "base_url": "smsbower.base_url",
        "service": "smsbower.service",
        "country": "smsbower.country",
    }
    _legacy_keys: ClassVar[dict[str, str]] = {
        "api_key": "legacy.sms.OPAI_SMSBOWER_API_KEY",
        "base_url": "legacy.sms.OPAI_SMSBOWER_API_BASE_URL",
        "service": "legacy.sms.OPAI_SMSBOWER_SERVICE",
        "country": "legacy.sms.OPAI_SMSBOWER_COUNTRY",
    }
    _provider: ClassVar[str] = "smsbower"

    def __init__(self, session_factory: sessionmaker[Session], codec: SecretCodec) -> None:
        self._session_factory = session_factory
        self._codec = codec

    def _decode(self, row: Setting | None) -> str:
        if row is None:
            return ""
        if row.is_secret:
            return self._codec.decrypt(row.value_ciphertext, context=f"setting:{row.key}")
        return row.value_text

    def get(self) -> SmsSettings:
        with self._session_factory() as session:
            rows = {
                row.key: row
                for row in session.scalars(
                    select(Setting).where(
                        Setting.key.in_([*self._keys.values(), *self._legacy_keys.values()])
                    )
                )
            }
        values: dict[str, str] = {}
        for name, key in self._keys.items():
            legacy_key = self._legacy_keys.get(name, "")
            value = self._decode(rows.get(key))
            if not value and legacy_key:
                value = self._decode(rows.get(legacy_key))
            values[name] = value.strip() or self._defaults.get(name, "")
        values["provider"] = self._provider
        return SmsSettings(**values)

    def save(
        self,
        *,
        api_key: str | None,
        base_url: str,
        service: str,
        country: str,
        clear_api_key: bool = False,
    ) -> SmsSettings:
        current = self.get()
        values = {
            "api_key": "" if clear_api_key else current.api_key if api_key is None else api_key.strip(),
            "base_url": base_url.strip().rstrip("/") or self._defaults["base_url"],
            "service": service.strip() or self._defaults["service"],
            "country": country.strip() or self._defaults["country"],
        }
        now = utc_now()
        with self._session_factory() as session, session.begin():
            for name, value in values.items():
                key = self._keys[name]
                secret = name == "api_key"
                statement = insert(Setting).values(
                    key=key,
                    value_text="" if secret else value,
                    value_ciphertext=(
                        self._codec.encrypt(value, context=f"setting:{key}") if secret and value else ""
                    ),
                    is_secret=secret,
                    updated_at=now,
                )
                session.execute(
                    statement.on_conflict_do_update(
                        index_elements=[Setting.key],
                        set_={
                            "value_text": "" if secret else value,
                            "value_ciphertext": (
                                self._codec.encrypt(value, context=f"setting:{key}")
                                if secret and value
                                else ""
                            ),
                            "is_secret": secret,
                            "updated_at": now,
                        },
                    )
                )
        return SmsSettings(**values, provider=self._provider)

    @staticmethod
    def public(value: SmsSettings) -> dict[str, object]:
        masked = ""
        if value.api_key:
            masked = f"{value.api_key[:3]}***{value.api_key[-3:]}" if len(value.api_key) > 8 else "***"
        return {
            "api_key_configured": bool(value.api_key),
            "api_key_masked": masked,
            "base_url": value.base_url,
            "service": value.service,
            "country": value.country,
        }


class HeroSmsSettingsStore(SmsSettingsStore):
    """Hero-SMS 独立配置，与 SMSBower 密钥及服务地址完全隔离。"""

    _defaults: ClassVar[dict[str, str]] = {
        "base_url": "https://hero-sms.com/stubs/handler_api.php",
        "service": "ni",
        "country": "6",
    }
    _keys: ClassVar[dict[str, str]] = {
        "api_key": "hero_sms.api_key",
        "base_url": "hero_sms.base_url",
        "service": "hero_sms.service",
        "country": "hero_sms.country",
    }
    _legacy_keys: ClassVar[dict[str, str]] = {}
    _provider: ClassVar[str] = "hero_sms"
