"""Background worker: ``python -m hub.worker``.

Runs publishing tasks. Several processes may run against one database; the
claim query hands each task to exactly one of them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from datetime import timedelta
from typing import Any, Awaitable, Callable, Final, Mapping

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hub.constants import WORKER_TASK_KINDS, TaskKind
from hub.db import create_engine, create_schema, create_session_factory
from hub.errors import ErrorCode, HubError
from hub.http import outbound_client
from hub.log import setup_logging
from hub.models import Task, Tenant
from hub.providers.sau import SauClient
from hub.services.publishing import run_publish
from hub.services.tasks import claim_next, fail_stale, mark_failed, mark_succeeded
from hub.services.webhooks import deliver
from hub.settings import Settings, get_settings

log = logging.getLogger("hub.worker")

Handler = Callable[..., Awaitable[Mapping[str, Any]]]
HANDLERS: Final[Mapping[str, Handler]] = {TaskKind.PUBLISH_VIDEO.value: run_publish}
CRASH_MESSAGE: Final = "unexpected error while running the task"


class Worker:
    def __init__(
        self,
        settings: Settings,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        sau: SauClient,
        http: httpx.AsyncClient,
        handlers: Mapping[str, Handler] = HANDLERS,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._sau = sau
        self._http = http
        self._handlers = handlers

    async def run_once(self) -> bool:
        """Claim and run one task; False when the queue was empty."""
        async with self._session_factory() as session:
            task = await claim_next(session, WORKER_TASK_KINDS)
            if task is None:
                return False
            await self._execute(session, task)
            await self._notify(session, task)
        return True

    async def _execute(self, session: AsyncSession, task: Task) -> None:
        log.info("worker.task_started", extra={"task_id": task.id, "kind": task.kind})
        handler = self._handlers.get(task.kind)
        try:
            if handler is None:
                raise HubError(ErrorCode.INTERNAL, f"no handler for task kind {task.kind}")
            result = await handler(
                task, session=session, sau=self._sau, http=self._http, settings=self._settings
            )
        except HubError as error:
            await self._reset(session, task)
            await mark_failed(session, task, error)
        except Exception:
            log.exception("worker.task_crashed", extra={"task_id": task.id})
            await self._reset(session, task)
            await mark_failed(session, task, HubError(ErrorCode.INTERNAL, CRASH_MESSAGE))
        else:
            await mark_succeeded(session, task, result)
        log.info("worker.task_finished", extra={"task_id": task.id, "status": task.status})

    @staticmethod
    async def _reset(session: AsyncSession, task: Task) -> None:
        """Discard whatever the handler left in the session and reload the task."""
        await session.rollback()
        await session.refresh(task)

    async def _notify(self, session: AsyncSession, task: Task) -> None:
        if not task.callback_url:
            return
        tenant = await session.get(Tenant, task.tenant_id)
        secret = tenant.webhook_secret if tenant is not None else None
        if not secret and self._settings.webhook_secret is not None:
            secret = self._settings.webhook_secret.get_secret_value()
        await session.commit()
        await deliver(
            self._http,
            task.callback_url,
            task,
            secret=secret,
            max_attempts=self._settings.webhook_max_attempts,
            request_timeout=self._settings.webhook_timeout_seconds,
        )

    async def _loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                claimed = await self.run_once()
            except Exception:
                log.exception("worker.loop_error")
                claimed = False
            if not claimed:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self._settings.worker_poll_seconds)

    async def run(self, stop: asyncio.Event) -> None:
        async with self._session_factory() as session:
            failed = await fail_stale(
                session,
                kinds=WORKER_TASK_KINDS,
                older_than=timedelta(seconds=self._settings.worker_stale_after_seconds),
            )
        if failed:
            log.warning("worker.stale_tasks_failed", extra={"count": failed})
        await asyncio.gather(*(self._loop(stop) for _ in range(self._settings.worker_concurrency)))


def _install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signum, stop.set)


async def _main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    if not settings.sau_base_url:
        raise SystemExit("HUB_SAU_BASE_URL is required to run the worker")
    engine = create_engine(settings.database_url)
    if settings.auto_create_schema:
        await create_schema(engine)
    sau = SauClient(
        settings.sau_base_url,
        request_timeout=settings.sau_request_timeout_seconds,
        publish_timeout=settings.sau_publish_timeout_seconds,
        login_timeout=settings.sau_login_timeout_seconds,
        video_dir=settings.sau_video_dir,
    )
    http = outbound_client(settings)
    stop = asyncio.Event()
    _install_signal_handlers(stop)
    worker = Worker(settings, session_factory=create_session_factory(engine), sau=sau, http=http)
    log.info("worker.started", extra={"concurrency": settings.worker_concurrency})
    try:
        await worker.run(stop)
    finally:
        await sau.aclose()
        await http.aclose()
        await engine.dispose()
        log.info("worker.stopped")


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
