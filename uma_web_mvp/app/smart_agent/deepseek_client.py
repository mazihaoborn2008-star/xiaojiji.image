from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from app.config import Settings


class DeepSeekError(RuntimeError):
    pass


async def complete_json(settings: Settings, *, system_prompt: str, user_prompt: str) -> dict[str, Any]:
    if not settings.deepseek_api_key:
        raise DeepSeekError("smart_agent_not_configured")
    base = settings.deepseek_base_url.rstrip("/")
    url = f"{base}/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.deepseek_api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": settings.deepseek_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "max_tokens": max(512, int(settings.deepseek_chat_max_output_tokens or 4096)),
    }
    last_error: Exception | None = None
    timeout = max(5, int(settings.deepseek_chat_timeout_seconds or settings.deepseek_timeout_seconds))
    attempts = max(1, int(settings.deepseek_max_retries) + 1)
    for index in range(attempts):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, headers=headers, json=payload)
            if response.status_code >= 400:
                raise DeepSeekError(f"deepseek_http_{response.status_code}")
            data = response.json()
            choice = data["choices"][0]
            content = choice["message"]["content"]
            parsed = _parse_json_object(str(content))
            finish_reason = str(choice.get("finish_reason") or "")
            if finish_reason:
                parsed["_finish_reason"] = finish_reason
            return parsed
        except Exception as exc:
            last_error = exc
            if index + 1 < attempts:
                await asyncio.sleep(1.2 * (index + 1))
    raise DeepSeekError(type(last_error).__name__ if last_error else "deepseek_failed")


def _parse_json_object(text: str) -> dict[str, Any]:
    clean = text.strip()
    if clean.startswith("```"):
        clean = clean.strip("`").strip()
        if clean.lower().startswith("json"):
            clean = clean[4:].strip()
    try:
        data = json.loads(clean)
    except json.JSONDecodeError as exc:
        raise DeepSeekError("deepseek_invalid_json") from exc
    if not isinstance(data, dict):
        raise DeepSeekError("deepseek_json_not_object")
    return data
