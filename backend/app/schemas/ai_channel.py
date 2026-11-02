from __future__ import annotations

from datetime import datetime
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

AIChannelProvider = Literal["openai_compatible", "openai", "anthropic"]
ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _clean_base_url(value: str | None) -> str | None:
    value = _clean_optional(value)
    if value is None:
        return None
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Base URL must start with http:// or https://")
    return value.rstrip("/")


class AIChannelFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    provider: AIChannelProvider
    base_url: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, max_length=200)
    reasoning_effort: ReasoningEffort | None = None
    max_tokens: int | None = Field(default=None, ge=1, le=200000)
    priority: int = Field(default=0, ge=-1000, le=1000)
    is_enabled: bool = True

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Name must not be blank")
        return value

    @field_validator("model")
    @classmethod
    def strip_model(cls, value: str | None) -> str | None:
        return _clean_optional(value)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str | None) -> str | None:
        return _clean_base_url(value)

    @model_validator(mode="after")
    def check_provider_fields(self) -> AIChannelFields:
        if self.provider == "openai_compatible" and not self.base_url:
            raise ValueError("OpenAI-compatible channels require a base URL")
        if self.provider != "openai":
            self.reasoning_effort = None
        if self.provider != "anthropic":
            self.max_tokens = None
        return self


class AIChannelCreate(AIChannelFields):
    api_key: str | None = Field(default=None, max_length=4000)


class AIChannelUpdate(AIChannelFields):
    # Omitted or empty keeps the stored key; clear_api_key removes it.
    api_key: str | None = Field(default=None, max_length=4000)
    clear_api_key: bool = False


class AIChannelResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    provider: AIChannelProvider
    base_url: str | None
    model: str | None
    reasoning_effort: str | None
    max_tokens: int | None
    priority: int
    is_enabled: bool
    has_api_key: bool
    api_key_hint: str | None
    api_key_unreadable: bool
    created_at: datetime
    updated_at: datetime


class AISpeechSettingsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    tts_channel_id: int | None = None
    tts_model: str | None = Field(default=None, max_length=200)
    tts_voice: str | None = Field(default=None, max_length=200)
    tts_speed: float = Field(default=1.0, ge=0.25, le=4.0)
    tts_openai_voices: bool = True
    stt_channel_id: int | None = None
    stt_model: str | None = Field(default=None, max_length=200)

    @field_validator("tts_model", "tts_voice", "stt_model")
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        return _clean_optional(value)


class AIChatSettingsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    learner_model_switch: bool = False


class AIRuntimeStatus(BaseModel):
    text_source: Literal["channels", "environment"]
    text_channels: list[str]
    environment_provider: str
    environment_model: str
    tts_source: Literal["channel", "environment"]
    tts_channel: str | None
    stt_source: Literal["channel", "environment"]
    stt_channel: str | None


class AIConfigResponse(BaseModel):
    channels: list[AIChannelResponse]
    speech: AISpeechSettingsPayload
    chat: AIChatSettingsPayload
    runtime: AIRuntimeStatus


class AIChannelProbeRequest(BaseModel):
    """Connection details from the edit form; a stored key is reused when omitted."""

    model_config = ConfigDict(extra="forbid")

    channel_id: int | None = None
    provider: AIChannelProvider
    base_url: str | None = Field(default=None, max_length=500)
    api_key: str | None = Field(default=None, max_length=4000)
    model: str | None = Field(default=None, max_length=200)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str | None) -> str | None:
        return _clean_base_url(value)

    @field_validator("model", "api_key")
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        return _clean_optional(value)


class AIChannelTestResponse(BaseModel):
    ok: bool
    latency_ms: int
    reply: str | None = None
    error: str | None = None


class AIChannelModelsResponse(BaseModel):
    ok: bool
    models: list[str] = []
    error: str | None = None
