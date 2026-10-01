from __future__ import annotations

import json

import httpx

from conftest import SAU_URL

QR_CODE = "data:image/png;base64,iVBORw0KGgo="


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((fields["event"], json.loads(fields["data"])))
    return events


def mock_login(upstream, outcome: str):
    login_route = upstream.get(f"{SAU_URL}/login").respond(
        200,
        content=f"data: {QR_CODE}\n\ndata: {outcome}\n\n".encode(),
        headers={"Content-Type": "text/event-stream"},
    )

    def accounts(request: httpx.Request) -> httpx.Response:
        user_name = login_route.calls.last.request.url.params["id"]
        rows = [[1, 3, "other.json", "someone", 1], [7, 3, "new.json", user_name, 1]]
        return httpx.Response(200, json={"code": 200, "msg": None, "data": rows})

    upstream.get(f"{SAU_URL}/getAccounts").mock(side_effect=accounts)
    return login_route


async def test_qr_login_streams_code_then_registers_account(client, tenant, upstream):
    login_route = mock_login(upstream, "200")
    response = await client.get(
        "/api/v1/accounts/login", params={"platform": "douyin", "name": "主账号"}, headers=tenant.headers
    )
    assert response.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(response.text)
    assert events[0] == ("qrcode", {"qrcode": QR_CODE})
    assert events[1][0] == "success"
    account = events[1][1]["account"]
    assert (account["platform"], account["name"], account["status"]) == ("douyin", "主账号", "active")
    assert login_route.calls.last.request.url.params["type"] == "3"

    listed = (await client.get("/api/v1/accounts", headers=tenant.headers)).json()["data"]["items"]
    assert [item["account_id"] for item in listed] == [account["account_id"]]


async def test_failed_login_registers_nothing(client, tenant, upstream):
    mock_login(upstream, "500")
    response = await client.get(
        "/api/v1/accounts/login", params={"platform": "channels", "name": "x"}, headers=tenant.headers
    )
    events = parse_sse(response.text)
    assert [name for name, _ in events] == ["qrcode", "failed"]
    listed = (await client.get("/api/v1/accounts", headers=tenant.headers)).json()["data"]["items"]
    assert listed == []


async def test_login_rejects_platforms_without_publishing(client, tenant):
    response = await client.get(
        "/api/v1/accounts/login", params={"platform": "kuaishou", "name": "x"}, headers=tenant.headers
    )
    assert response.status_code == 400


async def test_delete_account_removes_it_upstream(client, tenant, upstream):
    mock_login(upstream, "200")
    delete_route = upstream.get(f"{SAU_URL}/deleteAccount").respond(
        200, json={"code": 200, "msg": "ok", "data": None}
    )
    login = await client.get(
        "/api/v1/accounts/login", params={"platform": "douyin", "name": "x"}, headers=tenant.headers
    )
    account_id = parse_sse(login.text)[1][1]["account"]["account_id"]

    deleted = await client.delete(f"/api/v1/accounts/{account_id}", headers=tenant.headers)
    assert deleted.status_code == 200
    assert delete_route.calls.last.request.url.params["id"] == "7"
    assert (await client.get("/api/v1/accounts", headers=tenant.headers)).json()["data"]["items"] == []
