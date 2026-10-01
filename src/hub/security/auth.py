"""API key authentication, producing the tenant-bound principal."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hub.constants import HEADER_API_KEY, KeyStatus, Scope, TenantStatus
from hub.errors import ErrorCode, HubError
from hub.models import ApiKey, Tenant, as_utc
from hub.security.keys import key_prefix, verify_key
from hub.security.net import ip_in, parse_networks

BEARER_PREFIX: Final = "bearer "
#: last_used_at is a hint for operators, not an audit trail: writing it on every
#: request would turn each read into a write.
LAST_USED_RESOLUTION: Final = timedelta(minutes=5)
INVALID_KEY_MESSAGE: Final = "invalid, revoked or expired API key"


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    key_id: str
    scopes: frozenset[str]
    rate_limit_per_min: int | None

    def require(self, *scopes: Scope) -> None:
        missing = [scope.value for scope in scopes if scope.value not in self.scopes]
        if missing:
            raise HubError(
                ErrorCode.FORBIDDEN_SCOPE,
                "this API key lacks the scope required for this endpoint",
                details={"required": missing, "granted": sorted(self.scopes)},
            )


def presented_key(headers: Mapping[str, str]) -> str | None:
    key = headers.get(HEADER_API_KEY)
    if key:
        return key.strip()
    authorization = headers.get("Authorization", "")
    if authorization.lower().startswith(BEARER_PREFIX):
        return authorization[len(BEARER_PREFIX) :].strip() or None
    return None


async def authenticate(session: AsyncSession, *, key: str | None, ip: str | None, now: datetime) -> Principal:
    if not key:
        raise HubError(ErrorCode.UNAUTHENTICATED, "missing API key")
    prefix = key_prefix(key)
    row = None
    if prefix is not None:
        statement = (
            select(ApiKey, Tenant).join(Tenant, Tenant.id == ApiKey.tenant_id).where(ApiKey.prefix == prefix)
        )
        row = (await session.execute(statement)).first()
    if row is None:
        raise HubError(ErrorCode.UNAUTHENTICATED, INVALID_KEY_MESSAGE)
    api_key, tenant = row
    expires_at = as_utc(api_key.expires_at)
    if (
        not verify_key(key, api_key.key_hash)
        or api_key.status != KeyStatus.ACTIVE.value
        or tenant.status != TenantStatus.ACTIVE.value
        or (expires_at is not None and expires_at <= now)
    ):
        raise HubError(ErrorCode.UNAUTHENTICATED, INVALID_KEY_MESSAGE)
    if api_key.ip_allowlist and not ip_in(ip, parse_networks(api_key.ip_allowlist)):
        raise HubError(
            ErrorCode.IP_NOT_ALLOWED,
            "this API key may not be used from this address",
            details={"ip": ip},
        )
    last_used = as_utc(api_key.last_used_at)
    if last_used is None or now - last_used >= LAST_USED_RESOLUTION:
        api_key.last_used_at = now
        await session.commit()
    return Principal(
        tenant_id=tenant.id,
        key_id=api_key.id,
        scopes=frozenset(api_key.scopes or ()),
        rate_limit_per_min=(
            api_key.rate_limit_per_min
            if api_key.rate_limit_per_min is not None
            else tenant.rate_limit_per_min
        ),
    )
