from __future__ import annotations

import asyncio
import json
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


class VerifyingStream(httpx.AsyncByteStream):
    """QR code, the platform's identity check, a keepalive, then the outcome once the gate opens."""

    def __init__(self, gate: asyncio.Event, verify: str) -> None:
        self._gate = gate
        self._verify = verify

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield f"data: {QR_CODE}\n\n".encode()
        yield f"data: verify:{self._verify}\n\n".encode()
        yield b"data: ping\n\n"
        await self._gate.wait()
        yield b"data: 200\n\n"


def mock_verifying_login(upstream, gate: asyncio.Event, verify: str):
    login_route = upstream.get(f"{SAU_URL}/login").mock(
        side_effect=lambda request: httpx.Response(
            200, headers={"Content-Type": "text/event-stream"}, stream=VerifyingStream(gate, verify)
        )
    )

    def accounts(request: httpx.Request) -> httpx.Response:
        user_name = login_route.calls.last.request.url.params["id"]
        return httpx.Response(
            200, json={"code": 200, "msg": None, "data": [[7, 3, "new.json", user_name, 1]]}
        )

    upstream.get(f"{SAU_URL}/getAccounts").mock(side_effect=accounts)
    return login_route


SMS_CHECK = json.dumps(
    {"method": "sms", "phone": "138****1234", "screenshot": "data:image/jpeg;base64,AAAA", "junk": "x"}
)


async def test_an_identity_check_waits_for_the_owners_code(client, tenant, upstream):
    gate = asyncio.Event()
    login_route = mock_verifying_login(upstream, gate, SMS_CHECK)
    sau_ok = {"code": 200, "msg": None, "data": None}
    relayed = upstream.post(f"{SAU_URL}/loginInput").respond(200, json=sau_ok)

    created = (
        await client.post(SESSIONS, json={"platform": "douyin", "name": "主账号"}, headers=tenant.headers)
    ).json()["data"]
    waiting = await poll(
        client, tenant.headers, created["session_id"], {LoginSessionStatus.VERIFICATION_REQUIRED.value}
    )

    assert waiting["qrcode"] is None
    assert waiting["verification"] == {
        "method": "sms",
        "phone": "138****1234",
        "screenshot": "data:image/jpeg;base64,AAAA",
    }
    # The owner gets time to receive and type the code, beyond the plain QR window.
    assert waiting["expires_at"] > created["expires_at"]

    submitted = await client.post(
        f"{SESSIONS}/{created['session_id']}/verification", json={"code": "123456"}, headers=tenant.headers
    )
    assert submitted.status_code == 202
    assert submitted.json()["data"]["verification"]["submitted_at"]
    sent = json.loads(relayed.calls.last.request.content)
    assert sent == {"id": login_route.calls.last.request.url.params["id"], "code": "123456"}

    gate.set()
    done = await poll(client, tenant.headers, created["session_id"], TERMINAL)
    assert done["status"] == LoginSessionStatus.SUCCEEDED.value
    assert done["verification"] is None


async def test_a_code_is_refused_when_no_check_is_open(client, tenant, upstream):
    gate = asyncio.Event()
    mock_gated_login(upstream, gate, "200")
    created = (
        await client.post(SESSIONS, json={"platform": "douyin", "name": "x"}, headers=tenant.headers)
    ).json()["data"]
    await poll(client, tenant.headers, created["session_id"], {LoginSessionStatus.QRCODE_READY.value})

    refused = await client.post(
        f"{SESSIONS}/{created['session_id']}/verification", json={"code": "123456"}, headers=tenant.headers
    )
    malformed = await client.post(
        f"{SESSIONS}/{created['session_id']}/verification", json={"code": "12ab"}, headers=tenant.headers
    )

    assert refused.status_code == 409
    assert refused.json()["error"]["details"]["status"] == LoginSessionStatus.QRCODE_READY.value
    assert malformed.status_code == 400
    gate.set()


def test_the_verification_view_keeps_only_bounded_known_fields():
    from hub.services.accounts import verification_view

    assert verification_view("not json") == {"method": "unsupported"}
    assert verification_view(json.dumps({"method": "voice", "message": " 请完成验证 "})) == {
        "method": "unsupported",
        "message": "请完成验证",
    }
    long_error = verification_view(json.dumps({"method": "sms", "error": "错" * 500}))
    assert len(long_error["error"]) == 200
    assert "screenshot" not in verification_view(json.dumps({"method": "sms", "screenshot": "javascript:x"}))


async def test_the_owner_can_answer_an_sms_send_check_with_sent(client, tenant, upstream):
    gate = asyncio.Event()
    check = json.dumps({"method": "sms_send", "phone": "138****1234", "message": "编辑短信 Y7Q2 发送至 1069"})
    login_route = mock_verifying_login(upstream, gate, check)
    relayed = upstream.post(f"{SAU_URL}/loginInput").respond(
        200, json={"code": 200, "msg": None, "data": None}
    )

    created = (
        await client.post(SESSIONS, json={"platform": "douyin", "name": "主账号"}, headers=tenant.headers)
    ).json()["data"]
    waiting = await poll(
        client, tenant.headers, created["session_id"], {LoginSessionStatus.VERIFICATION_REQUIRED.value}
    )
    assert (waiting["verification"]["method"], waiting["verification"]["message"]) == (
        "sms_send",
        "编辑短信 Y7Q2 发送至 1069",
    )

    both = await client.post(
        f"{SESSIONS}/{created['session_id']}/verification",
        json={"code": "123456", "sent": True},
        headers=tenant.headers,
    )
    neither = await client.post(
        f"{SESSIONS}/{created['session_id']}/verification", json={}, headers=tenant.headers
    )
    sent = await client.post(
        f"{SESSIONS}/{created['session_id']}/verification", json={"sent": True}, headers=tenant.headers
    )

    assert (both.status_code, neither.status_code, sent.status_code) == (400, 400, 202)
    assert json.loads(relayed.calls.last.request.content) == {
        "id": login_route.calls.last.request.url.params["id"],
        "action": "sent",
    }
    gate.set()
    done = await poll(client, tenant.headers, created["session_id"], TERMINAL)
    assert done["status"] == LoginSessionStatus.SUCCEEDED.value


async def test_a_code_that_never_arrived_can_be_resent(client, tenant, upstream):
    gate = asyncio.Event()
    check = json.dumps({"method": "sms", "phone": "156******69", "notice": "已重新发送验证码，请留意短信"})
    login_route = mock_verifying_login(upstream, gate, check)
    relayed = upstream.post(f"{SAU_URL}/loginInput").respond(
        200, json={"code": 200, "msg": None, "data": None}
    )
    created = (
        await client.post(SESSIONS, json={"platform": "douyin", "name": "主账号"}, headers=tenant.headers)
    ).json()["data"]
    waiting = await poll(
        client, tenant.headers, created["session_id"], {LoginSessionStatus.VERIFICATION_REQUIRED.value}
    )
    assert waiting["verification"]["notice"] == "已重新发送验证码，请留意短信"

    resent = await client.post(
        f"{SESSIONS}/{created['session_id']}/verification", json={"resend": True}, headers=tenant.headers
    )
    three = await client.post(
        f"{SESSIONS}/{created['session_id']}/verification",
        json={"resend": True, "sent": True},
        headers=tenant.headers,
    )

    assert (resent.status_code, three.status_code) == (202, 400)
    assert "submitted_at" not in resent.json()["data"]["verification"], "a resend is not an answer to check"
    assert json.loads(relayed.calls.last.request.content) == {
        "id": login_route.calls.last.request.url.params["id"],
        "action": "resend",
    }
    gate.set()


async def test_a_replaced_login_is_cancelled_upstream_and_stays_cancelled(client, tenant, upstream):
    gate = asyncio.Event()
    login_route = mock_gated_login(upstream, gate, "200")
    cancelled = upstream.post(f"{SAU_URL}/loginCancel").respond(
        200, json={"code": 200, "msg": None, "data": None}
    )
    forgotten = upstream.get(f"{SAU_URL}/deleteAccount").respond(
        200, json={"code": 200, "msg": "ok", "data": None}
    )
    created = (
        await client.post(SESSIONS, json={"platform": "douyin", "name": "x"}, headers=tenant.headers)
    ).json()["data"]
    await poll(client, tenant.headers, created["session_id"], {LoginSessionStatus.QRCODE_READY.value})

    response = await client.delete(f"{SESSIONS}/{created['session_id']}", headers=tenant.headers)

    assert response.status_code == 200
    view = response.json()["data"]
    assert (view["status"], view["qrcode"], view["error"]["code"]) == ("expired", None, "CANCELLED")
    assert json.loads(cancelled.calls.last.request.content) == {
        "id": login_route.calls.last.request.url.params["id"]
    }
    # The login finishing upstream afterwards must not resurrect the session.
    gate.set()
    await asyncio.sleep(0.05)
    again = (await client.get(f"{SESSIONS}/{created['session_id']}", headers=tenant.headers)).json()["data"]
    assert again["status"] == "expired"
    listed = (await client.get("/api/v1/accounts", headers=tenant.headers)).json()["data"]["items"]
    assert listed == []
    assert forgotten.called, "the jar SAU stored for the cancelled login is removed"
