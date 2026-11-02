import asyncio
import json
from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.analytics import enqueue_analytics
from app.core.app_logger import get_logger
from app.core.database import get_db
from app.core.deps import (
    get_active_study_plan_optional,
    require_not_maintenance,
    require_subscription_or_freemium,
    require_subscription_or_freemium_readonly,
)
from app.core.limiter import limiter
from app.models.chat_history import ChatHistory
from app.models.conversation import Conversation
from app.models.llm_usage import LLMUsage
from app.models.progress import Progress
from app.models.user import User
from app.schemas.chat import (
    ChatHistoryResponse,
    ChatModelOption,
    ChatOptionsResponse,
    ChatRequest,
    ConversationCreate,
    ConversationResponse,
    ConversationTitleResponse,
    ConversationUpdate,
)
from app.services.language_helpers import get_language_name, get_native_language_name
from app.services.learning_analytics import record_lingu_practice
from app.services.llm_adapter import (
    LLMError,
    LLMStream,
    LLMStreamReset,
    LLMTimeoutError,
    LLMToolResultEvent,
    LLMUnavailableError,
    llm_adapter,
)
from app.services.memory_service import (
    build_memory_context,
    build_save_user_memory_tool,
    execute_save_user_memory,
    get_user_memories,
)
from app.services.progress_rewards import reward_conversation
from app.services.prompts.common import get_language_prompt_overlay
from app.services.prompts.tutor import build_tutor_system_prompt
from app.utils.db import db_session

router = APIRouter(prefix="/api/chat", tags=["chat"])

logger = get_logger(__name__)


def _build_tutor_system_prompt(
    *,
    student_name: str,
    cefr_level: str,
    native_language: str,
    target_language_name: str,
    total_xp: int,
    streak: int,
    lessons_today: int,
    skills: str,
    user_context: str,
    memory_context: str,
    language_prompt_overlay: str = "",
    memory_tools_enabled: bool = True,
) -> str:
    return build_tutor_system_prompt(
        student_name=student_name,
        cefr_level=cefr_level,
        native_language=native_language,
        target_language_name=target_language_name,
        total_xp=total_xp,
        streak=streak,
        lessons_today=lessons_today,
        skills=skills,
        user_context=user_context,
        memory_context=memory_context,
        language_prompt_overlay=language_prompt_overlay,
        memory_tools_enabled=memory_tools_enabled,
    )


MAX_HISTORY = 30
MAX_DISPLAY_MESSAGES = 200
DEFAULT_CEFR = "A2"
DEFAULT_TARGET_LANG = "en-GB"
TITLE_TIMEOUT_SECONDS = 20
# Keeps fire-and-forget saves alive after the request that started them is cancelled.
_background_tasks: set[asyncio.Task] = set()


def _can_switch_model(request: Request, user: User) -> bool:
    runtime = getattr(request.app.state, "ai_runtime", None)
    learners_allowed = bool(runtime and runtime.learner_model_switch)
    return user.role == "admin" or learners_allowed


def _model_options(request: Request) -> list[ChatModelOption]:
    runtime = getattr(request.app.state, "ai_runtime", None)
    if runtime is None:
        return []
    return [
        ChatModelOption(id=channel.id, name=channel.name, model=channel.model)
        for channel in runtime.text_channels
    ]


def _preferred_channel(request: Request, user: User, conv: Conversation) -> int | None:
    if conv.ai_channel_id is None or not _can_switch_model(request, user):
        return None
    return conv.ai_channel_id


def _validate_channel_choice(request: Request, user: User, channel_id: int | None) -> None:
    if channel_id is None:
        return
    if not _can_switch_model(request, user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Model selection is not enabled"
        )
    if channel_id not in {option.id for option in _model_options(request)}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Unknown AI model"
        )


async def _owned_conversation(
    db: AsyncSession, user: User, conversation_id: int
) -> Conversation:
    """Return the user's conversation in their active language, or raise 404."""
    from app.services.user_language_service import get_active_language

    conv = await db.get(Conversation, conversation_id)
    if not conv or conv.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    active_lang = await get_active_language(db, user.id)
    if active_lang and conv.target_language and conv.target_language != active_lang.target_language:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    return conv


def _clean_title(raw: str) -> str:
    title = " ".join(raw.split()).strip().strip("\"'“”‘’「」『』*#").strip()
    title = title.removeprefix("Title:").removeprefix("标题：").strip()
    return title[:60].rstrip()


async def _resolve_chat_context(
    db: AsyncSession,
    user: User,
) -> tuple[str, str, int | None]:
    """Resolve CEFR level, target language, and study_plan_id for chat.

    Only uses the active language's plan. If none exists, defaults to A2
    and the active language's code. Never falls back to another language's
    plan — that would make the tutor speak the wrong language.
    """
    from app.services.user_language_service import get_active_language

    # Try active study plan for the active language
    plan = await get_active_study_plan_optional(user, db)
    if plan:
        return plan.cefr_level, plan.target_language, plan.id

    # No plan — use active language's code or default
    active_lang = await get_active_language(db, user.id)
    target_language = active_lang.target_language if active_lang else DEFAULT_TARGET_LANG
    return DEFAULT_CEFR, target_language, None


# ── Conversations ────────────────────────────────────────────────────────────


@router.get("/conversations", response_model=list[ConversationResponse])
@limiter.limit("60/minute")
async def list_conversations(
    request: Request,
    q: str | None = Query(default=None, max_length=100),
    _maintenance: None = Depends(require_not_maintenance),
    current_user: User = Depends(require_subscription_or_freemium_readonly("chat")),
    db: AsyncSession = Depends(get_db),
):
    # Filter conversations by the active language (not study plan)
    from app.services.user_language_service import get_active_language

    active_lang = await get_active_language(db, current_user.id)
    if not active_lang:
        return []
    where_clause = (Conversation.user_id == current_user.id) & (
        Conversation.target_language == active_lang.target_language
    )
    query = (q or "").strip()
    if query:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        where_clause = where_clause & or_(
            Conversation.title.ilike(pattern, escape="\\"),
            exists().where(
                ChatHistory.conversation_id == Conversation.id,
                ChatHistory.user_id == current_user.id,
                ChatHistory.content.ilike(pattern, escape="\\"),
            ),
        )

    result = await db.execute(
        select(Conversation).where(where_clause).order_by(Conversation.updated_at.desc())
    )
    return result.scalars().all()


@router.get("/options", response_model=ChatOptionsResponse)
@limiter.limit("60/minute")
async def get_chat_options(
    request: Request,
    _maintenance: None = Depends(require_not_maintenance),
    current_user: User = Depends(require_subscription_or_freemium_readonly("chat")),
    db: AsyncSession = Depends(get_db),
):
    """Models the user may choose and the study plan used for voice input."""
    can_switch = _can_switch_model(request, current_user)
    plan = await get_active_study_plan_optional(current_user, db)
    return ChatOptionsResponse(
        can_switch_model=can_switch,
        models=_model_options(request) if can_switch else [],
        study_plan_id=plan.id if plan else None,
    )


@router.patch("/conversations/{conversation_id}", response_model=ConversationResponse)
@limiter.limit("60/minute")
async def update_conversation(
    request: Request,
    conversation_id: int,
    data: ConversationUpdate,
    _maintenance: None = Depends(require_not_maintenance),
    current_user: User = Depends(require_subscription_or_freemium_readonly("chat")),
    db: AsyncSession = Depends(get_db),
):
    conv = await _owned_conversation(db, current_user, conversation_id)
    if "title" in data.model_fields_set and data.title is not None:
        conv.title = data.title
    if "ai_channel_id" in data.model_fields_set:
        _validate_channel_choice(request, current_user, data.ai_channel_id)
        conv.ai_channel_id = data.ai_channel_id
    await db.commit()
    await db.refresh(conv)
    return conv


@router.post("/conversations/{conversation_id}/title", response_model=ConversationTitleResponse)
@limiter.limit("20/minute")
async def generate_conversation_title(
    request: Request,
    conversation_id: int,
    _maintenance: None = Depends(require_not_maintenance),
    current_user: User = Depends(require_subscription_or_freemium_readonly("chat")),
    db: AsyncSession = Depends(get_db),
):
    """Summarize the opening exchange into a short title; keeps the old title on failure."""
    conv = await _owned_conversation(db, current_user, conversation_id)
    rows = (
        await db.scalars(
            select(ChatHistory)
            .where(
                ChatHistory.conversation_id == conv.id,
                ChatHistory.user_id == current_user.id,
            )
            .order_by(ChatHistory.created_at.asc(), ChatHistory.id.asc())
            .limit(4)
        )
    ).all()
    if not rows:
        return ConversationTitleResponse(title=conv.title)
    transcript = "\n".join(f"{row.role}: {row.content[:500]}" for row in rows)
    language = get_native_language_name(current_user.native_language)
    prompt = (
        f"Write a short title (at most 8 words) in {language} for this language-practice "
        "conversation. Reply with the title only, without quotes or punctuation at the end."
    )
    extra = {}
    preferred = _preferred_channel(request, current_user, conv)
    if preferred is not None:
        extra["preferred_channel_id"] = preferred
    try:
        async with asyncio.timeout(TITLE_TIMEOUT_SECONDS):
            raw = await llm_adapter.chat(
                [{"role": "system", "content": prompt}, {"role": "user", "content": transcript}],
                **extra,
            )
    except (LLMError, TimeoutError):
        logger.info("Conversation title generation failed for %s", conv.id)
        return ConversationTitleResponse(title=conv.title)
    title = _clean_title(raw) if isinstance(raw, str) else ""
    if title:
        conv.title = title
        await db.commit()
    return ConversationTitleResponse(title=conv.title)


@router.post("/conversations", response_model=ConversationResponse)
@limiter.limit("60/minute")
async def create_conversation(
    request: Request,
    data: ConversationCreate,
    _maintenance: None = Depends(require_not_maintenance),
    current_user: User = Depends(require_subscription_or_freemium_readonly("chat")),
    db: AsyncSession = Depends(get_db),
):
    plan = await get_active_study_plan_optional(current_user, db)
    from app.services.user_language_service import get_active_language

    active_lang = await get_active_language(db, current_user.id)
    target_language = active_lang.target_language if active_lang else DEFAULT_TARGET_LANG
    conv = Conversation(
        user_id=current_user.id,
        title=data.title or "New conversation",
        study_plan_id=plan.id if plan else None,
        target_language=target_language,
    )
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    return conv


@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("60/minute")
async def delete_conversation(
    request: Request,
    conversation_id: int,
    _maintenance: None = Depends(require_not_maintenance),
    current_user: User = Depends(require_subscription_or_freemium_readonly("chat")),
    db: AsyncSession = Depends(get_db),
):
    conv = await _owned_conversation(db, current_user, conversation_id)
    await db.delete(conv)
    await db.commit()


@router.get("/conversations/{conversation_id}/messages", response_model=ChatHistoryResponse)
@limiter.limit("60/minute")
async def get_conversation_messages(
    request: Request,
    conversation_id: int,
    _maintenance: None = Depends(require_not_maintenance),
    current_user: User = Depends(require_subscription_or_freemium_readonly("chat")),
    db: AsyncSession = Depends(get_db),
):
    conv = await db.get(Conversation, conversation_id)
    if not conv or conv.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    # Filter messages by active language (not study plan)
    from app.services.user_language_service import get_active_language

    active_lang = await get_active_language(db, current_user.id)
    if not active_lang:
        return ChatHistoryResponse(messages=[])
    where_clause = (
        (ChatHistory.conversation_id == conversation_id)
        & (ChatHistory.user_id == current_user.id)
        & (ChatHistory.target_language == active_lang.target_language)
    )
    # Show the most recent messages, oldest first.
    result = await db.execute(
        select(ChatHistory)
        .where(where_clause)
        .order_by(ChatHistory.created_at.desc(), ChatHistory.id.desc())
        .limit(MAX_DISPLAY_MESSAGES)
    )
    rows = list(result.scalars().all())
    rows.reverse()
    messages = [
        {
            "id": m.id,
            "role": m.role,
            "content": m.content,
            "created_at": m.created_at,
            "modality": m.modality,
        }
        for m in rows
    ]
    return ChatHistoryResponse(messages=messages)


# ── Chat (streaming) ─────────────────────────────────────────────────────────


@router.post("")
@limiter.limit("30/minute")
async def chat(
    request: Request,
    request_data: ChatRequest,
    _maintenance: None = Depends(require_not_maintenance),
    current_user: User = Depends(require_subscription_or_freemium("chat")),
    db: AsyncSession = Depends(get_db),
):
    if current_user.monthly_tokens_limit > 0:
        from app.services.quota_service import check_monthly_tokens  # noqa: PLC0415

        allowed, tokens_used, token_limit = await check_monthly_tokens(
            db, current_user.id, current_user.monthly_tokens_limit
        )
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Monthly token limit reached ({tokens_used}/{token_limit} tokens). Chat is unavailable until next month.",
            )

    cefr_level, target_language, study_plan_id = await _resolve_chat_context(db, current_user)

    # Resolve or create conversation
    if request_data.conversation_id:
        conv = await db.get(Conversation, request_data.conversation_id)
        if not conv or conv.user_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found"
            )
        if conv.target_language and conv.target_language != target_language:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found"
            )
    else:
        _validate_channel_choice(request, current_user, request_data.ai_channel_id)
        message_text = request_data.message or ""
        # Auto-create a conversation titled with the first 60 chars of the message
        title = message_text[:60].replace("\n", " ").replace("\r", "").strip()
        if len(message_text) > 60:
            title += "..."
        conv = Conversation(
            user_id=current_user.id,
            title=title,
            study_plan_id=study_plan_id,
            target_language=target_language,
            ai_channel_id=request_data.ai_channel_id,
        )
        db.add(conv)
        await db.commit()
        await db.refresh(conv)

    conversation_id = conv.id

    # Edit or regenerate: drop the replaced turn and everything after it.
    regenerated_message: ChatHistory | None = None
    if request_data.regenerate:
        regenerated_message = await db.scalar(
            select(ChatHistory)
            .where(
                ChatHistory.conversation_id == conversation_id,
                ChatHistory.user_id == current_user.id,
                ChatHistory.role == "user",
            )
            .order_by(ChatHistory.id.desc())
            .limit(1)
        )
        if regenerated_message is None or regenerated_message.modality != "chat":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="Nothing to regenerate"
            )
        cutoff_id = regenerated_message.id + 1
    elif request_data.edit_message_id is not None:
        edited = await db.get(ChatHistory, request_data.edit_message_id)
        if (
            edited is None
            or edited.conversation_id != conversation_id
            or edited.user_id != current_user.id
            or edited.role != "user"
            or edited.modality != "chat"
        ):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
        cutoff_id = edited.id
    else:
        cutoff_id = None
    if cutoff_id is not None:
        await db.execute(
            delete(ChatHistory).where(
                ChatHistory.conversation_id == conversation_id,
                ChatHistory.user_id == current_user.id,
                ChatHistory.id >= cutoff_id,
            )
        )
        await db.commit()

    preferred_channel_id = _preferred_channel(request, current_user, conv)

    if study_plan_id:
        prog_result = await db.execute(
            select(Progress).where(
                Progress.study_plan_id == study_plan_id,
                Progress.date == date.today(),
            )
        )
        prog = prog_result.scalar_one_or_none()
        total_xp_result = await db.execute(
            select(Progress).where(Progress.study_plan_id == study_plan_id)
        )
        total_xp = sum(p.xp_earned for p in total_xp_result.scalars().all())
        skills_str = (
            ", ".join(f"{k}: {round(v * 100)}%" for k, v in (prog.skills or {}).items())
            if prog and prog.skills
            else "none yet"
        )
        streak = prog.streak_day if prog else 0
        lessons_today = prog.lessons_completed if prog else 0
    else:
        total_xp = 0
        skills_str = "none yet"
        streak = 0
        lessons_today = 0
        prog = None

    # Build user context section from bio and learning goals
    _ctx_parts: list[str] = []
    if current_user.learning_goals:
        try:
            import json as _json  # noqa: PLC0415

            goals = _json.loads(current_user.learning_goals)
            if goals:
                _ctx_parts.append(f"Learning goals: {', '.join(goals)}")
        except ValueError, TypeError:
            pass
    if current_user.bio and current_user.bio.strip():
        _ctx_parts.append(f"About the student: {current_user.bio.strip()}")
    user_context = (
        ("\nStudent context:\n" + "\n".join(f"- {p}" for p in _ctx_parts) + "\n")
        if _ctx_parts
        else ""
    )

    try:
        async with db_session() as memory_db:
            memories = await get_user_memories(memory_db, current_user.id)
        memory_context = build_memory_context(memories)
    except Exception:
        logger.exception("Failed to load memories for chat user %s — ignored", current_user.id)
        memory_context = ""

    target_language_name = get_language_name(target_language)
    native_language_name = get_native_language_name(current_user.native_language)
    language_prompt_overlay = get_language_prompt_overlay(target_language)

    system_prompt = _build_tutor_system_prompt(
        student_name=current_user.display_name,
        cefr_level=cefr_level,
        native_language=native_language_name,
        target_language_name=target_language_name,
        total_xp=total_xp,
        streak=streak,
        lessons_today=lessons_today,
        skills=skills_str,
        user_context=user_context,
        memory_context=memory_context,
        language_prompt_overlay=language_prompt_overlay,
    )
    fallback_system_prompt = _build_tutor_system_prompt(
        student_name=current_user.display_name,
        cefr_level=cefr_level,
        native_language=native_language_name,
        target_language_name=target_language_name,
        total_xp=total_xp,
        streak=streak,
        lessons_today=lessons_today,
        skills=skills_str,
        user_context=user_context,
        memory_context=memory_context,
        language_prompt_overlay=language_prompt_overlay,
        memory_tools_enabled=False,
    )

    if regenerated_message is not None:
        user_message_id = regenerated_message.id
    else:
        user_message = ChatHistory(
            user_id=current_user.id,
            conversation_id=conversation_id,
            role="user",
            content=request_data.message,
            study_plan_id=study_plan_id,
            target_language=target_language,
            modality="chat",
        )
        db.add(user_message)
        await db.commit()
        user_message_id = user_message.id

    result = await db.execute(
        select(ChatHistory)
        .where(
            ChatHistory.user_id == current_user.id,
            ChatHistory.conversation_id == conversation_id,
            ChatHistory.id <= user_message_id,
        )
        .order_by(ChatHistory.created_at.desc(), ChatHistory.id.desc())
        .limit(MAX_HISTORY)
    )
    db_messages = list(result.scalars().all())
    db_messages.reverse()

    messages = [{"role": "system", "content": system_prompt}] + [
        {"role": m.role, "content": m.content} for m in db_messages
    ]
    fallback_messages = [{"role": "system", "content": fallback_system_prompt}] + [
        {"role": m.role, "content": m.content} for m in db_messages
    ]

    async def event_stream():
        full_response = ""
        memory_updated_sent = False
        stream = None
        reply_saved = False
        try:
            yield (
                "data: "
                + json.dumps(
                    {"conversation_id": conversation_id, "user_message_id": user_message_id}
                )
                + "\n\n"
            )

            async def execute_memory_tool(call):
                async with db_session() as memory_db:
                    return await execute_save_user_memory(
                        memory_db,
                        current_user.id,
                        call,
                        "chat",
                        study_plan_id=study_plan_id,
                    )

            channel_choice = (
                {"preferred_channel_id": preferred_channel_id}
                if preferred_channel_id is not None
                else {}
            )
            stream = await llm_adapter.chat(
                messages,
                stream=True,
                tools=[build_save_user_memory_tool(native_language_name)],
                tool_executor=execute_memory_tool,
                fallback_messages=fallback_messages,
                **channel_choice,
            )
            async for chunk in stream:
                if isinstance(chunk, LLMToolResultEvent):
                    if chunk.result.content.get("saved") is True and not memory_updated_sent:
                        memory_updated_sent = True
                        yield f"data: {json.dumps({'memory_updated': True})}\n\n"
                    continue
                if isinstance(chunk, LLMStreamReset):
                    full_response = ""
                    yield f"data: {json.dumps({'response_reset': True})}\n\n"
                    continue
                token = (
                    chunk
                    if isinstance(chunk, str)
                    else getattr(getattr(chunk.choices[0], "delta", None), "content", None)
                )
                if token:
                    full_response += token
                    yield f"data: {json.dumps({'token': token})}\n\n"

            memory_updated = any(
                result.content.get("saved") is True
                for result in getattr(stream, "tool_results", [])
            )
            if memory_updated and not memory_updated_sent:
                yield f"data: {json.dumps({'memory_updated': True})}\n\n"

            completed_at = datetime.now(UTC).replace(tzinfo=None)
            assistant_message = ChatHistory(
                user_id=current_user.id,
                conversation_id=conversation_id,
                role="assistant",
                content=full_response,
                study_plan_id=study_plan_id,
                target_language=target_language,
                modality="chat",
                reply_to_id=user_message_id,
                created_at=completed_at,
            )
            db.add(assistant_message)
            conv.updated_at = completed_at
            await db.flush()
            await reward_conversation(db, assistant_message.id)
            await db.commit()
            reply_saved = True
            enqueue_analytics(
                request,
                record_lingu_practice,
                assistant_message.id,
                user_agent=request.headers.get("user-agent", ""),
            )

            yield f"data: {json.dumps({'done': True, 'message_id': assistant_message.id})}\n\n"

            # Record freemium chat usage (best-effort)
            from app.services.freemium_service import maybe_record_freemium_usage

            await maybe_record_freemium_usage(current_user, "chat")

        except (asyncio.CancelledError, GeneratorExit):
            # The learner pressed stop or left: keep the partial reply they already saw.
            if not reply_saved and full_response.strip():
                task = asyncio.get_running_loop().create_task(
                    _save_partial_reply(
                        user_id=current_user.id,
                        conversation_id=conversation_id,
                        reply_to_id=user_message_id,
                        content=full_response,
                        study_plan_id=study_plan_id,
                        target_language=target_language,
                    )
                )
                _background_tasks.add(task)
                task.add_done_callback(_background_tasks.discard)
            raise
        except LLMTimeoutError:
            logger.warning(
                "LLM timeout for user %s conversation %s",
                current_user.id,
                conversation_id,
            )
            yield f"data: {json.dumps({'error': 'The AI model took too long. Please try again.'})}\n\n"
        except LLMUnavailableError:
            logger.error("LLM unavailable for user %s", current_user.id)
            yield f"data: {json.dumps({'error': 'The AI service is currently unavailable.'})}\n\n"
        except LLMError:
            logger.exception(
                "LLM error for user %s conversation %s",
                current_user.id,
                conversation_id,
            )
            yield f"data: {json.dumps({'error': 'Something went wrong. Please try again.'})}\n\n"
        finally:
            if isinstance(stream, LLMStream) and (
                stream.prompt_tokens is not None or stream.completion_tokens is not None
            ):
                try:
                    async with db_session() as usage_db:
                        usage_db.add(
                            LLMUsage(
                                user_id=current_user.id,
                                source="chat",
                                prompt_tokens=stream.prompt_tokens,
                                completion_tokens=stream.completion_tokens,
                                total_tokens=stream.total_tokens,
                                study_plan_id=study_plan_id,
                            )
                        )
                        await usage_db.commit()
                except Exception:
                    logger.debug("Failed to save LLM usage — ignored")

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _save_partial_reply(
    *,
    user_id: int,
    conversation_id: int,
    reply_to_id: int,
    content: str,
    study_plan_id: int | None,
    target_language: str,
) -> None:
    """Persist a reply the learner stopped early. Stopped replies earn no rewards."""
    try:
        async with db_session() as save_db:
            completed_at = datetime.now(UTC).replace(tzinfo=None)
            save_db.add(
                ChatHistory(
                    user_id=user_id,
                    conversation_id=conversation_id,
                    role="assistant",
                    content=content,
                    study_plan_id=study_plan_id,
                    target_language=target_language,
                    modality="chat",
                    reply_to_id=reply_to_id,
                    created_at=completed_at,
                )
            )
            conv = await save_db.get(Conversation, conversation_id)
            if conv is not None:
                conv.updated_at = completed_at
            await save_db.commit()
    except Exception:
        logger.exception("Failed to save stopped chat reply for conversation %s", conversation_id)


# ── Legacy history endpoint (kept for backwards compat) ──────────────────────


@router.get("/history", response_model=ChatHistoryResponse)
@limiter.limit("60/minute")
async def get_history(
    request: Request,
    _maintenance: None = Depends(require_not_maintenance),
    current_user: User = Depends(require_subscription_or_freemium_readonly("chat")),
    db: AsyncSession = Depends(get_db),
):
    # Filter history by active language (not study plan)
    from app.services.user_language_service import get_active_language

    active_lang = await get_active_language(db, current_user.id)
    if not active_lang:
        return ChatHistoryResponse(messages=[])
    where_clause = (ChatHistory.user_id == current_user.id) & (
        ChatHistory.target_language == active_lang.target_language
    )
    result = await db.execute(
        select(ChatHistory)
        .where(where_clause)
        .order_by(ChatHistory.created_at.asc(), ChatHistory.id.asc())
        .limit(MAX_HISTORY)
    )
    messages = [{"role": m.role, "content": m.content} for m in result.scalars().all()]
    return ChatHistoryResponse(messages=messages)
