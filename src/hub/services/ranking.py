"""Heat ranking for content lists. Pure functions: no IO, no configuration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final, Mapping, Sequence

SECONDS_PER_HOUR: Final = 3600.0
HEAT_SCORE_FIELD: Final = "heat_score"


@dataclass(frozen=True)
class HeatWeights:
    """Weight of each interaction, plus the half-life of the time decay.

    A share or a save costs the viewer more than a like, so it says more about
    how far a post is travelling; the defaults encode that. ``half_life_hours``
    of 0 disables the decay and ranks purely on interactions.
    """

    like: float = 1.0
    comment: float = 3.0
    share: float = 5.0
    collect: float = 4.0
    play: float = 0.0
    half_life_hours: float = 72.0

    def metric_weights(self) -> tuple[tuple[str, float], ...]:
        return (
            ("digg_count", self.like),
            ("comment_count", self.comment),
            ("share_count", self.share),
            ("collect_count", self.collect),
            ("play_count", self.play),
        )


def parse_timestamp(value: Any) -> datetime | None:
    """An aware UTC datetime from an ISO string, epoch seconds or datetime."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        parsed = datetime.fromtimestamp(value, tz=timezone.utc)
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _count(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value) if value > 0 else 0.0


def heat_score(
    stats: Mapping[str, Any] | None,
    created_at: datetime | None,
    *,
    now: datetime,
    weights: HeatWeights,
) -> float:
    metrics = stats or {}
    raw = sum(weight * _count(metrics.get(name)) for name, weight in weights.metric_weights())
    if weights.half_life_hours <= 0 or created_at is None:
        return round(raw, 4)
    age_hours = max(0.0, (now - created_at).total_seconds() / SECONDS_PER_HOUR)
    return round(raw * 0.5 ** (age_hours / weights.half_life_hours), 4)


def rank_by_heat(
    items: Sequence[Mapping[str, Any]], *, now: datetime, weights: HeatWeights
) -> list[dict[str, Any]]:
    """Items with a ``heat_score`` added, hottest first; ties keep upstream order."""
    scored = [
        {
            **item,
            HEAT_SCORE_FIELD: heat_score(
                item.get("stats"),
                parse_timestamp(item.get("created_at")),
                now=now,
                weights=weights,
            ),
        }
        for item in items
    ]
    return sorted(scored, key=lambda item: item[HEAT_SCORE_FIELD], reverse=True)
