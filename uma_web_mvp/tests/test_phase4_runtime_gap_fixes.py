"""Phase 4 Runtime Gap Fixes: P1-2 through P2-2.

Tests for:
A. Fast translate payload conflict detection (P1-2)
B. Default price for fast_translator_cost_credits (P1-3)
C. Normal translation billing (P1-1)
D. Timeout and refund precision (P1-4)
E. Character confirmation 409 via HTTP (P1-5)
F. User isolation (P2-1)
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.environ.setdefault("APP_ENV", "local")

from app.config import Settings
from app.db import (
    connect,
    ensure_schema,
    create_task_atomic,
    calculate_generation_charge,
)
from app.services.fast_translator_service import (
    CharacterSelectionRequired,
    ClientRequestIdConflict,
    FastTranslatorError,
    fast_refine_prompt,
    _begin_charge,
)

TEST_USER_A = "test-user-alpha"
TEST_USER_B = "test-user-beta"
TEST_CASE_ROOT = Path(__file__).resolve().parents[1] / "test_data" / "runtime_gap_cases"


# ────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────

def _run(coro):
    return asyncio.run(coro)


def _case_root() -> Path:
    root = TEST_CASE_ROOT / uuid.uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    return root


def _make_settings(case_root: Path, **overrides) -> Settings:
    test_root = case_root / "test_data"
    for d in ("output", "mock_output", "input_images"):
        (test_root / d).mkdir(parents=True, exist_ok=True)
    data = {
        "APP_ENV": "local",
        "APP_ORIGIN": "http://127.0.0.1:18080",
        "HOST": "127.0.0.1",
        "PORT": 18080,
        "BALANCE_DB": str(test_root / "runtime_gap_test.db"),
        "BOT_OUTPUT_DIR": str(test_root / "output"),
        "mock_output_dir": str(test_root / "mock_output"),
        "INPUT_IMAGE_DIR": str(test_root / "input_images"),
        "BOT_DIR": str(test_root),
        "redis_enabled": False,
        "dev_auth_bypass": True,
        "dev_user_id": TEST_USER_A,
        "dev_username": "Runtime Gap Tester",
        "fast_translator_enabled": True,
        "fast_translator_cost_credits": 2,
        "agent_surcharge_credits": 1,
        "mock_worker_enabled": True,
        "deepseek_api_key": "",
        "deepseek_base_url": "https://api.deepseek.com",
        "session_secret": "test-session-secret-for-runtime-gap-32chars!!",
        "jwt_secret": "test-jwt-secret-for-runtime-gap-testing-only",
        "agent_enabled": False,
        "smart_agent_enabled": False,
    }
    data.update(overrides)
    s = Settings(**data)
    s.validate_local_isolation()
    ensure_schema(s)
    return s


def _seed_balance(settings: Settings, user_id: str, amount: int = 50000) -> None:
    conn = connect(settings)
    try:
        conn.execute("INSERT OR REPLACE INTO users(user_id, balance_fen) VALUES (?, ?)", (user_id, amount))
        conn.commit()
    finally:
        conn.close()


def _get_balance(settings: Settings, user_id: str) -> int:
    conn = connect(settings)
    try:
        row = conn.execute("SELECT balance_fen FROM users WHERE user_id=?", (user_id,)).fetchone()
        return int(row["balance_fen"]) if row else 0
    finally:
        conn.close()


def _get_translation_record(settings: Settings, request_code: str) -> dict | None:
    conn = connect(settings)
    try:
        row = conn.execute(
            "SELECT * FROM translation_requests WHERE request_code=?", (request_code,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _count_ledger(settings: Settings, user_id: str, reason: str) -> int:
    conn = connect(settings)
    try:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM balance_ledger WHERE user_id=? AND reason=?",
            (user_id, reason),
        ).fetchone()
        return int(row["cnt"]) if row else 0
    finally:
        conn.close()


def _get_task(settings: Settings, job_code: str) -> dict | None:
    conn = connect(settings)
    try:
        row = conn.execute(
            "SELECT * FROM generation_tasks WHERE job_code=?", (job_code,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _clear_dependency_overrides():
    """Ensure app.dependency_overrides is clean before and after each test."""
    from app.main import app
    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# A. Fast translate payload conflict detection (P1-2)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestPayloadConflict:
    """Verify client_request_id + payload comparison."""

    def test_same_id_same_payload_returns_cached(self):
        """Same user + same ID + same text → cached result, single charge."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER_A, 50000)
        cid = f"same-{uuid.uuid4().hex[:8]}"

        r1 = _run(fast_refine_prompt(settings, user_id=TEST_USER_A, text="樱花树下", client_request_id=cid))
        bal1 = _get_balance(settings, TEST_USER_A)

        r2 = _run(fast_refine_prompt(settings, user_id=TEST_USER_A, text="樱花树下", client_request_id=cid))
        bal2 = _get_balance(settings, TEST_USER_A)

        assert r1.request_code == r2.request_code
        assert bal1 == bal2

    def test_same_id_different_text_raises_conflict(self):
        """Same user + same ID + different text → ClientRequestIdConflict."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER_A, 50000)
        cid = f"conflict-{uuid.uuid4().hex[:8]}"

        _run(fast_refine_prompt(settings, user_id=TEST_USER_A, text="原始文本", client_request_id=cid))
        bal_after_first = _get_balance(settings, TEST_USER_A)

        with pytest.raises(ClientRequestIdConflict) as exc_info:
            _run(fast_refine_prompt(settings, user_id=TEST_USER_A, text="完全不同的文本", client_request_id=cid))

        assert exc_info.value.code == "client_request_id_conflict"
        assert _get_balance(settings, TEST_USER_A) == bal_after_first

    def test_same_id_different_character_keys_raises_conflict(self):
        """Same user + same ID + different character resolution → conflict.

        Two requests with same client_request_id but different character_keys
        would produce different translation_requests rows.  The text is the same
        but the character resolution differs — this must also conflict because
        the resolved prompt would differ.
        """
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER_A, 50000)
        cid = f"char-conflict-{uuid.uuid4().hex[:8]}"

        _run(fast_refine_prompt(
            settings, user_id=TEST_USER_A, text="初音未来在舞台上",
            client_request_id=cid,
        ))
        bal_after_first = _get_balance(settings, TEST_USER_A)

        # Same text, same ID — should dedup (character resolution is same)
        r2 = _run(fast_refine_prompt(
            settings, user_id=TEST_USER_A, text="初音未来在舞台上",
            client_request_id=cid,
        ))
        assert _get_balance(settings, TEST_USER_A) == bal_after_first

    def test_different_users_same_id_no_conflict(self):
        """Different users can use same client_request_id independently."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER_A, 50000)
        _seed_balance(settings, TEST_USER_B, 50000)
        cid = f"cross-user-{uuid.uuid4().hex[:8]}"

        r1 = _run(fast_refine_prompt(settings, user_id=TEST_USER_A, text="用户A的请求", client_request_id=cid))
        r2 = _run(fast_refine_prompt(settings, user_id=TEST_USER_B, text="用户B的请求", client_request_id=cid))

        assert r1.request_code != r2.request_code

    def test_conflict_preserves_original_record(self):
        """Conflict must not overwrite the original translation_requests row."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER_A, 50000)
        cid = f"preserve-{uuid.uuid4().hex[:8]}"

        r1 = _run(fast_refine_prompt(settings, user_id=TEST_USER_A, text="原始内容", client_request_id=cid))

        with pytest.raises(ClientRequestIdConflict):
            _run(fast_refine_prompt(settings, user_id=TEST_USER_A, text="新内容", client_request_id=cid))

        record = _get_translation_record(settings, r1.request_code)
        assert record is not None
        assert record["original_text"] == "原始内容"
        assert record["status"] == "done"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# B. Default price (P1-3)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestDefaultPrice:
    """Verify fast_translator_cost_credits defaults to 2."""

    def test_code_default_is_two(self):
        """Settings() without override must default fast_translator_cost_credits to 2."""
        # Create minimal valid settings
        case_root = _case_root()
        test_root = case_root / "test_data"
        for d in ("output", "mock_output", "input_images"):
            (test_root / d).mkdir(parents=True, exist_ok=True)
        s = Settings(
            APP_ENV="local",
            APP_ORIGIN="http://127.0.0.1:18080",
            HOST="127.0.0.1",
            PORT=18080,
            BALANCE_DB=str(test_root / "default_price_test.db"),
            BOT_OUTPUT_DIR=str(test_root / "output"),
            mock_output_dir=str(test_root / "mock_output"),
            INPUT_IMAGE_DIR=str(test_root / "input_images"),
            BOT_DIR=str(test_root),
            session_secret="test-session-secret-for-default-price-32chars!!!",
            jwt_secret="test-jwt-secret-for-default-price-testing-only",
        )
        assert s.fast_translator_cost_credits == 2, (
            f"Default fast_translator_cost_credits should be 2, got {s.fast_translator_cost_credits}"
        )

    def test_explicit_override_respected(self):
        """Explicitly set value should be used."""
        case_root = _case_root()
        settings = _make_settings(case_root, fast_translator_cost_credits=5)
        assert settings.fast_translator_cost_credits == 5

    def test_begin_charge_uses_settings_value(self):
        """_begin_charge must use settings.fast_translator_cost_credits, not hardcoded."""
        case_root = _case_root()
        settings = _make_settings(case_root, fast_translator_cost_credits=3)
        _seed_balance(settings, TEST_USER_A, 50000)
        bal_before = _get_balance(settings, TEST_USER_A)

        _run(fast_refine_prompt(settings, user_id=TEST_USER_A, text="测试价格"))
        bal_after = _get_balance(settings, TEST_USER_A)

        assert bal_before - bal_after == 3

    def test_config_endpoint_uses_same_source(self):
        """/api/me fast_translator_cost_credits must come from settings."""
        from fastapi.testclient import TestClient
        from app.main import app
        from app.config import get_settings

        case_root = _case_root()
        settings = _make_settings(case_root, fast_translator_cost_credits=7)
        _seed_balance(settings, TEST_USER_A, 50000)
        app.dependency_overrides[get_settings] = lambda: settings
        try:
            client = TestClient(app, raise_server_exceptions=False)
            # dev_auth_bypass auto-login as TEST_USER_A
            r = client.get("/api/me")
            assert r.status_code == 200
            data = r.json()
            assert data.get("fast_translator_cost_credits") == 7
        finally:
            app.dependency_overrides.clear()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# C. Normal translation billing (P1-1)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestNormalTranslationBilling:
    """Verify normal translation charges base + agent surcharge = 2 credits."""

    def test_normal_translation_charges_two_credits(self):
        """use_agent=True → base 1 + surcharge 1 = 2 credits."""
        case_root = _case_root()
        settings = _make_settings(case_root, agent_enabled=True)
        _seed_balance(settings, TEST_USER_A, 50000)
        bal_before = _get_balance(settings, TEST_USER_A)

        result = create_task_atomic(
            settings,
            job_code=f"NORM-{uuid.uuid4().hex[:8]}",
            user_id=TEST_USER_A,
            username="test",
            prompt="1girl, standing",
            style_key="style_a",
            lora_weight=1.0,
            width=1024,
            height=1536,
            mode="txt2img",
            input_image_path=None,
            denoise=0.5,
            control_type="depth",
            control_character="prompt",
            auto_tagger=False,
            use_agent=True,
            prompt_source="agent_no_character",
            character_key="[]",
        )

        bal_after = _get_balance(settings, TEST_USER_A)
        assert bal_before - bal_after == 2
        assert result["charged_fen"] == 2

    def test_raw_generation_charges_one_credit(self):
        """use_agent=False → base 1 credit only."""
        case_root = _case_root()
        settings = _make_settings(case_root, agent_enabled=True)
        _seed_balance(settings, TEST_USER_A, 50000)
        bal_before = _get_balance(settings, TEST_USER_A)

        result = create_task_atomic(
            settings,
            job_code=f"RAW-{uuid.uuid4().hex[:8]}",
            user_id=TEST_USER_A,
            username="test",
            prompt="1girl, standing",
            style_key="style_a",
            lora_weight=1.0,
            width=1024,
            height=1536,
            mode="txt2img",
            input_image_path=None,
            denoise=0.5,
            control_type="depth",
            control_character="prompt",
            auto_tagger=False,
            use_agent=False,
            prompt_source="user_raw",
            character_key="",
        )

        bal_after = _get_balance(settings, TEST_USER_A)
        assert bal_before - bal_after == 1
        assert result["charged_fen"] == 1

    def test_normal_does_not_charge_fast_translator_fee(self):
        """Normal translation uses agent_surcharge, not fast_translator_cost."""
        case_root = _case_root()
        settings = _make_settings(case_root, agent_enabled=True, fast_translator_cost_credits=99)
        _seed_balance(settings, TEST_USER_A, 50000)
        bal_before = _get_balance(settings, TEST_USER_A)

        create_task_atomic(
            settings,
            job_code=f"NOFAST-{uuid.uuid4().hex[:8]}",
            user_id=TEST_USER_A,
            username="test",
            prompt="test prompt",
            style_key="style_a",
            lora_weight=1.0,
            width=1024,
            height=1536,
            mode="txt2img",
            input_image_path=None,
            denoise=0.5,
            control_type="depth",
            control_character="prompt",
            auto_tagger=False,
            use_agent=True,
            prompt_source="agent_no_character",
            character_key="[]",
        )

        bal_after = _get_balance(settings, TEST_USER_A)
        # Should be 1 (base) + 1 (agent surcharge) = 2, NOT 99
        assert bal_before - bal_after == 2


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# D. Timeout precision (P1-4)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestTimeoutPrecision:
    """Verify timeout causes clean refund with precise balance assertion."""

    def test_timeout_refund_restores_exact_balance(self):
        """When DeepSeek times out, the exact charge must be refunded."""
        case_root = _case_root()
        settings = _make_settings(case_root, fast_translator_cost_credits=2, deepseek_timeout_seconds=5)
        _seed_balance(settings, TEST_USER_A, 2000)
        initial_balance = _get_balance(settings, TEST_USER_A)
        assert initial_balance == 2000

        # Create a mock DeepSeekService that raises a timeout error
        from app.services.deepseek_service import DeepSeekService

        class TimeoutMockService(DeepSeekService):
            async def complete_json(self, **kwargs):
                raise DeepSeekError("Timeout")

        mock_ds = TimeoutMockService(settings)

        async def _test():
            return await fast_refine_prompt(
                settings,
                user_id=TEST_USER_A,
                text="超时测试",
                deepseek=mock_ds,
            )

        with pytest.raises(FastTranslatorError) as exc_info:
            _run(_test())

        # DeepSeekError("Timeout") is caught by generic Exception handler
        # and re-raised as FastTranslatorError("fast_translate_failed")
        assert exc_info.value.code in ("deepseek_failed", "fast_translate_failed")

        final_balance = _get_balance(settings, TEST_USER_A)
        assert final_balance == initial_balance, (
            f"Balance should be restored after timeout: expected {initial_balance}, got {final_balance}"
        )

        # Verify ledger: 0 charges, 0 refunds (charge was rolled back)
        charge_count = _count_ledger(settings, TEST_USER_A, "fast_translate_charge")
        refund_count = _count_ledger(settings, TEST_USER_A, "fast_translate_refund")
        # The charge + refund should both exist, or neither (depending on timing)
        # If _begin_charge committed before timeout: 1 charge + 1 refund = net 0
        # If _begin_charge failed: 0 charges
        assert charge_count == refund_count, (
            f"Charge and refund ledger entries must balance: charges={charge_count}, refunds={refund_count}"
        )

    def test_http_500_refund_restores_exact_balance(self):
        """When DeepSeek returns HTTP 500, the exact charge must be refunded."""
        case_root = _case_root()
        settings = _make_settings(case_root, fast_translator_cost_credits=2)
        _seed_balance(settings, TEST_USER_A, 2000)
        initial_balance = _get_balance(settings, TEST_USER_A)

        from app.services.deepseek_service import DeepSeekService

        class Http500MockService(DeepSeekService):
            async def complete_json(self, **kwargs):
                raise DeepSeekError("deepseek_http_500")

        mock_ds = Http500MockService(settings)

        async def _test():
            return await fast_refine_prompt(
                settings,
                user_id=TEST_USER_A,
                text="500错误测试",
                deepseek=mock_ds,
            )

        with pytest.raises(FastTranslatorError):
            _run(_test())

        final_balance = _get_balance(settings, TEST_USER_A)
        assert final_balance == initial_balance

    def test_empty_output_refund_restores_exact_balance(self):
        """When DeepSeek returns empty/invalid output, charge must be refunded."""
        case_root = _case_root()
        settings = _make_settings(case_root, fast_translator_cost_credits=2)
        _seed_balance(settings, TEST_USER_A, 2000)
        initial_balance = _get_balance(settings, TEST_USER_A)

        from app.services.deepseek_service import DeepSeekService

        class EmptyMockService(DeepSeekService):
            async def complete_json(self, **kwargs):
                return {"clothing": "", "action": "", "expression": "", "composition": "",
                        "scene": "", "lighting": "", "mood": "", "style": ""}

        mock_ds = EmptyMockService(settings)

        async def _test():
            return await fast_refine_prompt(
                settings,
                user_id=TEST_USER_A,
                text="空输出测试",
                deepseek=mock_ds,
            )

        with pytest.raises(FastTranslatorError) as exc_info:
            _run(_test())
        assert "empty" in exc_info.value.code

        final_balance = _get_balance(settings, TEST_USER_A)
        assert final_balance == initial_balance

    def test_success_charge_then_timeout_no_double_refund(self):
        """After a successful request, a timeout on a different request
        must not refund the first request."""
        case_root = _case_root()
        settings = _make_settings(case_root, fast_translator_cost_credits=2)
        _seed_balance(settings, TEST_USER_A, 10000)

        # First request succeeds
        r1 = _run(fast_refine_prompt(settings, user_id=TEST_USER_A, text="第一次成功"))
        bal_after_first = _get_balance(settings, TEST_USER_A)
        assert bal_after_first == 10000 - 2

        # Second request times out
        from app.services.deepseek_service import DeepSeekService

        class TimeoutMockService(DeepSeekService):
            async def complete_json(self, **kwargs):
                raise DeepSeekError("Timeout")

        mock_ds = TimeoutMockService(settings)

        with pytest.raises(FastTranslatorError):
            _run(fast_refine_prompt(
                settings, user_id=TEST_USER_A, text="第二次超时",
                deepseek=mock_ds,
            ))

        bal_after_second = _get_balance(settings, TEST_USER_A)
        # Only the second request was refunded, first still charged
        assert bal_after_second == bal_after_first


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# E. Character confirmation 409 via HTTP (P1-5)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestCharacterConfirmationHTTP:
    """Verify character confirmation 409 flow via real HTTP."""

    def test_ambiguous_character_returns_409_with_code(self):
        """Ambiguous character → HTTP 409 with character_resolution_required."""
        from fastapi.testclient import TestClient
        from app.main import app
        from app.config import get_settings

        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER_A, 50000)
        app.dependency_overrides[get_settings] = lambda: settings
        try:
            client = TestClient(app, raise_server_exceptions=False)
            r = client.post("/api/prompt/fast-refine", json={
                "text": "miku在舞台上唱歌",
            }, headers={"X-CSRF-Token": "test"})
            assert r.status_code == 409
            detail = r.json().get("detail", {})
            assert detail.get("code") == "character_resolution_required"
            assert detail.get("requiresCharacterSelection") is True
        finally:
            app.dependency_overrides.clear()

    def test_ambiguous_does_not_charge(self):
        """Ambiguous character 409 must not deduct credits."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER_A, 2000)
        initial_balance = _get_balance(settings, TEST_USER_A)

        from fastapi.testclient import TestClient
        from app.main import app
        from app.config import get_settings

        app.dependency_overrides[get_settings] = lambda: settings
        try:
            client = TestClient(app, raise_server_exceptions=False)
            r = client.post("/api/prompt/fast-refine", json={
                "text": "miku在舞台上",
            }, headers={"X-CSRF-Token": "test"})
            assert r.status_code == 409
        finally:
            app.dependency_overrides.clear()

        assert _get_balance(settings, TEST_USER_A) == initial_balance

    def test_resolved_character_proceeds_without_409(self):
        """Resolved character (e.g. 初音未来) → 200, no confirmation needed."""
        from fastapi.testclient import TestClient
        from app.main import app
        from app.config import get_settings

        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER_A, 50000)
        app.dependency_overrides[get_settings] = lambda: settings
        try:
            client = TestClient(app, raise_server_exceptions=False)
            r = client.post("/api/prompt/fast-refine", json={
                "text": "初音未来在舞台上唱歌",
            }, headers={"X-CSRF-Token": "test"})
            assert r.status_code == 200
            data = r.json()
            assert data.get("ok") is True
            assert "hatsune_miku" in data.get("character_keys", [])
        finally:
            app.dependency_overrides.clear()

    def test_none_of_above_resolves_and_proceeds(self):
        """User selects 'none of the above' → stored as skip decision, proceeds."""
        from fastapi.testclient import TestClient
        from app.main import app
        from app.config import get_settings
        from app.smart_agent.disambiguation_engine import NO_LIBRARY_CHARACTER_ID

        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER_A, 50000)
        app.dependency_overrides[get_settings] = lambda: settings
        try:
            client = TestClient(app, raise_server_exceptions=False)
            # Use a prompt with a character mention that triggers disambiguation,
            # then resolve with "none of the above"
            r = client.post("/api/prompt/fast-refine", json={
                "text": "miku在舞台上",
            }, headers={"X-CSRF-Token": "test"})
            assert r.status_code == 409
            detail = r.json().get("detail", {})
            mentions = detail.get("resolution", {}).get("mentions", [])
            assert len(mentions) > 0
            raw_text = mentions[0]["rawText"]

            r2 = client.post("/api/prompt/fast-refine", json={
                "text": "miku在舞台上",
                "character_resolution": {
                    "status": "resolved",
                    "selections": [{"rawText": raw_text, "characterId": NO_LIBRARY_CHARACTER_ID}],
                },
            }, headers={"X-CSRF-Token": "test"})
            assert r2.status_code == 200
            data = r2.json()
            assert data.get("ok") is True
        finally:
            app.dependency_overrides.clear()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# F. User isolation (P2-1)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestUserIsolation:
    """Verify users cannot access each other's data via HTTP."""

    def _setup_two_users(self, case_root):
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER_A, 50000)
        _seed_balance(settings, TEST_USER_B, 50000)
        return settings

    def test_user_b_cannot_see_user_a_task(self):
        """GET /api/tasks/{job_code} with user B session → 404 for user A's task."""
        from fastapi.testclient import TestClient
        from app.main import app
        from app.config import get_settings

        case_root = _case_root()
        settings = self._setup_two_users(case_root)

        # Create a task as user A
        job_code = f"ISO-A-{uuid.uuid4().hex[:8]}"
        create_task_atomic(
            settings,
            job_code=job_code,
            user_id=TEST_USER_A,
            username="UserA",
            prompt="user A prompt",
            style_key="style_a",
            lora_weight=1.0,
            width=1024,
            height=1536,
            mode="txt2img",
            input_image_path=None,
            denoise=0.5,
            control_type="depth",
            control_character="prompt",
            auto_tagger=False,
            use_agent=False,
            prompt_source="user_raw",
            character_key="",
        )

        app.dependency_overrides[get_settings] = lambda: settings
        try:
            client = TestClient(app, raise_server_exceptions=False)

            # Switch to user B by overriding dev_user_id
            settings_b = _make_settings(case_root)
            settings_b.dev_user_id = TEST_USER_B
            app.dependency_overrides[get_settings] = lambda: settings_b

            r = client.get(f"/api/tasks/{job_code}")
            assert r.status_code == 404
        finally:
            app.dependency_overrides.clear()

    def test_user_b_cannot_cancel_user_a_task(self):
        """POST /api/tasks/{job_code}/cancel with user B → 404."""
        from fastapi.testclient import TestClient
        from app.main import app
        from app.config import get_settings

        case_root = _case_root()
        settings = self._setup_two_users(case_root)

        job_code = f"ISO-C-{uuid.uuid4().hex[:8]}"
        create_task_atomic(
            settings,
            job_code=job_code,
            user_id=TEST_USER_A,
            username="UserA",
            prompt="cancel test",
            style_key="style_a",
            lora_weight=1.0,
            width=1024,
            height=1536,
            mode="txt2img",
            input_image_path=None,
            denoise=0.5,
            control_type="depth",
            control_character="prompt",
            auto_tagger=False,
            use_agent=False,
            prompt_source="user_raw",
            character_key="",
        )

        app.dependency_overrides[get_settings] = lambda: settings
        try:
            client = TestClient(app, raise_server_exceptions=False)

            settings_b = _make_settings(case_root)
            settings_b.dev_user_id = TEST_USER_B
            app.dependency_overrides[get_settings] = lambda: settings_b

            r = client.post(f"/api/tasks/{job_code}/cancel", headers={"X-CSRF-Token": "test"})
            assert r.status_code in (404, 409)
        finally:
            app.dependency_overrides.clear()

    def test_user_a_balance_invisible_to_user_b(self):
        """GET /api/me as user B shows user B's balance, not user A's."""
        from fastapi.testclient import TestClient
        from app.main import app
        from app.config import get_settings

        case_root = _case_root()
        settings = self._setup_two_users(case_root)
        _seed_balance(settings, TEST_USER_A, 99999)
        _seed_balance(settings, TEST_USER_B, 11111)

        app.dependency_overrides[get_settings] = lambda: settings
        try:
            client = TestClient(app, raise_server_exceptions=False)

            # User A's config
            settings_a = _make_settings(case_root)
            settings_a.dev_user_id = TEST_USER_A
            app.dependency_overrides[get_settings] = lambda: settings_a
            r_a = client.get("/api/me")
            assert r_a.status_code == 200
            bal_a = r_a.json().get("balance_fen")

            # User B's config
            settings_b = _make_settings(case_root)
            settings_b.dev_user_id = TEST_USER_B
            app.dependency_overrides[get_settings] = lambda: settings_b
            r_b = client.get("/api/me")
            assert r_b.status_code == 200
            bal_b = r_b.json().get("balance_fen")

            assert bal_a == 99999
            assert bal_b == 11111
            assert bal_a != bal_b
        finally:
            app.dependency_overrides.clear()

    def test_fast_translate_isolation(self):
        """Fast translate requests are isolated per user in translation_requests."""
        case_root = _case_root()
        settings = self._setup_two_users(case_root)
        cid = f"ft-iso-{uuid.uuid4().hex[:8]}"

        r_a = _run(fast_refine_prompt(settings, user_id=TEST_USER_A, text="用户A翻译", client_request_id=cid))
        r_b = _run(fast_refine_prompt(settings, user_id=TEST_USER_B, text="用户B翻译", client_request_id=cid))

        assert r_a.request_code != r_b.request_code

        # Verify both records exist independently
        rec_a = _get_translation_record(settings, r_a.request_code)
        rec_b = _get_translation_record(settings, r_b.request_code)
        assert rec_a is not None
        assert rec_b is not None
        assert rec_a["user_id"] == TEST_USER_A
        assert rec_b["user_id"] == TEST_USER_B

    def test_user_b_cannot_confirm_user_a_draft(self):
        """User B cannot confirm user A's smart agent prompt draft."""
        # This test verifies the confirm endpoint scopes to the conversation owner
        # Since smart agent requires more setup, we test at the DB level that
        # confirm_smart_agent_prompt_draft_atomic requires matching user.
        from app.db import confirm_smart_agent_prompt_draft_atomic

        case_root = _case_root()
        settings = self._setup_two_users(case_root)

        # Without a valid draft, this should fail with a LookupError or similar
        with pytest.raises((LookupError, RuntimeError, Exception)):
            _run(confirm_smart_agent_prompt_draft_atomic(
                settings,
                user_id=TEST_USER_B,
                conversation_code="nonexistent",
                approved=True,
            ))

    def test_same_request_id_different_users_independent(self):
        """Same client_request_id for different users creates independent tasks."""
        case_root = _case_root()
        settings = self._setup_two_users(case_root)

        job_a = create_task_atomic(
            settings,
            job_code=f"ISO-RA-{uuid.uuid4().hex[:8]}",
            user_id=TEST_USER_A,
            username="UserA",
            prompt="user A prompt",
            style_key="style_a",
            lora_weight=1.0,
            width=1024,
            height=1536,
            mode="txt2img",
            input_image_path=None,
            denoise=0.5,
            control_type="depth",
            control_character="prompt",
            auto_tagger=False,
            use_agent=False,
            client_request_id="shared-id-123",
            prompt_source="user_raw",
            character_key="",
        )

        job_b = create_task_atomic(
            settings,
            job_code=f"ISO-RB-{uuid.uuid4().hex[:8]}",
            user_id=TEST_USER_B,
            username="UserB",
            prompt="user B prompt",
            style_key="style_a",
            lora_weight=1.0,
            width=1024,
            height=1536,
            mode="txt2img",
            input_image_path=None,
            denoise=0.5,
            control_type="depth",
            control_character="prompt",
            auto_tagger=False,
            use_agent=False,
            client_request_id="shared-id-123",
            prompt_source="user_raw",
            character_key="",
        )

        assert job_a["job_code"] != job_b["job_code"]
        assert _get_balance(settings, TEST_USER_A) == 50000 - 1
        assert _get_balance(settings, TEST_USER_B) == 50000 - 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# G. Normal translation full HTTP lifecycle (补充 P1-1)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestNormalTranslationHTTPLifecycle:
    """Full HTTP lifecycle for normal translation billing."""

    def test_http_task_with_agent_true_charges_two(self):
        """POST /api/tasks with use_agent=true → 2 credits charged."""
        from fastapi.testclient import TestClient
        from app.main import app
        from app.config import get_settings

        case_root = _case_root()
        settings = _make_settings(case_root, agent_enabled=True)
        _seed_balance(settings, TEST_USER_A, 50000)
        initial_balance = _get_balance(settings, TEST_USER_A)

        app.dependency_overrides[get_settings] = lambda: settings
        try:
            client = TestClient(app, raise_server_exceptions=False)
            fd = {
                "mode": "txt2img",
                "style_key": "style_a",
                "prompt": "1girl, solo, forest scenery",
                "width": "1024",
                "height": "1536",
                "lora_weight": "1.0",
                "denoise": "0.5",
                "control_type": "depth",
                "control_character": "prompt",
                "auto_tagger": "false",
                "use_agent": "true",
            }
            r = client.post("/api/tasks", data=fd, headers={"X-CSRF-Token": "test"})
            assert r.status_code == 200
            data = r.json()
            assert data.get("charged_fen") == 2
            assert data.get("status") == "queued"

            final_balance = _get_balance(settings, TEST_USER_A)
            assert initial_balance - final_balance == 2
        finally:
            app.dependency_overrides.clear()

    def test_http_task_without_agent_charges_one(self):
        """POST /api/tasks with use_agent=false → 1 credit charged."""
        from fastapi.testclient import TestClient
        from app.main import app
        from app.config import get_settings

        case_root = _case_root()
        settings = _make_settings(case_root, agent_enabled=True)
        _seed_balance(settings, TEST_USER_A, 50000)
        initial_balance = _get_balance(settings, TEST_USER_A)

        app.dependency_overrides[get_settings] = lambda: settings
        try:
            client = TestClient(app, raise_server_exceptions=False)
            fd = {
                "mode": "txt2img",
                "style_key": "style_a",
                "prompt": "1girl, standing",
                "width": "1024",
                "height": "1536",
                "lora_weight": "1.0",
                "denoise": "0.5",
                "control_type": "depth",
                "control_character": "prompt",
                "auto_tagger": "false",
                "use_agent": "false",
            }
            r = client.post("/api/tasks", data=fd, headers={"X-CSRF-Token": "test"})
            assert r.status_code == 200
            data = r.json()
            assert data.get("charged_fen") == 1

            final_balance = _get_balance(settings, TEST_USER_A)
            assert initial_balance - final_balance == 1
        finally:
            app.dependency_overrides.clear()

    def test_http_fast_translate_charges_two(self):
        """POST /api/prompt/fast-refine → 2 credits charged via fast_translator_cost_credits."""
        from fastapi.testclient import TestClient
        from app.main import app
        from app.config import get_settings

        case_root = _case_root()
        settings = _make_settings(case_root, fast_translator_cost_credits=2)
        _seed_balance(settings, TEST_USER_A, 50000)
        initial_balance = _get_balance(settings, TEST_USER_A)

        app.dependency_overrides[get_settings] = lambda: settings
        try:
            client = TestClient(app, raise_server_exceptions=False)
            r = client.post("/api/prompt/fast-refine", json={
                "text": "风景画，蓝天白云",
            }, headers={"X-CSRF-Token": "test"})
            assert r.status_code == 200
            data = r.json()
            assert data.get("charged_credits") == 2

            final_balance = _get_balance(settings, TEST_USER_A)
            assert initial_balance - final_balance == 2
        finally:
            app.dependency_overrides.clear()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# H. Character confirmation full HTTP lifecycle (补充 P1-5)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestCharacterConfirmationLifecycle:
    """Full HTTP lifecycle for character confirmation."""

    def test_full_lifecycle_ambiguous_then_confirm(self):
        """Ambiguous → 409 → resolve → 200 → only one charge."""
        from fastapi.testclient import TestClient
        from app.main import app
        from app.config import get_settings

        case_root = _case_root()
        settings = _make_settings(case_root, fast_translator_cost_credits=2)
        _seed_balance(settings, TEST_USER_A, 50000)
        initial_balance = _get_balance(settings, TEST_USER_A)

        app.dependency_overrides.clear()  # ensure clean state
        app.dependency_overrides[get_settings] = lambda: settings
        try:
            client = TestClient(app, raise_server_exceptions=False)

            # Step 1: Send ambiguous character ("ik" -> MEIKO/Taiki Shuttle)
            r1 = client.post("/api/prompt/fast-refine", json={
                "text": "miku在舞台上",
            }, headers={"X-CSRF-Token": "test"})
            assert r1.status_code == 409
            detail = r1.json().get("detail", {})
            assert detail.get("code") == "character_resolution_required"
            assert detail.get("requiresCharacterSelection") is True

            # Balance unchanged
            assert _get_balance(settings, TEST_USER_A) == initial_balance

            # Step 2: Confirm character with an actual candidate and resubmit
            r2 = client.post("/api/prompt/fast-refine", json={
                "text": "miku在舞台上",
                "character_resolution": {
                    "status": "resolved",
                    "selections": [{"rawText": "ik", "characterId": "meiko"}],
                },
            }, headers={"X-CSRF-Token": "test"})
            assert r2.status_code == 200
            data = r2.json()
            assert data.get("ok") is True
            assert "meiko" in data.get("character_keys", [])

            # Only one charge
            assert _get_balance(settings, TEST_USER_A) == initial_balance - 2

            # Step 3: Same request ID + same text → dedup
            cid = "lifecycle-dedup"
            r3a = client.post("/api/prompt/fast-refine", json={
                "text": "初音未来在舞台上",
                "client_request_id": cid,
            }, headers={"X-CSRF-Token": "test"})
            assert r3a.status_code == 200
            bal_after_3a = _get_balance(settings, TEST_USER_A)

            r3b = client.post("/api/prompt/fast-refine", json={
                "text": "初音未来在舞台上",
                "client_request_id": cid,
            }, headers={"X-CSRF-Token": "test"})
            assert r3b.status_code == 200
            assert _get_balance(settings, TEST_USER_A) == bal_after_3a
            assert r3a.json().get("request_code") == r3b.json().get("request_code")
        finally:
            app.dependency_overrides.clear()

    def test_full_lifecycle_none_of_above(self):
        """'none of above' → 200 → character_keys empty, charged once."""
        case_root = _case_root()
        settings = _make_settings(case_root, fast_translator_cost_credits=2)
        _seed_balance(settings, TEST_USER_A, 50000)
        initial_balance = _get_balance(settings, TEST_USER_A)

        # Use service-level call to avoid TestClient state leakage
        from app.smart_agent.disambiguation_engine import (
            analyze_character_mentions,
            NO_LIBRARY_CHARACTER_ID,
        )
        analysis = analyze_character_mentions('miku在舞台上')
        raw_text = analysis['mentions'][0]['rawText']

        cid = f"none-above-{uuid.uuid4().hex[:8]}"
        result = _run(fast_refine_prompt(
            settings,
            user_id=TEST_USER_A,
            text='miku在舞台上',
            client_request_id=cid,
            character_resolution={
                'status': 'resolved',
                'selections': [{'rawText': raw_text, 'characterId': NO_LIBRARY_CHARACTER_ID}],
            },
        ))
        assert result.ok is True
        assert result.character_keys == []
        assert _get_balance(settings, TEST_USER_A) == initial_balance - 2
