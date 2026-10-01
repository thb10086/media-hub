from __future__ import annotations

import pytest
from pydantic import ValidationError

from conftest import add_account
from hub.constants import Platform
from hub.platforms import DataCapability, catalog
from hub.settings import Settings

PLATFORMS_PATH = "/api/v1/platforms"


def _by_platform(items: list[dict]) -> dict[str, dict]:
    return {item["platform"]: item for item in items}


def test_the_catalog_reports_what_is_configured_right_now() -> None:
    items = _by_platform(
        catalog(
            dtk_available=True,
            channels_available=False,
            sau_available=True,
            publish_enabled={Platform.DOUYIN, Platform.CHANNELS},
        )
    )

    douyin = items["douyin"]
    assert douyin["data"]["available"] is True
    assert DataCapability.SEARCH.value in douyin["data"]["capabilities"]
    assert DataCapability.HOT.value in douyin["data"]["capabilities"]
    assert douyin["publish"] == {"available": True, "draft": False, "schedule": True}

    channels = items["channels"]
    assert channels["data"]["available"] is False
    assert channels["publish"]["available"] is True
    assert channels["publish"]["draft"] is True

    assert items["kuaishou"]["publish"]["available"] is False
    assert items["kuaishou"]["data"] == {"available": False, "capabilities": []}


def test_nothing_publishes_without_the_publishing_service() -> None:
    items = catalog(
        dtk_available=False,
        channels_available=False,
        sau_available=False,
        publish_enabled={Platform.DOUYIN},
    )

    assert not any(item["publish"]["available"] for item in items)


async def test_the_catalog_endpoint_lists_every_platform(client, tenant) -> None:
    response = await client.get(PLATFORMS_PATH, headers=tenant.headers)

    assert response.status_code == 200
    items = _by_platform(response.json()["data"]["items"])
    assert set(items) == {platform.value for platform in Platform}
    assert items["douyin"]["name"] == "抖音"
    assert items["douyin"]["data"]["available"] is True
    assert items["channels"]["data"]["available"] is True


async def test_the_catalog_needs_an_api_key(client) -> None:
    assert (await client.get(PLATFORMS_PATH)).status_code == 401


def test_only_platforms_with_a_publishing_driver_can_be_enabled() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, publish_platforms=["douyin", "no-such-platform"])


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"publish_platforms": [Platform.DOUYIN, Platform.KUAISHOU]})


async def test_publishing_follows_the_configured_platforms(app, client, tenant) -> None:
    kuaishou = await add_account(app, tenant.tenant_id, platform=Platform.KUAISHOU)
    channels = await add_account(app, tenant.tenant_id, platform=Platform.CHANNELS)
    body = {"video_url": "https://cdn.example.com/a.mp4", "title": "t"}

    accepted = await client.post(
        "/api/v1/publish/videos",
        json={**body, "platform": "kuaishou", "account_id": kuaishou},
        headers=tenant.headers,
    )
    refused = await client.post(
        "/api/v1/publish/videos",
        json={**body, "platform": "channels", "account_id": channels},
        headers=tenant.headers,
    )

    assert accepted.status_code == 202
    assert refused.status_code == 400
    assert refused.json()["error"]["details"]["supported"] == ["douyin", "kuaishou"]
