"""Tests for app/agent.py: timeout handling, retry, semaphore, error classification."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

# ---------------------------------------------------------------------------
# classify_agent_error (in bot_web_mvp.py, tested via import)
# ---------------------------------------------------------------------------


def _classify(exc: Exception) -> str:
    """Import classify_agent_error from bot_web_mvp.py without side effects."""
    import importlib, sys, types

    # bot_web_mvp.py has heavy side effects; extract the function directly
    # by reading source and exec-ing just the function.
    import pathlib, textwrap

    src = pathlib.Path(r"E:\discord-BOT\bot_web_mvp.py").read_text(encoding="utf-8")
    # Extract function body
    start = src.index("\ndef classify_agent_error(")
    # Find the next top-level def or end
    rest = src[start + 1 :]
    end = rest.index("\ndef ", 1) if "\ndef " in rest[1:] else len(rest)
    func_src = rest[:end].strip()

    ns: dict = {"httpx": httpx}
    exec(func_src, ns)
    return ns["classify_agent_error"](exc)


class TestClassifyAgentError:
    def test_read_timeout(self):
        assert _classify(httpx.ReadTimeout("read timeout")) == "agent_timeout"

    def test_connect_timeout(self):
        assert _classify(httpx.ConnectTimeout("connect timeout")) == "agent_timeout"

    def test_connect_error(self):
        assert _classify(httpx.ConnectError("refused")) == "agent_connect_error"

    def test_http_429(self):
        req = httpx.Request("POST", "http://x")
        resp = httpx.Response(429, request=req)
        assert _classify(httpx.HTTPStatusError("429", request=req, response=resp)) == "agent_rate_limited"

    def test_http_503(self):
        req = httpx.Request("POST", "http://x")
        resp = httpx.Response(503, request=req)
        assert _classify(httpx.HTTPStatusError("503", request=req, response=resp)) == "agent_server_error"

    def test_http_401(self):
        req = httpx.Request("POST", "http://x")
        resp = httpx.Response(401, request=req)
        assert _classify(httpx.HTTPStatusError("401", request=req, response=resp)) == "agent_auth_error"

    def test_http_400(self):
        req = httpx.Request("POST", "http://x")
        resp = httpx.Response(400, request=req)
        assert _classify(httpx.HTTPStatusError("400", request=req, response=resp)) == "agent_api_error"

    def test_unknown_exception(self):
        assert _classify(RuntimeError("something")) == "agent_unavailable"

    def test_agent_busy(self):
        assert _classify(RuntimeError("Agent 正忙，请稍后再试")) == "agent_busy"


# ---------------------------------------------------------------------------
# refine_prompt retry logic
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestRefinePromptRetry:
    """Test retry behavior in refine_prompt."""

    async def _call_refine(self, settings_mock, side_effects):
        """Call refine_prompt with mocked _refine_prompt_ollama."""
        import app.agent as agent_mod

        call_count = 0

        async def mock_ollama(s, text):
            nonlocal call_count
            result = side_effects[call_count]
            call_count += 1
            if isinstance(result, Exception):
                raise result
            return result

        with patch.object(agent_mod, "_refine_prompt_ollama", side_effect=mock_ollama):
            with patch.object(agent_mod, "_apply_character_registry_to_refined_prompt", side_effect=lambda t, p, **kw: p):
                return await agent_mod.refine_prompt(settings_mock, "test prompt")

    @pytest.fixture
    def settings(self):
        s = MagicMock()
        s.agent_enabled = True
        s.agent_provider = "ollama"
        s.agent_model = "test-model"
        s.agent_base_url = "http://127.0.0.1:11434"
        s.agent_api_key = ""
        s.agent_timeout_seconds = 120
        s.agent_keep_alive = "0"
        return s

    async def test_first_timeout_second_success(self, settings):
        """ReadTimeout on first attempt, success on second."""
        result = await self._call_refine(settings, [httpx.ReadTimeout("timeout"), "1girl, solo"])
        assert result == "1girl, solo"

    async def test_two_timeouts_raises(self, settings):
        """Two consecutive ReadTimeouts should raise."""
        with pytest.raises(httpx.ReadTimeout):
            await self._call_refine(settings, [httpx.ReadTimeout("timeout"), httpx.ReadTimeout("timeout")])

    async def test_http_400_no_retry(self, settings):
        """HTTP 400 should not be retried."""
        req = httpx.Request("POST", "http://x")
        resp = httpx.Response(400, request=req)
        exc = httpx.HTTPStatusError("400", request=req, response=resp)
        with pytest.raises(httpx.HTTPStatusError):
            await self._call_refine(settings, [exc])

    async def test_http_503_retries(self, settings):
        """HTTP 503 should be retried."""
        req = httpx.Request("POST", "http://x")
        resp = httpx.Response(503, request=req)
        exc = httpx.HTTPStatusError("503", request=req, response=resp)
        result = await self._call_refine(settings, [exc, "1girl, solo"])
        assert result == "1girl, solo"

    async def test_http_429_retries(self, settings):
        """HTTP 429 should be retried."""
        req = httpx.Request("POST", "http://x")
        resp = httpx.Response(429, request=req)
        exc = httpx.HTTPStatusError("429", request=req, response=resp)
        result = await self._call_refine(settings, [exc, "1girl, solo"])
        assert result == "1girl, solo"

    async def test_cancelled_not_swallowed(self, settings):
        """CancelledError must propagate, not be retried."""
        async def mock_ollama(s, text):
            raise asyncio.CancelledError()

        import app.agent as agent_mod
        with patch.object(agent_mod, "_refine_prompt_ollama", side_effect=mock_ollama):
            with pytest.raises(asyncio.CancelledError):
                await agent_mod.refine_prompt(settings, "test")


# ---------------------------------------------------------------------------
# Semaphore release
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestSemaphoreRelease:
    async def test_semaphore_released_after_exception(self):
        """Semaphore must be released even when an exception occurs."""
        import app.agent as agent_mod

        s = MagicMock()
        s.agent_enabled = True
        s.agent_provider = "ollama"
        s.agent_model = "test"
        s.agent_base_url = "http://127.0.0.1:11434"
        s.agent_api_key = ""
        s.agent_timeout_seconds = 10
        s.agent_keep_alive = "0"

        call_count = 0

        async def mock_ollama(s, text):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.ReadTimeout("timeout")
            return "ok"

        with patch.object(agent_mod, "_refine_prompt_ollama", side_effect=mock_ollama):
            with patch.object(agent_mod, "_apply_character_registry_to_refined_prompt", side_effect=lambda t, p, **kw: p):
                # First call: times out on first attempt, succeeds on retry
                await agent_mod.refine_prompt(s, "test1")
                assert call_count == 2

                # Semaphore should be released; second call should work
                call_count = 0
                async def mock_ollama2(s, text):
                    return "ok2"
                with patch.object(agent_mod, "_refine_prompt_ollama", side_effect=mock_ollama2):
                    result = await agent_mod.refine_prompt(s, "test2")
                    assert result == "ok2"


# ---------------------------------------------------------------------------
# Timeout configuration
# ---------------------------------------------------------------------------

class TestTimeoutConfig:
    def test_timeout_uses_httpx_timeout_object(self):
        """_refine_prompt_ollama should use httpx.Timeout with separate stages."""
        import app.agent as agent_mod
        import inspect

        src = inspect.getsource(agent_mod._refine_prompt_ollama)
        assert "httpx.Timeout(" in src
        assert "connect=" in src
        assert "read=" in src
        assert "write=" in src
        assert "pool=" in src

    def test_raises_httpx_status_error(self):
        """_refine_prompt_ollama should raise httpx.HTTPStatusError, not RuntimeError."""
        import app.agent as agent_mod
        import inspect

        src = inspect.getsource(agent_mod._refine_prompt_ollama)
        assert "httpx.HTTPStatusError" in src
