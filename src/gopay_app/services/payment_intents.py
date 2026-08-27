"""Midtrans 支付链接解析与公开状态辅助函数。"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

SNAP_PATH_PATTERN = re.compile(r"/snap/v[34]/redirection/([0-9a-fA-F-]{36})(?:/|$)")


def extract_snap_token(midtrans_url: str) -> str:
    """验证 Midtrans 主机并提取 Snap token。"""
    value = str(midtrans_url or "").strip()
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname != "app.midtrans.com":
        return ""
    matched = SNAP_PATH_PATTERN.search(parsed.path)
    return matched.group(1).lower() if matched else ""
