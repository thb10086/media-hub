"""Publishing accounts: QR login (polled session or SSE), listing, removal."""

from __future__ import annotations

from typing import Final

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from hub.api.deps import get_login_sessions, get_sau, get_session, require, settings_of
from hub.constants import Platform, Scope
from hub.envelope import ok
from hub.models import utcnow
from hub.platforms import ensure_publishing
from hub.providers.sau import SauClient
from hub.schemas import LoginSessionCreate
from hub.security.auth import Principal
from hub.services.accounts import (
    account_view,
    delete_account,
    get_tenant_account,
    list_accounts,
    login_events,
    login_stream,
)
from hub.services.login_sessions import (
    LoginSessionStore,
    drive_login_session,
    hold_slot,
    new_login_session,
    session_view,
    spawn,
)

SSE_MEDIA_TYPE: Final = "text/event-stream"
SSE_HEADERS: Final = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

router = APIRouter(prefix="/api/v1/accounts", tags=["accounts"])
MANAGE = require(Scope.ACCOUNTS)


@router.get("", summary="账号列表")
async def get_accounts(
    request: Request,
    platform: Platform | None = Query(default=None),
    principal: Principal = Depends(MANAGE),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    accounts = await list_accounts(session, principal.tenant_id, platform)
    return ok(request, {"items": [account_view(account) for account in accounts]})


@router.get(
    "/login",
    summary="扫码登录新账号（SSE：qrcode → success/failed）",
    response_class=StreamingResponse,
)
async def login(
    request: Request,
    platform: Platform = Query(...),
    name: str = Query(min_length=1, max_length=64, description="账号备注名"),
    principal: Principal = Depends(MANAGE),
    sau: SauClient = Depends(get_sau),
) -> StreamingResponse:
    ensure_publishing(platform, settings_of(request).publish_platforms)
    state = request.app.state
    events = login_stream(
        state.session_factory,
        sau,
        tenant_id=principal.tenant_id,
        platform=platform,
        display_name=name,
    )
    return StreamingResponse(
        hold_slot(state.login_slots, events), media_type=SSE_MEDIA_TYPE, headers=SSE_HEADERS
    )


@router.post(
    "/login-sessions",
    summary="创建扫码登录会话（Web 系统推荐：轮询获取二维码与结果）",
    status_code=202,
)
async def create_login_session(
    request: Request,
    body: LoginSessionCreate,
    principal: Principal = Depends(MANAGE),
    sau: SauClient = Depends(get_sau),
    store: LoginSessionStore = Depends(get_login_sessions),
) -> JSONResponse:
    ensure_publishing(body.platform, settings_of(request).publish_platforms)
    state = request.app.state
    now = utcnow()
    session = new_login_session(
        tenant_id=principal.tenant_id,
        platform=body.platform,
        name=body.name,
        login_timeout=settings_of(request).sau_login_timeout_seconds,
        now=now,
    )
    await store.save(session)
    view = session_view(session, now=now)
    events = login_events(
        state.session_factory,
        sau,
        tenant_id=principal.tenant_id,
        platform=body.platform,
        display_name=body.name,
    )
    spawn(state.background_tasks, drive_login_session(store, session, events, slots=state.login_slots))
    return ok(request, view, status_code=202)


@router.get("/login-sessions/{session_id}", summary="查询扫码登录会话：二维码、状态与绑定结果")
async def get_login_session(
    request: Request,
    session_id: str = Path(max_length=64),
    principal: Principal = Depends(MANAGE),
    store: LoginSessionStore = Depends(get_login_sessions),
) -> JSONResponse:
    session = await store.get_for_tenant(session_id, principal.tenant_id)
    return ok(request, session_view(session, now=utcnow()))


@router.delete("/{account_id}", summary="删除账号")
async def remove_account(
    request: Request,
    account_id: str = Path(max_length=64),
    principal: Principal = Depends(MANAGE),
    session: AsyncSession = Depends(get_session),
    sau: SauClient = Depends(get_sau),
) -> JSONResponse:
    account = await get_tenant_account(session, account_id, principal.tenant_id)
    await delete_account(session, sau, account)
    return ok(request, {"account_id": account_id, "deleted": True})
