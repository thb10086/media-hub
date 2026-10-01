"""Usage metering: one counter per tenant, UTC day and route."""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from hub.models import UsageDaily

POSTGRESQL_DIALECT = "postgresql"


async def record_usage(session: AsyncSession, *, tenant_id: str, endpoint: str, day: date) -> None:
    dialect = session.get_bind().dialect.name
    insert = postgresql_insert if dialect == POSTGRESQL_DIALECT else sqlite_insert
    statement = insert(UsageDaily).values(tenant_id=tenant_id, day=day, endpoint=endpoint, count=1)
    statement = statement.on_conflict_do_update(
        index_elements=[UsageDaily.tenant_id, UsageDaily.day, UsageDaily.endpoint],
        set_={"count": UsageDaily.count + 1},
    )
    await session.execute(statement)
    await session.commit()


async def usage_report(
    session: AsyncSession, *, tenant_id: str, start: date, end: date
) -> list[dict[str, Any]]:
    rows = await session.scalars(
        select(UsageDaily)
        .where(UsageDaily.tenant_id == tenant_id, UsageDaily.day >= start, UsageDaily.day <= end)
        .order_by(UsageDaily.day, UsageDaily.endpoint)
    )
    return [{"day": row.day.isoformat(), "endpoint": row.endpoint, "count": row.count} for row in rows]
