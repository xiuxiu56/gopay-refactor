"""任务状态机使用的异常类型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any


class TaskQueueError(RuntimeError):
    """任务队列基础异常。"""


class TaskNotFoundError(TaskQueueError):
    """任务不存在。"""


class TaskConflictError(TaskQueueError):
    """当前状态不允许执行操作。"""


class RetryableTaskError(TaskQueueError):
    """允许按退避策略重试的执行错误。"""

    def __init__(self, message: str, *, code: str = "temporary_error"):
        super().__init__(message)
        self.code = code


class PermanentTaskError(TaskQueueError):
    """不应自动重试的执行错误。"""

    def __init__(self, message: str, *, code: str = "permanent_error"):
        super().__init__(message)
        self.code = code


class ReviewTaskError(TaskQueueError):
    """远端副作用结果不确定，需要人工复核。"""

    def __init__(self, message: str, *, code: str = "review_required"):
        super().__init__(message)
        self.code = code


class TaskCancelled(TaskQueueError):
    """任务已由用户取消。"""


class TaskWaitingInput(TaskQueueError):
    """暂停任务并等待一次性输入。"""

    def __init__(
        self,
        input_type: str,
        *,
        expires_at: datetime,
        checkpoint: dict[str, Any] | None = None,
        message: str = "任务正在等待输入",
    ):
        super().__init__(message)
        self.input_type = input_type
        self.expires_at = expires_at
        self.checkpoint = checkpoint or {}
