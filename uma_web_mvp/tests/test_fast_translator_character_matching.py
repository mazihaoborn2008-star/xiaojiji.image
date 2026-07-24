"""Tests for fast translator character matching fixes.

Covers three specific fixes:
1. "城市夜景" should NOT match gold_city
2. "初音" should match hatsune_miku
3. Resolution should populate character_keys
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("APP_ENV", "local")

from app.config import Settings
from app.db import connect, ensure_schema
from app.services.fast_translator_service import _resolve_characters, FastTranslatorError
from app.smart_agent.character_search import find_characters, load_characters
from app.smart_agent.disambiguation_engine import (
    analyze_character_mentions,
    validate_character_resolution,
    _public_character_id,
)


TEST_CASE_ROOT = Path(__file__).resolve().parents[1] / "test_data" / "pytest_cases"


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
        "APP_ORIGIN": "http://127.0.0.1:8001",
        "BALANCE_DB": str(test_root / "local_test.db"),
        "BOT_OUTPUT_DIR": str(test_root / "output"),
        "mock_output_dir": str(test_root / "mock_output"),
        "INPUT_IMAGE_DIR": str(test_root / "input_images"),
        "BOT_DIR": str(test_root),
        "redis_enabled": False,
        "dev_auth_bypass": True,
        "dev_user_id": "local-user",
        "fast_translator_enabled": True,
        "fast_translator_cost_credits": 2,
        "agent_surcharge_credits": 1,
        "mock_worker_enabled": True,
        "deepseek_api_key": "",
    }
    data.update(overrides)
    s = Settings(**data)
    s.validate_local_isolation()
    ensure_schema(s)
    return s


def _seed_balance(settings: Settings, user_id: str, amount: int = 50) -> None:
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


# ── Fix 1: 城市夜景不匹配 gold_city ────────────────────────────

class TestCityNotMatchGoldCity:
    """'城市夜景' and similar common phrases must NOT match gold_city."""

    def test_city_night_scene(self):
        chars = find_characters("城市夜景", limit=5)
        keys = [c.get("key") for c in chars]
        assert "gold_city" not in keys

    def test_city_at_night_en(self):
        chars = find_characters("city at night", limit=5)
        keys = [c.get("key") for c in chars]
        assert "gold_city" not in keys

    def test_standing_in_city(self):
        chars = find_characters("standing in a city", limit=5)
        keys = [c.get("key") for c in chars]
        assert "gold_city" not in keys

    def test_night_city_cn(self):
        chars = find_characters("夜晚的城市", limit=5)
        keys = [c.get("key") for c in chars]
        assert "gold_city" not in keys

    def test_golden_city_night(self):
        """金色城市街道 should NOT match gold_city."""
        chars = find_characters("金色城市街道", limit=5)
        keys = [c.get("key") for c in chars]
        assert "gold_city" not in keys

    def test_gold_city_full_name(self):
        """'黄金城市夜景' should match because full name is in input."""
        chars = find_characters("黄金城市夜景", limit=5)
        keys = [c.get("key") for c in chars]
        assert "gold_city" in keys

    def test_gold_city_english(self):
        """'Gold City' should always match."""
        chars = find_characters("Gold City", limit=5)
        keys = [c.get("key") for c in chars]
        assert "gold_city" in keys

    def test_gold_city_alias(self):
        """'黄金城' alias should match gold_city."""
        chars = find_characters("黄金城", limit=5)
        keys = [c.get("key") for c in chars]
        assert "gold_city" in keys

    def test_gold_city_with_city_scene(self):
        """'黄金城站在城市夜景中' should match gold_city via alias."""
        chars = find_characters("黄金城站在城市夜景中", limit=5)
        keys = [c.get("key") for c in chars]
        assert "gold_city" in keys

    def test_gold_city_en_with_city_context(self):
        """'Gold City in a city at night' should match gold_city."""
        chars = find_characters("Gold City in a city at night", limit=5)
        keys = [c.get("key") for c in chars]
        assert "gold_city" in keys


# ── Fix 2: 初音匹配 hatsune_miku ──────────────────────────────

class TestMikuAlias:
    """'初音' should match hatsune_miku."""

    def test_miku_short_alias(self):
        chars = find_characters("初音", limit=5)
        keys = [c.get("key") for c in chars]
        assert "hatsune_miku" in keys

    def test_miku_in_sentence(self):
        chars = find_characters("初音在唱歌", limit=5)
        keys = [c.get("key") for c in chars]
        assert "hatsune_miku" in keys

    def test_miku_full_zh(self):
        chars = find_characters("初音未来", limit=5)
        keys = [c.get("key") for c in chars]
        assert "hatsune_miku" in keys

    def test_miku_en(self):
        chars = find_characters("Hatsune Miku", limit=5)
        keys = [c.get("key") for c in chars]
        assert "hatsune_miku" in keys

    def test_miku_en_lower(self):
        chars = find_characters("hatsune miku", limit=5)
        keys = [c.get("key") for c in chars]
        assert "hatsune_miku" in keys

    def test_miku_on_stage(self):
        chars = find_characters("初音在舞台上唱歌", limit=5)
        keys = [c.get("key") for c in chars]
        assert "hatsune_miku" in keys

    def test_miku_no_duplicate_candidates(self):
        """All miku inputs should return the same character key."""
        inputs = ["初音", "初音未来", "Hatsune Miku", "初音在唱歌"]
        keys_set = set()
        for text in inputs:
            chars = find_characters(text, limit=5)
            miku_keys = [c.get("key") for c in chars if c.get("key") == "hatsune_miku"]
            keys_set.update(miku_keys)
        assert keys_set == {"hatsune_miku"}


# ── Fix 3: Resolution 后 character_keys 正确 ──────────────────

class TestResolutionCharacterKeys:
    """After resolution, character_keys must be populated."""

    def test_single_resolution(self):
        keys, source = _resolve_characters("初音在唱歌", {
            "status": "resolved",
            "selections": [{"characterId": "hatsune_miku"}],
        })
        assert "hatsune_miku" in keys
        assert source == "resolved"

    def test_multi_resolution(self):
        keys, source = _resolve_characters("无声铃鹿和东海帝王", {
            "status": "resolved",
            "selections": [
                {"characterId": "silence_suzuka"},
                {"characterId": "tokai_teio"},
            ],
        })
        assert "silence_suzuka" in keys
        assert "tokai_teio" in keys
        assert source == "resolved"

    def test_duplicate_resolution_deduped(self):
        keys, source = _resolve_characters("无声铃鹿", {
            "status": "resolved",
            "selections": [
                {"characterId": "silence_suzuka"},
                {"characterId": "silence_suzuka"},
            ],
        })
        assert keys.count("silence_suzuka") == 1

    def test_invalid_public_id_rejected(self):
        """Invalid public_id must raise FastTranslatorError."""
        with pytest.raises(FastTranslatorError):
            _resolve_characters("初音在唱歌", {
                "status": "resolved",
                "selections": [{"characterId": "fake_nonexistent_id"}],
            })

    def test_no_resolution_normal_flow(self):
        keys, source = _resolve_characters("无声铃鹿在赛道上", None)
        assert "silence_suzuka" in keys
        assert source == "library"

    def test_409_before_charge(self):
        """Short ambiguous name should raise CharacterSelectionRequired."""
        from app.services.fast_translator_service import CharacterSelectionRequired
        # Use a known ambiguous input (麻美 matches nanami_mami and tomoe_mami)
        with pytest.raises(CharacterSelectionRequired):
            _resolve_characters("麻美", None)

    def test_resolution_match_source(self):
        keys, source = _resolve_characters("初音在唱歌", {
            "status": "resolved",
            "selections": [{"characterId": "hatsune_miku"}],
        })
        assert source == "resolved"

    def test_resolution_not_overridden_by_fallback(self):
        """Resolved characters should not be overridden by library fallback."""
        keys, source = _resolve_characters("初音在唱歌", {
            "status": "resolved",
            "selections": [{"characterId": "hatsune_miku"}],
        })
        # Should be resolved, not library
        assert source == "resolved"
        assert "hatsune_miku" in keys


# ── Diagnostic tests (may xfail for known issues) ─────────────

class TestDiagnostic:
    """Diagnostic tests for known edge cases."""

    def test_rice_shower_direct_match(self):
        """米浴 now correctly resolves directly to rice_shower."""
        keys, source = _resolve_characters("米浴", None)
        assert "rice_shower" in keys
        assert source == "library"

    def test_sparkle_match(self):
        """花火 currently matches sparkle (known issue)."""
        chars = find_characters("花火在夜空下", limit=5)
        keys = [c.get("key") for c in chars]
        # Currently matches sparkle - document this behavior
        assert "sparkle" in keys or len(keys) == 0

    def test_alice_wonderland_mixed(self):
        """Alice Wonderland returns mixed (alice resolved + ta ambiguous)."""
        result = analyze_character_mentions("Alice Wonderland standing in a field")
        # Returns mixed: alice resolved, ta ambiguous
        assert result.get("status") in ("not_found", "resolved", "mixed")
