"""本地登录接口滑动窗口限流。"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class SlidingWindowLimiter:
    def __init__(self, *, limit: int = 8, window_seconds: float = 60.0):
        self.limit = max(1, limit)
        self.window_seconds = max(1.0, window_seconds)
        self._lock = threading.Lock()
        self._attempts: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> tuple[bool, int]:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        normalized = key.strip() or "unknown"
        with self._lock:
            entries = self._attempts[normalized]
            while entries and entries[0] <= cutoff:
                entries.popleft()
            if len(entries) >= self.limit:
                retry_after = max(1, int(self.window_seconds - (now - entries[0])))
                return False, retry_after
            entries.append(now)
            return True, 0

    def reset(self, key: str) -> None:
        with self._lock:
            self._attempts.pop(key.strip() or "unknown", None)
