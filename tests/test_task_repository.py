"""P1 持久化任务状态机、并发领取和资源租约测试。"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from gopay_app.db.models import Task, TaskInput, utc_now
from gopay_app.tasks.errors import TaskConflictError
from gopay_app.tasks.registry import HandlerRegistry
from gopay_app.tasks.repository import TaskRepository


def _repository(database, *, retry_base_seconds: float = 0.01) -> TaskRepository:
    _engine, session_factory, codec = database
    return TaskRepository(
        session_factory,
        codec,
        lease_seconds=10,
        retry_base_seconds=retry_base_seconds,
        change_log_limit=1000,
    )


def test_atomic_claim_assigns_every_task_once(database):
    repository = _repository(database)
    for index in range(100):
        repository.create_task("system.echo", {"value": index})

    claimed: list[str] = []
    claimed_lock = threading.Lock()

    def consume(worker_number: int) -> None:
        worker_id = f"并发-worker-{worker_number}"
        while execution := repository.claim_next(worker_id):
            with claimed_lock:
                claimed.append(execution.snapshot.id)
            assert repository.complete(execution.snapshot.id, worker_id, {})

    with ThreadPoolExecutor(max_workers=12) as executor:
        list(executor.map(consume, range(12)))

    assert len(claimed) == 100
    assert len(set(claimed)) == 100
    rows, total = repository.list_tasks(status="succeeded", limit=200)
    assert total == 100
    assert len(rows) == 100


def test_expired_worker_recovery_distinguishes_side_effect_tasks(database):
    repository = _repository(database)
    registry = HandlerRegistry()
    registry.register("safe.read", lambda _context, _payload: {}, safe_to_retry=True)
    registry.register("payment.charge", lambda _context, _payload: {}, safe_to_retry=False)

    safe, _ = repository.create_task("safe.read")
    safe_execution = repository.claim_next("worker-safe")
    assert safe_execution and safe_execution.snapshot.id == safe.id
    with repository.session_factory() as session, session.begin():
        session.execute(
            update(Task).where(Task.id == safe.id).values(locked_until=utc_now() - timedelta(seconds=1))
        )
    assert repository.recover_expired(registry) == {"requeued": 1, "needs_review": 0}
    assert repository.get_task(safe.id).status == "retry_wait"

    side_effect, _ = repository.create_task("payment.charge", priority=100)
    side_effect_execution = repository.claim_next("worker-payment")
    assert side_effect_execution and side_effect_execution.snapshot.id == side_effect.id
    with repository.session_factory() as session, session.begin():
        session.execute(
            update(Task)
            .where(Task.id == side_effect.id)
            .values(locked_until=utc_now() - timedelta(seconds=1))
        )
    assert repository.recover_expired(registry) == {"requeued": 0, "needs_review": 1}
    assert repository.get_task(side_effect.id).status == "needs_review"


def test_one_time_input_is_encrypted_consumed_and_cleared(database):
    repository = _repository(database)
    task, _ = repository.create_task("system.wait_input")
    execution = repository.claim_next("worker-otp")
    assert execution is not None
    assert repository.mark_waiting_input(
        task.id,
        "worker-otp",
        input_type="otp",
        expires_at=utc_now() + timedelta(minutes=5),
        checkpoint={"phase": "otp"},
        message="等待 OTP",
    )

    repository.submit_input(task.id, "otp", "123456")
    with repository.session_factory() as session:
        stored = session.scalar(select(TaskInput).where(TaskInput.task_id == task.id))
        assert stored is not None
        assert "123456" not in stored.value_ciphertext
        assert stored.value_ciphertext.startswith("enc:v1:")

    resumed = repository.claim_next("worker-otp")
    assert resumed is not None
    assert repository.consume_input(task.id, "otp") == "123456"
    assert repository.consume_input(task.id, "otp") is None
    with repository.session_factory() as session:
        consumed = session.scalar(select(TaskInput).where(TaskInput.task_id == task.id))
        assert consumed is not None
        assert consumed.value_ciphertext == ""
        assert consumed.consumed_at is not None


def test_resource_lease_is_exclusive(database):
    repository = _repository(database)
    first, _ = repository.create_task("system.echo")
    second, _ = repository.create_task("system.echo")
    assert repository.acquire_resource(first.id, "account", "account-1")
    assert not repository.acquire_resource(second.id, "account", "account-1")
    repository.release_resources(first.id)
    assert repository.acquire_resource(second.id, "account", "account-1")


def test_retry_failure_manual_retry_and_cancel(database):
    repository = _repository(database)
    task, _ = repository.create_task("system.echo", max_attempts=3)
    first = repository.claim_next("worker-state")
    assert first is not None
    assert repository.fail(
        task.id,
        "worker-state",
        code="temporary",
        message="临时错误",
        retry=True,
    )
    assert repository.get_task(task.id).status == "retry_wait"

    with repository.session_factory() as session, session.begin():
        session.execute(update(Task).where(Task.id == task.id).values(run_after=utc_now()))
    second = repository.claim_next("worker-state")
    assert second is not None and second.snapshot.attempt == 2
    assert repository.fail(
        task.id,
        "worker-state",
        code="permanent",
        message="永久错误",
        retry=False,
    )
    assert repository.get_task(task.id).status == "failed"

    retried = repository.retry(task.id)
    assert retried.status == "queued"
    third = repository.claim_next("worker-state")
    assert third is not None and third.snapshot.attempt == 3
    cancelled = repository.cancel(task.id)
    assert cancelled.status == "cancelled"
    with pytest.raises(TaskConflictError):
        repository.cancel(task.id)


def test_input_type_must_match_encrypted_checkpoint(database):
    repository = _repository(database)
    task, _ = repository.create_task("system.wait_input")
    assert repository.claim_next("worker-input") is not None
    assert repository.mark_waiting_input(
        task.id,
        "worker-input",
        input_type="otp",
        expires_at=utc_now() + timedelta(minutes=5),
        checkpoint={"input_type": "otp"},
        message="等待 OTP",
    )
    with pytest.raises(TaskConflictError, match="OTP"):
        repository.submit_input(task.id, "pin", "123456")
