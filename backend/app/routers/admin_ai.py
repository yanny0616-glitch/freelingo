"""Administrator management of AI channels and speech routing."""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import require_admin
from app.core.limiter import limiter
from app.models.ai_channel import AIChannel, AISpeechSettings
from app.models.user import User
from app.schemas.ai_channel import (
    AIChannelCreate,
    AIChannelModelsResponse,
    AIChannelProbeRequest,
    AIChannelResponse,
    AIChannelTestResponse,
    AIChannelUpdate,
    AIConfigResponse,
    AIRuntimeStatus,
    AISpeechSettingsPayload,
)
from app.services.ai_channels import (
    SPEECH_PROVIDERS,
    RuntimeSnapshot,
    api_key_hint,
    apply_ai_runtime,
    decrypt_api_key,
    encrypt_api_key,
)
from app.services.llm_adapter import (
    LLMChannelConfig,
    LLMError,
    list_channel_models,
    probe_channel,
)

router = APIRouter(prefix="/api/admin/ai", tags=["admin"])

_ERROR_LIMIT = 500


def _channel_response(channel: AIChannel) -> AIChannelResponse:
    has_key = bool(channel.api_key_encrypted)
    return AIChannelResponse(
        id=channel.id,
        name=channel.name,
        provider=channel.provider,
        base_url=channel.base_url,
        model=channel.model,
        reasoning_effort=channel.reasoning_effort,
        max_tokens=channel.max_tokens,
        priority=channel.priority,
        is_enabled=channel.is_enabled,
        has_api_key=has_key,
        api_key_hint=channel.api_key_hint if has_key else None,
        api_key_unreadable=has_key and decrypt_api_key(channel.api_key_encrypted) is None,
        created_at=channel.created_at,
        updated_at=channel.updated_at,
    )


def _runtime_status(request: Request) -> AIRuntimeStatus:
    snapshot: RuntimeSnapshot | None = getattr(request.app.state, "ai_runtime", None)
    llm_channels = list(snapshot.llm_channels) if snapshot else []
    tts_channel = snapshot.tts_channel if snapshot else None
    stt_channel = snapshot.stt_channel if snapshot else None
    env_model = {
        "ollama": settings.OLLAMA_MODEL,
        "openai": settings.OPENAI_MODEL,
        "anthropic": settings.ANTHROPIC_MODEL,
        "deepseek": settings.DEEPSEEK_MODEL,
    }.get(settings.LLM_PROVIDER, "")
    return AIRuntimeStatus(
        text_source="channels" if llm_channels else "environment",
        text_channels=llm_channels,
        environment_provider=settings.LLM_PROVIDER,
        environment_model=env_model,
        tts_source="channel" if tts_channel else "environment",
        tts_channel=tts_channel,
        stt_source="channel" if stt_channel else "environment",
        stt_channel=stt_channel,
    )


async def _config_response(request: Request, db: AsyncSession) -> AIConfigResponse:
    channels = (
        await db.scalars(select(AIChannel).order_by(AIChannel.priority.desc(), AIChannel.id))
    ).all()
    speech = await db.get(AISpeechSettings, 1)
    return AIConfigResponse(
        channels=[_channel_response(channel) for channel in channels],
        speech=(
            AISpeechSettingsPayload.model_validate(speech)
            if speech
            else AISpeechSettingsPayload()
        ),
        runtime=_runtime_status(request),
    )


async def _commit_and_apply(request: Request, db: AsyncSession) -> None:
    await db.commit()
    await apply_ai_runtime(request.app, db, force=True)


def _set_api_key(channel: AIChannel, api_key: str | None) -> None:
    if api_key:
        api_key = api_key.strip()
    if not api_key:
        channel.api_key_encrypted = None
        channel.api_key_hint = None
        return
    channel.api_key_encrypted = encrypt_api_key(api_key)
    channel.api_key_hint = api_key_hint(api_key)


async def _get_channel(db: AsyncSession, channel_id: int) -> AIChannel:
    channel = await db.get(AIChannel, channel_id)
    if channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="AI channel not found")
    return channel


@router.get("", response_model=AIConfigResponse)
@limiter.limit("60/minute")
async def get_ai_config(
    request: Request,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    return await _config_response(request, db)


@router.post("/channels", response_model=AIConfigResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("30/minute")
async def create_channel(
    request: Request,
    data: AIChannelCreate,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    channel = AIChannel(**data.model_dump(exclude={"api_key"}))
    _set_api_key(channel, data.api_key)
    db.add(channel)
    await _commit_and_apply(request, db)
    return await _config_response(request, db)


@router.put("/channels/{channel_id}", response_model=AIConfigResponse)
@limiter.limit("30/minute")
async def update_channel(
    request: Request,
    channel_id: int,
    data: AIChannelUpdate,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    channel = await _get_channel(db, channel_id)
    for field, value in data.model_dump(exclude={"api_key", "clear_api_key"}).items():
        setattr(channel, field, value)
    if data.clear_api_key:
        _set_api_key(channel, None)
    elif data.api_key and data.api_key.strip():
        _set_api_key(channel, data.api_key)
    if channel.provider not in SPEECH_PROVIDERS:
        speech = await db.get(AISpeechSettings, 1)
        if speech and channel_id in (speech.tts_channel_id, speech.stt_channel_id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This channel is used for speech, which Anthropic does not provide",
            )
    await _commit_and_apply(request, db)
    return await _config_response(request, db)


@router.delete("/channels/{channel_id}", response_model=AIConfigResponse)
@limiter.limit("30/minute")
async def delete_channel(
    request: Request,
    channel_id: int,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    channel = await _get_channel(db, channel_id)
    speech = await db.get(AISpeechSettings, 1)
    if speech is not None:
        # Mirror ON DELETE SET NULL explicitly so every database behaves the same.
        if speech.tts_channel_id == channel_id:
            speech.tts_channel_id = None
        if speech.stt_channel_id == channel_id:
            speech.stt_channel_id = None
    await db.delete(channel)
    await _commit_and_apply(request, db)
    return await _config_response(request, db)


@router.put("/speech", response_model=AIConfigResponse)
@limiter.limit("30/minute")
async def update_speech(
    request: Request,
    data: AISpeechSettingsPayload,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    for channel_id in (data.tts_channel_id, data.stt_channel_id):
        if channel_id is None:
            continue
        channel = await _get_channel(db, channel_id)
        if channel.provider not in SPEECH_PROVIDERS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Speech requires an OpenAI or OpenAI-compatible channel",
            )
    speech = await db.get(AISpeechSettings, 1)
    if speech is None:
        speech = AISpeechSettings(id=1)
        db.add(speech)
    for field, value in data.model_dump().items():
        setattr(speech, field, value)
    await _commit_and_apply(request, db)
    return await _config_response(request, db)


async def _probe_config(db: AsyncSession, data: AIChannelProbeRequest) -> LLMChannelConfig:
    api_key = data.api_key or ""
    channel: AIChannel | None = None
    if data.channel_id is not None:
        channel = await _get_channel(db, data.channel_id)
        if not data.api_key:
            stored = decrypt_api_key(channel.api_key_encrypted)
            if stored is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="The stored API key cannot be read; enter it again",
                )
            api_key = stored
    if data.provider == "openai_compatible" and not data.base_url:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="OpenAI-compatible channels require a base URL",
        )
    return LLMChannelConfig(
        provider=data.provider,
        model=data.model or (channel.model if channel else "") or "",
        api_key=api_key,
        base_url=data.base_url,
        reasoning_effort=channel.reasoning_effort if channel else None,
        max_tokens=channel.max_tokens if channel else None,
        name=channel.name if channel else "probe",
    )


@router.post("/test", response_model=AIChannelTestResponse)
@limiter.limit("10/minute")
async def test_channel(
    request: Request,
    data: AIChannelProbeRequest,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    config = await _probe_config(db, data)
    if not config.model:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Choose a model to test",
        )
    started = time.perf_counter()
    try:
        reply = await probe_channel(config)
    except LLMError as exc:
        return AIChannelTestResponse(
            ok=False,
            latency_ms=round((time.perf_counter() - started) * 1000),
            error=str(exc)[:_ERROR_LIMIT],
        )
    return AIChannelTestResponse(
        ok=True,
        latency_ms=round((time.perf_counter() - started) * 1000),
        reply=reply[:200],
    )


@router.post("/models", response_model=AIChannelModelsResponse)
@limiter.limit("10/minute")
async def fetch_models(
    request: Request,
    data: AIChannelProbeRequest,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    config = await _probe_config(db, data)
    try:
        models = await list_channel_models(config)
    except LLMError as exc:
        return AIChannelModelsResponse(ok=False, error=str(exc)[:_ERROR_LIMIT])
    return AIChannelModelsResponse(ok=True, models=models)
