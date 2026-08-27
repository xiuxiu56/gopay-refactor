"""固定线程 Worker 池与任务租约心跳。"""

from __future__ import annotations

import logging
import os
import re
import socket
import threading
import time
import uuid

from .context import TaskContext
from .errors import (
    PermanentTaskError,
    RetryableTaskError,
    ReviewTaskError,
    TaskCancelled,
    TaskWaitingInput,
)
from .registry import HandlerRegistry
from .repository import TaskExecution, TaskRepository

logger = logging.getLogger(__name__)


def _safe_error(exc: Exception) -> str:
    text = str(exc)[:2000]
    text = re.sub(r"(?i)(bearer\s+)[a-z0-9._~+\-/=]+", r"\1***", text)
    text = re.sub(
        r"(?i)((?:access[_-]?token|refresh[_-]?token|api[_-]?key|pin|otp)[\"']?\s*[:=]\s*[\"']?)[^\s,}\"']+",
        r"\1***",
        text,
    )
    return text or exc.__class__.__name__


class WorkerPool:
    """进程内固定数量的非 daemon Worker。"""

    def __init__(
        self,
        repository: TaskRepository,
        registry: HandlerRegistry,
        *,
        worker_count: int,
        heartbeat_seconds: float,
        poll_seconds: float,
        shutdown_seconds: float,
    ) -> None:
        self._repository = repository
        self._registry = registry
        self._worker_count = worker_count
        self._heartbeat_seconds = heartbeat_seconds
        self._poll_seconds = poll_seconds
        self._shutdown_seconds = shutdown_seconds
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._supervisor: threading.Thread | None = None
        self._active: dict[str, str] = {}
        self._active_lock = threading.Lock()
        self._started = False
        self._instance_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._recovery = {"requeued": 0, "needs_review": 0}

    @property
    def started(self) -> bool:
        return self._started

    @property
    def active_count(self) -> int:
        with self._active_lock:
            return len(self._active)

    def status(self) -> dict[str, object]:
        return {
            "started": self._started,
            "configured_workers": self._worker_count,
            "alive_workers": sum(thread.is_alive() for thread in self._threads),
            "active_tasks": self.active_count,
            "recovery": dict(self._recovery),
        }

    def start(self) -> None:
        if self._started:
            return
        self._recovery = self._repository.recover_expired(self._registry)
        self._stop_event.clear()
        self._threads = []
        for index in range(self._worker_count):
            worker_id = f"{self._instance_id}:w{index + 1}"
            thread = threading.Thread(
                target=self._worker_loop,
                args=(worker_id,),
                name=f"gopay-worker-{index + 1}",
                daemon=False,
            )
            thread.start()
            self._threads.append(thread)
        self._supervisor = threading.Thread(
            target=self._supervisor_loop,
            name="gopay-worker-supervisor",
            daemon=False,
        )
        self._supervisor.start()
        self._started = True
        logger.info("固定 Worker 池已启动：%d 个 Worker", self._worker_count)

    def stop(self) -> None:
        if not self._started:
            return
        self._stop_event.set()
        deadline = time.monotonic() + self._shutdown_seconds
        threads = [*self._threads]
        if self._supervisor is not None:
            threads.append(self._supervisor)
        for thread in threads:
            remaining = max(0.0, deadline - time.monotonic())
            thread.join(remaining)
        alive = [thread.name for thread in threads if thread.is_alive()]
        self._started = False
        if alive:
            logger.warning("Worker 优雅停止超时，租约到期后将自动恢复：%s", "、".join(alive))
        else:
            logger.info("固定 Worker 池已停止")

    def _set_active(self, worker_id: str, task_id: str) -> None:
        with self._active_lock:
            self._active[worker_id] = task_id

    def _clear_active(self, worker_id: str) -> None:
        with self._active_lock:
            self._active.pop(worker_id, None)

    def _worker_loop(self, worker_id: str) -> None:
        while not self._stop_event.is_set():
            try:
                execution = self._repository.claim_next(worker_id)
            except Exception:
                logger.exception("Worker 领取任务时发生数据库错误")
                self._stop_event.wait(self._poll_seconds)
                continue
            if execution is None:
                self._stop_event.wait(self._poll_seconds)
                continue
            self._set_active(worker_id, execution.snapshot.id)
            try:
                self._execute(worker_id, execution)
            finally:
                self._clear_active(worker_id)

    def _execute(self, worker_id: str, execution: TaskExecution) -> None:
        task = execution.snapshot
        spec = self._registry.get(task.task_type)
        if spec is None:
            self._repository.fail(
                task.id,
                worker_id,
                code="handler_not_registered",
                message=f"任务类型未注册：{task.task_type}",
                retry=False,
            )
            return
        context = TaskContext(self._repository, task.id, worker_id)
        try:
            result = spec.handler(context, execution.payload)
            context.ensure_active()
            self._repository.complete(task.id, worker_id, result or {})
        except TaskWaitingInput as exc:
            self._repository.mark_waiting_input(
                task.id,
                worker_id,
                input_type=exc.input_type,
                expires_at=exc.expires_at,
                checkpoint=exc.checkpoint,
                message=str(exc),
            )
        except TaskCancelled:
            logger.info("任务已停止：%s", task.id)
        except RetryableTaskError as exc:
            self._repository.fail(
                task.id,
                worker_id,
                code=exc.code,
                message=_safe_error(exc),
                retry=True,
            )
        except PermanentTaskError as exc:
            self._repository.fail(
                task.id,
                worker_id,
                code=exc.code,
                message=_safe_error(exc),
                retry=False,
            )
        except ReviewTaskError as exc:
            self._repository.fail(
                task.id,
                worker_id,
                code=exc.code,
                message=_safe_error(exc),
                retry=False,
                needs_review=True,
            )
        except Exception as exc:
            logger.exception("任务执行异常：%s", task.id)
            self._repository.fail(
                task.id,
                worker_id,
                code="unhandled_error",
                message=_safe_error(exc),
                retry=spec.safe_to_retry,
                needs_review=not spec.safe_to_retry,
            )

    def _supervisor_loop(self) -> None:
        while not self._stop_event.wait(self._heartbeat_seconds):
            with self._active_lock:
                active = list(self._active.items())
            for worker_id, task_id in active:
                try:
                    self._repository.heartbeat(task_id, worker_id)
                except Exception:
                    logger.exception("续租任务失败：%s", task_id)
            try:
                self._repository.expire_waiting_inputs()
            except Exception:
                logger.exception("清理超时输入任务失败")
