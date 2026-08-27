"""FastAPI 应用工厂。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from gopay_app import __version__
from gopay_app.auth.limiter import SlidingWindowLimiter
from gopay_app.auth.service import AuthError, AuthService
from gopay_app.config import Settings, get_settings
from gopay_app.db.engine import build_session_factory, create_database_engine, upgrade_database
from gopay_app.protocols.legacy import LegacyProtocolAdapter
from gopay_app.security.codec import SecretCodec
from gopay_app.services.account_flow_defaults import AccountFlowDefaultsStore
from gopay_app.services.sms_settings import HeroSmsSettingsStore, SmsSettingsStore
from gopay_app.tasks.handlers import build_default_registry
from gopay_app.tasks.repository import TaskRepository
from gopay_app.tasks.worker_pool import WorkerPool

from .responses import failure, success
from .routes.account_flows import router as account_flows_router
from .routes.accounts import router as accounts_router
from .routes.auth import router as auth_router
from .routes.payments import router as payments_router
from .routes.realtime import router as realtime_router
from .routes.settings import router as settings_router
from .routes.system import router as system_router
from .routes.tasks import router as tasks_router


def create_app(settings: Settings | None = None) -> FastAPI:
    effective_settings = settings or get_settings()
    web_root = Path(__file__).resolve().parents[1] / "web_dist"
    web_index = web_root / "index.html"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        upgrade_database(effective_settings)
        engine = create_database_engine(effective_settings)
        session_factory = build_session_factory(engine)
        app.state.settings = effective_settings
        app.state.engine = engine
        app.state.session_factory = session_factory
        app.state.secret_codec = SecretCodec.load(effective_settings.database_key_path)
        app.state.auth_service = AuthService(
            session_factory,
            session_ttl_hours=effective_settings.session_ttl_hours,
        )
        app.state.login_limiter = SlidingWindowLimiter(limit=8, window_seconds=60)
        app.state.sms_settings_store = SmsSettingsStore(
            session_factory,
            app.state.secret_codec,
        )
        app.state.hero_sms_settings_store = HeroSmsSettingsStore(
            session_factory,
            app.state.secret_codec,
        )
        app.state.account_flow_defaults_store = AccountFlowDefaultsStore(
            session_factory,
            app.state.secret_codec,
        )
        app.state.protocol_adapter = LegacyProtocolAdapter(effective_settings.legacy_app_path)
        app.state.task_repository = TaskRepository(
            session_factory,
            app.state.secret_codec,
            lease_seconds=effective_settings.worker_lease_seconds,
            retry_base_seconds=effective_settings.task_retry_base_seconds,
            change_log_limit=effective_settings.change_log_limit,
        )
        app.state.task_registry = build_default_registry(
            app.state.task_repository, effective_settings.legacy_app_path
        )
        app.state.worker_pool = WorkerPool(
            app.state.task_repository,
            app.state.task_registry,
            worker_count=effective_settings.worker_count,
            heartbeat_seconds=effective_settings.worker_heartbeat_seconds,
            poll_seconds=effective_settings.worker_poll_seconds,
            shutdown_seconds=effective_settings.worker_shutdown_seconds,
        )
        app.state.worker_pool.start()
        try:
            yield
        finally:
            app.state.worker_pool.stop()
            engine.dispose()

    app = FastAPI(
        title="GoPay 本地控制台",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=effective_settings.allowed_hosts)

    @app.middleware("http")
    async def security_boundary(request: Request, call_next):
        content_length = request.headers.get("content-length", "")
        if content_length.isdigit() and int(content_length) > effective_settings.max_request_bytes:
            return failure(413, "request_too_large", "请求内容超过大小限制")
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin", "").rstrip("/")
            fetch_site = request.headers.get("sec-fetch-site", "").lower()
            allowed_origins = {item.rstrip("/") for item in effective_settings.allowed_origins}
            request_host = request.headers.get("host", "").strip()
            if request_host:
                allowed_origins.add(f"{request.url.scheme}://{request_host}")
            if origin and origin not in allowed_origins:
                return failure(403, "origin_rejected", "请求来源不受信任")
            if fetch_site == "cross-site":
                return failure(403, "cross_site_rejected", "跨站请求已被拦截")
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; font-src 'self' data:; connect-src 'self'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(AuthError)
    async def handle_auth_error(_request: Request, exc: AuthError):
        status_code = 409 if exc.code == "setup_completed" else 400
        if exc.code == "invalid_credentials":
            status_code = 401
        return failure(status_code, exc.code, str(exc))

    @app.exception_handler(HTTPException)
    async def handle_http_error(_request: Request, exc: HTTPException):
        if isinstance(exc.detail, dict):
            return failure(
                exc.status_code,
                str(exc.detail.get("code") or "http_error"),
                str(exc.detail.get("message") or "请求失败"),
            )
        return failure(exc.status_code, "http_error", str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_request: Request, _exc: RequestValidationError):
        return failure(422, "validation_failed", "请求字段格式不正确")

    assets_path = web_root / "assets"
    if assets_path.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_path), name="web-assets")

    @app.get("/", include_in_schema=False)
    def root():
        if web_index.is_file():
            return FileResponse(web_index)
        return success({"name": "GoPay 本地控制台", "version": __version__, "stage": "P4"})

    app.include_router(auth_router)
    app.include_router(system_router)
    app.include_router(tasks_router)
    app.include_router(account_flows_router)
    app.include_router(accounts_router)
    app.include_router(payments_router)
    app.include_router(settings_router)
    app.include_router(realtime_router)

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str):
        if full_path.startswith("api/") or full_path == "health":
            raise HTTPException(
                status_code=404,
                detail={"code": "route_not_found", "message": "接口不存在"},
            )
        requested = (web_root / full_path).resolve()
        if web_root.resolve() in requested.parents and requested.is_file():
            return FileResponse(requested)
        if web_index.is_file() and "." not in Path(full_path).name:
            return FileResponse(web_index)
        raise HTTPException(
            status_code=404,
            detail={"code": "page_not_found", "message": "页面不存在"},
        )

    return app
