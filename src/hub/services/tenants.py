"""Tenants and their API keys. Shared by the admin API and the CLI."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from hub.constants import KeyStatus, Scope
from hub.errors import ErrorCode, HubError
from hub.models import ApiKey, Tenant, as_utc
from hub.security.keys import generate_key, generate_secret
from hub.security.net import parse_networks


def _iso(value: datetime | None) -> str | None:
    moment = as_utc(value)
    return moment.isoformat() if moment else None


def tenant_view(tenant: Tenant) -> dict[str, Any]:
    return {
        "tenant_id": tenant.id,
        "name": tenant.name,
        "status": tenant.status,
        "rate_limit_per_min": tenant.rate_limit_per_min,
        "created_at": _iso(tenant.created_at),
    }


def key_view(key: ApiKey) -> dict[str, Any]:
    return {
        "key_id": key.id,
        "tenant_id": key.tenant_id,
        "name": key.name,
        "prefix": key.prefix,
        "scopes": list(key.scopes or ()),
        "ip_allowlist": list(key.ip_allowlist or ()),
        "status": key.status,
        "rate_limit_per_min": key.rate_limit_per_min,
        "expires_at": _iso(key.expires_at),
        "last_used_at": _iso(key.last_used_at),
        "created_at": _iso(key.created_at),
    }


async def create_tenant(session: AsyncSession, *, name: str, rate_limit_per_min: int | None = None) -> Tenant:
    tenant = Tenant(name=name, rate_limit_per_min=rate_limit_per_min, webhook_secret=generate_secret())
    session.add(tenant)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HubError(ErrorCode.CONFLICT, "a tenant with this name exists", details={"name": name}) from exc
    return tenant


async def get_tenant(session: AsyncSession, tenant_id: str) -> Tenant:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HubError(ErrorCode.NOT_FOUND, "no such tenant", details={"tenant_id": tenant_id})
    return tenant


async def find_tenant_by_name(session: AsyncSession, name: str) -> Tenant | None:
    return await session.scalar(select(Tenant).where(Tenant.name == name))


async def list_tenants(session: AsyncSession) -> list[Tenant]:
    return list((await session.scalars(select(Tenant).order_by(Tenant.created_at))).all())


async def issue_key(
    session: AsyncSession,
    tenant: Tenant,
    *,
    name: str,
    scopes: Iterable[Scope],
    ip_allowlist: Iterable[str] = (),
    rate_limit_per_min: int | None = None,
    expires_at: datetime | None = None,
) -> tuple[ApiKey, str]:
    """The stored key and the plaintext key, which is shown exactly once."""
    allowlist = [cidr.strip() for cidr in ip_allowlist if cidr.strip()]
    try:
        parse_networks(allowlist)
    except ValueError as exc:
        raise HubError(
            ErrorCode.INVALID_PARAM, "invalid CIDR in ip_allowlist", details={"field": "ip_allowlist"}
        ) from exc
    generated = generate_key()
    key = ApiKey(
        tenant_id=tenant.id,
        name=name,
        prefix=generated.prefix,
        key_hash=generated.key_hash,
        scopes=sorted({scope.value for scope in scopes}),
        ip_allowlist=allowlist,
        rate_limit_per_min=rate_limit_per_min,
        expires_at=expires_at,
    )
    session.add(key)
    await session.commit()
    return key, generated.key


async def list_keys(session: AsyncSession, tenant_id: str) -> list[ApiKey]:
    return list(
        (
            await session.scalars(
                select(ApiKey).where(ApiKey.tenant_id == tenant_id).order_by(ApiKey.created_at)
            )
        ).all()
    )


async def revoke_key(session: AsyncSession, key_id: str) -> ApiKey:
    key = await session.get(ApiKey, key_id)
    if key is None:
        raise HubError(ErrorCode.NOT_FOUND, "no such key", details={"key_id": key_id})
    key.status = KeyStatus.REVOKED.value
    await session.commit()
    return key
