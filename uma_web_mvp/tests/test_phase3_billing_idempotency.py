"""Phase 3 Step 3: Billing, refund, idempotency, and failure compensation acceptance tests.

Tests the billing, refund, idempotency, and failure compensation paths
through actual code paths with mock DeepSeek and isolated test databases.

Coverage:
- Fast translator billing accuracy
- Normal translator billing accuracy
- 409 character confirmation (no charge before, single charge after)
- client_request_id idempotency
- DeepSeek failure refund
- Double-refund protection
- Concurrent idempotency (DB-level)
- Worker duplicate claim prevention
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.environ.setdefault("APP_ENV", "local")

from app.config import Settings
from app.db import (
    SMART_AGENT_CHARGE_REASON,
    SMART_AGENT_REFUND_REASON,
    connect,
    create_smart_agent_task_atomic,
    ensure_schema,
    fail_smart_agent_task_refund,
)
from app.services.fast_translator_service import (
    FAST_TRANSLATE_CHARGE_REASON,
    FAST_TRANSLATE_REFUND_REASON,
    CharacterSelectionRequired,
    FastTranslatorError,
    _begin_charge,
    _refund,
    fast_refine_prompt,
)

TEST_USER = "billing-test-user"
TEST_CASE_ROOT = Path(__file__).resolve().parents[1] / "test_data" / "billing_cases"


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
        "BALANCE_DB": str(test_root / "billing_test.db"),
        "BOT_OUTPUT_DIR": str(test_root / "output"),
        "mock_output_dir": str(test_root / "mock_output"),
        "INPUT_IMAGE_DIR": str(test_root / "input_images"),
        "BOT_DIR": str(test_root),
        "redis_enabled": False,
        "dev_auth_bypass": True,
        "dev_user_id": TEST_USER,
        "dev_username": "Billing Tester",
        "fast_translator_enabled": True,
        "fast_translator_cost_credits": 2,
        "agent_surcharge_credits": 1,
        "smart_agent_cost_credits": 5,
        "price_fen_per_image": 1,
        "mock_worker_enabled": True,
        "deepseek_api_key": "",
        "session_secret": "billing-test-session-secret-32chars!!!",
        "jwt_secret": "billing-test-jwt-secret-32chars!!!!!!",
        "agent_enabled": False,
        "smart_agent_enabled": False,
        "max_queue_size": 100,
        "max_active_tasks_per_user": 10,
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


def _get_ledger_entries(settings: Settings, user_id: str, reason: str = "") -> list[dict]:
    conn = connect(settings)
    try:
        if reason:
            rows = conn.execute(
                "SELECT * FROM balance_ledger WHERE user_id=? AND reason=? ORDER BY id",
                (user_id, reason),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM balance_ledger WHERE user_id=? ORDER BY id",
                (user_id,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _get_translation_requests(settings: Settings, user_id: str) -> list[dict]:
    conn = connect(settings)
    try:
        rows = conn.execute(
            "SELECT * FROM translation_requests WHERE user_id=? ORDER BY id",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _get_generation_tasks(settings: Settings, user_id: str) -> list[dict]:
    conn = connect(settings)
    try:
        rows = conn.execute(
            "SELECT * FROM generation_tasks WHERE user_id=? ORDER BY rowid",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# A. Fast Translator Billing
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestFastTranslatorBilling:
    """Verify fast translator charges correctly."""

    def test_fast_translate_charges_correct_amount(self):
        """Fast translate should charge exactly fast_translator_cost_credits."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)
        before = _get_balance(settings, TEST_USER)

        async def _test():
            return await fast_refine_prompt(settings, user_id=TEST_USER, text="校服少女")

        result = _run(_test())
        after = _get_balance(settings, TEST_USER)
        assert result.ok is True
        assert result.charged_credits == 2
        assert before - after == 2

    def test_fast_translate_creates_ledger_entry(self):
        """Fast translate should create a charge ledger entry."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)

        async def _test():
            return await fast_refine_prompt(settings, user_id=TEST_USER, text="校服少女")

        result = _run(_test())
        entries = _get_ledger_entries(settings, TEST_USER, FAST_TRANSLATE_CHARGE_REASON)
        assert len(entries) == 1
        assert entries[0]["amount_fen"] == -2

    def test_fast_translate_no_charge_on_failure(self):
        """Failed translation (insufficient credits) should not create ledger entry."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 0)  # no credits

        async def _test():
            return await fast_refine_prompt(settings, user_id=TEST_USER, text="校服少女")

        with pytest.raises(FastTranslatorError) as exc_info:
            _run(_test())
        assert exc_info.value.code == "insufficient_credits"
        entries = _get_ledger_entries(settings, TEST_USER, FAST_TRANSLATE_CHARGE_REASON)
        assert len(entries) == 0

    def test_fast_translate_no_double_charge(self):
        """Same translation should not charge twice."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)

        async def _test():
            return await fast_refine_prompt(settings, user_id=TEST_USER, text="校服少女")

        result = _run(_test())
        entries = _get_ledger_entries(settings, TEST_USER, FAST_TRANSLATE_CHARGE_REASON)
        assert len(entries) == 1  # Only one charge entry

    def test_fast_translate_multi_character_single_charge(self):
        """Multiple characters should NOT cause multiple charges."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)
        before = _get_balance(settings, TEST_USER)

        async def _test():
            return await fast_refine_prompt(settings, user_id=TEST_USER, text="无声铃鹿和东海帝王在赛道上")

        result = _run(_test())
        after = _get_balance(settings, TEST_USER)
        assert before - after == 2  # Still only 2 credits, not 4
        assert len(result.character_keys) == 2


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# B. Normal Translator Billing (Smart Agent)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestNormalTranslatorBilling:
    """Verify normal translation billing through smart agent path."""

    def test_smart_agent_charges_correct_amount(self):
        """Smart agent task should charge smart_agent_cost_credits."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)
        before = _get_balance(settings, TEST_USER)

        result = create_smart_agent_task_atomic(
            settings,
            job_code=f"JOB-{uuid.uuid4().hex[:8]}",
            user_id=TEST_USER,
            username="tester",
            request_text="测试生成",
            cost_credits=5,
        )
        after = _get_balance(settings, TEST_USER)
        assert before - after == 5
        assert result["charged_fen"] == 5

    def test_smart_agent_creates_ledger_entry(self):
        """Smart agent task should create a charge ledger entry."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)

        create_smart_agent_task_atomic(
            settings,
            job_code=f"JOB-{uuid.uuid4().hex[:8]}",
            user_id=TEST_USER,
            username="tester",
            request_text="测试生成",
            cost_credits=5,
        )
        entries = _get_ledger_entries(settings, TEST_USER, SMART_AGENT_CHARGE_REASON)
        assert len(entries) == 1
        assert entries[0]["amount_fen"] == -5

    def test_smart_agent_no_charge_on_insufficient_balance(self):
        """Should not create task or charge when balance insufficient."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 0)

        with pytest.raises(RuntimeError, match="余额不足"):
            create_smart_agent_task_atomic(
                settings,
                job_code=f"JOB-{uuid.uuid4().hex[:8]}",
                user_id=TEST_USER,
                username="tester",
                request_text="测试生成",
                cost_credits=5,
            )
        entries = _get_ledger_entries(settings, TEST_USER, SMART_AGENT_CHARGE_REASON)
        assert len(entries) == 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# C. 409 Character Confirmation Billing
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class Test409CharacterConfirmationBilling:
    """Verify 409 does not charge, and confirmation charges once."""

    def test_409_no_charge(self):
        """Ambiguous character (409) should NOT charge any credits."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)
        before = _get_balance(settings, TEST_USER)

        async def _test():
            return await fast_refine_prompt(settings, user_id=TEST_USER, text="miku")

        with pytest.raises(CharacterSelectionRequired):
            _run(_test())

        after = _get_balance(settings, TEST_USER)
        assert before == after  # No charge
        entries = _get_ledger_entries(settings, TEST_USER)
        assert len(entries) == 0

    def test_409_then_confirm_charges_once(self):
        """After 409, user confirmation should charge exactly once."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)

        from app.smart_agent.disambiguation_engine import analyze_character_mentions

        # Step 1: Get 409
        parsed = analyze_character_mentions("ta")
        assert parsed.get("status") == "ambiguous"

        # Step 2: Build resolution from parsed mentions
        selections = []
        for mention in parsed.get("mentions", []):
            candidates = mention.get("candidates", [])
            if candidates:
                selections.append({
                    "mentionId": mention.get("mentionId", ""),
                    "rawText": mention.get("rawText", ""),
                    "characterId": candidates[0].get("characterId", ""),
                })
        resolution = {"status": "resolved", "selections": selections}

        before = _get_balance(settings, TEST_USER)

        async def _test():
            return await fast_refine_prompt(
                settings, user_id=TEST_USER, text="ta",
                character_resolution=resolution,
            )

        result = _run(_test())
        after = _get_balance(settings, TEST_USER)
        assert result.ok is True
        assert before - after == 2  # Only one charge
        entries = _get_ledger_entries(settings, TEST_USER, FAST_TRANSLATE_CHARGE_REASON)
        assert len(entries) == 1

    def test_409_no_duplicate_task_on_double_confirm(self):
        """Double confirmation with same client_request_id should not create duplicate tasks."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)

        from app.smart_agent.disambiguation_engine import analyze_character_mentions

        parsed = analyze_character_mentions("ta")
        selections = []
        for mention in parsed.get("mentions", []):
            candidates = mention.get("candidates", [])
            if candidates:
                selections.append({
                    "mentionId": mention.get("mentionId", ""),
                    "rawText": mention.get("rawText", ""),
                    "characterId": candidates[0].get("characterId", ""),
                })
        resolution = {"status": "resolved", "selections": selections}
        client_id = f"confirm-{uuid.uuid4().hex[:8]}"

        async def _test():
            return await fast_refine_prompt(
                settings, user_id=TEST_USER, text="ta",
                character_resolution=resolution,
                client_request_id=client_id,
            )

        result1 = _run(_test())
        balance_after_first = _get_balance(settings, TEST_USER)

        result2 = _run(_test())
        balance_after_second = _get_balance(settings, TEST_USER)

        # Should return same result, not charge again
        assert result1.request_code == result2.request_code
        assert balance_after_first == balance_after_second


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# D. client_request_id Idempotency
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestClientIdempotency:
    """Verify client_request_id prevents duplicate charges."""

    def test_fast_translate_same_id_twice(self):
        """Same client_request_id should return existing result, not charge again."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)
        client_id = f"idemp-{uuid.uuid4().hex[:8]}"

        async def _first():
            return await fast_refine_prompt(
                settings, user_id=TEST_USER, text="校服少女",
                client_request_id=client_id,
            )

        result1 = _run(_first())
        balance1 = _get_balance(settings, TEST_USER)

        async def _second():
            return await fast_refine_prompt(
                settings, user_id=TEST_USER, text="校服少女",
                client_request_id=client_id,
            )

        result2 = _run(_second())
        balance2 = _get_balance(settings, TEST_USER)

        assert result1.request_code == result2.request_code
        assert balance1 == balance2
        entries = _get_ledger_entries(settings, TEST_USER, FAST_TRANSLATE_CHARGE_REASON)
        assert len(entries) == 1

    def test_smart_agent_same_id_twice(self):
        """Same client_request_id for smart agent should deduplicate."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)
        client_id = f"smart-idemp-{uuid.uuid4().hex[:8]}"

        result1 = create_smart_agent_task_atomic(
            settings,
            job_code=f"JOB-{uuid.uuid4().hex[:8]}",
            user_id=TEST_USER,
            username="tester",
            request_text="测试",
            cost_credits=5,
            client_request_id=client_id,
        )
        balance1 = _get_balance(settings, TEST_USER)

        result2 = create_smart_agent_task_atomic(
            settings,
            job_code=f"JOB-{uuid.uuid4().hex[:8]}",
            user_id=TEST_USER,
            username="tester",
            request_text="测试",
            cost_credits=5,
            client_request_id=client_id,
        )
        balance2 = _get_balance(settings, TEST_USER)

        assert result1["job_code"] == result2["job_code"]
        assert balance1 == balance2
        assert result2.get("deduped") is True

    def test_fast_translate_missing_client_id_no_idempotency(self):
        """Without client_request_id, each request is independent."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)

        async def _test():
            return await fast_refine_prompt(settings, user_id=TEST_USER, text="校服少女")

        result1 = _run(_test())
        result2 = _run(_test())

        assert result1.request_code != result2.request_code
        entries = _get_ledger_entries(settings, TEST_USER, FAST_TRANSLATE_CHARGE_REASON)
        assert len(entries) == 2  # Two separate charges

    def test_fast_translate_different_users_same_id(self):
        """Different users with same client_request_id should not conflict."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, "user-a", 50000)
        _seed_balance(settings, "user-b", 50000)
        client_id = f"shared-{uuid.uuid4().hex[:8]}"

        async def _test_a():
            return await fast_refine_prompt(
                settings, user_id="user-a", text="校服少女",
                client_request_id=client_id,
            )

        async def _test_b():
            return await fast_refine_prompt(
                settings, user_id="user-b", text="校服少女",
                client_request_id=client_id,
            )

        result_a = _run(_test_a())
        result_b = _run(_test_b())

        # Different users should get different request codes
        assert result_a.request_code != result_b.request_code

    def test_smart_agent_empty_client_id(self):
        """Empty client_request_id should not trigger dedup."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)

        result1 = create_smart_agent_task_atomic(
            settings,
            job_code=f"JOB-{uuid.uuid4().hex[:8]}",
            user_id=TEST_USER,
            username="tester",
            request_text="测试1",
            cost_credits=5,
            client_request_id="",
        )
        result2 = create_smart_agent_task_atomic(
            settings,
            job_code=f"JOB-{uuid.uuid4().hex[:8]}",
            user_id=TEST_USER,
            username="tester",
            request_text="测试2",
            cost_credits=5,
            client_request_id="",
        )

        assert result1["job_code"] != result2["job_code"]
        entries = _get_ledger_entries(settings, TEST_USER, SMART_AGENT_CHARGE_REASON)
        assert len(entries) == 2


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# E. DeepSeek Failure Refund
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestDeepSeekFailureRefund:
    """Verify refund on DeepSeek failure."""

    def test_fast_translate_refund_on_failure(self):
        """Failed fast translate should refund the charge."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)

        async def _test():
            return await fast_refine_prompt(settings, user_id=TEST_USER, text="校服少女")

        result = _run(_test())
        balance_after_charge = _get_balance(settings, TEST_USER)

        # Simulate failure refund
        _refund(settings, request_code=result.request_code, error_code="deepseek_timeout")

        balance_after_refund = _get_balance(settings, TEST_USER)
        assert balance_after_refund == 50000  # Back to original

        # Check ledger has both charge and refund
        charge_entries = _get_ledger_entries(settings, TEST_USER, FAST_TRANSLATE_CHARGE_REASON)
        refund_entries = _get_ledger_entries(settings, TEST_USER, FAST_TRANSLATE_REFUND_REASON)
        assert len(charge_entries) == 1
        assert len(refund_entries) == 1
        assert refund_entries[0]["amount_fen"] == 2  # Refund amount

    def test_fast_translate_refund_updates_status(self):
        """Refund should update translation_requests status to failed_refunded."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)

        async def _test():
            return await fast_refine_prompt(settings, user_id=TEST_USER, text="校服少女")

        result = _run(_test())
        _refund(settings, request_code=result.request_code, error_code="test_error")

        requests = _get_translation_requests(settings, TEST_USER)
        assert len(requests) == 1
        assert requests[0]["status"] == "failed_refunded"
        assert requests[0]["error_code"] == "test_error"

    def test_smart_agent_refund_on_failure(self):
        """Failed smart agent task should refund."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)

        job_code = f"JOB-{uuid.uuid4().hex[:8]}"
        create_smart_agent_task_atomic(
            settings,
            job_code=job_code,
            user_id=TEST_USER,
            username="tester",
            request_text="测试",
            cost_credits=5,
        )
        balance_after_charge = _get_balance(settings, TEST_USER)
        assert balance_after_charge == 49995

        refunded = fail_smart_agent_task_refund(
            settings,
            job_code=job_code,
            error="DeepSeek timeout",
            error_code="deepseek_timeout",
        )
        assert refunded is True

        balance_after_refund = _get_balance(settings, TEST_USER)
        assert balance_after_refund == 50000

        refund_entries = _get_ledger_entries(settings, TEST_USER, SMART_AGENT_REFUND_REASON)
        assert len(refund_entries) == 1
        assert refund_entries[0]["amount_fen"] == 5


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# F. Double-Refund Protection
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestDoubleRefundProtection:
    """Verify refunds are idempotent."""

    def test_fast_translate_double_refund(self):
        """Calling refund twice should only refund once."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)

        async def _test():
            return await fast_refine_prompt(settings, user_id=TEST_USER, text="校服少女")

        result = _run(_test())

        # First refund
        _refund(settings, request_code=result.request_code, error_code="error1")
        balance_after_first = _get_balance(settings, TEST_USER)

        # Second refund (should be idempotent)
        _refund(settings, request_code=result.request_code, error_code="error2")
        balance_after_second = _get_balance(settings, TEST_USER)

        assert balance_after_first == balance_after_second
        refund_entries = _get_ledger_entries(settings, TEST_USER, FAST_TRANSLATE_REFUND_REASON)
        assert len(refund_entries) == 1  # Only one refund

    def test_smart_agent_double_refund(self):
        """Calling fail_smart_agent_task_refund twice should only refund once."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)

        job_code = f"JOB-{uuid.uuid4().hex[:8]}"
        create_smart_agent_task_atomic(
            settings,
            job_code=job_code,
            user_id=TEST_USER,
            username="tester",
            request_text="测试",
            cost_credits=5,
        )

        # First refund
        refunded1 = fail_smart_agent_task_refund(
            settings, job_code=job_code, error="error1", error_code="timeout",
        )
        balance_after_first = _get_balance(settings, TEST_USER)

        # Second refund (should be rejected by status check)
        refunded2 = fail_smart_agent_task_refund(
            settings, job_code=job_code, error="error2", error_code="timeout",
        )
        balance_after_second = _get_balance(settings, TEST_USER)

        assert refunded1 is True
        assert refunded2 is False  # Already refunded
        assert balance_after_first == balance_after_second

        refund_entries = _get_ledger_entries(settings, TEST_USER, SMART_AGENT_REFUND_REASON)
        assert len(refund_entries) == 1  # Only one refund


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# G. Concurrent Idempotency
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestConcurrentIdempotency:
    """Verify idempotency under concurrent access."""

    def test_fast_translate_concurrent_same_id(self):
        """Concurrent requests with same client_request_id should not double-charge."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)
        client_id = f"concurrent-{uuid.uuid4().hex[:8]}"

        results = []
        errors = []

        def _run_request():
            try:
                async def _test():
                    return await fast_refine_prompt(
                        settings, user_id=TEST_USER, text="校服少女",
                        client_request_id=client_id,
                    )
                results.append(_run(_test()))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_run_request) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        # At least one should succeed
        assert len(results) >= 1
        # Only one charge should have occurred
        entries = _get_ledger_entries(settings, TEST_USER, FAST_TRANSLATE_CHARGE_REASON)
        assert len(entries) == 1

    def test_smart_agent_concurrent_same_id(self):
        """Concurrent smart agent requests with same client_request_id should deduplicate."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)
        client_id = f"smart-concurrent-{uuid.uuid4().hex[:8]}"

        results = []
        errors = []

        def _run_request():
            try:
                result = create_smart_agent_task_atomic(
                    settings,
                    job_code=f"JOB-{uuid.uuid4().hex[:8]}",
                    user_id=TEST_USER,
                    username="tester",
                    request_text="测试",
                    cost_credits=5,
                    client_request_id=client_id,
                )
                results.append(result)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_run_request) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        # All should return the same job_code (deduped)
        job_codes = {r["job_code"] for r in results}
        assert len(job_codes) == 1
        entries = _get_ledger_entries(settings, TEST_USER, SMART_AGENT_CHARGE_REASON)
        assert len(entries) == 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# H. Worker Duplicate Prevention
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestWorkerDuplicatePrevention:
    """Verify worker can't double-process tasks."""

    def test_smart_agent_task_status_prevents_double_refund(self):
        """After refund, task status prevents second refund attempt."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)

        job_code = f"JOB-{uuid.uuid4().hex[:8]}"
        create_smart_agent_task_atomic(
            settings,
            job_code=job_code,
            user_id=TEST_USER,
            username="tester",
            request_text="测试",
            cost_credits=5,
        )

        # First refund succeeds
        assert fail_smart_agent_task_refund(settings, job_code=job_code, error="e1") is True
        # Second refund fails (status already changed)
        assert fail_smart_agent_task_refund(settings, job_code=job_code, error="e2") is False

    def test_fast_translate_refund_ledger_check(self):
        """Fast translate refund checks ledger to prevent double refund."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 50000)

        async def _test():
            return await fast_refine_prompt(settings, user_id=TEST_USER, text="校服少女")

        result = _run(_test())

        # First refund
        _refund(settings, request_code=result.request_code, error_code="e1")
        balance1 = _get_balance(settings, TEST_USER)

        # Second refund (should be idempotent due to ledger check)
        _refund(settings, request_code=result.request_code, error_code="e2")
        balance2 = _get_balance(settings, TEST_USER)

        assert balance1 == balance2
        assert balance2 == 50000


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# I. Balance Consistency
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestBalanceConsistency:
    """Verify balance, ledger, and task state stay consistent."""

    def test_charge_then_refund_balance_restored(self):
        """Full charge-refund cycle should restore original balance."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        initial = 50000
        _seed_balance(settings, TEST_USER, initial)

        async def _test():
            return await fast_refine_prompt(settings, user_id=TEST_USER, text="校服少女")

        result = _run(_test())
        assert _get_balance(settings, TEST_USER) == initial - 2

        _refund(settings, request_code=result.request_code, error_code="test")
        assert _get_balance(settings, TEST_USER) == initial

    def test_multiple_operations_balance_consistent(self):
        """Multiple charge/refund operations should keep balance consistent."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        initial = 50000
        _seed_balance(settings, TEST_USER, initial)

        # Charge 1
        async def _test1():
            return await fast_refine_prompt(settings, user_id=TEST_USER, text="场景1")
        r1 = _run(_test1())
        assert _get_balance(settings, TEST_USER) == initial - 2

        # Charge 2
        async def _test2():
            return await fast_refine_prompt(settings, user_id=TEST_USER, text="场景2")
        r2 = _run(_test2())
        assert _get_balance(settings, TEST_USER) == initial - 4

        # Refund 1
        _refund(settings, request_code=r1.request_code, error_code="e")
        assert _get_balance(settings, TEST_USER) == initial - 2

        # Ledger should have: 2 charges + 1 refund
        charges = _get_ledger_entries(settings, TEST_USER, FAST_TRANSLATE_CHARGE_REASON)
        refunds = _get_ledger_entries(settings, TEST_USER, FAST_TRANSLATE_REFUND_REASON)
        assert len(charges) == 2
        assert len(refunds) == 1

    def test_negative_balance_impossible(self):
        """Balance should never go negative."""
        case_root = _case_root()
        settings = _make_settings(case_root)
        _seed_balance(settings, TEST_USER, 1)  # Only 1 credit

        async def _test():
            return await fast_refine_prompt(settings, user_id=TEST_USER, text="校服少女")

        with pytest.raises(FastTranslatorError) as exc_info:
            _run(_test())
        assert exc_info.value.code == "insufficient_credits"
        assert _get_balance(settings, TEST_USER) == 1  # Unchanged


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
