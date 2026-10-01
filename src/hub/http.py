"""The HTTP client for outbound calls: webhooks and video downloads."""

from __future__ import annotations

import httpx

from hub.settings import Settings


def outbound_client(
    settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=transport,
        proxy=settings.outbound_proxy if transport is None else None,
        timeout=settings.webhook_timeout_seconds,
        trust_env=False,
    )
