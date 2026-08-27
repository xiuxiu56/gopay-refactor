"""P0 数据模型。"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


class Admin(Base):
    __tablename__ = "admins"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    singleton_key: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    username: Mapped[str] = mapped_column(String(64, collation="NOCASE"), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("singleton_key", name="uq_admin_singleton"),
        UniqueConstraint("username", name="uq_admin_username"),
        CheckConstraint("singleton_key = 1", name="ck_admin_singleton"),
    )


class WebSession(Base):
    __tablename__ = "web_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    admin_id: Mapped[str] = mapped_column(
        ForeignKey("admins.id", ondelete="CASCADE", name="fk_web_session_admin"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    csrf_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_web_session_token"),
        Index("idx_web_sessions_expiry", "expires_at"),
    )


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    phone: Mapped[str] = mapped_column(String(40), nullable=False)
    phone_normalized: Mapped[str] = mapped_column(String(32), nullable=False)
    local_phone: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    customer_id: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    remote_account_id: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    balance: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pin_setup_status: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    pin_change_status: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    pin_change_message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    sms_activation_status: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    payment_fingerprint_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    registered_at: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    __table_args__ = (
        UniqueConstraint("phone_normalized", name="uq_account_phone_normalized"),
        Index("idx_accounts_status_balance", "pin_setup_status", "balance"),
    )


class AccountSecret(Base):
    __tablename__ = "account_secrets"

    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE", name="fk_account_secret_account"), primary_key=True
    )
    secret_payload_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class PhoneNumber(Base):
    __tablename__ = "phone_numbers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    phone: Mapped[str] = mapped_column(String(40), nullable=False)
    phone_normalized: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(32), default="imported", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="available", nullable=False)
    sms_url_ciphertext: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    __table_args__ = (UniqueConstraint("phone_normalized", name="uq_phone_number_normalized"),)


class SmsActivation(Base):
    __tablename__ = "sms_activations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL", name="fk_sms_activation_account")
    )
    phone_number_id: Mapped[str | None] = mapped_column(
        ForeignKey("phone_numbers.id", ondelete="SET NULL", name="fk_sms_activation_phone")
    )
    provider: Mapped[str] = mapped_column(String(32), default="smsbower", nullable=False)
    provider_activation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    consumed_code_hashes_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("provider", "provider_activation_id", name="uq_sms_activation_provider_id"),
        Index("idx_sms_activation_status", "status", "updated_at"),
    )


class PaymentIntent(Base):
    __tablename__ = "payment_intents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    snap_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    order_id: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    account_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL", name="fk_payment_intent_account")
    )
    task_id: Mapped[str | None] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    amount: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="IDR", nullable=False)
    transaction_status: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    last_error_message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    midtrans_url_ciphertext: Mapped[str] = mapped_column(Text, default="", nullable=False)
    raw_state_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("snap_token_hash", name="uq_payment_intent_snap"),
        Index("idx_payment_intents_status_updated", "status", "updated_at"),
        Index("idx_payment_intents_task", "task_id"),
    )


class TaskBatch(Base):
    __tablename__ = "task_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_type: Mapped[str] = mapped_column(String(48), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    desired_concurrency: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    succeeded: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    strategy: Mapped[str] = mapped_column(String(32), default="fixed", nullable=False)
    plan_ciphertext: Mapped[str] = mapped_column(Text, default="", nullable=False)
    next_sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    batch_id: Mapped[str | None] = mapped_column(
        ForeignKey("task_batches.id", ondelete="SET NULL", name="fk_task_batch")
    )
    task_type: Mapped[str] = mapped_column(String(48), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    progress: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    payload_ciphertext: Mapped[str] = mapped_column(Text, default="", nullable=False)
    checkpoint_ciphertext: Mapped[str] = mapped_column(Text, default="", nullable=False)
    result_ciphertext: Mapped[str] = mapped_column(Text, default="", nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(160))
    attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    locked_by: Mapped[str] = mapped_column(String(96), default="", nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str] = mapped_column(String(96), default="", nullable=False)
    last_error_message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_task_idempotency"),
        Index("idx_tasks_claim", "status", "run_after", "priority", "created_at"),
        Index("idx_tasks_lock", "status", "locked_until"),
    )


class TaskAttempt(Base):
    __tablename__ = "task_attempts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE", name="fk_task_attempt_task"), nullable=False
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str] = mapped_column(String(96), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("task_id", "attempt", name="uq_task_attempt"),)


class TaskEvent(Base):
    __tablename__ = "task_events"

    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, nullable=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE", name="fk_task_event_task"), nullable=False
    )
    level: Mapped[str] = mapped_column(String(16), default="info", nullable=False)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    # SQLite 自增主键在反射时会被识别为可空。关闭隐式 RETURNING，
    # 避免 SQLAlchemy 批量写入任务事件时将该列误判为 sentinel。
    __table_args__ = (
        Index("idx_task_events_task_sequence", "task_id", "sequence"),
        {"implicit_returning": False},
    )


class TaskInput(Base):
    __tablename__ = "task_inputs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE", name="fk_task_input_task"), nullable=False
    )
    input_type: Mapped[str] = mapped_column(String(32), nullable=False)
    value_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    value_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    __table_args__ = (Index("idx_task_inputs_pending", "task_id", "input_type", "consumed_at"),)


class ResourceLease(Base):
    __tablename__ = "resource_leases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_key: Mapped[str] = mapped_column(String(160), nullable=False)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE", name="fk_resource_lease_task"), nullable=False
    )
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("resource_type", "resource_key", name="uq_resource_lease"),
        Index("idx_resource_lease_expiry", "expires_at"),
    )


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(160), primary_key=True)
    value_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    value_ciphertext: Mapped[str] = mapped_column(Text, default="", nullable=False)
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class ChangeLog(Base):
    __tablename__ = "change_log"

    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, nullable=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource: Mapped[str] = mapped_column(String(48), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    __table_args__ = (
        Index("idx_change_log_created", "created_at"),
        {"implicit_returning": False},
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(36), default="", nullable=False)
    resource_type: Mapped[str] = mapped_column(String(48), default="", nullable=False)
    resource_id: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    detail_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    __table_args__ = (Index("idx_audit_events_created", "created_at"),)


class LegacyImport(Base):
    __tablename__ = "legacy_imports"

    source_file: Mapped[str] = mapped_column(Text, primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    record_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
