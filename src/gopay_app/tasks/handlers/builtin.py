"""用于检查任务引擎的内置 Handler。"""

from __future__ import annotations

import time
from typing import Any

from ..context import TaskContext


def echo_handler(context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
    context.progress(1.0, "回显任务执行完成")
    return {"echo": payload.get("value")}


def sleep_handler(context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
    duration = max(0.0, min(60.0, float(payload.get("seconds", 0.1))))
    started = time.monotonic()
    while True:
        elapsed = time.monotonic() - started
        if elapsed >= duration:
            break
        context.progress(min(0.99, elapsed / max(duration, 0.001)))
        time.sleep(min(0.05, duration - elapsed))
    context.progress(1.0, "等待任务执行完成")
    return {"elapsed_seconds": round(time.monotonic() - started, 3)}


def wait_input_handler(context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
    input_type = str(payload.get("input_type") or "otp").strip().lower()
    timeout_seconds = int(payload.get("timeout_seconds") or 300)
    value = context.wait_for_input(
        input_type,
        timeout_seconds=max(1, min(1800, timeout_seconds)),
        checkpoint={"phase": "waiting_input", "input_type": input_type},
        message=f"任务正在等待 {input_type.upper()} 输入",
    )
    context.progress(1.0, "一次性输入已验证并消费")
    return {"input_received": bool(value), "input_type": input_type}
