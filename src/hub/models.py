"""Persistent business data: tenants, keys, accounts, media, tasks, usage.

Identifiers are UUID strings and JSON columns use the portable ``JSON`` type
(JSONB on PostgreSQL), so the same models run on PostgreSQL in production and
SQLite in tests.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from hub.constants import AccountStatus, KeyStatus, TaskStatus, TenantStatus

JsonType = JSON().with_variant(JSONB(), "postgresql")
ID_LENGTH = 36


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite hands datetimes back naive; every stored value is UTC."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    status: Mapped[str] = mapped_column(String(16), default=TenantStatus.ACTIVE.value)
    rate_limit_per_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    webhook_secret: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    prefix: Mapped[str] = mapped_column(String(32), unique=True)
    key_hash: Mapped[str] = mapped_column(String(64))
    scopes: Mapped[list[str]] = mapped_column(JsonType, default=list)
    ip_allowlist: Mapped[list[str]] = mapped_column(JsonType, default=list)
    status: Mapped[str] = mapped_column(String(16), default=KeyStatus.ACTIVE.value)
    rate_limit_per_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PlatformAccount(Base):
    """A publishing account owned by one tenant, stored in social-auto-upload."""

    __tablename__ = "platform_accounts"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    platform: Mapped[str] = mapped_column(String(32))
    display_name: Mapped[str] = mapped_column(String(128))
    sau_account_id: Mapped[int] = mapped_column(Integer)
    sau_user_name: Mapped[str] = mapped_column(String(64), unique=True)
    sau_cookie_file: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), default=AccountStatus.ACTIVE.value)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MediaFile(Base):
    __tablename__ = "media_files"

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    original_name: Mapped[str] = mapped_column(String(255))
    stored_name: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_tasks_tenant_idempotency"),
        Index("ix_tasks_claim", "status", "kind", "run_after"),
    )

    id: Mapped[str] = mapped_column(String(ID_LENGTH), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default=TaskStatus.QUEUED.value)
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    callback_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UsageDaily(Base):
    """Successful calls per tenant, day and route: the basis for billing."""

    __tablename__ = "usage_daily"

    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    endpoint: Mapped[str] = mapped_column(String(128), primary_key=True)
    count: Mapped[int] = mapped_column(BigInteger, default=0)
