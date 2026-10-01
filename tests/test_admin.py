from __future__ import annotations

from conftest import ADMIN_TOKEN, DTK_URL, envelope

ADMIN = {"X-Admin-Token": ADMIN_TOKEN}


async def test_admin_token_is_required(client):
    missing = await client.get("/api/v1/admin/tenants")
    wrong = await client.get("/api/v1/admin/tenants", headers={"X-Admin-Token": "nope"})
    assert missing.status_code == wrong.status_code == 401


async def test_tenant_and_scoped_key_lifecycle(client, upstream):
    upstream.get(f"{DTK_URL}/api/v1/douyin/hot").respond(200, json=envelope({"items": []}))

    created = await client.post("/api/v1/admin/tenants", json={"name": "customer-a"}, headers=ADMIN)
    assert created.status_code == 201
    tenant = created.json()["data"]
    assert tenant["webhook_secret"]
    duplicate = await client.post("/api/v1/admin/tenants", json={"name": "customer-a"}, headers=ADMIN)
    assert duplicate.status_code == 409

    issued = await client.post(
        f"/api/v1/admin/tenants/{tenant['tenant_id']}/keys",
        json={"name": "backend", "scopes": ["douyin:read"], "ip_allowlist": ["127.0.0.1/32"]},
        headers=ADMIN,
    )
    assert issued.status_code == 201
    key = issued.json()["data"]
    headers = {"X-API-Key": key["key"]}

    assert (await client.get("/api/v1/douyin/hot", headers=headers)).status_code == 200
    assert (
        await client.get("/api/v1/channels/search", params={"keyword": "x"}, headers=headers)
    ).status_code == 403

    listed = (await client.get(f"/api/v1/admin/tenants/{tenant['tenant_id']}/keys", headers=ADMIN)).json()[
        "data"
    ]["items"]
    assert [item["key_id"] for item in listed] == [key["key_id"]]
    assert "key" not in listed[0]

    revoked = await client.post(f"/api/v1/admin/keys/{key['key_id']}/revoke", headers=ADMIN)
    assert revoked.json()["data"]["status"] == "revoked"
    assert (await client.get("/api/v1/douyin/hot", headers=headers)).status_code == 401


async def test_invalid_cidr_is_rejected(client):
    tenant = (await client.post("/api/v1/admin/tenants", json={"name": "c"}, headers=ADMIN)).json()["data"]
    response = await client.post(
        f"/api/v1/admin/tenants/{tenant['tenant_id']}/keys",
        json={"name": "k", "ip_allowlist": ["not-a-cidr"]},
        headers=ADMIN,
    )
    assert response.status_code == 400


async def test_mock_channels_provider_serves_integration_data(client, tenant):
    response = await client.get(
        "/api/v1/channels/search", params={"keyword": "露营", "count": 2}, headers=tenant.headers
    )
    items = response.json()["data"]["items"]
    assert len(items) == 2
    assert all(item["mock"] is True and item["platform"] == "channels" for item in items)
