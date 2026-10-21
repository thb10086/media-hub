"""Video publishing: accepted synchronously, executed by the worker."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from hub.api.deps import get_sau, get_session, require, settings_of
from hub.constants import HEADER_IDEMPOTENCY_KEY, Scope, TaskKind
from hub.envelope import ok
from hub.platforms import ensure_publishing
from hub.providers.sau import SauClient
from hub.schemas import PublishVideoRequest
from hub.security.auth import Principal
from hub.services.publishing import load_account, load_media
from hub.services.tasks import create_task, task_view
from hub.services.urls import ensure_public_url

router = APIRouter(prefix="/api/v1/publish", tags=["publish"])


@router.post("/videos", summary="发布视频（异步任务，支持幂等键与回调）", status_code=202)
async def publish_video(
    request: Request,
    body: PublishVideoRequest,
    idempotency_key: str | None = Header(default=None, alias=HEADER_IDEMPOTENCY_KEY, max_length=128),
    principal: Principal = Depends(require(Scope.PUBLISH)),
    session: AsyncSession = Depends(get_session),
    sau: SauClient = Depends(get_sau),
) -> JSONResponse:
    settings = settings_of(request)
    ensure_publishing(body.platform, settings.publish_platforms)
    await load_account(session, body, principal.tenant_id)
    if body.media_id is not None:
        await load_media(session, body.media_id, principal.tenant_id)
    for field, url in (
        ("video_url", body.video_url),
        ("cover_url", body.cover_url),
        ("callback_url", body.callback_url),
    ):
        if url is not None:
            await ensure_public_url(url, field=field, allow_private=settings.allow_private_urls)
    task, created = await create_task(
        session,
        tenant_id=principal.tenant_id,
        kind=TaskKind.PUBLISH_VIDEO,
        payload=body.model_dump(mode="json"),
        idempotency_key=idempotency_key,
        callback_url=body.callback_url,
    )
    return ok(request, task_view(task), status_code=202 if created else 200)
