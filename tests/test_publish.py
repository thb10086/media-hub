from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from conftest import SAU_URL, add_account
from hub.constants import Platform, TaskKind, TaskStatus
from hub.models import Task, utcnow
from hub.providers.sau import SauClient
from hub.services.tasks import fail_stale
from hub.services.webhooks import sign
from hub.worker import Worker

CALLBACK_URL = "http://client.test/hooks"
VIDEO_BYTES = b"\x00\x00\x00\x18ftypmp42fake-video"


def sau_ok(data: object = None) -> dict:
    return {"code": 200, "msg": "ok", "data": data}


async def upload(client, tenant, name: str = "clip.mp4", content: bytes = VIDEO_BYTES):
    return await client.post(
        "/api/v1/media", files={"file": (name, content, "video/mp4")}, headers=tenant.headers
    )


def worker_for(app) -> Worker:
    state = app.state
    return Worker(state.settings, session_factory=state.session_factory, sau=state.sau, http=state.http)


def publish_body(account_id: str, media_id: str, **overrides: object) -> dict:
    return {
        "platform": "douyin",
        "account_id": account_id,
        "media_id": media_id,
        "title": "测试视频",
        "tags": ["露营", "户外"],
        **overrides,
    }


@pytest.fixture
async def prepared(app, client, tenant):
    media = await upload(client, tenant)
    account_id = await add_account(app, tenant.tenant_id)
    return media.json()["data"]["media_id"], account_id


async def test_upload_accepts_videos_and_rejects_other_files(client, tenant):
    accepted = await upload(client, tenant)
    rejected = await upload(client, tenant, name="notes.txt")
    assert accepted.status_code == 201
    assert accepted.json()["data"]["size_bytes"] == len(VIDEO_BYTES)
    assert rejected.status_code == 400


async def test_upload_over_the_limit_is_refused_before_reading(client, tenant, settings):
    oversized = str(settings.max_upload_bytes * 2)
    response = await client.post(
        "/api/v1/media",
        content=b"x",
        headers={
            **tenant.headers,
            "Content-Type": "multipart/form-data; boundary=b",
            "Content-Length": oversized,
        },
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


async def test_publish_runs_in_worker_and_signs_the_callback(app, client, tenant, upstream, prepared):
    media_id, account_id = prepared
    upstream.post(f"{SAU_URL}/upload").respond(200, json=sau_ok("staged.mp4"))
    post_route = upstream.post(f"{SAU_URL}/postVideo").respond(200, json=sau_ok())
    hook = upstream.post(CALLBACK_URL).respond(200)

    accepted = await client.post(
        "/api/v1/publish/videos",
        json=publish_body(account_id, media_id, callback_url=CALLBACK_URL),
        headers=tenant.headers,
    )
    assert accepted.status_code == 202
    task_id = accepted.json()["data"]["task_id"]
    assert accepted.json()["data"]["status"] == "queued"

    worker = worker_for(app)
    assert await worker.run_once() is True
    assert await worker.run_once() is False

    task = (await client.get(f"/api/v1/tasks/{task_id}", headers=tenant.headers)).json()["data"]
    assert task["status"] == "succeeded"
    assert task["result"] == {
        "platform": "douyin",
        "account_id": account_id,
        "scheduled": False,
        "draft": False,
        "cover": False,
    }

    sent = json.loads(post_route.calls.last.request.content)
    assert sent["type"] == 3
    assert sent["fileList"] == ["staged.mp4"]
    assert sent["accountList"] == ["cookie.json"]
    assert sent["tags"] == ["露营", "户外"]
    assert sent["enableTimer"] is False

    delivered = hook.calls.last.request
    assert delivered.headers["X-Hub-Event"] == "task.succeeded"
    assert delivered.headers["X-Hub-Signature"] == sign(delivered.content, tenant.webhook_secret)
    assert json.loads(delivered.content)["task_id"] == task_id


async def test_publish_failure_is_recorded(app, client, tenant, upstream, prepared):
    media_id, account_id = prepared
    upstream.post(f"{SAU_URL}/upload").respond(200, json=sau_ok("staged.mp4"))
    upstream.post(f"{SAU_URL}/postVideo").respond(
        500, json={"code": 500, "msg": "发布失败: 页面超时", "data": None}
    )

    accepted = await client.post(
        "/api/v1/publish/videos", json=publish_body(account_id, media_id), headers=tenant.headers
    )
    await worker_for(app).run_once()

    task = (
        await client.get(f"/api/v1/tasks/{accepted.json()['data']['task_id']}", headers=tenant.headers)
    ).json()["data"]
    assert task["status"] == "failed"
    assert task["error"]["code"] == "UPSTREAM_ERROR"
    assert "页面超时" in task["error"]["message"]


async def test_schedule_and_draft_reach_the_publisher(app, client, tenant, upstream):
    media = (await upload(client, tenant)).json()["data"]["media_id"]
    account_id = await add_account(app, tenant.tenant_id, platform=Platform.CHANNELS)
    upstream.post(f"{SAU_URL}/upload").respond(200, json=sau_ok("staged.mp4"))
    post_route = upstream.post(f"{SAU_URL}/postVideo").respond(200, json=sau_ok())

    body = publish_body(
        account_id, media, platform="channels", draft=True, schedule={"daily_times": [16], "start_days": 1}
    )
    accepted = await client.post("/api/v1/publish/videos", json=body, headers=tenant.headers)
    assert accepted.status_code == 202
    await worker_for(app).run_once()

    sent = json.loads(post_route.calls.last.request.content)
    assert (sent["type"], sent["enableTimer"], sent["dailyTimes"], sent["startDays"], sent["isDraft"]) == (
        2,
        True,
        [16],
        1,
        True,
    )


async def test_video_url_is_downloaded_then_removed(app, client, tenant, upstream, settings):
    account_id = await add_account(app, tenant.tenant_id)
    upstream.get("http://cdn.test/v/clip.mp4").respond(200, content=VIDEO_BYTES)
    upload_route = upstream.post(f"{SAU_URL}/upload").respond(200, json=sau_ok("staged.mp4"))
    upstream.post(f"{SAU_URL}/postVideo").respond(200, json=sau_ok())

    body = publish_body(account_id, None, video_url="http://cdn.test/v/clip.mp4")
    accepted = await client.post("/api/v1/publish/videos", json=body, headers=tenant.headers)
    assert accepted.status_code == 202
    await worker_for(app).run_once()

    assert VIDEO_BYTES in upload_route.calls.last.request.content
    leftovers = list((Path(settings.media_dir) / tenant.tenant_id).iterdir())
    assert leftovers == []


async def test_idempotency_key_returns_the_same_task_or_conflicts(client, tenant, prepared):
    media_id, account_id = prepared
    headers = {**tenant.headers, "Idempotency-Key": "order-42"}
    first = await client.post(
        "/api/v1/publish/videos", json=publish_body(account_id, media_id), headers=headers
    )
    repeat = await client.post(
        "/api/v1/publish/videos", json=publish_body(account_id, media_id), headers=headers
    )
    changed = await client.post(
        "/api/v1/publish/videos", json=publish_body(account_id, media_id, title="另一个标题"), headers=headers
    )
    assert first.status_code == 202
    assert repeat.status_code == 200
    assert repeat.json()["data"]["task_id"] == first.json()["data"]["task_id"]
    assert changed.status_code == 409
    assert changed.json()["error"]["code"] == "CONFLICT"


@pytest.mark.parametrize(
    "overrides",
    [
        {"video_url": "http://cdn.test/a.mp4"},
        {"draft": True},
        {"platform": "xiaohongshu"},
        {"platform": "channels"},
        {"media_id": "no-such-media"},
    ],
)
async def test_invalid_publish_requests_are_refused(client, tenant, prepared, overrides):
    media_id, account_id = prepared
    body = {**publish_body(account_id, media_id), **overrides}
    response = await client.post("/api/v1/publish/videos", json=body, headers=tenant.headers)
    assert response.status_code in (400, 404)
    assert response.json()["success"] is False


async def test_accounts_of_other_tenants_cannot_be_used(app, client, tenant, prepared):
    from conftest import make_tenant

    media_id, account_id = prepared
    other = await make_tenant(app, name="other")
    response = await client.post(
        "/api/v1/publish/videos", json=publish_body(account_id, media_id), headers=other.headers
    )
    assert response.status_code == 404


async def test_stale_running_tasks_fail_instead_of_retrying(app, tenant):
    async with app.state.session_factory() as session:
        task = Task(
            tenant_id=tenant.tenant_id,
            kind=TaskKind.PUBLISH_VIDEO.value,
            status=TaskStatus.RUNNING.value,
            payload={},
            started_at=utcnow() - timedelta(hours=2),
        )
        session.add(task)
        await session.commit()
        failed = await fail_stale(
            session, kinds=[TaskKind.PUBLISH_VIDEO.value], older_than=timedelta(hours=1)
        )
        await session.refresh(task)
    assert failed == 1
    assert task.status == TaskStatus.FAILED.value
    assert task.error["code"] == "INTERNAL"


async def test_shared_directory_staging_copies_and_discards(tmp_path):
    shared = tmp_path / "videoFile"
    shared.mkdir()
    source = tmp_path / "clip.mp4"
    source.write_bytes(VIDEO_BYTES)
    client = SauClient(SAU_URL, request_timeout=1, publish_timeout=1, login_timeout=1, video_dir=str(shared))
    try:
        name = await client.stage_video(source)
        assert (shared / name).read_bytes() == VIDEO_BYTES
        await client.discard_video(name)
        assert not (shared / name).exists()
    finally:
        await client.aclose()


COVER_BYTES = b"\xff\xd8\xff\xe0fake-jpeg"


async def test_body_text_and_cover_reach_the_douyin_uploader(app, client, tenant, upstream, settings):
    account_id = await add_account(app, tenant.tenant_id)
    upstream.get("http://cdn.test/v/clip.mp4").respond(200, content=VIDEO_BYTES)
    upstream.get("http://cdn.test/c/cover.jpg").respond(200, content=COVER_BYTES)
    upload_route = upstream.post(f"{SAU_URL}/upload")
    upload_route.side_effect = [
        httpx.Response(200, json=sau_ok("staged.mp4")),
        httpx.Response(200, json=sau_ok("cover.jpg")),
    ]
    post_route = upstream.post(f"{SAU_URL}/postVideo").respond(200, json=sau_ok())

    body = publish_body(
        account_id,
        None,
        video_url="http://cdn.test/v/clip.mp4",
        description="店就在长河地铁 B 口，晚上九点前都有货。",
        cover_url="http://cdn.test/c/cover.jpg",
    )
    accepted = await client.post("/api/v1/publish/videos", json=body, headers=tenant.headers)
    assert accepted.status_code == 202
    await worker_for(app).run_once()

    staged_cover = upload_route.calls[1].request
    assert COVER_BYTES in staged_cover.content and b"image/jpeg" in staged_cover.content
    sent = json.loads(post_route.calls.last.request.content)
    assert sent["desc"] == "店就在长河地铁 B 口，晚上九点前都有货。"
    assert sent["thumbnailPortrait"] == "cover.jpg"
    task = (
        await client.get(f"/api/v1/tasks/{accepted.json()['data']['task_id']}", headers=tenant.headers)
    ).json()
    assert task["data"]["result"]["cover"] is True
    assert list((Path(settings.media_dir) / tenant.tenant_id).iterdir()) == [], "video and cover removed"


async def test_a_cover_that_cannot_be_fetched_does_not_stop_the_post(app, client, tenant, upstream, settings):
    account_id = await add_account(app, tenant.tenant_id)
    upstream.get("http://cdn.test/v/clip.mp4").respond(200, content=VIDEO_BYTES)
    upstream.get("http://cdn.test/c/gone.jpg").respond(404)
    upstream.post(f"{SAU_URL}/upload").respond(200, json=sau_ok("staged.mp4"))
    post_route = upstream.post(f"{SAU_URL}/postVideo").respond(200, json=sau_ok())

    body = publish_body(
        account_id, None, video_url="http://cdn.test/v/clip.mp4", cover_url="http://cdn.test/c/gone.jpg"
    )
    accepted = await client.post("/api/v1/publish/videos", json=body, headers=tenant.headers)
    await worker_for(app).run_once()

    sent = json.loads(post_route.calls.last.request.content)
    assert (sent["thumbnailPortrait"], sent["desc"]) == ("", "")
    task = (
        await client.get(f"/api/v1/tasks/{accepted.json()['data']['task_id']}", headers=tenant.headers)
    ).json()
    assert task["data"]["status"] == "succeeded"
    assert task["data"]["result"]["cover"] is False
    assert list((Path(settings.media_dir) / tenant.tenant_id).iterdir()) == []


async def test_a_cover_url_on_a_private_address_is_refused(client, tenant, prepared, settings):
    media_id, account_id = prepared
    settings.allow_private_urls = False
    body = publish_body(account_id, media_id, cover_url="http://127.0.0.1/cover.jpg")
    response = await client.post("/api/v1/publish/videos", json=body, headers=tenant.headers)
    assert response.status_code == 400
    assert response.json()["error"]["details"]["field"] == "cover_url"
