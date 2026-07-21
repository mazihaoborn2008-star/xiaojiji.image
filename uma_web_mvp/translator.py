"""Character name translator using DeepSeek API (preferred) or Ollama fallback.

Rules:
1. On failure, return original text (never raise).
2. Log tracebacks without keys/tokens.
3. Suggest user add English name or Tag when translation is ambiguous.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

# Read config from .env directly (avoid circular import with app.config)
_ENV_PATH = Path(__file__).resolve().parent / ".env"


def _read_env(key: str, default: str = "") -> str:
    """Read a single key from .env without importing app.config."""
    try:
        for line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == key:
                return v.strip().strip('"').strip("'")
    except Exception:
        pass
    return os.environ.get(key, default)


async def translate_text(text: str) -> str:
    """Translate CJK character/series names to English tags.

    Priority: DeepSeek API → Ollama local → return original.
    """
    if not text or not any("\u4e00" <= ch <= "\u9fff" for ch in text):
        return text

    # Try DeepSeek first
    deepseek_key = _read_env("DEEPSEEK_API_KEY")
    deepseek_base = _read_env("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    deepseek_model = _read_env("DEEPSEEK_MODEL", "deepseek-v4-flash")

    if deepseek_key:
        try:
            result = await _translate_deepseek(text, deepseek_key, deepseek_base, deepseek_model)
            if result and result != text:
                return result
        except Exception as exc:
            print(f"[TRANSLATOR] deepseek_failed error={type(exc).__name__}: {str(exc)[:100]}", flush=True)

    # Try Ollama fallback
    ollama_base = _read_env("AGENT_BASE_URL", "http://127.0.0.1:11434")
    ollama_model = _read_env("AGENT_MODEL", "qwen3.5-9b-uncensored:latest")
    try:
        result = await _translate_ollama(text, ollama_base, ollama_model)
        if result and result != text:
            return result
    except Exception as exc:
        print(f"[TRANSLATOR] ollama_failed error={type(exc).__name__}: {str(exc)[:100]}", flush=True)

    # Both failed: return original
    return text


async def _translate_deepseek(text: str, api_key: str, base_url: str, model: str) -> str:
    import httpx

    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    payload = {
        "model": model,
        "temperature": 0.1,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a translator for anime/booru character names and series names. "
                    "Convert Chinese names to their standard English booru tag equivalents. "
                    "Rules:\n"
                    "- Return ONLY the English name, no explanation.\n"
                    "- For character names: use the standard booru tag format (e.g., '七海麻美' → 'nanami_mami').\n"
                    "- For series names: use the standard franchise tag.\n"
                    "- If unsure, return the most common English transliteration.\n"
                    "- Never return Chinese characters."
                ),
            },
            {"role": "user", "content": text},
        ],
    }
    timeout = 15
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, headers=headers, json=payload)
        if response.status_code != 200:
            raise RuntimeError(f"DeepSeek returned HTTP {response.status_code}")
        data = response.json()
        content = data["choices"][0]["message"]["content"].strip()
        # Clean up: remove markdown, quotes, explanations
        content = re.sub(r"^```.*?\n?", "", content, flags=re.MULTILINE)
        content = re.sub(r"\n?```$", "", content, flags=re.MULTILINE)
        content = content.strip().strip('"').strip("'")
        # Take first line only
        content = content.split("\n")[0].strip()
        # Remove any remaining CJK
        if any("\u4e00" <= ch <= "\u9fff" for ch in content):
            # Still has CJK, try extracting English part
            en_parts = re.findall(r"[a-zA-Z_][a-zA-Z0-9_ ]+", content)
            if en_parts:
                content = max(en_parts, key=len).strip()
        return content


async def _translate_ollama(text: str, base_url: str, model: str) -> str:
    import httpx

    url = f"{base_url.rstrip('/')}/api/chat"
    payload = {
        "model": model,
        "stream": False,
        "keep_alive": "5m",
        "options": {"temperature": 0.1, "num_predict": 100},
        "messages": [
            {
                "role": "system",
                "content": (
                    "Translate Chinese anime character names to English booru tags. "
                    "Return ONLY the English name, nothing else. Example: 七海麻美 → nanami_mami"
                ),
            },
            {"role": "user", "content": text},
        ],
    }
    timeout = 20
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, json=payload)
        if response.status_code != 200:
            raise RuntimeError(f"Ollama returned HTTP {response.status_code}")
        data = response.json()
        content = data["message"]["content"].strip()
        content = re.sub(r"^```.*?\n?", "", content, flags=re.MULTILINE)
        content = re.sub(r"\n?```$", "", content, flags=re.MULTILINE)
        content = content.strip().strip('"').strip("'")
        content = content.split("\n")[0].strip()
        return content
