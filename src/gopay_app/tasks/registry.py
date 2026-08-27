"""任务 Handler 注册表。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from .context import TaskContext


class TaskHandler(Protocol):
    def __call__(self, context: TaskContext, payload: dict[str, Any]) -> dict[str, Any] | None: ...


@dataclass(frozen=True, slots=True)
class HandlerSpec:
    task_type: str
    handler: TaskHandler
    safe_to_retry: bool = True
    description: str = ""


class HandlerRegistry:
    """启动前注册、运行期只读的 Handler 集合。"""

    def __init__(self) -> None:
        self._handlers: dict[str, HandlerSpec] = {}

    def register(
        self,
        task_type: str,
        handler: TaskHandler,
        *,
        safe_to_retry: bool = True,
        description: str = "",
    ) -> HandlerSpec:
        normalized = task_type.strip().lower()
        if not normalized:
            raise ValueError("任务类型不能为空")
        if normalized in self._handlers:
            raise ValueError(f"任务类型已注册：{normalized}")
        spec = HandlerSpec(normalized, handler, safe_to_retry, description)
        self._handlers[normalized] = spec
        return spec

    def handler(
        self,
        task_type: str,
        *,
        safe_to_retry: bool = True,
        description: str = "",
    ) -> Callable[[TaskHandler], TaskHandler]:
        def decorator(callback: TaskHandler) -> TaskHandler:
            self.register(
                task_type,
                callback,
                safe_to_retry=safe_to_retry,
                description=description,
            )
            return callback

        return decorator

    def get(self, task_type: str) -> HandlerSpec | None:
        return self._handlers.get(task_type.strip().lower())

    def require(self, task_type: str) -> HandlerSpec:
        spec = self.get(task_type)
        if spec is None:
            raise KeyError(f"未注册的任务类型：{task_type}")
        return spec

    def descriptions(self) -> list[dict[str, Any]]:
        return [
            {
                "task_type": spec.task_type,
                "safe_to_retry": spec.safe_to_retry,
                "description": spec.description,
            }
            for spec in sorted(self._handlers.values(), key=lambda item: item.task_type)
        ]
