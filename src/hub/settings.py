"""Runtime configuration, read from ``HUB_*`` environment variables or ``.env``."""

from __future__ import annotations

from enum import Enum
from functools import lru_cache

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from hub.constants import DEFAULT_DOUYIN_USER_AGENT, SAU_PLATFORM_TYPES, Platform
from hub.services.ranking import HeatWeights

BYTES_PER_MB = 1024 * 1024


class ChannelsProviderName(str, Enum):
    NONE = "none"
    MOCK = "mock"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HUB_",
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    database_url: str = "sqlite+aiosqlite:///./data/hub.db"
    #: Create missing tables at startup. Turn off when several processes start
    #: together and a one-shot ``media-hub init-db`` job owns the schema.
    auto_create_schema: bool = True
    redis_url: str | None = None
    admin_token: SecretStr | None = None
    #: CIDRs of reverse proxies whose X-Forwarded-For header is trusted.
    trusted_proxies: list[str] = Field(default_factory=list)
    default_rate_limit_per_min: int = Field(default=120, ge=0)
    expose_docs: bool = True
    log_level: str = "INFO"

    dtk_base_url: str | None = None
    dtk_api_key: SecretStr | None = None
    dtk_wait_seconds: int = Field(default=20, ge=0, le=30)
    dtk_timeout_seconds: float = Field(default=45.0, gt=0)
    dtk_max_connections: int = Field(default=20, ge=1)
    dtk_max_keepalive: int = Field(default=10, ge=1)

    #: Cache TTLs (seconds).  0 disables caching for that operation.
    cache_hot_ttl: int = Field(default=300, ge=0, description="热榜缓存秒数")
    cache_search_ttl: int = Field(default=60, ge=0, description="搜索缓存秒数")
    cache_video_ttl: int = Field(default=1800, ge=0, description="视频详情缓存秒数")
    cache_comments_ttl: int = Field(default=120, ge=0, description="评论缓存秒数")
    cache_user_ttl: int = Field(default=900, ge=0, description="用户资料缓存秒数")

    sau_base_url: str | None = None
    #: Directory shared with social-auto-upload's ``videoFile`` volume. When set,
    #: videos are handed over through the filesystem instead of an HTTP upload.
    sau_video_dir: str | None = None
    sau_request_timeout_seconds: float = Field(default=30.0, gt=0)
    sau_publish_timeout_seconds: float = Field(default=1800.0, gt=0)
    sau_login_timeout_seconds: float = Field(default=240.0, gt=0)
    #: Platforms accounts can be bound to and published on. Any platform the
    #: publishing service can drive may be listed, e.g. ["douyin","channels","kuaishou"].
    publish_platforms: list[Platform] = Field(default_factory=lambda: [Platform.DOUYIN, Platform.CHANNELS])
    #: QR logins run one headless browser each; this caps them per gateway process.
    login_max_concurrency: int = Field(default=1, ge=1)
    #: How long a finished login session can still be read.
    login_session_retention_seconds: float = Field(default=600.0, gt=0)

    #: Import the login of every Douyin account bound by QR code into DTK's
    #: identity pool, so search, the hot board and downloads have an identity as
    #: soon as anyone has bound an account. Needs a DTK key with ``identity:manage``.
    #: Off: nothing new is imported, but removed accounts are still retired in DTK.
    data_identity_from_logins: bool = True
    #: The User-Agent SAU's browser logs in with; deploy/compose.yml hands the same
    #: value to SAU as SAU_USER_AGENT. DTK picks its TLS profile from it.
    douyin_user_agent: str = DEFAULT_DOUYIN_USER_AGENT
    douyin_language: str = "zh-CN"
    douyin_timezone: str = "Asia/Shanghai"
    #: How often the worker imports accounts still missing an identity.
    data_identity_sync_seconds: float = Field(default=300.0, gt=0)
    #: Wait after a failed import, growing with each failure (up to six times).
    data_identity_retry_seconds: float = Field(default=1800.0, gt=0)

    channels_provider: ChannelsProviderName = ChannelsProviderName.NONE

    media_dir: str = "./data/media"
    max_upload_mb: int = Field(default=500, gt=0)
    remote_fetch_timeout_seconds: float = Field(default=300.0, gt=0)
    #: Allow video_url / callback_url to point at private addresses.
    allow_private_urls: bool = False
    #: Proxy for requests to the outside world (callbacks, video_url downloads).
    #: Environment proxy variables are deliberately ignored.
    outbound_proxy: str | None = None

    heat_like_weight: float = 1.0
    heat_comment_weight: float = 3.0
    heat_share_weight: float = 5.0
    heat_collect_weight: float = 4.0
    heat_play_weight: float = 0.0
    heat_half_life_hours: float = 72.0

    worker_concurrency: int = Field(default=1, ge=1)
    worker_poll_seconds: float = Field(default=2.0, gt=0)
    worker_stale_after_seconds: float = Field(default=3600.0, gt=0)

    webhook_secret: SecretStr | None = None
    webhook_timeout_seconds: float = Field(default=10.0, gt=0)
    webhook_max_attempts: int = Field(default=3, ge=1)

    @field_validator("publish_platforms")
    @classmethod
    def _drivable(cls, value: list[Platform]) -> list[Platform]:
        undrivable = [platform.value for platform in value if platform not in SAU_PLATFORM_TYPES]
        if undrivable:
            raise ValueError(f"no publishing driver for: {', '.join(undrivable)}")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> Settings:
        if self.dtk_timeout_seconds <= self.dtk_wait_seconds:
            raise ValueError("HUB_DTK_TIMEOUT_SECONDS must exceed HUB_DTK_WAIT_SECONDS")
        if self.dtk_base_url and self.dtk_api_key is None:
            raise ValueError("HUB_DTK_API_KEY is required when HUB_DTK_BASE_URL is set")
        return self

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * BYTES_PER_MB

    @property
    def heat_weights(self) -> HeatWeights:
        return HeatWeights(
            like=self.heat_like_weight,
            comment=self.heat_comment_weight,
            share=self.heat_share_weight,
            collect=self.heat_collect_weight,
            play=self.heat_play_weight,
            half_life_hours=self.heat_half_life_hours,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
