"""GoPay 注册与已有账号登录任务入口。"""

from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, SecretStr, field_validator
from sqlalchemy import select

from gopay_app.api.dependencies import require_admin, require_csrf
from gopay_app.api.responses import success
from gopay_app.db.models import Account, SmsActivation
from gopay_app.tasks.errors import TaskConflictError, TaskNotFoundError

router = APIRouter(prefix="/api/v1/account-flows", tags=["账号流程"])
AUTOMATIC_PHONE_SOURCES = {"smsbower", "hero_sms"}


class AccountFlowCreate(BaseModel):
    mode: str = Field(pattern=r"^(register|login)$")
    phone_source: str = Field(default="smsbower", pattern=r"^[a-z][a-z0-9_]{1,31}$")
    phone: str = Field(default="", max_length=40)
    pin: SecretStr | None = Field(default=None, max_length=6)
    change_pin: bool | None = None
    new_pin: SecretStr | None = Field(default=None, max_length=6)
    proxy: SecretStr | None = Field(default=None, max_length=600)
    proxy_region: str = Field(default="", pattern=r"^(|[A-Za-z0-9]{2,12})$")
    count: int | None = Field(default=None, ge=1, le=1000)
    concurrency: int | None = Field(default=None, ge=1, le=50)

    @field_validator("phone", mode="after")
    @classmethod
    def trim_phone(cls, value: str) -> str:
        return value.strip()


@router.get("/sources")
def list_phone_sources(request: Request, _admin=Depends(require_admin)):
    sms_configured = bool(request.app.state.sms_settings_store.get().api_key)
    hero_sms_configured = bool(request.app.state.hero_sms_settings_store.get().api_key)
    return success(
        [
            {
                "value": "smsbower",
                "label": "SMSBower 自动取号",
                "description": "注册和已有账号登录都会自动申请新号码并取码",
                "modes": ["register", "login"],
                "available": sms_configured,
            },
            {
                "value": "hero_sms",
                "label": "Hero-SMS 自动取号",
                "description": "使用 Hero-SMS 的印度尼西亚 Gojek 号码自动取号并取码",
                "modes": ["register", "login"],
                "available": hero_sms_configured,
            },
        ]
    )


@router.get("/logs")
def list_account_flow_logs(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    all_items: bool = Query(default=False, alias="all"),
    _admin=Depends(require_admin),
):
    items, total = request.app.state.task_repository.list_account_flow_logs(
        limit=None if all_items else limit,
        offset=0 if all_items else offset,
    )
    active = request.app.state.task_repository.account_flow_active_counts()
    runs = request.app.state.task_repository.account_flow_run_state()
    return success({"items": items, "total": total, "active": active, "runs": runs})


@router.delete("/logs")
def clear_account_flow_logs(request: Request, _admin=Depends(require_csrf)):
    return success(request.app.state.task_repository.clear_account_flow_logs())


@router.post("/runs/{batch_id}/stop")
def stop_account_flow_run(batch_id: str, request: Request, _admin=Depends(require_csrf)):
    try:
        data = request.app.state.task_repository.stop_account_flow_run(batch_id)
    except TaskNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "account_flow_run_not_found", "message": str(exc)},
        ) from exc
    except TaskConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "account_flow_run_finished", "message": str(exc)},
        ) from exc
    return success(data)


@router.post("", status_code=201)
def create_account_flow(
    request: Request,
    body: AccountFlowCreate,
    _admin=Depends(require_csrf),
):
    defaults = request.app.state.account_flow_defaults_store.get()
    supplied_pin = body.pin.get_secret_value() if body.pin else ""
    supplied_new_pin = body.new_pin.get_secret_value() if body.new_pin else ""
    pin = supplied_pin or (defaults.register_pin if body.mode == "register" else defaults.login_pin)
    new_pin = supplied_new_pin or defaults.new_pin
    change_pin = body.mode == "login" and (
        defaults.change_pin_enabled if body.change_pin is None else body.change_pin
    )
    count = body.count or defaults.task_count
    concurrency_requested = body.concurrency or defaults.concurrency
    proxy_region = (body.proxy_region or defaults.default_proxy_region).strip().upper()
    supplied_proxy = body.proxy.get_secret_value().strip() if body.proxy is not None else ""
    if body.mode == "register" and body.phone_source not in {*AUTOMATIC_PHONE_SOURCES, "manual"}:
        raise HTTPException(
            status_code=422,
            detail={"code": "phone_source_invalid", "message": "当前注册号码来源尚未接入"},
        )
    if body.mode == "login" and body.phone_source not in {
        "accounts",
        *AUTOMATIC_PHONE_SOURCES,
        "manual",
    }:
        raise HTTPException(
            status_code=422,
            detail={"code": "phone_source_invalid", "message": "当前登录号码来源尚未接入"},
        )
    if not re.fullmatch(r"\d{6}", pin):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "pin_invalid",
                "message": "请填写 6 位 PIN，或先在系统设置中保存默认 PIN",
            },
        )
    if body.phone_source == "manual" and not body.phone and body.mode == "register":
        raise HTTPException(
            status_code=422,
            detail={"code": "phone_required", "message": "手动号码来源需要填写手机号"},
        )
    if change_pin and (not re.fullmatch(r"\d{6}", new_pin) or new_pin == pin or body.mode != "login"):
        raise HTTPException(
            status_code=422,
            detail={"code": "new_pin_invalid", "message": "新 PIN 必须是不同的 6 位数字"},
        )

    worker_count = int(request.app.state.settings.worker_count)
    base_payload = {
        "pin": pin,
        "change_pin": change_pin,
        "new_pin": new_pin,
        "proxy_region": proxy_region,
        "country_code": "+62",
    }

    def task_payload(index: int, **values: object) -> dict[str, object]:
        return {
            **base_payload,
            "proxy": supplied_proxy or defaults.proxy_for(proxy_region, index),
            **values,
        }

    payloads: list[dict[str, object]] = []
    if body.mode == "register":
        source = body.phone_source
        effective_count = 1 if source == "manual" else count
        payloads = [
            task_payload(index, phone_source=source, phone=body.phone) for index in range(effective_count)
        ]
    elif body.phone_source in AUTOMATIC_PHONE_SOURCES:
        payloads = [
            task_payload(index, phone_source=body.phone_source, phone="") for index in range(count)
        ]
    elif body.phone:
        source = body.phone_source
        if source == "accounts":
            with request.app.state.session_factory() as session:
                account = session.scalar(
                    select(Account).where(Account.phone_normalized == re.sub(r"\D", "", body.phone))
                )
                activation = None
                if account is not None:
                    activation = session.scalar(
                        select(SmsActivation).where(
                            SmsActivation.account_id == account.id,
                            SmsActivation.status.in_(("active", "unknown", "rented")),
                        )
                    )
                source = activation.provider if activation else "manual"
        payloads = [task_payload(0, phone_source=source, phone=body.phone)]
    else:
        with request.app.state.session_factory() as session:
            account_query = select(Account).order_by(Account.updated_at.desc(), Account.id)
            accounts = session.scalars(account_query.limit(count)).unique().all()
            if not accounts:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "account_missing", "message": "当前账号库中没有可登录的 GoPay 账号"},
                )
            payloads = [
                task_payload(
                    index,
                    phone_source=(body.phone_source if body.phone_source in AUTOMATIC_PHONE_SOURCES else "manual"),
                    phone=account.phone,
                )
                for index, account in enumerate(accounts)
            ]
    # 期望并发属于整个滚动批次，不能被本次新增数量限制。
    # 用户逐条追加任务时，只要批次仍有空闲槽，新任务就应立即进入 Worker 队列。
    concurrency = min(concurrency_requested, worker_count)
    task_type = f"account.{body.mode}"
    if request.app.state.task_registry.get(task_type) is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "handler_unavailable", "message": "账号协议 Handler 尚未就绪"},
        )
    try:
        appended = request.app.state.task_repository.append_active_rolling_batch(
            task_type,
            payloads,
            desired_concurrency=concurrency,
        )
        if appended is None:
            batch, tasks = request.app.state.task_repository.create_rolling_batch(
                task_type,
                payloads,
                desired_concurrency=concurrency,
                max_attempts=1,
                idempotency_prefix=f"account-flow:{uuid.uuid4()}",
            )
        else:
            batch, tasks = appended
    except TaskConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "account_flow_running", "message": str(exc)},
        ) from exc
    return success(
        {"batch": batch, "tasks": [task.to_dict() for task in tasks]},
        status_code=201,
    )
