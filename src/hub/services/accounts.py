"""Publishing accounts: QR login through social-auto-upload, tenant ownership."""

from __future__ import annotations

import json
import uuid
from datetime import timezone
from enum import Enum
from typing import Any, AsyncIterator, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hub.constants import SAU_PLATFORM_TYPES, Platform
from hub.errors import ErrorCode, HubError
from hub.models import PlatformAccount, as_utc
from hub.providers.sau import LOGIN_FAILED, LOGIN_SUCCEEDED, SauClient

SAU_USER_NAME_PREFIX: Final = "hub-"


class LoginEvent(str, Enum):
    QRCODE = "qrcode"
    SUCCESS = "success"
    FAILED = "failed"


def new_sau_user_name() -> str:
    return f"{SAU_USER_NAME_PREFIX}{uuid.uuid4().hex}"


def sse(event: LoginEvent, data: dict[str, Any]) -> str:
    return f"event: {event.value}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def account_view(account: PlatformAccount) -> dict[str, Any]:
    created = as_utc(account.created_at)
    return {
        "account_id": account.id,
        "platform": account.platform,
        "name": account.display_name,
        "status": account.status,
        "created_at": created.astimezone(timezone.utc).isoformat() if created else None,
    }


async def register_account(
    session: AsyncSession,
    sau: SauClient,
    *,
    tenant_id: str,
    platform: Platform,
    display_name: str,
    sau_user_name: str,
) -> PlatformAccount:
    stored = await sau.find_account(sau_user_name)
    if stored is None:
        raise HubError(
            ErrorCode.UPSTREAM_ERROR,
            "the login succeeded but the publishing service did not store the account",
        )
    account = PlatformAccount(
        tenant_id=tenant_id,
        platform=platform.value,
        display_name=display_name,
        sau_account_id=stored.id,
        sau_user_name=sau_user_name,
        sau_cookie_file=stored.cookie_file,
    )
    session.add(account)
    await session.commit()
    return account


def _failure(code: ErrorCode, message: str) -> tuple[LoginEvent, dict[str, Any]]:
    return LoginEvent.FAILED, {"code": code.value, "message": message}


async def login_events(
    session_factory: async_sessionmaker[AsyncSession],
    sau: SauClient,
    *,
    tenant_id: str,
    platform: Platform,
    display_name: str,
) -> AsyncIterator[tuple[LoginEvent, dict[str, Any]]]:
    """One QR login: ``qrcode`` (repeatable), then exactly one ``success`` or ``failed``."""
    sau_user_name = new_sau_user_name()
    try:
        async for payload in sau.login_events(SAU_PLATFORM_TYPES[platform], sau_user_name):
            if payload == LOGIN_SUCCEEDED:
                async with session_factory() as session:
                    account = await register_account(
                        session,
                        sau,
                        tenant_id=tenant_id,
                        platform=platform,
                        display_name=display_name,
                        sau_user_name=sau_user_name,
                    )
                    view = account_view(account)
                yield LoginEvent.SUCCESS, {"account": view}
                return
            if payload == LOGIN_FAILED:
                yield _failure(ErrorCode.UPSTREAM_ERROR, "the QR login failed or timed out")
                return
            yield LoginEvent.QRCODE, {"qrcode": payload}
    except HubError as error:
        yield LoginEvent.FAILED, {"code": error.code.value, "message": error.message}
        return
    yield _failure(ErrorCode.UPSTREAM_ERROR, "the login stream ended unexpectedly")


async def login_stream(
    session_factory: async_sessionmaker[AsyncSession],
    sau: SauClient,
    *,
    tenant_id: str,
    platform: Platform,
    display_name: str,
) -> AsyncIterator[str]:
    """The same login as :func:`login_events`, rendered as server-sent events."""
    async for event, data in login_events(
        session_factory, sau, tenant_id=tenant_id, platform=platform, display_name=display_name
    ):
        yield sse(event, data)


async def list_accounts(
    session: AsyncSession, tenant_id: str, platform: Platform | None = None
) -> list[PlatformAccount]:
    statement = select(PlatformAccount).where(PlatformAccount.tenant_id == tenant_id)
    if platform is not None:
        statement = statement.where(PlatformAccount.platform == platform.value)
    return list((await session.scalars(statement.order_by(PlatformAccount.created_at))).all())


async def get_tenant_account(session: AsyncSession, account_id: str, tenant_id: str) -> PlatformAccount:
    account = await session.get(PlatformAccount, account_id)
    if account is None or account.tenant_id != tenant_id:
        raise HubError(ErrorCode.NOT_FOUND, "no such account", details={"account_id": account_id})
    return account


async def delete_account(session: AsyncSession, sau: SauClient, account: PlatformAccount) -> None:
    await sau.delete_account(account.sau_account_id)
    await session.delete(account)
    await session.commit()
