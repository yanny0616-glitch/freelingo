"""Administrator-managed AI channels: key storage and runtime activation.

Channels live in the database so administrators can change providers from the
web interface. Every worker reloads them when they change; when no channel is
usable, the environment configuration remains in effect.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from contextlib import suppress
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.app_logger import get_logger
from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.ai_channel import AIChannel, AISpeechSettings
from app.services.llm_adapter import LLMChannelConfig, llm_adapter
from app.services.stt_service import OpenAISTTService
from app.services.tts_service import OpenAITTSService

logger = get_logger(__name__)

REFRESH_INTERVAL_SECONDS = 15
DEFAULT_TTS_MODEL = "tts-1"
DEFAULT_TTS_VOICE = "nova"
DEFAULT_STT_MODEL = "whisper-1"
SPEECH_PROVIDERS = frozenset({"openai", "openai_compatible"})


def _fernet() -> Fernet:
    digest = hashlib.sha256(b"freelingo-ai-channel-keys:" + settings.SECRET_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_api_key(api_key: str) -> str:
    return _fernet().encrypt(api_key.encode()).decode()


def decrypt_api_key(token: str | None) -> str | None:
    """Return the plaintext key, "" when none is stored, or None when unreadable."""
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return None


def api_key_hint(api_key: str) -> str:
    return f"…{api_key[-4:]}" if len(api_key) > 8 else "…"


def channel_config(channel: AIChannel, *, model: str | None = None) -> LLMChannelConfig | None:
    """Build the adapter configuration, or None when the stored key is unreadable."""
    api_key = decrypt_api_key(channel.api_key_encrypted)
    if api_key is None:
        return None
    return LLMChannelConfig(
        provider=channel.provider,
        model=model if model is not None else (channel.model or ""),
        api_key=api_key,
        base_url=channel.base_url,
        reasoning_effort=channel.reasoning_effort,
        max_tokens=channel.max_tokens,
        name=channel.name,
    )


@dataclass(frozen=True)
class RuntimeSnapshot:
    llm_channels: tuple[str, ...]
    tts_channel: str | None
    stt_channel: str | None


async def _load(db: AsyncSession) -> tuple[list[AIChannel], AISpeechSettings | None]:
    channels = list(
        (
            await db.scalars(
                select(AIChannel).order_by(AIChannel.priority.desc(), AIChannel.id.asc())
            )
        ).all()
    )
    speech = await db.get(AISpeechSettings, 1)
    return channels, speech


def _fingerprint(channels: list[AIChannel], speech: AISpeechSettings | None) -> tuple:
    return (
        tuple((channel.id, channel.updated_at) for channel in channels),
        speech.updated_at if speech else None,
    )


def _speech_channel(
    channels: list[AIChannel], channel_id: int | None, kind: str
) -> tuple[AIChannel, str] | None:
    channel = next((item for item in channels if item.id == channel_id), None)
    if channel is None or not channel.is_enabled or channel.provider not in SPEECH_PROVIDERS:
        return None
    api_key = decrypt_api_key(channel.api_key_encrypted)
    if api_key is None:
        logger.error(
            "AI channel %s has an unreadable API key; %s stays on defaults", channel.name, kind
        )
        return None
    return channel, api_key


def _apply(app: FastAPI, channels: list[AIChannel], speech: AISpeechSettings | None) -> None:
    llm_configs: list[LLMChannelConfig] = []
    for channel in channels:
        if not channel.is_enabled or not channel.model:
            continue
        config = channel_config(channel)
        if config is None:
            logger.error("AI channel %s has an unreadable API key; skipping it", channel.name)
            continue
        llm_configs.append(config)
    llm_adapter.set_channels(llm_configs)

    tts_service = getattr(app.state, "env_tts_service", None)
    tts_name = None
    tts = _speech_channel(channels, speech.tts_channel_id if speech else None, "TTS")
    if speech and tts:
        channel, api_key = tts
        tts_service = OpenAITTSService(
            api_key=api_key,
            model=speech.tts_model or DEFAULT_TTS_MODEL,
            voice=speech.tts_voice or DEFAULT_TTS_VOICE,
            speed=speech.tts_speed,
            base_url=channel.base_url,
            openai_voices=speech.tts_openai_voices,
        )
        tts_name = channel.name

    stt_service = getattr(app.state, "env_stt_service", None)
    stt_name = None
    stt = _speech_channel(channels, speech.stt_channel_id if speech else None, "STT")
    if speech and stt:
        channel, api_key = stt
        stt_service = OpenAISTTService(
            api_key=api_key,
            model=speech.stt_model or DEFAULT_STT_MODEL,
            base_url=channel.base_url,
        )
        stt_name = channel.name

    app.state.tts_service = tts_service
    app.state.stt_service = stt_service
    app.state.ai_runtime = RuntimeSnapshot(
        llm_channels=tuple(config.name for config in llm_configs),
        tts_channel=tts_name,
        stt_channel=stt_name,
    )


async def apply_ai_runtime(app: FastAPI, db: AsyncSession, *, force: bool = False) -> None:
    """Load channels from the database and activate them if they changed."""
    channels, speech = await _load(db)
    fingerprint = _fingerprint(channels, speech)
    if not hasattr(app.state, "env_tts_service"):
        app.state.env_tts_service = getattr(app.state, "tts_service", None)
        app.state.env_stt_service = getattr(app.state, "stt_service", None)
    if not force and getattr(app.state, "ai_runtime_fingerprint", None) == fingerprint:
        return
    _apply(app, channels, speech)
    app.state.ai_runtime_fingerprint = fingerprint
    logger.info(
        "AI runtime updated: text channels=%s tts=%s stt=%s",
        list(app.state.ai_runtime.llm_channels) or "environment",
        app.state.ai_runtime.tts_channel or "environment",
        app.state.ai_runtime.stt_channel or "environment",
    )


async def _refresh_forever(app: FastAPI) -> None:
    while True:
        await asyncio.sleep(REFRESH_INTERVAL_SECONDS)
        try:
            async with AsyncSessionLocal() as db:
                await apply_ai_runtime(app, db)
        except Exception:
            logger.exception("AI channel refresh failed; keeping the current configuration")


async def start_ai_runtime(app: FastAPI) -> asyncio.Task:
    """Activate stored channels now and keep every worker in sync afterwards."""
    app.state.env_tts_service = getattr(app.state, "tts_service", None)
    app.state.env_stt_service = getattr(app.state, "stt_service", None)
    try:
        async with AsyncSessionLocal() as db:
            await apply_ai_runtime(app, db, force=True)
    except Exception:
        logger.exception("Could not load AI channels; using the environment configuration")
    return asyncio.create_task(_refresh_forever(app))


async def stop_ai_runtime(task: asyncio.Task) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
