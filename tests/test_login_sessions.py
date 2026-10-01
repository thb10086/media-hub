from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import AsyncIterator

import httpx

from conftest import SAU_URL, make_tenant
from hub.constants import LoginSessionStatus
from hub.main import create_app
from hub.models import utcnow
from hub.services.login_sessions import LoginSession, LoginSessionStore

QR_CODE = "data:image/png;base64,iVBORw0KGgo="
SESSIONS = "/api/v1/accounts/login-sessions"
TERMINAL = {LoginSessionStatus.SUCCEEDED.value, LoginSessionStatus.FAILED.value}


class GatedStream(httpx.AsyncByteStream):
    """An SSE body that sends the QR code, then holds the outcome until the gate opens."""

    def __init__(self, outcome: str, gate: asyncio.Event) -> None:
        self._outcome = outcome
        self._gate = gate

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield f"data: {QR_CODE}\n\n".encode()
        await self._gate.wait()
        yield f"data: {self._outcome}\n\n".encode()


def mock_gated_login(upstream, gate: asyncio.Event, outcome: str):
    login_route = upstream.get(f"{SAU_URL}/login").mock(
        side_effect=lambda request: httpx.Response(
            200, headers={"Content-Type": "text/event-stream"}, stream=GatedStream(outcome, gate)
        )
    )

    def accounts(request: httpx.Request) -> httpx.Response:
        user_name = login_route.calls.last.request.url.params["id"]
        return httpx.Response(
            200, json={"code": 200, "msg": None, "data": [[7, 3, "new.json", user_name, 1]]}
        )

    upstream.get(f"{SAU_URL}/getAccounts").mock(side_effect=accounts)
    return login_route


async def poll(client, headers, session_id: str, until: set[str]) -> dict:
    for _ in range(300):
        body = (await client.get(f"{SESSIONS}/{session_id}", headers=headers)).json()
        if body["data"]["status"] in until:
            return body["data"]
        await asyncio.sleep(0.01)
    raise AssertionError(f"session {session_id} never reached {until}")


async def test_a_session_shows_the_qr_code_then_the_bound_account(client, tenant, upstream):
    gate = asyncio.Event()
    login_route = mock_gated_login(upstream, gate, "200")

    created = await client.post(
        SESSIONS, json={"platform": "douyin", "name": "主账号"}, headers=tenant.headers
    )

    assert created.status_code == 202
    session = created.json()["data"]
    assert (session["status"], session["qrcode"], session["account"]) == ("pending", None, None)

    ready = await poll(client, tenant.headers, session["session_id"], {LoginSessionStatus.QRCODE_READY.value})
    assert ready["qrcode"] == QR_CODE

    gate.set()
    done = await poll(client, tenant.headers, session["session_id"], TERMINAL)
    assert done["status"] == LoginSessionStatus.SUCCEEDED.value
    assert (done["account"]["platform"], done["account"]["name"]) == ("douyin", "主账号")
    assert login_route.calls.last.request.url.params["type"] == "3"

    listed = (await client.get("/api/v1/accounts", headers=tenant.headers)).json()["data"]["items"]
    assert [item["account_id"] for item in listed] == [done["account"]["account_id"]]


async def test_a_failed_login_is_reported_with_its_error(client, tenant, upstream):
    gate = asyncio.Event()
    gate.set()
    mock_gated_login(upstream, gate, "500")

    created = await client.post(SESSIONS, json={"platform": "channels", "name": "x"}, headers=tenant.headers)
    done = await poll(client, tenant.headers, created.json()["data"]["session_id"], TERMINAL)

    assert done["status"] == LoginSessionStatus.FAILED.value
    assert done["error"]["code"] == "UPSTREAM_ERROR"
    assert done["account"] is None


async def test_a_session_is_visible_only_to_its_tenant(app, client, tenant, upstream):
    gate = asyncio.Event()
    gate.set()
    mock_gated_login(upstream, gate, "200")
    created = await client.post(SESSIONS, json={"platform": "douyin", "name": "x"}, headers=tenant.headers)
    other = await make_tenant(app, name="other")

    response = await client.get(f"{SESSIONS}/{created.json()['data']['session_id']}", headers=other.headers)

    assert response.status_code == 404


async def test_an_abandoned_session_reads_as_expired(app, client, tenant):
    started = utcnow() - timedelta(minutes=10)
    session = LoginSession(
        session_id="abandoned",
        tenant_id=tenant.tenant_id,
        platform="douyin",
        name="x",
        status=LoginSessionStatus.PENDING.value,
        created_at=started,
        expires_at=started + timedelta(minutes=5),
    )
    await LoginSessionStore(app.state.redis, retention_seconds=600).save(session)

    response = await client.get(f"{SESSIONS}/abandoned", headers=tenant.headers)

    assert response.json()["data"]["status"] == LoginSessionStatus.EXPIRED.value


async def test_sessions_are_for_publishing_platforms_only(client, tenant):
    response = await client.post(SESSIONS, json={"platform": "kuaishou", "name": "x"}, headers=tenant.headers)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_PARAM"


async def test_sessions_need_redis(settings):
    app = create_app(settings, configure_logging=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://hub.test"
        ) as http:
            tenant = await make_tenant(app)
            response = await http.post(
                SESSIONS, json={"platform": "douyin", "name": "x"}, headers=tenant.headers
            )

    assert response.status_code == 501
    assert response.json()["error"]["code"] == "NOT_CONFIGURED"
