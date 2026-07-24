"""Tests for character matching false positive prevention.

Ensures that common English and Chinese scene words do NOT trigger
character resolution, while real character names still match correctly.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.smart_agent.disambiguation_engine import analyze_character_mentions


# ────────────────────────────────────────────────────────────
# A. Must NOT match (scene words)
# ────────────────────────────────────────────────────────────

class TestNoFalsePositives:
    """Common scene words must return not_found."""

    @pytest.mark.parametrize("text", [
        "photo",
        "anime",
        "at a",
        "anime style",
        "a photo",
        "cinematic anime photo",
        "at a train station",
        "night city lights",
        "blue sky",
        "white dress",
        "black coat",
        "side view",
        "looking back",
        "train station",
        "school uniform",
        "bedroom",
        "classroom",
    ])
    def test_scene_word_no_match(self, text):
        result = analyze_character_mentions(text)
        assert result["status"] == "not_found", (
            f"'{text}' should not trigger character matching, "
            f"got status={result['status']}, mentions={result['mentions']}"
        )
        assert result["mentions"] == []
        assert result["resolvedCharacters"] == []

    @pytest.mark.parametrize("text", [
        "rainy night train station platform, transparent umbrella, neon lights, low angle",
        "cute anime girl holding umbrella in night rain, neon lights, low angle",
        "night city street with colorful lights, wet road after rain, low angle photo",
        "a girl walking on a sunny beach, white dress, side view",
        "sunset over mountains, golden hour, panoramic view",
    ])
    def test_english_scene_prompt_no_match(self, text):
        result = analyze_character_mentions(text)
        assert result["status"] == "not_found"
        assert result["mentions"] == []


# ────────────────────────────────────────────────────────────
# B. Chinese scene words
# ────────────────────────────────────────────────────────────

class TestChineseScenesNoMatch:
    """Chinese scene descriptions must not trigger character resolution."""

    @pytest.mark.parametrize("text", [
        "雨夜车站月台，透明雨伞，霓虹灯",
        "女孩在教室穿校服",
        "白色连衣裙，海边散步",
        "蝴蝶结发饰",
        "黑色长风衣",
        "卧室柔和灯光",
    ])
    def test_chinese_scene_no_match(self, text):
        result = analyze_character_mentions(text)
        assert result["status"] == "not_found"
        assert result["mentions"] == []


# ────────────────────────────────────────────────────────────
# C. Real characters must match
# ────────────────────────────────────────────────────────────

class TestRealCharacterMatch:
    """Known characters must be correctly identified."""

    def test_rice_shower_chinese(self):
        result = analyze_character_mentions("米浴")
        assert result["status"] == "resolved"
        ids = [c["characterId"] for c in result["resolvedCharacters"]]
        assert "rice_shower" in ids

    def test_rice_shower_english(self):
        result = analyze_character_mentions("rice shower")
        assert result["status"] == "resolved"
        ids = [c["characterId"] for c in result["resolvedCharacters"]]
        assert "rice_shower" in ids

    def test_rice_shower_with_franchise(self):
        result = analyze_character_mentions("rice shower (umamusume)")
        assert result["status"] == "resolved"
        ids = [c["characterId"] for c in result["resolvedCharacters"]]
        assert "rice_shower" in ids

    def test_rice_shower_in_context(self):
        result = analyze_character_mentions("赛马娘里的米浴")
        assert result["status"] == "resolved"
        ids = [c["characterId"] for c in result["resolvedCharacters"]]
        assert "rice_shower" in ids


# ────────────────────────────────────────────────────────────
# D. Real ambiguity must be preserved
# ────────────────────────────────────────────────────────────

class TestAmbiguityPreserved:
    """Known ambiguous names must still require confirmation."""

    def test_mami_ambiguous(self):
        result = analyze_character_mentions("麻美穿风衣")
        assert result["status"] in ("ambiguous", "mixed")
        assert len(result["mentions"]) >= 1
        mention = result["mentions"][0]
        assert len(mention["candidates"]) >= 2

    def test_alice_ambiguous(self):
        result = analyze_character_mentions("爱丽丝穿校服")
        assert result["status"] in ("ambiguous", "mixed")
        assert len(result["mentions"]) >= 1


# ────────────────────────────────────────────────────────────
# E. Franchise names
# ────────────────────────────────────────────────────────────

class TestFranchiseNames:
    """Franchise names alone should not resolve to a specific character."""

    def test_umamusume_keyword(self):
        result = analyze_character_mentions("umamusume")
        # May be ambiguous (multiple characters) but must NOT auto-resolve
        if result["status"] != "not_found":
            # If it matches, it must be ambiguous, not resolved
            assert result["status"] in ("ambiguous", "mixed")
            assert len(result["resolvedCharacters"]) == 0

    def test_uma_musume_separate(self):
        result = analyze_character_mentions("uma musume")
        # Should not match individual characters from franchise name alone
        if result["status"] != "not_found":
            assert result["status"] in ("ambiguous", "mixed")
