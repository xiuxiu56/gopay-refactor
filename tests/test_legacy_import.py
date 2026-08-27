"""旧数据预检与幂等迁移测试。"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select

from gopay_app.db.models import AccountSecret, Setting
from gopay_app.migration.legacy import LegacyImporter, count_imported_rows, inspect_legacy


def _write_legacy_fixture(root: Path) -> None:
    config = root / "config"
    config.mkdir(parents=True)
    (config / "gopay_worker_accounts.json").write_text(
        json.dumps(
            [
                {
                    "phone": "+628123456789",
                    "local": "08123456789",
                    "pin": "147258",
                    "access_token": "access-secret-value",
                    "refresh_token": "refresh-secret-value",
                    "proxy": "http://user:pass@proxy.invalid:8080",
                    "balance": 1000,
                    "activation_id": "12345",
                    "sms_activation_status": "active",
                    "payment_fingerprint": {"profile_id": "fixture"},
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (config / "gopay_phone_pool.json").write_text(
        json.dumps(
            [{"phone": "+628199999999", "sms_url": "https://sms.invalid/?key=secret", "status": "available"}]
        ),
        encoding="utf-8",
    )
    (config / "midtrans_snap_state.json").write_text(
        json.dumps(
            {
                "snap-secret-token": {
                    "status": "failed",
                    "phone": "+628123456789",
                    "midtrans_url": "https://app.midtrans.com/snap/v4/redirection/snap-secret-token",
                }
            }
        ),
        encoding="utf-8",
    )
    (config / "payment_tasks.json").write_text(
        json.dumps({"jobs": [{"id": "legacy-task", "status": "running", "pin": "147258"}]}),
        encoding="utf-8",
    )
    (config / "sms.env").write_text("OPAI_SMSBOWER_API_KEY=sms-secret-key\n", encoding="utf-8")


def test_legacy_import_is_encrypted_and_idempotent(tmp_path: Path, database):
    _engine, session_factory, codec = database
    legacy_root = tmp_path / "legacy"
    _write_legacy_fixture(legacy_root)

    preview = inspect_legacy(legacy_root)
    assert preview.valid is True
    assert preview.counts == {"accounts": 1, "phones": 1, "payments": 1, "tasks": 1, "sms": 1}

    importer = LegacyImporter(session_factory, codec)
    first = importer.apply(preview)
    assert first.imported == {"accounts": 1, "phones": 1, "payments": 1, "tasks": 1, "sms": 1}
    assert count_imported_rows(session_factory) == {
        "accounts": 1,
        "phones": 1,
        "payments": 1,
        "tasks": 1,
        "sms": 1,
    }

    with session_factory() as session:
        account_secret = session.scalar(select(AccountSecret.secret_payload_ciphertext))
        sms_secret = session.scalar(select(Setting.value_ciphertext))
        assert "access-secret-value" not in account_secret
        assert "147258" not in account_secret
        assert "sms-secret-key" not in sms_secret
        assert "access-secret-value" in codec.decrypt(
            account_secret,
            context=f"account:{session.scalar(select(AccountSecret.account_id))}",
        )

    second = importer.apply(preview)
    assert second.imported == {}
    assert second.skipped == {"accounts": 1, "phones": 1, "payments": 1, "tasks": 1, "sms": 1}
