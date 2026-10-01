"""Video files on local disk: tenant uploads and videos fetched from a URL."""

from __future__ import annotations

import uuid
from pathlib import Path, PurePath
from typing import Final

import anyio
import httpx
from fastapi import UploadFile

from hub.errors import ErrorCode, HubError

ALLOWED_VIDEO_SUFFIXES: Final = frozenset({".mp4", ".mov", ".m4v", ".webm"})
CHUNK_BYTES: Final = 1024 * 1024
PARTIAL_SUFFIX: Final = ".part"


def tenant_dir(media_dir: str, tenant_id: str) -> Path:
    path = Path(media_dir) / tenant_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def video_suffix(filename: str | None) -> str:
    suffix = PurePath(filename or "").suffix.lower()
    if suffix not in ALLOWED_VIDEO_SUFFIXES:
        raise HubError(
            ErrorCode.INVALID_PARAM,
            "unsupported video file type",
            details={"allowed": sorted(ALLOWED_VIDEO_SUFFIXES), "got": suffix or None},
        )
    return suffix


def stored_name(suffix: str) -> str:
    return f"{uuid.uuid4().hex}{suffix}"


def _too_large(max_bytes: int) -> HubError:
    return HubError(
        ErrorCode.PAYLOAD_TOO_LARGE, "the video exceeds the size limit", details={"max_bytes": max_bytes}
    )


async def save_upload(upload: UploadFile, destination: Path, *, max_bytes: int) -> int:
    """Copy an upload to disk in chunks; nothing is kept if it is too large."""
    partial = destination.with_name(destination.name + PARTIAL_SUFFIX)
    written = 0
    try:
        async with await anyio.open_file(partial, "wb") as handle:
            while chunk := await upload.read(CHUNK_BYTES):
                written += len(chunk)
                if written > max_bytes:
                    raise _too_large(max_bytes)
                await handle.write(chunk)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    partial.replace(destination)
    return written


async def download_to(
    client: httpx.AsyncClient, url: str, destination: Path, *, max_bytes: int, request_timeout: float
) -> int:
    partial = destination.with_name(destination.name + PARTIAL_SUFFIX)
    written = 0
    try:
        async with client.stream("GET", url, timeout=request_timeout, follow_redirects=True) as response:
            if not response.is_success:
                raise HubError(
                    ErrorCode.INVALID_PARAM,
                    "the video URL could not be fetched",
                    details={"field": "video_url", "status": response.status_code},
                )
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise _too_large(max_bytes)
            async with await anyio.open_file(partial, "wb") as handle:
                async for chunk in response.aiter_bytes(CHUNK_BYTES):
                    written += len(chunk)
                    if written > max_bytes:
                        raise _too_large(max_bytes)
                    await handle.write(chunk)
    except httpx.HTTPError as exc:
        partial.unlink(missing_ok=True)
        raise HubError(
            ErrorCode.INVALID_PARAM,
            "the video URL could not be fetched",
            details={"field": "video_url", "reason": type(exc).__name__},
        ) from exc
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    partial.replace(destination)
    return written
