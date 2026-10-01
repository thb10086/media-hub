"""Video uploads, referenced later by ``media_id`` when publishing."""

from __future__ import annotations

from typing import Any, Final

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from hub.api.deps import get_session, require, settings_of
from hub.constants import Scope
from hub.envelope import ok
from hub.models import MediaFile, as_utc
from hub.security.auth import Principal
from hub.services.media import save_upload, stored_name, tenant_dir, video_suffix

MEDIA_PATH: Final = "/api/v1/media"
MAX_NAME_LENGTH: Final = 255

router = APIRouter(prefix=MEDIA_PATH, tags=["media"])


def media_view(media: MediaFile) -> dict[str, Any]:
    created = as_utc(media.created_at)
    return {
        "media_id": media.id,
        "filename": media.original_name,
        "size_bytes": media.size_bytes,
        "content_type": media.content_type,
        "created_at": created.isoformat() if created else None,
    }


@router.post("", summary="上传待发布的视频素材", status_code=201)
async def upload_media(
    request: Request,
    file: UploadFile = File(...),
    principal: Principal = Depends(require(Scope.PUBLISH)),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    settings = settings_of(request)
    name = stored_name(video_suffix(file.filename))
    destination = tenant_dir(settings.media_dir, principal.tenant_id) / name
    size = await save_upload(file, destination, max_bytes=settings.max_upload_bytes)
    media = MediaFile(
        tenant_id=principal.tenant_id,
        original_name=(file.filename or name)[:MAX_NAME_LENGTH],
        stored_name=name,
        size_bytes=size,
        content_type=file.content_type,
    )
    session.add(media)
    await session.commit()
    return ok(request, media_view(media), status_code=201)
