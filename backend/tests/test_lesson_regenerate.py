"""POST /api/lessons/{id}/regenerate replaces untouched lessons."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models.lesson import Exercise, Lesson
from tests.conftest import make_study_plan

NEW_CONTENT = {
    "lesson_type": "grammar",
    "title": "Fresh lesson",
    "explanation": {"text": "Nuevo"},
    "native_explanation": {"text": "新的讲解"},
    "exercises": [
        {
            "type": "multiple_choice",
            "question": "Pick one",
            "options": ["a", "b"],
            "correct": "a",
            "explanation": "Because.",
        },
        {"type": "fill_blank", "question": "I ___ here.", "correct": "am"},
    ],
}


async def _lesson(db_session, user_id, *, unit_id="a1-unit-1", answered=False, completed=False):
    plan = await make_study_plan(
        db_session,
        user_id=user_id,
        cefr_level="A1",
        goals=["grammar"],
        duration_weeks=4,
        days_per_week=4,
        current_unit="",
        generated_plan={},
        is_active=True,
    )
    sibling = Lesson(
        study_plan_id=plan.id,
        title="Earlier lesson",
        lesson_type="vocabulary",
        cefr_level="A1",
        week_number=1,
        day_number=1,
        unit_id=unit_id,
        content={"title": "Earlier"},
    )
    lesson = Lesson(
        study_plan_id=plan.id,
        title="Greetings",
        lesson_type="grammar",
        cefr_level="A1",
        week_number=1,
        day_number=2,
        unit_id=unit_id,
        content={"title": "Old lesson"},
        is_completed=completed,
    )
    db_session.add_all([sibling, lesson])
    await db_session.flush()
    db_session.add(
        Exercise(
            lesson_id=lesson.id,
            exercise_type="fill_blank",
            question="Old ___",
            correct_answer="old",
            user_answer="old" if answered else None,
            answered_at=datetime(2026, 1, 1) if answered else None,
        )
    )
    await db_session.commit()
    await db_session.refresh(lesson)
    return lesson


@pytest.mark.asyncio
async def test_regenerate_replaces_content_and_exercises(client, test_user, db_session):
    user, headers = test_user
    native_language = user.native_language
    lesson = await _lesson(db_session, user.id)
    lesson_id = lesson.id

    with patch(
        "app.routers.lessons.generate_lesson", new=AsyncMock(return_value=NEW_CONTENT)
    ) as generate:
        response = await client.post(f"/api/lessons/{lesson_id}/regenerate", headers=headers)

    assert response.status_code == 200
    assert response.json()["content"]["title"] == "Fresh lesson"
    kwargs = generate.await_args.kwargs
    assert kwargs["topic"] == "Greetings"
    assert kwargs["week"] == 1 and kwargs["day"] == 2
    assert kwargs["native_language"] == native_language
    assert [item["title"] for item in kwargs["previous_lessons"]] == ["Earlier lesson"]

    db_session.expire_all()
    exercises = (
        await db_session.scalars(
            select(Exercise).where(Exercise.lesson_id == lesson_id).order_by(Exercise.id)
        )
    ).all()
    assert [item.question for item in exercises] == ["Pick one", "I ___ here."]
    assert exercises[0].options == ["a", "b"]
    assert exercises[1].correct_answer == "am"
    refreshed = await db_session.get(Lesson, lesson_id)
    assert refreshed.content["native_explanation"]["text"] == "新的讲解"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("options", "detail"),
    [
        ({"answered": True}, "answered"),
        ({"completed": True}, "Completed"),
        ({"unit_id": "completion-test"}, "cannot be regenerated"),
    ],
)
async def test_regenerate_refuses_started_lessons(client, test_user, db_session, options, detail):
    user, headers = test_user
    if options.get("unit_id"):
        from app.data.curriculum import COMPLETION_UNIT_ID

        options = {"unit_id": COMPLETION_UNIT_ID}
    lesson = await _lesson(db_session, user.id, **options)

    with patch("app.routers.lessons.generate_lesson", new=AsyncMock()) as generate:
        response = await client.post(f"/api/lessons/{lesson.id}/regenerate", headers=headers)

    assert response.status_code == 409
    assert detail in response.json()["detail"]
    generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_regenerate_rechecks_after_generation(client, test_user, db_session):
    """An answer submitted while generating keeps the original lesson."""
    user, headers = test_user
    lesson = await _lesson(db_session, user.id)
    lesson_id = lesson.id

    async def answer_meanwhile(**_kwargs):
        exercise = await db_session.scalar(select(Exercise).where(Exercise.lesson_id == lesson_id))
        exercise.user_answer = "old"
        await db_session.commit()
        return NEW_CONTENT

    with patch("app.routers.lessons.generate_lesson", new=answer_meanwhile):
        response = await client.post(f"/api/lessons/{lesson_id}/regenerate", headers=headers)

    assert response.status_code == 409
    db_session.expire_all()
    refreshed = await db_session.get(Lesson, lesson_id)
    assert refreshed.content == {"title": "Old lesson"}


@pytest.mark.asyncio
async def test_regenerate_keeps_lesson_when_generation_fails(client, test_user, db_session):
    user, headers = test_user
    lesson = await _lesson(db_session, user.id)
    lesson_id = lesson.id

    with patch(
        "app.routers.lessons.generate_lesson", new=AsyncMock(side_effect=RuntimeError("boom"))
    ):
        response = await client.post(f"/api/lessons/{lesson_id}/regenerate", headers=headers)

    assert response.status_code == 503
    db_session.expire_all()
    refreshed = await db_session.get(Lesson, lesson_id)
    assert refreshed.content == {"title": "Old lesson"}
    count = len(
        (await db_session.scalars(select(Exercise).where(Exercise.lesson_id == lesson_id))).all()
    )
    assert count == 1


@pytest.mark.asyncio
async def test_regenerate_other_users_lesson_is_not_found(client, test_user, db_session):
    from app.core.security import hash_password
    from app.models.user import User

    owner = User(
        username="regenowner",
        email="regenowner@example.com",
        display_name="Owner",
        hashed_password=hash_password("ownerpass"),
        role="user",
        native_language="es",
        is_active=True,
    )
    db_session.add(owner)
    await db_session.commit()
    await db_session.refresh(owner)
    lesson = await _lesson(db_session, owner.id)
    _, headers = test_user

    with patch("app.routers.lessons.generate_lesson", new=AsyncMock()):
        response = await client.post(f"/api/lessons/{lesson.id}/regenerate", headers=headers)

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_slow_regeneration_returns_202_then_finishes(
    client, test_user, db_session, monkeypatch
):
    import asyncio

    from app.services import generation_jobs

    user, headers = test_user
    monkeypatch.setattr(generation_jobs, "WAIT_SECONDS", 0.0)
    lesson = await _lesson(db_session, user.id)
    lesson_id = lesson.id
    release = asyncio.Event()
    calls = 0

    async def slow_generate(**_kwargs):
        nonlocal calls
        calls += 1
        await release.wait()
        return NEW_CONTENT

    with patch("app.routers.lessons.generate_lesson", new=slow_generate):
        started = await client.post(f"/api/lessons/{lesson_id}/regenerate", headers=headers)
        assert started.status_code == 202
        assert started.json() == {"status": "generating"}

        # A second click joins the running job instead of starting another.
        repeat = await client.post(f"/api/lessons/{lesson_id}/regenerate", headers=headers)
        assert repeat.status_code == 202
        polled = await client.get(f"/api/lessons/{lesson_id}/regenerate", headers=headers)
        assert polled.status_code == 202

        release.set()
        await generation_jobs.get(("regenerate-lesson", lesson_id))
        finished = await client.get(f"/api/lessons/{lesson_id}/regenerate", headers=headers)

    assert calls == 1
    assert finished.status_code == 200
    assert finished.json()["content"]["title"] == "Fresh lesson"


@pytest.mark.asyncio
async def test_regeneration_status_without_job_is_not_found(client, test_user, db_session):
    user, headers = test_user
    lesson = await _lesson(db_session, user.id)

    response = await client.get(f"/api/lessons/{lesson.id}/regenerate", headers=headers)

    assert response.status_code == 404
