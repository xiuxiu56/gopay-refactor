"""支付意图创建、公开摘要与远端复核接口。"""

from __future__ import annotations

import hashlib
import json
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import func, select

from gopay_app.api.dependencies import require_admin, require_csrf
from gopay_app.api.responses import success
from gopay_app.db.models import Account, AccountSecret, PaymentIntent, utc_now
from gopay_app.services.payment_intents import extract_snap_token
from gopay_app.tasks.repository import _iso

router = APIRouter(prefix="/api/v1/payments", tags=["支付"])


class PaymentCreate(BaseModel):
    midtrans_url: str = Field(min_length=40, max_length=2048)
    account_id: str | None = Field(default=None, max_length=36)
    pin: str = Field(default="", max_length=6)
    proxy: SecretStr | None = Field(default=None, max_length=600)
    proxy_region: str = Field(default="", pattern=r"^(|[A-Za-z0-9]{2,12})$")


def _payment_dict(payment: PaymentIntent) -> dict[str, object]:
    return {
        "id": payment.id,
        "order_id": payment.order_id,
        "account_id": payment.account_id,
        "task_id": payment.task_id,
        "status": payment.status,
        "amount": payment.amount,
        "currency": payment.currency,
        "transaction_status": payment.transaction_status,
        "last_error_message": payment.last_error_message,
        "has_midtrans_url": bool(payment.midtrans_url_ciphertext),
        "has_raw_state": bool(payment.raw_state_ciphertext),
        "created_at": _iso(payment.created_at),
        "updated_at": _iso(payment.updated_at),
    }


def _select_account(request: Request, account_id: str | None) -> tuple[Account, dict[str, object]]:
    with request.app.state.session_factory() as session:
        if account_id:
            account = session.get(Account, account_id)
        else:
            account = session.scalar(
                select(Account)
                .join(AccountSecret, AccountSecret.account_id == Account.id)
                .where(Account.pin_setup_status == "configured", Account.balance > 0)
                .order_by(Account.balance.desc(), Account.updated_at.asc())
                .limit(1)
            )
        if account is None:
            raise HTTPException(
                status_code=409,
                detail={"code": "payment_account_missing", "message": "没有可用的 GoPay 支付账号"},
            )
        secret_row = session.get(AccountSecret, account.id)
        if secret_row is None:
            raise HTTPException(
                status_code=409,
                detail={"code": "payment_secret_missing", "message": "支付账号密钥记录不存在"},
            )
        secret = json.loads(
            request.app.state.secret_codec.decrypt(
                secret_row.secret_payload_ciphertext,
                context=f"account:{account.id}",
            )
        )
        session.expunge(account)
    return account, secret


@router.get("")
def list_payments(
    request: Request,
    status: str = Query(default="", max_length=32),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _admin=Depends(require_admin),
):
    filters = [PaymentIntent.status == status] if status else []
    with request.app.state.session_factory() as session:
        total = session.scalar(select(func.count()).select_from(PaymentIntent).where(*filters)) or 0
        rows = session.scalars(
            select(PaymentIntent)
            .where(*filters)
            .order_by(PaymentIntent.updated_at.desc(), PaymentIntent.id)
            .offset(offset)
            .limit(limit)
        ).all()
    return success({"items": [_payment_dict(row) for row in rows], "total": int(total)})


@router.delete("")
def clear_payment_logs(request: Request, _admin=Depends(require_csrf)):
    return success(request.app.state.task_repository.clear_payment_logs())


@router.post("")
def create_payment(request: Request, body: PaymentCreate, _admin=Depends(require_csrf)):
    snap = extract_snap_token(body.midtrans_url)
    if not snap:
        raise HTTPException(
            status_code=422,
            detail={"code": "midtrans_url_invalid", "message": "请输入有效的 Midtrans Snap 链接"},
        )
    account, secret = _select_account(request, body.account_id)
    pin = body.pin.strip() or str(secret.get("pin") or "").strip()
    if not re.fullmatch(r"\d{6}", pin):
        raise HTTPException(
            status_code=409,
            detail={"code": "payment_pin_missing", "message": "支付账号缺少有效的 6 位 PIN"},
        )
    token_hash = hashlib.sha256(snap.encode()).hexdigest()
    proxy = body.proxy.get_secret_value().strip() if body.proxy else ""
    proxy_region = body.proxy_region.strip().upper()
    if not proxy and proxy_region:
        defaults = request.app.state.account_flow_defaults_store.get()
        proxy = defaults.proxy_for(proxy_region, int(token_hash[:8], 16))
        if not proxy:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "payment_proxy_region_missing",
                    "message": f"区域 {proxy_region} 当前没有可用代理",
                },
            )
    now = utc_now()
    created = False
    with request.app.state.session_factory() as session, session.begin():
        intent = session.scalar(select(PaymentIntent).where(PaymentIntent.snap_token_hash == token_hash))
        if intent is None:
            created = True
            intent = PaymentIntent(
                id=str(uuid.uuid4()),
                snap_token_hash=token_hash,
                order_id="",
                account_id=account.id,
                task_id=None,
                status="queued",
                amount=0,
                currency="IDR",
                transaction_status="",
                last_error_message="",
                midtrans_url_ciphertext="",
                raw_state_ciphertext="",
                created_at=now,
                updated_at=now,
            )
            session.add(intent)
            session.flush([intent])
        elif intent.task_id:
            return success({"payment": _payment_dict(intent), "created": False})
        intent.account_id = account.id
        intent.status = "queued"
        intent.last_error_message = ""
        intent.midtrans_url_ciphertext = request.app.state.secret_codec.encrypt(
            body.midtrans_url.strip(),
            context=f"payment:{intent.id}:url",
        )
        intent.raw_state_ciphertext = request.app.state.secret_codec.encrypt(
            json.dumps(
                {"phase": "queued", "source": "web", "snap": snap},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            context=f"payment:{intent.id}:state",
        )
        intent.updated_at = now
        payment_id = intent.id

    task, task_created = request.app.state.task_repository.create_task(
        "payment.execute",
        {
            "payment_id": payment_id,
            "pin": pin,
            "proxy": proxy,
        },
        max_attempts=3,
        idempotency_key=f"payment.execute:{token_hash}",
    )
    with request.app.state.session_factory() as session, session.begin():
        intent = session.get(PaymentIntent, payment_id)
        if intent is None:
            raise HTTPException(
                status_code=409,
                detail={"code": "payment_disappeared", "message": "支付意图创建后状态丢失"},
            )
        intent.task_id = task.id
        intent.updated_at = utc_now()
        result = _payment_dict(intent)
    return success(
        {"payment": result, "task": task.to_dict(), "created": created and task_created},
        status_code=201 if created and task_created else 200,
    )


@router.get("/{payment_id}")
def payment_detail(payment_id: str, request: Request, _admin=Depends(require_admin)):
    with request.app.state.session_factory() as session:
        payment = session.get(PaymentIntent, payment_id)
        if payment is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "payment_not_found", "message": "支付意图不存在"},
            )
        data = _payment_dict(payment)
    return success(data)


@router.post("/{payment_id}/reconcile")
def reconcile_payment(payment_id: str, request: Request, _admin=Depends(require_csrf)):
    with request.app.state.session_factory() as session:
        payment = session.get(PaymentIntent, payment_id)
        if payment is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "payment_not_found", "message": "支付意图不存在"},
            )
    task, _created = request.app.state.task_repository.create_task(
        "payment.reconcile",
        {"payment_id": payment_id},
        priority=10,
        max_attempts=5,
    )
    with request.app.state.session_factory() as session, session.begin():
        payment = session.get(PaymentIntent, payment_id)
        if payment is not None:
            payment.task_id = task.id
            payment.status = "queued"
            payment.last_error_message = ""
            payment.updated_at = utc_now()
    return success({"task": task.to_dict()}, status_code=201)
