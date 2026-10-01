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
    LOGIN: Final = "/login"


SAU_OK: Final = 200
SSE_DATA_PREFIX: Final = "data:"
LOGIN_SUCCEEDED: Final = "200"
LOGIN_FAILED: Final = "500"
LOGIN_TERMINAL: Final = frozenset({LOGIN_SUCCEEDED, LOGIN_FAILED})
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

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._client.request(method, path, **kwargs)
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

    async def stage_video(self, source: Path) -> str:
        """Hand a video to SAU and return the file name ``/postVideo`` expects."""
        name = f"{uuid.uuid4().hex}{source.suffix.lower()}"
        if self._video_dir is not None:
            await asyncio.to_thread(shutil.copyfile, source, self._video_dir / name)
            return name
        with source.open("rb") as handle:
            data = await self._request(
                "POST",
                SauPath.UPLOAD,
                files={"file": (name, handle, DEFAULT_VIDEO_TYPE)},
                timeout=self._publish_timeout,
            )
        return str(data)

    async def discard_video(self, name: str) -> None:
        """Remove a staged video. Only possible through the shared directory."""
        if self._video_dir is not None:
            await asyncio.to_thread((self._video_dir / name).unlink, missing_ok=True)

    async def post_video(self, spec: PostVideoSpec) -> None:
        await self._request("POST", SauPath.POST_VIDEO, json=spec.payload(), timeout=self._publish_timeout)

    async def login_events(self, platform_type: int, user_name: str) -> AsyncIterator[str]:
        """The raw SSE payloads of one QR login: the QR image, then ``200`` or ``500``."""
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
