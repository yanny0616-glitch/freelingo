from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator


class ChatRequest(BaseModel):
    message: str | None = Field(default=None, max_length=5000)
    conversation_id: int | None = None
    # Replace the last tutor reply instead of sending a new message.
    regenerate: bool = False
    # Replace this learner message (and everything after it) with `message`.
    edit_message_id: int | None = None
    # Preferred AI channel for a conversation created by this request.
    ai_channel_id: int | None = None

    @model_validator(mode="after")
    def check_mode(self) -> "ChatRequest":
        if self.regenerate:
            if self.conversation_id is None or self.edit_message_id is not None:
                raise ValueError("Regenerate requires a conversation and no edited message")
        elif not self.message:
            raise ValueError("Message must not be empty")
        if self.edit_message_id is not None and self.conversation_id is None:
            raise ValueError("Editing requires a conversation")
        return self


class ChatHistoryItem(BaseModel):
    role: str
    content: str
    id: int | None = None
    created_at: datetime | None = None
    modality: str | None = None


class ChatHistoryResponse(BaseModel):
    messages: list[ChatHistoryItem]


class ConversationCreate(BaseModel):
    title: str | None = None


class ConversationUpdate(BaseModel):
    """Fields omitted from the body are left unchanged; `ai_channel_id: null` resets it."""

    title: str | None = Field(default=None, max_length=200)
    ai_channel_id: int | None = None

    @field_validator("title")
    @classmethod
    def strip_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = " ".join(value.split())
        if not value:
            raise ValueError("Title must not be blank")
        return value


class ConversationResponse(BaseModel):
    id: int
    title: str
    source: str
    created_at: datetime
    updated_at: datetime
    ai_channel_id: int | None = None

    model_config = {"from_attributes": True}


class ChatModelOption(BaseModel):
    id: int
    name: str
    model: str


class ChatOptionsResponse(BaseModel):
    can_switch_model: bool
    models: list[ChatModelOption]
    study_plan_id: int | None = None


class ConversationTitleResponse(BaseModel):
    title: str
