"""统一接口响应。"""

from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse


def success(data: Any = None, *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"success": True, "data": data})


def failure(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"success": False, "code": code, "message": message},
    )
