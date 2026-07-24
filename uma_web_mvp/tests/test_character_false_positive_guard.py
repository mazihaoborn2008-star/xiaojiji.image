"""Tests for character matching false positive prevention and exact-only matching.

Ensures that common English and Chinese scene words do NOT trigger
character resolution, while real character names still match correctly
with exact-only character ID sets.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.smart_agent.disambiguation_engine import analyze_character_mentions
from app.smart_agent.character_index import (
    _ZH_SUBSTRING_INDEX, _ZH_NAME_INDEX, _ZH_ALIAS_INDEX,
    _EN_SHORT_INDEX, _FRANCHISE_INDEX, _contains_cjk_or_kana,
)


# ────────────────────────────────────────────────────────────
# A. CJK index structure
# ────────────────────────────────────────────────────────────

class TestCJKIndexStructure:
    """Verify CJK index language isolation."""

    def test_zh_substring_no_ascii_keys(self):
        """All _ZH_SUBSTRING_INDEX keys must contain CJK/Kana."""
        for key in _ZH_SUBSTRING_INDEX:
            assert _contains_cjk_or_kana(key), (
                f"Pure ASCII key {key!r} found in _ZH_SUBSTRING_INDEX"
            )

    def test_zh_name_no_ascii_keys(self):
        """All _ZH_NAME_INDEX keys must contain CJK/Kana."""
        for key in _ZH_NAME_INDEX:
            assert _contains_cjk_or_kana(key), (
                f"Pure ASCII key {key!r} found in _ZH_NAME_INDEX"
            )

    def test_zh_alias_no_ascii_keys(self):
        """All _ZH_ALIAS_INDEX keys must contain CJK/Kana."""
        for key in _ZH_ALIAS_INDEX:
            assert _contains_cjk_or_kana(key), (
                f"Pure ASCII key {key!r} found in _ZH_ALIAS_INDEX"
            )

    def test_specific_absent_keys(self):
        """Known problematic ASCII keys must not exist in ZH indexes."""
        absent = ["to", "me", "ta", "ik", "miku", "zero", "city", "gold",
                  "black", "love", "sky", "photo", "anime", "umamusume"]
        for key in absent:
            assert key not in _ZH_SUBSTRING_INDEX
            assert key not in _ZH_NAME_INDEX
            assert key not in _ZH_ALIAS_INDEX


# ────────────────────────────────────────────────────────────
# B. EN short blocked words
# ────────────────────────────────────────────────────────────

class TestENShortBlockedWords:
    """Verify blocked words are excluded from en_short index."""

    def test_blocked_words_not_in_en_short(self):
        from app.smart_agent.character_index import _EN_SHORT_BLOCKED_WORDS
        blocked = ["black", "blue", "city", "coat", "dress", "gold", "green",
                   "light", "love", "night", "rain", "red", "silver", "sky",
                   "snow", "special", "spring", "star", "sun", "summer",
                   "white", "winter", "umamusume"]
        for word in blocked:
            assert word not in _EN_SHORT_INDEX, (
                f"Blocked word {word!r} found in _EN_SHORT_INDEX"
            )

    def test_franchise_tokens_not_in_en_short(self):
        """Franchise tokens must not be in en_short index."""
        for franchise_key in _FRANCHISE_INDEX:
            for token in franchise_key.split():
                if len(token) >= 3:
                    assert token not in _EN_SHORT_INDEX, (
                        f"Franchise token {token!r} found in _EN_SHORT_INDEX"
                    )


# ────────────────────────────────────────────────────────────
# C. Must NOT match (scene words)
# ────────────────────────────────────────────────────────────

class TestNoFalsePositives:
    """Common scene words must return not_found."""

    @pytest.mark.parametrize("text", [
        "photo", "anime", "at a", "anime style", "a photo",
        "cinematic anime photo", "at a train station", "night city lights",
        "blue sky", "white dress", "black coat", "side view", "looking back",
        "train station", "school uniform", "bedroom", "classroom",
        "golden light", "city lights", "love story",
    ])
    def test_scene_word_no_match(self, text):
        result = analyze_character_mentions(text)
        assert result["status"] == "not_found"
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


# ────────────────────────────────────────────────────────────
# D. Chinese scene words
# ────────────────────────────────────────────────────────────

class TestChineseScenesNoMatch:
    """Chinese scene descriptions must not trigger character resolution."""

    @pytest.mark.parametrize("text", [
        "雨夜车站月台，透明雨伞，霓虹灯", "女孩在教室穿校服",
        "白色连衣裙，海边散步", "蝴蝶结发饰", "黑色长风衣", "卧室柔和灯光",
    ])
    def test_chinese_scene_no_match(self, text):
        result = analyze_character_mentions(text)
        assert result["status"] == "not_found"


# ────────────────────────────────────────────────────────────
# E. Franchise names
# ────────────────────────────────────────────────────────────

class TestFranchiseNames:
    """Franchise names alone must not resolve to a specific character."""

    @pytest.mark.parametrize("text", [
        "umamusume", "uma musume", "赛马娘", "anime", "other anime", "其他动漫",
    ])
    def test_franchise_no_character_match(self, text):
        result = analyze_character_mentions(text)
        assert result["status"] == "not_found"
        assert result["mentions"] == []
        assert result["resolvedCharacters"] == []

    def test_umamusume_with_character(self):
        result = analyze_character_mentions("umamusume rice shower")
        ids = set(c["characterId"] for c in result["resolvedCharacters"])
        assert ids == {"rice_shower"}

    def test_franchise_hint_extraction(self):
        from app.smart_agent.character_index import extract_franchise_hints
        hints = extract_franchise_hints("umamusume rice shower")
        assert "umamusume" in hints


# ────────────────────────────────────────────────────────────
# F. Exact-only character matching
# ────────────────────────────────────────────────────────────

class TestExactOnlyMatch:
    """Characters must resolve to exact ID sets, no extra characters."""

    def test_special_week_exact(self):
        result = analyze_character_mentions("special week")
        ids = set(c["characterId"] for c in result["resolvedCharacters"])
        assert ids == {"special_week"}, f"Expected {{special_week}}, got {ids}"

    def test_gold_ship_exact(self):
        result = analyze_character_mentions("gold ship")
        ids = set(c["characterId"] for c in result["resolvedCharacters"])
        assert ids == {"gold_ship"}

    def test_rice_shower_exact(self):
        result = analyze_character_mentions("rice shower")
        ids = set(c["characterId"] for c in result["resolvedCharacters"])
        assert ids == {"rice_shower"}

    def test_rice_shower_with_franchise_exact(self):
        result = analyze_character_mentions("rice shower (umamusume)")
        ids = set(c["characterId"] for c in result["resolvedCharacters"])
        assert ids == {"rice_shower"}

    def test_umamusume_rice_shower_exact(self):
        result = analyze_character_mentions("umamusume rice shower")
        ids = set(c["characterId"] for c in result["resolvedCharacters"])
        assert ids == {"rice_shower"}

    def test_rice_shower_chinese_exact(self):
        result = analyze_character_mentions("米浴")
        ids = set(c["characterId"] for c in result["resolvedCharacters"])
        assert ids == {"rice_shower"}

    def test_rice_shower_chinese_context_exact(self):
        result = analyze_character_mentions("赛马娘里的米浴")
        ids = set(c["characterId"] for c in result["resolvedCharacters"])
        assert ids == {"rice_shower"}


# ────────────────────────────────────────────────────────────
# G. Legitimate short names preserved
# ────────────────────────────────────────────────────────────

class TestLegitShortNames:
    """Legitimate character short names must keep their current behavior."""

    def test_miku_behavior(self):
        """miku has 1 en_short identity (hatsune_miku) → not_found (auto-resolve disabled for en_short)."""
        result = analyze_character_mentions("miku")
        assert result["status"] == "not_found"

    def test_zero_behavior(self):
        """zero has 1 en_short identity → not_found."""
        result = analyze_character_mentions("zero")
        assert result["status"] == "not_found"

    def test_ia_resolves(self):
        """ia is a full character name → resolved."""
        result = analyze_character_mentions("ia")
        ids = set(c["characterId"] for c in result["resolvedCharacters"])
        assert ids == {"ia"}

    def test_gold_no_match(self):
        """gold is a blocked word → not_found."""
        result = analyze_character_mentions("gold")
        assert result["status"] == "not_found"

    def test_special_no_match(self):
        """special is a blocked word → not_found."""
        result = analyze_character_mentions("special")
        assert result["status"] == "not_found"

    def test_rice_no_match(self):
        """rice alone → not_found (not a full character name)."""
        result = analyze_character_mentions("rice")
        assert result["status"] == "not_found"


# ────────────────────────────────────────────────────────────
# H. Real ambiguity preserved
# ────────────────────────────────────────────────────────────

class TestAmbiguityPreserved:
    """Known ambiguous names must still require confirmation."""

    def test_mami_ambiguous(self):
        result = analyze_character_mentions("麻美穿风衣")
        assert result["status"] in ("ambiguous", "mixed")
        assert len(result["mentions"]) >= 1

    def test_alice_ambiguous(self):
        result = analyze_character_mentions("爱丽丝穿校服")
        assert result["status"] in ("ambiguous", "mixed")
        assert len(result["mentions"]) >= 1
