"""Client for social-auto-upload's Flask backend (``sau_backend.py``).

SAU owns the browser automation: QR-code login stores a Playwright storage
state per account, and ``/postVideo`` drives the creator site synchronously,
returning only once the upload has finished - which is why publishing runs in
the background worker with a long timeout. SAU has no authentication of its
own and must only ever be reachable on the internal network.
"""

from __future__ import annotations

import asyncio
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Final

import httpx

from hub.errors import ErrorCode, HubError


class SauPath:
    UPLOAD: Final = "/upload"
    POST_VIDEO: Final = "/postVideo"
    ACCOUNTS: Final = "/getAccounts"
    DELETE_ACCOUNT: Final = "/deleteAccount"
    DOWNLOAD_COOKIE: Final = "/downloadCookie"
    LOGIN: Final = "/login"
    LOGIN_INPUT: Final = "/loginInput"
    LOGIN_CANCEL: Final = "/loginCancel"


SAU_OK: Final = 200
SSE_DATA_PREFIX: Final = "data:"
LOGIN_SUCCEEDED: Final = "200"
LOGIN_FAILED: Final = "500"
LOGIN_TERMINAL: Final = frozenset({LOGIN_SUCCEEDED, LOGIN_FAILED})
#: The platform asked the account owner to prove it is them; JSON follows the prefix.
LOGIN_VERIFY_PREFIX: Final = "verify:"
#: Sent while waiting on the owner, so the stream's read timeout does not fire.
LOGIN_KEEPALIVE: Final = "ping"
ACCOUNT_ROW_FIELDS: Final = 5
DEFAULT_VIDEO_TYPE: Final = "video/mp4"
VIDEOS_PER_DAY: Final = 1


@dataclass(frozen=True)
class SauAccount:
    id: int
    platform_type: int
    cookie_file: str
    user_name: str
    status: int


@dataclass(frozen=True)
class PostVideoSpec:
    platform_type: int
    video_file: str
    cookie_file: str
    title: str
    tags: tuple[str, ...] = ()
    daily_times: tuple[int, ...] = ()
    start_days: int = 0
    draft: bool = False
    #: Body text and a staged portrait cover: read by the patched Douyin route (deploy/sau/patch-publish.py).
    description: str | None = None
    cover_file: str = ""

    def payload(self) -> dict[str, Any]:
        scheduled = bool(self.daily_times)
        return {
            "type": self.platform_type,
            "fileList": [self.video_file],
            "accountList": [self.cookie_file],
            "title": self.title,
            "tags": list(self.tags),
            "category": 0,
            "enableTimer": scheduled,
            "videosPerDay": VIDEOS_PER_DAY,
            "dailyTimes": list(self.daily_times) if scheduled else None,
            "startDays": self.start_days,
            "isDraft": self.draft,
            "thumbnail": "",
            "desc": self.description or "",
            "thumbnailPortrait": self.cover_file,
            "productLink": "",
            "productTitle": "",
        }


class SauClient:
    def __init__(
        self,
        base_url: str,
        *,
        request_timeout: float,
        publish_timeout: float,
        login_timeout: float,
        video_dir: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._request_timeout = request_timeout
        self._publish_timeout = publish_timeout
        self._login_timeout = login_timeout
        self._video_dir = Path(video_dir) if video_dir else None
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=request_timeout,
            transport=transport,
            trust_env=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise HubError(
                ErrorCode.UPSTREAM_TIMEOUT, "the publishing service did not answer in time"
            ) from exc
        except httpx.HTTPError as exc:
            raise HubError(
                ErrorCode.UPSTREAM_UNAVAILABLE,
                "the publishing service is unreachable",
                details={"reason": type(exc).__name__},
            ) from exc

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self._send(method, path, **kwargs)
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise HubError(
                ErrorCode.UPSTREAM_ERROR,
                "unexpected response from the publishing service",
                details={"status": response.status_code},
            )
        if body.get("code") != SAU_OK:
            raise HubError(
                ErrorCode.UPSTREAM_ERROR,
                str(body.get("msg") or "the publishing service reported a failure"),
                details={"status": response.status_code, "upstream_code": body.get("code")},
            )
        return body.get("data")

    async def list_accounts(self) -> list[SauAccount]:
        rows = await self._request("GET", SauPath.ACCOUNTS) or []
        return [
            SauAccount(
                id=int(row[0]),
                platform_type=int(row[1]),
                cookie_file=str(row[2]),
                user_name=str(row[3]),
                status=int(row[4] or 0),
            )
            for row in rows
            if isinstance(row, list) and len(row) >= ACCOUNT_ROW_FIELDS
        ]

    async def find_account(self, user_name: str) -> SauAccount | None:
        for account in await self.list_accounts():
            if account.user_name == user_name:
                return account
        return None

    async def delete_account(self, sau_account_id: int) -> None:
        await self._request("GET", SauPath.DELETE_ACCOUNT, params={"id": sau_account_id})

    async def cookie_jar(self, cookie_file: str) -> dict[str, Any]:
        """The Playwright storage state SAU saved for an account: ``{"cookies": [...], "origins": [...]}``.

        ``/downloadCookie`` answers with the file itself on success and with SAU's
        JSON envelope only on failure, so it does not go through ``_request``.
        """
        response = await self._send("GET", SauPath.DOWNLOAD_COOKIE, params={"filePath": cookie_file})
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code != httpx.codes.OK or not isinstance(body, dict):
            raise HubError(
                ErrorCode.UPSTREAM_ERROR,
                "the publishing service has no saved login for this account",
                details={"status": response.status_code},
            )
        if not isinstance(body.get("cookies"), list):
            raise HubError(
                ErrorCode.UPSTREAM_ERROR,
                str(body.get("msg") or "the saved login is not a browser storage state"),
                details={"status": response.status_code},
            )
        return body

    async def stage_video(self, source: Path, content_type: str = DEFAULT_VIDEO_TYPE) -> str:
        """Hand a file (the video, or its cover) to SAU and return the name ``/postVideo`` expects."""
        name = f"{uuid.uuid4().hex}{source.suffix.lower()}"
        if self._video_dir is not None:
            await asyncio.to_thread(shutil.copyfile, source, self._video_dir / name)
            return name
        with source.open("rb") as handle:
            data = await self._request(
                "POST",
                SauPath.UPLOAD,
                files={"file": (name, handle, content_type)},
                timeout=self._publish_timeout,
            )
        return str(data)

    async def discard_video(self, name: str) -> None:
        """Remove a staged video. Only possible through the shared directory."""
        if self._video_dir is not None:
            await asyncio.to_thread((self._video_dir / name).unlink, missing_ok=True)

    async def post_video(self, spec: PostVideoSpec) -> None:
        await self._request("POST", SauPath.POST_VIDEO, json=spec.payload(), timeout=self._publish_timeout)

    async def submit_login_input(
        self, user_name: str, code: str | None = None, *, sent: bool = False, resend: bool = False
    ) -> None:
        """Hand the owner's answer to the login that asked: the SMS code, "sent" or "resend"."""
        answer: dict[str, str] = {"id": user_name}
        if sent:
            answer["action"] = "sent"
        elif resend:
            answer["action"] = "resend"
        else:
            answer["code"] = code or ""
        await self._request("POST", SauPath.LOGIN_INPUT, json=answer)

    async def cancel_login(self, user_name: str) -> None:
        """Stop a login nobody will finish: SAU closes its browser instead of waiting minutes."""
        await self._request("POST", SauPath.LOGIN_CANCEL, json={"id": user_name})

    async def login_events(self, platform_type: int, user_name: str) -> AsyncIterator[str]:
        """The raw SSE payloads of one QR login.

        QR images (repeatable), ``verify:{json}`` when the platform wants an
        identity check, ``ping`` keepalives, then ``200`` or ``500``.
        """
        timeout = httpx.Timeout(self._login_timeout, connect=self._request_timeout)
        try:
            async with self._client.stream(
                "GET",
                SauPath.LOGIN,
                params={"type": platform_type, "id": user_name},
                timeout=timeout,
            ) as response:
                if response.status_code != SAU_OK:
                    raise HubError(
                        ErrorCode.UPSTREAM_ERROR,
                        "the publishing service refused the login request",
                        details={"status": response.status_code},
                    )
                async for line in response.aiter_lines():
                    if not line.startswith(SSE_DATA_PREFIX):
                        continue
                    payload = line[len(SSE_DATA_PREFIX) :].strip()
                    if payload:
                        yield payload
                    if payload in LOGIN_TERMINAL:
                        return
        except httpx.TimeoutException as exc:
            raise HubError(ErrorCode.UPSTREAM_TIMEOUT, "the QR login timed out") from exc
        except httpx.HTTPError as exc:
            raise HubError(ErrorCode.UPSTREAM_UNAVAILABLE, "the publishing service is unreachable") from exc
