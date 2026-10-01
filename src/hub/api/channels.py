"""WeChat Channels (视频号) data, served by the configured provider."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from hub.api.deps import get_channels, require
from hub.constants import Scope
from hub.envelope import ok
from hub.errors import ErrorCode, HubError
from hub.providers.channels import ChannelsProvider
from hub.security.auth import Principal

router = APIRouter(prefix="/api/v1/channels", tags=["channels"])
READ = require(Scope.CHANNELS_READ)

URL_QUERY = Query(default=None, max_length=4096, description="视频号分享链接")
OBJECT_ID_QUERY = Query(default=None, max_length=128, description="视频号作品 ID，与 url 二选一")
CURSOR_QUERY = Query(default=None, max_length=512)
COUNT_QUERY = Query(default=20, ge=1, le=50)


def _exactly_one(**values: Any) -> None:
    if sum(1 for value in values.values() if value) != 1:
        raise HubError(ErrorCode.INVALID_PARAM, f"provide exactly one of: {', '.join(values)}")


@router.get("/search", summary="视频号关键词搜索")
async def search(
    request: Request,
    keyword: str = Query(min_length=1, max_length=100),
    cursor: str | None = CURSOR_QUERY,
    count: int = COUNT_QUERY,
    principal: Principal = Depends(READ),
    provider: ChannelsProvider = Depends(get_channels),
) -> JSONResponse:
    return ok(request, await provider.search(keyword=keyword, cursor=cursor, count=count))


@router.get("/video", summary="视频号作品详情（含视频地址）")
async def video(
    request: Request,
    url: str | None = URL_QUERY,
    object_id: str | None = OBJECT_ID_QUERY,
    principal: Principal = Depends(READ),
    provider: ChannelsProvider = Depends(get_channels),
) -> JSONResponse:
    _exactly_one(url=url, object_id=object_id)
    return ok(request, await provider.video(url=url, object_id=object_id))


@router.get("/comments", summary="视频号作品评论（分页）")
async def comments(
    request: Request,
    url: str | None = URL_QUERY,
    object_id: str | None = OBJECT_ID_QUERY,
    cursor: str | None = CURSOR_QUERY,
    count: int = COUNT_QUERY,
    principal: Principal = Depends(READ),
    provider: ChannelsProvider = Depends(get_channels),
) -> JSONResponse:
    _exactly_one(url=url, object_id=object_id)
    return ok(
        request,
        await provider.comments(url=url, object_id=object_id, cursor=cursor, count=count),
    )
