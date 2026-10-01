"""Per-key fixed-window rate limiting in Redis."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final

from redis.asyncio import Redis
from redis.exceptions import RedisError

log = logging.getLogger(__name__)

WINDOW_SECONDS: Final = 60
KEY_TEMPLATE: Final = "hub:ratelimit:{key_id}:{window}"


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    limit: int
    remaining: int
    reset_at: int

    def headers(self) -> dict[str, str]:
        return {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(self.remaining),
            "X-RateLimit-Reset": str(self.reset_at),
        }


class RateLimiter:
    """Counts requests per API key per minute.

    Without Redis, or with a limit of 0, nothing is counted. A Redis outage
    fails open: refusing every caller because the counter is down would turn a
    cache failure into a full outage.
    """

    def __init__(self, redis: Redis | None) -> None:
        self._redis = redis

    async def hit(self, key_id: str, limit: int, *, now: float) -> RateDecision | None:
        if self._redis is None or limit <= 0:
            return None
        window = int(now // WINDOW_SECONDS)
        redis_key = KEY_TEMPLATE.format(key_id=key_id, window=window)
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.incr(redis_key)
                pipe.expire(redis_key, WINDOW_SECONDS * 2)
                count, _ = await pipe.execute()
        except RedisError:
            log.warning("ratelimit.redis_unavailable", exc_info=True)
            return None
        return RateDecision(
            allowed=count <= limit,
            limit=limit,
            remaining=max(0, limit - count),
            reset_at=(window + 1) * WINDOW_SECONDS,
        )
