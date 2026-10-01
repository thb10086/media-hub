"""Turning data-service outcomes into gateway answers and tasks.

A result that arrives within the wait window is returned directly. One that
does not becomes a ``data_fetch`` task owned by the tenant, so the upstream
task id never leaks and one tenant cannot read another's results. The task is
resolved lazily, when the caller polls it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from hub.constants import DataOperation, RankMode, TaskKind, TaskStatus
from hub.errors import ErrorCode, HubError
from hub.models import Task, utcnow
from hub.providers.dtk import Done, DtkClient, Outcome
from hub.services.ranking import HeatWeights, rank_by_heat
from hub.services.tasks import create_task, mark_failed, mark_succeeded, task_view

HTTP_OK: Final = 200
HTTP_ACCEPTED: Final = 202
OPTION_RANK: Final = "rank"
PAYLOAD_UPSTREAM_TASK: Final = "upstream_task_id"
PAYLOAD_OPERATION: Final = "operation"
PAYLOAD_OPTIONS: Final = "options"
PAYLOAD_DOWNLOAD_ID: Final = "download_id"
ITEMS_FIELD: Final = "items"
VALUE_FIELD: Final = "value"
DOWNLOAD_SUCCEEDED_STATES: Final = frozenset({"done", "partial"})
DOWNLOAD_FAILED_STATES: Final = frozenset({"failed", "cancelled"})
FILE_FIELDS: Final[tuple[str, ...]] = ("name", "kind", "state", "bytes", "content_type")


@dataclass(frozen=True)
class DataResult:
    status_code: int
    data: Any
    meta: Mapping[str, Any] = field(default_factory=dict)


def postprocess(operation: str, data: Any, options: Mapping[str, Any], *, weights: HeatWeights) -> Any:
    if (
        operation == DataOperation.SEARCH.value
        and options.get(OPTION_RANK) == RankMode.HEAT.value
        and isinstance(data, dict)
    ):
        items = data.get(ITEMS_FIELD) or []
        return {**data, ITEMS_FIELD: rank_by_heat(items, now=utcnow(), weights=weights)}
    return data


def _as_mapping(data: Any) -> dict[str, Any]:
    return data if isinstance(data, dict) else {VALUE_FIELD: data}


async def settle(
    outcome: Outcome,
    *,
    session: AsyncSession,
    tenant_id: str,
    operation: DataOperation,
    options: Mapping[str, Any],
    weights: HeatWeights,
) -> DataResult:
    if isinstance(outcome, Done):
        data = postprocess(operation.value, outcome.data, options, weights=weights)
        return DataResult(HTTP_OK, data, outcome.public_meta())
    task, _ = await create_task(
        session,
        tenant_id=tenant_id,
        kind=TaskKind.DATA_FETCH,
        status=TaskStatus.RUNNING,
        payload={
            PAYLOAD_UPSTREAM_TASK: outcome.task_id,
            PAYLOAD_OPERATION: operation.value,
            PAYLOAD_OPTIONS: dict(options),
        },
    )
    return DataResult(HTTP_ACCEPTED, task_view(task))


async def refresh_data_task(
    session: AsyncSession, task: Task, dtk: DtkClient, *, weights: HeatWeights
) -> Task:
    state = await dtk.task(str(task.payload[PAYLOAD_UPSTREAM_TASK]))
    if not state.finished:
        return task
    if state.error is not None:
        await mark_failed(session, task, state.error)
        return task
    data = postprocess(
        str(task.payload.get(PAYLOAD_OPERATION)),
        state.data,
        task.payload.get(PAYLOAD_OPTIONS) or {},
        weights=weights,
    )
    await mark_succeeded(session, task, _as_mapping(data))
    return task


def download_view(info: Mapping[str, Any]) -> dict[str, Any]:
    files = [
        {name: item.get(name) for name in FILE_FIELDS}
        for item in info.get("files") or []
        if isinstance(item, dict) and item.get("name")
    ]
    return {"state": info.get("state"), "bytes_total": info.get("bytes_total"), "files": files}


async def start_download(
    session: AsyncSession,
    dtk: DtkClient,
    *,
    tenant_id: str,
    url: str | None,
    aweme_id: str | None,
) -> Task:
    started = await dtk.start_download(url=url, aweme_id=aweme_id)
    download_id = started.get("download_id")
    if not download_id:
        raise HubError(ErrorCode.UPSTREAM_ERROR, "the data service did not return a download id")
    task, _ = await create_task(
        session,
        tenant_id=tenant_id,
        kind=TaskKind.MEDIA_DOWNLOAD,
        status=TaskStatus.RUNNING,
        payload={PAYLOAD_DOWNLOAD_ID: str(download_id), "url": url, "aweme_id": aweme_id},
    )
    return await refresh_download_task(session, task, dtk)


async def refresh_download_task(session: AsyncSession, task: Task, dtk: DtkClient) -> Task:
    info = await dtk.download(str(task.payload[PAYLOAD_DOWNLOAD_ID]))
    state = info.get("state")
    if state in DOWNLOAD_SUCCEEDED_STATES:
        await mark_succeeded(session, task, download_view(info))
    elif state in DOWNLOAD_FAILED_STATES:
        await mark_failed(
            session,
            task,
            HubError(
                ErrorCode.UPSTREAM_ERROR,
                str(info.get("error") or "the media download failed"),
                details={"upstream_state": state},
            ),
        )
    return task


async def refresh_task(
    session: AsyncSession, task: Task, dtk: DtkClient | None, *, weights: HeatWeights
) -> Task:
    """Bring a lazily resolved task up to date; worker tasks are returned as-is."""
    if task.kind not in (TaskKind.DATA_FETCH.value, TaskKind.MEDIA_DOWNLOAD.value):
        return task
    if dtk is None:
        raise HubError(ErrorCode.NOT_CONFIGURED, "the Douyin data service is not configured")
    if task.kind == TaskKind.DATA_FETCH.value:
        return await refresh_data_task(session, task, dtk, weights=weights)
    return await refresh_download_task(session, task, dtk)
