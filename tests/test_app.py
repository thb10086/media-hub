from __future__ import annotations

from fakeredis import FakeAsyncRedis
from sqlalchemy import inspect

from hub.main import create_app
from hub.models import Base
from hub.settings import Settings


def _settings(tmp_path, **overrides) -> Settings:
    return Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'hub.db').as_posix()}",
        media_dir=str(tmp_path / "media"),
        **overrides,
    )


async def _tables_after_startup(settings: Settings) -> set[str]:
    redis = FakeAsyncRedis()
    app = create_app(settings, redis=redis, configure_logging=False)
    async with app.router.lifespan_context(app):
        async with app.state.engine.connect() as connection:
            tables = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))
    await redis.aclose()
    return tables


async def test_startup_creates_the_schema_by_default(tmp_path) -> None:
    assert await _tables_after_startup(_settings(tmp_path)) == set(Base.metadata.tables)


async def test_startup_leaves_the_schema_to_the_migrate_job_when_disabled(tmp_path) -> None:
    """Several processes starting at once must not race on CREATE TABLE."""
    assert await _tables_after_startup(_settings(tmp_path, auto_create_schema=False)) == set()
