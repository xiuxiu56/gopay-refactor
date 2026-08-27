"""创建 P0 初始数据库结构。

Revision ID: 0001_p0_initial
Revises:
"""

from __future__ import annotations

from alembic import op

revision = "0001_p0_initial"
down_revision = None
branch_labels = None
depends_on = None


STATEMENTS = [
    """
    CREATE TABLE admins (
        id VARCHAR(36) PRIMARY KEY NOT NULL,
        singleton_key INTEGER NOT NULL DEFAULT 1,
        username VARCHAR(64) NOT NULL COLLATE NOCASE,
        password_hash TEXT NOT NULL,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        last_login_at DATETIME,
        CONSTRAINT uq_admin_singleton UNIQUE(singleton_key),
        CONSTRAINT uq_admin_username UNIQUE(username),
        CONSTRAINT ck_admin_singleton CHECK(singleton_key = 1)
    )
    """,
    """
    CREATE TABLE web_sessions (
        id VARCHAR(36) PRIMARY KEY NOT NULL,
        admin_id VARCHAR(36) NOT NULL,
        token_hash VARCHAR(64) NOT NULL,
        csrf_token_hash VARCHAR(64) NOT NULL,
        created_at DATETIME NOT NULL,
        last_seen_at DATETIME NOT NULL,
        expires_at DATETIME NOT NULL,
        CONSTRAINT fk_web_session_admin FOREIGN KEY(admin_id) REFERENCES admins(id) ON DELETE CASCADE,
        CONSTRAINT uq_web_session_token UNIQUE(token_hash)
    )
    """,
    "CREATE INDEX idx_web_sessions_expiry ON web_sessions(expires_at)",
    """
    CREATE TABLE accounts (
        id VARCHAR(36) PRIMARY KEY NOT NULL,
        phone VARCHAR(40) NOT NULL,
        phone_normalized VARCHAR(32) NOT NULL,
        local_phone VARCHAR(32) NOT NULL DEFAULT '',
        customer_id VARCHAR(128) NOT NULL DEFAULT '',
        remote_account_id VARCHAR(128) NOT NULL DEFAULT '',
        balance INTEGER NOT NULL DEFAULT 0,
        pin_setup_status VARCHAR(32) NOT NULL DEFAULT 'unknown',
        pin_change_status VARCHAR(32) NOT NULL DEFAULT '',
        pin_change_message TEXT NOT NULL DEFAULT '',
        sms_activation_status VARCHAR(32) NOT NULL DEFAULT 'unknown',
        payment_fingerprint_json TEXT NOT NULL DEFAULT '{}',
        registered_at VARCHAR(64) NOT NULL DEFAULT '',
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        version INTEGER NOT NULL DEFAULT 1,
        CONSTRAINT uq_account_phone_normalized UNIQUE(phone_normalized)
    )
    """,
    "CREATE INDEX idx_accounts_status_balance ON accounts(pin_setup_status, balance)",
    """
    CREATE TABLE account_secrets (
        account_id VARCHAR(36) PRIMARY KEY NOT NULL,
        secret_payload_ciphertext TEXT NOT NULL,
        updated_at DATETIME NOT NULL,
        CONSTRAINT fk_account_secret_account FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE phone_numbers (
        id VARCHAR(36) PRIMARY KEY NOT NULL,
        phone VARCHAR(40) NOT NULL,
        phone_normalized VARCHAR(32) NOT NULL,
        source VARCHAR(32) NOT NULL DEFAULT 'imported',
        status VARCHAR(32) NOT NULL DEFAULT 'available',
        sms_url_ciphertext TEXT NOT NULL DEFAULT '',
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        CONSTRAINT uq_phone_number_normalized UNIQUE(phone_normalized)
    )
    """,
    """
    CREATE TABLE sms_activations (
        id VARCHAR(36) PRIMARY KEY NOT NULL,
        account_id VARCHAR(36),
        phone_number_id VARCHAR(36),
        provider VARCHAR(32) NOT NULL DEFAULT 'smsbower',
        provider_activation_id VARCHAR(128) NOT NULL,
        status VARCHAR(32) NOT NULL DEFAULT 'unknown',
        consumed_code_hashes_json TEXT NOT NULL DEFAULT '[]',
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        CONSTRAINT uq_sms_activation_provider_id UNIQUE(provider, provider_activation_id),
        CONSTRAINT fk_sms_activation_account FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE SET NULL,
        CONSTRAINT fk_sms_activation_phone FOREIGN KEY(phone_number_id) REFERENCES phone_numbers(id) ON DELETE SET NULL
    )
    """,
    "CREATE INDEX idx_sms_activation_status ON sms_activations(status, updated_at)",
    """
    CREATE TABLE payment_intents (
        id VARCHAR(36) PRIMARY KEY NOT NULL,
        snap_token_hash VARCHAR(64) NOT NULL,
        order_id VARCHAR(160) NOT NULL DEFAULT '',
        account_id VARCHAR(36),
        status VARCHAR(32) NOT NULL DEFAULT 'unknown',
        midtrans_url_ciphertext TEXT NOT NULL DEFAULT '',
        raw_state_ciphertext TEXT NOT NULL,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        CONSTRAINT uq_payment_intent_snap UNIQUE(snap_token_hash),
        CONSTRAINT fk_payment_intent_account FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE SET NULL
    )
    """,
    "CREATE INDEX idx_payment_intents_status_updated ON payment_intents(status, updated_at)",
    """
    CREATE TABLE task_batches (
        id VARCHAR(36) PRIMARY KEY NOT NULL,
        task_type VARCHAR(48) NOT NULL,
        status VARCHAR(32) NOT NULL DEFAULT 'queued',
        total INTEGER NOT NULL DEFAULT 0,
        desired_concurrency INTEGER NOT NULL DEFAULT 1,
        succeeded INTEGER NOT NULL DEFAULT 0,
        failed INTEGER NOT NULL DEFAULT 0,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL
    )
    """,
    """
    CREATE TABLE tasks (
        id VARCHAR(36) PRIMARY KEY NOT NULL,
        batch_id VARCHAR(36),
        task_type VARCHAR(48) NOT NULL,
        status VARCHAR(32) NOT NULL DEFAULT 'queued',
        priority INTEGER NOT NULL DEFAULT 0,
        progress FLOAT NOT NULL DEFAULT 0,
        payload_ciphertext TEXT NOT NULL DEFAULT '',
        checkpoint_ciphertext TEXT NOT NULL DEFAULT '',
        result_ciphertext TEXT NOT NULL DEFAULT '',
        idempotency_key VARCHAR(160),
        attempt INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 3,
        run_after DATETIME NOT NULL,
        locked_by VARCHAR(96) NOT NULL DEFAULT '',
        locked_until DATETIME,
        last_error_code VARCHAR(96) NOT NULL DEFAULT '',
        last_error_message TEXT NOT NULL DEFAULT '',
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL,
        started_at DATETIME,
        finished_at DATETIME,
        CONSTRAINT uq_task_idempotency UNIQUE(idempotency_key),
        CONSTRAINT fk_task_batch FOREIGN KEY(batch_id) REFERENCES task_batches(id) ON DELETE SET NULL
    )
    """,
    "CREATE INDEX idx_tasks_claim ON tasks(status, run_after, priority, created_at)",
    "CREATE INDEX idx_tasks_lock ON tasks(status, locked_until)",
    """
    CREATE TABLE task_attempts (
        id VARCHAR(36) PRIMARY KEY NOT NULL,
        task_id VARCHAR(36) NOT NULL,
        attempt INTEGER NOT NULL,
        worker_id VARCHAR(96) NOT NULL DEFAULT '',
        status VARCHAR(32) NOT NULL,
        error_message TEXT NOT NULL DEFAULT '',
        started_at DATETIME NOT NULL,
        finished_at DATETIME,
        CONSTRAINT uq_task_attempt UNIQUE(task_id, attempt),
        CONSTRAINT fk_task_attempt_task FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE task_events (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id VARCHAR(36) NOT NULL,
        level VARCHAR(16) NOT NULL DEFAULT 'info',
        event_type VARCHAR(48) NOT NULL,
        message TEXT NOT NULL,
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at DATETIME NOT NULL,
        CONSTRAINT fk_task_event_task FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX idx_task_events_task_sequence ON task_events(task_id, sequence)",
    """
    CREATE TABLE task_inputs (
        id VARCHAR(36) PRIMARY KEY NOT NULL,
        task_id VARCHAR(36) NOT NULL,
        input_type VARCHAR(32) NOT NULL,
        value_ciphertext TEXT NOT NULL,
        value_hash VARCHAR(64) NOT NULL,
        consumed_at DATETIME,
        expires_at DATETIME NOT NULL,
        created_at DATETIME NOT NULL,
        CONSTRAINT fk_task_input_task FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX idx_task_inputs_pending ON task_inputs(task_id, input_type, consumed_at)",
    """
    CREATE TABLE resource_leases (
        id VARCHAR(36) PRIMARY KEY NOT NULL,
        resource_type VARCHAR(32) NOT NULL,
        resource_key VARCHAR(160) NOT NULL,
        task_id VARCHAR(36) NOT NULL,
        acquired_at DATETIME NOT NULL,
        expires_at DATETIME NOT NULL,
        CONSTRAINT uq_resource_lease UNIQUE(resource_type, resource_key),
        CONSTRAINT fk_resource_lease_task FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX idx_resource_lease_expiry ON resource_leases(expires_at)",
    """
    CREATE TABLE settings (
        key VARCHAR(160) PRIMARY KEY NOT NULL,
        value_text TEXT NOT NULL DEFAULT '',
        value_ciphertext TEXT NOT NULL DEFAULT '',
        is_secret BOOLEAN NOT NULL DEFAULT 0,
        updated_at DATETIME NOT NULL
    )
    """,
    """
    CREATE TABLE change_log (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type VARCHAR(64) NOT NULL,
        resource VARCHAR(48) NOT NULL,
        resource_id VARCHAR(160) NOT NULL DEFAULT '',
        operation VARCHAR(32) NOT NULL,
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at DATETIME NOT NULL
    )
    """,
    "CREATE INDEX idx_change_log_created ON change_log(created_at)",
    """
    CREATE TABLE audit_events (
        id VARCHAR(36) PRIMARY KEY NOT NULL,
        event_type VARCHAR(64) NOT NULL,
        actor_id VARCHAR(36) NOT NULL DEFAULT '',
        resource_type VARCHAR(48) NOT NULL DEFAULT '',
        resource_id VARCHAR(160) NOT NULL DEFAULT '',
        detail_json TEXT NOT NULL DEFAULT '{}',
        created_at DATETIME NOT NULL
    )
    """,
    "CREATE INDEX idx_audit_events_created ON audit_events(created_at)",
    """
    CREATE TABLE legacy_imports (
        source_file TEXT PRIMARY KEY NOT NULL,
        sha256 VARCHAR(64) NOT NULL,
        record_count INTEGER NOT NULL DEFAULT 0,
        imported_at DATETIME NOT NULL
    )
    """,
]


DROP_TABLES = [
    "legacy_imports",
    "audit_events",
    "change_log",
    "settings",
    "resource_leases",
    "task_inputs",
    "task_events",
    "task_attempts",
    "tasks",
    "task_batches",
    "payment_intents",
    "sms_activations",
    "phone_numbers",
    "account_secrets",
    "accounts",
    "web_sessions",
    "admins",
]


def upgrade() -> None:
    connection = op.get_bind()
    for statement in STATEMENTS:
        connection.exec_driver_sql(statement.strip())


def downgrade() -> None:
    connection = op.get_bind()
    for table_name in DROP_TABLES:
        connection.exec_driver_sql(f"DROP TABLE IF EXISTS {table_name}")
