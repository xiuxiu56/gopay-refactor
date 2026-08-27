"""P1 固定 Worker 池上限与 OTP 恢复测试。"""

from __future__ import annotations

import threading
import time

from gopay_app.tasks.context import TaskContext
from gopay_app.tasks.errors import PermanentTaskError
from gopay_app.tasks.handlers.builtin import wait_input_handler
from gopay_app.tasks.registry import HandlerRegistry
from gopay_app.tasks.repository import TaskRepository
from gopay_app.tasks.worker_pool import WorkerPool


def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("等待任务状态超时")


def test_fixed_pool_never_exceeds_configured_concurrency(database):
    _engine, session_factory, codec = database
    repository = TaskRepository(session_factory, codec, lease_seconds=10, retry_base_seconds=0.01)
    registry = HandlerRegistry()
    lock = threading.Lock()
    active = 0
    maximum = 0

    def tracked_handler(context: TaskContext, _payload):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        try:
            time.sleep(0.08)
            context.ensure_active()
            return {}
        finally:
            with lock:
                active -= 1

    registry.register("test.concurrent", tracked_handler)
    for _index in range(18):
        repository.create_task("test.concurrent")

    pool = WorkerPool(
        repository,
        registry,
        worker_count=3,
        heartbeat_seconds=0.2,
        poll_seconds=0.01,
        shutdown_seconds=3,
    )
    pool.start()
    try:
        _wait_until(lambda: repository.list_tasks(status="succeeded", limit=30)[1] == 18)
        assert maximum == 3
        assert pool.status()["configured_workers"] == 3
    finally:
        pool.stop()


def test_batch_never_exceeds_desired_concurrency(database):
    _engine, session_factory, codec = database
    repository = TaskRepository(session_factory, codec, lease_seconds=10, retry_base_seconds=0.01)
    registry = HandlerRegistry()
    lock = threading.Lock()
    active = 0
    maximum = 0

    def tracked_handler(context: TaskContext, _payload):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        try:
            time.sleep(0.08)
            context.ensure_active()
            return {}
        finally:
            with lock:
                active -= 1

    registry.register("test.batch-concurrent", tracked_handler)
    repository.create_batch(
        "test.batch-concurrent",
        [{"index": index} for index in range(8)],
        desired_concurrency=2,
    )
    pool = WorkerPool(
        repository,
        registry,
        worker_count=4,
        heartbeat_seconds=0.2,
        poll_seconds=0.01,
        shutdown_seconds=3,
    )
    pool.start()
    try:
        _wait_until(lambda: repository.list_tasks(status="succeeded", limit=20)[1] == 8)
        assert maximum == 2
    finally:
        pool.stop()


def test_rolling_batch_only_replenishes_after_each_task_finishes(database):
    _engine, session_factory, codec = database
    repository = TaskRepository(session_factory, codec, lease_seconds=10, retry_base_seconds=0.01)
    registry = HandlerRegistry()
    lock = threading.Lock()
    active = 0
    maximum = 0

    def tracked_handler(context: TaskContext, payload):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        try:
            time.sleep(0.08)
            context.ensure_active()
            return {"index": payload["index"]}
        finally:
            with lock:
                active -= 1

    registry.register("account.register", tracked_handler)
    batch, initial = repository.create_rolling_batch(
        "account.register",
        [{"index": index} for index in range(5)],
        desired_concurrency=2,
        max_attempts=1,
    )
    assert batch["strategy"] == "rolling"
    assert batch["created"] == 2
    assert len(initial) == 2
    assert repository.list_tasks(task_type="account.register", limit=20)[1] == 2

    pool = WorkerPool(
        repository,
        registry,
        worker_count=4,
        heartbeat_seconds=0.2,
        poll_seconds=0.01,
        shutdown_seconds=3,
    )
    pool.start()
    try:
        _wait_until(
            lambda: repository.list_tasks(
                status="succeeded", task_type="account.register", limit=20
            )[1]
            == 5
        )
        assert repository.list_tasks(task_type="account.register", limit=20)[1] == 5
        assert maximum == 2
        run_state = repository.account_flow_run_state()
        assert run_state["current"] is None
        latest = run_state["latest"]["register"]
        assert latest["id"] == batch["id"]
        assert latest["status"] == "succeeded"
        assert latest["target"] == 5
        assert latest["active"] == 0
        assert latest["succeeded"] == 5
        assert latest["failed"] == 0
    finally:
        pool.stop()


def test_waiting_input_releases_rolling_batch_concurrency_slot(database):
    _engine, session_factory, codec = database
    repository = TaskRepository(session_factory, codec, lease_seconds=10, retry_base_seconds=0.01)
    registry = HandlerRegistry()

    def waiting_then_success(context: TaskContext, payload):
        if payload["index"] == 1:
            return context.wait_for_input(
                "otp",
                timeout_seconds=60,
                checkpoint={"index": 1},
                message="正在等待手动 OTP",
            )
        return {"index": payload["index"]}

    registry.register("account.login", waiting_then_success)
    batch, initial = repository.create_rolling_batch(
        "account.login",
        [{"index": 1}, {"index": 2}],
        desired_concurrency=1,
        max_attempts=1,
    )
    assert len(initial) == 1

    pool = WorkerPool(
        repository,
        registry,
        worker_count=1,
        heartbeat_seconds=0.2,
        poll_seconds=0.01,
        shutdown_seconds=3,
    )
    pool.start()
    try:
        _wait_until(
            lambda: repository.list_tasks(
                status="waiting_input", task_type="account.login", limit=20
            )[1]
            == 1
            and repository.list_tasks(
                status="succeeded", task_type="account.login", limit=20
            )[1]
            == 1
        )
        assert repository.list_tasks(task_type="account.login", limit=20)[1] == 2
        current = repository.account_flow_run_state()["current"]
        assert current["id"] == batch["id"]
        assert current["created"] == 2
        assert current["active"] == 0
    finally:
        pool.stop()


def test_failed_final_task_closes_rolling_account_batch(database):
    _engine, session_factory, codec = database
    repository = TaskRepository(session_factory, codec, lease_seconds=10, retry_base_seconds=0.01)
    registry = HandlerRegistry()

    def failing_handler(_context: TaskContext, _payload):
        raise PermanentTaskError("模拟账号任务失败", code="test_failure")

    registry.register("account.login", failing_handler)
    batch, tasks = repository.create_rolling_batch(
        "account.login",
        [{"index": 1}],
        desired_concurrency=1,
        max_attempts=1,
    )
    pool = WorkerPool(
        repository,
        registry,
        worker_count=1,
        heartbeat_seconds=0.2,
        poll_seconds=0.01,
        shutdown_seconds=3,
    )
    pool.start()
    try:
        _wait_until(lambda: repository.get_task(tasks[0].id).status == "failed")
        run_state = repository.account_flow_run_state()
        assert run_state["current"] is None
        latest = run_state["latest"]["login"]
        assert latest["id"] == batch["id"]
        assert latest["status"] == "failed"
        assert latest["target"] == 1
        assert latest["active"] == 0
        assert latest["succeeded"] == 0
        assert latest["failed"] == 1
    finally:
        pool.stop()


def test_worker_pool_pauses_and_resumes_for_otp(database):
    _engine, session_factory, codec = database
    repository = TaskRepository(session_factory, codec, lease_seconds=10, retry_base_seconds=0.01)
    registry = HandlerRegistry()
    registry.register("system.wait_input", wait_input_handler)
    task, _ = repository.create_task("system.wait_input", {"input_type": "otp", "timeout_seconds": 60})
    pool = WorkerPool(
        repository,
        registry,
        worker_count=1,
        heartbeat_seconds=0.2,
        poll_seconds=0.01,
        shutdown_seconds=3,
    )
    pool.start()
    try:
        _wait_until(lambda: repository.get_task(task.id).status == "waiting_input")
        repository.submit_input(task.id, "otp", "654321")
        _wait_until(lambda: repository.get_task(task.id).status == "succeeded")
        assert repository.consume_input(task.id, "otp") is None
    finally:
        pool.stop()
