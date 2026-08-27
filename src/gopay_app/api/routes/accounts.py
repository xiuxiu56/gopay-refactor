"""账号只读 REST 接口。"""

from __future__ import annotations

import json
import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from gopay_app.api.dependencies import require_admin, require_csrf
from gopay_app.api.responses import success
from gopay_app.db.models import (
    Account,
    AccountSecret,
    ChangeLog,
    PaymentIntent,
    ResourceLease,
    SmsActivation,
    utc_now,
)
from gopay_app.security.codec import SecretCodecError
from gopay_app.tasks.errors import TaskConflictError, TaskNotFoundError
from gopay_app.tasks.repository import _iso

router = APIRouter(prefix="/api/v1/accounts", tags=["账号"])


class AccountLoginAction(BaseModel):
    old_pin: SecretStr | None = Field(default=None, max_length=6)
    new_pin: SecretStr | None = Field(default=None, max_length=6)


ACCOUNT_STATUS_LABELS = {
    "available": "可用",
    "no_balance": "余额不足",
    "pin_missing": "未设置 PIN",
    "relogin_required": "需要重新登录",
    "reserved": "使用中",
    "payment_success": "支付成功",
    "payment_failed": "支付失败",
    "unknown": "状态未知",
}


def _account_secret(request: Request, secret_row: AccountSecret | None) -> dict[str, object]:
    if secret_row is None:
        return {}
    try:
        secret = json.loads(
            request.app.state.secret_codec.decrypt(
                secret_row.secret_payload_ciphertext,
                context=f"account:{secret_row.account_id}",
            )
        )
    except (json.JSONDecodeError, SecretCodecError, TypeError):
        return {}
    return secret if isinstance(secret, dict) else {}


def _account_state(
    account: Account,
    secret: dict[str, object],
    *,
    leased: bool,
    payment: PaymentIntent | None,
) -> tuple[str, str]:
    payment_status = str(payment.status if payment is not None else "").strip().lower()
    if payment_status == "succeeded":
        return "payment_success", "最近一次支付已经成功完成"
    if payment_status in {"failed", "needs_review", "cancelled"}:
        message = str(payment.last_error_message if payment is not None else "").strip()
        return "payment_failed", message or "最近一次支付失败或需要复核"
    if leased or payment_status in {"queued", "running", "waiting_otp", "retry_wait"}:
        return "reserved", "后台任务正在使用此账号"
    if not str(secret.get("access_token") or secret.get("refresh_token") or "").strip():
        return "relogin_required", "账号缺少有效登录令牌"
    pin = str(secret.get("pin") or "").strip()
    if account.pin_setup_status != "configured" or not re.fullmatch(r"\d{6}", pin):
        return "pin_missing", "账号尚未保存可用的 6 位 PIN"
    if account.balance < 1:
        return "no_balance", "当前余额不足，不参与自动支付"
    return "available", "账号令牌、PIN 与余额均可用"


def _account_state_context(
    session: Session,
    account_ids: list[str],
) -> tuple[dict[str, PaymentIntent], set[str]]:
    if not account_ids:
        return {}, set()
    payments = session.scalars(
        select(PaymentIntent)
        .where(PaymentIntent.account_id.in_(account_ids))
        .order_by(PaymentIntent.updated_at.desc(), PaymentIntent.id.desc())
    ).all()
    latest_payments: dict[str, PaymentIntent] = {}
    for payment in payments:
        if payment.account_id:
            latest_payments.setdefault(payment.account_id, payment)
    leased_accounts = set(
        session.scalars(
            select(ResourceLease.resource_key).where(
                ResourceLease.resource_type == "account",
                ResourceLease.resource_key.in_(account_ids),
                ResourceLease.expires_at > utc_now(),
            )
        ).all()
    )
    return latest_payments, leased_accounts


def _account_dict(
    account: Account,
    *,
    secret: dict[str, object] | None = None,
    leased: bool = False,
    payment: PaymentIntent | None = None,
    sms_provider: str = "",
) -> dict[str, object]:
    secret = secret or {}
    pin = str(secret.get("pin") or "").strip()
    account_status, account_status_message = _account_state(
        account,
        secret,
        leased=leased,
        payment=payment,
    )
    return {
        "id": account.id,
        "phone": account.phone,
        "phone_source": sms_provider or "manual",
        "pin": pin,
        "local_phone": account.local_phone,
        "customer_id": account.customer_id,
        "remote_account_id": account.remote_account_id,
        "balance": account.balance,
        "account_status": account_status,
        "account_status_label": ACCOUNT_STATUS_LABELS[account_status],
        "account_status_message": account_status_message,
        "pin_setup_status": account.pin_setup_status,
        "pin_change_status": account.pin_change_status,
        "pin_change_message": account.pin_change_message,
        "sms_activation_status": account.sms_activation_status,
        "sms_provider": sms_provider,
        "registered_at": account.registered_at,
        "version": account.version,
        "created_at": _iso(account.created_at),
        "updated_at": _iso(account.updated_at),
    }


def _account_and_secret(request: Request, account_id: str) -> tuple[Account, dict[str, object], str]:
    with request.app.state.session_factory() as session:
        account = session.get(Account, account_id)
        secret_row = session.get(AccountSecret, account_id)
        if account is None or secret_row is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "account_not_found", "message": "账号或账号密钥记录不存在"},
            )
        secret = json.loads(
            request.app.state.secret_codec.decrypt(
                secret_row.secret_payload_ciphertext,
                context=f"account:{account_id}",
            )
        )
        activation = session.scalar(
            select(SmsActivation)
            .where(
                SmsActivation.account_id == account_id,
                SmsActivation.status.in_(("active", "unknown", "rented")),
            )
            .order_by(SmsActivation.updated_at.desc())
            .limit(1)
        )
        activation_provider = activation.provider if activation is not None else ""
        session.expunge(account)
    return account, secret, activation_provider


def _create_account_task(request: Request, task_type: str, account_id: str):
    task, _created = request.app.state.task_repository.create_task(
        task_type,
        {"account_id": account_id},
        max_attempts=1 if task_type == "account.refresh_sms_code" else 5,
    )
    return success({"task": task.to_dict()}, status_code=201)


@router.get("")
def list_accounts(
    request: Request,
    search: str = Query(default="", max_length=64),
    pin_status: str = Query(default="", max_length=32),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    all_items: bool = Query(default=False, alias="all"),
    _admin=Depends(require_admin),
):
    filters = []
    normalized_search = search.strip()
    if normalized_search:
        filters.append(
            or_(
                Account.phone.contains(normalized_search, autoescape=True),
                Account.customer_id.contains(normalized_search, autoescape=True),
                Account.remote_account_id.contains(normalized_search, autoescape=True),
            )
        )
    if pin_status:
        filters.append(Account.pin_setup_status == pin_status)
    with request.app.state.session_factory() as session:
        total = session.scalar(select(func.count()).select_from(Account).where(*filters)) or 0
        statement = select(Account).where(*filters).order_by(Account.updated_at.desc(), Account.id)
        if not all_items:
            statement = statement.offset(offset).limit(limit)
        rows = session.scalars(statement).all()
        account_ids = [row.id for row in rows]
        secret_rows = (
            session.scalars(select(AccountSecret).where(AccountSecret.account_id.in_(account_ids))).all()
            if account_ids
            else []
        )
        secrets = {row.account_id: _account_secret(request, row) for row in secret_rows}
        activation_rows = (
            session.scalars(
                select(SmsActivation)
                .where(SmsActivation.account_id.in_(account_ids))
                .order_by(SmsActivation.updated_at.desc())
            ).all()
            if account_ids
            else []
        )
        activation_providers: dict[str, str] = {}
        for activation in activation_rows:
            if activation.account_id:
                activation_providers.setdefault(activation.account_id, activation.provider)
        payments, leased_accounts = _account_state_context(session, account_ids)
        items = [
            _account_dict(
                row,
                secret=secrets.get(row.id),
                leased=row.id in leased_accounts,
                payment=payments.get(row.id),
                sms_provider=activation_providers.get(row.id, ""),
            )
            for row in rows
        ]
    return success({"items": items, "total": int(total)})


@router.get("/{account_id}")
def account_detail(account_id: str, request: Request, _admin=Depends(require_admin)):
    with request.app.state.session_factory() as session:
        account = session.get(Account, account_id)
        if account is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "account_not_found", "message": "账号不存在"},
            )
        secret_row = session.get(AccountSecret, account_id)
        payments, leased_accounts = _account_state_context(session, [account_id])
        activation = session.scalar(
            select(SmsActivation)
            .where(SmsActivation.account_id == account_id)
            .order_by(SmsActivation.updated_at.desc())
            .limit(1)
        )
        data = _account_dict(
            account,
            secret=_account_secret(request, secret_row),
            leased=account_id in leased_accounts,
            payment=payments.get(account_id),
            sms_provider=activation.provider if activation is not None else "",
        )
    return success(data)


@router.post("/{account_id}/actions/refresh")
def refresh_account(account_id: str, request: Request, _admin=Depends(require_csrf)):
    _account_and_secret(request, account_id)
    return _create_account_task(request, "account.refresh", account_id)


@router.post("/{account_id}/actions/check-pin")
def check_account_pin(account_id: str, request: Request, _admin=Depends(require_csrf)):
    _account_and_secret(request, account_id)
    return _create_account_task(request, "account.check_pin", account_id)


@router.post("/{account_id}/actions/release-number")
def release_account_number(account_id: str, request: Request, _admin=Depends(require_csrf)):
    _account_and_secret(request, account_id)
    return _create_account_task(request, "account.release_number", account_id)


@router.post("/{account_id}/actions/refresh-sms-code")
def refresh_account_sms_code(account_id: str, request: Request, _admin=Depends(require_csrf)):
    _account_and_secret(request, account_id)
    return _create_account_task(request, "account.refresh_sms_code", account_id)


@router.post("/{account_id}/actions/refresh-sms-code/{task_id}/result")
def consume_account_sms_code(
    account_id: str,
    task_id: str,
    request: Request,
    _admin=Depends(require_csrf),
):
    repository = request.app.state.task_repository
    try:
        execution = repository.get_execution(task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "task_not_found", "message": str(exc)},
        ) from exc
    if (
        execution.snapshot.task_type != "account.refresh_sms_code"
        or str(execution.payload.get("account_id") or "") != account_id
    ):
        raise HTTPException(
            status_code=404,
            detail={"code": "sms_code_task_not_found", "message": "验证码任务与账号不匹配"},
        )
    if execution.snapshot.status != "succeeded":
        message = execution.snapshot.last_error_message or "最新验证码仍在获取中"
        raise HTTPException(
            status_code=409,
            detail={"code": "sms_code_not_ready", "message": message},
        )
    try:
        result = repository.consume_result(task_id)
    except TaskConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "sms_code_consumed", "message": str(exc)},
        ) from exc
    code = str(result.get("code") or "").strip()
    if not re.fullmatch(r"\d{4,8}", code):
        raise HTTPException(
            status_code=409,
            detail={"code": "sms_code_missing", "message": "任务结果中没有有效验证码"},
        )
    return success({"task_id": task_id, "account_id": account_id, "code": code})


def _create_login_task(
    request: Request,
    account_id: str,
    body: AccountLoginAction,
    *,
    change_pin: bool,
):
    account, secret, activation_provider = _account_and_secret(request, account_id)
    old_pin = (
        body.old_pin.get_secret_value().strip()
        if body.old_pin is not None
        else str(secret.get("pin") or "").strip()
    )
    new_pin = body.new_pin.get_secret_value().strip() if body.new_pin is not None else ""
    if not re.fullmatch(r"\d{6}", old_pin):
        raise HTTPException(
            status_code=422,
            detail={"code": "pin_invalid", "message": "请输入有效的 6 位原 PIN"},
        )
    if change_pin and (not re.fullmatch(r"\d{6}", new_pin) or new_pin == old_pin):
        raise HTTPException(
            status_code=422,
            detail={"code": "new_pin_invalid", "message": "新 PIN 必须是不同的 6 位数字"},
        )
    task, _created = request.app.state.task_repository.create_task(
        "account.login",
        {
            "phone_source": activation_provider or "manual",
            "phone": account.phone,
            "pin": old_pin,
            "change_pin": change_pin,
            "new_pin": new_pin,
            "proxy": str(secret.get("proxy") or ""),
            "country_code": "+62",
        },
        max_attempts=8,
    )
    return success({"task": task.to_dict()}, status_code=201)


@router.post("/{account_id}/actions/relogin")
def relogin_account(
    account_id: str,
    request: Request,
    body: AccountLoginAction,
    _admin=Depends(require_csrf),
):
    return _create_login_task(request, account_id, body, change_pin=False)


@router.post("/{account_id}/actions/change-pin")
def change_account_pin(
    account_id: str,
    request: Request,
    body: AccountLoginAction,
    _admin=Depends(require_csrf),
):
    return _create_login_task(request, account_id, body, change_pin=True)


@router.get("/{account_id}/pool-format")
def account_pool_format(account_id: str, request: Request, _admin=Depends(require_admin)):
    with request.app.state.session_factory() as session:
        account = session.get(Account, account_id)
        if account is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "account_not_found", "message": "账号不存在"},
            )
        activation = session.scalar(
            select(SmsActivation)
            .where(SmsActivation.account_id == account_id)
            .order_by(SmsActivation.updated_at.desc())
            .limit(1)
        )
        suffix = f"{activation.provider}://{activation.provider_activation_id}" if activation else ""
        value = f"{account.phone}----{suffix}"
    return success({"value": value})


@router.delete("/{account_id}")
def delete_account(account_id: str, request: Request, _admin=Depends(require_csrf)):
    now = utc_now()
    with request.app.state.session_factory() as session, session.begin():
        account = session.get(Account, account_id)
        if account is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "account_not_found", "message": "账号不存在"},
            )
        phone = account.phone
        session.delete(account)
        session.add(
            ChangeLog(
                event_type="account.updated",
                resource="account",
                resource_id=account_id,
                operation="delete",
                payload_json=json.dumps(
                    {"id": account_id, "deleted": True},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                created_at=now,
            )
        )
    return success({"id": account_id, "phone": phone, "deleted": True})
