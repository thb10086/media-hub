"""Application factory: ``uvicorn hub.main:create_app --factory``."""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from typing import Any, AsyncIterator, Final

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import Response
from redis.asyncio import Redis
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from hub import __version__
from hub.api import api_router
from hub.api.media import MEDIA_PATH
from hub.constants import HEADER_REQUEST_ID
from hub.db import create_engine, create_schema, create_session_factory
from hub.envelope import ERROR_ENVELOPE_SCHEMA, fail
from hub.errors import ErrorCode, HubError
from hub.http import outbound_client
from hub.log import request_id_var, setup_logging
from hub.providers.channels import build_channels_provider
from hub.providers.dtk import DtkClient
from hub.providers.sau import SauClient
from hub.security.net import parse_networks
from hub.security.ratelimit import RateLimiter
from hub.services.cache import ResponseCache
from hub.services.data_identities import build_data_identities
from hub.services.usage import record_usage
from hub.settings import Settings, get_settings

log = logging.getLogger("hub.access")

REQUEST_ID_PATTERN: Final = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
MULTIPART_OVERHEAD_BYTES: Final = 1024 * 1024
HTTP_SERVER_ERROR: Final = 500
HTTP_STATUS_CODES: Final[dict[int, ErrorCode]] = {
    400: ErrorCode.INVALID_PARAM,
    401: ErrorCode.UNAUTHENTICATED,
    403: ErrorCode.FORBIDDEN_SCOPE,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.METHOD_NOT_ALLOWED,
    413: ErrorCode.PAYLOAD_TOO_LARGE,
    429: ErrorCode.RATE_LIMITED,
}
FASTAPI_VALIDATION_STATUS: Final = "422"
INVALID_PARAM_STATUS: Final = "400"
FASTAPI_VALIDATION_SCHEMAS: Final = ("HTTPValidationError", "ValidationError")
ERROR_ENVELOPE_NAME: Final = "ErrorEnvelope"
API_DESCRIPTION: Final = (
    "认证：请求头 `X-API-Key`（仅限服务端调用）。所有 JSON 响应使用统一信封 "
    "`{success, data, error, meta}`，按 `error.code` 分支。慢操作返回 `202` 与任务对象，"
    "用 `GET /api/v1/tasks/{task_id}` 轮询或通过 `callback_url` 接收回调。完整说明见 docs/API.md。"
)


@dataclass(frozen=True)
class Overrides:
    """Test seams: an injected Redis and an HTTP transport for every upstream client."""

    redis: Redis | None = None
    http_transport: httpx.AsyncBaseTransport | None = None


def _request_id(candidate: str | None) -> str:
    if candidate and REQUEST_ID_PATTERN.match(candidate):
        return candidate
    return uuid.uuid4().hex


def _oversized_upload(request: Request) -> Response | None:
    """Refuse an upload by its declared size before the body is spooled to disk."""
    if request.method != "POST" or request.url.path != MEDIA_PATH:
        return None
    max_bytes = request.app.state.settings.max_upload_bytes
    declared = request.headers.get("Content-Length", "")
    if declared.isdigit() and int(declared) > max_bytes + MULTIPART_OVERHEAD_BYTES:
        return fail(
            request,
            HubError(
                ErrorCode.PAYLOAD_TOO_LARGE,
                "the upload exceeds the size limit",
                details={"max_bytes": max_bytes},
            ),
        )
    return None


async def _meter(request: Request, status_code: int) -> None:
    principal = getattr(request.state, "principal", None)
    if principal is None or status_code >= HTTP_SERVER_ERROR:
        return
    endpoint = getattr(request.scope.get("route"), "path", request.url.path)
    try:
        async with request.app.state.session_factory() as session:
            await record_usage(
                session,
                tenant_id=principal.tenant_id,
                endpoint=endpoint,
                day=datetime.now(timezone.utc).date(),
            )
    except Exception:
        log.warning("usage.record_failed", exc_info=True, extra={"endpoint": endpoint})


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = _request_id(request.headers.get(HEADER_REQUEST_ID))
        request.state.request_id = request_id
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        try:
            response = _oversized_upload(request) or await call_next(request)
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            response.headers[HEADER_REQUEST_ID] = request_id
            response.headers["X-Response-Time-Ms"] = str(elapsed_ms)
            decision = getattr(request.state, "rate_limit", None)
            if decision is not None:
                response.headers.update(decision.headers())
            await _meter(request, response.status_code)
            log.info(
                "request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": elapsed_ms,
                },
            )
            return response
        finally:
            request_id_var.reset(token)


def _register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(HubError)
    async def _hub_error(request: Request, exc: HubError) -> Response:
        return fail(request, exc)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> Response:
        errors = [
            {"loc": list(error.get("loc", ())), "msg": error.get("msg"), "type": error.get("type")}
            for error in exc.errors()
        ]
        return fail(
            request,
            HubError(ErrorCode.INVALID_PARAM, "request validation failed", details={"errors": errors}),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
        code = HTTP_STATUS_CODES.get(exc.status_code, ErrorCode.INTERNAL)
        return fail(request, HubError(code, str(exc.detail)))

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> Response:
        log.error("unhandled_exception", exc_info=exc)
        return fail(request, HubError(ErrorCode.INTERNAL, "internal error"))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    state = app.state
    settings: Settings = state.settings
    overrides: Overrides = state.overrides
    engine = create_engine(settings.database_url)
    if settings.auto_create_schema:
        await create_schema(engine)
    owns_redis = overrides.redis is None and settings.redis_url is not None
    redis = (
        overrides.redis
        if overrides.redis is not None
        else (Redis.from_url(settings.redis_url) if settings.redis_url else None)
    )
    transport = overrides.http_transport
    state.engine = engine
    state.session_factory = create_session_factory(engine)
    state.redis = redis
    state.rate_limiter = RateLimiter(redis)
    state.trusted_networks = parse_networks(settings.trusted_proxies)
    state.http = outbound_client(settings, transport=transport)
    state.dtk = (
        DtkClient(
            settings.dtk_base_url,
            settings.dtk_api_key.get_secret_value() if settings.dtk_api_key else "",
            wait_seconds=settings.dtk_wait_seconds,
            timeout=settings.dtk_timeout_seconds,
            max_connections=settings.dtk_max_connections,
            max_keepalive=settings.dtk_max_keepalive,
            transport=transport,
        )
        if settings.dtk_base_url
        else None
    )
    state.cache = ResponseCache(redis)
    state.sau = (
        SauClient(
            settings.sau_base_url,
            request_timeout=settings.sau_request_timeout_seconds,
            publish_timeout=settings.sau_publish_timeout_seconds,
            login_timeout=settings.sau_login_timeout_seconds,
            video_dir=settings.sau_video_dir,
            transport=transport,
        )
        if settings.sau_base_url
        else None
    )
    state.identities = build_data_identities(settings, state.dtk, state.sau)
    state.channels = build_channels_provider(settings.channels_provider)
    state.login_slots = asyncio.Semaphore(settings.login_max_concurrency)
    state.background_tasks = set()
    try:
        yield
    finally:
        for task in list(state.background_tasks):
            task.cancel()
        await asyncio.gather(*state.background_tasks, return_exceptions=True)
        for client in (state.dtk, state.sau, state.channels):
            if client is not None:
                await client.aclose()
        await state.http.aclose()
        if owns_redis and redis is not None:
            await redis.aclose()
        await engine.dispose()


def build_openapi(app: FastAPI) -> dict[str, Any]:
    """FastAPI's document, corrected to what this API actually sends.

    Validation failures are answered as 400 in the error envelope, never as
    FastAPI's 422, so the 422 entries and their schemas are replaced.
    """
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        summary=app.summary,
        description=app.description,
        routes=app.routes,
    )
    schemas = schema.setdefault("components", {}).setdefault("schemas", {})
    for name in FASTAPI_VALIDATION_SCHEMAS:
        schemas.pop(name, None)
    schemas[ERROR_ENVELOPE_NAME] = ERROR_ENVELOPE_SCHEMA
    error_content = {"application/json": {"schema": {"$ref": f"#/components/schemas/{ERROR_ENVELOPE_NAME}"}}}
    for operations in schema.get("paths", {}).values():
        for operation in operations.values():
            responses = operation.get("responses", {})
            if responses.pop(FASTAPI_VALIDATION_STATUS, None) is not None:
                responses[INVALID_PARAM_STATUS] = {
                    "description": "参数校验失败（INVALID_PARAM）",
                    "content": error_content,
                }
    app.openapi_schema = schema
    return schema


def create_app(
    settings: Settings | None = None,
    *,
    redis: Redis | None = None,
    http_transport: httpx.AsyncBaseTransport | None = None,
    configure_logging: bool = True,
) -> FastAPI:
    settings = settings or get_settings()
    if configure_logging:
        setup_logging(settings.log_level)
    app = FastAPI(
        title="media-hub",
        version=__version__,
        summary="短视频平台统一 API：抖音数据、视频发布、视频号数据",
        description=API_DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs" if settings.expose_docs else None,
        redoc_url="/redoc" if settings.expose_docs else None,
        openapi_url="/openapi.json" if settings.expose_docs else None,
    )
    app.state.settings = settings
    app.state.overrides = Overrides(redis=redis, http_transport=http_transport)
    app.add_middleware(RequestContextMiddleware)
    _register_exception_handlers(app)
    app.include_router(api_router)
    app.openapi = partial(build_openapi, app)  # type: ignore[method-assign]
    return app
