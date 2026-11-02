"""Reject oversized input before persistence; exercise real structured-output validation."""

import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.models.flashcard import Flashcard
from app.services.llm_adapter import LLMAdapter
from tests.conftest import make_study_plan


@pytest.fixture
async def plan(db_session, test_user):
    plan = await make_study_plan(
        db_session, user_id=test_user[0].id, cefr_level="A1", is_active=True
    )
    await db_session.commit()
    return plan


def content(**changes):
    return {
        "word": "hello",
        "definition": "a greeting",
        "example_sentence": "Hello!",
        "translation": "hola",
        **changes,
    }


@pytest.mark.parametrize("endpoint", ["", "/bulk"])
@pytest.mark.parametrize("field,limit", [("word", 100), ("translation", 255)])
async def test_oversized_content_rejects_the_whole_request_before_writing(
    client, test_user, db_session, plan, endpoint, field, limit
):
    invalid = content(**{field: "界" * (limit + 1)})
    body = (
        invalid
        if not endpoint
        else {
            "study_plan_id": plan.id,
            "target_language": plan.target_language,
            "flashcards": [content(word="valid"), invalid],
        }
    )
    response = await client.post(f"/api/flashcards{endpoint}", headers=test_user[1], json=body)
    assert response.status_code == 422
    assert any(error["loc"][-1] == field for error in response.json()["detail"])
    assert await db_session.scalar(select(func.count()).select_from(Flashcard)) == 0


@pytest.mark.parametrize("endpoint", ["", "/bulk"])
async def test_storage_boundary_accepts_unicode_without_truncation(
    client, test_user, db_session, plan, endpoint
):
    card = content(word="界" * 100, translation="🙂" * 255)
    body = (
        card
        if not endpoint
        else {
            "study_plan_id": plan.id,
            "target_language": plan.target_language,
            "flashcards": [card],
        }
    )
    response = await client.post(f"/api/flashcards{endpoint}", headers=test_user[1], json=body)
    assert response.status_code == 200
    saved = await db_session.scalar(select(Flashcard))
    assert saved.word == card["word"] and saved.translation == card["translation"]


async def test_selected_word_is_validated_before_llm_call(client, test_user, plan, monkeypatch):
    call = AsyncMock()
    monkeypatch.setattr(LLMAdapter, "_call_with_retry", call)
    response = await client.post(
        "/api/flashcards/from-word", headers=test_user[1], json={"word": "a" * 101}
    )
    assert response.status_code == 422
    call.assert_not_awaited()


@pytest.mark.parametrize("endpoint", ["/generate", "/from-word"])
@pytest.mark.parametrize("field,limit", [("word", 100), ("translation", 255)])
@pytest.mark.parametrize("recovers", [True, False])
async def test_invalid_model_content_uses_existing_retry_and_never_partially_persists(
    client, test_user, db_session, plan, monkeypatch, endpoint, field, limit, recovers
):
    invalid = content(**{field: "x" * (limit + 1)})
    valid = content()

    def output(card):
        # Valid preceding content must not be committed if the second item is invalid.
        return json.dumps(
            {"flashcards": [content(word="first"), card]} if endpoint == "/generate" else card
        )

    call = AsyncMock(side_effect=[output(invalid), output(valid if recovers else invalid)])
    monkeypatch.setattr(LLMAdapter, "_call_with_retry", call)
    body = {"topic": "greetings", "count": 2} if endpoint == "/generate" else {"word": "hello"}
    response = await client.post(f"/api/flashcards{endpoint}", headers=test_user[1], json=body)
    assert call.await_count == 2
    if recovers:
        assert response.status_code == 200
        cards = (await db_session.scalars(select(Flashcard))).all()
        assert len(cards) == (2 if endpoint == "/generate" else 1)
        assert any(card.word == "hello" and card.translation == "hola" for card in cards)
    else:
        assert response.status_code == 502
        assert response.json()["detail"] == "ai_service_error"
        assert await db_session.scalar(select(func.count()).select_from(Flashcard)) == 0


@pytest.mark.parametrize("word", ["", " \u3000 ", "(noun)", '""', "'  '"])
@pytest.mark.parametrize("recovers", [True, False])
async def test_lookup_validates_cleaned_word_inside_model_retry(
    client, test_user, db_session, plan, monkeypatch, word, recovers
):
    invalid = json.dumps(content(word=word))
    call = AsyncMock(
        side_effect=[invalid, json.dumps(content(word="  book (noun)  ")) if recovers else invalid]
    )
    monkeypatch.setattr(LLMAdapter, "_call_with_retry", call)
    response = await client.post(
        "/api/flashcards/from-word", headers=test_user[1], json={"word": "book"}
    )
    assert call.await_count == 2
    if recovers:
        assert response.status_code == 200
        assert response.json()["word"] == "book"
    else:
        assert response.status_code == 502
        assert response.json()["detail"] == "ai_service_error"
    from sqlalchemy.ext.asyncio import AsyncSession

    async with AsyncSession(bind=db_session.bind) as persisted:
        cards = (await persisted.scalars(select(Flashcard))).all()
        assert len(cards) == int(recovers)
        assert all(card.word.strip() and card.source == "from_text" for card in cards)


async def test_generation_keeps_empty_and_duplicate_counters(
    client, test_user, db_session, plan, monkeypatch
):
    call = AsyncMock(
        return_value=json.dumps(
            {
                "flashcards": [
                    content(word="(noun)"),
                    content(word="book"),
                    content(word="book (noun)"),
                ]
            }
        )
    )
    monkeypatch.setattr(LLMAdapter, "_call_with_retry", call)
    response = await client.post(
        "/api/flashcards/generate", headers=test_user[1], json={"topic": "objects", "count": 3}
    )
    assert response.status_code == 200
    result = response.json()
    assert (result["created"], result["skipped_empty"], result["skipped_duplicates"]) == (1, 1, 1)
    assert [card["word"] for card in result["flashcards"]] == ["book"]
    call.assert_awaited_once()
    assert await db_session.scalar(select(func.count()).select_from(Flashcard)) == 1
