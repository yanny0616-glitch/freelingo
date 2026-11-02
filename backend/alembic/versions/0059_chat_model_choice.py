"""Generalize AI settings and let conversations choose an AI channel.

Revision ID: 0059_chat_model_choice
Revises: 0058_ai_channels
"""

import sqlalchemy as sa

from alembic import op

revision = "0059_chat_model_choice"
down_revision = "0058_ai_channels"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.rename_table("ai_speech_settings", "ai_settings")
    op.execute(
        "ALTER TABLE ai_settings RENAME CONSTRAINT ck_ai_speech_settings_singleton "
        "TO ck_ai_settings_singleton"
    )
    op.add_column(
        "ai_settings",
        sa.Column(
            "learner_model_switch", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column(
        "conversations",
        sa.Column(
            "ai_channel_id",
            sa.Integer(),
            sa.ForeignKey(
                "ai_channels.id", ondelete="SET NULL", name="fk_conversations_ai_channel_id"
            ),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_constraint("fk_conversations_ai_channel_id", "conversations", type_="foreignkey")
    op.drop_column("conversations", "ai_channel_id")
    op.drop_column("ai_settings", "learner_model_switch")
    op.execute(
        "ALTER TABLE ai_settings RENAME CONSTRAINT ck_ai_settings_singleton "
        "TO ck_ai_speech_settings_singleton"
    )
    op.rename_table("ai_settings", "ai_speech_settings")
