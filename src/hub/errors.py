"""The error contract: stable codes, their HTTP status and retry semantics."""

from __future__ import annotations

from enum import Enum
from typing import Any, Final, Mapping


class ErrorCode(str, Enum):
    INVALID_PARAM = "INVALID_PARAM"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN_SCOPE = "FORBIDDEN_SCOPE"
    IP_NOT_ALLOWED = "IP_NOT_ALLOWED"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    CONTENT_UNAVAILABLE = "CONTENT_UNAVAILABLE"
    CONFLICT = "CONFLICT"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    RATE_LIMITED = "RATE_LIMITED"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
    UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"
    INTERNAL = "INTERNAL"


HTTP_STATUS: Final[Mapping[ErrorCode, int]] = {
    ErrorCode.INVALID_PARAM: 400,
    ErrorCode.UNAUTHENTICATED: 401,
    ErrorCode.FORBIDDEN_SCOPE: 403,
    ErrorCode.IP_NOT_ALLOWED: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.METHOD_NOT_ALLOWED: 405,
    ErrorCode.CONTENT_UNAVAILABLE: 404,
    ErrorCode.CONFLICT: 409,
    ErrorCode.PAYLOAD_TOO_LARGE: 413,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.NOT_CONFIGURED: 501,
    ErrorCode.UPSTREAM_ERROR: 502,
    ErrorCode.UPSTREAM_UNAVAILABLE: 503,
    ErrorCode.UPSTREAM_TIMEOUT: 504,
    ErrorCode.INTERNAL: 500,
}

RETRYABLE: Final[frozenset[ErrorCode]] = frozenset(
    {
        ErrorCode.RATE_LIMITED,
        ErrorCode.UPSTREAM_UNAVAILABLE,
        ErrorCode.UPSTREAM_TIMEOUT,
        ErrorCode.INTERNAL,
    }
)


class HubError(Exception):
    """A failure that is rendered to the caller as an error envelope."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details) if details else None
        self.retry_after = retry_after

    @property
    def status_code(self) -> int:
        return HTTP_STATUS[self.code]

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.details:
            body["details"] = self.details
        if self.retry_after is not None:
            body["retry_after"] = self.retry_after
        return body

    @classmethod
    def from_dict(cls, body: Mapping[str, Any]) -> HubError:
        try:
            code = ErrorCode(body.get("code"))
        except ValueError:
            code = ErrorCode.UPSTREAM_ERROR
        return cls(
            code,
            str(body.get("message") or code.value),
            details=body.get("details"),
            retry_after=body.get("retry_after"),
        )
