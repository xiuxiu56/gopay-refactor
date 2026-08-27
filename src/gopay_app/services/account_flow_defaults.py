"""注册、登录与动态区域代理池默认配置。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import ClassVar
from urllib.parse import unquote, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session, sessionmaker

from gopay_app.db.models import Setting, utc_now
from gopay_app.security.codec import SecretCodec

_REGION_PATTERN = re.compile(r"(?:^|[-_])region[-_]([a-z0-9]{2,12})(?:[-_]|$)", re.IGNORECASE)
_ALLOWED_PROXY_SCHEMES = {"http", "https", "socks5", "socks5h"}
_REGION_NAMES = {
    "ID": "印度尼西亚",
    "GB": "英国",
    "JP": "日本",
    "PH": "菲律宾",
    "SG": "新加坡",
    "US": "美国",
    "UN": "未识别区域",
}


@dataclass(frozen=True, slots=True)
class ProxyEntry:
    url: str
    region: str


def _normalize_proxy_url(value: str, line_number: int) -> str:
    text = value.strip().replace(r"\@", "@")
    if not text:
        return ""
    if "://" not in text:
        if "@" in text:
            text = f"http://{text}"
        else:
            parts = text.split(":")
            if len(parts) >= 4 and parts[1].isdigit():
                host, port, username = parts[:3]
                password = ":".join(parts[3:])
                text = f"http://{username}:{password}@{host}:{port}"
            else:
                text = f"http://{text}"
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"第 {line_number} 条代理的端口格式不正确") from exc
    if parsed.scheme.lower() not in _ALLOWED_PROXY_SCHEMES or not parsed.hostname or port is None:
        raise ValueError(f"第 {line_number} 条代理格式不正确，请使用 用户名:密码@主机:端口 或完整代理地址")
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc,
            parsed.path,
            parsed.query,
            parsed.fragment,
        )
    )


def _detect_region(proxy_url: str) -> str:
    parsed = urlsplit(proxy_url)
    username = unquote(parsed.username or "")
    matched = _REGION_PATTERN.search(username)
    return matched.group(1).upper() if matched else "UN"


def parse_proxy_pool(value: str) -> tuple[ProxyEntry, ...]:
    """解析多行代理、去重并从用户名中的 region-XX 自动识别区域。"""
    entries: list[ProxyEntry] = []
    seen: set[str] = set()
    for line_number, raw in enumerate(value.splitlines(), 1):
        normalized = _normalize_proxy_url(raw, line_number)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        entries.append(ProxyEntry(url=normalized, region=_detect_region(normalized)))
        if len(entries) > 500:
            raise ValueError("代理池最多保存 500 条代理")
    return tuple(entries)


@dataclass(frozen=True, slots=True)
class AccountFlowDefaults:
    register_pin: str
    login_pin: str
    new_pin: str
    task_count: int
    concurrency: int
    sms_otp_timeout_seconds: int
    manual_otp_timeout_seconds: int
    change_pin_enabled: bool
    default_proxy_region: str
    proxy_pool: tuple[ProxyEntry, ...]

    def proxy_for(self, region: str, index: int = 0) -> str:
        normalized_region = region.strip().upper()
        candidates = [item.url for item in self.proxy_pool if item.region == normalized_region]
        return candidates[index % len(candidates)] if candidates else ""


class AccountFlowDefaultsStore:
    """将 PIN 和动态代理池加密保存，普通数值以明文保存。"""

    _prefix = "account_flow."
    _defaults: ClassVar[dict[str, str]] = {
        "register_pin": "",
        "login_pin": "",
        "new_pin": "",
        "task_count": "1",
        "concurrency": "2",
        "sms_otp_timeout_seconds": "60",
        "manual_otp_timeout_seconds": "300",
        "change_pin_enabled": "1",
        "default_proxy_region": "",
        "proxy_pool": "",
    }
    _legacy_proxy_names: ClassVar[tuple[str, ...]] = ("proxy_gb", "proxy_jp", "proxy_ph")
    _secret_names: ClassVar[set[str]] = {
        "register_pin",
        "login_pin",
        "new_pin",
        "proxy_pool",
    }

    def __init__(self, session_factory: sessionmaker[Session], codec: SecretCodec) -> None:
        self._session_factory = session_factory
        self._codec = codec

    def _key(self, name: str) -> str:
        return f"{self._prefix}{name}"

    def _decode(self, row: Setting | None) -> str:
        if row is None:
            return ""
        if row.is_secret:
            return self._codec.decrypt(row.value_ciphertext, context=f"setting:{row.key}")
        return row.value_text

    def get(self) -> AccountFlowDefaults:
        names = [*self._defaults, *self._legacy_proxy_names]
        keys = [self._key(name) for name in names]
        with self._session_factory() as session:
            rows = {row.key: row for row in session.scalars(select(Setting).where(Setting.key.in_(keys)))}
        values = {
            name: self._decode(rows.get(self._key(name))) or default
            for name, default in self._defaults.items()
        }
        proxy_text = values["proxy_pool"]
        if not proxy_text:
            proxy_text = "\n".join(
                value
                for name in self._legacy_proxy_names
                if (value := self._decode(rows.get(self._key(name))).strip())
            )
        try:
            task_count = min(1000, max(1, int(values["task_count"])))
        except ValueError:
            task_count = 1
        try:
            concurrency = min(50, max(1, int(values["concurrency"])))
        except ValueError:
            concurrency = 2
        try:
            sms_otp_timeout_seconds = min(
                60,
                max(30, int(values["sms_otp_timeout_seconds"])),
            )
        except ValueError:
            sms_otp_timeout_seconds = 60
        try:
            manual_otp_timeout_seconds = min(
                1800,
                max(60, int(values["manual_otp_timeout_seconds"])),
            )
        except ValueError:
            manual_otp_timeout_seconds = 300
        proxy_pool = parse_proxy_pool(proxy_text)
        regions = {item.region for item in proxy_pool}
        proxy_region = values["default_proxy_region"].strip().upper()
        if proxy_region and proxy_region not in regions:
            proxy_region = ""
        return AccountFlowDefaults(
            register_pin=values["register_pin"].strip(),
            login_pin=values["login_pin"].strip(),
            new_pin=values["new_pin"].strip(),
            task_count=task_count,
            concurrency=concurrency,
            sms_otp_timeout_seconds=sms_otp_timeout_seconds,
            manual_otp_timeout_seconds=manual_otp_timeout_seconds,
            change_pin_enabled=values["change_pin_enabled"] == "1",
            default_proxy_region=proxy_region,
            proxy_pool=proxy_pool,
        )

    def save(
        self,
        *,
        register_pin: str | None,
        login_pin: str | None,
        new_pin: str | None,
        task_count: int,
        concurrency: int,
        sms_otp_timeout_seconds: int,
        manual_otp_timeout_seconds: int,
        change_pin_enabled: bool,
        default_proxy_region: str,
        proxy_pool: str | None,
        clear_proxy_pool: bool = False,
    ) -> AccountFlowDefaults:
        current = self.get()
        current_urls = [item.url for item in current.proxy_pool]
        if clear_proxy_pool:
            merged_entries: tuple[ProxyEntry, ...] = ()
        elif proxy_pool is None or not proxy_pool.strip():
            merged_entries = current.proxy_pool
        else:
            incoming = parse_proxy_pool(proxy_pool)
            merged_entries = parse_proxy_pool("\n".join([*current_urls, *(item.url for item in incoming)]))
        normalized_region = default_proxy_region.strip().upper()
        available_regions = {item.region for item in merged_entries}
        if normalized_region and normalized_region not in available_regions:
            normalized_region = ""
        if not normalized_region and merged_entries and not current.proxy_pool:
            normalized_region = sorted(available_regions)[0]
        values = {
            "register_pin": current.register_pin if register_pin is None else register_pin.strip(),
            "login_pin": current.login_pin if login_pin is None else login_pin.strip(),
            "new_pin": current.new_pin if new_pin is None else new_pin.strip(),
            "task_count": str(min(1000, max(1, task_count))),
            "concurrency": str(min(50, max(1, concurrency))),
            "sms_otp_timeout_seconds": str(
                min(60, max(30, sms_otp_timeout_seconds))
            ),
            "manual_otp_timeout_seconds": str(
                min(1800, max(60, manual_otp_timeout_seconds))
            ),
            "change_pin_enabled": "1" if change_pin_enabled else "0",
            "default_proxy_region": normalized_region,
            "proxy_pool": "\n".join(item.url for item in merged_entries),
        }
        now = utc_now()
        with self._session_factory() as session, session.begin():
            for name, value in values.items():
                key = self._key(name)
                secret = name in self._secret_names
                ciphertext = self._codec.encrypt(value, context=f"setting:{key}") if secret and value else ""
                statement = insert(Setting).values(
                    key=key,
                    value_text="" if secret else value,
                    value_ciphertext=ciphertext,
                    is_secret=secret,
                    updated_at=now,
                )
                session.execute(
                    statement.on_conflict_do_update(
                        index_elements=[Setting.key],
                        set_={
                            "value_text": "" if secret else value,
                            "value_ciphertext": ciphertext,
                            "is_secret": secret,
                            "updated_at": now,
                        },
                    )
                )
        return self.get()

    def add_proxy_pool(self, proxy_pool: str) -> AccountFlowDefaults:
        """追加已通过连通性测试的代理，其他默认配置保持不变。"""
        current = self.get()
        return self.save(
            register_pin=None,
            login_pin=None,
            new_pin=None,
            task_count=current.task_count,
            concurrency=current.concurrency,
            sms_otp_timeout_seconds=current.sms_otp_timeout_seconds,
            manual_otp_timeout_seconds=current.manual_otp_timeout_seconds,
            change_pin_enabled=current.change_pin_enabled,
            default_proxy_region=current.default_proxy_region,
            proxy_pool=proxy_pool,
        )

    @staticmethod
    def _mask_proxy(value: str) -> str:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        return f"***@{host}{port}" if parsed.username else f"{host}{port}"

    @classmethod
    def mask_proxy(cls, value: str) -> str:
        """返回可安全展示的代理摘要。"""
        return cls._mask_proxy(value)

    @classmethod
    def public(cls, value: AccountFlowDefaults) -> dict[str, object]:
        grouped: dict[str, list[ProxyEntry]] = {}
        for item in value.proxy_pool:
            grouped.setdefault(item.region, []).append(item)
        profiles = [
            {
                "region": region,
                "label": f"{region} 自动分配" if region != "UN" else "未识别区域",
                "description": _REGION_NAMES.get(region, f"{region} 区域"),
                "configured": True,
                "count": len(items),
                "masked": cls._mask_proxy(items[0].url),
            }
            for region, items in sorted(grouped.items())
        ]
        return {
            "register_pin": value.register_pin,
            "login_pin": value.login_pin,
            "new_pin": value.new_pin,
            "task_count": value.task_count,
            "concurrency": value.concurrency,
            "sms_otp_timeout_seconds": value.sms_otp_timeout_seconds,
            "manual_otp_timeout_seconds": value.manual_otp_timeout_seconds,
            "change_pin_enabled": value.change_pin_enabled,
            "default_proxy_region": value.default_proxy_region,
            "proxy_count": len(value.proxy_pool),
            "proxy_profiles": profiles,
        }
