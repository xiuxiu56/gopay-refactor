"""Handler 可用的受控任务上下文。"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from gopay_app.db.models import utc_now

from .errors import RetryableTaskError, TaskCancelled, TaskWaitingInput
from .repository import TaskRepository


class TaskContext:
    """向 Handler 暴露进度、检查点、一次性输入与资源租约。"""

    def __init__(self, repository: TaskRepository, task_id: str, worker_id: str) -> None:
        self.repository = repository
        self.task_id = task_id
        self.worker_id = worker_id

    def ensure_active(self) -> None:
        if not self.repository.is_running(self.task_id, self.worker_id):
            raise TaskCancelled("任务已取消或 Worker 租约失效")

    def heartbeat(self) -> None:
        if not self.repository.heartbeat(self.task_id, self.worker_id):
            raise TaskCancelled("任务已取消或 Worker 租约失效")

    def progress(self, value: float, message: str = "") -> None:
        if not self.repository.update_progress(self.task_id, self.worker_id, value, message):
            raise TaskCancelled("任务已取消或 Worker 租约失效")

    def save_checkpoint(self, checkpoint: dict[str, Any]) -> None:
        if not self.repository.save_checkpoint(self.task_id, self.worker_id, checkpoint):
            raise TaskCancelled("任务已取消或 Worker 租约失效")

    def checkpoint(self) -> dict[str, Any]:
        self.ensure_active()
        return self.repository.get_execution(self.task_id).checkpoint

    def acquire_resource(
        self,
        resource_type: str,
        resource_key: str,
        *,
        ttl_seconds: int | None = None,
    ) -> None:
        self.ensure_active()
        if not self.repository.acquire_resource(
            self.task_id,
            resource_type,
            resource_key,
            ttl_seconds=ttl_seconds,
        ):
            raise RetryableTaskError(f"资源正在被其他任务使用：{resource_type}", code="resource_busy")

    def consume_input(self, input_type: str) -> str | None:
        self.ensure_active()
        return self.repository.consume_input(self.task_id, input_type)

    def wait_for_input(
        self,
        input_type: str,
        *,
        timeout_seconds: int = 300,
        checkpoint: dict[str, Any] | None = None,
        message: str = "任务正在等待一次性输入",
    ) -> str:
        value = self.consume_input(input_type)
        if value is not None:
            return value
        raise TaskWaitingInput(
            input_type,
            expires_at=utc_now() + timedelta(seconds=timeout_seconds),
            checkpoint=checkpoint,
            message=message,
        )
