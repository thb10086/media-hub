"""Publishing a video through social-auto-upload; runs inside the worker."""

from __future__ import annotations

from pathlib import Path, PurePath
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from hub.constants import SAU_PLATFORM_TYPES, AccountStatus
from hub.errors import ErrorCode, HubError
from hub.models import MediaFile, PlatformAccount, Task
from hub.providers.sau import PostVideoSpec, SauClient
from hub.schemas import PublishVideoRequest
from hub.services.media import ALLOWED_VIDEO_SUFFIXES, download_to, stored_name, tenant_dir
from hub.services.urls import ensure_public_url
from hub.settings import Settings

FALLBACK_SUFFIX = ".mp4"


async def load_media(session: AsyncSession, media_id: str, tenant_id: str) -> MediaFile:
    media = await session.get(MediaFile, media_id)
    if media is None or media.tenant_id != tenant_id:
        raise HubError(ErrorCode.NOT_FOUND, "no such media", details={"media_id": media_id})
    return media


async def load_account(
    session: AsyncSession, request: PublishVideoRequest, tenant_id: str
) -> PlatformAccount:
    account = await session.get(PlatformAccount, request.account_id)
    if account is None or account.tenant_id != tenant_id:
        raise HubError(ErrorCode.NOT_FOUND, "no such account", details={"account_id": request.account_id})
    if account.platform != request.platform.value:
        raise HubError(
            ErrorCode.INVALID_PARAM,
            "the account belongs to a different platform",
            details={"account_platform": account.platform, "platform": request.platform.value},
        )
    if account.status != AccountStatus.ACTIVE.value:
        raise HubError(
            ErrorCode.INVALID_PARAM,
            "the account needs to log in again",
            details={"account_id": account.id, "status": account.status},
        )
    return account


async def _source(
    request: PublishVideoRequest,
    media: MediaFile | None,
    tenant_id: str,
    *,
    http: httpx.AsyncClient,
    settings: Settings,
) -> tuple[Path, bool]:
    """The local video file, and whether it is a temporary download."""
    directory = tenant_dir(settings.media_dir, tenant_id)
    if media is not None:
        path = directory / media.stored_name
        if not path.is_file():
            raise HubError(
                ErrorCode.NOT_FOUND, "the media file is no longer stored", details={"media_id": media.id}
            )
        return path, False
    url = await ensure_public_url(
        str(request.video_url), field="video_url", allow_private=settings.allow_private_urls
    )
    suffix = PurePath(urlsplit(url).path).suffix.lower()
    destination = directory / stored_name(suffix if suffix in ALLOWED_VIDEO_SUFFIXES else FALLBACK_SUFFIX)
    await download_to(
        http,
        url,
        destination,
        max_bytes=settings.max_upload_bytes,
        request_timeout=settings.remote_fetch_timeout_seconds,
    )
    return destination, True


async def run_publish(
    task: Task,
    *,
    session: AsyncSession,
    sau: SauClient,
    http: httpx.AsyncClient,
    settings: Settings,
) -> dict[str, Any]:
    request = PublishVideoRequest.model_validate(task.payload)
    account = await load_account(session, request, task.tenant_id)
    media = (
        await load_media(session, request.media_id, task.tenant_id) if request.media_id is not None else None
    )
    await session.commit()
    source, temporary = await _source(request, media, task.tenant_id, http=http, settings=settings)
    staged: str | None = None
    try:
        staged = await sau.stage_video(source)
        await sau.post_video(
            PostVideoSpec(
                platform_type=SAU_PLATFORM_TYPES[request.platform],
                video_file=staged,
                cookie_file=account.sau_cookie_file,
                title=request.title,
                tags=tuple(request.tags),
                daily_times=tuple(request.schedule.daily_times) if request.schedule else (),
                start_days=request.schedule.start_days if request.schedule else 0,
                draft=request.draft,
            )
        )
    finally:
        if staged is not None:
            await sau.discard_video(staged)
        if temporary:
            source.unlink(missing_ok=True)
    return {
        "platform": request.platform.value,
        "account_id": account.id,
        "scheduled": request.schedule is not None,
        "draft": request.draft,
    }
