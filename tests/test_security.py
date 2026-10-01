from __future__ import annotations

from conftest import make_tenant
from hub.constants import Scope
from hub.security.keys import generate_key, hash_key, key_prefix, verify_key
from hub.security.net import client_ip, parse_networks

TRUSTED = parse_networks(["10.0.0.0/8"])


def test_generated_key_round_trips():
    generated = generate_key()
    assert generated.key.startswith("mh_")
    assert key_prefix(generated.key) == generated.prefix
    assert verify_key(generated.key, generated.key_hash)
    assert not verify_key(generated.key + "x", generated.key_hash)
    assert generated.key_hash == hash_key(generated.key)


def test_malformed_keys_have_no_prefix():
    for candidate in ("", "mh", "mh_abc_def", "xx_0123456789ab_secret", "mh_0123456789ab_"):
        assert key_prefix(candidate) is None


def test_forwarded_for_is_ignored_from_untrusted_peers():
    assert client_ip("203.0.113.9", "1.2.3.4", TRUSTED) == "203.0.113.9"


def test_forwarded_for_takes_rightmost_untrusted_hop():
    assert client_ip("10.0.0.2", "6.6.6.6, 198.51.100.7, 10.0.0.3", TRUSTED) == "198.51.100.7"


def test_forwarded_for_all_trusted_falls_back_to_first_hop():
    assert client_ip("10.0.0.2", "10.0.0.5, 10.0.0.3", TRUSTED) == "10.0.0.5"


async def test_missing_and_invalid_keys_are_rejected(client):
    missing = await client.get("/api/v1/channels/search", params={"keyword": "x"})
    invalid = await client.get(
        "/api/v1/channels/search",
        params={"keyword": "x"},
        headers={"Authorization": "Bearer mh_0123456789ab_nope"},
    )
    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert invalid.json()["error"]["code"] == "UNAUTHENTICATED"


async def test_bearer_header_is_accepted(app, client):
    tenant = await make_tenant(app)
    response = await client.get(
        "/api/v1/channels/search",
        params={"keyword": "x"},
        headers={"Authorization": f"Bearer {tenant.key}"},
    )
    assert response.status_code == 200


async def test_scope_is_enforced(app, client):
    tenant = await make_tenant(app, scopes=[Scope.PUBLISH])
    response = await client.get("/api/v1/channels/search", params={"keyword": "x"}, headers=tenant.headers)
    assert response.status_code == 403
    assert response.json()["error"]["details"]["required"] == ["channels:read"]


async def test_ip_allowlist_is_enforced(app, client):
    blocked = await make_tenant(app, name="blocked", ip_allowlist=["10.0.0.0/8"])
    allowed = await make_tenant(app, name="allowed", ip_allowlist=["127.0.0.1/32"])
    params = {"keyword": "x"}
    denied = await client.get("/api/v1/channels/search", params=params, headers=blocked.headers)
    granted = await client.get("/api/v1/channels/search", params=params, headers=allowed.headers)
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "IP_NOT_ALLOWED"
    assert granted.status_code == 200


async def test_rate_limit_answers_429_with_headers(app, client):
    tenant = await make_tenant(app, rate_limit=2)
    params = {"keyword": "x"}
    first = await client.get("/api/v1/channels/search", params=params, headers=tenant.headers)
    await client.get("/api/v1/channels/search", params=params, headers=tenant.headers)
    third = await client.get("/api/v1/channels/search", params=params, headers=tenant.headers)
    assert first.headers["X-RateLimit-Limit"] == "2"
    assert first.headers["X-RateLimit-Remaining"] == "1"
    assert third.status_code == 429
    assert third.json()["error"]["code"] == "RATE_LIMITED"
    assert int(third.headers["Retry-After"]) >= 1


async def test_request_id_is_echoed_or_generated(client):
    echoed = await client.get("/healthz", headers={"X-Request-ID": "trace-123"})
    generated = await client.get("/healthz", headers={"X-Request-ID": "bad id with spaces"})
    assert echoed.headers["X-Request-ID"] == "trace-123"
    assert generated.headers["X-Request-ID"] != "bad id with spaces"
