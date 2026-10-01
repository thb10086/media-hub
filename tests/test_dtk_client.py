from __future__ import annotations

import httpx
import pytest
import respx

from conftest import DTK_URL, envelope, upstream_error
from hub.errors import ErrorCode, HubError
from hub.providers.dtk import Done, DtkClient, Pending, map_upstream_error


@pytest.fixture
def router() -> respx.Router:
    return respx.Router(assert_all_called=False)


@pytest.fixture
async def dtk(router: respx.Router):
    client = DtkClient(
        DTK_URL, "dtk-key", wait_seconds=7, timeout=10, transport=httpx.MockTransport(router.async_handler)
    )
    yield client
    await client.aclose()


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("IDENTITY_POOL_EXHAUSTED", ErrorCode.UPSTREAM_UNAVAILABLE),
        ("UPSTREAM_RISK_CONTROL", ErrorCode.UPSTREAM_UNAVAILABLE),
        ("INVALID_URL", ErrorCode.INVALID_PARAM),
        ("CONTENT_PRIVATE", ErrorCode.CONTENT_UNAVAILABLE),
        ("NOT_FOUND", ErrorCode.CONTENT_UNAVAILABLE),
        ("NOT_CONFIGURED", ErrorCode.NOT_CONFIGURED),
        ("UNAUTHENTICATED", ErrorCode.UPSTREAM_ERROR),
        ("UPSTREAM_CHANGED", ErrorCode.UPSTREAM_ERROR),
        ("SOMETHING_NEW", ErrorCode.UPSTREAM_ERROR),
    ],
)
def test_upstream_codes_fold_into_gateway_codes(code, expected):
    error = map_upstream_error({"code": code, "message": "m", "retry_after": 3})
    assert error.code is expected
    assert error.details["upstream_code"] == code


def test_missing_route_means_not_configured_only_when_asked():
    assert (
        map_upstream_error({"code": "NOT_FOUND"}, missing_route_is_unconfigured=True).code
        is ErrorCode.NOT_CONFIGURED
    )


async def test_result_within_wait_window_is_done(router, dtk):
    route = router.get(f"{DTK_URL}/api/v1/douyin/video").respond(
        200, json=envelope({"content_id": "1"}, meta={"cached": True, "task_id": "t", "cursor": None})
    )
    outcome = await dtk.video(url=None, aweme_id="1")
    assert isinstance(outcome, Done)
    assert outcome.data == {"content_id": "1"}
    assert outcome.public_meta() == {"cached": True, "cursor": None}
    sent = route.calls.last.request
    assert sent.headers["X-API-Key"] == "dtk-key"
    assert sent.url.params["wait"] == "7"
    assert "url" not in sent.url.params


async def test_accepted_is_pending_with_upstream_task(router, dtk):
    router.post(f"{DTK_URL}/api/v1/parse").respond(
        202, json=envelope({"task_id": "up-1", "state": "running"})
    )
    outcome = await dtk.parse("https://v.douyin.com/abc/")
    assert outcome == Pending("up-1")


async def test_error_envelope_raises_mapped_error_with_retry_after(router, dtk):
    router.get(f"{DTK_URL}/api/v1/douyin/video/comments").respond(
        503, json=upstream_error("QUEUE_FULL", retry_after=10)
    )
    with pytest.raises(HubError) as caught:
        await dtk.comments(url=None, aweme_id="1", cursor=None, count=20)
    assert caught.value.code is ErrorCode.UPSTREAM_UNAVAILABLE
    assert caught.value.retry_after == 10


async def test_search_without_patched_route_is_not_configured(router, dtk):
    router.get(f"{DTK_URL}/api/v1/douyin/search").respond(404, json=upstream_error("NOT_FOUND", "Not Found"))
    with pytest.raises(HubError) as caught:
        await dtk.search(keyword="k", sort_type=1, publish_time=0, cursor=None, count=20)
    assert caught.value.code is ErrorCode.NOT_CONFIGURED


async def test_non_envelope_body_is_upstream_error(router, dtk):
    router.get(f"{DTK_URL}/api/v1/douyin/hot").respond(502, text="<html>bad gateway</html>")
    with pytest.raises(HubError) as caught:
        await dtk.hot()
    assert caught.value.code is ErrorCode.UPSTREAM_ERROR


async def test_transport_failures_map_to_unavailable_and_timeout(router, dtk):
    router.get(f"{DTK_URL}/api/v1/douyin/user").mock(side_effect=httpx.ConnectError("refused"))
    router.get(f"{DTK_URL}/api/v1/douyin/user/posts").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(HubError) as unavailable:
        await dtk.user(url=None, sec_user_id="MS4wLjAB")
    with pytest.raises(HubError) as timeout:
        await dtk.user_posts(url=None, sec_user_id="MS4wLjAB", cursor=None, count=20)
    assert unavailable.value.code is ErrorCode.UPSTREAM_UNAVAILABLE
    assert timeout.value.code is ErrorCode.UPSTREAM_TIMEOUT


async def test_task_states(router, dtk):
    router.get(f"{DTK_URL}/api/v1/tasks/running").respond(200, json=envelope({"state": "running"}))
    router.get(f"{DTK_URL}/api/v1/tasks/done").respond(
        200, json=envelope({"state": "done", "data": {"x": 1}})
    )
    router.get(f"{DTK_URL}/api/v1/tasks/failed").respond(
        200, json=envelope({"state": "failed", "error": {"code": "CONTENT_PRIVATE", "message": "gone"}})
    )
    assert not (await dtk.task("running")).finished
    assert (await dtk.task("done")).data == {"x": 1}
    failed = await dtk.task("failed")
    assert failed.finished and failed.error.code is ErrorCode.CONTENT_UNAVAILABLE
