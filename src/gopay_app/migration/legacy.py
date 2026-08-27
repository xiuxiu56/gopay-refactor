"""旧 JSON/ENV 数据的只读预检和幂等导入。"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session, sessionmaker

from gopay_app.db.models import (
    Account,
    AccountSecret,
    ChangeLog,
    LegacyImport,
    PaymentIntent,
    PhoneNumber,
    Setting,
    SmsActivation,
    Task,
    utc_now,
)
from gopay_app.security.codec import SecretCodec

LEGACY_FILES = {
    "accounts": Path("config/gopay_worker_accounts.json"),
    "phones": Path("config/gopay_phone_pool.json"),
    "payments": Path("config/midtrans_snap_state.json"),
    "tasks": Path("config/payment_tasks.json"),
    "sms": Path("config/sms.env"),
}


def _digits(value: object) -> str:
    return "".join(character for character in str(value or "") if character.isdigit())


def _stable_id(namespace: str, value: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"gopay-v2:{namespace}:{value}"))


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sanitize_public_text(value: object, limit: int = 1000) -> str:
    text = str(value or "")
    text = re.sub(r"://[^/@\s]+@", "://***@", text)
    text = re.sub(
        r"(?i)((?:api[_-]?key|token|secret|password|pin)(?:\s*[:=]\s*))[^\s,;]+",
        r"\1***",
        text,
    )
    return text[:limit]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        normalized_key = key.strip()
        if normalized_key:
            values[normalized_key] = value.strip().strip('"').strip("'")
    return values


@dataclass(slots=True)
class FilePreview:
    name: str
    path: Path
    exists: bool
    sha256: str = ""
    records: int = 0
    valid: bool = True
    message: str = ""


@dataclass(slots=True)
class LegacyPreview:
    source: Path
    files: list[FilePreview] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return all(item.valid for item in self.files)

    @property
    def counts(self) -> dict[str, int]:
        return {item.name: item.records for item in self.files}


@dataclass(slots=True)
class ImportResult:
    imported: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)


def inspect_legacy(source: Path) -> LegacyPreview:
    root = source.expanduser().resolve()
    preview = LegacyPreview(source=root)
    for name, relative_path in LEGACY_FILES.items():
        path = root / relative_path
        item = FilePreview(name=name, path=path, exists=path.is_file())
        preview.files.append(item)
        if not item.exists:
            item.message = "文件不存在，迁移时跳过"
            continue
        item.sha256 = _sha256(path)
        try:
            if name == "sms":
                item.records = len(_read_env(path))
            else:
                data = _read_json(path)
                if name in {"accounts", "phones"}:
                    if not isinstance(data, list):
                        raise ValueError("根节点应为数组")
                    item.records = len([row for row in data if isinstance(row, dict)])
                elif name == "tasks":
                    if not isinstance(data, dict) or not isinstance(data.get("jobs", []), list):
                        raise ValueError("任务文件结构不正确")
                    item.records = len([row for row in data.get("jobs", []) if isinstance(row, dict)])
                elif name == "payments":
                    if not isinstance(data, dict):
                        raise ValueError("支付状态根节点应为对象")
                    item.records = len([row for row in data.values() if isinstance(row, dict)])
        except Exception as exc:
            item.valid = False
            item.message = f"解析失败：{exc}"

    accounts_path = root / LEGACY_FILES["accounts"]
    if accounts_path.is_file():
        try:
            accounts = _read_json(accounts_path)
            phones = [
                _digits(row.get("phone") or row.get("local")) for row in accounts if isinstance(row, dict)
            ]
            duplicates = sorted({phone for phone in phones if phone and phones.count(phone) > 1})
            invalid = sum(1 for phone in phones if not phone)
            if duplicates:
                preview.warnings.append(f"账号数据存在 {len(duplicates)} 个重复手机号，迁移时按手机号更新")
            if invalid:
                preview.warnings.append(f"账号数据存在 {invalid} 条无有效手机号记录，迁移时跳过")
        except Exception:
            pass
    return preview


class LegacyImporter:
    def __init__(self, session_factory: sessionmaker[Session], codec: SecretCodec):
        self._session_factory = session_factory
        self._codec = codec

    def apply(self, preview: LegacyPreview) -> ImportResult:
        if not preview.valid:
            raise ValueError("旧数据预检存在错误，请先修复解析失败的文件")
        result = ImportResult()
        with self._session_factory() as session:
            for file_preview in preview.files:
                if not file_preview.exists:
                    result.skipped[file_preview.name] = 0
                    continue
                imported = self._apply_file(session, file_preview)
                if imported is None:
                    result.skipped[file_preview.name] = file_preview.records
                else:
                    result.imported[file_preview.name] = imported
            session.add(
                ChangeLog(
                    event_type="legacy.imported",
                    resource="database",
                    resource_id="legacy",
                    operation="import",
                    payload_json=_json_text({"counts": result.imported}),
                )
            )
            session.commit()
        return result

    def _apply_file(self, session: Session, item: FilePreview) -> int | None:
        source_key = str(item.path.resolve())
        prior = session.get(LegacyImport, source_key)
        if prior is not None and prior.sha256 == item.sha256:
            return None

        if item.name == "accounts":
            count = self._import_accounts(session, _read_json(item.path))
        elif item.name == "phones":
            count = self._import_phones(session, _read_json(item.path))
        elif item.name == "payments":
            count = self._import_payments(session, _read_json(item.path))
        elif item.name == "tasks":
            count = self._import_tasks(session, _read_json(item.path))
        elif item.name == "sms":
            count = self._import_sms_settings(session, _read_env(item.path))
        else:
            count = 0

        statement = insert(LegacyImport).values(
            source_file=source_key,
            sha256=item.sha256,
            record_count=count,
            imported_at=utc_now(),
        )
        session.execute(
            statement.on_conflict_do_update(
                index_elements=[LegacyImport.source_file],
                set_={"sha256": item.sha256, "record_count": count, "imported_at": utc_now()},
            )
        )
        return count

    def _import_accounts(self, session: Session, rows: list[dict[str, Any]]) -> int:
        imported = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            phone = str(row.get("phone") or row.get("local") or "").strip()
            normalized = _digits(phone)
            if not normalized:
                continue
            account_id = _stable_id("account", normalized)
            account_values = {
                "id": account_id,
                "phone": phone,
                "phone_normalized": normalized,
                "local_phone": _digits(row.get("local")),
                "customer_id": str(row.get("customer_id") or ""),
                "remote_account_id": str(row.get("account_id") or ""),
                "balance": int(row.get("balance") or 0),
                "pin_setup_status": str(row.get("pin_setup_status") or "unknown"),
                "pin_change_status": str(row.get("pin_change_status") or ""),
                "pin_change_message": _sanitize_public_text(row.get("pin_change_message")),
                "sms_activation_status": str(row.get("sms_activation_status") or "unknown"),
                "payment_fingerprint_json": _json_text(row.get("payment_fingerprint") or {}),
                "registered_at": str(row.get("registered_at") or ""),
                "updated_at": utc_now(),
            }
            account_stmt = insert(Account).values(**account_values, created_at=utc_now(), version=1)
            session.execute(
                account_stmt.on_conflict_do_update(
                    index_elements=[Account.phone_normalized],
                    set_={
                        key: value
                        for key, value in account_values.items()
                        if key not in {"id", "phone_normalized"}
                    },
                )
            )
            encrypted = self._codec.encrypt(_json_text(row), context=f"account:{account_id}")
            secret_stmt = insert(AccountSecret).values(
                account_id=account_id,
                secret_payload_ciphertext=encrypted,
                updated_at=utc_now(),
            )
            session.execute(
                secret_stmt.on_conflict_do_update(
                    index_elements=[AccountSecret.account_id],
                    set_={"secret_payload_ciphertext": encrypted, "updated_at": utc_now()},
                )
            )
            activation_id = str(row.get("activation_id") or row.get("aid") or "").strip()
            if activation_id:
                activation_stmt = insert(SmsActivation).values(
                    id=_stable_id("sms-activation", f"smsbower:{activation_id}"),
                    account_id=account_id,
                    phone_number_id=None,
                    provider="smsbower",
                    provider_activation_id=activation_id,
                    status=str(row.get("sms_activation_status") or "unknown"),
                    consumed_code_hashes_json=_json_text(row.get("sms_consumed_code_hashes") or []),
                    created_at=utc_now(),
                    updated_at=utc_now(),
                )
                session.execute(
                    activation_stmt.on_conflict_do_update(
                        index_elements=[SmsActivation.provider, SmsActivation.provider_activation_id],
                        set_={
                            "account_id": account_id,
                            "status": str(row.get("sms_activation_status") or "unknown"),
                            "consumed_code_hashes_json": _json_text(
                                row.get("sms_consumed_code_hashes") or []
                            ),
                            "updated_at": utc_now(),
                        },
                    )
                )
            imported += 1
        return imported

    def _import_phones(self, session: Session, rows: list[dict[str, Any]]) -> int:
        imported = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            phone = str(row.get("phone") or "").strip()
            normalized = _digits(phone)
            if not normalized:
                continue
            phone_id = _stable_id("phone", normalized)
            sms_url = str(row.get("sms_url") or "")
            values = {
                "id": phone_id,
                "phone": phone,
                "phone_normalized": normalized,
                "source": "imported",
                "status": str(row.get("status") or "available"),
                "sms_url_ciphertext": self._codec.encrypt(sms_url, context=f"phone:{phone_id}:sms-url"),
                "updated_at": utc_now(),
            }
            statement = insert(PhoneNumber).values(**values, created_at=utc_now())
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=[PhoneNumber.phone_normalized],
                    set_={
                        key: value for key, value in values.items() if key not in {"id", "phone_normalized"}
                    },
                )
            )
            imported += 1
        return imported

    def _import_payments(self, session: Session, rows: dict[str, dict[str, Any]]) -> int:
        imported = 0
        for snap_token, row in rows.items():
            if not isinstance(row, dict):
                continue
            token = str(snap_token or row.get("snap") or "").strip()
            if not token:
                continue
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            intent_id = _stable_id("payment-intent", token_hash)
            normalized_phone = _digits(row.get("phone"))
            account_id = _stable_id("account", normalized_phone) if normalized_phone else None
            if account_id and session.get(Account, account_id) is None:
                account_id = None
            midtrans_url = str(row.get("midtrans_url") or "")
            values = {
                "id": intent_id,
                "snap_token_hash": token_hash,
                "order_id": str(row.get("order_id") or ""),
                "account_id": account_id,
                "status": str(row.get("status") or "unknown"),
                "midtrans_url_ciphertext": self._codec.encrypt(
                    midtrans_url, context=f"payment:{intent_id}:url"
                ),
                "raw_state_ciphertext": self._codec.encrypt(
                    _json_text({"snap": token, **row}), context=f"payment:{intent_id}:state"
                ),
                "updated_at": utc_now(),
            }
            statement = insert(PaymentIntent).values(**values, created_at=utc_now())
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=[PaymentIntent.snap_token_hash],
                    set_={
                        key: value for key, value in values.items() if key not in {"id", "snap_token_hash"}
                    },
                )
            )
            imported += 1
        return imported

    def _import_tasks(self, session: Session, data: dict[str, Any]) -> int:
        imported = 0
        status_map = {
            "success": "succeeded",
            "done": "succeeded",
            "failed": "failed",
            "cancelled": "cancelled",
            "canceled": "cancelled",
            "running": "needs_review",
            "waiting_otp": "needs_review",
        }
        for row in data.get("jobs", []):
            if not isinstance(row, dict):
                continue
            legacy_id = str(row.get("id") or uuid.uuid4())
            task_id = _stable_id("legacy-payment-task", legacy_id)
            status = status_map.get(str(row.get("status") or "").lower(), "needs_review")
            payload = self._codec.encrypt(_json_text(row), context=f"task:{task_id}:payload")
            values = {
                "id": task_id,
                "batch_id": None,
                "task_type": "payment",
                "status": status,
                "priority": 0,
                "progress": 1.0 if status in {"succeeded", "failed", "cancelled"} else 0.0,
                "payload_ciphertext": payload,
                "checkpoint_ciphertext": "",
                "result_ciphertext": "",
                "idempotency_key": f"legacy-payment-task:{legacy_id}",
                "attempt": 0,
                "max_attempts": 1,
                "run_after": utc_now(),
                "locked_by": "",
                "locked_until": None,
                "last_error_code": "legacy_interrupted" if status == "needs_review" else "",
                "last_error_message": "旧版运行中任务需要人工确认" if status == "needs_review" else "",
                "updated_at": utc_now(),
            }
            statement = insert(Task).values(**values, created_at=utc_now(), started_at=None, finished_at=None)
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=[Task.idempotency_key],
                    set_={
                        key: value for key, value in values.items() if key not in {"id", "idempotency_key"}
                    },
                )
            )
            imported += 1
        return imported

    def _import_sms_settings(self, session: Session, values: dict[str, str]) -> int:
        imported = 0
        for key, value in values.items():
            setting_key = f"legacy.sms.{key}"
            ciphertext = self._codec.encrypt(value, context=f"setting:{setting_key}")
            statement = insert(Setting).values(
                key=setting_key,
                value_text="",
                value_ciphertext=ciphertext,
                is_secret=True,
                updated_at=utc_now(),
            )
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=[Setting.key],
                    set_={"value_ciphertext": ciphertext, "is_secret": True, "updated_at": utc_now()},
                )
            )
            imported += 1
        return imported


def count_imported_rows(session_factory: sessionmaker[Session]) -> dict[str, int]:
    with session_factory() as session:
        return {
            "accounts": len(session.scalars(select(Account.id)).all()),
            "phones": len(session.scalars(select(PhoneNumber.id)).all()),
            "payments": len(session.scalars(select(PaymentIntent.id)).all()),
            "tasks": len(session.scalars(select(Task.id)).all()),
            "sms": len(session.scalars(select(Setting.key).where(Setting.key.like("legacy.sms.%"))).all()),
        }
