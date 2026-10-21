"""Publishing accounts: QR login through social-auto-upload, tenant ownership."""

from __future__ import annotations

import json
import uuid
from datetime import timezone
from enum import Enum
from typing import Any, AsyncIterator, Awaitable, Callable, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hub.constants import SAU_PLATFORM_TYPES, Platform
from hub.errors import ErrorCode, HubError
from hub.models import AccountDataIdentity, PlatformAccount, as_utc
from hub.providers.sau import LOGIN_FAILED, LOGIN_SUCCEEDED, LOGIN_VERIFY_PREFIX, SauClient
from hub.services.data_identities import DataIdentities, data_identity_view

SAU_USER_NAME_PREFIX: Final = "hub-"
#: What a QR code payload can be: an inline image or a URL to one.
QRCODE_PREFIXES: Final = ("data:image/", "https://", "http://")
VERIFICATION_METHODS: Final = frozenset({"sms", "sms_send", "unsupported"})
VERIFICATION_TEXT_LIMIT: Final = 200
#: A screenshot of the platform's own prompt, so the owner sees what it asks for.
SCREENSHOT_PREFIX: Final = "data:image/"
SCREENSHOT_LIMIT: Final = 400_000


class LoginEvent(str, Enum):
    QRCODE = "qrcode"
    #: The platform wants the account owner to prove it is them (an SMS code).
    VERIFICATION = "verification"
    SUCCESS = "success"
    FAILED = "failed"


def verification_view(raw: str) -> dict[str, Any]:
    """The part of SAU's ``verify:{json}`` payload worth showing, bounded and typed.

    ``method``: ``sms`` (a code was sent to ``phone``, a masked number),
    ``sms_send`` (the owner must send an SMS; ``message`` says what and where) or
    ``unsupported`` (a check this service cannot relay; ``message`` is what the
    platform says). ``error`` is set when the platform refused the last answer;
    ``notice`` is neutral news (code resent, still checking).
    """
    try:
        data = json.loads(raw)
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    method = data.get("method") if data.get("method") in VERIFICATION_METHODS else "unsupported"
    view: dict[str, Any] = {"method": method}
    for key in ("phone", "message", "error", "notice"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            view[key] = value.strip()[:VERIFICATION_TEXT_LIMIT]
    screenshot = data.get("screenshot")
    if isinstance(screenshot, str) and screenshot.startswith(SCREENSHOT_PREFIX):
        if len(screenshot) <= SCREENSHOT_LIMIT:
            view["screenshot"] = screenshot
    return view


def new_sau_user_name() -> str:
    return f"{SAU_USER_NAME_PREFIX}{uuid.uuid4().hex}"


def sse(event: LoginEvent, data: dict[str, Any]) -> str:
    return f"event: {event.value}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def account_view(
    account: PlatformAccount,
    data_identity: AccountDataIdentity | None = None,
    *,
    importing: bool = False,
) -> dict[str, Any]:
    created = as_utc(account.created_at)
    return {
        "account_id": account.id,
        "platform": account.platform,
        "name": account.display_name,
        "status": account.status,
        "created_at": created.astimezone(timezone.utc).isoformat() if created else None,
        "data_identity": data_identity_view(account, data_identity, importing=importing),
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


async def _forget_sau_login(sau: SauClient, sau_user_name: str) -> None:
    """Remove the jar SAU stored for a login nobody wants any more (best effort)."""
    try:
        stored = await sau.find_account(sau_user_name)
        if stored is not None:
            await sau.delete_account(stored.id)
    except HubError:
        pass


def _failure(code: ErrorCode, message: str) -> tuple[LoginEvent, dict[str, Any]]:
    return LoginEvent.FAILED, {"code": code.value, "message": message}


async def login_events(
    session_factory: async_sessionmaker[AsyncSession],
    sau: SauClient,
    *,
    tenant_id: str,
    platform: Platform,
    display_name: str,
    identities: DataIdentities | None = None,
    sau_user_name: str | None = None,
    cancelled: Callable[[], Awaitable[bool]] | None = None,
) -> AsyncIterator[tuple[LoginEvent, dict[str, Any]]]:
    """One QR login: ``qrcode`` (repeatable), ``verification`` when the platform asks the
    owner for an SMS code (repeatable, e.g. after a wrong code), then exactly one
    ``success`` or ``failed``.

    With ``identities``, a Douyin login is also imported into the data service's
    identity pool before ``success`` is sent; its outcome is in the account's
    ``data_identity`` and never turns the login into a failure. ``sau_user_name``
    is the login's name in SAU, which is where a verification code is sent.
    ``cancelled`` is asked right before the account is registered: a login that
    finishes upstream just as its session was cancelled is undone in SAU instead.
    """
    sau_user_name = sau_user_name or new_sau_user_name()
    try:
        async for payload in sau.login_events(SAU_PLATFORM_TYPES[platform], sau_user_name):
            if payload == LOGIN_SUCCEEDED:
                if cancelled is not None and await cancelled():
                    await _forget_sau_login(sau, sau_user_name)
                    yield _failure(ErrorCode.CONFLICT, "the login was cancelled")
                    return
                async with session_factory() as session:
                    account = await register_account(
                        session,
                        sau,
                        tenant_id=tenant_id,
                        platform=platform,
                        display_name=display_name,
                        sau_user_name=sau_user_name,
                    )
                    record = await identities.attach(session, account) if identities else None
                    view = account_view(
                        account, record, importing=identities is not None and identities.imports_logins
                    )
                yield LoginEvent.SUCCESS, {"account": view}
                return
            if payload == LOGIN_FAILED:
                yield _failure(ErrorCode.UPSTREAM_ERROR, "the QR login failed or timed out")
                return
            if payload.startswith(LOGIN_VERIFY_PREFIX):
                yield LoginEvent.VERIFICATION, verification_view(payload[len(LOGIN_VERIFY_PREFIX) :])
            elif payload.startswith(QRCODE_PREFIXES):
                yield LoginEvent.QRCODE, {"qrcode": payload}
            # Anything else is a keepalive (or something a newer SAU says): nothing to show.
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
    identities: DataIdentities | None = None,
) -> AsyncIterator[str]:
    """The same login as :func:`login_events`, rendered as server-sent events."""
    async for event, data in login_events(
        session_factory,
        sau,
        tenant_id=tenant_id,
        platform=platform,
        display_name=display_name,
        identities=identities,
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


async def delete_account(
    session: AsyncSession,
    sau: SauClient,
    account: PlatformAccount,
    identities: DataIdentities | None = None,
) -> None:
    await sau.delete_account(account.sau_account_id)
    if identities is not None:
        await identities.detach(session, account)
    await session.delete(account)
    await session.commit()
