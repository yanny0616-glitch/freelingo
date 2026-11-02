from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class AIChannel(Base):
    """An administrator-managed AI provider connection.

    API keys are stored encrypted (see ``app.services.ai_channels``) and are
    never returned by the API.
    """

    __tablename__ = "ai_channels"
    __table_args__ = (
        CheckConstraint(
            "provider IN ('openai_compatible', 'openai', 'anthropic')",
            name="ck_ai_channels_provider",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    api_key_hint: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Text model used for chat, lessons, and every other LLM feature.
    # Empty means the channel is only used for speech.
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    reasoning_effort: Mapped[str | None] = mapped_column(String(16), nullable=True)
    max_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Higher priority channels are tried first; lower ones are failover targets.
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, onupdate=_utcnow
    )


class AISettings(Base):
    """Singleton AI settings: speech routing and learner model choice."""

    __tablename__ = "ai_settings"
    __table_args__ = (CheckConstraint("id = 1", name="ck_ai_settings_singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False, default=1)
    tts_channel_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("ai_channels.id", ondelete="SET NULL"), nullable=True
    )
    tts_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    tts_voice: Mapped[str | None] = mapped_column(String(200), nullable=True)
    tts_speed: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    # When true, learners may pick from the standard OpenAI voice list.
    tts_openai_voices: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    stt_channel_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("ai_channels.id", ondelete="SET NULL"), nullable=True
    )
    stt_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Administrators can always pick a channel per conversation; learners only when enabled.
    learner_model_switch: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, onupdate=_utcnow
    )
