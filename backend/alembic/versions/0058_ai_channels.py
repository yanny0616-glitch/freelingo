"""Add administrator-managed AI channels and speech routing.

Revision ID: 0058_ai_channels
Revises: 0057_flashcard_review_receipts
"""

import sqlalchemy as sa

from alembic import op

revision = "0058_ai_channels"
down_revision = "0057_flashcard_review_receipts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_channels",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("base_url", sa.String(500), nullable=True),
        sa.Column("api_key_encrypted", sa.Text(), nullable=True),
        sa.Column("api_key_hint", sa.String(16), nullable=True),
        sa.Column("model", sa.String(200), nullable=True),
        sa.Column("reasoning_effort", sa.String(16), nullable=True),
        sa.Column("max_tokens", sa.Integer(), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "provider IN ('openai_compatible', 'openai', 'anthropic')",
            name="ck_ai_channels_provider",
        ),
    )
    op.create_table(
        "ai_speech_settings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column(
            "tts_channel_id",
            sa.Integer(),
            sa.ForeignKey("ai_channels.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("tts_model", sa.String(200), nullable=True),
        sa.Column("tts_voice", sa.String(200), nullable=True),
        sa.Column("tts_speed", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("tts_openai_voices", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "stt_channel_id",
            sa.Integer(),
            sa.ForeignKey("ai_channels.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("stt_model", sa.String(200), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_ai_speech_settings_singleton"),
    )


def downgrade() -> None:
    op.drop_table("ai_speech_settings")
    op.drop_table("ai_channels")
