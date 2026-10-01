"""Probes for the load balancer and the container runtime. No envelope, no auth."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

router = APIRouter(tags=["system"])
log = logging.getLogger(__name__)


@router.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", include_in_schema=False)
async def readyz(request: Request) -> JSONResponse:
    state = request.app.state
    failed: list[str] = []
    try:
        async with state.session_factory() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        log.warning("readyz.database_failed", exc_info=True)
        failed.append("database")
    if state.redis is not None:
        try:
            await state.redis.ping()
        except Exception:
            log.warning("readyz.redis_failed", exc_info=True)
            failed.append("redis")
    if failed:
        return JSONResponse(status_code=503, content={"status": "not_ready", "failed": failed})
    return JSONResponse(content={"status": "ready"})
