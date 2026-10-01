"""Transparent Redis cache for upstream data responses.

Hot board results change infrequently (minutes), search and video details
moderately (seconds to minutes). Caching them avoids burning identity-pool
quota and reduces perceived latency from 1-20s (upstream round trip) to <5ms.

The cache is *optional*: without Redis nothing is cached and every call goes
through.  A Redis failure (connection down, timeout) fails open—a cache miss
is better than a 500.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Final

from redis.asyncio import Redis
from redis.exceptions import RedisError

log = logging.getLogger(__name__)

PREFIX: Final = "hub:cache:"
# Key patterns per data operation
HOT_KEY: Final = PREFIX + "hot"
SEARCH_KEY: Final = PREFIX + "search:{hash}"
VIDEO_KEY: Final = PREFIX + "video:{id}"
COMMENTS_KEY: Final = PREFIX + "comments:{id}:{cursor}"
USER_KEY: Final = PREFIX + "user:{id}"


def _hash_params(**kwargs: Any) -> str:
    """Stable short hash of the query parameters for cache keying."""
    raw = json.dumps(kwargs, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class ResponseCache:
    """A thin wrapper that caches JSON-serialisable DTK responses in Redis.

    Every ``get`` / ``put`` pair uses the same ``key``; the caller decides
    whether to call the upstream when ``get`` returns ``None``.
    """

    def __init__(self, redis: Redis | None, *, default_ttl: int = 60) -> None:
        self._redis = redis
        self._default_ttl = default_ttl

    @property
    def enabled(self) -> bool:
        return self._redis is not None

    async def get(self, key: str) -> Any | None:
        if self._redis is None:
            return None
        try:
            raw = await self._redis.get(key)
            if raw is not None:
                return json.loads(raw)
        except RedisError:
            log.warning("cache.get_failed", exc_info=True, extra={"key": key})
        except (json.JSONDecodeError, TypeError):
            log.warning("cache.decode_failed", extra={"key": key})
        return None

    async def put(self, key: str, data: Any, *, ttl: int | None = None) -> None:
        if self._redis is None:
            return
        try:
            raw = json.dumps(data, default=str, ensure_ascii=False)
            await self._redis.set(key, raw, ex=ttl or self._default_ttl)
        except (RedisError, TypeError):
            log.warning("cache.put_failed", exc_info=True, extra={"key": key})

    async def delete(self, key: str) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.delete(key)
        except RedisError:
            pass

    # ── Convenience builders for common data operations ──────────────

    @staticmethod
    def hot_key() -> str:
        return HOT_KEY

    @staticmethod
    def search_key(keyword: str, sort_type: int, publish_time: int, cursor: str | None, count: int) -> str:
        h = _hash_params(k=keyword, s=sort_type, p=publish_time, c=cursor or "", n=count)
        return SEARCH_KEY.format(hash=h)

    @staticmethod
    def video_key(*, url: str | None, aweme_id: str | None) -> str:
        identifier = aweme_id or url or ""
        return VIDEO_KEY.format(id=hashlib.sha256(identifier.encode()).hexdigest()[:16])

    @staticmethod
    def comments_key(*, aweme_id: str | None, url: str | None, cursor: str | None) -> str:
        identifier = aweme_id or url or ""
        return COMMENTS_KEY.format(
            id=hashlib.sha256(identifier.encode()).hexdigest()[:16],
            cursor=cursor or "0",
        )

    @staticmethod
    def user_key(*, url: str | None, sec_user_id: str | None) -> str:
        identifier = sec_user_id or url or ""
        return USER_KEY.format(id=hashlib.sha256(identifier.encode()).hexdigest()[:16])
