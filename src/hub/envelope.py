"""The response envelope every JSON answer is wrapped in.

``{"success": bool, "data": ..., "error": {...} | null, "meta": {"request_id": ...}}``
Callers branch on ``success`` and ``error.code``, never on the message text.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from hub.errors import HubError

UNKNOWN_REQUEST_ID = "-"

#: The failure envelope as a JSON schema, published in the OpenAPI document.
ERROR_ENVELOPE_SCHEMA: dict[str, Any] = {
    "title": "ErrorEnvelope",
    "type": "object",
    "required": ["success", "data", "error", "meta"],
    "properties": {
        "success": {"type": "boolean", "const": False},
        "data": {"type": "null"},
        "error": {
            "type": "object",
            "required": ["code", "message", "retryable"],
            "properties": {
                "code": {"type": "string", "description": "稳定的错误码枚举，按它分支，不要按 message 分支"},
                "message": {"type": "string"},
                "retryable": {"type": "boolean"},
                "details": {"type": "object"},
                "retry_after": {"type": "number", "description": "建议的重试等待秒数"},
            },
        },
        "meta": {
            "type": "object",
            "required": ["request_id"],
            "properties": {"request_id": {"type": "string"}},
        },
    },
}


def request_id_of(request: Request) -> str:
    return getattr(request.state, "request_id", UNKNOWN_REQUEST_ID)


def ok(
    request: Request,
    data: Any,
    *,
    status_code: int = 200,
    meta: Mapping[str, Any] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "success": True,
            "data": jsonable_encoder(data),
            "error": None,
            "meta": {"request_id": request_id_of(request), **jsonable_encoder(meta or {})},
        },
    )


def fail(request: Request, error: HubError) -> JSONResponse:
    headers = None
    if error.retry_after is not None:
        headers = {"Retry-After": str(max(1, math.ceil(error.retry_after)))}
    return JSONResponse(
        status_code=error.status_code,
        content={
            "success": False,
            "data": None,
            "error": jsonable_encoder(error.to_dict()),
            "meta": {"request_id": request_id_of(request)},
        },
        headers=headers,
    )
