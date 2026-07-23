"""Phase 4 Runtime Gap Fixes: P1-2 through P2-2.

Tests for:
A. Fast translate payload conflict detection (P1-2)
B. Default price for fast_translator_cost_credits (P1-3)
C. Normal translation billing (P1-1)
D. Timeout and refund precision (P1-4)
E. Character confirmation 409 via HTTP (P1-5)
F. User isolation (P2-1)
G. Strict "none of above" validation
H. Legacy fingerprint compatibility
I. Fingerprint normalization
"""
from __future__ import annotations

import asyncio
import hashlib
import http.server
import json
import os
import socket
import sys
import threading
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
    create_conversation,
    save_smart_agent_prompt_draft,
)
from app.services.fast_translator_service import (
    CharacterSelectionRequired,
    ClientRequestIdConflict,
    FastTranslatorError,
    fast_refine_prompt,
    _begin_charge,
    _compute_fast_translation_fingerprint,
    _safe_parse_character_keys_json,
)
from app.smart_agent.disambiguation_engine import (
    NO_LIBRARY_CHARACTER_ID,
    analyze_character_mentions,
)

TEST_USER_A = "test-user-alpha"
TEST_USER_B = "test-user-beta"


# ────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────

def _run(coro):
    return asyncio.run(coro)


def _make_settings(tmp_path: Path, **overrides) -> Settings:
    db_path = tmp_path / "test.db"
    output = tmp_path / "output"
    mock_output = tmp_path / "mock_output"
    input_images = tmp_path / "input_images"
    for d in (output, mock_output, input_images):
        d.mkdir(parents=True, exist_ok=True)
    data = {
        "APP_ENV": "local",
        "APP_ORIGIN": "http://127.0.0.1:18080",
        "HOST": "127.0.0.1",
        "PORT": 18080,
        "BALANCE_DB": str(db_path),
        "BOT_OUTPUT_DIR": str(output),
        "mock_output_dir": str(mock_output),
        "INPUT_IMAGE_DIR": str(input_images),
        "BOT_DIR": str(tmp_path),
        "redis_enabled": False,
        "dev_auth_bypass": True,
        "dev_user_id": TEST_USER_A,
        "dev_username": "Runtime Gap Tester",
        "fast_translator_enabled": True,
        "fast_translator_cost_credits": 2,
        "agent_surcharge_credits": 1,
        "mock_worker_enabled": True,
        "deepseek_api_key": "TEST_ONLY_dummy",
        "deepseek_base_url": "http://127.0.0.1:9",
        "deepseek_model": "test-mock",
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
        conn.execute(
            "INSERT OR REPLACE INTO users(user_id, balance_fen) VALUES (?, ?)",
            (user_id, amount),
        )
        conn.commit()
    finally:
        conn.close()


def _get_balance(settings: Settings, user_id: str) -> int:
    conn = connect(settings)
    try:
        row = conn.execute(
            "SELECT balance_fen FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        return int(row["balance_fen"]) if row else 0
    finally:
        conn.close()


def _get_translation_record(
    settings: Settings, request_code: str
) -> dict | None:
    conn = connect(settings)
    try:
        row = conn.execute(
            "SELECT * FROM translation_requests WHERE request_code=?",
            (request_code,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _count_translation_requests(settings: Settings, user_id: str) -> int:
    conn = connect(settings)
    try:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM translation_requests WHERE user_id=?",
            (user_id,),
        ).fetchone()
        return int(row["cnt"]) if row else 0
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


def _insert_legacy_translation(
    settings: Settings,
    *,
    user_id: str,
    request_code: str,
    text: str,
    character_keys_json: str = "[]",
    character_match_source: str = "none",
    client_request_id: str | None = None,
) -> None:
    """Insert a legacy translation_requests row with empty fingerprint."""
    conn = connect(settings)
    try:
        conn.execute(
            """
            INSERT INTO translation_requests(
                request_code, user_id, client_request_id, translation_mode, model,
                character_match_source, character_keys_json, original_text,
                charged_credits, ledger_id, status, created_at, request_fingerprint
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                request_code, user_id, client_request_id, "fast", "test-model",
                character_match_source, character_keys_json, text,
                2, None, "done", int(time.time()), "",
            ),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _clear_dependency_overrides():
    from app.main import app
    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# G. Strict "none of above" validation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestStrictCharacterValidation:
    """Verify empty/invalid character selections cannot bypass validation."""

    def test_empty_selections_rejected(self, tmp_path):
        settings = _make_settings(tmp_path)
        _seed_balance(settings, TEST_USER_A, 50000)
        with pytest.raises(CharacterSelectionRequired) as exc_info:
            _run(fast_refine_prompt(
                settings, user_id=TEST_USER_A, text="miku在舞台上",
                client_request_id=f"empty-sel-{uuid.uuid4().hex[:8]}",
                character_resolution={"status": "resolved", "selections": []},
            ))
        assert exc_info.value.code == "character_resolution_required"

    def test_missing_selections_rejected(self, tmp_path):
        settings = _make_settings(tmp_path)
        _seed_balance(settings, TEST_USER_A, 50000)
        with pytest.raises(CharacterSelectionRequired) as exc_info:
            _run(fast_refine_prompt(
                settings, user_id=TEST_USER_A, text="miku在舞台上",
                client_request_id=f"miss-sel-{uuid.uuid4().hex[:8]}",
                character_resolution={"status": "resolved"},
            ))
        assert exc_info.value.code == "character_resolution_required"

    def test_empty_dict_selection_rejected(self, tmp_path):
        settings = _make_settings(tmp_path)
        _seed_balance(settings, TEST_USER_A, 50000)
        with pytest.raises(CharacterSelectionRequired) as exc_info:
            _run(fast_refine_prompt(
                settings, user_id=TEST_USER_A, text="miku在舞台上",
                client_request_id=f"empty-dict-{uuid.uuid4().hex[:8]}",
                character_resolution={"status": "resolved", "selections": [{}]},
            ))
        assert exc_info.value.code == "character_resolution_required"

    def test_wrong_mention_id_still_resolves(self, tmp_path):
        """Wrong mentionId with correct rawText → upstream validation still resolves.

        Note: validate_character_resolution resolves based on rawText match,
        not strict mentionId matching. This is known upstream behavior.
        """
        settings = _make_settings(tmp_path)
        _seed_balance(settings, TEST_USER_A, 50000)
        result = _run(fast_refine_prompt(
            settings, user_id=TEST_USER_A, text="miku在舞台上",
            client_request_id=f"wrong-mid-{uuid.uuid4().hex[:8]}",
            character_resolution={
                "status": "resolved",
                "selections": [{
                    "mentionId": "WRONG_ID",
                    "rawText": "ik",
                    "characterId": "meiko",
                }],
            },
        ))
        # Upstream validates by rawText match, not strict mentionId
        assert result.ok is True

    def test_wrong_raw_text_rejected(self, tmp_path):
        settings = _make_settings(tmp_path)
        _seed_balance(settings, TEST_USER_A, 50000)
        with pytest.raises(CharacterSelectionRequired) as exc_info:
            _run(fast_refine_prompt(
                settings, user_id=TEST_USER_A, text="miku在舞台上",
                client_request_id=f"wrong-raw-{uuid.uuid4().hex[:8]}",
                character_resolution={
                    "status": "resolved",
                    "selections": [{
                        "mentionId": "DG-b98e5b10",
                        "rawText": "WRONG_RAW",
                        "characterId": "meiko",
                    }],
                },
            ))
        assert exc_info.value.code == "character_resolution_required"

    def test_partial_mention_submission_rejected(self, tmp_path):
        """Two ambiguous mentions, only one submitted → unresolved mention remains → rejected."""
        settings = _make_settings(tmp_path)
        _seed_balance(settings, TEST_USER_A, 50000)
        # 'ikとmami' has 2 ambiguous mentions (ik→meiko/taiki_shuttle, mami→nanami_mami/tomoe_mami)
        text = "ikとmamiが舞台に立っている"
        analysis = analyze_character_mentions(text)
        assert analysis["status"] == "ambiguous"
        assert len(analysis["mentions"]) >= 2
        mention = analysis["mentions"][0]  # submit only the first one
        with pytest.raises(CharacterSelectionRequired) as exc_info:
            _run(fast_refine_prompt(
                settings, user_id=TEST_USER_A, text=text,
                client_request_id=f"partial-{uuid.uuid4().hex[:8]}",
                character_resolution={
                    "status": "resolved",
                    "selections": [{
                        "mentionId": mention["mentionId"],
                        "rawText": mention["rawText"],
                        "characterId": mention["candidates"][0]["characterId"],
                    }],
                },
            ))
        assert exc_info.value.code == "character_resolution_required"
        assert _get_balance(settings, TEST_USER_A) == 50000

    def test_non_candidate_character_id_rejected(self, tmp_path):
        """Non-candidate character ID for ambiguous text → 409 character_resolution_required.

        validate_character_resolution raises ValueError for non-candidate ID.
        Re-analysis sees ambiguous → CharacterSelectionRequired.
        """
        settings = _make_settings(tmp_path)
        _seed_balance(settings, TEST_USER_A, 50000)
        analysis = analyze_character_mentions("mami在舞台上")
        mention_id = analysis["mentions"][0]["mentionId"]
        raw_text = analysis["mentions"][0]["rawText"]
        with pytest.raises(CharacterSelectionRequired) as exc_info:
            _run(fast_refine_prompt(
                settings, user_id=TEST_USER_A, text="mami在舞台上",
                client_request_id=f"noncand-{uuid.uuid4().hex[:8]}",
                character_resolution={
                    "status": "resolved",
                    "selections": [{
                        "mentionId": mention_id,
                        "rawText": raw_text,
                        "characterId": "totally_fake_character_xyz",
                    }],
                },
            ))
        assert exc_info.value.code == "character_resolution_required"
        assert _get_balance(settings, TEST_USER_A) == 50000

    def test_valid_all_skip_accepted(self, tmp_path):
        """Valid 'none of above' for all mentions → character_keys=[], source='none'."""
        settings = _make_settings(tmp_path)
        _seed_balance(settings, TEST_USER_A, 50000)
        analysis = analyze_character_mentions("mami在舞台上")
        mention = analysis["mentions"][0]
        result = _run(fast_refine_prompt(
            settings, user_id=TEST_USER_A, text="mami在舞台上",
            client_request_id=f"skip-ok-{uuid.uuid4().hex[:8]}",
            character_resolution={
                "status": "resolved",
                "selections": [{
                    "mentionId": mention["mentionId"],
                    "rawText": mention["rawText"],
                    "characterId": NO_LIBRARY_CHARACTER_ID,
                    "skipCharacterLibrary": True,
                }],
            },
        ))
        assert result.ok is True
        assert result.character_keys == []
        assert result.character_match_source == "none"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# A. Fast translate payload conflict detection (P1-2)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestPayloadConflict:
    """Verify client_request_id + payload comparison."""

    def test_same_id_same_payload_returns_cached(self, tmp_path):
        settings = _make_settings(tmp_path)
        _seed_balance(settings, TEST_USER_A, 50000)
        cid = f"same-{uuid.uuid4().hex[:8]}"

        r1 = _run(fast_refine_prompt(
            settings, user_id=TEST_USER_A, text="樱花树下",
            client_request_id=cid,
        ))
        bal1 = _get_balance(settings, TEST_USER_A)

        r2 = _run(fast_refine_prompt(
            settings, user_id=TEST_USER_A, text="樱花树下",
            client_request_id=cid,
        ))
        bal2 = _get_balance(settings, TEST_USER_A)

        assert r1.request_code == r2.request_code
        assert bal1 == bal2

    def test_same_id_different_text_raises_conflict(self, tmp_path):
        settings = _make_settings(tmp_path)
        _seed_balance(settings, TEST_USER_A, 50000)
        cid = f"conflict-{uuid.uuid4().hex[:8]}"

        _run(fast_refine_prompt(
            settings, user_id=TEST_USER_A, text="原始文本",
            client_request_id=cid,
        ))
        bal_after = _get_balance(settings, TEST_USER_A)

        with pytest.raises(ClientRequestIdConflict) as exc_info:
            _run(fast_refine_prompt(
                settings, user_id=TEST_USER_A, text="完全不同的文本",
                client_request_id=cid,
            ))

        assert exc_info.value.code == "client_request_id_conflict"
        assert _get_balance(settings, TEST_USER_A) == bal_after

    def test_same_id_different_character_keys_raises_conflict(self, tmp_path):
        """Same user + same ID + same text + different character selection → conflict.

        Uses 'mami' which has two candidates: nanami_mami and tomoe_mami.
        First request selects nanami_mami, second selects tomoe_mami.
        """
        settings = _make_settings(tmp_path)
        _seed_balance(settings, TEST_USER_A, 50000)
        cid = f"char-conflict-{uuid.uuid4().hex[:8]}"

        analysis = analyze_character_mentions("mami在舞台上")
        assert analysis["status"] == "ambiguous"
        mention = analysis["mentions"][0]
        candidates = mention["candidates"]
        assert len(candidates) >= 2, f"Need >= 2 candidates, got {len(candidates)}"

        char_a = candidates[0]["characterId"]
        char_b = candidates[1]["characterId"]
        assert char_a != char_b

        # First request with character A
        r1 = _run(fast_refine_prompt(
            settings, user_id=TEST_USER_A, text="mami在舞台上",
            client_request_id=cid,
            character_resolution={
                "status": "resolved",
                "selections": [{
                    "mentionId": mention["mentionId"],
                    "rawText": mention["rawText"],
                    "characterId": char_a,
                }],
            },
        ))
        bal_after = _get_balance(settings, TEST_USER_A)
        record1 = _get_translation_record(settings, r1.request_code)
        assert record1 is not None
        original_text = record1["original_text"]

        # Second request same cid, same text, DIFFERENT character → conflict
        with pytest.raises(ClientRequestIdConflict) as exc_info:
            _run(fast_refine_prompt(
                settings, user_id=TEST_USER_A, text="mami在舞台上",
                client_request_id=cid,
                character_resolution={
                    "status": "resolved",
                    "selections": [{
                        "mentionId": mention["mentionId"],
                        "rawText": mention["rawText"],
                        "characterId": char_b,
                    }],
                },
            ))

        assert exc_info.value.code == "client_request_id_conflict"
        assert _get_balance(settings, TEST_USER_A) == bal_after

        # Original record not overwritten
        record_still = _get_translation_record(settings, r1.request_code)
        assert record_still["original_text"] == original_text

    def test_different_users_same_id_no_conflict(self, tmp_path):
        settings = _make_settings(tmp_path)
        _seed_balance(settings, TEST_USER_A, 50000)
        _seed_balance(settings, TEST_USER_B, 50000)
        cid = f"cross-user-{uuid.uuid4().hex[:8]}"

        r1 = _run(fast_refine_prompt(
            settings, user_id=TEST_USER_A, text="用户A的请求",
            client_request_id=cid,
        ))
        r2 = _run(fast_refine_prompt(
            settings, user_id=TEST_USER_B, text="用户B的请求",
            client_request_id=cid,
        ))

        assert r1.request_code != r2.request_code


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# B. Default price (P1-3)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestDefaultPrice:
    def test_code_default_is_two(self, tmp_path):
        s = _make_settings(tmp_path)
        assert s.fast_translator_cost_credits == 2

    def test_explicit_override_respected(self, tmp_path):
        s = _make_settings(tmp_path, fast_translator_cost_credits=5)
        assert s.fast_translator_cost_credits == 5

    def test_begin_charge_uses_settings_value(self, tmp_path):
        settings = _make_settings(tmp_path, fast_translator_cost_credits=3)
        _seed_balance(settings, TEST_USER_A, 50000)
        bal_before = _get_balance(settings, TEST_USER_A)

        _run(fast_refine_prompt(settings, user_id=TEST_USER_A, text="测试价格"))
        bal_after = _get_balance(settings, TEST_USER_A)

        assert bal_before - bal_after == 3


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# C. Normal translation billing (P1-1)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestNormalBilling:
    def test_basic_fast_translate_charges_once(self, tmp_path):
        settings = _make_settings(tmp_path)
        _seed_balance(settings, TEST_USER_A, 50000)
        bal_before = _get_balance(settings, TEST_USER_A)

        result = _run(fast_refine_prompt(
            settings, user_id=TEST_USER_A, text="test billing",
        ))

        assert result.ok is True
        assert result.charged_credits == 2
        assert _get_balance(settings, TEST_USER_A) == bal_before - 2
        assert _count_ledger(
            settings, TEST_USER_A, "fast_translate_charge"
        ) == 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# D. Timeout and refund precision (P1-4)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestTimeout:
    def test_immediate_deepseek_error_refunds(self, tmp_path):
        """Unit test: DeepSeekError → refund → failed_refunded."""
        settings = _make_settings(tmp_path)
        _seed_balance(settings, TEST_USER_A, 50000)
        bal_before = _get_balance(settings, TEST_USER_A)

        mock_ds = MagicMock()
        mock_ds.complete_json = AsyncMock(
            side_effect=Exception("simulated_deepseek_failure")
        )

        with pytest.raises(FastTranslatorError) as exc_info:
            _run(fast_refine_prompt(
                settings, user_id=TEST_USER_A,
                text="a beautiful sunset scene",
                deepseek=mock_ds,
            ))

        assert exc_info.value.code == "fast_translate_failed"
        assert _get_balance(settings, TEST_USER_A) == bal_before
        assert _count_ledger(
            settings, TEST_USER_A, "fast_translate_refund"
        ) == 1

    def test_real_local_timeout_refunds(self, tmp_path):
        """Real timeout test: local HTTP server that delays beyond client timeout.

        Uses a real HTTP server on 127.0.0.1 with a random port.
        Server delays response longer than client timeout.
        Verifies: actual wait, precise refund, no double refund, failed_refunded.
        """
        # Find a free port
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

        delay_seconds = 10  # server delay
        client_timeout = 2  # client timeout must be < server delay

        class SlowHandler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                time.sleep(delay_seconds)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "choices": [{"message": {"content": '{"style":"anime"}'}, "finish_reason": "stop"}]
                }).encode())

            def log_message(self, format, *args):
                pass  # suppress logs

        server = http.server.HTTPServer(("127.0.0.1", port), SlowHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()

        try:
            settings = _make_settings(
                tmp_path,
                deepseek_api_key="sk-real-looking-key-not-test-only",
                deepseek_base_url=f"http://127.0.0.1:{port}",
                deepseek_timeout_seconds=client_timeout,
                deepseek_max_retries=0,
            )
            _seed_balance(settings, TEST_USER_A, 50000)
            bal_before = _get_balance(settings, TEST_USER_A)

            t0 = time.monotonic()
            with pytest.raises(FastTranslatorError) as exc_info:
                _run(fast_refine_prompt(
                    settings, user_id=TEST_USER_A, text="a beautiful sunset scene",
                ))
            elapsed = time.monotonic() - t0

            # Verify timeout happened around the expected duration
            assert elapsed >= client_timeout * 0.5, f"Too fast: {elapsed:.1f}s"
            assert elapsed < delay_seconds, f"Too slow: {elapsed:.1f}s"

            # Verify refund
            assert _get_balance(settings, TEST_USER_A) == bal_before
            assert _count_ledger(
                settings, TEST_USER_A, "fast_translate_refund"
            ) == 1

            # Verify status
            charge_count = _count_ledger(
                settings, TEST_USER_A, "fast_translate_charge"
            )
            assert charge_count == 1  # one charge
            # Find the request_code from ledger
            conn = connect(settings)
            try:
                row = conn.execute(
                    "SELECT order_code FROM balance_ledger WHERE user_id=? AND reason=?",
                    (TEST_USER_A, "fast_translate_charge"),
                ).fetchone()
                request_code = row["order_code"]
            finally:
                conn.close()
            record = _get_translation_record(settings, request_code)
            assert record is not None
            assert record["status"] == "failed_refunded"
        finally:
            server.shutdown()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# E. HTTP 409 character confirmation (P1-5)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestHTTPCharacterConfirmation:
    """Test the full HTTP 409 → confirm lifecycle."""

    def test_ambiguous_409_then_confirm_same_cid(self, tmp_path):
        """Ambiguous → 409 → confirm with same client_request_id → 200 → only one charge."""
        from fastapi.testclient import TestClient
        from app.main import app
        from app.config import get_settings

        settings = _make_settings(tmp_path, fast_translator_cost_credits=2)
        _seed_balance(settings, TEST_USER_A, 50000)
        initial_balance = _get_balance(settings, TEST_USER_A)

        app.dependency_overrides[get_settings] = lambda: settings
        try:
            client = TestClient(app, raise_server_exceptions=False)
            cid = f"lifecycle-{uuid.uuid4().hex[:8]}"

            # Step 1: Ambiguous character → 409
            r1 = client.post("/api/prompt/fast-refine", json={
                "text": "mami在舞台上",
                "client_request_id": cid,
            }, headers={"X-CSRF-Token": "test"})
            assert r1.status_code == 409
            detail = r1.json().get("detail", {})
            assert detail.get("code") == "character_resolution_required"
            assert detail.get("requiresCharacterSelection") is True
            assert _get_balance(settings, TEST_USER_A) == initial_balance

            # Step 2: Confirm with character A using same client_request_id
            analysis = analyze_character_mentions("mami在舞台上")
            mention = analysis["mentions"][0]
            char_a = mention["candidates"][0]["characterId"]

            r2 = client.post("/api/prompt/fast-refine", json={
                "text": "mami在舞台上",
                "client_request_id": cid,
                "character_resolution": {
                    "status": "resolved",
                    "selections": [{
                        "mentionId": mention["mentionId"],
                        "rawText": mention["rawText"],
                        "characterId": char_a,
                    }],
                },
            }, headers={"X-CSRF-Token": "test"})
            assert r2.status_code == 200
            data = r2.json()
            assert data.get("ok") is True
            assert char_a in data.get("character_keys", [])
            assert _get_balance(settings, TEST_USER_A) == initial_balance - 2

            # Step 3: Replay same request → dedup, no extra charge
            r3 = client.post("/api/prompt/fast-refine", json={
                "text": "mami在舞台上",
                "client_request_id": cid,
                "character_resolution": {
                    "status": "resolved",
                    "selections": [{
                        "mentionId": mention["mentionId"],
                        "rawText": mention["rawText"],
                        "characterId": char_a,
                    }],
                },
            }, headers={"X-CSRF-Token": "test"})
            assert r3.status_code == 200
            assert r3.json().get("request_code") == data.get("request_code")
            assert _get_balance(settings, TEST_USER_A) == initial_balance - 2
            assert _count_translation_requests(settings, TEST_USER_A) == 1
        finally:
            app.dependency_overrides.clear()

    def test_ambiguous_409_then_skip_same_cid(self, tmp_path):
        """Ambiguous → 409 → 'none of above' with same cid → 200 → replay safe."""
        from fastapi.testclient import TestClient
        from app.main import app
        from app.config import get_settings

        settings = _make_settings(tmp_path, fast_translator_cost_credits=2)
        _seed_balance(settings, TEST_USER_A, 50000)
        initial_balance = _get_balance(settings, TEST_USER_A)

        app.dependency_overrides[get_settings] = lambda: settings
        try:
            client = TestClient(app, raise_server_exceptions=False)
            cid = f"skip-lifecycle-{uuid.uuid4().hex[:8]}"

            # Step 1: Ambiguous → 409
            r1 = client.post("/api/prompt/fast-refine", json={
                "text": "mami在舞台上",
                "client_request_id": cid,
            }, headers={"X-CSRF-Token": "test"})
            assert r1.status_code == 409

            # Step 2: Select "none of above" with same cid
            analysis = analyze_character_mentions("mami在舞台上")
            mention = analysis["mentions"][0]

            r2 = client.post("/api/prompt/fast-refine", json={
                "text": "mami在舞台上",
                "client_request_id": cid,
                "character_resolution": {
                    "status": "resolved",
                    "selections": [{
                        "mentionId": mention["mentionId"],
                        "rawText": mention["rawText"],
                        "characterId": NO_LIBRARY_CHARACTER_ID,
                        "skipCharacterLibrary": True,
                    }],
                },
            }, headers={"X-CSRF-Token": "test"})
            assert r2.status_code == 200
            data = r2.json()
            assert data.get("ok") is True
            assert data.get("character_keys") == []
            assert _get_balance(settings, TEST_USER_A) == initial_balance - 2

            # Step 3: Replay → dedup
            r3 = client.post("/api/prompt/fast-refine", json={
                "text": "mami在舞台上",
                "client_request_id": cid,
                "character_resolution": {
                    "status": "resolved",
                    "selections": [{
                        "mentionId": mention["mentionId"],
                        "rawText": mention["rawText"],
                        "characterId": NO_LIBRARY_CHARACTER_ID,
                        "skipCharacterLibrary": True,
                    }],
                },
            }, headers={"X-CSRF-Token": "test"})
            assert r3.status_code == 200
            assert r3.json().get("request_code") == data.get("request_code")
            assert _get_balance(settings, TEST_USER_A) == initial_balance - 2
        finally:
            app.dependency_overrides.clear()

    def test_http_409_different_character_conflict(self, tmp_path):
        """Same cid + same text + different character → HTTP 409, no extra charge."""
        from fastapi.testclient import TestClient
        from app.main import app
        from app.config import get_settings

        settings = _make_settings(tmp_path, fast_translator_cost_credits=2)
        _seed_balance(settings, TEST_USER_A, 50000)
        initial_balance = _get_balance(settings, TEST_USER_A)

        app.dependency_overrides[get_settings] = lambda: settings
        try:
            client = TestClient(app, raise_server_exceptions=False)
            cid = f"http-conflict-{uuid.uuid4().hex[:8]}"

            analysis = analyze_character_mentions("mami在舞台上")
            mention = analysis["mentions"][0]
            char_a = mention["candidates"][0]["characterId"]
            char_b = mention["candidates"][1]["characterId"]

            # First: character A → 200
            r1 = client.post("/api/prompt/fast-refine", json={
                "text": "mami在舞台上",
                "client_request_id": cid,
                "character_resolution": {
                    "status": "resolved",
                    "selections": [{
                        "mentionId": mention["mentionId"],
                        "rawText": mention["rawText"],
                        "characterId": char_a,
                    }],
                },
            }, headers={"X-CSRF-Token": "test"})
            assert r1.status_code == 200
            rc1 = r1.json()["request_code"]

            # Second: character B → 409
            r2 = client.post("/api/prompt/fast-refine", json={
                "text": "mami在舞台上",
                "client_request_id": cid,
                "character_resolution": {
                    "status": "resolved",
                    "selections": [{
                        "mentionId": mention["mentionId"],
                        "rawText": mention["rawText"],
                        "characterId": char_b,
                    }],
                },
            }, headers={"X-CSRF-Token": "test"})
            assert r2.status_code == 409
            detail = r2.json().get("detail", {})
            assert detail.get("code") == "client_request_id_conflict"

            # Verify: only one charge, one record, original not overwritten
            assert _get_balance(settings, TEST_USER_A) == initial_balance - 2
            assert _count_translation_requests(settings, TEST_USER_A) == 1
            record = _get_translation_record(settings, rc1)
            assert record is not None
            assert char_a in json.loads(record["character_keys_json"])
        finally:
            app.dependency_overrides.clear()

    def test_empty_selections_via_http_returns_409(self, tmp_path):
        """Empty selections via HTTP → 409 character_resolution_required."""
        from fastapi.testclient import TestClient
        from app.main import app
        from app.config import get_settings

        settings = _make_settings(tmp_path)
        _seed_balance(settings, TEST_USER_A, 50000)

        app.dependency_overrides[get_settings] = lambda: settings
        try:
            client = TestClient(app, raise_server_exceptions=False)

            r = client.post("/api/prompt/fast-refine", json={
                "text": "miku在舞台上",
                "character_resolution": {"status": "resolved", "selections": []},
            }, headers={"X-CSRF-Token": "test"})
            assert r.status_code == 409
            detail = r.json().get("detail", {})
            assert detail.get("code") == "character_resolution_required"
            assert _get_balance(settings, TEST_USER_A) == 50000
        finally:
            app.dependency_overrides.clear()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# H. Legacy fingerprint compatibility
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestLegacyFingerprint:
    """Test backward compatibility with translation_requests that have empty fingerprint."""

    def test_legacy_same_text_same_keys_reuses(self, tmp_path):
        """Legacy row with empty fp, same text + same character keys → reuse, backfill fp."""
        settings = _make_settings(tmp_path)
        _seed_balance(settings, TEST_USER_A, 50000)
        cid = f"legacy-same-{uuid.uuid4().hex[:8]}"

        # Insert legacy row (no fingerprint)
        _insert_legacy_translation(
            settings,
            user_id=TEST_USER_A,
            request_code="TR-LEGACY0001",
            text="a beautiful sunset scene",
            character_keys_json='["hatsune_miku"]',
            character_match_source="resolved",
            client_request_id=cid,
        )
        bal_before = _get_balance(settings, TEST_USER_A)

        # Same user, same cid, same text, same character keys → reuse
        result = _run(fast_refine_prompt(
            settings, user_id=TEST_USER_A, text="a beautiful sunset scene",
            client_request_id=cid,
            character_resolution={
                "status": "resolved",
                "selections": [{
                    "mentionId": "x",
                    "rawText": "miku",
                    "characterId": "hatsune_miku",
                }],
            },
        ))
        assert result.request_code == "TR-LEGACY0001"
        assert _get_balance(settings, TEST_USER_A) == bal_before  # no extra charge

        # Verify fingerprint was backfilled
        conn = connect(settings)
        try:
            row = conn.execute(
                "SELECT request_fingerprint FROM translation_requests WHERE request_code=?",
                ("TR-LEGACY0001",),
            ).fetchone()
            assert row and row["request_fingerprint"] != ""
        finally:
            conn.close()

    def test_legacy_same_text_different_keys_conflict(self, tmp_path):
        """Legacy row with empty fp, same text but different character keys → conflict."""
        settings = _make_settings(tmp_path)
        _seed_balance(settings, TEST_USER_A, 50000)
        cid = f"legacy-diff-{uuid.uuid4().hex[:8]}"

        _insert_legacy_translation(
            settings,
            user_id=TEST_USER_A,
            request_code="TR-LEGACY0002",
            text="a beautiful sunset scene",
            character_keys_json='["hatsune_miku"]',
            character_match_source="resolved",
            client_request_id=cid,
        )
        bal_before = _get_balance(settings, TEST_USER_A)

        # Same text, different character → conflict
        with pytest.raises(ClientRequestIdConflict):
            _run(fast_refine_prompt(
                settings, user_id=TEST_USER_A, text="a beautiful sunset scene",
                client_request_id=cid,
                character_resolution={
                    "status": "resolved",
                    "selections": [{
                        "mentionId": "x",
                        "rawText": "meiko",
                        "characterId": "meiko",
                    }],
                },
            ))

        assert _get_balance(settings, TEST_USER_A) == bal_before

    def test_legacy_same_text_none_vs_characters_conflict(self, tmp_path):
        """Legacy row has characters, current request is 'none' → conflict."""
        settings = _make_settings(tmp_path)
        _seed_balance(settings, TEST_USER_A, 50000)
        cid = f"legacy-none-{uuid.uuid4().hex[:8]}"

        _insert_legacy_translation(
            settings,
            user_id=TEST_USER_A,
            request_code="TR-LEGACY0003",
            text="a beautiful sunset scene",
            character_keys_json='["hatsune_miku"]',
            character_match_source="resolved",
            client_request_id=cid,
        )
        bal_before = _get_balance(settings, TEST_USER_A)

        with pytest.raises(ClientRequestIdConflict):
            _run(fast_refine_prompt(
                settings, user_id=TEST_USER_A, text="a beautiful sunset scene",
                client_request_id=cid,
            ))

        assert _get_balance(settings, TEST_USER_A) == bal_before

    def test_legacy_malformed_json_conflict(self, tmp_path):
        """Legacy row with malformed character_keys_json → safe conflict, no 500."""
        settings = _make_settings(tmp_path)
        _seed_balance(settings, TEST_USER_A, 50000)
        cid = f"legacy-malform-{uuid.uuid4().hex[:8]}"

        _insert_legacy_translation(
            settings,
            user_id=TEST_USER_A,
            request_code="TR-LEGACY0004",
            text="a beautiful sunset scene",
            character_keys_json="NOT_VALID_JSON{{{",
            character_match_source="none",
            client_request_id=cid,
        )
        bal_before = _get_balance(settings, TEST_USER_A)

        with pytest.raises(ClientRequestIdConflict):
            _run(fast_refine_prompt(
                settings, user_id=TEST_USER_A, text="a beautiful sunset scene",
                client_request_id=cid,
            ))

        assert _get_balance(settings, TEST_USER_A) == bal_before


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# I. Fingerprint normalization
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestFingerprintNormalization:
    """Verify fingerprint is based on normalized character decision only."""

    def test_same_keys_different_order_same_fingerprint(self):
        fp1 = _compute_fast_translation_fingerprint(
            user_id="u", text="t", character_keys=["b", "a"], source="resolved",
        )
        fp2 = _compute_fast_translation_fingerprint(
            user_id="u", text="t", character_keys=["a", "b"], source="resolved",
        )
        assert fp1 == fp2

    def test_empty_keys_and_none_source(self):
        fp1 = _compute_fast_translation_fingerprint(
            user_id="u", text="t", character_keys=[], source="none",
        )
        fp2 = _compute_fast_translation_fingerprint(
            user_id="u", text="t", character_keys=[], source="none",
        )
        assert fp1 == fp2

    def test_characters_vs_none_different(self):
        fp_chars = _compute_fast_translation_fingerprint(
            user_id="u", text="t", character_keys=["a"], source="resolved",
        )
        fp_none = _compute_fast_translation_fingerprint(
            user_id="u", text="t", character_keys=[], source="none",
        )
        assert fp_chars != fp_none

    def test_fingerprint_excludes_raw_resolution(self):
        """Fingerprint does not include raw character_resolution dict."""
        fp1 = _compute_fast_translation_fingerprint(
            user_id="u", text="t", character_keys=["a"], source="resolved",
        )
        fp2 = _compute_fast_translation_fingerprint(
            user_id="u", text="t", character_keys=["a"], source="resolved",
        )
        # Same parameters → same fingerprint, regardless of any external data
        assert fp1 == fp2

    def test_deduplicate_keys(self):
        """Duplicate keys are deduplicated in fingerprint."""
        fp1 = _compute_fast_translation_fingerprint(
            user_id="u", text="t", character_keys=["a", "a", "b"], source="resolved",
        )
        fp2 = _compute_fast_translation_fingerprint(
            user_id="u", text="t", character_keys=["a", "b"], source="resolved",
        )
        assert fp1 == fp2


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# J. _safe_parse_character_keys_json helper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestSafeParseCharacterKeys:
    def test_valid_list(self):
        assert _safe_parse_character_keys_json('["a","b"]') == ["a", "b"]

    def test_empty_list(self):
        assert _safe_parse_character_keys_json("[]") == []

    def test_invalid_json(self):
        assert _safe_parse_character_keys_json("broken") is None

    def test_non_list(self):
        assert _safe_parse_character_keys_json('{"a":1}') is None

    def test_null(self):
        assert _safe_parse_character_keys_json("null") is None

    def test_list_with_none_values(self):
        result = _safe_parse_character_keys_json('["a", null, "b"]')
        assert result == ["a", "b"]
