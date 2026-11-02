"""Chat client features: search, rename, titles, edit/regenerate, stop, and model choice."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.main import app
from app.models.ai_channel import AIChannel
from app.models.chat_history import ChatHistory
from app.models.conversation import Conversation
from app.routers import chat as chat_router
from app.services.ai_channels import RuntimeSnapshot, TextChannel
from tests.conftest import make_study_plan


@asynccontextmanager
async def same_session(session):
    yield session


@pytest.fixture
async def runtime(db_session):
    """Install an AI runtime with two text channels and restore the previous one."""
    for channel_id, name in ((7, "Fast"), (8, "Smart")):
        db_session.add(
            AIChannel(id=channel_id, name=name, provider="openai", model=f"{name.lower()}-1")
        )
    await db_session.commit()
    had = hasattr(app.state, "ai_runtime")
    previous = getattr(app.state, "ai_runtime", None)

    def install(*, learners: bool = False):
        app.state.ai_runtime = RuntimeSnapshot(
            llm_channels=("Fast", "Smart"),
            tts_channel=None,
            stt_channel=None,
            text_channels=(TextChannel(7, "Fast", "fast-1"), TextChannel(8, "Smart", "smart-1")),
            learner_model_switch=learners,
        )

    install()
    yield install
    if had:
        app.state.ai_runtime = previous
    elif hasattr(app.state, "ai_runtime"):
        del app.state.ai_runtime


async def plan_for(db_session, user):
    return await make_study_plan(
        db_session,
        user_id=user.id,
        cefr_level="A2",
        target_language="en-US",
        goals=["speaking"],
        duration_weeks=4,
        days_per_week=4,
        current_unit="",
        generated_plan={},
        is_active=True,
    )


async def conversation_with(db_session, user, *turns, title="Chat"):
    conv = Conversation(user_id=user.id, title=title, target_language="en-US")
    db_session.add(conv)
    await db_session.flush()
    previous = None
    for role, content in turns:
        row = ChatHistory(
            user_id=user.id,
            conversation_id=conv.id,
            role=role,
            content=content,
            target_language="en-US",
            modality="chat",
            reply_to_id=previous.id if role == "assistant" and previous else None,
        )
        db_session.add(row)
        await db_session.flush()
        previous = row
    await db_session.commit()
    return conv


def text_stream(*tokens):
    async def generate():
        for token in tokens:
            yield token

    return generate()


def events(body: str) -> list[dict]:
    return [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]


async def post_chat(client, headers, db_session, body, *tokens, chat_mock=None):
    mock = chat_mock or AsyncMock(return_value=text_stream(*tokens))
    with (
        patch("app.routers.chat.llm_adapter.chat", mock),
        patch("app.routers.chat.db_session", side_effect=lambda: same_session(db_session)),
    ):
        response = await client.post("/api/chat", headers=headers, json=body)
    return response, mock


async def history(db_session, conv_id):
    db_session.expire_all()
    rows = await db_session.scalars(
        select(ChatHistory).where(ChatHistory.conversation_id == conv_id).order_by(ChatHistory.id)
    )
    return [(row.role, row.content) for row in rows]


async def test_search_matches_titles_and_messages(client, test_user, db_session, user_language):
    user, headers = test_user
    await conversation_with(db_session, user, ("user", "I love pizza"), title="Food")
    await conversation_with(db_session, user, ("user", "Job interview tips"), title="Work")
    await conversation_with(db_session, user, ("user", "100% sure"), title="Percent")

    async def titles(q):
        response = await client.get("/api/chat/conversations", headers=headers, params={"q": q})
        return sorted(item["title"] for item in response.json())

    assert await titles("PIZZA") == ["Food"]
    assert await titles("work") == ["Work"]
    assert await titles("%") == ["Percent"]
    assert await titles("") == ["Food", "Percent", "Work"]


async def test_rename_conversation(client, test_user, db_session, user_language):
    user, headers = test_user
    conv = await conversation_with(db_session, user)
    response = await client.patch(
        f"/api/chat/conversations/{conv.id}", headers=headers, json={"title": "  Trip   plans "}
    )
    assert response.status_code == 200
    assert response.json()["title"] == "Trip plans"
    blank = await client.patch(
        f"/api/chat/conversations/{conv.id}", headers=headers, json={"title": "   "}
    )
    assert blank.status_code == 422


async def test_messages_include_ids_and_latest_window(client, test_user, db_session, user_language):
    user, headers = test_user
    turns = [("user" if i % 2 == 0 else "assistant", f"m{i}") for i in range(205)]
    conv = await conversation_with(db_session, user, *turns)
    response = await client.get(f"/api/chat/conversations/{conv.id}/messages", headers=headers)
    messages = response.json()["messages"]
    assert len(messages) == 200
    assert messages[0]["content"] == "m5" and messages[-1]["content"] == "m204"
    assert all(item["id"] and item["created_at"] for item in messages)


async def test_generate_title_uses_native_language(client, test_user, db_session, user_language):
    user, headers = test_user
    conv = await conversation_with(
        db_session, user, ("user", "Can we practise ordering coffee?"), ("assistant", "Sure!")
    )
    mock = AsyncMock(return_value='"Ordering coffee practice."')
    with patch("app.routers.chat.llm_adapter.chat", mock):
        response = await client.post(f"/api/chat/conversations/{conv.id}/title", headers=headers)
    assert response.json() == {"title": "Ordering coffee practice."}
    prompt = mock.await_args.args[0]
    assert "ordering coffee" in prompt[1]["content"]


async def test_title_failure_keeps_existing_title(client, test_user, db_session, user_language):
    from app.services.llm_adapter import LLMError

    user, headers = test_user
    conv = await conversation_with(db_session, user, ("user", "Hello"), title="Hello")
    with patch("app.routers.chat.llm_adapter.chat", AsyncMock(side_effect=LLMError("down"))):
        response = await client.post(f"/api/chat/conversations/{conv.id}/title", headers=headers)
    assert response.json() == {"title": "Hello"}


async def test_regenerate_replaces_last_reply(client, test_user, db_session, user_language):
    user, headers = test_user
    await plan_for(db_session, user)
    conv = await conversation_with(
        db_session, user, ("user", "Hi"), ("assistant", "Hello!"), ("user", "Bye"), ("assistant", "See ya")
    )
    response, mock = await post_chat(
        client, headers, db_session, {"conversation_id": conv.id, "regenerate": True}, "Goodbye!"
    )
    assert response.status_code == 200
    data = events(response.text)
    assert data[-1]["done"] is True and data[-1]["message_id"]
    assert await history(db_session, conv.id) == [
        ("user", "Hi"),
        ("assistant", "Hello!"),
        ("user", "Bye"),
        ("assistant", "Goodbye!"),
    ]
    sent = mock.await_args.args[0]
    assert sent[-1] == {"role": "user", "content": "Bye"}


async def test_edit_replaces_message_and_following_turns(
    client, test_user, db_session, user_language
):
    user, headers = test_user
    await plan_for(db_session, user)
    conv = await conversation_with(
        db_session, user, ("user", "Hi"), ("assistant", "Hello!"), ("user", "I goed"), ("assistant", "Went")
    )
    rows = await db_session.scalars(
        select(ChatHistory).where(ChatHistory.content == "I goed")
    )
    edited_id = rows.one().id
    response, _ = await post_chat(
        client,
        headers,
        db_session,
        {"conversation_id": conv.id, "edit_message_id": edited_id, "message": "I went"},
        "Great!",
    )
    assert response.status_code == 200
    start = events(response.text)[0]
    assert start["conversation_id"] == conv.id and start["user_message_id"]
    assert await history(db_session, conv.id) == [
        ("user", "Hi"),
        ("assistant", "Hello!"),
        ("user", "I went"),
        ("assistant", "Great!"),
    ]


async def test_edit_rejects_tutor_and_foreign_messages(
    client, test_user, db_session, user_language
):
    user, headers = test_user
    await plan_for(db_session, user)
    conv = await conversation_with(db_session, user, ("user", "Hi"), ("assistant", "Hello!"))
    reply = (
        await db_session.scalars(select(ChatHistory).where(ChatHistory.role == "assistant"))
    ).one()
    response, mock = await post_chat(
        client,
        headers,
        db_session,
        {"conversation_id": conv.id, "edit_message_id": reply.id, "message": "x"},
    )
    assert response.status_code == 404
    mock.assert_not_awaited()


@pytest.mark.parametrize(
    "body",
    [
        {"regenerate": True},
        {"message": ""},
        {"message": "x", "edit_message_id": 1},
    ],
)
async def test_invalid_chat_modes(client, test_user, body):
    _, headers = test_user
    response = await client.post("/api/chat", headers=headers, json=body)
    assert response.status_code == 422


async def test_regenerate_without_messages_conflicts(client, test_user, db_session, user_language):
    user, headers = test_user
    await plan_for(db_session, user)
    conv = await conversation_with(db_session, user)
    response, _ = await post_chat(
        client, headers, db_session, {"conversation_id": conv.id, "regenerate": True}
    )
    assert response.status_code == 409


async def test_partial_reply_is_saved_when_stream_is_cancelled(db_session, test_user):
    user, _ = test_user
    conv = await conversation_with(db_session, user, ("user", "Tell me a story"))
    prompt = (await db_session.scalars(select(ChatHistory))).one()
    with patch("app.routers.chat.db_session", side_effect=lambda: same_session(db_session)):
        await chat_router._save_partial_reply(
            user_id=user.id,
            conversation_id=conv.id,
            reply_to_id=prompt.id,
            content="Once upon",
            study_plan_id=None,
            target_language="en-US",
        )
    assert await history(db_session, conv.id) == [
        ("user", "Tell me a story"),
        ("assistant", "Once upon"),
    ]


async def test_options_for_learners_and_admins(
    client, test_user, admin_user, db_session, runtime, user_language
):
    _, headers = test_user
    learner = (await client.get("/api/chat/options", headers=headers)).json()
    assert learner["can_switch_model"] is False and learner["models"] == []

    runtime(learners=True)
    learner = (await client.get("/api/chat/options", headers=headers)).json()
    assert [item["name"] for item in learner["models"]] == ["Fast", "Smart"]

    runtime(learners=False)
    _, admin_headers = admin_user
    admin = (await client.get("/api/chat/options", headers=admin_headers)).json()
    assert admin["can_switch_model"] is True
    assert admin["models"][1] == {"id": 8, "name": "Smart", "model": "smart-1"}


async def test_model_choice_permissions(client, test_user, db_session, runtime, user_language):
    user, headers = test_user
    conv = await conversation_with(db_session, user)
    url = f"/api/chat/conversations/{conv.id}"
    assert (await client.patch(url, headers=headers, json={"ai_channel_id": 8})).status_code == 403

    runtime(learners=True)
    assert (await client.patch(url, headers=headers, json={"ai_channel_id": 99})).status_code == 422
    chosen = await client.patch(url, headers=headers, json={"ai_channel_id": 8})
    assert chosen.json()["ai_channel_id"] == 8
    reset = await client.patch(url, headers=headers, json={"ai_channel_id": None})
    assert reset.json()["ai_channel_id"] is None


async def test_chosen_model_is_preferred(client, test_user, db_session, runtime, user_language):
    user, headers = test_user
    runtime(learners=True)
    await plan_for(db_session, user)
    response, mock = await post_chat(
        client, headers, db_session, {"message": "Hi", "ai_channel_id": 8}, "Hello"
    )
    assert response.status_code == 200
    assert mock.await_args.kwargs["preferred_channel_id"] == 8
    conv = await db_session.scalar(select(Conversation))
    assert conv.ai_channel_id == 8

    # Without permission, the stored choice is ignored rather than failing the chat.
    runtime(learners=False)
    response, mock = await post_chat(
        client, headers, db_session, {"message": "Again", "conversation_id": conv.id}, "Hi"
    )
    assert response.status_code == 200
    assert "preferred_channel_id" not in mock.await_args.kwargs


async def test_router_prefers_chosen_channel(monkeypatch):
    from app.services.llm_adapter import LLMChannelConfig, llm_adapter

    saved = llm_adapter._channels
    try:
        llm_adapter.set_channels(
            [
                LLMChannelConfig(
                    provider="openai_compatible",
                    model="m",
                    base_url="http://x",
                    name=name,
                    channel_id=channel_id,
                )
                for name, channel_id in (("A", 1), ("B", 2), ("C", 3))
            ]
        )
        assert [a.name for a in llm_adapter.adapters(2)] == ["B", "A", "C"]
        assert [a.name for a in llm_adapter.adapters(99)] == ["A", "B", "C"]
        assert llm_adapter.has_channel(3) and not llm_adapter.has_channel(4)
    finally:
        llm_adapter._channels = saved


def test_clean_title():
    assert chat_router._clean_title('  "Coffee   order"  ') == "Coffee order"
    assert chat_router._clean_title("Title: Travel") == "Travel"
    assert chat_router._clean_title("「点咖啡」") == "点咖啡"
    assert len(chat_router._clean_title("x" * 100)) == 60


