"""Task polling. Tasks are visible only to the tenant that created them."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from hub.api.deps import get_principal, get_session, settings_of
from hub.envelope import ok
from hub.security.auth import Principal
from hub.services.data import refresh_task
from hub.services.tasks import get_tenant_task, is_terminal, task_view

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])


@router.get("/{task_id}", summary="查询任务状态与结果")
async def get_task(
    request: Request,
    task_id: str = Path(max_length=64),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    task = await get_tenant_task(session, task_id, principal.tenant_id)
    if not is_terminal(task):
        task = await refresh_task(
            session, task, request.app.state.dtk, weights=settings_of(request).heat_weights
        )
    return ok(request, task_view(task))
