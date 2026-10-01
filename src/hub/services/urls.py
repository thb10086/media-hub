"""Guard for URLs the server fetches or calls on a caller's behalf (SSRF)."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Final
from urllib.parse import urlsplit

from hub.errors import ErrorCode, HubError

ALLOWED_SCHEMES: Final = frozenset({"http", "https"})
DEFAULT_PORTS: Final = {"http": 80, "https": 443}


def _reject(field: str, reason: str) -> HubError:
    return HubError(
        ErrorCode.INVALID_PARAM,
        f"{field} is not an acceptable URL",
        details={"field": field, "reason": reason},
    )


async def ensure_public_url(url: str, *, field: str, allow_private: bool) -> str:
    """Accept only http(s) URLs whose host resolves to public addresses.

    Resolution happens again when the request is actually made, so this does
    not defend against DNS rebinding; it stops the common mistakes and probes.
    """
    parts = urlsplit(url)
    if parts.scheme not in ALLOWED_SCHEMES:
        raise _reject(field, "scheme_not_allowed")
    if not parts.hostname:
        raise _reject(field, "host_missing")
    if allow_private:
        return url
    port = parts.port or DEFAULT_PORTS[parts.scheme]
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise _reject(field, "host_unresolvable") from exc
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise _reject(field, "private_address")
    return url
