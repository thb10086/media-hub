from __future__ import annotations

import hashlib
import hmac

import httpx
import pytest
import respx

from hub.constants import TaskKind, TaskStatus
from hub.errors import ErrorCode, HubError
from hub.models import Task
from hub.providers.channels import UnconfiguredChannelsProvider
from hub.services.webhooks import deliver, sign

URL = "https://client.test/hook"


def finished_task(status: TaskStatus = TaskStatus.SUCCEEDED) -> Task:
    return Task(
        id="t-1", tenant_id="tenant", kind=TaskKind.PUBLISH_VIDEO.value, status=status.value, payload={}
    )


def test_signature_is_hmac_sha256_of_raw_body():
    body = b'{"task_id":"t-1"}'
    expected = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert sign(body, "secret") == f"sha256={expected}"


@pytest.fixture
def router() -> respx.Router:
    return respx.Router()


async def _deliver(router: respx.Router, task: Task, attempts: int = 3) -> bool:
    async with httpx.AsyncClient(transport=httpx.MockTransport(router.async_handler)) as http:
        return await deliver(
            http, URL, task, secret="s", max_attempts=attempts, request_timeout=1, backoff=(0,)
        )


async def test_delivery_retries_server_errors(router):
    route = router.post(URL).mock(side_effect=[httpx.Response(500), httpx.Response(200)])
    assert await _deliver(router, finished_task()) is True
    assert route.call_count == 2


async def test_delivery_stops_on_client_errors(router):
    route = router.post(URL).respond(400)
    assert await _deliver(router, finished_task()) is False
    assert route.call_count == 1


async def test_delivery_survives_network_errors(router):
    route = router.post(URL).mock(side_effect=[httpx.ConnectError("down"), httpx.Response(204)])
    assert await _deliver(router, finished_task(TaskStatus.FAILED)) is True
    assert route.calls.last.request.headers["X-Hub-Event"] == "task.failed"


async def test_unconfigured_channels_provider_refuses():
    with pytest.raises(HubError) as caught:
        await UnconfiguredChannelsProvider().search(keyword="x", cursor=None, count=1)
    assert caught.value.code is ErrorCode.NOT_CONFIGURED
