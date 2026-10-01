"""WeChat Channels (视频号) data provider interface.

Channels content is only distributed inside the WeChat client, so search,
other creators' posts and their comments come from a commercial data vendor.
The vendor is not chosen yet: this module fixes the interface and ships an
"unconfigured" provider plus a deterministic mock for client integration.
Results use the same shape as the Douyin endpoints (``items``/``cursor``/
``has_more`` pages of content items).
"""

from __future__ import annotations

from typing import Any, Final, Protocol

from hub.constants import Platform
from hub.errors import ErrorCode, HubError
from hub.settings import ChannelsProviderName

MOCK_PAGE_SIZE: Final = 3


class ChannelsProvider(Protocol):
    async def search(self, *, keyword: str, cursor: str | None, count: int) -> dict[str, Any]: ...

    async def video(self, *, url: str | None, object_id: str | None) -> dict[str, Any]: ...

    async def comments(
        self, *, url: str | None, object_id: str | None, cursor: str | None, count: int
    ) -> dict[str, Any]: ...

    async def aclose(self) -> None: ...


class UnconfiguredChannelsProvider:
    @staticmethod
    def _refuse() -> HubError:
        return HubError(
            ErrorCode.NOT_CONFIGURED,
            "no WeChat Channels data provider is configured (HUB_CHANNELS_PROVIDER)",
        )

    async def search(self, *, keyword: str, cursor: str | None, count: int) -> dict[str, Any]:
        raise self._refuse()

    async def video(self, *, url: str | None, object_id: str | None) -> dict[str, Any]:
        raise self._refuse()

    async def comments(
        self, *, url: str | None, object_id: str | None, cursor: str | None, count: int
    ) -> dict[str, Any]:
        raise self._refuse()

    async def aclose(self) -> None:
        return None


class MockChannelsProvider:
    """Deterministic sample data, flagged ``mock: true``, for integration tests."""

    @staticmethod
    def _item(index: int, seed: str) -> dict[str, Any]:
        object_id = f"mock-{seed}-{index}"
        return {
            "platform": Platform.CHANNELS.value,
            "content_id": object_id,
            "title": f"[mock] {seed} #{index}",
            "web_url": f"https://channels.weixin.qq.com/mock/{object_id}",
            "author": {"uid": f"mock-author-{index}", "nickname": f"mock author {index}"},
            "stats": {"digg_count": 100 * index, "comment_count": 10 * index},
            "media": {"video": {"url": f"https://example.invalid/{object_id}.mp4"}},
            "mock": True,
        }

    async def search(self, *, keyword: str, cursor: str | None, count: int) -> dict[str, Any]:
        size = min(count, MOCK_PAGE_SIZE)
        return {
            "items": [self._item(index, keyword) for index in range(1, size + 1)],
            "cursor": None,
            "has_more": False,
        }

    async def video(self, *, url: str | None, object_id: str | None) -> dict[str, Any]:
        return self._item(1, object_id or "url")

    async def comments(
        self, *, url: str | None, object_id: str | None, cursor: str | None, count: int
    ) -> dict[str, Any]:
        content_id = object_id or "url"
        return {
            "items": [
                {
                    "platform": Platform.CHANNELS.value,
                    "comment_id": f"mock-comment-{index}",
                    "content_id": content_id,
                    "text": f"[mock] comment {index}",
                    "digg_count": index,
                    "mock": True,
                }
                for index in range(1, min(count, MOCK_PAGE_SIZE) + 1)
            ],
            "cursor": None,
            "has_more": False,
        }

    async def aclose(self) -> None:
        return None


def build_channels_provider(name: ChannelsProviderName) -> ChannelsProvider:
    if name is ChannelsProviderName.MOCK:
        return MockChannelsProvider()
    return UnconfiguredChannelsProvider()
