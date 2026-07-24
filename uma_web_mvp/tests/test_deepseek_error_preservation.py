from __future__ import annotations

import asyncio
import sqlite3
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.config import Settings
from app.db import connect, ensure_schema, fail_fast_translation_task_refund_atomic
from app.provider_error_codes import classify_deepseek_failure, sanitize_public_error_code
from app.services.task_view_service import get_owned_task_summary
from app.smart_agent.deepseek_client import DeepSeekError, complete_json


def _settings(tmp_path):
    db_path = tmp_path / "test.db"
    env = tmp_path / ".env"
    env.write_text(
        "APP_ENV=local\n"
        f"BALANCE_DB={db_path}\n"
        f"BOT_OUTPUT_DIR={tmp_path / 'output'}\n"
        f"MOCK_OUTPUT_DIR={tmp_path / 'mock_output'}\n"
        "DEEPSEEK_API_KEY=sk-test1234567890abcdef\n"
        "DEEPSEEK_BASE_URL=https://api.deepseek.com/v1?token=SHOULD_NOT_LEAK\n"
        "DEEPSEEK_MODEL=deepseek-chat\n"
        "DEEPSEEK_MAX_RETRIES=0\n"
        "DEEPSEEK_TIMEOUT_SECONDS=10\n"
        "DEEPSEEK_CHAT_TIMEOUT_SECONDS=10\n",
        encoding="utf-8",
    )
    settings = Settings(_env_file=str(env))
    ensure_schema(settings)
    return settings


def _assert_safe_code(code: str, expected: str) -> None:
    assert code == expected
    assert len(code) <= 80
    assert "http" not in code
    assert ":\\" not in code
    assert "/home/" not in code
    assert "sk-" not in code
    assert "token" not in code.lower()


def _http_error(status_code: int, url: str = "https://api.deepseek.com/v1/chat/completions?api_key=sk-leak") -> httpx.HTTPStatusError:
    response = httpx.Response(
        status_code=status_code,
        json={"error": {"message": "provider detail should not become public"}},
        request=httpx.Request("POST", url),
    )
    return httpx.HTTPStatusError(f"{status_code} provider detail", request=response.request, response=response)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_http_error(401), "deepseek_auth_failed"),
        (_http_error(403), "deepseek_auth_failed"),
        (_http_error(429), "deepseek_rate_limited"),
        (httpx.ReadTimeout("Connection timed out while calling https://internal.local"), "deepseek_timeout"),
        (httpx.ConnectTimeout("connect timed out"), "deepseek_timeout"),
        (httpx.ConnectError("connect failed to http://10.0.0.2"), "deepseek_unavailable"),
        (_http_error(503), "deepseek_unavailable"),
        (DeepSeekError("deepseek_invalid_json"), "deepseek_invalid_response"),
        (RuntimeError(r"E:\discord-BOT\secret path sk-should-not-leak"), "deepseek_unavailable"),
    ],
)
def test_deepseek_failures_map_to_safe_public_codes(exc, expected):
    info = classify_deepseek_failure(exc)
    _assert_safe_code(info.public_code, expected)
    assert "sk-" not in info.internal_code
    assert "api_key" not in info.internal_code
    assert "http://" not in info.internal_code
    assert "https://" not in info.internal_code


def test_sanitize_public_error_code_rejects_raw_provider_strings():
    raw = r"401 Unauthorized for https://api.deepseek.com/chat?api_key=sk-secret in E:\discord-BOT\file"
    _assert_safe_code(sanitize_public_error_code(raw), "deepseek_auth_failed")
    _assert_safe_code(sanitize_public_error_code("not valid json at all"), "deepseek_invalid_response")


def test_complete_json_raises_safe_public_code_for_http_401(tmp_path):
    settings = _settings(tmp_path)
    http_error = _http_error(401)

    async def mock_post(*args, **kwargs):
        raise http_error

    with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=mock_post)):
        with patch("httpx.AsyncClient.__aenter__", return_value=AsyncMock(post=mock_post)):
            with patch("httpx.AsyncClient.__aexit__", return_value=False):
                with pytest.raises(DeepSeekError) as exc_info:
                    asyncio.run(complete_json(settings, system_prompt="test", user_prompt="test"))

    exc = exc_info.value
    _assert_safe_code(exc.code, "deepseek_auth_failed")
    assert str(exc) == "deepseek_auth_failed"
    assert exc.http_status == 401


def test_complete_json_raises_safe_public_code_for_malformed_json(tmp_path):
    settings = _settings(tmp_path)
    response = httpx.Response(
        status_code=200,
        content=b"not valid json at all",
        request=httpx.Request("POST", "https://api.deepseek.com/chat/completions"),
        headers={"content-type": "application/json"},
    )

    async def mock_post(*args, **kwargs):
        return response

    with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=mock_post)):
        with patch("httpx.AsyncClient.__aenter__", return_value=AsyncMock(post=mock_post)):
            with patch("httpx.AsyncClient.__aexit__", return_value=False):
                with pytest.raises(DeepSeekError) as exc_info:
                    asyncio.run(complete_json(settings, system_prompt="test", user_prompt="test"))

    _assert_safe_code(exc_info.value.code, "deepseek_invalid_response")


def test_fast_translation_failure_db_and_task_summary_do_not_expose_raw_provider_error(tmp_path):
    settings = _settings(tmp_path)
    conn = connect(settings)
    try:
        now = 123456
        conn.execute("INSERT INTO users(user_id,balance_fen) VALUES (?,?)", ("u1", 0))
        conn.execute(
            """
            INSERT INTO generation_tasks(
                job_code,user_id,username,prompt,style_key,width,height,lora_weight,status,source,created_at,charged_fen,
                translation_mode,fast_translation_request_code
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "GEN-AAAAAAAAAAAA",
                "u1",
                "Tester",
                "prompt",
                "default",
                512,
                768,
                0.8,
                "translating",
                "web",
                now,
                3,
                "fast",
                "TR-1",
            ),
        )
        conn.execute(
            """
            INSERT INTO translation_requests(
                request_code,user_id,client_request_id,translation_mode,model,original_text,refined_prompt,status,
                charged_credits,created_at,finished_at,error_code,generation_job_code
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            ("TR-1", "u1", "c1", "fast", "deepseek-chat", "text", "", "processing", 2, now, None, "", "GEN-AAAAAAAAAAAA"),
        )
        conn.commit()
    finally:
        conn.close()

    raw_error = "401 Unauthorized for https://api.deepseek.com/chat?api_key=sk-secret"
    assert fail_fast_translation_task_refund_atomic(
        settings,
        job_code="GEN-AAAAAAAAAAAA",
        error_code=raw_error,
    )

    conn = sqlite3.connect(settings.balance_db)
    conn.row_factory = sqlite3.Row
    try:
        task = conn.execute(
            "SELECT error,error_code FROM generation_tasks WHERE job_code='GEN-AAAAAAAAAAAA'"
        ).fetchone()
        tr = conn.execute(
            "SELECT error_code FROM translation_requests WHERE request_code='TR-1'"
        ).fetchone()
    finally:
        conn.close()

    _assert_safe_code(task["error_code"], "deepseek_auth_failed")
    _assert_safe_code(task["error"], "deepseek_auth_failed")
    _assert_safe_code(tr["error_code"], "deepseek_auth_failed")

    summary = get_owned_task_summary(settings, "u1", "GEN-AAAAAAAAAAAA")
    assert summary is not None
    _assert_safe_code(summary["public_error"], "deepseek_auth_failed")
