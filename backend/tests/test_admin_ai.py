"""Administrator-managed AI channels, speech routing, and channel failover."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from pydantic import BaseModel
from sqlalchemy import select

from app.main import app
from app.models.ai_channel import AIChannel
from app.routers import admin_ai
from app.services import ai_channels
from app.services.llm_adapter import (
    LLMChannelConfig,
    LLMError,
    LLMUnavailableError,
    llm_adapter,
)
from app.services.stt_service import OpenAISTTService
from app.services.tts_service import OpenAITTSService

_STATE_KEYS = (
    "tts_service",
    "stt_service",
    "env_tts_service",
    "env_stt_service",
    "ai_runtime",
    "ai_runtime_fingerprint",
)


@pytest.fixture(autouse=True)
def isolate_ai_runtime():
    saved_state = {key: getattr(app.state, key, None) for key in _STATE_KEYS}
    present = {key for key in _STATE_KEYS if hasattr(app.state, key)}
    saved_channels = llm_adapter._channels
    env_tts = SimpleNamespace(name="env-tts")
    env_stt = SimpleNamespace(name="env-stt")
    app.state.tts_service = env_tts
    app.state.stt_service = env_stt
    app.state.env_tts_service = env_tts
    app.state.env_stt_service = env_stt
    yield SimpleNamespace(tts=env_tts, stt=env_stt)
    llm_adapter._channels = saved_channels
    for key in _STATE_KEYS:
        if key in present:
            setattr(app.state, key, saved_state[key])
        elif hasattr(app.state, key):
            delattr(app.state, key)


def channel_body(**overrides):
    body = {
        "name": "Gateway",
        "provider": "openai_compatible",
        "base_url": "https://gateway.example/v1/",
        "api_key": "sk-test-secret-1234",
        "model": "gpt-test",
        "priority": 10,
        "is_enabled": True,
    }
    body.update(overrides)
    return body


async def create(client, headers, **overrides):
    response = await client.post(
        "/api/admin/ai/channels", headers=headers, json=channel_body(**overrides)
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_requires_admin(client, test_user):
    _, headers = test_user
    assert (await client.get("/api/admin/ai", headers=headers)).status_code == 403
    response = await client.post("/api/admin/ai/channels", headers=headers, json=channel_body())
    assert response.status_code == 403


async def test_create_channel_encrypts_key_and_activates_it(client, admin_user, db_session):
    _, headers = admin_user
    data = await create(client, headers)

    channel = data["channels"][0]
    assert channel["base_url"] == "https://gateway.example/v1"
    assert channel["has_api_key"] is True
    assert channel["api_key_hint"] == "…1234"
    assert "sk-test-secret-1234" not in str(data)
    stored = await db_session.scalar(select(AIChannel))
    assert stored.api_key_encrypted and "sk-test-secret-1234" not in stored.api_key_encrypted
    assert ai_channels.decrypt_api_key(stored.api_key_encrypted) == "sk-test-secret-1234"

    assert data["runtime"]["text_source"] == "channels"
    assert data["runtime"]["text_channels"] == ["Gateway"]
    (adapter,) = llm_adapter.adapters()
    assert (adapter.provider, adapter.model, adapter.name) == (
        "openai_compatible",
        "gpt-test",
        "Gateway",
    )


async def test_update_keeps_or_clears_key(client, admin_user):
    _, headers = admin_user
    channel_id = (await create(client, headers))["channels"][0]["id"]
    body = channel_body(name="Renamed", api_key=None)

    response = await client.put(f"/api/admin/ai/channels/{channel_id}", headers=headers, json=body)
    assert response.status_code == 200
    channel = response.json()["channels"][0]
    assert channel["name"] == "Renamed" and channel["has_api_key"] is True

    body["clear_api_key"] = True
    response = await client.put(f"/api/admin/ai/channels/{channel_id}", headers=headers, json=body)
    assert response.json()["channels"][0]["has_api_key"] is False


async def test_priority_order_and_inactive_channels(client, admin_user):
    _, headers = admin_user
    await create(client, headers, name="Backup", priority=1)
    await create(client, headers, name="Primary", priority=50)
    await create(client, headers, name="Off", priority=99, is_enabled=False)
    data = await create(client, headers, name="Speech only", priority=100, model=None)

    names = [item["name"] for item in data["channels"]]
    assert names == ["Speech only", "Off", "Primary", "Backup"]
    assert data["runtime"]["text_channels"] == ["Primary", "Backup"]
    assert [adapter.name for adapter in llm_adapter.adapters()] == ["Primary", "Backup"]


async def test_deleting_last_channel_returns_to_environment(client, admin_user):
    _, headers = admin_user
    channel_id = (await create(client, headers))["channels"][0]["id"]
    response = await client.delete(f"/api/admin/ai/channels/{channel_id}", headers=headers)
    assert response.status_code == 200
    assert response.json()["runtime"]["text_source"] == "environment"
    assert not llm_adapter.uses_channels


@pytest.mark.parametrize(
    "overrides",
    [
        {"base_url": None},
        {"base_url": "ftp://gateway.example"},
        {"provider": "other"},
        {"name": "   "},
    ],
)
async def test_invalid_channels_are_rejected(client, admin_user, overrides):
    _, headers = admin_user
    response = await client.post(
        "/api/admin/ai/channels", headers=headers, json=channel_body(**overrides)
    )
    assert response.status_code == 422


async def test_provider_specific_options_are_normalized(client, admin_user):
    _, headers = admin_user
    data = await create(
        client,
        headers,
        provider="anthropic",
        base_url=None,
        reasoning_effort="high",
        max_tokens=4096,
    )
    channel = data["channels"][0]
    assert channel["reasoning_effort"] is None
    assert channel["max_tokens"] == 4096


async def test_speech_channels_replace_environment_services(client, admin_user, isolate_ai_runtime):
    _, headers = admin_user
    channel_id = (await create(client, headers, model=None))["channels"][0]["id"]

    response = await client.put(
        "/api/admin/ai/speech",
        headers=headers,
        json={
            "tts_channel_id": channel_id,
            "tts_model": "cosy-voice",
            "tts_voice": "alex",
            "tts_speed": 1.2,
            "tts_openai_voices": False,
            "stt_channel_id": channel_id,
            "stt_model": "sense-voice",
        },
    )
    assert response.status_code == 200, response.text
    runtime = response.json()["runtime"]
    assert runtime["tts_channel"] == runtime["stt_channel"] == "Gateway"

    tts = app.state.tts_service
    assert isinstance(tts, OpenAITTSService)
    assert (tts.model, tts.voice, tts.speed, tts.base_url) == (
        "cosy-voice",
        "alex",
        1.2,
        "https://gateway.example/v1",
    )
    assert isinstance(app.state.stt_service, OpenAISTTService)
    assert app.state.stt_service.model == "sense-voice"

    config = (await client.get("/api/config")).json()
    assert config["tts_provider"] == "custom"
    assert config["openai_tts_voice"] == "alex"

    await client.delete(f"/api/admin/ai/channels/{channel_id}", headers=headers)
    assert app.state.tts_service is isolate_ai_runtime.tts
    assert app.state.stt_service is isolate_ai_runtime.stt
    speech = (await client.get("/api/admin/ai", headers=headers)).json()["speech"]
    assert speech["tts_channel_id"] is None and speech["stt_channel_id"] is None


async def test_openai_voice_list_is_reported_to_learners(client, admin_user):
    _, headers = admin_user
    data = await create(client, headers, provider="openai", base_url=None)
    channel_id = data["channels"][0]["id"]
    await client.put(
        "/api/admin/ai/speech",
        headers=headers,
        json={"tts_channel_id": channel_id, "tts_voice": "coral"},
    )
    config = (await client.get("/api/config")).json()
    assert config["tts_provider"] == "openai"
    assert config["openai_tts_voice"] == "coral"


async def test_anthropic_cannot_serve_speech(client, admin_user):
    _, headers = admin_user
    channel_id = (
        await create(client, headers, provider="anthropic", base_url=None)
    )["channels"][0]["id"]
    response = await client.put(
        "/api/admin/ai/speech", headers=headers, json={"tts_channel_id": channel_id}
    )
    assert response.status_code == 422


async def test_disabled_speech_channel_falls_back(client, admin_user, isolate_ai_runtime):
    _, headers = admin_user
    channel_id = (await create(client, headers))["channels"][0]["id"]
    await client.put("/api/admin/ai/speech", headers=headers, json={"tts_channel_id": channel_id})
    assert isinstance(app.state.tts_service, OpenAITTSService)

    await client.put(
        f"/api/admin/ai/channels/{channel_id}",
        headers=headers,
        json=channel_body(is_enabled=False, api_key=None),
    )
    assert app.state.tts_service is isolate_ai_runtime.tts


async def test_probe_reuses_stored_key(client, admin_user, monkeypatch):
    _, headers = admin_user
    channel_id = (await create(client, headers))["channels"][0]["id"]
    probe = AsyncMock(return_value="OK")
    monkeypatch.setattr(admin_ai, "probe_channel", probe)

    response = await client.post(
        "/api/admin/ai/test",
        headers=headers,
        json={
            "channel_id": channel_id,
            "provider": "openai_compatible",
            "base_url": "https://other.example/v1",
            "model": "other-model",
        },
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True and response.json()["reply"] == "OK"
    config = probe.await_args.args[0]
    assert (config.api_key, config.base_url, config.model) == (
        "sk-test-secret-1234",
        "https://other.example/v1",
        "other-model",
    )


async def test_probe_reports_provider_errors(client, admin_user, monkeypatch):
    _, headers = admin_user
    monkeypatch.setattr(
        admin_ai, "probe_channel", AsyncMock(side_effect=LLMError("openai error: 401"))
    )
    response = await client.post(
        "/api/admin/ai/test",
        headers=headers,
        json={"provider": "openai", "api_key": "sk-x", "model": "gpt-test"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "ok": False,
        "latency_ms": response.json()["latency_ms"],
        "reply": None,
        "error": "openai error: 401",
    }


async def test_fetch_models(client, admin_user, monkeypatch):
    _, headers = admin_user
    monkeypatch.setattr(
        admin_ai, "list_channel_models", AsyncMock(return_value=["a-model", "b-model"])
    )
    response = await client.post(
        "/api/admin/ai/models",
        headers=headers,
        json={"provider": "openai_compatible", "base_url": "https://gateway.example/v1"},
    )
    assert response.json() == {"ok": True, "models": ["a-model", "b-model"], "error": None}


async def test_unreadable_key_is_flagged_and_skipped(client, admin_user, monkeypatch):
    _, headers = admin_user
    await create(client, headers)
    # Simulate a rotated SECRET_KEY without invalidating the admin's session.
    other_key = Fernet(Fernet.generate_key())
    monkeypatch.setattr(ai_channels, "_fernet", lambda: other_key)
    data = (await client.get("/api/admin/ai", headers=headers)).json()
    assert data["channels"][0]["api_key_unreadable"] is True

    response = await client.put(
        f"/api/admin/ai/channels/{data['channels'][0]['id']}",
        headers=headers,
        json=channel_body(name="Touched", api_key=None),
    )
    assert response.json()["runtime"]["text_source"] == "environment"


class Answer(BaseModel):
    answer: str


def _channels(*names):
    llm_adapter.set_channels(
        [
            LLMChannelConfig(provider="openai_compatible", model="m", base_url="http://x", name=n)
            for n in names
        ]
    )
    return llm_adapter.adapters()


async def test_router_fails_over_to_next_channel(monkeypatch):
    first, second = _channels("first", "second")
    monkeypatch.setattr(first, "chat", AsyncMock(side_effect=LLMUnavailableError("down")))
    monkeypatch.setattr(second, "chat", AsyncMock(return_value="Hello"))
    assert await llm_adapter.chat([{"role": "user", "content": "Hi"}]) == "Hello"

    monkeypatch.setattr(
        first, "structured_output", AsyncMock(side_effect=LLMError("bad gateway"))
    )
    monkeypatch.setattr(second, "structured_output", AsyncMock(return_value=Answer(answer="ok")))
    assert await llm_adapter.structured_output([], Answer) == Answer(answer="ok")


async def test_router_raises_last_error_when_all_channels_fail(monkeypatch):
    first, second = _channels("first", "second")
    monkeypatch.setattr(first, "chat", AsyncMock(side_effect=LLMUnavailableError("one")))
    monkeypatch.setattr(second, "chat", AsyncMock(side_effect=LLMUnavailableError("two")))
    with pytest.raises(LLMUnavailableError, match="two"):
        await llm_adapter.chat([{"role": "user", "content": "Hi"}])


async def test_router_does_not_fail_over_past_deadline(monkeypatch):
    import asyncio

    first, second = _channels("first", "second")
    monkeypatch.setattr(first, "structured_output", AsyncMock(side_effect=LLMError("late")))
    second_call = AsyncMock()
    monkeypatch.setattr(second, "structured_output", second_call)
    deadline = asyncio.get_running_loop().time() - 1
    with pytest.raises(LLMError, match="late"):
        await llm_adapter.structured_output([], Answer, deadline=deadline)
    second_call.assert_not_awaited()


def test_channel_config_applies_provider_options():
    from app.services.llm_adapter import LLMAdapter

    adapter = LLMAdapter(
        LLMChannelConfig(
            provider="openai", model="gpt-x", api_key="sk", reasoning_effort="low", name="o"
        )
    )
    assert adapter._reasoning_effort == "low"
    anthropic = LLMAdapter(
        LLMChannelConfig(provider="anthropic", model="claude-x", api_key="sk", max_tokens=1234)
    )
    assert anthropic._anthropic_max_tokens == 1234
    assert anthropic.client is None
