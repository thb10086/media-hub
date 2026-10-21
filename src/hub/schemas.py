"""Request bodies shared by the API and the worker."""

from __future__ import annotations

from datetime import datetime
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from hub.constants import Platform, Scope
from hub.platforms import supports_draft

HOURS_PER_DAY: Final = 24
MAX_URL_LENGTH: Final = 2048


class PublishSchedule(BaseModel):
    """Publish on day ``start_days + 1`` at the first of ``daily_times`` (hours, 0-23)."""

    model_config = ConfigDict(extra="forbid")

    daily_times: list[int] = Field(min_length=1, max_length=HOURS_PER_DAY)
    start_days: int = Field(default=0, ge=0, le=30)

    @field_validator("daily_times")
    @classmethod
    def _hours(cls, value: list[int]) -> list[int]:
        if any(hour < 0 or hour >= HOURS_PER_DAY for hour in value):
            raise ValueError("daily_times are hours of the day, 0-23")
        return value


class PublishVideoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: Platform
    account_id: str = Field(min_length=1, max_length=64)
    media_id: str | None = Field(default=None, max_length=64)
    video_url: str | None = Field(default=None, max_length=MAX_URL_LENGTH)
    title: str = Field(min_length=1, max_length=1000)
    #: The post's body text (Douyin's 作品描述, before the tags). Platforms without one ignore it.
    description: str | None = Field(default=None, max_length=1000)
    #: A portrait cover image (JPG/PNG/WEBP) the worker downloads; Douyin only. A cover that cannot be
    #: fetched or set is skipped and the video is published with the platform's own cover.
    cover_url: str | None = Field(default=None, max_length=MAX_URL_LENGTH)
    tags: list[str] = Field(default_factory=list, max_length=20)
    schedule: PublishSchedule | None = None
    draft: bool = False
    callback_url: str | None = Field(default=None, max_length=MAX_URL_LENGTH)

    @model_validator(mode="after")
    def _consistent(self) -> PublishVideoRequest:
        if (self.media_id is None) == (self.video_url is None):
            raise ValueError("provide exactly one of media_id and video_url")
        if self.draft and not supports_draft(self.platform):
            raise ValueError(f"draft is not supported for {self.platform.value}")
        return self


class LoginSessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: Platform
    name: str = Field(min_length=1, max_length=64)


class LoginVerificationSubmit(BaseModel):
    """The owner's answer to the platform's identity check: the SMS code, or that they sent the SMS."""

    model_config = ConfigDict(extra="forbid")

    code: str | None = Field(
        default=None, pattern=r"^\d{4,8}$", description="method 为 sms 时：平台发到账号手机上的短信验证码"
    )
    sent: bool = Field(default=False, description="method 为 sms_send 时：用户已按平台要求发出短信")
    resend: bool = Field(default=False, description="method 为 sms 时：没收到验证码，请平台重新发送")

    @model_validator(mode="after")
    def _one_answer(self) -> LoginVerificationSubmit:
        if [self.code is not None, self.sent, self.resend].count(True) != 1:
            raise ValueError("send exactly one of code, sent=true or resend=true")
        return self


class DownloadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str | None = Field(default=None, max_length=MAX_URL_LENGTH)
    aweme_id: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _one_target(self) -> DownloadRequest:
        if (self.url is None) == (self.aweme_id is None):
            raise ValueError("provide exactly one of url and aweme_id")
        return self


class ParseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=4096)


class TenantCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    rate_limit_per_min: int | None = Field(default=None, ge=0)


class KeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    scopes: list[Scope] = Field(default_factory=lambda: list(Scope))
    ip_allowlist: list[str] = Field(default_factory=list)
    rate_limit_per_min: int | None = Field(default=None, ge=0)
    expires_at: datetime | None = None
