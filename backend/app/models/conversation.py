from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="New conversation")
    source: Mapped[str] = mapped_column(String(10), nullable=False, default="chat")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC).replace(tzinfo=None)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC).replace(tzinfo=None)
    )
    study_plan_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("study_plans.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    target_language: Mapped[str | None] = mapped_column(String(10), nullable=True, index=True)
    # Preferred AI channel for this conversation; other channels remain failover targets.
    ai_channel_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("ai_channels.id", ondelete="SET NULL"), nullable=True
    )

    def __repr__(self) -> str:
        return f"<Conversation(id={self.id}, user_id={self.user_id}, source={self.source}, target_language={self.target_language})>"
