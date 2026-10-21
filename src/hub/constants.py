"""Enumerations and protocol constants shared by every layer.

Values here are part of the public API contract (scopes, task states, sort
names) or of an upstream contract (social-auto-upload platform codes), so they
live in one place instead of being spelled out at each call site.
"""

from __future__ import annotations

from enum import Enum
from typing import Final


class Platform(str, Enum):
    DOUYIN = "douyin"
    CHANNELS = "channels"
    KUAISHOU = "kuaishou"
    XIAOHONGSHU = "xiaohongshu"


class Scope(str, Enum):
    DOUYIN_READ = "douyin:read"
    MEDIA_DOWNLOAD = "media:download"
    PUBLISH = "publish:write"
    ACCOUNTS = "accounts:manage"
    CHANNELS_READ = "channels:read"


ALL_SCOPES: Final[tuple[str, ...]] = tuple(scope.value for scope in Scope)


class TaskKind(str, Enum):
    DATA_FETCH = "data_fetch"
    MEDIA_DOWNLOAD = "media_download"
    PUBLISH_VIDEO = "publish_video"


#: Kinds executed by the background worker. The other kinds are resolved lazily
#: against the data service when a caller reads them.
WORKER_TASK_KINDS: Final[tuple[str, ...]] = (TaskKind.PUBLISH_VIDEO.value,)


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_TASK_STATUSES: Final[frozenset[str]] = frozenset(
    {TaskStatus.SUCCEEDED.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value}
)


class DataOperation(str, Enum):
    PARSE = "douyin.parse"
    VIDEO = "douyin.video"
    COMMENTS = "douyin.comments"
    COMMENT_REPLIES = "douyin.comment_replies"
    USER = "douyin.user"
    USER_POSTS = "douyin.user_posts"
    SEARCH = "douyin.search"
    HOT = "douyin.hot"


class SearchSort(str, Enum):
    GENERAL = "general"
    MOST_LIKED = "most_liked"
    LATEST = "latest"


#: Douyin's own numeric ``sort_type`` values.
DOUYIN_SORT_VALUES: Final[dict[SearchSort, int]] = {
    SearchSort.GENERAL: 0,
    SearchSort.MOST_LIKED: 1,
    SearchSort.LATEST: 2,
}


class PublishTime(int, Enum):
    """Douyin's ``publish_time`` filter, in days."""

    UNLIMITED = 0
    ONE_DAY = 1
    ONE_WEEK = 7
    HALF_YEAR = 180


class RankMode(str, Enum):
    UPSTREAM = "upstream"
    HEAT = "heat"


class TenantStatus(str, Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class KeyStatus(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"


class AccountStatus(str, Enum):
    ACTIVE = "active"
    INVALID = "invalid"


class DataIdentityStatus(str, Enum):
    """Whether a Douyin account's login also stocks DTK's identity pool.

    ``pending`` (not imported yet) -> ``active`` (DTK may sign data requests with
    it) or ``failed`` (retried by the worker). ``retiring`` outlives its account:
    the account is gone but DTK has not confirmed the identity is retired yet.
    """

    PENDING = "pending"
    ACTIVE = "active"
    FAILED = "failed"
    RETIRING = "retiring"


#: The User-Agent SAU's browser logs in with in the container (SAU_USER_AGENT).
#: DTK chooses its TLS profile from it, so the imported jar and the browser that
#: produced it must agree; change both together with SAU's Playwright version.
DEFAULT_DOUYIN_USER_AGENT: Final = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)


class WebhookEvent(str, Enum):
    TASK_SUCCEEDED = "task.succeeded"
    TASK_FAILED = "task.failed"


class LoginSessionStatus(str, Enum):
    """A polled QR login: ``pending`` -> ``qrcode_ready`` -> ``succeeded`` / ``failed`` / ``expired``.

    ``verification_required`` sits between the scan and success when the platform
    asks the account owner to prove it is them (an SMS code), which is answered
    through ``POST /accounts/login-sessions/{id}/verification``.
    """

    PENDING = "pending"
    QRCODE_READY = "qrcode_ready"
    VERIFICATION_REQUIRED = "verification_required"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    EXPIRED = "expired"


TERMINAL_LOGIN_STATUSES: Final[frozenset[str]] = frozenset(
    {LoginSessionStatus.SUCCEEDED.value, LoginSessionStatus.FAILED.value, LoginSessionStatus.EXPIRED.value}
)


#: social-auto-upload identifies platforms by number (see its /login handler).
SAU_PLATFORM_TYPES: Final[dict[Platform, int]] = {
    Platform.XIAOHONGSHU: 1,
    Platform.CHANNELS: 2,
    Platform.DOUYIN: 3,
    Platform.KUAISHOU: 4,
}

API_KEY_PREFIX: Final = "mh"
HEADER_API_KEY: Final = "X-API-Key"
HEADER_ADMIN_TOKEN: Final = "X-Admin-Token"
HEADER_REQUEST_ID: Final = "X-Request-ID"
HEADER_IDEMPOTENCY_KEY: Final = "Idempotency-Key"
HEADER_SIGNATURE: Final = "X-Hub-Signature"
HEADER_EVENT: Final = "X-Hub-Event"
