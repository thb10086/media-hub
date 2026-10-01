from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hub.services.ranking import HeatWeights, heat_score, parse_timestamp, rank_by_heat

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
NO_DECAY = HeatWeights(half_life_hours=0)


def test_heat_score_weights_each_interaction():
    stats = {"digg_count": 10, "comment_count": 2, "share_count": 1, "collect_count": 1, "play_count": 999}
    assert heat_score(stats, None, now=NOW, weights=NO_DECAY) == 10 + 2 * 3 + 5 + 4


def test_heat_score_halves_after_one_half_life():
    weights = HeatWeights(half_life_hours=24)
    fresh = heat_score({"digg_count": 100}, NOW, now=NOW, weights=weights)
    old = heat_score({"digg_count": 100}, NOW - timedelta(hours=24), now=NOW, weights=weights)
    assert fresh == 100
    assert old == 50


def test_heat_score_ignores_missing_negative_and_boolean_values():
    stats = {"digg_count": None, "comment_count": -5, "share_count": True, "collect_count": "7"}
    assert heat_score(stats, None, now=NOW, weights=NO_DECAY) == 0


def test_parse_timestamp_accepts_z_suffix_epoch_and_naive():
    assert parse_timestamp("2026-09-28T12:00:00Z") == NOW
    assert parse_timestamp(NOW.timestamp()) == NOW
    assert parse_timestamp("2026-09-28T12:00:00") == NOW
    assert parse_timestamp("not a date") is None
    assert parse_timestamp(None) is None


def test_rank_by_heat_orders_hottest_first_and_keeps_upstream_order_on_ties():
    items = [
        {"content_id": "a", "stats": {"digg_count": 1}},
        {"content_id": "b", "stats": {"digg_count": 50}},
        {"content_id": "c", "stats": {"digg_count": 1}},
        {"content_id": "d"},
    ]
    ranked = rank_by_heat(items, now=NOW, weights=NO_DECAY)
    assert [item["content_id"] for item in ranked] == ["b", "a", "c", "d"]
    assert ranked[0]["heat_score"] == 50
    assert "heat_score" not in items[0]


def test_rank_by_heat_prefers_recent_posts_with_similar_interactions():
    weights = HeatWeights(half_life_hours=24)
    items = [
        {
            "content_id": "old",
            "stats": {"digg_count": 120},
            "created_at": (NOW - timedelta(days=3)).isoformat(),
        },
        {"content_id": "new", "stats": {"digg_count": 100}, "created_at": NOW.isoformat()},
    ]
    assert [item["content_id"] for item in rank_by_heat(items, now=NOW, weights=weights)] == ["new", "old"]
