from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Iterable

import httpx
import pytest
import respx
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI

from hub.constants import Platform, Scope
from hub.main import create_app
from hub.models import PlatformAccount
from hub.services.tenants import create_tenant, issue_key
from hub.settings import Settings

DTK_URL = "http://dtk.test"
SAU_URL = "http://sau.test"
ADMIN_TOKEN = "admin-test-token"
HUB_URL = "http://hub.test"


@dataclass(frozen=True)
class TenantKey:
    tenant_id: str
    key: str
    webhook_secret: str

    @property
    def headers(self) -> dict[str, str]:
        return {"X-API-Key": self.key}


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'hub.db').as_posix()}",
        dtk_base_url=DTK_URL,
        dtk_api_key="dtk-key",
        dtk_wait_seconds=5,
        dtk_timeout_seconds=10,
        sau_base_url=SAU_URL,
        media_dir=str(tmp_path / "media"),
        admin_token=ADMIN_TOKEN,
        allow_private_urls=True,
        channels_provider="mock",
        webhook_max_attempts=2,
    )


@pytest.fixture
def upstream() -> respx.Router:
    return respx.Router(assert_all_called=False)


@pytest.fixture
async def app(settings: Settings, upstream: respx.Router) -> AsyncIterator[FastAPI]:
    redis = FakeAsyncRedis()
    application = create_app(
        settings,
        redis=redis,
        http_transport=httpx.MockTransport(upstream.async_handler),
        configure_logging=False,
    )
    async with application.router.lifespan_context(application):
        yield application
    await redis.aclose()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=HUB_URL) as http:
        yield http


async def make_tenant(
    app: FastAPI,
    *,
    name: str = "acme",
    scopes: Iterable[Scope] = tuple(Scope),
    ip_allowlist: Iterable[str] = (),
    rate_limit: int | None = None,
) -> TenantKey:
    async with app.state.session_factory() as session:
        tenant = await create_tenant(session, name=name)
        _, key = await issue_key(
            session,
            tenant,
            name="test",
            scopes=scopes,
            ip_allowlist=ip_allowlist,
            rate_limit_per_min=rate_limit,
        )
        return TenantKey(tenant_id=tenant.id, key=key, webhook_secret=tenant.webhook_secret or "")


@pytest.fixture
async def tenant(app: FastAPI) -> TenantKey:
    return await make_tenant(app)


async def add_account(
    app: FastAPI, tenant_id: str, *, platform: Platform = Platform.DOUYIN, cookie_file: str = "cookie.json"
) -> str:
    async with app.state.session_factory() as session:
        account = PlatformAccount(
            tenant_id=tenant_id,
            platform=platform.value,
            display_name="main",
            sau_account_id=7,
            sau_user_name=f"hub-{tenant_id}-{platform.value}",
            sau_cookie_file=cookie_file,
        )
        session.add(account)
        await session.commit()
        return account.id


def envelope(data: object, *, meta: dict | None = None) -> dict:
    return {"success": True, "data": data, "error": None, "meta": {"request_id": "up", **(meta or {})}}


def upstream_error(code: str, message: str = "upstream says no", **extra: object) -> dict:
    return {
        "success": False,
        "data": None,
        "error": {"code": code, "message": message, **extra},
        "meta": {"request_id": "up"},
    }
