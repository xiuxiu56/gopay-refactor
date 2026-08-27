"""健康与数据库状态接口。"""

from fastapi import APIRouter, Depends, Request

from gopay_app.api.dependencies import require_admin
from gopay_app.api.responses import success
from gopay_app.db.engine import database_status

router = APIRouter(tags=["系统"])


@router.get("/health")
def health():
    return success({"status": "ok", "stage": "P4"})


@router.get("/api/v1/system/status")
def system_status(request: Request, _admin=Depends(require_admin)):
    status = database_status(request.app.state.engine, request.app.state.settings.database_path)
    status["worker_count"] = request.app.state.settings.worker_count
    status["worker_pool_started"] = request.app.state.worker_pool.started
    status["worker_pool"] = request.app.state.worker_pool.status()
    status["stage"] = "P4"
    return success(status)
