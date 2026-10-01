"""Task completion callbacks, signed with HMAC-SHA256.

The body says what happened, not what was fetched; the receiver reads the
result with ``GET /api/v1/tasks/{task_id}``. Verify ``X-Hub-Signature``
against the raw body bytes, not a re-serialised object.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Any, Final, Sequence

import httpx

from hub.constants import HEADER_EVENT, HEADER_SIGNATURE, TaskStatus, WebhookEvent
from hub.models import Task

log = logging.getLogger(__name__)

SIGNATURE_SCHEME: Final = "sha256="
DEFAULT_BACKOFF_SECONDS: Final[tuple[float, ...]] = (2.0, 8.0)
HTTP_TOO_MANY_REQUESTS: Final = 429
HTTP_CLIENT_ERROR: Final = range(400, 500)


def sign(body: bytes, secret: str) -> str:
    return SIGNATURE_SCHEME + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def event_for(task: Task) -> WebhookEvent:
    if task.status == TaskStatus.SUCCEEDED.value:
        return WebhookEvent.TASK_SUCCEEDED
    return WebhookEvent.TASK_FAILED


def notification(task: Task) -> dict[str, Any]:
    body: dict[str, Any] = {
        "event": event_for(task).value,
        "task_id": task.id,
        "kind": task.kind,
        "status": task.status,
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }
    if task.error:
        body["error"] = {"code": task.error.get("code"), "message": task.error.get("message")}
    return body


async def deliver(
    client: httpx.AsyncClient,
    url: str,
    task: Task,
    *,
    secret: str | None,
    max_attempts: int,
    request_timeout: float,
    backoff: Sequence[float] = DEFAULT_BACKOFF_SECONDS,
) -> bool:
    """POST the notification; True once the receiver answered 2xx.

    A failed delivery never changes the task: it is logged and dropped.
    """
    body = json.dumps(notification(task), ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", HEADER_EVENT: event_for(task).value}
    if secret:
        headers[HEADER_SIGNATURE] = sign(body, secret)
    for attempt in range(1, max_attempts + 1):
        try:
            response = await client.post(
                url, content=body, headers=headers, timeout=request_timeout, follow_redirects=False
            )
        except httpx.HTTPError as exc:
            log.warning(
                "webhook.attempt_failed",
                extra={"task_id": task.id, "attempt": attempt, "reason": type(exc).__name__},
            )
        else:
            if response.is_success:
                return True
            log.warning(
                "webhook.rejected",
                extra={"task_id": task.id, "attempt": attempt, "status": response.status_code},
            )
            if response.status_code in HTTP_CLIENT_ERROR and response.status_code != HTTP_TOO_MANY_REQUESTS:
                return False
        if attempt < max_attempts:
            await asyncio.sleep(backoff[min(attempt - 1, len(backoff) - 1)] if backoff else 0)
    return False
