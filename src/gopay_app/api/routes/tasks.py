"""持久化任务队列 REST 接口。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from gopay_app.api.dependencies import require_admin, require_csrf
from gopay_app.api.responses import success
from gopay_app.tasks.errors import TaskConflictError, TaskNotFoundError
from gopay_app.tasks.repository import ALL_STATUSES, TaskRepository
from gopay_app.tasks.schemas import TaskCreate, TaskInputSubmit

router = APIRouter(prefix="/api/v1/tasks", tags=["任务"])


def _repository(request: Request) -> TaskRepository:
    return request.app.state.task_repository


def _raise_queue_error(exc: Exception) -> None:
    if isinstance(exc, TaskNotFoundError):
        raise HTTPException(status_code=404, detail={"code": "task_not_found", "message": str(exc)})
    if isinstance(exc, TaskConflictError):
        raise HTTPException(status_code=409, detail={"code": "task_conflict", "message": str(exc)})
    raise exc


@router.get("/types")
def task_types(request: Request, _admin=Depends(require_admin)):
    return success(request.app.state.task_registry.descriptions())


@router.get("")
def list_tasks(
    request: Request,
    status: str = Query(default="", max_length=32),
    task_type: str = Query(default="", max_length=48),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _admin=Depends(require_admin),
):
    if status and status not in ALL_STATUSES:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_status", "message": "任务状态筛选值不正确"},
        )
    rows, total = _repository(request).list_tasks(
        status=status,
        task_type=task_type.strip().lower(),
        limit=limit,
        offset=offset,
    )
    return success({"items": [row.to_dict() for row in rows], "total": total})


@router.post("", status_code=201)
def create_task(request: Request, body: TaskCreate, _admin=Depends(require_csrf)):
    if request.app.state.task_registry.get(body.task_type) is None:
        raise HTTPException(
            status_code=422,
            detail={"code": "task_type_unknown", "message": "任务类型尚未注册"},
        )
    task, created = _repository(request).create_task(
        body.task_type,
        body.payload,
        priority=body.priority,
        max_attempts=body.max_attempts,
        idempotency_key=body.idempotency_key,
        run_after=body.run_after,
    )
    return success({"task": task.to_dict(), "created": created}, status_code=201 if created else 200)


@router.get("/{task_id}")
def task_detail(
    task_id: str,
    request: Request,
    event_after: int = Query(default=0, ge=0),
    event_limit: int = Query(default=100, ge=1, le=500),
    _admin=Depends(require_admin),
):
    try:
        task = _repository(request).get_task(task_id)
        events = _repository(request).list_events(task_id, after=event_after, limit=event_limit)
    except (TaskNotFoundError, TaskConflictError) as exc:
        _raise_queue_error(exc)
    return success({"task": task.to_dict(), "events": events})


@router.post("/{task_id}/cancel")
def cancel_task(task_id: str, request: Request, _admin=Depends(require_csrf)):
    try:
        task = _repository(request).cancel(task_id)
    except (TaskNotFoundError, TaskConflictError) as exc:
        _raise_queue_error(exc)
    return success({"task": task.to_dict()})


@router.post("/{task_id}/retry")
def retry_task(task_id: str, request: Request, _admin=Depends(require_csrf)):
    try:
        task = _repository(request).retry(task_id)
    except (TaskNotFoundError, TaskConflictError) as exc:
        _raise_queue_error(exc)
    return success({"task": task.to_dict()})


@router.post("/{task_id}/input")
def submit_task_input(
    task_id: str,
    request: Request,
    body: TaskInputSubmit,
    _admin=Depends(require_csrf),
):
    try:
        task = _repository(request).submit_input(
            task_id,
            body.input_type,
            body.value.get_secret_value(),
            ttl_seconds=body.ttl_seconds,
        )
    except (TaskNotFoundError, TaskConflictError) as exc:
        _raise_queue_error(exc)
    return success({"task": task.to_dict(), "accepted": True})
