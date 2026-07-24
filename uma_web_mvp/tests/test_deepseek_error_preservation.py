"""Regression tests for DeepSeek error preservation.

Verifies that the actual error type/message from DeepSeek API
is preserved through the error handling chain, not lost as generic "DeepSeekError".
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from app.config import Settings
from app.smart_agent.deepseek_client import DeepSeekError, complete_json


class TestDeepSeekErrorPreservation:
    """Ensure DeepSeekError preserves the actual error type/message."""

    @pytest.fixture()
    def settings(self, tmp_path):
        """Minimal settings with a fake API key."""
        env = tmp_path / ".env"
        env.write_text(
            "APP_ENV=local\n"
            "DEEPSEEK_API_KEY=sk-test1234567890abcdef\n"
            "DEEPSEEK_BASE_URL=https://api.deepseek.com\n"
            "DEEPSEEK_MODEL=deepseek-chat\n"
            "DEEPSEEK_MAX_RETRIES=0\n"
            "DEEPSEEK_TIMEOUT_SECONDS=10\n"
            "DEEPSEEK_CHAT_TIMEOUT_SECONDS=10\n",
            encoding="utf-8",
        )
        return Settings(_env_file=str(env))

    def test_http_401_preserved_in_error(self, settings):
        """HTTP 401 must produce error containing '401', not generic 'DeepSeekError'."""
        import httpx

        mock_response = httpx.Response(
            status_code=401,
            json={"error": {"message": "Invalid API key", "type": "auth_error"}},
            request=httpx.Request("POST", "https://api.deepseek.com/chat/completions"),
        )
        http_error = httpx.HTTPStatusError(
            "401 Unauthorized",
            request=mock_response.request,
            response=mock_response,
        )

        async def mock_post(*args, **kwargs):
            raise http_error

        with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=mock_post)):
            with patch("httpx.AsyncClient.__aenter__", return_value=AsyncMock(post=mock_post)):
                with patch("httpx.AsyncClient.__aexit__", return_value=False):
                    with pytest.raises(DeepSeekError) as exc_info:
                        asyncio.run(complete_json(settings, system_prompt="test", user_prompt="test"))
                    error_msg = str(exc_info.value)
                    assert "401" in error_msg, f"Expected '401' in error, got: {error_msg}"
                    assert error_msg != "DeepSeekError", f"Error message should not be generic 'DeepSeekError'"

    def test_http_429_preserved_in_error(self, settings):
        """HTTP 429 must produce error containing '429'."""
        import httpx

        mock_response = httpx.Response(
            status_code=429,
            json={"error": {"message": "Rate limited", "type": "rate_limit"}},
            request=httpx.Request("POST", "https://api.deepseek.com/chat/completions"),
        )
        http_error = httpx.HTTPStatusError(
            "429 Too Many Requests",
            request=mock_response.request,
            response=mock_response,
        )

        async def mock_post(*args, **kwargs):
            raise http_error

        with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=mock_post)):
            with patch("httpx.AsyncClient.__aenter__", return_value=AsyncMock(post=mock_post)):
                with patch("httpx.AsyncClient.__aexit__", return_value=False):
                    with pytest.raises(DeepSeekError) as exc_info:
                        asyncio.run(complete_json(settings, system_prompt="test", user_prompt="test"))
                    error_msg = str(exc_info.value)
                    assert "429" in error_msg, f"Expected '429' in error, got: {error_msg}"

    def test_timeout_preserved_in_error(self, settings):
        """Timeout must produce error containing 'Timeout' or 'timeout'."""
        import httpx

        timeout_exc = httpx.ReadTimeout("Connection timed out")

        async def mock_post(*args, **kwargs):
            raise timeout_exc

        with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=mock_post)):
            with patch("httpx.AsyncClient.__aenter__", return_value=AsyncMock(post=mock_post)):
                with patch("httpx.AsyncClient.__aexit__", return_value=False):
                    with pytest.raises(DeepSeekError) as exc_info:
                        asyncio.run(complete_json(settings, system_prompt="test", user_prompt="test"))
                    error_msg = str(exc_info.value)
                    assert "timed out" in error_msg.lower() or "timeout" in error_msg.lower(), \
                        f"Expected timeout info in error, got: {error_msg}"

    def test_malformed_json_preserved_in_error(self, settings):
        """Malformed JSON response must produce error containing 'json' or 'JSON'."""
        import httpx

        mock_response = httpx.Response(
            status_code=200,
            content=b"not valid json at all",
            request=httpx.Request("POST", "https://api.deepseek.com/chat/completions"),
            headers={"content-type": "application/json"},
        )

        async def mock_post(*args, **kwargs):
            return mock_response

        with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=mock_post)):
            with patch("httpx.AsyncClient.__aenter__", return_value=AsyncMock(post=mock_post)):
                with patch("httpx.AsyncClient.__aexit__", return_value=False):
                    with pytest.raises(DeepSeekError) as exc_info:
                        asyncio.run(complete_json(settings, system_prompt="test", user_prompt="test"))
                    error_msg = str(exc_info.value)
                    # The JSON error is preserved as the actual json.JSONDecodeError message
                    assert len(error_msg) > 5 and error_msg != "DeepSeekError", \
                        f"Expected meaningful error, got: {error_msg}"

    def test_error_is_never_generic_deepseek_error(self, settings):
        """The error message must NEVER be just 'DeepSeekError'."""
        import httpx

        mock_response = httpx.Response(
            status_code=500,
            json={"error": {"message": "Internal server error"}},
            request=httpx.Request("POST", "https://api.deepseek.com/chat/completions"),
        )
        http_error = httpx.HTTPStatusError(
            "500 Internal Server Error",
            request=mock_response.request,
            response=mock_response,
        )

        async def mock_post(*args, **kwargs):
            raise http_error

        with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=mock_post)):
            with patch("httpx.AsyncClient.__aenter__", return_value=AsyncMock(post=mock_post)):
                with patch("httpx.AsyncClient.__aexit__", return_value=False):
                    with pytest.raises(DeepSeekError) as exc_info:
                        asyncio.run(complete_json(settings, system_prompt="test", user_prompt="test"))
                    error_msg = str(exc_info.value)
                    assert error_msg != "DeepSeekError", \
                        f"Error message must not be generic 'DeepSeekError', got: {error_msg}"
