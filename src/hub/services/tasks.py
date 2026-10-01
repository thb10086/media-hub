"""Task lifecycle: creation with idempotency, claiming, completion.

Tasks are rows in the database, which makes PostgreSQL the queue: workers
claim with ``SELECT ... FOR UPDATE SKIP LOCKED``, so several worker processes
can run side by side without handing the same job to two of them.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from hub.constants import TERMINAL_TASK_STATUSES, TaskKind, TaskStatus
from hub.errors import ErrorCode, HubError
from hub.models import Task, as_utc, utcnow

INTERRUPTED_MESSAGE = "the worker stopped while this task was running; it was not retried"


def request_hash(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _by_idempotency_key(session: AsyncSession, tenant_id: str, key: str) -> Task | None:
    return await session.scalar(select(Task).where(Task.tenant_id == tenant_id, Task.idempotency_key == key))


def _reuse(existing: Task, digest: str) -> Task:
    if existing.request_hash != digest:
        raise HubError(
            ErrorCode.CONFLICT,
            "this Idempotency-Key was already used with a different request",
            details={"task_id": existing.id},
        )
    return existing


async def create_task(
    session: AsyncSession,
    *,
    tenant_id: str,
    kind: TaskKind,
    payload: Mapping[str, Any],
    status: TaskStatus = TaskStatus.QUEUED,
    idempotency_key: str | None = None,
    callback_url: str | None = None,
) -> tuple[Task, bool]:
    """The task, and whether it was created by this call."""
    digest = request_hash(payload)
    if idempotency_key:
        existing = await _by_idempotency_key(session, tenant_id, idempotency_key)
        if existing is not None:
            return _reuse(existing, digest), False
    now = utcnow()
    task = Task(
        tenant_id=tenant_id,
        kind=kind.value,
        status=status.value,
        payload=dict(payload),
        idempotency_key=idempotency_key,
        request_hash=digest,
        callback_url=callback_url,
        run_after=now,
        created_at=now,
        started_at=now if status is TaskStatus.RUNNING else None,
    )
    session.add(task)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await _by_idempotency_key(session, tenant_id, idempotency_key) if idempotency_key else None
        if existing is None:
            raise
        return _reuse(existing, digest), False
    return task, True


async def get_tenant_task(session: AsyncSession, task_id: str, tenant_id: str) -> Task:
    task = await session.get(Task, task_id)
    if task is None or task.tenant_id != tenant_id:
        raise HubError(ErrorCode.NOT_FOUND, "no such task", details={"task_id": task_id})
    return task


async def claim_next(session: AsyncSession, kinds: Sequence[str]) -> Task | None:
    now = utcnow()
    statement = (
        select(Task)
        .where(
            Task.status == TaskStatus.QUEUED.value,
            Task.kind.in_(kinds),
            Task.run_after <= now,
        )
        .order_by(Task.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    task = await session.scalar(statement)
    if task is None:
        await session.rollback()
        return None
    task.status = TaskStatus.RUNNING.value
    task.started_at = now
    task.attempts += 1
    await session.commit()
    return task


async def mark_succeeded(session: AsyncSession, task: Task, result: Mapping[str, Any] | None) -> None:
    task.status = TaskStatus.SUCCEEDED.value
    task.result = dict(result) if result is not None else None
    task.error = None
    task.finished_at = utcnow()
    await session.commit()


async def mark_failed(session: AsyncSession, task: Task, error: HubError) -> None:
    task.status = TaskStatus.FAILED.value
    task.error = error.to_dict()
    task.finished_at = utcnow()
    await session.commit()


async def fail_stale(session: AsyncSession, *, kinds: Sequence[str], older_than: timedelta) -> int:
    """Fail tasks a crashed worker left running.

    Publishing is not idempotent on the platform side - a retry after a crash
    mid-upload can post the video twice - so stale tasks fail instead of
    being re-queued. The caller decides whether to submit again.
    """
    cutoff = utcnow() - older_than
    stale = (
        await session.scalars(
            select(Task).where(
                Task.status == TaskStatus.RUNNING.value,
                Task.kind.in_(kinds),
                Task.started_at < cutoff,
            )
        )
    ).all()
    error = HubError(ErrorCode.INTERNAL, INTERRUPTED_MESSAGE)
    for task in stale:
        task.status = TaskStatus.FAILED.value
        task.error = error.to_dict()
        task.finished_at = utcnow()
    await session.commit()
    return len(stale)


def is_terminal(task: Task) -> bool:
    return task.status in TERMINAL_TASK_STATUSES


def _iso(value: datetime | None) -> str | None:
    moment = as_utc(value)
    return moment.isoformat() if moment else None


def task_view(task: Task) -> dict[str, Any]:
    return {
        "task_id": task.id,
        "kind": task.kind,
        "status": task.status,
        "result": task.result,
        "error": task.error,
        "created_at": _iso(task.created_at),
        "started_at": _iso(task.started_at),
        "finished_at": _iso(task.finished_at),
    }
