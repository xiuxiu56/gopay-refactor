"""SQLite 持久化任务仓库与事务状态机。"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, aliased, sessionmaker

from gopay_app.db.models import (
    ChangeLog,
    PaymentIntent,
    ResourceLease,
    Task,
    TaskAttempt,
    TaskBatch,
    TaskEvent,
    TaskInput,
    utc_now,
)
from gopay_app.security.codec import SecretCodec

from .errors import TaskConflictError, TaskNotFoundError
from .registry import HandlerRegistry

CLAIMABLE_STATUSES = ("queued", "retry_wait")
TERMINAL_STATUSES = ("succeeded", "failed", "cancelled", "needs_review")
ALL_STATUSES = (*CLAIMABLE_STATUSES, "running", "waiting_input", *TERMINAL_STATUSES)
BATCH_SLOT_STATUSES = (*CLAIMABLE_STATUSES, "running")


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _json_load(value: str) -> dict[str, Any]:
    if not value:
        return {}
    try:
        result = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return result if isinstance(result, dict) else {"value": result}


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class TaskSnapshot:
    id: str
    batch_id: str | None
    task_type: str
    status: str
    priority: int
    progress: float
    attempt: int
    max_attempts: int
    run_after: datetime
    locked_by: str
    locked_until: datetime | None
    last_error_code: str
    last_error_message: str
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    has_payload: bool
    has_checkpoint: bool
    has_result: bool

    @classmethod
    def from_model(cls, task: Task) -> TaskSnapshot:
        return cls(
            id=task.id,
            batch_id=task.batch_id,
            task_type=task.task_type,
            status=task.status,
            priority=task.priority,
            progress=task.progress,
            attempt=task.attempt,
            max_attempts=task.max_attempts,
            run_after=task.run_after,
            locked_by=task.locked_by,
            locked_until=task.locked_until,
            last_error_code=task.last_error_code,
            last_error_message=task.last_error_message,
            created_at=task.created_at,
            updated_at=task.updated_at,
            started_at=task.started_at,
            finished_at=task.finished_at,
            has_payload=bool(task.payload_ciphertext),
            has_checkpoint=bool(task.checkpoint_ciphertext),
            has_result=bool(task.result_ciphertext),
        )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        for field in (
            "run_after",
            "locked_until",
            "created_at",
            "updated_at",
            "started_at",
            "finished_at",
        ):
            value[field] = _iso(value[field])
        return value


@dataclass(frozen=True, slots=True)
class TaskExecution:
    snapshot: TaskSnapshot
    payload: dict[str, Any]
    checkpoint: dict[str, Any]


class TaskRepository:
    """封装所有任务状态迁移，避免 Worker 直接改表。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        codec: SecretCodec,
        *,
        lease_seconds: int = 60,
        retry_base_seconds: float = 2.0,
        change_log_limit: int = 10_000,
    ) -> None:
        self._session_factory = session_factory
        self._codec = codec
        self._lease_seconds = lease_seconds
        self._retry_base_seconds = retry_base_seconds
        self._change_log_limit = change_log_limit

    @property
    def session_factory(self) -> sessionmaker[Session]:
        return self._session_factory

    @property
    def codec(self) -> SecretCodec:
        return self._codec

    def _encrypt_task_value(self, task_id: str, field: str, value: dict[str, Any]) -> str:
        if not value:
            return ""
        return self._codec.encrypt(_json_dump(value), context=f"task:{task_id}:{field}")

    def _decrypt_task_value(self, task_id: str, field: str, value: str) -> dict[str, Any]:
        return _json_load(self._codec.decrypt(value, context=f"task:{task_id}:{field}"))

    def _append_event(
        self,
        session: Session,
        task_id: str,
        event_type: str,
        message: str,
        *,
        level: str = "info",
        payload: dict[str, Any] | None = None,
    ) -> None:
        session.add(
            TaskEvent(
                task_id=task_id,
                level=level,
                event_type=event_type,
                message=message,
                payload_json=_json_dump(payload or {}),
                created_at=utc_now(),
            )
        )

    def _append_change(
        self,
        session: Session,
        task_id: str,
        operation: str,
        *,
        status: str,
        task_type: str,
    ) -> None:
        change = ChangeLog(
            event_type="task.updated",
            resource="task",
            resource_id=task_id,
            operation=operation,
            payload_json=_json_dump({"id": task_id, "status": status, "task_type": task_type}),
            created_at=utc_now(),
        )
        session.add(change)
        session.flush([change])
        if change.sequence and change.sequence % 100 == 0:
            self._prune_change_log(session)

    def _prune_change_log(self, session: Session) -> None:
        cutoff = session.scalar(
            select(ChangeLog.sequence)
            .order_by(ChangeLog.sequence.desc())
            .offset(self._change_log_limit)
            .limit(1)
        )
        if cutoff is not None:
            session.execute(delete(ChangeLog).where(ChangeLog.sequence <= cutoff))

    def create_task(
        self,
        task_type: str,
        payload: dict[str, Any] | None = None,
        *,
        priority: int = 0,
        max_attempts: int = 3,
        idempotency_key: str | None = None,
        run_after: datetime | None = None,
        batch_id: str | None = None,
    ) -> tuple[TaskSnapshot, bool]:
        normalized_type = task_type.strip().lower()
        normalized_key = (idempotency_key or "").strip() or None
        now = utc_now()
        with self._session_factory() as session, session.begin():
            if normalized_key:
                existing = session.scalar(select(Task).where(Task.idempotency_key == normalized_key))
                if existing is not None:
                    return TaskSnapshot.from_model(existing), False

            task_id = str(uuid.uuid4())
            task = Task(
                id=task_id,
                batch_id=batch_id,
                task_type=normalized_type,
                status="queued",
                priority=priority,
                progress=0,
                payload_ciphertext=self._encrypt_task_value(task_id, "payload", payload or {}),
                checkpoint_ciphertext="",
                result_ciphertext="",
                idempotency_key=normalized_key,
                attempt=0,
                max_attempts=max_attempts,
                run_after=run_after or now,
                locked_by="",
                locked_until=None,
                last_error_code="",
                last_error_message="",
                created_at=now,
                updated_at=now,
                started_at=None,
                finished_at=None,
            )
            session.add(task)
            self._append_event(session, task_id, "task.created", "任务已进入持久化队列")
            self._append_change(session, task_id, "create", status="queued", task_type=normalized_type)
            session.flush()
            self._prune_change_log(session)
            return TaskSnapshot.from_model(task), True

    def create_batch(
        self,
        task_type: str,
        payloads: list[dict[str, Any]],
        *,
        desired_concurrency: int = 1,
        priority: int = 0,
        max_attempts: int = 8,
        idempotency_prefix: str | None = None,
    ) -> tuple[dict[str, Any], list[TaskSnapshot]]:
        if not payloads:
            raise TaskConflictError("批次至少需要一个任务")
        normalized_type = task_type.strip().lower()
        batch_id = str(uuid.uuid4())
        now = utc_now()
        effective_concurrency = min(max(1, desired_concurrency), len(payloads))
        snapshots: list[TaskSnapshot] = []
        with self._session_factory() as session, session.begin():
            batch = TaskBatch(
                id=batch_id,
                task_type=normalized_type,
                status="queued",
                total=len(payloads),
                desired_concurrency=effective_concurrency,
                succeeded=0,
                failed=0,
                created_at=now,
                updated_at=now,
            )
            session.add(batch)
            session.flush([batch])
            for index, payload in enumerate(payloads, 1):
                task_id = str(uuid.uuid4())
                task = Task(
                    id=task_id,
                    batch_id=batch_id,
                    task_type=normalized_type,
                    status="queued",
                    priority=priority,
                    progress=0,
                    payload_ciphertext=self._encrypt_task_value(task_id, "payload", payload),
                    checkpoint_ciphertext="",
                    result_ciphertext="",
                    idempotency_key=(f"{idempotency_prefix}:{index}" if idempotency_prefix else None),
                    attempt=0,
                    max_attempts=max_attempts,
                    run_after=now,
                    locked_by="",
                    locked_until=None,
                    last_error_code="",
                    last_error_message="",
                    created_at=now,
                    updated_at=now,
                    started_at=None,
                    finished_at=None,
                )
                session.add(task)
                self._append_event(session, task_id, "task.created", "任务已进入持久化批次")
                self._append_change(
                    session,
                    task_id,
                    "create",
                    status="queued",
                    task_type=normalized_type,
                )
                snapshots.append(TaskSnapshot.from_model(task))
            session.flush()
            self._prune_change_log(session)
        return (
            {
                "id": batch_id,
                "task_type": normalized_type,
                "status": "queued",
                "total": len(payloads),
                "desired_concurrency": effective_concurrency,
                "strategy": "fixed",
                "created": len(payloads),
            },
            snapshots,
        )

    def _rolling_plan(self, batch: TaskBatch) -> dict[str, Any]:
        if not batch.plan_ciphertext:
            return {}
        return _json_load(
            self._codec.decrypt(
                batch.plan_ciphertext,
                context=f"task-batch:{batch.id}:plan",
            )
        )

    def _create_rolling_task(
        self,
        session: Session,
        batch: TaskBatch,
        plan: dict[str, Any],
        sequence: int,
    ) -> TaskSnapshot:
        payloads = plan.get("payloads")
        if not isinstance(payloads, list) or sequence < 1 or sequence > len(payloads):
            raise TaskConflictError("滚动批次计划已经损坏")
        payload = payloads[sequence - 1]
        if not isinstance(payload, dict):
            raise TaskConflictError("滚动批次任务参数格式不正确")
        task_id = str(uuid.uuid4())
        now = utc_now()
        prefix = str(plan.get("idempotency_prefix") or f"rolling:{batch.id}")
        task = Task(
            id=task_id,
            batch_id=batch.id,
            task_type=batch.task_type,
            status="queued",
            priority=int(plan.get("priority") or 0),
            progress=0,
            payload_ciphertext=self._encrypt_task_value(task_id, "payload", payload),
            checkpoint_ciphertext="",
            result_ciphertext="",
            idempotency_key=f"{prefix}:{sequence}",
            attempt=0,
            max_attempts=max(1, int(plan.get("max_attempts") or 1)),
            run_after=now,
            locked_by="",
            locked_until=None,
            last_error_code="",
            last_error_message="",
            created_at=now,
            updated_at=now,
            started_at=None,
            finished_at=None,
        )
        session.add(task)
        batch.next_sequence = sequence
        batch.updated_at = now
        self._append_event(
            session,
            task_id,
            "task.created",
            f"滚动批次已补充第 {sequence}/{batch.total} 条任务",
        )
        self._append_change(
            session,
            task_id,
            "create",
            status="queued",
            task_type=batch.task_type,
        )
        return TaskSnapshot.from_model(task)

    def _replenish_rolling_batch(
        self,
        session: Session,
        batch: TaskBatch,
    ) -> list[TaskSnapshot]:
        if batch.strategy != "rolling" or batch.status == "stopped":
            return []
        session.flush()
        active = (
            session.scalar(
                select(func.count())
                .select_from(Task)
                .where(Task.batch_id == batch.id, Task.status.in_(BATCH_SLOT_STATUSES))
            )
            or 0
        )
        remaining = max(0, batch.total - batch.next_sequence)
        available_slots = max(0, batch.desired_concurrency - int(active))
        create_count = min(remaining, available_slots)
        if not create_count:
            return []
        plan = self._rolling_plan(batch)
        snapshots = [
            self._create_rolling_task(session, batch, plan, batch.next_sequence + 1)
            for _index in range(create_count)
        ]
        session.flush()
        return snapshots

    def create_rolling_batch(
        self,
        task_type: str,
        payloads: list[dict[str, Any]],
        *,
        desired_concurrency: int = 1,
        priority: int = 0,
        max_attempts: int = 1,
        idempotency_prefix: str | None = None,
        exclusive_task_types: tuple[str, ...] = (),
    ) -> tuple[dict[str, Any], list[TaskSnapshot]]:
        """只创建当前并发所需任务，任务结束后再持久化补充下一条。"""
        if not payloads:
            raise TaskConflictError("滚动批次至少需要一个任务")
        normalized_type = task_type.strip().lower()
        batch_id = str(uuid.uuid4())
        now = utc_now()
        effective_concurrency = min(50, max(1, desired_concurrency))
        plan = {
            "payloads": payloads,
            "priority": priority,
            "max_attempts": max(1, max_attempts),
            "idempotency_prefix": idempotency_prefix or f"rolling:{batch_id}",
        }
        with self._session_factory() as session, session.begin():
            if exclusive_task_types:
                active = session.scalar(
                    select(TaskBatch)
                    .where(
                        TaskBatch.task_type.in_(exclusive_task_types),
                        TaskBatch.status.in_(("queued", "running")),
                    )
                    .order_by(TaskBatch.created_at.desc())
                    .limit(1)
                )
                if active is not None:
                    raise TaskConflictError("已有注册或登录任务正在运行，请先停止当前任务")
            batch = TaskBatch(
                id=batch_id,
                task_type=normalized_type,
                status="queued",
                total=len(payloads),
                desired_concurrency=effective_concurrency,
                succeeded=0,
                failed=0,
                strategy="rolling",
                plan_ciphertext=self._codec.encrypt(
                    _json_dump(plan),
                    context=f"task-batch:{batch_id}:plan",
                ),
                next_sequence=0,
                created_at=now,
                updated_at=now,
            )
            session.add(batch)
            session.flush([batch])
            snapshots = self._replenish_rolling_batch(session, batch)
            self._prune_change_log(session)
        return (
            {
                "id": batch_id,
                "task_type": normalized_type,
                "status": "queued",
                "total": len(payloads),
                "desired_concurrency": effective_concurrency,
                "strategy": "rolling",
                "created": len(snapshots),
            },
            snapshots,
        )

    def append_active_rolling_batch(
        self,
        task_type: str,
        payloads: list[dict[str, Any]],
        *,
        desired_concurrency: int = 1,
    ) -> tuple[dict[str, Any], list[TaskSnapshot]] | None:
        """向当前同类型滚动批次追加任务；没有活动批次时返回空值。"""
        if not payloads:
            raise TaskConflictError("追加任务参数不能为空")
        normalized_type = task_type.strip().lower()
        with self._session_factory() as session, session.begin():
            active = session.scalar(
                select(TaskBatch)
                .where(
                    TaskBatch.task_type == normalized_type,
                    TaskBatch.status.in_(("queued", "running")),
                    TaskBatch.strategy == "rolling",
                    TaskBatch.plan_ciphertext != "",
                )
                .order_by(TaskBatch.created_at.desc())
                .limit(1)
            )
            if active is None:
                return None

            plan = self._rolling_plan(active)
            planned_payloads = plan.get("payloads")
            if not isinstance(planned_payloads, list):
                raise TaskConflictError("滚动批次计划已经损坏")
            planned_payloads.extend(payloads)
            plan["payloads"] = planned_payloads
            active.total = len(planned_payloads)
            active.desired_concurrency = min(
                50,
                max(active.desired_concurrency, max(1, desired_concurrency)),
            )
            active.plan_ciphertext = self._codec.encrypt(
                _json_dump(plan),
                context=f"task-batch:{active.id}:plan",
            )
            active.updated_at = utc_now()
            snapshots = self._replenish_rolling_batch(session, active)
            self._prune_change_log(session)
            return (
                {
                    "id": active.id,
                    "task_type": active.task_type,
                    "status": active.status,
                    "total": active.total,
                    "desired_concurrency": active.desired_concurrency,
                    "strategy": active.strategy,
                    "created": len(snapshots),
                    "appended": len(payloads),
                },
                snapshots,
            )

    def _refresh_batch(self, session: Session, batch_id: str | None) -> None:
        if not batch_id:
            return
        batch = session.get(TaskBatch, batch_id)
        if batch is None:
            return
        # Session 关闭了自动 flush；先写入当前任务终态，再统计批次，否则页面会一直显示运行中。
        session.flush()
        rows = session.execute(
            select(Task.status, func.count()).where(Task.batch_id == batch_id).group_by(Task.status)
        ).all()
        counts = {str(status): int(count) for status, count in rows}
        batch.succeeded = counts.get("succeeded", 0)
        batch.failed = sum(counts.get(status, 0) for status in ("failed", "cancelled", "needs_review"))
        terminal = batch.succeeded + batch.failed
        if batch.strategy == "rolling":
            if batch.status != "stopped":
                self._replenish_rolling_batch(session, batch)
                terminal = batch.succeeded + batch.failed
                if batch.next_sequence >= batch.total and terminal >= batch.total:
                    batch.status = "succeeded" if batch.failed == 0 else "failed"
                    batch.plan_ciphertext = ""
                elif batch.next_sequence:
                    batch.status = "running"
                else:
                    batch.status = "queued"
            batch.updated_at = utc_now()
            return
        if terminal >= batch.total:
            batch.status = "succeeded" if batch.failed == 0 else "failed"
        elif any(counts.get(status, 0) for status in ("running", "waiting_input", "retry_wait")):
            batch.status = "running"
        else:
            batch.status = "queued"
        batch.updated_at = utc_now()

    def _batch_summary(self, session: Session, batch: TaskBatch) -> dict[str, Any]:
        active = (
            session.scalar(
                select(func.count())
                .select_from(Task)
                .where(Task.batch_id == batch.id, Task.status.in_(BATCH_SLOT_STATUSES))
            )
            or 0
        )
        return {
            "id": batch.id,
            "task_type": batch.task_type,
            "mode": batch.task_type.removeprefix("account."),
            "status": batch.status,
            "strategy": batch.strategy,
            "target": batch.total,
            "created": batch.next_sequence if batch.strategy == "rolling" else batch.total,
            "active": int(active),
            "succeeded": batch.succeeded,
            "failed": batch.failed,
            "remaining": max(0, batch.total - batch.succeeded - batch.failed),
            "desired_concurrency": batch.desired_concurrency,
            "created_at": _iso(batch.created_at),
            "updated_at": _iso(batch.updated_at),
        }

    def account_flow_run_state(self) -> dict[str, Any]:
        """返回当前独占运行批次和各模式最近一次批次。"""
        task_types = ("account.register", "account.login")
        with self._session_factory() as session:
            rows = session.scalars(
                select(TaskBatch)
                .where(TaskBatch.task_type.in_(task_types))
                .order_by(TaskBatch.created_at.desc(), TaskBatch.id.desc())
            ).all()
            latest: dict[str, dict[str, Any]] = {}
            current: dict[str, Any] | None = None
            for batch in rows:
                mode = batch.task_type.removeprefix("account.")
                if mode not in latest:
                    latest[mode] = self._batch_summary(session, batch)
                if current is None and batch.status in {"queued", "running"}:
                    current = self._batch_summary(session, batch)
            return {"current": current, "latest": latest}

    def stop_account_flow_run(self, batch_id: str) -> dict[str, Any]:
        """停止一个注册或登录批次，并取消尚未完成的任务。"""
        now = utc_now()
        with self._session_factory() as session, session.begin():
            batch = session.get(TaskBatch, batch_id)
            if batch is None or batch.task_type not in {"account.register", "account.login"}:
                raise TaskNotFoundError("注册或登录运行批次不存在")
            if batch.status not in {"queued", "running"}:
                raise TaskConflictError("注册或登录运行批次已经结束")
            batch.status = "stopped"
            batch.plan_ciphertext = ""
            batch.updated_at = now
            tasks = session.scalars(
                select(Task).where(
                    Task.batch_id == batch.id,
                    Task.status.not_in(TERMINAL_STATUSES),
                )
            ).all()
            for task in tasks:
                task.status = "cancelled"
                task.locked_by = ""
                task.locked_until = None
                task.updated_at = now
                task.finished_at = now
                if task.attempt:
                    attempt = session.scalar(
                        select(TaskAttempt).where(
                            TaskAttempt.task_id == task.id,
                            TaskAttempt.attempt == task.attempt,
                        )
                    )
                    if attempt is not None and attempt.finished_at is None:
                        attempt.status = "cancelled"
                        attempt.finished_at = now
                session.execute(delete(ResourceLease).where(ResourceLease.task_id == task.id))
                session.execute(
                    update(TaskInput)
                    .where(TaskInput.task_id == task.id, TaskInput.consumed_at.is_(None))
                    .values(value_ciphertext="", consumed_at=now)
                )
                self._append_event(session, task.id, "task.cancelled", "运行批次已由用户停止")
                self._append_change(
                    session,
                    task.id,
                    "cancel",
                    status="cancelled",
                    task_type=task.task_type,
                )
            self._refresh_batch(session, batch.id)
            summary = self._batch_summary(session, batch)
            summary["stopped_tasks"] = len(tasks)
            return summary

    def get_task(self, task_id: str) -> TaskSnapshot:
        with self._session_factory() as session:
            task = session.get(Task, task_id)
            if task is None:
                raise TaskNotFoundError("任务不存在")
            return TaskSnapshot.from_model(task)

    def get_execution(self, task_id: str) -> TaskExecution:
        with self._session_factory() as session:
            task = session.get(Task, task_id)
            if task is None:
                raise TaskNotFoundError("任务不存在")
            return TaskExecution(
                snapshot=TaskSnapshot.from_model(task),
                payload=self._decrypt_task_value(task.id, "payload", task.payload_ciphertext),
                checkpoint=self._decrypt_task_value(task.id, "checkpoint", task.checkpoint_ciphertext),
            )

    def consume_result(self, task_id: str) -> dict[str, Any]:
        """读取并清除已成功任务的加密结果，供一次性敏感结果使用。"""
        with self._session_factory() as session, session.begin():
            task = session.get(Task, task_id)
            if task is None:
                raise TaskNotFoundError("任务不存在")
            if task.status != "succeeded":
                raise TaskConflictError("任务尚未成功完成")
            if not task.result_ciphertext:
                raise TaskConflictError("任务结果已经读取或清理")
            result = self._decrypt_task_value(task.id, "result", task.result_ciphertext)
            task.result_ciphertext = ""
            task.updated_at = utc_now()
            self._append_event(
                session,
                task.id,
                "task.result_consumed",
                "一次性任务结果已读取并清理",
            )
            self._append_change(
                session,
                task.id,
                "consume_result",
                status=task.status,
                task_type=task.task_type,
            )
            return result

    def list_tasks(
        self,
        *,
        status: str = "",
        task_type: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[TaskSnapshot], int]:
        filters = []
        if status:
            filters.append(Task.status == status)
        if task_type:
            filters.append(Task.task_type == task_type)
        with self._session_factory() as session:
            total = session.scalar(select(func.count()).select_from(Task).where(*filters)) or 0
            rows = session.scalars(
                select(Task)
                .where(*filters)
                .order_by(Task.created_at.desc(), Task.id.desc())
                .offset(offset)
                .limit(limit)
            ).all()
            return [TaskSnapshot.from_model(row) for row in rows], int(total)

    def list_account_flow_logs(
        self,
        *,
        limit: int | None = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """只暴露注册与登录日志所需的手机号，不返回 PIN、代理或令牌。"""
        task_types = ("account.register", "account.login")
        with self._session_factory() as session:
            latest_event_message = (
                select(TaskEvent.message)
                .where(TaskEvent.task_id == Task.id)
                .order_by(TaskEvent.sequence.desc())
                .limit(1)
                .correlate(Task)
                .scalar_subquery()
            )
            total = (
                session.scalar(select(func.count()).select_from(Task).where(Task.task_type.in_(task_types)))
                or 0
            )
            statement = (
                select(Task, latest_event_message.label("latest_event_message"))
                .where(Task.task_type.in_(task_types))
                .order_by(Task.created_at.desc(), Task.id.desc())
            )
            if limit is not None:
                statement = statement.offset(offset).limit(limit)
            rows = session.execute(statement).all()
            items: list[dict[str, Any]] = []
            for row, latest_message in rows:
                payload = self._decrypt_task_value(row.id, "payload", row.payload_ciphertext)
                checkpoint = self._decrypt_task_value(
                    row.id,
                    "checkpoint",
                    row.checkpoint_ciphertext,
                )
                item = TaskSnapshot.from_model(row).to_dict()
                item["phone"] = str(checkpoint.get("phone") or payload.get("phone") or "")
                item["phone_source"] = str(
                    checkpoint.get("phone_source") or payload.get("phone_source") or "manual"
                )
                item["latest_event_message"] = str(latest_message or "")
                items.append(item)
            return items, int(total)

    def account_flow_active_counts(self) -> dict[str, int]:
        """返回注册与登录任务的活动数量。"""
        with self._session_factory() as session:
            rows = session.execute(
                select(Task.task_type, func.count())
                .where(
                    Task.task_type.in_(("account.register", "account.login")),
                    Task.status.not_in(TERMINAL_STATUSES),
                )
                .group_by(Task.task_type)
            ).all()
        counts = {str(task_type): int(count) for task_type, count in rows}
        return {
            "register": counts.get("account.register", 0),
            "login": counts.get("account.login", 0),
        }

    def clear_account_flow_logs(self) -> dict[str, int]:
        """停止并清理全部注册与登录任务及其阶段日志。"""
        task_types = ("account.register", "account.login")
        with self._session_factory() as session, session.begin():
            rows = session.execute(
                select(Task.id, Task.batch_id, Task.status).where(Task.task_type.in_(task_types))
            ).all()
            task_ids = [str(row.id) for row in rows]
            batch_ids = {str(row.batch_id) for row in rows if row.batch_id}
            active_removed = sum(1 for row in rows if row.status not in TERMINAL_STATUSES)
            if task_ids:
                session.execute(delete(Task).where(Task.id.in_(task_ids)))
                session.flush()
            removed_batches = 0
            for batch_id in batch_ids:
                remaining = session.scalar(
                    select(func.count()).select_from(Task).where(Task.batch_id == batch_id)
                )
                if not remaining:
                    result = session.execute(delete(TaskBatch).where(TaskBatch.id == batch_id))
                    removed_batches += int(result.rowcount or 0)
            if task_ids:
                session.add(
                    ChangeLog(
                        event_type="task.updated",
                        resource="task",
                        resource_id="account-flow-logs",
                        operation="clear",
                        payload_json=_json_dump({"removed": len(task_ids), "active_removed": active_removed}),
                        created_at=utc_now(),
                    )
                )
            return {
                "removed": len(task_ids),
                "removed_batches": removed_batches,
                "active_removed": active_removed,
                "active_retained": 0,
            }

    def clear_payment_logs(self) -> dict[str, int]:
        """停止支付任务并清理全部支付记录及其阶段日志。"""
        task_types = ("payment.execute", "payment.reconcile")
        with self._session_factory() as session, session.begin():
            task_rows = session.execute(
                select(Task.id, Task.batch_id, Task.status).where(Task.task_type.in_(task_types))
            ).all()
            task_ids = [str(row.id) for row in task_rows]
            batch_ids = {str(row.batch_id) for row in task_rows if row.batch_id}
            active_tasks_removed = sum(
                1 for row in task_rows if row.status not in TERMINAL_STATUSES
            )
            payment_count = (
                session.scalar(select(func.count()).select_from(PaymentIntent)) or 0
            )

            if payment_count:
                session.execute(delete(PaymentIntent))
            if task_ids:
                session.execute(delete(Task).where(Task.id.in_(task_ids)))
                session.flush()

            removed_batches = 0
            for batch_id in batch_ids:
                remaining = session.scalar(
                    select(func.count()).select_from(Task).where(Task.batch_id == batch_id)
                )
                if not remaining:
                    result = session.execute(delete(TaskBatch).where(TaskBatch.id == batch_id))
                    removed_batches += int(result.rowcount or 0)

            if payment_count or task_ids:
                session.add(
                    ChangeLog(
                        event_type="payment.updated",
                        resource="payment",
                        resource_id="payment-logs",
                        operation="clear",
                        payload_json=_json_dump(
                            {
                                "removed": int(payment_count),
                                "tasks_removed": len(task_ids),
                                "active_tasks_removed": active_tasks_removed,
                            }
                        ),
                        created_at=utc_now(),
                    )
                )
            return {
                "removed": int(payment_count),
                "tasks_removed": len(task_ids),
                "removed_batches": removed_batches,
                "active_tasks_removed": active_tasks_removed,
            }

    def list_events(self, task_id: str, *, after: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            if session.get(Task, task_id) is None:
                raise TaskNotFoundError("任务不存在")
            rows = session.scalars(
                select(TaskEvent)
                .where(TaskEvent.task_id == task_id, TaskEvent.sequence > after)
                .order_by(TaskEvent.sequence)
                .limit(limit)
            ).all()
            return [
                {
                    "sequence": row.sequence,
                    "level": row.level,
                    "event_type": row.event_type,
                    "message": row.message,
                    "payload": _json_load(row.payload_json),
                    "created_at": _iso(row.created_at),
                }
                for row in rows
            ]

    def claim_next(self, worker_id: str) -> TaskExecution | None:
        now = utc_now()
        locked_until = now + timedelta(seconds=self._lease_seconds)
        running_task = aliased(Task)
        running_in_batch = (
            select(func.count(running_task.id))
            .where(running_task.batch_id == Task.batch_id, running_task.status == "running")
            .correlate(Task)
            .scalar_subquery()
        )
        batch_limit = (
            select(TaskBatch.desired_concurrency)
            .where(TaskBatch.id == Task.batch_id)
            .correlate(Task)
            .scalar_subquery()
        )
        candidate = (
            select(Task.id)
            .where(
                Task.status.in_(CLAIMABLE_STATUSES),
                Task.run_after <= now,
                or_(Task.batch_id.is_(None), running_in_batch < batch_limit),
            )
            .order_by(Task.priority.desc(), Task.created_at, Task.id)
            .limit(1)
            .scalar_subquery()
        )
        statement = (
            update(Task)
            .where(Task.id == candidate, Task.status.in_(CLAIMABLE_STATUSES))
            .values(
                status="running",
                attempt=Task.attempt + 1,
                locked_by=worker_id,
                locked_until=locked_until,
                started_at=func.coalesce(Task.started_at, now),
                finished_at=None,
                updated_at=now,
                last_error_code="",
                last_error_message="",
            )
            .returning(Task.id)
        )
        with self._session_factory() as session, session.begin():
            task_id = session.execute(statement).scalar_one_or_none()
            if task_id is None:
                return None
            task = session.get(Task, task_id)
            if task is None:  # pragma: no cover
                return None
            session.add(
                TaskAttempt(
                    id=str(uuid.uuid4()),
                    task_id=task.id,
                    attempt=task.attempt,
                    worker_id=worker_id,
                    status="running",
                    error_message="",
                    started_at=now,
                    finished_at=None,
                )
            )
            self._append_event(
                session,
                task.id,
                "task.claimed",
                "任务已被 Worker 领取",
                payload={"attempt": task.attempt},
            )
            self._append_change(session, task.id, "claim", status="running", task_type=task.task_type)
            self._refresh_batch(session, task.batch_id)
            return TaskExecution(
                snapshot=TaskSnapshot.from_model(task),
                payload=self._decrypt_task_value(task.id, "payload", task.payload_ciphertext),
                checkpoint=self._decrypt_task_value(task.id, "checkpoint", task.checkpoint_ciphertext),
            )

    def heartbeat(self, task_id: str, worker_id: str) -> bool:
        now = utc_now()
        with self._session_factory() as session, session.begin():
            result = session.execute(
                update(Task)
                .where(
                    Task.id == task_id,
                    Task.status == "running",
                    Task.locked_by == worker_id,
                )
                .values(
                    locked_until=now + timedelta(seconds=self._lease_seconds),
                    updated_at=now,
                )
            )
            session.execute(
                update(ResourceLease)
                .where(ResourceLease.task_id == task_id)
                .values(expires_at=now + timedelta(seconds=self._lease_seconds))
            )
            return bool(result.rowcount)

    def is_running(self, task_id: str, worker_id: str) -> bool:
        with self._session_factory() as session:
            return bool(
                session.scalar(
                    select(func.count())
                    .select_from(Task)
                    .where(
                        Task.id == task_id,
                        Task.status == "running",
                        Task.locked_by == worker_id,
                    )
                )
            )

    def update_progress(
        self,
        task_id: str,
        worker_id: str,
        progress: float,
        message: str = "",
    ) -> bool:
        now = utc_now()
        normalized = max(0.0, min(1.0, float(progress)))
        with self._session_factory() as session, session.begin():
            task = session.scalar(
                select(Task).where(
                    Task.id == task_id,
                    Task.status == "running",
                    Task.locked_by == worker_id,
                )
            )
            if task is None:
                return False
            task.progress = normalized
            task.updated_at = now
            if message:
                self._append_event(session, task.id, "task.progress", message)
            self._append_change(session, task.id, "progress", status=task.status, task_type=task.task_type)
            return True

    def save_checkpoint(
        self,
        task_id: str,
        worker_id: str,
        checkpoint: dict[str, Any],
    ) -> bool:
        with self._session_factory() as session, session.begin():
            result = session.execute(
                update(Task)
                .where(
                    Task.id == task_id,
                    Task.status == "running",
                    Task.locked_by == worker_id,
                )
                .values(
                    checkpoint_ciphertext=self._encrypt_task_value(task_id, "checkpoint", checkpoint),
                    updated_at=utc_now(),
                )
            )
            return bool(result.rowcount)

    def complete(
        self,
        task_id: str,
        worker_id: str,
        result_payload: dict[str, Any] | None,
    ) -> bool:
        now = utc_now()
        with self._session_factory() as session, session.begin():
            task = session.scalar(
                select(Task).where(
                    Task.id == task_id,
                    Task.status == "running",
                    Task.locked_by == worker_id,
                )
            )
            if task is None:
                return False
            task.status = "succeeded"
            task.progress = 1.0
            task.result_ciphertext = self._encrypt_task_value(task.id, "result", result_payload or {})
            task.locked_by = ""
            task.locked_until = None
            task.updated_at = now
            task.finished_at = now
            attempt = session.scalar(
                select(TaskAttempt).where(TaskAttempt.task_id == task.id, TaskAttempt.attempt == task.attempt)
            )
            if attempt is not None:
                attempt.status = "succeeded"
                attempt.finished_at = now
            session.execute(delete(ResourceLease).where(ResourceLease.task_id == task.id))
            self._append_event(session, task.id, "task.succeeded", "任务执行成功")
            self._append_change(session, task.id, "complete", status=task.status, task_type=task.task_type)
            self._refresh_batch(session, task.batch_id)
            return True

    def fail(
        self,
        task_id: str,
        worker_id: str,
        *,
        code: str,
        message: str,
        retry: bool,
        needs_review: bool = False,
    ) -> bool:
        now = utc_now()
        with self._session_factory() as session, session.begin():
            task = session.scalar(
                select(Task).where(
                    Task.id == task_id,
                    Task.status == "running",
                    Task.locked_by == worker_id,
                )
            )
            if task is None:
                return False
            if needs_review:
                next_status = "needs_review"
            elif retry and task.attempt < task.max_attempts:
                next_status = "retry_wait"
            else:
                next_status = "failed"
            delay = self._retry_base_seconds * (2 ** max(0, task.attempt - 1))
            task.status = next_status
            task.run_after = now + timedelta(seconds=delay) if next_status == "retry_wait" else now
            task.locked_by = ""
            task.locked_until = None
            task.last_error_code = code[:96]
            task.last_error_message = message[:2000]
            task.updated_at = now
            task.finished_at = None if next_status == "retry_wait" else now
            attempt = session.scalar(
                select(TaskAttempt).where(TaskAttempt.task_id == task.id, TaskAttempt.attempt == task.attempt)
            )
            if attempt is not None:
                attempt.status = next_status
                attempt.error_message = message[:2000]
                attempt.finished_at = now
            session.execute(delete(ResourceLease).where(ResourceLease.task_id == task.id))
            level = "warning" if next_status == "retry_wait" else "error"
            self._append_event(
                session,
                task.id,
                f"task.{next_status}",
                "任务稍后自动重试" if next_status == "retry_wait" else message[:500],
                level=level,
                payload={"code": code, "attempt": task.attempt},
            )
            self._append_change(session, task.id, "fail", status=task.status, task_type=task.task_type)
            self._refresh_batch(session, task.batch_id)
            return True

    def mark_waiting_input(
        self,
        task_id: str,
        worker_id: str,
        *,
        input_type: str,
        expires_at: datetime,
        checkpoint: dict[str, Any],
        message: str,
    ) -> bool:
        now = utc_now()
        with self._session_factory() as session, session.begin():
            task = session.scalar(
                select(Task).where(
                    Task.id == task_id,
                    Task.status == "running",
                    Task.locked_by == worker_id,
                )
            )
            if task is None:
                return False
            task.status = "waiting_input"
            task.run_after = expires_at
            task.checkpoint_ciphertext = self._encrypt_task_value(task.id, "checkpoint", checkpoint)
            task.locked_by = ""
            task.locked_until = None
            task.updated_at = now
            attempt = session.scalar(
                select(TaskAttempt).where(TaskAttempt.task_id == task.id, TaskAttempt.attempt == task.attempt)
            )
            if attempt is not None:
                attempt.status = "waiting_input"
                attempt.finished_at = now
            session.execute(
                update(ResourceLease).where(ResourceLease.task_id == task.id).values(expires_at=expires_at)
            )
            self._append_event(
                session,
                task.id,
                "task.waiting_input",
                message,
                payload={"input_type": input_type, "expires_at": _iso(expires_at)},
            )
            self._append_change(session, task.id, "wait_input", status=task.status, task_type=task.task_type)
            self._refresh_batch(session, task.batch_id)
            return True

    def submit_input(
        self,
        task_id: str,
        input_type: str,
        value: str,
        *,
        ttl_seconds: int = 300,
    ) -> TaskSnapshot:
        normalized_type = input_type.strip().lower()
        normalized_value = value.strip()
        now = utc_now()
        value_hash = self._codec.lookup_hash(
            normalized_value, namespace=f"task-input:{task_id}:{normalized_type}"
        )
        with self._session_factory() as session, session.begin():
            task = session.get(Task, task_id)
            if task is None:
                raise TaskNotFoundError("任务不存在")
            if task.status != "waiting_input":
                raise TaskConflictError("任务当前未等待输入")
            checkpoint = self._decrypt_task_value(task.id, "checkpoint", task.checkpoint_ciphertext)
            expected_type = str(checkpoint.get("input_type") or "").strip().lower()
            if expected_type and expected_type != normalized_type:
                raise TaskConflictError(f"任务正在等待 {expected_type.upper()} 输入")
            duplicate = session.scalar(
                select(TaskInput.id).where(
                    TaskInput.task_id == task_id,
                    TaskInput.input_type == normalized_type,
                    TaskInput.value_hash == value_hash,
                )
            )
            if duplicate is not None:
                raise TaskConflictError("该输入已经提交过")
            input_id = str(uuid.uuid4())
            session.add(
                TaskInput(
                    id=input_id,
                    task_id=task_id,
                    input_type=normalized_type,
                    value_ciphertext=self._codec.encrypt(normalized_value, context=f"task-input:{input_id}"),
                    value_hash=value_hash,
                    consumed_at=None,
                    expires_at=now + timedelta(seconds=ttl_seconds),
                    created_at=now,
                )
            )
            task.status = "queued"
            task.run_after = now
            task.updated_at = now
            task.last_error_code = ""
            task.last_error_message = ""
            self._append_event(
                session,
                task.id,
                "task.input_received",
                "一次性输入已接收，任务重新进入队列",
                payload={"input_type": normalized_type},
            )
            self._append_change(session, task.id, "input", status=task.status, task_type=task.task_type)
            return TaskSnapshot.from_model(task)

    def consume_input(self, task_id: str, input_type: str) -> str | None:
        now = utc_now()
        with self._session_factory() as session, session.begin():
            row = session.scalar(
                select(TaskInput)
                .where(
                    TaskInput.task_id == task_id,
                    TaskInput.input_type == input_type.strip().lower(),
                    TaskInput.consumed_at.is_(None),
                    TaskInput.expires_at > now,
                )
                .order_by(TaskInput.created_at)
                .limit(1)
            )
            if row is None:
                return None
            value = self._codec.decrypt(row.value_ciphertext, context=f"task-input:{row.id}")
            row.value_ciphertext = ""
            row.consumed_at = now
            self._append_event(
                session,
                task_id,
                "task.input_consumed",
                "一次性输入已消费并清除密文",
                payload={"input_type": row.input_type},
            )
            return value

    def cancel(self, task_id: str) -> TaskSnapshot:
        now = utc_now()
        with self._session_factory() as session, session.begin():
            task = session.get(Task, task_id)
            if task is None:
                raise TaskNotFoundError("任务不存在")
            if task.status in TERMINAL_STATUSES:
                raise TaskConflictError("任务已经结束")
            task.status = "cancelled"
            task.locked_by = ""
            task.locked_until = None
            task.updated_at = now
            task.finished_at = now
            if task.attempt:
                attempt = session.scalar(
                    select(TaskAttempt).where(
                        TaskAttempt.task_id == task.id, TaskAttempt.attempt == task.attempt
                    )
                )
                if attempt is not None and attempt.finished_at is None:
                    attempt.status = "cancelled"
                    attempt.finished_at = now
            session.execute(delete(ResourceLease).where(ResourceLease.task_id == task.id))
            session.execute(
                update(TaskInput)
                .where(TaskInput.task_id == task.id, TaskInput.consumed_at.is_(None))
                .values(value_ciphertext="", consumed_at=now)
            )
            self._append_event(session, task.id, "task.cancelled", "任务已取消")
            self._append_change(session, task.id, "cancel", status=task.status, task_type=task.task_type)
            self._refresh_batch(session, task.batch_id)
            return TaskSnapshot.from_model(task)

    def retry(self, task_id: str) -> TaskSnapshot:
        now = utc_now()
        with self._session_factory() as session, session.begin():
            task = session.get(Task, task_id)
            if task is None:
                raise TaskNotFoundError("任务不存在")
            if task.status not in {"failed", "cancelled", "needs_review"}:
                raise TaskConflictError("当前任务状态不允许手动重试")
            batch = session.get(TaskBatch, task.batch_id) if task.batch_id else None
            if batch is not None and batch.strategy == "rolling":
                raise TaskConflictError("滚动批次会自动补充下一条任务，无需重复执行旧任务")
            task.status = "queued"
            task.max_attempts = max(task.max_attempts, task.attempt + 1)
            task.run_after = now
            task.locked_by = ""
            task.locked_until = None
            task.last_error_code = ""
            task.last_error_message = ""
            task.finished_at = None
            task.updated_at = now
            self._append_event(session, task.id, "task.retried", "任务已手动重新入队")
            self._append_change(session, task.id, "retry", status=task.status, task_type=task.task_type)
            self._refresh_batch(session, task.batch_id)
            return TaskSnapshot.from_model(task)

    def acquire_resource(
        self,
        task_id: str,
        resource_type: str,
        resource_key: str,
        *,
        ttl_seconds: int | None = None,
    ) -> bool:
        now = utc_now()
        lease_id = str(uuid.uuid4())
        expires_at = now + timedelta(seconds=ttl_seconds or self._lease_seconds)
        statement = (
            sqlite_insert(ResourceLease)
            .values(
                id=lease_id,
                resource_type=resource_type,
                resource_key=resource_key,
                task_id=task_id,
                acquired_at=now,
                expires_at=expires_at,
            )
            .on_conflict_do_update(
                index_elements=[ResourceLease.resource_type, ResourceLease.resource_key],
                set_={
                    "id": lease_id,
                    "task_id": task_id,
                    "acquired_at": now,
                    "expires_at": expires_at,
                },
                where=or_(
                    ResourceLease.expires_at <= now,
                    ResourceLease.task_id == task_id,
                ),
            )
            .returning(ResourceLease.id)
        )
        with self._session_factory() as session, session.begin():
            return session.execute(statement).scalar_one_or_none() is not None

    def release_resources(self, task_id: str) -> None:
        with self._session_factory() as session, session.begin():
            session.execute(delete(ResourceLease).where(ResourceLease.task_id == task_id))

    def recover_expired(self, registry: HandlerRegistry) -> dict[str, int]:
        now = utc_now()
        recovered = 0
        review = 0
        with self._session_factory() as session, session.begin():
            expired = session.scalars(
                select(Task).where(
                    Task.status == "running",
                    or_(Task.locked_until.is_(None), Task.locked_until <= now),
                )
            ).all()
            for task in expired:
                spec = registry.get(task.task_type)
                safe_to_retry = bool(spec and spec.safe_to_retry and task.attempt < task.max_attempts)
                attempts_exhausted = bool(spec and spec.safe_to_retry and task.attempt >= task.max_attempts)
                task.status = (
                    "retry_wait" if safe_to_retry else "failed" if attempts_exhausted else "needs_review"
                )
                task.run_after = now
                task.locked_by = ""
                task.locked_until = None
                task.updated_at = now
                task.last_error_code = "worker_lease_expired"
                if safe_to_retry:
                    task.last_error_message = "Worker 租约过期，任务已重新入队"
                elif attempts_exhausted:
                    task.last_error_message = "Worker 租约过期且自动重试次数已用完"
                else:
                    task.last_error_message = "Worker 租约过期，副作用任务需要人工复核"
                if task.status != "retry_wait":
                    task.finished_at = now
                    if task.status == "needs_review":
                        review += 1
                else:
                    recovered += 1
                attempt = session.scalar(
                    select(TaskAttempt).where(
                        TaskAttempt.task_id == task.id,
                        TaskAttempt.attempt == task.attempt,
                    )
                )
                if attempt is not None and attempt.finished_at is None:
                    attempt.status = "interrupted"
                    attempt.error_message = task.last_error_message
                    attempt.finished_at = now
                self._append_event(
                    session,
                    task.id,
                    f"task.{task.status}",
                    task.last_error_message,
                    level="warning",
                )
                self._append_change(session, task.id, "recover", status=task.status, task_type=task.task_type)
                self._refresh_batch(session, task.batch_id)
            session.execute(delete(ResourceLease).where(ResourceLease.expires_at <= now))
        return {"requeued": recovered, "needs_review": review}

    def expire_waiting_inputs(self) -> int:
        now = utc_now()
        expired_count = 0
        with self._session_factory() as session, session.begin():
            tasks = session.scalars(
                select(Task).where(Task.status == "waiting_input", Task.run_after <= now)
            ).all()
            for task in tasks:
                task.status = "failed"
                task.last_error_code = "input_timeout"
                task.last_error_message = "等待一次性输入超时"
                task.updated_at = now
                task.finished_at = now
                session.execute(delete(ResourceLease).where(ResourceLease.task_id == task.id))
                session.execute(
                    update(TaskInput)
                    .where(TaskInput.task_id == task.id, TaskInput.consumed_at.is_(None))
                    .values(value_ciphertext="", consumed_at=now)
                )
                self._append_event(
                    session,
                    task.id,
                    "task.failed",
                    task.last_error_message,
                    level="error",
                )
                self._append_change(
                    session, task.id, "input_timeout", status=task.status, task_type=task.task_type
                )
                self._refresh_batch(session, task.batch_id)
                expired_count += 1
            session.execute(
                delete(TaskInput).where(TaskInput.consumed_at.is_not(None), TaskInput.expires_at <= now)
            )
        return expired_count

    def list_changes(self, after: int, *, limit: int = 200) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(ChangeLog)
                .where(ChangeLog.sequence > max(0, after))
                .order_by(ChangeLog.sequence)
                .limit(limit)
            ).all()
            return [
                {
                    "sequence": row.sequence,
                    "event_type": row.event_type,
                    "resource": row.resource,
                    "resource_id": row.resource_id,
                    "operation": row.operation,
                    "payload": _json_load(row.payload_json),
                    "created_at": _iso(row.created_at),
                }
                for row in rows
            ]
