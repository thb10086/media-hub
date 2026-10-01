"""QR login sessions for web front ends.

The SSE endpoint suits callers that can hold one connection open for minutes;
a SaaS backend usually cannot pass that through its own proxies to a browser.
A session runs the same login in the background: the caller creates it and
then polls it for the QR code and finally the bound account. State lives in
Redis, so any gateway process can answer the poll.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, AsyncIterator, Coroutine, Final, TypeVar

from redis.asyncio import Redis

from hub.constants import TERMINAL_LOGIN_STATUSES, LoginSessionStatus, Platform
from hub.errors import ErrorCode, HubError
from hub.models import utcnow
from hub.services.accounts import LoginEvent

log = logging.getLogger(__name__)

KEY_TEMPLATE: Final = "hub:login-session:{session_id}"
#: Added to the login timeout so a session never reads as expired while the
#: upstream login could still finish.
EXPIRY_GRACE_SECONDS: Final = 30.0
DATETIME_FIELDS: Final = ("created_at", "expires_at")
BUSY_MESSAGE: Final = "the login service stayed busy until this session expired; try again"
CRASH_MESSAGE: Final = "unexpected error during the QR login"

LoginEvents = AsyncIterator[tuple[LoginEvent, dict[str, Any]]]
T = TypeVar("T")


@dataclass
class LoginSession:
    session_id: str
    tenant_id: str
    platform: str
    name: str
    status: str
    created_at: datetime
    expires_at: datetime
    qrcode: str | None = None
    account: dict[str, Any] | None = None
    error: dict[str, Any] | None = None

    def to_json(self) -> str:
        data = asdict(self)
        for key in DATETIME_FIELDS:
            data[key] = data[key].isoformat()
        return json.dumps(data, ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str | bytes) -> LoginSession:
        data = json.loads(raw)
        for key in DATETIME_FIELDS:
            data[key] = datetime.fromisoformat(data[key])
        return cls(**data)

    def status_at(self, now: datetime) -> str:
        if self.status not in TERMINAL_LOGIN_STATUSES and now >= self.expires_at:
            return LoginSessionStatus.EXPIRED.value
        return self.status

    def apply(self, event: LoginEvent, data: dict[str, Any]) -> None:
        if event is LoginEvent.QRCODE:
            self.status = LoginSessionStatus.QRCODE_READY.value
            self.qrcode = data["qrcode"]
            return
        self.qrcode = None
        if event is LoginEvent.SUCCESS:
            self.status = LoginSessionStatus.SUCCEEDED.value
            self.account = data["account"]
        else:
            self.status = LoginSessionStatus.FAILED.value
            self.error = data


def new_login_session(
    *, tenant_id: str, platform: Platform, name: str, login_timeout: float, now: datetime
) -> LoginSession:
    return LoginSession(
        session_id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        platform=platform.value,
        name=name,
        status=LoginSessionStatus.PENDING.value,
        created_at=now,
        expires_at=now + timedelta(seconds=login_timeout + EXPIRY_GRACE_SECONDS),
    )


def session_view(session: LoginSession, *, now: datetime) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "platform": session.platform,
        "name": session.name,
        "status": session.status_at(now),
        "qrcode": session.qrcode,
        "account": session.account,
        "error": session.error,
        "created_at": session.created_at.isoformat(),
        "expires_at": session.expires_at.isoformat(),
    }


class LoginSessionStore:
    def __init__(self, redis: Redis, *, retention_seconds: float) -> None:
        self._redis = redis
        self._retention = retention_seconds

    async def save(self, session: LoginSession) -> None:
        remaining = (session.expires_at - utcnow()).total_seconds()
        ttl = max(1, int(remaining + self._retention))
        await self._redis.set(KEY_TEMPLATE.format(session_id=session.session_id), session.to_json(), ex=ttl)

    async def get_for_tenant(self, session_id: str, tenant_id: str) -> LoginSession:
        raw = await self._redis.get(KEY_TEMPLATE.format(session_id=session_id))
        session = LoginSession.from_json(raw) if raw is not None else None
        if session is None or session.tenant_id != tenant_id:
            raise HubError(ErrorCode.NOT_FOUND, "no such login session", details={"session_id": session_id})
        return session


async def hold_slot(slots: asyncio.Semaphore, stream: AsyncIterator[T]) -> AsyncIterator[T]:
    """Pass ``stream`` through while holding one of the limited login slots."""
    async with slots:
        async for item in stream:
            yield item


async def drive_login_session(
    store: LoginSessionStore, session: LoginSession, events: LoginEvents, *, slots: asyncio.Semaphore
) -> None:
    """Run one login to its end in the background, recording every step."""
    remaining = (session.expires_at - utcnow()).total_seconds()
    try:
        await asyncio.wait_for(slots.acquire(), timeout=max(remaining, 0.0))
    except asyncio.TimeoutError:
        session.apply(
            LoginEvent.FAILED, {"code": ErrorCode.UPSTREAM_UNAVAILABLE.value, "message": BUSY_MESSAGE}
        )
        await store.save(session)
        return
    try:
        async for event, data in events:
            session.apply(event, data)
            await store.save(session)
    except Exception:
        log.exception("login_session.crashed", extra={"session_id": session.session_id})
        session.apply(LoginEvent.FAILED, {"code": ErrorCode.INTERNAL.value, "message": CRASH_MESSAGE})
        await store.save(session)
    finally:
        slots.release()


def spawn(tasks: set[asyncio.Task[Any]], coroutine: Coroutine[Any, Any, None]) -> None:
    """Start a background task and keep a reference until it finishes."""
    task = asyncio.create_task(coroutine)
    tasks.add(task)
    task.add_done_callback(tasks.discard)
