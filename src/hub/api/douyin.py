"""Douyin data: search, hot board, link parsing, comments, authors, downloads."""

from __future__ import annotations

from typing import Any, Final, Mapping

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from hub.api.deps import get_cache, get_dtk, get_session, require, settings_of
from hub.constants import (
    DOUYIN_SORT_VALUES,
    DataOperation,
    PublishTime,
    RankMode,
    Scope,
    SearchSort,
    TaskKind,
    TaskStatus,
)
from hub.envelope import ok
from hub.errors import ErrorCode, HubError
from hub.providers.dtk import Done, DtkClient, Outcome
from hub.schemas import DownloadRequest, ParseRequest
from hub.security.auth import Principal
from hub.services.cache import ResponseCache
from hub.services.data import (
    OPTION_RANK,
    PAYLOAD_DOWNLOAD_ID,
    postprocess,
    refresh_download_task,
    settle,
    start_download,
)
from hub.services.tasks import get_tenant_task, is_terminal, task_view

router = APIRouter(prefix="/api/v1/douyin", tags=["douyin"])

URL_QUERY = Query(default=None, max_length=4096, description="作品/主页链接，支持整段分享文案")
AWEME_ID_QUERY = Query(default=None, max_length=64, description="作品 ID，与 url 二选一")
SEC_USER_ID_QUERY = Query(default=None, max_length=256, description="作者 sec_user_id，与 url 二选一")
CURSOR_QUERY = Query(default=None, max_length=512, description="上一页返回的游标，首页不传")
COUNT_QUERY = Query(default=20, ge=1, le=50, description="每页条数")
PASSTHROUGH_HEADERS: Final[tuple[str, ...]] = ("Content-Type", "Content-Disposition")
IDENTITY_ENCODING: Final = "identity"

READ = require(Scope.DOUYIN_READ)
DOWNLOAD = require(Scope.MEDIA_DOWNLOAD)


def _exactly_one(**values: Any) -> None:
    given = [name for name, value in values.items() if value]
    if len(given) != 1:
        raise HubError(
            ErrorCode.INVALID_PARAM,
            f"provide exactly one of: {', '.join(values)}",
            details={"given": given},
        )


async def _answer(
    request: Request,
    session: AsyncSession,
    principal: Principal,
    outcome: Outcome,
    operation: DataOperation,
    options: Mapping[str, Any] | None = None,
) -> JSONResponse:
    result = await settle(
        outcome,
        session=session,
        tenant_id=principal.tenant_id,
        operation=operation,
        options=options or {},
        weights=settings_of(request).heat_weights,
    )
    return ok(request, result.data, status_code=result.status_code, meta=result.meta)


@router.get("/search", summary="关键词搜索作品（支持按热度排序）")
async def search(
    request: Request,
    keyword: str = Query(min_length=1, max_length=100),
    sort: SearchSort = Query(SearchSort.GENERAL, description="上游排序：综合/最多点赞/最新"),
    publish_time: PublishTime = Query(PublishTime.UNLIMITED, description="发布时间范围（天）"),
    cursor: str | None = CURSOR_QUERY,
    count: int = COUNT_QUERY,
    rank: RankMode = Query(RankMode.HEAT, description="heat=按热度分重排本页；upstream=保持平台顺序"),
    principal: Principal = Depends(READ),
    session: AsyncSession = Depends(get_session),
    dtk: DtkClient = Depends(get_dtk),
    cache: ResponseCache = Depends(get_cache),
) -> JSONResponse:
    settings = settings_of(request)
    cache_key = cache.search_key(keyword, DOUYIN_SORT_VALUES[sort], publish_time.value, cursor, count)
    cached = await cache.get(cache_key) if settings.cache_search_ttl else None
    if cached is not None:
        data = postprocess(
            DataOperation.SEARCH.value, cached, {OPTION_RANK: rank.value}, weights=settings.heat_weights
        )
        return ok(request, data, meta={"cached": True})
    outcome = await dtk.search(
        keyword=keyword,
        sort_type=DOUYIN_SORT_VALUES[sort],
        publish_time=publish_time.value,
        cursor=cursor,
        count=count,
    )
    result = await _answer(
        request, session, principal, outcome, DataOperation.SEARCH, {OPTION_RANK: rank.value}
    )
    if isinstance(outcome, Done) and settings.cache_search_ttl:
        await cache.put(cache_key, outcome.data, ttl=settings.cache_search_ttl)
    return result


@router.get("/hot", summary="抖音热榜")
async def hot(
    request: Request,
    principal: Principal = Depends(READ),
    session: AsyncSession = Depends(get_session),
    dtk: DtkClient = Depends(get_dtk),
    cache: ResponseCache = Depends(get_cache),
) -> JSONResponse:
    settings = settings_of(request)
    cache_key = cache.hot_key()
    cached = await cache.get(cache_key) if settings.cache_hot_ttl else None
    if cached is not None:
        return ok(request, cached, meta={"cached": True})
    outcome = await dtk.hot()
    result = await _answer(request, session, principal, outcome, DataOperation.HOT)
    if isinstance(outcome, Done) and settings.cache_hot_ttl:
        await cache.put(cache_key, outcome.data, ttl=settings.cache_hot_ttl)
    return result


@router.post("/parse", summary="解析分享链接/文案，返回作品详情与无水印地址")
async def parse(
    request: Request,
    body: ParseRequest,
    principal: Principal = Depends(READ),
    session: AsyncSession = Depends(get_session),
    dtk: DtkClient = Depends(get_dtk),
) -> JSONResponse:
    return await _answer(request, session, principal, await dtk.parse(body.url), DataOperation.PARSE)


@router.get("/video", summary="作品详情")
async def video(
    request: Request,
    url: str | None = URL_QUERY,
    aweme_id: str | None = AWEME_ID_QUERY,
    principal: Principal = Depends(READ),
    session: AsyncSession = Depends(get_session),
    dtk: DtkClient = Depends(get_dtk),
    cache: ResponseCache = Depends(get_cache),
) -> JSONResponse:
    _exactly_one(url=url, aweme_id=aweme_id)
    settings = settings_of(request)
    cache_key = cache.video_key(url=url, aweme_id=aweme_id)
    cached = await cache.get(cache_key) if settings.cache_video_ttl else None
    if cached is not None:
        return ok(request, cached, meta={"cached": True})
    outcome = await dtk.video(url=url, aweme_id=aweme_id)
    result = await _answer(request, session, principal, outcome, DataOperation.VIDEO)
    if isinstance(outcome, Done) and settings.cache_video_ttl:
        await cache.put(cache_key, outcome.data, ttl=settings.cache_video_ttl)
    return result


@router.get("/comments", summary="作品一级评论（分页）")
async def comments(
    request: Request,
    url: str | None = URL_QUERY,
    aweme_id: str | None = AWEME_ID_QUERY,
    cursor: str | None = CURSOR_QUERY,
    count: int = COUNT_QUERY,
    principal: Principal = Depends(READ),
    session: AsyncSession = Depends(get_session),
    dtk: DtkClient = Depends(get_dtk),
    cache: ResponseCache = Depends(get_cache),
) -> JSONResponse:
    _exactly_one(url=url, aweme_id=aweme_id)
    settings = settings_of(request)
    cache_key = cache.comments_key(aweme_id=aweme_id, url=url, cursor=cursor)
    cached = await cache.get(cache_key) if settings.cache_comments_ttl else None
    if cached is not None:
        return ok(request, cached, meta={"cached": True})
    outcome = await dtk.comments(url=url, aweme_id=aweme_id, cursor=cursor, count=count)
    result = await _answer(request, session, principal, outcome, DataOperation.COMMENTS)
    if isinstance(outcome, Done) and settings.cache_comments_ttl:
        await cache.put(cache_key, outcome.data, ttl=settings.cache_comments_ttl)
    return result


@router.get("/comments/replies", summary="评论的回复（分页）")
async def comment_replies(
    request: Request,
    comment_id: str = Query(min_length=1, max_length=64),
    url: str | None = URL_QUERY,
    aweme_id: str | None = AWEME_ID_QUERY,
    cursor: str | None = CURSOR_QUERY,
    count: int = COUNT_QUERY,
    principal: Principal = Depends(READ),
    session: AsyncSession = Depends(get_session),
    dtk: DtkClient = Depends(get_dtk),
) -> JSONResponse:
    _exactly_one(url=url, aweme_id=aweme_id)
    outcome = await dtk.comment_replies(
        url=url, aweme_id=aweme_id, comment_id=comment_id, cursor=cursor, count=count
    )
    return await _answer(request, session, principal, outcome, DataOperation.COMMENT_REPLIES)


@router.get("/user", summary="作者资料")
async def user(
    request: Request,
    url: str | None = URL_QUERY,
    sec_user_id: str | None = SEC_USER_ID_QUERY,
    principal: Principal = Depends(READ),
    session: AsyncSession = Depends(get_session),
    dtk: DtkClient = Depends(get_dtk),
    cache: ResponseCache = Depends(get_cache),
) -> JSONResponse:
    _exactly_one(url=url, sec_user_id=sec_user_id)
    settings = settings_of(request)
    cache_key = cache.user_key(url=url, sec_user_id=sec_user_id)
    cached = await cache.get(cache_key) if settings.cache_user_ttl else None
    if cached is not None:
        return ok(request, cached, meta={"cached": True})
    outcome = await dtk.user(url=url, sec_user_id=sec_user_id)
    result = await _answer(request, session, principal, outcome, DataOperation.USER)
    if isinstance(outcome, Done) and settings.cache_user_ttl:
        await cache.put(cache_key, outcome.data, ttl=settings.cache_user_ttl)
    return result


@router.get("/user/posts", summary="作者作品列表（分页）")
async def user_posts(
    request: Request,
    url: str | None = URL_QUERY,
    sec_user_id: str | None = SEC_USER_ID_QUERY,
    cursor: str | None = CURSOR_QUERY,
    count: int = COUNT_QUERY,
    principal: Principal = Depends(READ),
    session: AsyncSession = Depends(get_session),
    dtk: DtkClient = Depends(get_dtk),
) -> JSONResponse:
    _exactly_one(url=url, sec_user_id=sec_user_id)
    outcome = await dtk.user_posts(url=url, sec_user_id=sec_user_id, cursor=cursor, count=count)
    return await _answer(request, session, principal, outcome, DataOperation.USER_POSTS)


@router.post("/downloads", summary="服务端下载作品媒体（异步）")
async def create_download(
    request: Request,
    body: DownloadRequest,
    principal: Principal = Depends(DOWNLOAD),
    session: AsyncSession = Depends(get_session),
    dtk: DtkClient = Depends(get_dtk),
) -> JSONResponse:
    task = await start_download(
        session, dtk, tenant_id=principal.tenant_id, url=body.url, aweme_id=body.aweme_id
    )
    return ok(request, task_view(task), status_code=200 if is_terminal(task) else 202)


async def _download_task(session: AsyncSession, task_id: str, principal: Principal) -> Any:
    task = await get_tenant_task(session, task_id, principal.tenant_id)
    if task.kind != TaskKind.MEDIA_DOWNLOAD.value:
        raise HubError(ErrorCode.NOT_FOUND, "no such download", details={"task_id": task_id})
    return task


@router.get("/downloads/{task_id}", summary="下载任务状态与文件列表")
async def get_download(
    request: Request,
    task_id: str = Path(max_length=64),
    principal: Principal = Depends(DOWNLOAD),
    session: AsyncSession = Depends(get_session),
    dtk: DtkClient = Depends(get_dtk),
) -> JSONResponse:
    task = await _download_task(session, task_id, principal)
    if not is_terminal(task):
        task = await refresh_download_task(session, task, dtk)
    return ok(request, task_view(task))


@router.get("/downloads/{task_id}/files/{name}", summary="下载已存储的媒体文件")
async def download_file(
    task_id: str = Path(max_length=64),
    name: str = Path(max_length=255),
    principal: Principal = Depends(DOWNLOAD),
    session: AsyncSession = Depends(get_session),
    dtk: DtkClient = Depends(get_dtk),
) -> StreamingResponse:
    task = await _download_task(session, task_id, principal)
    files = (task.result or {}).get("files") or []
    if task.status != TaskStatus.SUCCEEDED.value or name not in {item.get("name") for item in files}:
        raise HubError(ErrorCode.NOT_FOUND, "no such file", details={"task_id": task_id, "name": name})
    upstream = await dtk.open_file(str(task.payload[PAYLOAD_DOWNLOAD_ID]), name)
    headers = {key: upstream.headers[key] for key in PASSTHROUGH_HEADERS if key in upstream.headers}
    encoding = upstream.headers.get("Content-Encoding", IDENTITY_ENCODING)
    if encoding == IDENTITY_ENCODING and "Content-Length" in upstream.headers:
        headers["Content-Length"] = upstream.headers["Content-Length"]
    return StreamingResponse(
        upstream.aiter_bytes(), headers=headers, background=BackgroundTask(upstream.aclose)
    )
