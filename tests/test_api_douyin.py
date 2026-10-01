from __future__ import annotations

from datetime import datetime, timezone

from conftest import ADMIN_TOKEN, DTK_URL, envelope, make_tenant, upstream_error

SEARCH_PAGE = {
    "items": [
        {"content_id": "cold", "stats": {"digg_count": 3}, "created_at": "2026-09-28T00:00:00Z"},
        {
            "content_id": "hot",
            "stats": {"digg_count": 900, "share_count": 40},
            "created_at": "2026-09-28T00:00:00Z",
        },
    ],
    "cursor": "20|log-1",
    "has_more": True,
}


async def test_search_passes_filters_upstream_and_ranks_by_heat(client, tenant, upstream):
    route = upstream.get(f"{DTK_URL}/api/v1/douyin/search").respond(
        200,
        json=envelope(SEARCH_PAGE, meta={"cached": False, "cursor": {"next": "20|log-1", "has_more": True}}),
    )
    response = await client.get(
        "/api/v1/douyin/search",
        params={"keyword": "露营", "sort": "most_liked", "publish_time": 7, "count": 10},
        headers=tenant.headers,
    )
    body = response.json()
    assert response.status_code == 200
    assert [item["content_id"] for item in body["data"]["items"]] == ["hot", "cold"]
    assert body["data"]["items"][0]["heat_score"] > body["data"]["items"][1]["heat_score"]
    assert body["data"]["cursor"] == "20|log-1"
    assert body["meta"]["cursor"] == {"next": "20|log-1", "has_more": True}
    sent = route.calls.last.request.url.params
    assert (sent["keyword"], sent["sort_type"], sent["publish_time"], sent["count"]) == (
        "露营",
        "1",
        "7",
        "10",
    )


async def test_search_upstream_rank_keeps_platform_order(client, tenant, upstream):
    upstream.get(f"{DTK_URL}/api/v1/douyin/search").respond(200, json=envelope(SEARCH_PAGE))
    response = await client.get(
        "/api/v1/douyin/search", params={"keyword": "k", "rank": "upstream"}, headers=tenant.headers
    )
    items = response.json()["data"]["items"]
    assert [item["content_id"] for item in items] == ["cold", "hot"]
    assert "heat_score" not in items[0]


async def test_slow_search_becomes_task_resolved_on_poll_and_ranked(client, tenant, upstream):
    upstream.get(f"{DTK_URL}/api/v1/douyin/search").respond(
        202, json=envelope({"task_id": "up-9", "state": "running"})
    )
    task_route = upstream.get(f"{DTK_URL}/api/v1/tasks/up-9")
    task_route.respond(200, json=envelope({"state": "running"}))

    accepted = await client.get("/api/v1/douyin/search", params={"keyword": "k"}, headers=tenant.headers)
    assert accepted.status_code == 202
    task_id = accepted.json()["data"]["task_id"]
    assert task_id != "up-9"

    still_running = await client.get(f"/api/v1/tasks/{task_id}", headers=tenant.headers)
    assert still_running.json()["data"]["status"] == "running"

    task_route.respond(200, json=envelope({"state": "done", "data": SEARCH_PAGE}))
    done = (await client.get(f"/api/v1/tasks/{task_id}", headers=tenant.headers)).json()["data"]
    assert done["status"] == "succeeded"
    assert [item["content_id"] for item in done["result"]["items"]] == ["hot", "cold"]

    calls_before = task_route.call_count
    again = (await client.get(f"/api/v1/tasks/{task_id}", headers=tenant.headers)).json()["data"]
    assert again["status"] == "succeeded"
    assert task_route.call_count == calls_before


async def test_tasks_are_invisible_to_other_tenants(app, client, tenant, upstream):
    upstream.post(f"{DTK_URL}/api/v1/parse").respond(
        202, json=envelope({"task_id": "up-1", "state": "queued"})
    )
    accepted = await client.post(
        "/api/v1/douyin/parse", json={"url": "https://v.douyin.com/x/"}, headers=tenant.headers
    )
    other = await make_tenant(app, name="other")
    response = await client.get(f"/api/v1/tasks/{accepted.json()['data']['task_id']}", headers=other.headers)
    assert response.status_code == 404


async def test_failed_upstream_task_is_reported_as_failed(client, tenant, upstream):
    upstream.get(f"{DTK_URL}/api/v1/douyin/video").respond(202, json=envelope({"task_id": "up-2"}))
    upstream.get(f"{DTK_URL}/api/v1/tasks/up-2").respond(
        200, json=envelope({"state": "failed", "error": {"code": "CONTENT_PRIVATE", "message": "private"}})
    )
    accepted = await client.get("/api/v1/douyin/video", params={"aweme_id": "7"}, headers=tenant.headers)
    polled = (
        await client.get(f"/api/v1/tasks/{accepted.json()['data']['task_id']}", headers=tenant.headers)
    ).json()
    assert polled["data"]["status"] == "failed"
    assert polled["data"]["error"]["code"] == "CONTENT_UNAVAILABLE"


async def test_video_requires_exactly_one_identifier(client, tenant):
    neither = await client.get("/api/v1/douyin/video", headers=tenant.headers)
    both = await client.get(
        "/api/v1/douyin/video", params={"url": "u", "aweme_id": "1"}, headers=tenant.headers
    )
    assert neither.status_code == both.status_code == 400
    assert neither.json()["error"]["code"] == "INVALID_PARAM"


async def test_upstream_pressure_is_503_with_retry_after(client, tenant, upstream):
    upstream.get(f"{DTK_URL}/api/v1/douyin/video/comments").respond(
        503, json=upstream_error("IDENTITY_POOL_EXHAUSTED", retry_after=30)
    )
    response = await client.get("/api/v1/douyin/comments", params={"aweme_id": "1"}, headers=tenant.headers)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "UPSTREAM_UNAVAILABLE"
    assert response.json()["error"]["retryable"] is True
    assert response.headers["Retry-After"] == "30"


async def test_comment_replies_and_user_posts_are_forwarded(client, tenant, upstream):
    replies = upstream.get(f"{DTK_URL}/api/v1/douyin/video/comments/replies").respond(
        200, json=envelope({"items": [], "cursor": None, "has_more": False})
    )
    posts = upstream.get(f"{DTK_URL}/api/v1/douyin/user/posts").respond(
        200, json=envelope({"items": [], "cursor": None, "has_more": False})
    )
    r1 = await client.get(
        "/api/v1/douyin/comments/replies",
        params={"aweme_id": "1", "comment_id": "c9", "cursor": "20"},
        headers=tenant.headers,
    )
    r2 = await client.get(
        "/api/v1/douyin/user/posts", params={"sec_user_id": "MS4wLjAB"}, headers=tenant.headers
    )
    assert r1.status_code == r2.status_code == 200
    assert replies.calls.last.request.url.params["comment_id"] == "c9"
    assert posts.calls.last.request.url.params["sec_user_id"] == "MS4wLjAB"


async def test_hot_board(client, tenant, upstream):
    upstream.get(f"{DTK_URL}/api/v1/douyin/hot").respond(
        200, json=envelope({"items": [{"word": "热点", "hot_value": 100}], "active_time": None})
    )
    response = await client.get("/api/v1/douyin/hot", headers=tenant.headers)
    assert response.json()["data"]["items"][0]["word"] == "热点"


async def test_download_lifecycle_and_file_streaming(client, tenant, upstream):
    upstream.post(f"{DTK_URL}/api/v1/downloads").respond(
        202, json=envelope({"download_id": "dl-1", "task_id": "t", "state": "queued"})
    )
    download_route = upstream.get(f"{DTK_URL}/api/v1/downloads/dl-1")
    download_route.respond(200, json=envelope({"id": "dl-1", "state": "running", "files": []}))
    upstream.get(f"{DTK_URL}/api/v1/downloads/dl-1/files/video.mp4").respond(
        200,
        content=b"MP4DATA",
        headers={"Content-Type": "video/mp4", "Content-Disposition": 'attachment; filename="video.mp4"'},
    )

    started = await client.post("/api/v1/douyin/downloads", json={"aweme_id": "7"}, headers=tenant.headers)
    assert started.status_code == 202
    task_id = started.json()["data"]["task_id"]

    download_route.respond(
        200,
        json=envelope(
            {
                "id": "dl-1",
                "state": "done",
                "bytes_total": 7,
                "files": [{"name": "video.mp4", "kind": "video", "state": "done", "bytes": 7}],
            }
        ),
    )
    finished = (await client.get(f"/api/v1/douyin/downloads/{task_id}", headers=tenant.headers)).json()[
        "data"
    ]
    assert finished["status"] == "succeeded"
    assert finished["result"]["files"][0]["name"] == "video.mp4"

    file_response = await client.get(
        f"/api/v1/douyin/downloads/{task_id}/files/video.mp4", headers=tenant.headers
    )
    assert file_response.status_code == 200
    assert file_response.content == b"MP4DATA"
    assert file_response.headers["Content-Type"] == "video/mp4"

    unknown = await client.get(f"/api/v1/douyin/downloads/{task_id}/files/other.mp4", headers=tenant.headers)
    assert unknown.status_code == 404


async def test_successful_calls_are_metered_per_route(client, tenant, upstream):
    upstream.get(f"{DTK_URL}/api/v1/douyin/hot").respond(200, json=envelope({"items": []}))
    for _ in range(2):
        await client.get("/api/v1/douyin/hot", headers=tenant.headers)
    today = datetime.now(timezone.utc).date().isoformat()
    report = await client.get(
        f"/api/v1/admin/tenants/{tenant.tenant_id}/usage",
        params={"start": today, "end": today},
        headers={"X-Admin-Token": ADMIN_TOKEN},
    )
    counts = {row["endpoint"]: row["count"] for row in report.json()["data"]["items"]}
    assert counts["/api/v1/douyin/hot"] == 2
