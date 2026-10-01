"""Client for the Douyin data service (Douyin_TikTok_Download_API v5, "DTK").

DTK answers data requests asynchronously: ``?wait=N`` holds the connection up
to N seconds and returns 200 with the result, or 202 with a task id when the
upstream call takes longer. Both shapes are surfaced as :class:`Done` and
:class:`Pending`. DTK's error codes are folded into this gateway's smaller,
stable set; the original code travels in ``details.upstream_code``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final, Mapping, Union
from urllib.parse import quote

import httpx

from hub.errors import ErrorCode, HubError


class DtkPath:
    PARSE: Final = "/api/v1/parse"
    VIDEO: Final = "/api/v1/douyin/video"
    COMMENTS: Final = "/api/v1/douyin/video/comments"
    COMMENT_REPLIES: Final = "/api/v1/douyin/video/comments/replies"
    USER: Final = "/api/v1/douyin/user"
    USER_POSTS: Final = "/api/v1/douyin/user/posts"
    SEARCH: Final = "/api/v1/douyin/search"
    HOT: Final = "/api/v1/douyin/hot"
    TASK: Final = "/api/v1/tasks/{task_id}"
    DOWNLOADS: Final = "/api/v1/downloads"
    DOWNLOAD: Final = "/api/v1/downloads/{download_id}"
    DOWNLOAD_FILE: Final = "/api/v1/downloads/{download_id}/files/{name}"


HEADER_KEY: Final = "X-API-Key"
HTTP_OK: Final = 200
HTTP_ACCEPTED: Final = 202
PLATFORM_DOUYIN: Final = "douyin"
STATE_DONE: Final = "done"
STATE_FAILED: Final = "failed"
PUBLIC_META_KEYS: Final[tuple[str, ...]] = ("cached", "cursor")

_RETRY_LATER: Final = frozenset(
    {
        "RATE_LIMITED",
        "QUEUE_FULL",
        "IDENTITY_POOL_EXHAUSTED",
        "ENDPOINT_CIRCUIT_OPEN",
        "UPSTREAM_RISK_CONTROL",
        "SIGNING_FAILED",
        "DOWNLOADER_UNAVAILABLE",
    }
)
_BAD_INPUT: Final = frozenset({"INVALID_URL", "INVALID_PARAM", "UNSUPPORTED_CONTENT"})
_NO_CONTENT: Final = frozenset({"NOT_FOUND", "CONTENT_PRIVATE", "TASK_NOT_FOUND"})
_REJECTED_CREDENTIALS: Final = frozenset({"UNAUTHENTICATED", "FORBIDDEN_SCOPE"})
UPSTREAM_NOT_FOUND: Final = "NOT_FOUND"
UPSTREAM_NOT_CONFIGURED: Final = "NOT_CONFIGURED"


def map_upstream_error(error: Mapping[str, Any], *, missing_route_is_unconfigured: bool = False) -> HubError:
    code = str(error.get("code") or "")
    message = str(error.get("message") or code or "data service error")
    details: dict[str, Any] = {"upstream_code": code}
    if error.get("details"):
        details["upstream_details"] = error["details"]
    retry_after = error.get("retry_after")
    if code in _RETRY_LATER:
        return HubError(ErrorCode.UPSTREAM_UNAVAILABLE, message, details=details, retry_after=retry_after)
    if code in _BAD_INPUT:
        return HubError(ErrorCode.INVALID_PARAM, message, details=details)
    if code == UPSTREAM_NOT_FOUND and missing_route_is_unconfigured:
        return HubError(
            ErrorCode.NOT_CONFIGURED,
            "the data service does not provide this endpoint; apply patches/dtk to it",
            details=details,
        )
    if code in _NO_CONTENT:
        return HubError(ErrorCode.CONTENT_UNAVAILABLE, message, details=details)
    if code == UPSTREAM_NOT_CONFIGURED:
        return HubError(ErrorCode.NOT_CONFIGURED, message, details=details)
    if code in _REJECTED_CREDENTIALS:
        return HubError(
            ErrorCode.UPSTREAM_ERROR,
            "the data service rejected the gateway's credentials",
            details=details,
        )
    return HubError(ErrorCode.UPSTREAM_ERROR, message, details=details, retry_after=retry_after)


@dataclass(frozen=True)
class Done:
    data: Any
    meta: Mapping[str, Any] = field(default_factory=dict)

    def public_meta(self) -> dict[str, Any]:
        return {key: self.meta[key] for key in PUBLIC_META_KEYS if key in self.meta}


@dataclass(frozen=True)
class Pending:
    task_id: str


Outcome = Union[Done, Pending]


@dataclass(frozen=True)
class TaskState:
    finished: bool
    data: Any = None
    error: HubError | None = None


def _query(params: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in params.items() if value is not None}


class DtkClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        wait_seconds: int,
        timeout: float,
        max_connections: int = 20,
        max_keepalive: int = 10,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._wait = wait_seconds
        limits = httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive,
            keepalive_expiry=30,
        )
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={HEADER_KEY: api_key},
            timeout=timeout,
            limits=limits,
            transport=transport,
            trust_env=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        missing_route_is_unconfigured: bool = False,
    ) -> tuple[int, Any, Mapping[str, Any]]:
        try:
            response = await self._client.request(method, path, params=_query(params or {}), json=json)
        except httpx.TimeoutException as exc:
            raise HubError(ErrorCode.UPSTREAM_TIMEOUT, "the data service did not answer in time") from exc
        except httpx.HTTPError as exc:
            raise HubError(
                ErrorCode.UPSTREAM_UNAVAILABLE,
                "the data service is unreachable",
                details={"reason": type(exc).__name__},
            ) from exc
        return self._unwrap(response, missing_route_is_unconfigured=missing_route_is_unconfigured)

    @staticmethod
    def _unwrap(
        response: httpx.Response, *, missing_route_is_unconfigured: bool = False
    ) -> tuple[int, Any, Mapping[str, Any]]:
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict) or "success" not in body:
            raise HubError(
                ErrorCode.UPSTREAM_ERROR,
                "unexpected response from the data service",
                details={"status": response.status_code},
            )
        if not body.get("success"):
            raise map_upstream_error(
                body.get("error") or {}, missing_route_is_unconfigured=missing_route_is_unconfigured
            )
        return response.status_code, body.get("data"), body.get("meta") or {}

    async def _submit(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        missing_route_is_unconfigured: bool = False,
    ) -> Outcome:
        status, data, meta = await self._send(
            method,
            path,
            params={**(params or {}), "wait": self._wait},
            json=json,
            missing_route_is_unconfigured=missing_route_is_unconfigured,
        )
        if status == HTTP_ACCEPTED:
            task_id = data.get("task_id") if isinstance(data, dict) else None
            if not task_id:
                raise HubError(ErrorCode.UPSTREAM_ERROR, "the data service accepted work without a task id")
            return Pending(str(task_id))
        return Done(data=data, meta=meta)

    async def parse(self, url: str) -> Outcome:
        return await self._submit("POST", DtkPath.PARSE, json={"url": url})

    async def video(self, *, url: str | None, aweme_id: str | None) -> Outcome:
        return await self._submit("GET", DtkPath.VIDEO, params={"url": url, "aweme_id": aweme_id})

    async def comments(
        self, *, url: str | None, aweme_id: str | None, cursor: str | None, count: int
    ) -> Outcome:
        return await self._submit(
            "GET",
            DtkPath.COMMENTS,
            params={"url": url, "aweme_id": aweme_id, "cursor": cursor, "count": count},
        )

    async def comment_replies(
        self,
        *,
        url: str | None,
        aweme_id: str | None,
        comment_id: str,
        cursor: str | None,
        count: int,
    ) -> Outcome:
        return await self._submit(
            "GET",
            DtkPath.COMMENT_REPLIES,
            params={
                "url": url,
                "aweme_id": aweme_id,
                "comment_id": comment_id,
                "cursor": cursor,
                "count": count,
            },
        )

    async def user(self, *, url: str | None, sec_user_id: str | None) -> Outcome:
        return await self._submit("GET", DtkPath.USER, params={"url": url, "sec_user_id": sec_user_id})

    async def user_posts(
        self, *, url: str | None, sec_user_id: str | None, cursor: str | None, count: int
    ) -> Outcome:
        return await self._submit(
            "GET",
            DtkPath.USER_POSTS,
            params={"url": url, "sec_user_id": sec_user_id, "cursor": cursor, "count": count},
        )

    async def search(
        self,
        *,
        keyword: str,
        sort_type: int,
        publish_time: int,
        cursor: str | None,
        count: int,
    ) -> Outcome:
        return await self._submit(
            "GET",
            DtkPath.SEARCH,
            params={
                "keyword": keyword,
                "sort_type": sort_type,
                "publish_time": publish_time,
                "cursor": cursor,
                "count": count,
            },
            missing_route_is_unconfigured=True,
        )

    async def hot(self) -> Outcome:
        return await self._submit("GET", DtkPath.HOT, missing_route_is_unconfigured=True)

    async def task(self, task_id: str) -> TaskState:
        _, data, _ = await self._send("GET", DtkPath.TASK.format(task_id=quote(task_id, safe="")))
        body = data if isinstance(data, dict) else {}
        state = body.get("state")
        if state == STATE_DONE:
            return TaskState(finished=True, data=body.get("data"))
        if state == STATE_FAILED:
            return TaskState(finished=True, error=map_upstream_error(body.get("error") or {}))
        return TaskState(finished=False)

    async def start_download(self, *, url: str | None, aweme_id: str | None) -> dict[str, Any]:
        body: dict[str, Any] = {"url": url} if url else {"platform": PLATFORM_DOUYIN, "content_id": aweme_id}
        body["skip_existing"] = True
        _, data, _ = await self._send("POST", DtkPath.DOWNLOADS, json=body)
        return data if isinstance(data, dict) else {}

    async def download(self, download_id: str) -> dict[str, Any]:
        _, data, _ = await self._send("GET", DtkPath.DOWNLOAD.format(download_id=quote(download_id, safe="")))
        return data if isinstance(data, dict) else {}

    async def open_file(self, download_id: str, name: str) -> httpx.Response:
        """A streaming response for one stored file. The caller must close it."""
        path = DtkPath.DOWNLOAD_FILE.format(
            download_id=quote(download_id, safe=""), name=quote(name, safe="")
        )
        request = self._client.build_request("GET", path)
        try:
            response = await self._client.send(request, stream=True)
        except httpx.TimeoutException as exc:
            raise HubError(ErrorCode.UPSTREAM_TIMEOUT, "the data service did not answer in time") from exc
        except httpx.HTTPError as exc:
            raise HubError(ErrorCode.UPSTREAM_UNAVAILABLE, "the data service is unreachable") from exc
        if response.status_code != HTTP_OK:
            await response.aread()
            await response.aclose()
            self._unwrap(response)
            raise HubError(
                ErrorCode.UPSTREAM_ERROR,
                "the data service could not serve the file",
                details={"status": response.status_code},
            )
        return response
