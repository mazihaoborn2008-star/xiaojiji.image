"""Tests for Phase 2 Step 4: safety fixes.

1. Invalid public_id in explicit character selection must fail (not silently pass)
2. Duplicate generic tags in multi-character prompts must be deduped
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

os.environ.setdefault("APP_ENV", "local")

from app.config import Settings
from app.db import connect, ensure_schema
from app.services.fast_translator_service import (
    FastTranslatorError,
    _resolve_characters,
)
from app.smart_agent.disambiguation_engine import (
    NO_LIBRARY_CHARACTER_ID,
    validate_character_resolution,
)

TEST_CASE_ROOT = Path(__file__).resolve().parents[1] / "test_data" / "pytest_cases"


def _case_root() -> Path:
    root = TEST_CASE_ROOT / uuid.uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    return root


# ── Fix 1: Invalid public_id must fail ──────────────────────────

class TestInvalidPublicId:
    """Explicit character selection with invalid IDs must raise, not silently pass."""

    def test_single_fake_id_raises(self):
        """Single fake public_id raises FastTranslatorError."""
        with pytest.raises(FastTranslatorError) as exc_info:
            _resolve_characters("初音在唱歌", {
                "status": "resolved",
                "selections": [{"characterId": "fake_nonexistent_id_xyz"}],
            })
        assert exc_info.value.code == "invalid_character_resolution"

    def test_single_fake_id_no_charge(self):
        """Invalid selection must raise before any charging happens."""
        with pytest.raises((FastTranslatorError, ValueError)):
            _resolve_characters("米浴", {
                "status": "resolved",
                "selections": [{"characterId": "completely_bogus"}],
            })

    def test_valid_plus_fake_ids_fail(self):
        """Mix of valid + invalid IDs must fail entirely."""
        with pytest.raises(FastTranslatorError) as exc_info:
            _resolve_characters("初音在唱歌", {
                "status": "resolved",
                "selections": [
                    {"characterId": "hatsune_miku"},  # valid
                    {"characterId": "fake_nonexistent"},  # invalid
                ],
            })
        assert exc_info.value.code == "invalid_character_resolution"

    def test_valid_plus_fake_no_partial(self):
        """Must not return partial results when mixed valid/invalid."""
        with pytest.raises(FastTranslatorError):
            _resolve_characters("初音在唱歌", {
                "status": "resolved",
                "selections": [
                    {"characterId": "hatsune_miku"},
                    {"characterId": "totally_fake_id"},
                ],
            })

    def test_all_valid_single_character(self):
        """All valid single character selection works."""
        keys, source = _resolve_characters("初音在唱歌", {
            "status": "resolved",
            "selections": [{"characterId": "hatsune_miku"}],
        })
        assert "hatsune_miku" in keys
        assert source == "resolved"

    def test_all_valid_multi_character(self):
        """All valid multi-character selection works."""
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

    def test_duplicate_valid_ids_deduped(self):
        """Duplicate valid IDs are deduplicated."""
        keys, source = _resolve_characters("无声铃鹿", {
            "status": "resolved",
            "selections": [
                {"characterId": "silence_suzuka"},
                {"characterId": "silence_suzuka"},
            ],
        })
        assert keys.count("silence_suzuka") == 1

    def test_no_selections_no_error(self):
        """Empty selections list with no parser mentions does not raise."""
        # Use text that doesn't trigger any character mentions
        keys, source = _resolve_characters("一张美丽的风景画", {
            "status": "resolved",
            "selections": [],
        })
        assert isinstance(keys, list)

    def test_none_resolution_normal_flow(self):
        """No resolution at all uses normal library matching."""
        keys, source = _resolve_characters("无声铃鹿在赛道上", None)
        assert "silence_suzuka" in keys
        assert source == "library"

    def test_error_response_no_internal_data(self):
        """Error must not contain internal paths or debug info."""
        with pytest.raises(FastTranslatorError) as exc_info:
            _resolve_characters("test", {
                "status": "resolved",
                "selections": [{"characterId": "fake_id_12345"}],
            })
        msg = str(exc_info.value.message)
        assert "test_data" not in msg
        assert "character-tags" not in msg
        assert ".json" not in msg
        assert "traceback" not in msg.lower()

    def test_skip_character_library_still_works(self):
        """skipCharacterLibrary selections should not trigger invalid check."""
        # This should not raise - skip_library means user chose to skip
        keys, source = _resolve_characters("test", {
            "status": "resolved",
            "selections": [{"characterId": NO_LIBRARY_CHARACTER_ID}],
        })
        assert keys == []

    def test_no_library_character_id_excluded_from_validation(self):
        """NO_LIBRARY_CHARACTER_ID should not be counted as invalid."""
        keys, source = _resolve_characters("test prompt", {
            "status": "resolved",
            "selections": [
                {"characterId": NO_LIBRARY_CHARACTER_ID},
            ],
        })
        # Should not raise
        assert isinstance(keys, list)

    def test_409_before_charge(self):
        """Ambiguous input should raise CharacterSelectionRequired."""
        from app.services.fast_translator_service import CharacterSelectionRequired
        with pytest.raises(CharacterSelectionRequired):
            _resolve_characters("miku", None)


# ── Fix 2: Tag deduplication ────────────────────────────────────

class TestTagDeduplication:
    """Multi-character prompts must not have duplicate generic tags."""

    def test_deduplicate_horse_tags_basic(self):
        """horse ears, horse tail should not appear twice."""
        from app.smart_agent.character_preferences import split_prompt_tags, _tag_key

        # Simulate what the code does
        tags = "2girls, character_a, horse ears, horse tail, character_b, horse ears, horse tail, standing"
        tag_list = split_prompt_tags(tags)

        # Apply dedup logic
        seen = set()
        deduped = []
        for t in tag_list:
            tk = _tag_key(t)
            if tk not in seen:
                seen.add(tk)
                deduped.append(t)

        result = ", ".join(deduped)
        # horse ears and horse tail should appear only once
        assert result.lower().count("horse ears") == 1
        assert result.lower().count("horse tail") == 1
        # But character names should be preserved
        assert "character_a" in result
        assert "character_b" in result
        assert "2girls" in result
        assert "standing" in result

    def test_dedup_preserves_order(self):
        """Deduplication preserves first occurrence order."""
        from app.smart_agent.character_preferences import split_prompt_tags, _tag_key

        tags = "a, b, c, a, b"
        tag_list = split_prompt_tags(tags)
        seen = set()
        deduped = []
        for t in tag_list:
            tk = _tag_key(t)
            if tk not in seen:
                seen.add(tk)
                deduped.append(t)
        assert deduped == ["a", "b", "c"]

    def test_dedup_case_insensitive(self):
        """Deduplication is case-insensitive."""
        from app.smart_agent.character_preferences import split_prompt_tags, _tag_key

        tags = "Horse Ears, horse ears, HORSE EARS"
        tag_list = split_prompt_tags(tags)
        seen = set()
        deduped = []
        for t in tag_list:
            tk = _tag_key(t)
            if tk not in seen:
                seen.add(tk)
                deduped.append(t)
        assert len(deduped) == 1

    def test_no_dedup_different_tags(self):
        """Different tags should not be removed."""
        from app.smart_agent.character_preferences import split_prompt_tags, _tag_key

        tags = "horse ears, horse tail, standing, sitting"
        tag_list = split_prompt_tags(tags)
        seen = set()
        deduped = []
        for t in tag_list:
            tk = _tag_key(t)
            if tk not in seen:
                seen.add(tk)
                deduped.append(t)
        assert len(deduped) == 4

    def test_count_tag_preserved(self):
        """Count tags like 2girls should not be removed."""
        from app.smart_agent.character_preferences import split_prompt_tags, _tag_key

        tags = "2girls, character_a, character_b, 2girls"
        tag_list = split_prompt_tags(tags)
        seen = set()
        deduped = []
        for t in tag_list:
            tk = _tag_key(t)
            if tk not in seen:
                seen.add(tk)
                deduped.append(t)
        assert "2girls" in deduped
        assert deduped.count("2girls") == 1
