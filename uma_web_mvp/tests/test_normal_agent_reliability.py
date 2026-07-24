from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings
from app import agent


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


def _settings(**overrides) -> Settings:
    defaults = {
        "APP_ENV": "test",
        "APP_ORIGIN": "http://127.0.0.1:18080",
        "HOST": "127.0.0.1",
        "PORT": 18080,
        "BALANCE_DB": ":memory:",
        "BOT_OUTPUT_DIR": ".",
        "INPUT_IMAGE_DIR": ".",
        "BOT_DIR": ".",
        "redis_enabled": False,
        "agent_enabled": True,
        "agent_provider": "openai",
        "agent_base_url": "http://agent.test",
        "agent_model": "test-model",
        "agent_timeout_seconds": 10,
        "agent_max_concurrency": 2,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def test_normal_agent_uses_configured_concurrency(monkeypatch):
    settings = _settings(agent_max_concurrency=2)
    agent.AGENT_SEMAPHORE = None
    agent.AGENT_SEMAPHORE_LIMIT = 0

    async def fake_refine(_settings, _text):
        await asyncio.sleep(0.05)
        return "1girl, school uniform"

    monkeypatch.setattr(agent, "_refine_prompt_openai_compatible", fake_refine)

    async def run_two():
        return await asyncio.gather(
            agent.refine_prompt(settings, "school girl"),
            agent.refine_prompt(settings, "school girl"),
        )

    assert _run(run_two()) == ["1girl, school uniform", "1girl, school uniform"]


def test_normal_agent_post_retries_temporary_5xx():
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(503, json={"error": "temporary"})
        return httpx.Response(200, json={"ok": True})

    async def run_request():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, timeout=5) as client:
            return await agent._post_with_retries(client, "http://agent.test/chat/completions")

    response = _run(run_request())
    assert response.status_code == 200
    assert calls["count"] == 2
