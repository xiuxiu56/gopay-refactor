"""基于 change_log 的可恢复 SSE 实时更新接口。"""

from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from gopay_app.api.dependencies import require_admin

router = APIRouter(tags=["实时更新"])


def _event_id(request: Request, after: int) -> int:
    header = request.headers.get("Last-Event-ID", "").strip()
    if header.isdigit():
        return max(after, int(header))
    return after


@router.get("/api/v1/realtime")
async def realtime(
    request: Request,
    after: int = Query(default=0, ge=0),
    once: bool = Query(default=False),
    _admin=Depends(require_admin),
):
    repository = request.app.state.task_repository
    settings = request.app.state.settings
    initial_id = _event_id(request, after)

    async def stream():
        last_id = initial_id
        last_output = time.monotonic()
        yield "retry: 2000\n\n"
        while True:
            changes = await asyncio.to_thread(repository.list_changes, last_id, limit=200)
            for change in changes:
                last_id = int(change["sequence"])
                data = json.dumps(change, ensure_ascii=False, separators=(",", ":"))
                yield f"id: {last_id}\nevent: {change['event_type']}\ndata: {data}\n\n"
                last_output = time.monotonic()
            if once:
                return
            if await request.is_disconnected():
                return
            if time.monotonic() - last_output >= settings.sse_heartbeat_seconds:
                yield ": 心跳\n\n"
                last_output = time.monotonic()
            await asyncio.sleep(settings.sse_poll_seconds)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
