"""Tests for fuzzy character match confirmation.

Phase 3 Step 1: Non-exact character matches must require user confirmation
even when only one candidate is found.
"""
from __future__ import annotations

from app.smart_agent.disambiguation_engine import (
    AUTO_RESOLVE_MATCH_TYPES,
    CONFIRM_MATCH_TYPES,
    _requires_confirmation,
    analyze_character_mentions,
    analyze_user_request,
    validate_character_resolution,
)


def _ids(items: list[dict]) -> list[str]:
    return sorted(str(item.get("characterId") or item.get("identity_key") or "") for item in items)


# ── Match type classification ────────────────────────────────────────

class TestMatchTypeClassification:
    """Verify the match type sets are correctly defined."""

    def test_auto_resolve_types_include_exact(self):
        assert "exact_zh" in AUTO_RESOLVE_MATCH_TYPES
        assert "exact_en" in AUTO_RESOLVE_MATCH_TYPES
        assert "tag" in AUTO_RESOLVE_MATCH_TYPES

    def test_confirm_types_include_fuzzy(self):
        assert "zh_substring" in CONFIRM_MATCH_TYPES
        assert "en_short" in CONFIRM_MATCH_TYPES

    def test_no_overlap(self):
        assert AUTO_RESOLVE_MATCH_TYPES.isdisjoint(CONFIRM_MATCH_TYPES)

    def test_requires_confirmation_exact(self):
        assert _requires_confirmation("exact_zh") is False
        assert _requires_confirmation("exact_en") is False
        assert _requires_confirmation("tag") is False

    def test_requires_confirmation_fuzzy(self):
        assert _requires_confirmation("zh_substring") is True
        assert _requires_confirmation("en_short") is True

    def test_requires_confirmation_unknown_defaults_to_true(self):
        assert _requires_confirmation("") is True
        assert _requires_confirmation("unknown_future_type") is True
        assert _requires_confirmation("some_new_type") is True


# ── Exact single-candidate auto-resolve ──────────────────────────────

class TestExactSingleCandidateAutoResolve:
    """Exact matches with one candidate should auto-resolve without dialog."""

    def test_exact_zh_single(self):
        result = analyze_character_mentions("蝴蝶忍")
        assert result["status"] == "resolved"
        assert len(result["resolvedCharacters"]) == 1
        assert result["resolvedCharacters"][0]["characterId"] == "kochou_shinobu"
        assert result["mentions"] == []

    def test_exact_en_single(self):
        result = analyze_character_mentions("Hatsune Miku")
        assert result["status"] == "resolved"
        assert len(result["resolvedCharacters"]) == 1
        assert result["resolvedCharacters"][0]["characterId"] == "hatsune_miku"
        assert result["mentions"] == []

    def test_exact_zh_alias(self):
        """黄金城 is an alias for gold_city."""
        result = analyze_character_mentions("黄金城")
        assert result["status"] == "resolved"
        assert len(result["resolvedCharacters"]) == 1
        assert result["resolvedCharacters"][0]["characterId"] == "gold_city"

    def test_exact_zh_single_character(self):
        result = analyze_character_mentions("米浴")
        assert result["status"] == "resolved"
        assert len(result["resolvedCharacters"]) == 1
        assert result["resolvedCharacters"][0]["characterId"] == "rice_shower"

    def test_exact_zh_full_name(self):
        result = analyze_character_mentions("无声铃鹿")
        assert result["status"] == "resolved"
        assert len(result["resolvedCharacters"]) == 1
        assert result["resolvedCharacters"][0]["characterId"] == "silence_suzuka"


# ── Multi-candidate remains ambiguous ────────────────────────────────

class TestMultiCandidateAmbiguous:
    """Multiple candidates should still require user confirmation."""

    def test_ambiguous_cjk_substring(self):
        """麻美 matches nanami_mami and tomoe_mami."""
        result = analyze_character_mentions("麻美穿风衣")
        assert result["status"] == "ambiguous"
        assert len(result["mentions"]) == 1
        mention = result["mentions"][0]
        assert mention["status"] == "ambiguous"
        assert len(mention["candidates"]) >= 2
        ids = _ids(mention["candidates"])
        assert "nanami_mami" in ids
        assert "tomoe_mami" in ids

    def test_ambiguous_alice(self):
        """爱丽丝 matches multiple characters."""
        result = analyze_character_mentions("爱丽丝")
        assert result["status"] == "ambiguous"
        assert len(result["mentions"]) == 1
        assert len(result["mentions"][0]["candidates"]) >= 2


# ── No match stays not_found ────────────────────────────────────────

class TestNoMatch:
    """Text without character matches should return not_found."""

    def test_no_character(self):
        result = analyze_character_mentions("女孩穿风衣走在街上")
        assert result["status"] == "not_found"
        assert result["resolvedCharacters"] == []
        assert result["mentions"] == []

    def test_butterfly_knot_no_match(self):
        """蝴蝶结 currently has no character match (蝴蝶 substring has only 1 identity)."""
        result = analyze_character_mentions("蝴蝶结")
        assert result["status"] == "not_found"


# ── MatchType field in mentions ──────────────────────────────────────

class TestMatchTypeInMentions:
    """Ambiguous mentions should carry matchType for frontend use."""

    def test_ambiguous_mention_has_match_type(self):
        result = analyze_character_mentions("麻美穿风衣")
        assert result["status"] == "ambiguous"
        mention = result["mentions"][0]
        assert "matchType" in mention
        assert mention["matchType"] in {"zh_substring", "zh_index", "exact_zh"}

    def test_alice_mention_has_match_type(self):
        result = analyze_character_mentions("爱丽丝")
        assert result["status"] == "ambiguous"
        mention = result["mentions"][0]
        assert "matchType" in mention


# ── No-character text stays not_found ───────────────────────────────

class TestOriginalCharacterRequest:
    """Original character requests should be ignored."""

    def test_oc_request(self):
        result = analyze_character_mentions("这是我的原创角色")
        assert result["status"] == "not_found"


# ── Regression: existing behavior preserved ─────────────────────────

class TestRegressionPreserved:
    """Verify existing exact-match behavior is not broken."""

    def test_full_name_in_sentence(self):
        result = analyze_character_mentions("蝴蝶忍站在庭院里")
        assert result["status"] == "resolved"
        assert result["resolvedCharacters"][0]["characterId"] == "kochou_shinobu"

    def test_gold_city_alias_in_sentence(self):
        result = analyze_character_mentions("黄金城夜景")
        assert result["status"] == "resolved"
        assert result["resolvedCharacters"][0]["characterId"] == "gold_city"

    def test_miku_alias_in_sentence(self):
        """初音未来 is the full name, should auto-resolve."""
        result = analyze_character_mentions("初音未来在唱歌")
        assert result["status"] == "resolved"
        assert result["resolvedCharacters"][0]["characterId"] == "hatsune_miku"

    def test_mixed_exact_and_ambiguous(self):
        """蝴蝶忍 exact + 麻美 ambiguous = mixed."""
        result = analyze_character_mentions("蝴蝶忍和麻美")
        assert result["status"] == "mixed"
        assert len(result["resolvedCharacters"]) >= 1
        assert len(result["mentions"]) >= 1


# ── Validate resolution still works ─────────────────────────────────

class TestValidateResolution:
    """validate_character_resolution should still work for ambiguous mentions."""

    def test_validate_select_character(self):
        """Selecting a valid character from ambiguous group should work."""
        parsed = analyze_character_mentions("麻美穿风衣")
        assert parsed["status"] == "ambiguous"
        mention = parsed["mentions"][0]
        first_candidate = mention["candidates"][0]
        resolution = {
            "selections": [{
                "mentionId": mention["mentionId"],
                "rawText": mention["rawText"],
                "characterId": first_candidate["characterId"],
            }]
        }
        validated = validate_character_resolution("麻美穿风衣", resolution)
        assert validated["status"] == "resolved"
        assert len(validated["resolvedCharacters"]) >= 1

    def test_validate_skip_library(self):
        """Skipping character library should work."""
        parsed = analyze_character_mentions("麻美穿风衣")
        assert parsed["status"] == "ambiguous"
        mention = parsed["mentions"][0]
        resolution = {
            "selections": [{
                "mentionId": mention["mentionId"],
                "rawText": mention["rawText"],
                "characterId": None,
                "skipCharacterLibrary": True,
            }]
        }
        validated = validate_character_resolution("麻美穿风衣", resolution)
        assert validated["status"] == "not_found"
        assert len(validated["skippedMentions"]) == 1
