from __future__ import annotations

import json
import time
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from conftest import DTK_URL, SAU_URL, add_account, envelope, upstream_error
from hub.constants import DEFAULT_DOUYIN_USER_AGENT, DataIdentityStatus, Platform
from hub.models import AccountDataIdentity, utcnow
from hub.services.data_identities import cookie_header, douyin_web_cookies
from test_accounts import mock_login, parse_sse

IMPORT_URL = f"{DTK_URL}/api/v1/admin/identities/import"
IDENTITY_ID = "6f1d2c3b-0000-4000-8000-000000000001"
FAR_FUTURE = time.time() + 30 * 86400


def storage_state(*, with_ttwid: bool = True) -> dict:
    cookies = [
        {"name": "sessionid", "value": "sess-1", "domain": ".douyin.com", "path": "/", "expires": FAR_FUTURE},
        {"name": "sid_guard", "value": "guard", "domain": ".douyin.com", "path": "/", "expires": FAR_FUTURE},
        {"name": "odin_tt", "value": "odin", "domain": ".douyin.com", "path": "/", "expires": -1},
        {"name": "x-web-secsdk-uid", "value": "creator-side", "domain": "creator.douyin.com", "path": "/"},
        {"name": "x-web-secsdk-uid", "value": "www-side", "domain": "www.douyin.com", "path": "/"},
        {"name": "csrf_session_id", "value": "creator-only", "domain": "creator.douyin.com", "path": "/"},
        {"name": "passport_auth", "value": "other-site", "domain": ".bytedance.com", "path": "/"},
        {"name": "stale", "value": "gone", "domain": ".douyin.com", "path": "/", "expires": 1000},
        {"name": "", "value": "nameless", "domain": "www.douyin.com", "path": "/"},
    ]
    if with_ttwid:
        cookies.append({"name": "ttwid", "value": "tt-1", "domain": ".douyin.com", "path": "/"})
    return {"cookies": cookies, "origins": []}


def mock_jar(upstream, *, state: dict | None = None):
    return upstream.get(f"{SAU_URL}/downloadCookie").respond(200, json=state or storage_state())


def mock_import(upstream, *, identity_id: str = IDENTITY_ID):
    report = {"authenticated": True, "expires_at": "2026-11-30T00:00:00+00:00", "usable": True}
    return upstream.post(IMPORT_URL).respond(
        201, json=envelope({"stored": True, "identity_id": identity_id, "report": report})
    )


def mock_retire(upstream, identity_id: str = IDENTITY_ID, *, status: int = 200, body: dict | None = None):
    return upstream.delete(f"{DTK_URL}/api/v1/admin/identities/{identity_id}").respond(
        status, json=body or envelope({"id": identity_id, "state": "retired"})
    )


async def bind(client, tenant, *, platform: str = "douyin") -> dict:
    response = await client.get(
        "/api/v1/accounts/login", params={"platform": platform, "name": "主账号"}, headers=tenant.headers
    )
    events = parse_sse(response.text)
    assert events[-1][0] == "success", events
    return events[-1][1]["account"]


async def record_of(app, account_id: str) -> AccountDataIdentity | None:
    async with app.state.session_factory() as session:
        return await session.get(AccountDataIdentity, account_id)


def test_only_cookies_sent_to_www_douyin_are_kept() -> None:
    cookies = douyin_web_cookies(storage_state())
    assert cookies == {
        "sessionid": "sess-1",
        "sid_guard": "guard",
        "odin_tt": "odin",
        "x-web-secsdk-uid": "www-side",
        "ttwid": "tt-1",
    }
    assert cookie_header({"a": "1", "b": "2"}) == "a=1; b=2"


def test_the_host_value_wins_over_the_site_value_whatever_the_order() -> None:
    state = {
        "cookies": [
            {"name": "k", "value": "host", "domain": "www.douyin.com"},
            {"name": "k", "value": "site", "domain": ".douyin.com"},
        ]
    }
    assert douyin_web_cookies(state) == {"k": "host"}


async def test_a_douyin_login_is_imported_into_the_identity_pool(app, client, tenant, upstream):
    mock_login(upstream, "200")
    jar = mock_jar(upstream)
    imported = mock_import(upstream)

    account = await bind(client, tenant)

    assert jar.calls.last.request.url.params["filePath"] == "new.json"
    payload = json.loads(imported.calls.last.request.content)
    assert payload["platform"] == "douyin"
    assert payload["user_agent"] == DEFAULT_DOUYIN_USER_AGENT
    assert (payload["language"], payload["timezone"]) == ("zh-CN", "Asia/Shanghai")
    sent = dict(pair.split("=", 1) for pair in payload["cookies"].split("; "))
    assert sent["ttwid"] == "tt-1" and sent["sessionid"] == "sess-1"
    assert "csrf_session_id" not in sent and "passport_auth" not in sent
    assert imported.calls.last.request.headers["X-API-Key"] == "dtk-key"

    view = account["data_identity"]
    assert (view["status"], view["authenticated"]) == ("active", True)
    assert view["expires_at"].startswith("2026-11-30")
    assert "error" not in view

    listed = (await client.get("/api/v1/accounts", headers=tenant.headers)).json()["data"]["items"]
    assert listed[0]["data_identity"]["status"] == "active"
    record = await record_of(app, account["account_id"])
    assert record is not None and record.identity_id == IDENTITY_ID


async def test_a_failed_import_never_fails_the_login_and_is_retried(app, client, tenant, upstream):
    mock_login(upstream, "200")
    mock_jar(upstream)
    rejected = upstream.post(IMPORT_URL).respond(
        400,
        json=upstream_error(
            "INVALID_PARAM",
            "this cookie set cannot be used as an identity",
            details={"missing_required": ["ttwid"]},
        ),
    )

    account = await bind(client, tenant)

    view = account["data_identity"]
    assert view["status"] == "failed"
    assert "missing ttwid" in view["error"]
    assert rejected.called

    # Not due yet: the worker leaves it alone.
    report = await app.state.identities.sync(app.state.session_factory)
    assert report.as_dict() == {"imported": 0, "failed": 0, "retired": 0}

    async with app.state.session_factory() as session:
        record = await session.get(AccountDataIdentity, account["account_id"])
        record.next_attempt_at = utcnow() - timedelta(seconds=1)
        await session.commit()
    upstream.routes.clear()
    mock_jar(upstream)
    mock_import(upstream)
    report = await app.state.identities.sync(app.state.session_factory)
    assert report.imported == 1
    record = await record_of(app, account["account_id"])
    assert (record.status, record.identity_id, record.error, record.attempts) == (
        "active",
        IDENTITY_ID,
        None,
        0,
    )


async def test_a_jar_without_www_cookies_is_not_sent_to_dtk(app, client, tenant, upstream):
    mock_login(upstream, "200")
    mock_jar(upstream, state=storage_state(with_ttwid=False))
    imported = mock_import(upstream)

    account = await bind(client, tenant)

    assert not imported.called
    assert account["data_identity"]["status"] == "failed"
    assert "missing ttwid" in account["data_identity"]["error"]


async def test_removing_the_account_retires_its_identity(app, client, tenant, upstream):
    mock_login(upstream, "200")
    mock_jar(upstream)
    mock_import(upstream)
    upstream.get(f"{SAU_URL}/deleteAccount").respond(200, json={"code": 200, "msg": "ok", "data": None})
    retired = mock_retire(upstream)
    account = await bind(client, tenant)

    deleted = await client.delete(f"/api/v1/accounts/{account['account_id']}", headers=tenant.headers)

    assert deleted.status_code == 200
    assert json.loads(retired.calls.last.request.content)["reason"].startswith("media-hub")
    assert await record_of(app, account["account_id"]) is None


async def test_an_unconfirmed_retirement_is_finished_by_the_worker(app, client, tenant, upstream):
    mock_login(upstream, "200")
    mock_jar(upstream)
    mock_import(upstream)
    upstream.get(f"{SAU_URL}/deleteAccount").respond(200, json={"code": 200, "msg": "ok", "data": None})
    mock_retire(upstream, status=503, body=upstream_error("INTERNAL", "busy"))
    account = await bind(client, tenant)

    deleted = await client.delete(f"/api/v1/accounts/{account['account_id']}", headers=tenant.headers)
    assert deleted.status_code == 200
    assert (await client.get("/api/v1/accounts", headers=tenant.headers)).json()["data"]["items"] == []
    record = await record_of(app, account["account_id"])
    assert record is not None and record.status == DataIdentityStatus.RETIRING.value

    async with app.state.session_factory() as session:
        row = await session.get(AccountDataIdentity, account["account_id"])
        row.next_attempt_at = utcnow() - timedelta(seconds=1)
        await session.commit()
    upstream.routes.clear()
    mock_retire(upstream)
    report = await app.state.identities.sync(app.state.session_factory)
    assert report.retired == 1
    assert await record_of(app, account["account_id"]) is None


async def test_accounts_bound_before_the_feature_are_backfilled(app, client, tenant, upstream):
    account_id = await add_account(app, tenant.tenant_id, cookie_file="old.json")
    await add_account(app, tenant.tenant_id, platform=Platform.CHANNELS, cookie_file="channels.json")
    jar = mock_jar(upstream)
    mock_import(upstream)

    listed = (await client.get("/api/v1/accounts", headers=tenant.headers)).json()["data"]["items"]
    views = {item["platform"]: item["data_identity"] for item in listed}
    assert views == {"douyin": {"status": "pending", "authenticated": False}, "channels": None}

    report = await app.state.identities.sync(app.state.session_factory)

    assert report.imported == 1
    assert jar.calls.last.request.url.params["filePath"] == "old.json"
    record = await record_of(app, account_id)
    assert record.status == "active"
    async with app.state.session_factory() as session:
        rows = (await session.scalars(select(AccountDataIdentity))).all()
    assert [row.account_id for row in rows] == [account_id]


async def test_channels_logins_have_no_data_identity(client, tenant, upstream):
    mock_login(upstream, "200")
    imported = mock_import(upstream)

    account = await bind(client, tenant, platform="channels")

    assert account["data_identity"] is None
    assert not imported.called


async def test_with_importing_off_nothing_is_imported_but_old_identities_are_retired(
    app, client, tenant, upstream
):
    app.state.identities._import_logins = False
    mock_login(upstream, "200")
    imported = mock_import(upstream)
    account = await bind(client, tenant)
    assert account["data_identity"] is None
    assert not imported.called

    async with app.state.session_factory() as session:
        session.add(
            AccountDataIdentity(
                account_id=account["account_id"],
                tenant_id=tenant.tenant_id,
                status="active",
                identity_id=IDENTITY_ID,
                attempts=0,
            )
        )
        await session.commit()
    upstream.get(f"{SAU_URL}/deleteAccount").respond(200, json={"code": 200, "msg": "ok", "data": None})
    retired = mock_retire(upstream)

    await client.delete(f"/api/v1/accounts/{account['account_id']}", headers=tenant.headers)

    assert retired.called
    assert await record_of(app, account["account_id"]) is None


async def test_the_jar_download_reports_a_missing_file(app, upstream):
    upstream.get(f"{SAU_URL}/downloadCookie").respond(404, json={"code": 500, "msg": "Cookie文件不存在"})
    with pytest.raises(Exception) as caught:
        await app.state.sau.cookie_jar("missing.json")
    assert "no saved login" in str(caught.value)


async def test_dtk_client_import_and_retire_shapes(app, upstream):
    imported = mock_import(upstream)
    retired = mock_retire(upstream)

    result = await app.state.dtk.import_identity(cookies="ttwid=1", user_agent="UA")
    await app.state.dtk.retire_identity(IDENTITY_ID, reason="why")

    assert result["identity_id"] == IDENTITY_ID
    assert json.loads(imported.calls.last.request.content) == {
        "platform": "douyin",
        "cookies": "ttwid=1",
        "user_agent": "UA",
    }
    assert isinstance(retired.calls.last.request, httpx.Request)
    assert json.loads(retired.calls.last.request.content) == {"reason": "why"}
