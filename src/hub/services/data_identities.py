"""Douyin accounts bound by QR login also stock the data service's identity pool.

DTK signs search, hot-board, detail and download requests with a cookie jar from
its identity pool, and answers ``IDENTITY_POOL_EXHAUSTED`` while the pool is
empty. A QR login already produces such a jar: SAU saves the browser's storage
state, and its patched login opens www.douyin.com before saving, so the jar
carries the cookies DTK needs (``ttwid`` and friends) next to the session.

After a Douyin login the gateway reads that jar back from SAU and imports it
into DTK, and retires the identity when the account is removed. Nothing here
ever fails a login or a removal - publishing does not depend on it. A failed
import is recorded on the account and retried by the worker; a retirement DTK
did not confirm keeps its row (``retiring``) until it does, because a merchant
who removed an account must not keep lending their session to the pool.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Mapping

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hub.constants import AccountStatus, DataIdentityStatus, Platform
from hub.errors import ErrorCode, HubError
from hub.models import AccountDataIdentity, PlatformAccount, as_utc, utcnow
from hub.providers.dtk import DtkClient
from hub.providers.sau import SauClient

if TYPE_CHECKING:
    from hub.settings import Settings

log = logging.getLogger("hub.data_identities")

DOUYIN_WEB_HOST: Final = "www.douyin.com"
DOUYIN_SITE: Final = "douyin.com"
#: DTK refuses a Douyin jar without it (identity/importing.py REQUIRED).
REQUIRED_COOKIE: Final = "ttwid"
RETIRE_REASON: Final = "media-hub: publishing account removed"
REPLACE_REASON: Final = "media-hub: replaced by a newer login of the same account"
ERROR_MAX_LENGTH: Final = 255
#: Retries back off linearly up to this many retry intervals.
MAX_BACKOFF_STEPS: Final = 6


def douyin_web_cookies(storage_state: Mapping[str, Any], *, now: float | None = None) -> dict[str, str]:
    """The cookies a browser would send to www.douyin.com, read from a Playwright storage state.

    Cookies for other hosts (creator.douyin.com, bytedance.com, ...) are left
    out, as are expired ones. When a name exists both for the host and for the
    whole site, the host's own value wins, as it would in the browser's header.
    """
    moment = time.time() if now is None else now
    picked: dict[str, tuple[int, str]] = {}
    for cookie in storage_state.get("cookies") or ():
        if not isinstance(cookie, Mapping):
            continue
        name = str(cookie.get("name") or "")
        value = cookie.get("value")
        rank = _host_rank(str(cookie.get("domain") or ""))
        if not name or value is None or rank is None:
            continue
        expires = cookie.get("expires")
        if isinstance(expires, (int, float)) and 0 < expires < moment:
            continue
        if name not in picked or rank > picked[name][0]:
            picked[name] = (rank, str(value))
    return {name: value for name, (_, value) in picked.items()}


def _host_rank(domain: str) -> int | None:
    host = domain.strip().lower().lstrip(".")
    if host == DOUYIN_WEB_HOST:
        return 2
    if host == DOUYIN_SITE:
        return 1
    return None


def cookie_header(cookies: Mapping[str, str]) -> str:
    return "; ".join(f"{name}={value}" for name, value in cookies.items())


def _describe(error: HubError) -> str:
    """A short, secret-free reason for the account view: code, message, missing cookies."""
    text = f"{error.code.value}: {error.message}"
    upstream = error.details.get("upstream_details") if isinstance(error.details, dict) else None
    missing = upstream.get("missing_required") if isinstance(upstream, dict) else None
    if missing:
        text += f" (missing {', '.join(str(name) for name in missing)})"
    return text[:ERROR_MAX_LENGTH]


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _iso(value: datetime | None) -> str | None:
    moment = as_utc(value)
    return moment.isoformat() if moment else None


def data_identity_view(
    account: PlatformAccount, record: AccountDataIdentity | None, *, importing: bool
) -> dict[str, Any] | None:
    """What the account list shows about the data side.

    ``None`` for platforms without one, and for an account never imported while
    importing is switched off.
    """
    if account.platform != Platform.DOUYIN.value:
        return None
    if record is None:
        return {"status": DataIdentityStatus.PENDING.value, "authenticated": False} if importing else None
    view: dict[str, Any] = {"status": record.status, "authenticated": record.authenticated}
    if record.session_expires_at is not None:
        view["expires_at"] = _iso(record.session_expires_at)
    if record.error:
        view["error"] = record.error
    view["updated_at"] = _iso(record.updated_at)
    return view


async def records_for(session: AsyncSession, account_ids: list[str]) -> dict[str, AccountDataIdentity]:
    if not account_ids:
        return {}
    rows = await session.scalars(
        select(AccountDataIdentity).where(AccountDataIdentity.account_id.in_(account_ids))
    )
    return {row.account_id: row for row in rows}


@dataclass(frozen=True)
class BrowserProfile:
    """How the browser that produced the jar presented itself; DTK must present the same way."""

    user_agent: str
    language: str
    timezone: str


async def import_jar(dtk: DtkClient, profile: BrowserProfile, jar: Mapping[str, Any]) -> dict[str, Any]:
    """Import the www.douyin.com cookies of a Playwright storage state as one DTK identity.

    Returns DTK's answer (``identity_id`` and the import ``report``). A jar
    without ``ttwid`` is refused here, before DTK would refuse it.
    """
    cookies = douyin_web_cookies(jar)
    if REQUIRED_COOKIE not in cookies:
        raise HubError(
            ErrorCode.UPSTREAM_ERROR,
            "the saved login has no www.douyin.com cookies; bind the account again",
            details={"upstream_details": {"missing_required": [REQUIRED_COOKIE]}},
        )
    imported = await dtk.import_identity(
        cookies=cookie_header(cookies),
        user_agent=profile.user_agent,
        language=profile.language,
        timezone=profile.timezone,
    )
    if not imported.get("identity_id"):
        raise HubError(ErrorCode.UPSTREAM_ERROR, "the data service stored no identity")
    return imported


def browser_profile(settings: Settings) -> BrowserProfile:
    return BrowserProfile(
        user_agent=settings.douyin_user_agent,
        language=settings.douyin_language,
        timezone=settings.douyin_timezone,
    )


@dataclass
class SyncReport:
    imported: int = 0
    failed: int = 0
    retired: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"imported": self.imported, "failed": self.failed, "retired": self.retired}


class DataIdentities:
    def __init__(
        self,
        dtk: DtkClient,
        sau: SauClient,
        profile: BrowserProfile,
        *,
        retry_after: timedelta,
        import_logins: bool = True,
    ) -> None:
        self._dtk = dtk
        self._sau = sau
        self._profile = profile
        self._retry_after = retry_after
        self._import_logins = import_logins

    @property
    def imports_logins(self) -> bool:
        """Off means: import nothing new, but still retire what earlier imports left in DTK."""
        return self._import_logins

    async def attach(self, session: AsyncSession, account: PlatformAccount) -> AccountDataIdentity | None:
        """Import the account's saved login into DTK. Never raises; the outcome is on the returned row."""
        if not self._import_logins or account.platform != Platform.DOUYIN.value:
            return None
        record = await session.get(AccountDataIdentity, account.id)
        if record is None:
            record = AccountDataIdentity(
                account_id=account.id,
                tenant_id=account.tenant_id,
                status=DataIdentityStatus.PENDING.value,
                attempts=0,
            )
            session.add(record)
        now = utcnow()
        previous = record.identity_id
        try:
            imported = await self._import(account)
        except HubError as error:
            self._failed(record, _describe(error), now)
        except Exception:
            log.exception("data_identity.import_crashed", extra={"account_id": account.id})
            self._failed(record, f"{ErrorCode.INTERNAL.value}: unexpected error while importing", now)
        else:
            report = imported.get("report") if isinstance(imported.get("report"), dict) else {}
            record.identity_id = str(imported["identity_id"])
            record.status = DataIdentityStatus.ACTIVE.value
            record.authenticated = bool(report.get("authenticated"))
            record.session_expires_at = _parse_time(report.get("expires_at"))
            record.error = None
            record.attempts = 0
            record.next_attempt_at = None
            log.info(
                "data_identity.imported",
                extra={
                    "account_id": account.id,
                    "identity_id": record.identity_id,
                    "authenticated": record.authenticated,
                },
            )
        record.updated_at = now
        await session.commit()
        if previous and previous != record.identity_id and record.status == DataIdentityStatus.ACTIVE.value:
            await self._retire_quietly(previous, REPLACE_REASON)
        return record

    async def _import(self, account: PlatformAccount) -> dict[str, Any]:
        jar = await self._sau.cookie_jar(account.sau_cookie_file)
        return await import_jar(self._dtk, self._profile, jar)

    def _failed(self, record: AccountDataIdentity, reason: str, now: datetime) -> None:
        record.attempts = (record.attempts or 0) + 1
        record.status = (
            DataIdentityStatus.ACTIVE.value if record.identity_id else DataIdentityStatus.FAILED.value
        )
        record.error = reason
        record.next_attempt_at = now + self._retry_after * min(record.attempts, MAX_BACKOFF_STEPS)
        log.warning("data_identity.import_failed", extra={"account_id": record.account_id, "reason": reason})

    async def detach(self, session: AsyncSession, account: PlatformAccount) -> None:
        """Retire the account's identity before the account goes. The caller commits."""
        record = await session.get(AccountDataIdentity, account.id)
        if record is None:
            return
        if record.identity_id and not await self._retire_quietly(record.identity_id, RETIRE_REASON):
            record.status = DataIdentityStatus.RETIRING.value
            record.next_attempt_at = utcnow() + self._retry_after
            record.updated_at = utcnow()
            return
        await session.delete(record)

    async def _retire_quietly(self, identity_id: str, reason: str) -> bool:
        try:
            await self._dtk.retire_identity(identity_id, reason=reason)
        except HubError as error:
            if error.code is ErrorCode.CONTENT_UNAVAILABLE:
                return True  # DTK no longer knows it: nothing left to retire
            log.warning(
                "data_identity.retire_failed", extra={"identity_id": identity_id, "reason": _describe(error)}
            )
            return False
        log.info("data_identity.retired", extra={"identity_id": identity_id, "reason": reason})
        return True

    async def sync(self, session_factory: async_sessionmaker[AsyncSession], *, limit: int = 20) -> SyncReport:
        """Import accounts that have no identity yet or are due a retry; finish pending retirements."""
        report = SyncReport()
        now = utcnow()
        due = or_(AccountDataIdentity.next_attempt_at.is_(None), AccountDataIdentity.next_attempt_at <= now)
        async with session_factory() as session:
            retiring = list(
                await session.scalars(
                    select(AccountDataIdentity)
                    .where(AccountDataIdentity.status == DataIdentityStatus.RETIRING.value, due)
                    .limit(limit)
                )
            )
            for record in retiring:
                if not record.identity_id or await self._retire_quietly(record.identity_id, RETIRE_REASON):
                    await session.delete(record)
                    report.retired += 1
                else:
                    record.next_attempt_at = now + self._retry_after
            await session.commit()
            if not self._import_logins:
                return report
            accounts = list(
                await session.scalars(
                    select(PlatformAccount)
                    .outerjoin(AccountDataIdentity, AccountDataIdentity.account_id == PlatformAccount.id)
                    .where(
                        PlatformAccount.platform == Platform.DOUYIN.value,
                        PlatformAccount.status == AccountStatus.ACTIVE.value,
                        or_(
                            AccountDataIdentity.account_id.is_(None),
                            (AccountDataIdentity.identity_id.is_(None) & due),
                        ),
                    )
                    .order_by(PlatformAccount.created_at)
                    .limit(limit)
                )
            )
        for account in accounts:
            async with session_factory() as session:
                fresh = await session.get(PlatformAccount, account.id)
                if fresh is None:
                    continue
                record = await self.attach(session, fresh)
                if record is not None and record.status == DataIdentityStatus.ACTIVE.value:
                    report.imported += 1
                else:
                    report.failed += 1
        if report.imported or report.failed or report.retired:
            log.info("data_identity.synced", extra=report.as_dict())
        return report


def build_data_identities(
    settings: Settings, dtk: DtkClient | None, sau: SauClient | None
) -> DataIdentities | None:
    """The service, when both sides exist; it also retires old identities with importing off."""
    if dtk is None or sau is None:
        return None
    return DataIdentities(
        dtk,
        sau,
        browser_profile(settings),
        retry_after=timedelta(seconds=settings.data_identity_retry_seconds),
        import_logins=settings.data_identity_from_logins,
    )
