"""Publishing accounts: QR login (polled session or SSE), listing, removal."""

from __future__ import annotations

import contextlib
from typing import Final

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from hub.api.deps import get_identities, get_login_sessions, get_sau, get_session, require, settings_of
from hub.constants import TERMINAL_LOGIN_STATUSES, LoginSessionStatus, Platform, Scope
from hub.envelope import ok
from hub.errors import ErrorCode, HubError
from hub.models import utcnow
from hub.platforms import ensure_publishing
from hub.providers.sau import SauClient
from hub.schemas import LoginSessionCreate, LoginVerificationSubmit
from hub.security.auth import Principal
from hub.services.accounts import (
    account_view,
    delete_account,
    get_tenant_account,
    list_accounts,
    login_events,
    login_stream,
    new_sau_user_name,
)
from hub.services.data_identities import DataIdentities, records_for
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
    identities: DataIdentities | None = Depends(get_identities),
) -> JSONResponse:
    accounts = await list_accounts(session, principal.tenant_id, platform)
    records = await records_for(session, [account.id for account in accounts])
    importing = identities is not None and identities.imports_logins
    items = [account_view(account, records.get(account.id), importing=importing) for account in accounts]
    return ok(request, {"items": items})


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
    identities: DataIdentities | None = Depends(get_identities),
) -> StreamingResponse:
    ensure_publishing(platform, settings_of(request).publish_platforms)
    state = request.app.state
    events = login_stream(
        state.session_factory,
        sau,
        tenant_id=principal.tenant_id,
        platform=platform,
        display_name=name,
        identities=identities,
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
    identities: DataIdentities | None = Depends(get_identities),
) -> JSONResponse:
    ensure_publishing(body.platform, settings_of(request).publish_platforms)
    state = request.app.state
    now = utcnow()
    sau_login_id = new_sau_user_name()
    session = new_login_session(
        tenant_id=principal.tenant_id,
        platform=body.platform,
        name=body.name,
        login_timeout=settings_of(request).sau_login_timeout_seconds,
        now=now,
        sau_login_id=sau_login_id,
    )
    await store.save(session)
    view = session_view(session, now=now)
    events = login_events(
        state.session_factory,
        sau,
        tenant_id=principal.tenant_id,
        platform=body.platform,
        display_name=body.name,
        identities=identities,
        sau_user_name=sau_login_id,
        cancelled=lambda: store.finished(session.session_id),
    )
    spawn(state.background_tasks, drive_login_session(store, session, events, slots=state.login_slots))
    return ok(request, view, status_code=202)


@router.post(
    "/login-sessions/{session_id}/verification",
    summary="回应扫码后平台的身份验证：短信验证码，或“已发送短信”（会话状态为 verification_required 时）",
    status_code=202,
)
async def submit_login_verification(
    request: Request,
    body: LoginVerificationSubmit,
    session_id: str = Path(max_length=64),
    principal: Principal = Depends(MANAGE),
    sau: SauClient = Depends(get_sau),
    store: LoginSessionStore = Depends(get_login_sessions),
) -> JSONResponse:
    """Pass the owner's answer to the login waiting for it.

    ``{"code": "123456"}`` when the platform sent a code (``verification.method`` is
    ``sms``), ``{"resend": true}`` when it never arrived; ``{"sent": true}`` once the
    owner sent the SMS it asked for (``sms_send``).

    The outcome arrives on the session like every other step: ``succeeded`` once
    the platform accepts the code, or ``verification_required`` again with
    ``verification.error`` set when it does not.
    """
    session = await store.get_for_tenant(session_id, principal.tenant_id)
    now = utcnow()
    status = session.status_at(now)
    if status != LoginSessionStatus.VERIFICATION_REQUIRED.value or not session.sau_login_id:
        raise HubError(
            ErrorCode.CONFLICT,
            "this login session is not waiting for a verification code",
            details={"status": status},
        )
    await sau.submit_login_input(session.sau_login_id, body.code, sent=body.sent, resend=body.resend)
    kept = {
        key: value for key, value in (session.verification or {}).items() if key not in ("error", "notice")
    }
    session.verification = kept if body.resend else {**kept, "submitted_at": now.isoformat()}
    await store.save(session)
    return ok(request, session_view(session, now=now), status_code=202)


@router.delete(
    "/login-sessions/{session_id}",
    summary="取消扫码登录会话（换新二维码时取消旧的，立即释放服务端浏览器）",
)
async def cancel_login_session(
    request: Request,
    session_id: str = Path(max_length=64),
    principal: Principal = Depends(MANAGE),
    sau: SauClient = Depends(get_sau),
    store: LoginSessionStore = Depends(get_login_sessions),
) -> JSONResponse:
    """End a login nobody will finish. Each one holds a headless browser and one of the few
    login slots until it times out (minutes, more with an identity check), so a newer login
    for the same person would otherwise queue behind it. Finished sessions are returned as is.
    """
    session = await store.get_for_tenant(session_id, principal.tenant_id)
    now = utcnow()
    if session.status not in TERMINAL_LOGIN_STATUSES:
        session.status = LoginSessionStatus.EXPIRED.value
        session.qrcode = None
        session.error = {"code": "CANCELLED", "message": "the login was cancelled"}
        await store.save(session)
        if session.sau_login_id:
            with contextlib.suppress(HubError):
                await sau.cancel_login(session.sau_login_id)
    return ok(request, session_view(session, now=now))


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
    identities: DataIdentities | None = Depends(get_identities),
) -> JSONResponse:
    account = await get_tenant_account(session, account_id, principal.tenant_id)
    await delete_account(session, sau, account, identities)
    return ok(request, {"account_id": account_id, "deleted": True})
