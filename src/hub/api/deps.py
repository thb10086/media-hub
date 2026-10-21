"""Request-scoped dependencies: session, principal, upstream clients."""

from __future__ import annotations

import time
from typing import AsyncIterator, Awaitable, Callable, Final

import httpx
from fastapi import Depends, Request, Security
from fastapi.security import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession

from hub.constants import HEADER_API_KEY, Scope
from hub.errors import ErrorCode, HubError
from hub.models import utcnow
from hub.providers.channels import ChannelsProvider
from hub.providers.dtk import DtkClient
from hub.providers.sau import SauClient
from hub.security.auth import Principal, authenticate, presented_key
from hub.security.net import client_ip
from hub.services.cache import ResponseCache
from hub.services.data_identities import DataIdentities
from hub.services.login_sessions import LoginSessionStore
from hub.settings import Settings

FORWARDED_FOR_HEADER: Final = "X-Forwarded-For"
API_KEY_SCHEME: Final = APIKeyHeader(
    name=HEADER_API_KEY,
    scheme_name="ApiKeyAuth",
    description="租户 API Key。也可使用 Authorization: Bearer <key>。",
    auto_error=False,
)


def settings_of(request: Request) -> Settings:
    return request.app.state.settings


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as session:
        yield session


async def get_principal(
    request: Request,
    api_key: str | None = Security(API_KEY_SCHEME),
    session: AsyncSession = Depends(get_session),
) -> Principal:
    state = request.app.state
    ip = client_ip(
        request.client.host if request.client else None,
        request.headers.get(FORWARDED_FOR_HEADER),
        state.trusted_networks,
    )
    key = api_key.strip() if api_key else presented_key(request.headers)
    principal = await authenticate(session, key=key, ip=ip, now=utcnow())
    limit = (
        principal.rate_limit_per_min
        if principal.rate_limit_per_min is not None
        else state.settings.default_rate_limit_per_min
    )
    now = time.time()
    decision = await state.rate_limiter.hit(principal.key_id, limit, now=now)
    if decision is not None:
        request.state.rate_limit = decision
        if not decision.allowed:
            raise HubError(
                ErrorCode.RATE_LIMITED,
                "rate limit exceeded",
                details={"limit": limit},
                retry_after=max(1.0, decision.reset_at - now),
            )
    request.state.principal = principal
    return principal


def require(*scopes: Scope) -> Callable[..., Awaitable[Principal]]:
    async def dependency(principal: Principal = Depends(get_principal)) -> Principal:
        principal.require(*scopes)
        return principal

    return dependency


def get_dtk(request: Request) -> DtkClient:
    client = request.app.state.dtk
    if client is None:
        raise HubError(
            ErrorCode.NOT_CONFIGURED, "the Douyin data service is not configured (HUB_DTK_BASE_URL)"
        )
    return client


def get_sau(request: Request) -> SauClient:
    client = request.app.state.sau
    if client is None:
        raise HubError(
            ErrorCode.NOT_CONFIGURED, "the publishing service is not configured (HUB_SAU_BASE_URL)"
        )
    return client


def get_channels(request: Request) -> ChannelsProvider:
    return request.app.state.channels


def get_login_sessions(request: Request) -> LoginSessionStore:
    state = request.app.state
    if state.redis is None:
        raise HubError(ErrorCode.NOT_CONFIGURED, "login sessions need Redis (HUB_REDIS_URL)")
    return LoginSessionStore(state.redis, retention_seconds=state.settings.login_session_retention_seconds)


def get_cache(request: Request) -> ResponseCache:
    return request.app.state.cache


def get_http(request: Request) -> httpx.AsyncClient:
    return request.app.state.http


def get_identities(request: Request) -> DataIdentities | None:
    """Imports Douyin logins into DTK's identity pool; ``None`` without DTK or SAU."""
    return request.app.state.identities
