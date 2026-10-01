"""Capability discovery: which platforms this deployment can serve, and how."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from hub.api.deps import get_principal, settings_of
from hub.envelope import ok
from hub.platforms import catalog
from hub.security.auth import Principal
from hub.settings import ChannelsProviderName

router = APIRouter(prefix="/api/v1/platforms", tags=["platforms"])


@router.get("", summary="平台能力目录：各平台当前可用的数据与发布能力")
async def list_platforms(request: Request, principal: Principal = Depends(get_principal)) -> JSONResponse:
    state = request.app.state
    settings = settings_of(request)
    items = catalog(
        dtk_available=state.dtk is not None,
        channels_available=settings.channels_provider is not ChannelsProviderName.NONE,
        sau_available=state.sau is not None,
        publish_enabled=settings.publish_platforms,
    )
    return ok(request, {"items": items})
