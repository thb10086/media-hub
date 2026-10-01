"""Operator API: tenants, keys, usage. Guarded by ``X-Admin-Token``."""

from __future__ import annotations

import hmac
from datetime import date

from fastapi import APIRouter, Depends, Header, Path, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from hub.api.deps import get_session, settings_of
from hub.constants import HEADER_ADMIN_TOKEN
from hub.envelope import ok
from hub.errors import ErrorCode, HubError
from hub.schemas import KeyCreate, TenantCreate
from hub.services.tenants import (
    create_tenant,
    get_tenant,
    issue_key,
    key_view,
    list_keys,
    list_tenants,
    revoke_key,
    tenant_view,
)
from hub.services.usage import usage_report


async def require_admin(
    request: Request, token: str | None = Header(default=None, alias=HEADER_ADMIN_TOKEN)
) -> None:
    configured = settings_of(request).admin_token
    if configured is None:
        raise HubError(ErrorCode.NOT_CONFIGURED, "the admin API is disabled (HUB_ADMIN_TOKEN)")
    if not token or not hmac.compare_digest(token, configured.get_secret_value()):
        raise HubError(ErrorCode.UNAUTHENTICATED, "invalid admin token")


router = APIRouter(prefix="/api/v1/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@router.post("/tenants", summary="创建租户", status_code=201)
async def post_tenant(
    request: Request, body: TenantCreate, session: AsyncSession = Depends(get_session)
) -> JSONResponse:
    tenant = await create_tenant(session, name=body.name, rate_limit_per_min=body.rate_limit_per_min)
    return ok(request, {**tenant_view(tenant), "webhook_secret": tenant.webhook_secret}, status_code=201)


@router.get("/tenants", summary="租户列表")
async def get_tenants(request: Request, session: AsyncSession = Depends(get_session)) -> JSONResponse:
    return ok(request, {"items": [tenant_view(tenant) for tenant in await list_tenants(session)]})


@router.post("/tenants/{tenant_id}/keys", summary="签发 API Key（明文仅返回一次）", status_code=201)
async def post_key(
    request: Request,
    body: KeyCreate,
    tenant_id: str = Path(max_length=64),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    tenant = await get_tenant(session, tenant_id)
    key, plaintext = await issue_key(
        session,
        tenant,
        name=body.name,
        scopes=body.scopes,
        ip_allowlist=body.ip_allowlist,
        rate_limit_per_min=body.rate_limit_per_min,
        expires_at=body.expires_at,
    )
    return ok(request, {**key_view(key), "key": plaintext}, status_code=201)


@router.get("/tenants/{tenant_id}/keys", summary="租户的 API Key 列表")
async def get_keys(
    request: Request,
    tenant_id: str = Path(max_length=64),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    await get_tenant(session, tenant_id)
    return ok(request, {"items": [key_view(key) for key in await list_keys(session, tenant_id)]})


@router.post("/keys/{key_id}/revoke", summary="吊销 API Key")
async def post_revoke(
    request: Request,
    key_id: str = Path(max_length=64),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    return ok(request, key_view(await revoke_key(session, key_id)))


@router.get("/tenants/{tenant_id}/usage", summary="租户用量（按天、按接口）")
async def get_usage(
    request: Request,
    start: date = Query(...),
    end: date = Query(...),
    tenant_id: str = Path(max_length=64),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    if end < start:
        raise HubError(ErrorCode.INVALID_PARAM, "end is before start")
    await get_tenant(session, tenant_id)
    items = await usage_report(session, tenant_id=tenant_id, start=start, end=end)
    return ok(request, {"items": items})
