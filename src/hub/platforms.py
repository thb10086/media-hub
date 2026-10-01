"""Platform registry: the one place that says what each platform supports.

Adding a platform takes a ``Platform`` member, an entry in ``PLATFORMS``, and
the provider behind each capability it declares (a data backend and/or a
publishing driver code in ``SAU_PLATFORM_TYPES``). ``GET /api/v1/platforms``
publishes this table together with what is configured right now, so clients
build their UI from it instead of hard-coding platform lists.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Collection, Final, Mapping

from hub.constants import SAU_PLATFORM_TYPES, Platform
from hub.errors import ErrorCode, HubError


class DataCapability(str, Enum):
    SEARCH = "search"
    HOT = "hot"
    PARSE = "parse"
    VIDEO = "video"
    COMMENTS = "comments"
    COMMENT_REPLIES = "comment_replies"
    USER = "user"
    USER_POSTS = "user_posts"
    DOWNLOAD = "download"


class DataBackend(str, Enum):
    DTK = "dtk"
    CHANNELS_PROVIDER = "channels_provider"


@dataclass(frozen=True)
class PlatformSpec:
    display_name: str
    data_backend: DataBackend | None = None
    data_capabilities: tuple[DataCapability, ...] = ()
    supports_draft: bool = False


PLATFORMS: Final[Mapping[Platform, PlatformSpec]] = {
    Platform.DOUYIN: PlatformSpec(
        display_name="抖音",
        data_backend=DataBackend.DTK,
        data_capabilities=tuple(DataCapability),
    ),
    Platform.CHANNELS: PlatformSpec(
        display_name="视频号",
        data_backend=DataBackend.CHANNELS_PROVIDER,
        data_capabilities=(DataCapability.SEARCH, DataCapability.VIDEO, DataCapability.COMMENTS),
        supports_draft=True,
    ),
    Platform.KUAISHOU: PlatformSpec(display_name="快手"),
    Platform.XIAOHONGSHU: PlatformSpec(display_name="小红书"),
}


def ensure_publishing(platform: Platform, enabled: Collection[Platform]) -> None:
    if platform not in enabled:
        raise HubError(
            ErrorCode.INVALID_PARAM,
            "publishing is not enabled for this platform",
            details={"platform": platform.value, "supported": sorted(p.value for p in enabled)},
        )


def supports_draft(platform: Platform) -> bool:
    return PLATFORMS[platform].supports_draft


def catalog(
    *,
    dtk_available: bool,
    channels_available: bool,
    sau_available: bool,
    publish_enabled: Collection[Platform],
) -> list[dict[str, Any]]:
    """Every platform with the capabilities this deployment can serve right now."""
    backends = {DataBackend.DTK: dtk_available, DataBackend.CHANNELS_PROVIDER: channels_available}
    items = []
    for platform, spec in PLATFORMS.items():
        publishable = sau_available and platform in SAU_PLATFORM_TYPES and platform in publish_enabled
        items.append(
            {
                "platform": platform.value,
                "name": spec.display_name,
                "data": {
                    "available": bool(spec.data_backend and backends[spec.data_backend]),
                    "capabilities": [capability.value for capability in spec.data_capabilities],
                },
                "publish": {
                    "available": publishable,
                    "draft": publishable and spec.supports_draft,
                    "schedule": publishable,
                },
            }
        )
    return items
