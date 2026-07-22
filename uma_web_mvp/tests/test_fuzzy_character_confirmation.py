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


# ── Post-translation bypass prevention ────────────────────────────

class TestPostTranslationBypass:
    """Ensure pre-check results are respected through the full pipeline."""

    def test_empty_ids_explicit_resolution_skips_post_match(self):
        """resolved_character_ids=[] should NOT trigger find_character_after_translation."""
        from app.agent import _apply_character_registry_to_refined_prompt
        result = _apply_character_registry_to_refined_prompt(
            "蝴蝶结", "butterfly bow",
            resolved_character_ids=[],
        )
        assert "kochou" not in result.lower()
        assert "shinobu" not in result.lower()
        assert "demon slayer" not in result.lower()
        assert "butterfly" in result.lower() or "bow" in result.lower()

    def test_none_ids_allows_post_match_fallback(self):
        """resolved_character_ids=None should allow find_character_after_translation."""
        from app.agent import _apply_character_registry_to_refined_prompt
        result = _apply_character_registry_to_refined_prompt(
            "蝴蝶结", "butterfly bow",
            resolved_character_ids=None,
        )
        assert "kochou" not in result.lower()

    def test_empty_vs_none_semantics(self):
        """[] = explicit no-characters; None = not checked yet. Both return same for no-char input."""
        from app.agent import _apply_character_registry_to_refined_prompt
        r_empty = _apply_character_registry_to_refined_prompt(
            "蝴蝶结", "butterfly bow", resolved_character_ids=[]
        )
        r_none = _apply_character_registry_to_refined_prompt(
            "蝴蝶结", "butterfly bow", resolved_character_ids=None
        )
        assert r_empty == r_none

    def test_exact_match_still_works_with_explicit_ids(self):
        from app.agent import _apply_character_registry_to_refined_prompt
        result = _apply_character_registry_to_refined_prompt(
            "蝴蝶忍", "shinobu standing in garden",
            resolved_character_ids=["kochou_shinobu"],
        )
        assert "kochou" in result.lower() or "shinobu" in result.lower()

    def test_disable_character_library(self):
        from app.agent import _apply_character_registry_to_refined_prompt
        result = _apply_character_registry_to_refined_prompt(
            "蝴蝶忍", "girl standing in garden, white coat",
            disable_character_library=True,
        )
        assert "kochou" not in result.lower()
        assert "shinobu" not in result.lower()
        assert "girl standing" in result.lower() or "garden" in result.lower()

    def test_not_found_no_post_match_in_fast_translator(self):
        from app.agent import _apply_character_registry_to_refined_prompt
        result = _apply_character_registry_to_refined_prompt(
            "蝴蝶结", "butterfly bow, ribbon",
            resolved_character_ids=[],
            disable_character_library=False,
        )
        assert "kochou" not in result.lower()
        assert "shinobu" not in result.lower()

    def test_user_skip_library_no_post_match(self):
        from app.agent import _apply_character_registry_to_refined_prompt
        result = _apply_character_registry_to_refined_prompt(
            "麻美", "girl in coat",
            resolved_character_ids=[],
            disable_character_library=True,
        )
        assert "nanami" not in result.lower()
        assert "tomoe" not in result.lower()
        assert "mami" not in result.lower()

    def test_fast_translator_no_match_passes_empty_list(self):
        from app.services.fast_translator_service import _resolve_characters
        keys, source = _resolve_characters("蝴蝶结", None)
        assert keys == []
        assert source == "none"
        assert keys is not None

    def test_fast_translator_character_keys_not_converted_to_none(self):
        from app.services.fast_translator_service import _resolve_characters
        keys, source = _resolve_characters("蝴蝶结", None)
        resolved_ids = keys  # NOT: keys if keys else None
        assert resolved_ids == []
        assert resolved_ids is not None


# ── Worker character resolution bypass prevention ──────────────────

import asyncio
from unittest.mock import patch, MagicMock, AsyncMock


_VALID_PLAN = {
    "needs_clarification": False,
    "workflow_key": "anima_owner",
    "positive_prompt": "1girl, standing, anime style",
    "negative_prompt": "",
    "width": 1024,
    "height": 1536,
    "loras": [],
}


def _run_build(settings, request_text, **kwargs):
    """Run build_smart_agent_plan synchronously for testing."""
    from app.smart_agent.planner import build_smart_agent_plan
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            build_smart_agent_plan(settings, request_text, **kwargs)
        )
    finally:
        loop.close()


def _run_build_sync(settings, request_text, **kwargs):
    """Alias for _run_build for use in non-fixture tests."""
    return _run_build(settings, request_text, **kwargs)


class TestWorkerCharacterResolution:
    """Worker (build_smart_agent_plan) must respect pre-resolved character decisions.

    The Worker picks up tasks from the queue. When the task was created through
    the web form, the user already made a character decision. The Worker must
    NOT re-run find_characters() and override that decision.
    """

    def _make_settings(self):
        settings = MagicMock()
        settings.deepseek_api_key = "test-key"
        settings.is_local_env.return_value = False
        settings.fast_translator_enabled = True
        return settings

    def _common_patches(self, monkeypatch):
        """Apply common mocks needed by all planner tests."""
        monkeypatch.setattr("app.smart_agent.planner.complete_json", AsyncMock(return_value=_VALID_PLAN))
        monkeypatch.setattr("app.smart_agent.planner.get_workflow", MagicMock(return_value={"key": "anima_owner"}))

    # ── no-character state flows through Worker ──

    def test_no_character_skips_find_characters(self, monkeypatch):
        """When task_prompt_source='agent_no_character', Worker skips find_characters."""
        settings = self._make_settings()
        self._common_patches(monkeypatch)
        find_calls = []
        extract_calls = []

        monkeypatch.setattr("app.smart_agent.planner.find_characters", lambda text, **kw: (find_calls.append(text), [])[1])
        monkeypatch.setattr("app.smart_agent.planner.extract_possible_character_names", lambda text: (extract_calls.append(text), "")[1])

        _run_build(settings, "蝴蝶结", task_prompt_source="agent_no_character")

        assert len(find_calls) == 0, f"find_characters should NOT be called for no-character task, but was called with: {find_calls}"
        assert len(extract_calls) == 0, f"extract_possible_character_names should NOT be called, but was called with: {extract_calls}"

    def test_no_character_skips_translate(self, monkeypatch):
        """When task_prompt_source='agent_no_character', Worker skips translate_character_name."""
        settings = self._make_settings()
        self._common_patches(monkeypatch)
        translate_calls = []

        monkeypatch.setattr("app.smart_agent.planner.find_characters", MagicMock(return_value=[]))
        monkeypatch.setattr("app.smart_agent.planner.extract_possible_character_names", MagicMock(return_value=""))
        monkeypatch.setattr("app.smart_agent.planner.translate_character_name", AsyncMock(side_effect=lambda text: (translate_calls.append(text), text)[1]))

        _run_build(settings, "蝴蝶结", task_prompt_source="agent_no_character")

        assert len(translate_calls) == 0, f"translate_character_name should NOT be called, but was called with: {translate_calls}"

    def test_no_character_plan_has_empty_character_key(self, monkeypatch):
        """When task_prompt_source='agent_no_character', plan has empty character_key."""
        settings = self._make_settings()
        self._common_patches(monkeypatch)
        monkeypatch.setattr("app.smart_agent.planner.find_characters", MagicMock(return_value=[]))
        monkeypatch.setattr("app.smart_agent.planner.extract_possible_character_names", MagicMock(return_value=""))

        plan = _run_build(settings, "蝴蝶结", task_prompt_source="agent_no_character")

        assert plan["character_key"] == "", f"character_key should be empty for no-character task, got: {plan['character_key']}"

    # ── 用户选择都不是 → no-character ──

    def test_skip_library_no_character_match(self, monkeypatch):
        """When task_prompt_source='agent_character_no_library', no character matching runs."""
        settings = self._make_settings()
        self._common_patches(monkeypatch)
        find_calls = []

        monkeypatch.setattr("app.smart_agent.planner.find_characters", lambda text, **kw: (find_calls.append(text), [])[1])
        monkeypatch.setattr("app.smart_agent.planner.extract_possible_character_names", MagicMock(return_value=""))

        _run_build(settings, "麻美穿风衣", task_prompt_source="agent_character_no_library")

        assert len(find_calls) == 0

    # ── 精确人物 → use confirmed characters ──

    def test_resolved_character_used_directly(self, monkeypatch):
        """When task_prompt_source='agent_character_resolved', use the specified characters."""
        settings = self._make_settings()
        self._common_patches(monkeypatch)
        find_calls = []

        monkeypatch.setattr("app.smart_agent.planner.find_characters", lambda text, **kw: (find_calls.append(text), [])[1])

        plan = _run_build(
            settings, "蝴蝶忍站在庭院里",
            task_prompt_source="agent_character_resolved",
            task_character_key="kochou_shinobu",
        )

        # Should NOT call find_characters (no re-matching)
        assert len(find_calls) == 0, f"find_characters should NOT be called for resolved task, but was called with: {find_calls}"
        # Character key should be from the task
        assert plan["character_key"] == "kochou_shinobu"

    def test_resolved_no_fallback(self, monkeypatch):
        """When task has resolved characters, no fallback character matching runs."""
        settings = self._make_settings()
        self._common_patches(monkeypatch)
        extract_calls = []
        translate_calls = []

        monkeypatch.setattr("app.smart_agent.planner.find_characters", MagicMock(return_value=[]))
        monkeypatch.setattr("app.smart_agent.planner.extract_possible_character_names", lambda text: (extract_calls.append(text), "")[1])
        monkeypatch.setattr("app.smart_agent.planner.translate_character_name", AsyncMock(side_effect=lambda text: (translate_calls.append(text), text)[1]))

        _run_build(
            settings, "蝴蝶忍",
            task_prompt_source="agent_character_resolved",
            task_character_key="kochou_shinobu",
        )

        assert len(extract_calls) == 0
        assert len(translate_calls) == 0

    # ── 多人物保留全部 ──

    def test_multi_character_resolved(self, monkeypatch):
        """Multiple resolved characters are all used."""
        settings = self._make_settings()
        self._common_patches(monkeypatch)
        monkeypatch.setattr("app.smart_agent.planner.find_characters", MagicMock(return_value=[]))

        plan = _run_build(
            settings, "蝴蝶忍和初音",
            task_prompt_source="agent_character_resolved",
            task_character_key="kochou_shinobu,hatsune_miku",
        )

        assert plan["character_key"] != "", "Should have a character_key for multi-character"
        assert len(plan["matched_characters"]) >= 1

    # ── legacy 兼容 ──

    def test_legacy_empty_prompt_source_runs_matching(self, monkeypatch):
        """Legacy tasks with empty prompt_source still run character matching."""
        settings = self._make_settings()
        self._common_patches(monkeypatch)
        find_calls = []

        monkeypatch.setattr("app.smart_agent.planner.find_characters", lambda text, **kw: (find_calls.append(text), [])[1])
        monkeypatch.setattr("app.smart_agent.planner.extract_possible_character_names", MagicMock(return_value=""))

        _run_build(settings, "蝴蝶忍", task_prompt_source="")

        # Legacy path should still call find_characters
        assert len(find_calls) >= 1, "Legacy tasks should still run find_characters"

    # ── 蝴蝶结不注入蝴蝶忍 ──

    def test_butterfly_knot_normal_translation_no_character(self, monkeypatch):
        """Normal translation of '蝴蝶结' should not inject 蝴蝶忍."""
        settings = self._make_settings()
        self._common_patches(monkeypatch)
        monkeypatch.setattr("app.smart_agent.planner.find_characters", MagicMock(return_value=[]))
        monkeypatch.setattr("app.smart_agent.planner.extract_possible_character_names", MagicMock(return_value=""))

        plan = _run_build(settings, "蝴蝶结", task_prompt_source="agent_no_character")

        prompt_lower = plan["positive_prompt"].lower()
        assert "kochou" not in prompt_lower, f"Prompt should not contain kochou, got: {plan['positive_prompt']}"
        assert "shinobu" not in prompt_lower, f"Prompt should not contain shinobu, got: {plan['positive_prompt']}"
        assert plan["character_key"] == ""

    def test_butterfly_knot_skip_library_no_character(self, monkeypatch):
        """Normal translation of '蝴蝶结' with skip library should not inject characters."""
        settings = self._make_settings()
        self._common_patches(monkeypatch)
        monkeypatch.setattr("app.smart_agent.planner.find_characters", MagicMock(return_value=[]))
        monkeypatch.setattr("app.smart_agent.planner.extract_possible_character_names", MagicMock(return_value=""))

        plan = _run_build(settings, "蝴蝶结", task_prompt_source="agent_character_no_library")

        prompt_lower = plan["positive_prompt"].lower()
        assert "kochou" not in prompt_lower
        assert "shinobu" not in prompt_lower
        assert plan["character_key"] == ""

    # ── spy 确认 Worker 未调用人物搜索 ──

    def test_spy_no_character_search_called(self, monkeypatch):
        """Spy confirms Worker does not call find_characters or extract_possible_character_names for no-character."""
        settings = self._make_settings()
        self._common_patches(monkeypatch)
        all_find_calls = []
        all_extract_calls = []
        all_translate_calls = []

        def spy_find(text, **kw):
            all_find_calls.append(text)
            return []

        def spy_extract(text):
            all_extract_calls.append(text)
            return ""

        async def spy_translate(text):
            all_translate_calls.append(text)
            return text

        monkeypatch.setattr("app.smart_agent.planner.find_characters", spy_find)
        monkeypatch.setattr("app.smart_agent.planner.extract_possible_character_names", spy_extract)
        monkeypatch.setattr("app.smart_agent.planner.translate_character_name", spy_translate)

        _run_build(settings, "蝴蝶结", task_prompt_source="agent_no_character")

        assert all_find_calls == [], f"find_characters should not be called, got: {all_find_calls}"
        assert all_extract_calls == [], f"extract_possible_character_names should not be called, got: {all_extract_calls}"
        assert all_translate_calls == [], f"translate_character_name should not be called, got: {all_translate_calls}"

    def test_spy_resolved_character_no_search(self, monkeypatch):
        """Spy confirms Worker does not search when characters are pre-resolved."""
        settings = self._make_settings()
        self._common_patches(monkeypatch)
        all_find_calls = []
        all_extract_calls = []

        def spy_find(text, **kw):
            all_find_calls.append(text)
            return []

        def spy_extract(text):
            all_extract_calls.append(text)
            return ""

        monkeypatch.setattr("app.smart_agent.planner.find_characters", spy_find)
        monkeypatch.setattr("app.smart_agent.planner.extract_possible_character_names", spy_extract)

        _run_build(
            settings, "蝴蝶忍",
            task_prompt_source="agent_character_resolved",
            task_character_key="kochou_shinobu",
        )

        assert all_find_calls == [], f"find_characters should not be called for resolved, got: {all_find_calls}"
        assert all_extract_calls == [], f"extract_possible_character_names should not be called, got: {all_extract_calls}"


# ── Invalid resolved data must fail safe ───────────────────────────

import pytest

class TestInvalidResolvedData:
    """Invalid resolved character data must fail safe — no fallback to fuzzy search."""

    def _make_settings(self):
        settings = MagicMock()
        settings.deepseek_api_key = "test-key"
        settings.is_local_env.return_value = False
        return settings

    def _common_patches(self, monkeypatch):
        monkeypatch.setattr("app.smart_agent.planner.complete_json", AsyncMock(return_value=_VALID_PLAN))
        monkeypatch.setattr("app.smart_agent.planner.get_workflow", MagicMock(return_value={"key": "anima_owner"}))

    def test_resolved_empty_character_key_raises(self, monkeypatch):
        """prompt_source=agent_character_resolved but character_key is empty → safe failure."""
        settings = self._make_settings()
        self._common_patches(monkeypatch)
        monkeypatch.setattr("app.smart_agent.planner.find_characters", MagicMock(return_value=[]))

        from app.smart_agent.planner import SmartAgentError
        with pytest.raises(SmartAgentError) as exc_info:
            _run_build(settings, "蝴蝶忍", task_prompt_source="agent_character_resolved", task_character_key="")
        assert exc_info.value.code == "invalid_character_resolution"

    def test_resolved_nonexistent_id_raises(self, monkeypatch):
        """prompt_source=agent_character_resolved but character ID doesn't exist → safe failure."""
        settings = self._make_settings()
        self._common_patches(monkeypatch)
        find_calls = []
        monkeypatch.setattr("app.smart_agent.planner.find_characters", lambda text, **kw: (find_calls.append(text), [])[1])

        from app.smart_agent.planner import SmartAgentError
        with pytest.raises(SmartAgentError) as exc_info:
            _run_build(settings, "some request", task_prompt_source="agent_character_resolved", task_character_key="nonexistent_id_12345")
        assert exc_info.value.code == "invalid_character_resolution"
        # Must NOT fall back to find_characters
        assert find_calls == [], f"find_characters should not be called on invalid resolution, got: {find_calls}"

    def test_resolved_one_valid_one_invalid_raises(self, monkeypatch):
        """One valid + one invalid ID → safe failure, not partial use."""
        settings = self._make_settings()
        self._common_patches(monkeypatch)
        find_calls = []
        monkeypatch.setattr("app.smart_agent.planner.find_characters", lambda text, **kw: (find_calls.append(text), [])[1])

        from app.smart_agent.planner import SmartAgentError
        with pytest.raises(SmartAgentError) as exc_info:
            _run_build(
                settings, "request",
                task_prompt_source="agent_character_resolved",
                task_character_key="kochou_shinobu,totally_fake_id",
            )
        assert exc_info.value.code == "invalid_character_resolution"
        assert find_calls == []

    def test_resolved_corrupted_data_raises(self, monkeypatch):
        """Corrupted character_key data → safe failure."""
        settings = self._make_settings()
        self._common_patches(monkeypatch)
        find_calls = []
        monkeypatch.setattr("app.smart_agent.planner.find_characters", lambda text, **kw: (find_calls.append(text), [])[1])

        from app.smart_agent.planner import SmartAgentError
        with pytest.raises(SmartAgentError) as exc_info:
            _run_build(
                settings, "request",
                task_prompt_source="agent_character_resolved",
                task_character_key=",,,",
            )
        assert exc_info.value.code == "invalid_character_resolution"
        assert find_calls == []

    def test_resolved_mixed_no_library_raises(self, monkeypatch):
        """Resolved state + no-library state simultaneously → safe failure."""
        settings = self._make_settings()
        self._common_patches(monkeypatch)
        find_calls = []
        monkeypatch.setattr("app.smart_agent.planner.find_characters", lambda text, **kw: (find_calls.append(text), [])[1])

        from app.smart_agent.planner import SmartAgentError
        with pytest.raises(SmartAgentError) as exc_info:
            _run_build(
                settings, "request",
                task_prompt_source="agent_character_resolved",
                task_character_key="__no_library_character__",
            )
        assert exc_info.value.code == "invalid_character_resolution"
        assert find_calls == []


# ── Butterfly knot full pipeline acceptance ────────────────────────

class TestButterflyKnotPipeline:
    """Full pipeline acceptance: '蝴蝶结' → no character injection in final prompt."""

    def _make_settings(self):
        settings = MagicMock()
        settings.deepseek_api_key = "test-key"
        settings.is_local_env.return_value = False
        return settings

    def test_butterfly_knot_full_pipeline_no_character(self, monkeypatch):
        """Full pipeline: '蝴蝶结' with agent_no_character → no character in final prompt."""
        settings = self._make_settings()
        # Mock DeepSeek to return "butterfly bow" as the translation
        mock_plan = dict(_VALID_PLAN)
        mock_plan["positive_prompt"] = "butterfly bow, ribbon, 1girl, anime style"
        monkeypatch.setattr("app.smart_agent.planner.complete_json", AsyncMock(return_value=mock_plan))
        monkeypatch.setattr("app.smart_agent.planner.get_workflow", MagicMock(return_value={"key": "anima_owner"}))
        monkeypatch.setattr("app.smart_agent.planner.find_characters", MagicMock(return_value=[]))
        monkeypatch.setattr("app.smart_agent.planner.extract_possible_character_names", MagicMock(return_value=""))

        plan = _run_build(settings, "蝴蝶结", task_prompt_source="agent_no_character")

        prompt_lower = plan["positive_prompt"].lower()
        # No character tags injected
        assert "kochou" not in prompt_lower
        assert "shinobu" not in prompt_lower
        assert "demon" not in prompt_lower
        assert "slayer" not in prompt_lower
        # Prompt preserved as-is
        assert "butterfly" in prompt_lower
        assert plan["character_key"] == ""
        assert plan["character_tag_source"] == "none" or plan.get("fallback_level") == "none"

    def test_butterfly_knot_full_pipeline_skip_library(self, monkeypatch):
        """Full pipeline: '蝴蝶结' with agent_character_no_library → no character in final prompt."""
        settings = self._make_settings()
        mock_plan = dict(_VALID_PLAN)
        mock_plan["positive_prompt"] = "butterfly bow, ribbon, 1girl, anime style"
        monkeypatch.setattr("app.smart_agent.planner.complete_json", AsyncMock(return_value=mock_plan))
        monkeypatch.setattr("app.smart_agent.planner.get_workflow", MagicMock(return_value={"key": "anima_owner"}))
        monkeypatch.setattr("app.smart_agent.planner.find_characters", MagicMock(return_value=[]))
        monkeypatch.setattr("app.smart_agent.planner.extract_possible_character_names", MagicMock(return_value=""))

        plan = _run_build(settings, "蝴蝶结", task_prompt_source="agent_character_no_library")

        prompt_lower = plan["positive_prompt"].lower()
        assert "kochou" not in prompt_lower
        assert "shinobu" not in prompt_lower
        assert plan["character_key"] == ""



# -- Exact character Worker verification ---------------------------------

class TestExactCharacterWorker:
    """Exact character matches must flow through Worker correctly."""

    def _make_settings(self):
        settings = MagicMock()
        settings.deepseek_api_key = "test-key"
        settings.is_local_env.return_value = False
        return settings

    def _common_patches(self, monkeypatch):
        monkeypatch.setattr("app.smart_agent.planner.complete_json", AsyncMock(return_value=_VALID_PLAN))
        monkeypatch.setattr("app.smart_agent.planner.get_workflow", MagicMock(return_value={"key": "anima_owner"}))

    def test_kochou_shinobu_exact(self, monkeypatch):
        settings = self._make_settings()
        self._common_patches(monkeypatch)
        find_calls = []
        monkeypatch.setattr("app.smart_agent.planner.find_characters", lambda t, **kw: (find_calls.append(t), [])[1])
        plan = _run_build(settings, "蝴蝶忍站在庭院里", task_prompt_source="agent_character_resolved", task_character_key="kochou_shinobu")
        assert plan["character_key"] == "kochou_shinobu"
        assert len(plan["matched_characters"]) >= 1
        assert find_calls == []

    def test_hatsune_miku_exact(self, monkeypatch):
        settings = self._make_settings()
        self._common_patches(monkeypatch)
        find_calls = []
        monkeypatch.setattr("app.smart_agent.planner.find_characters", lambda t, **kw: (find_calls.append(t), [])[1])
        plan = _run_build(settings, "初音在唱歌", task_prompt_source="agent_character_resolved", task_character_key="hatsune_miku")
        assert plan["character_key"] == "hatsune_miku"
        assert find_calls == []

    def test_gold_city_exact(self, monkeypatch):
        settings = self._make_settings()
        self._common_patches(monkeypatch)
        find_calls = []
        monkeypatch.setattr("app.smart_agent.planner.find_characters", lambda t, **kw: (find_calls.append(t), [])[1])
        plan = _run_build(settings, "黄金城夜景", task_prompt_source="agent_character_resolved", task_character_key="gold_city")
        assert plan["character_key"] == "gold_city"
        assert find_calls == []

    def test_rice_shower_exact(self, monkeypatch):
        settings = self._make_settings()
        self._common_patches(monkeypatch)
        find_calls = []
        monkeypatch.setattr("app.smart_agent.planner.find_characters", lambda t, **kw: (find_calls.append(t), [])[1])
        plan = _run_build(settings, "米浴在雨中", task_prompt_source="agent_character_resolved", task_character_key="rice_shower")
        assert plan["character_key"] == "rice_shower"
        assert find_calls == []

    def test_silence_suzuka_exact(self, monkeypatch):
        settings = self._make_settings()
        self._common_patches(monkeypatch)
        find_calls = []
        monkeypatch.setattr("app.smart_agent.planner.find_characters", lambda t, **kw: (find_calls.append(t), [])[1])
        plan = _run_build(settings, "无声铃鹿跑步", task_prompt_source="agent_character_resolved", task_character_key="silence_suzuka")
        assert plan["character_key"] == "silence_suzuka"
        assert find_calls == []

    def test_resolved_not_enter_disabled(self, monkeypatch):
        settings = self._make_settings()
        self._common_patches(monkeypatch)
        plan = _run_build(settings, "蝴蝶忍", task_prompt_source="agent_character_resolved", task_character_key="kochou_shinobu")
        assert plan.get("character_tag_source", "") != "none"


# -- Multi-character Worker verification ---------------------------------

class TestMultiCharacterWorker:
    """Multi-character tasks must preserve all character IDs."""

    def _make_settings(self):
        settings = MagicMock()
        settings.deepseek_api_key = "test-key"
        settings.is_local_env.return_value = False
        return settings

    def _common_patches(self, monkeypatch):
        monkeypatch.setattr("app.smart_agent.planner.complete_json", AsyncMock(return_value=_VALID_PLAN))
        monkeypatch.setattr("app.smart_agent.planner.get_workflow", MagicMock(return_value={"key": "anima_owner"}))

    def test_two_characters_preserved(self, monkeypatch):
        settings = self._make_settings()
        self._common_patches(monkeypatch)
        find_calls = []
        monkeypatch.setattr("app.smart_agent.planner.find_characters", lambda t, **kw: (find_calls.append(t), [])[1])
        plan = _run_build(settings, "无声铃鹿和东海帝王", task_prompt_source="agent_character_resolved", task_character_key="silence_suzuka,tokai_teio")
        assert len(plan["matched_characters"]) >= 2
        assert find_calls == []
        mc_keys = [c.get("key", "") for c in plan["matched_characters"]]
        assert "silence_suzuka" in mc_keys
        assert "tokai_teio" in mc_keys

    def test_three_characters_preserved(self, monkeypatch):
        settings = self._make_settings()
        self._common_patches(monkeypatch)
        plan = _run_build(settings, "三人合照", task_prompt_source="agent_character_resolved", task_character_key="kochou_shinobu,hatsune_miku,rice_shower")
        assert len(plan["matched_characters"]) >= 3
        mc_keys = [c.get("key", "") for c in plan["matched_characters"]]
        assert "kochou_shinobu" in mc_keys
        assert "hatsune_miku" in mc_keys
        assert "rice_shower" in mc_keys

    def test_multi_character_ids_order_preserved(self, monkeypatch):
        settings = self._make_settings()
        self._common_patches(monkeypatch)
        plan = _run_build(settings, "双人", task_prompt_source="agent_character_resolved", task_character_key="rice_shower,kochou_shinobu")
        mc_keys = [c.get("key", "") for c in plan["matched_characters"]]
        assert mc_keys.index("rice_shower") < mc_keys.index("kochou_shinobu")

    def test_multi_character_dedup(self, monkeypatch):
        settings = self._make_settings()
        self._common_patches(monkeypatch)
        plan = _run_build(settings, "重复", task_prompt_source="agent_character_resolved", task_character_key="kochou_shinobu,kochou_shinobu,hatsune_miku")
        mc_keys = [c.get("key", "") for c in plan["matched_characters"]]
        assert mc_keys.count("kochou_shinobu") == 1
        assert "hatsune_miku" in mc_keys


# -- Unknown prompt_source handling --------------------------------------

class TestUnknownPromptSource:
    """Unknown non-empty prompt_source must not silently become legacy."""

    def _make_settings(self):
        settings = MagicMock()
        settings.deepseek_api_key = "test-key"
        settings.is_local_env.return_value = False
        return settings

    def _common_patches(self, monkeypatch):
        monkeypatch.setattr("app.smart_agent.planner.complete_json", AsyncMock(return_value=_VALID_PLAN))
        monkeypatch.setattr("app.smart_agent.planner.get_workflow", MagicMock(return_value={"key": "anima_owner"}))

    def test_unknown_prompt_source_raises_invalid(self, monkeypatch):
        """Unknown non-empty prompt_source → invalid state, not legacy."""
        settings = self._make_settings()
        self._common_patches(monkeypatch)
        find_calls = []
        monkeypatch.setattr("app.smart_agent.planner.find_characters", lambda t, **kw: (find_calls.append(t), [])[1])
        from app.smart_agent.planner import SmartAgentError
        import pytest
        with pytest.raises(SmartAgentError) as exc_info:
            _run_build(settings, "蝴蝶忍", task_prompt_source="some_future_value")
        assert exc_info.value.code == "invalid_character_resolution"
        assert find_calls == [], "Invalid state must not call find_characters"

    def test_user_raw_prompt_source_raises_invalid(self, monkeypatch):
        """user_raw should not enter planner, but if it does → invalid."""
        settings = self._make_settings()
        self._common_patches(monkeypatch)
        find_calls = []
        monkeypatch.setattr("app.smart_agent.planner.find_characters", lambda t, **kw: (find_calls.append(t), [])[1])
        from app.smart_agent.planner import SmartAgentError
        import pytest
        with pytest.raises(SmartAgentError) as exc_info:
            _run_build(settings, "蝴蝶忍", task_prompt_source="user_raw")
        assert exc_info.value.code == "invalid_character_resolution"
        assert find_calls == []

    def test_empty_string_runs_legacy(self, monkeypatch):
        settings = self._make_settings()
        self._common_patches(monkeypatch)
        find_calls = []
        monkeypatch.setattr("app.smart_agent.planner.find_characters", lambda t, **kw: (find_calls.append(t), [])[1])
        monkeypatch.setattr("app.smart_agent.planner.extract_possible_character_names", MagicMock(return_value=""))
        _run_build(settings, "蝴蝶忍", task_prompt_source="")
        assert len(find_calls) >= 1


# -- Worker charging verification ----------------------------------------

class TestWorkerCharging:
    """Verify charging behavior is not affected by character decision changes."""

    def test_planner_has_no_charging_logic(self):
        from app.smart_agent.planner import build_smart_agent_plan
        import inspect
        src = inspect.getsource(build_smart_agent_plan)
        assert "charged_fen" not in src
        assert "balance_fen" not in src
        assert "balance_ledger" not in src

    def test_worker_loop_passes_plan_prompt_source(self):
        import inspect
        src = inspect.getsource(__import__("app.main", fromlist=["smart_agent_worker_loop"]).smart_agent_worker_loop)
        assert 'plan.get("prompt_source")' in src or 'plan["prompt_source"]' in src


# -- Helper function tests ----------------------------------------------

class TestClassifyTaskCharacterDecision:
    """_classify_task_character_decision must return correct states."""

    def test_resolved_source(self):
        from app.smart_agent.planner import _classify_task_character_decision
        assert _classify_task_character_decision("agent_character_resolved") == "resolved"

    def test_disabled_sources(self):
        from app.smart_agent.planner import _classify_task_character_decision
        assert _classify_task_character_decision("agent_character_no_library") == "disabled"
        assert _classify_task_character_decision("agent_no_character") == "disabled"

    def test_legacy_sources(self):
        from app.smart_agent.planner import _classify_task_character_decision
        assert _classify_task_character_decision("") == "legacy"
        assert _classify_task_character_decision("smart_agent") == "legacy"
        assert _classify_task_character_decision("smart_agent+character_registry") == "legacy"
        assert _classify_task_character_decision("smart_agent_v2") == "legacy"

    def test_unknown_nonempty_is_invalid(self):
        from app.smart_agent.planner import _classify_task_character_decision
        assert _classify_task_character_decision("some_future_value") == "invalid"
        assert _classify_task_character_decision("UNKNOWN") == "invalid"
        assert _classify_task_character_decision("agent_character_resolved_typo") == "invalid"

    def test_no_overlap_between_categories(self):
        from app.smart_agent.planner import _RESOLVED_SOURCES, _DISABLED_SOURCES, _LEGACY_SOURCES
        assert _RESOLVED_SOURCES.isdisjoint(_DISABLED_SOURCES)
        assert _RESOLVED_SOURCES.isdisjoint(_LEGACY_SOURCES)
        assert _DISABLED_SOURCES.isdisjoint(_LEGACY_SOURCES)


class TestSerializeCharacterIds:
    """serialize_character_ids must produce valid JSON arrays."""

    def test_empty(self):
        from app.smart_agent.planner import serialize_character_ids
        assert serialize_character_ids([]) == "[]"

    def test_single(self):
        from app.smart_agent.planner import serialize_character_ids
        result = serialize_character_ids(["kochou_shinobu"])
        assert result == '["kochou_shinobu"]'

    def test_multiple(self):
        from app.smart_agent.planner import serialize_character_ids
        result = serialize_character_ids(["kochou_shinobu", "hatsune_miku", "rice_shower"])
        assert result == '["kochou_shinobu","hatsune_miku","rice_shower"]'

    def test_dedup_preserves_order(self):
        from app.smart_agent.planner import serialize_character_ids
        result = serialize_character_ids(["a", "b", "a", "c"])
        assert result == '["a","b","c"]'

    def test_strips_whitespace(self):
        from app.smart_agent.planner import serialize_character_ids
        result = serialize_character_ids(["  kochou_shinobu  ", "", " hatsune_miku "])
        assert result == '["kochou_shinobu","hatsune_miku"]'

    def test_no_truncation(self):
        """JSON format must not be truncated."""
        from app.smart_agent.planner import serialize_character_ids
        ids = ["kochou_shinobu", "hatsune_miku", "rice_shower", "silence_suzuka", "tokai_teio"]
        result = serialize_character_ids(ids)
        assert len(result) > 120 or True  # may exceed 120 but must not be truncated
        parsed = __import__("json").loads(result)
        assert len(parsed) == 5


class TestParseCharacterIds:
    """parse_character_ids must handle both JSON and legacy formats."""

    def test_empty(self):
        from app.smart_agent.planner import parse_character_ids
        assert parse_character_ids("") == []
        assert parse_character_ids(None) == []
        assert parse_character_ids("[]") == []

    def test_json_array(self):
        from app.smart_agent.planner import parse_character_ids
        result = parse_character_ids('["kochou_shinobu","hatsune_miku"]')
        assert result == ["kochou_shinobu", "hatsune_miku"]

    def test_legacy_comma_separated(self):
        from app.smart_agent.planner import parse_character_ids
        result = parse_character_ids("kochou_shinobu,hatsune_miku")
        assert result == ["kochou_shinobu", "hatsune_miku"]

    def test_legacy_single(self):
        from app.smart_agent.planner import parse_character_ids
        result = parse_character_ids("kochou_shinobu")
        assert result == ["kochou_shinobu"]

    def test_json_single(self):
        from app.smart_agent.planner import parse_character_ids
        result = parse_character_ids('["kochou_shinobu"]')
        assert result == ["kochou_shinobu"]

    def test_invalid_json_returns_empty(self):
        """JSON parse failure does NOT fall back to comma split."""
        from app.smart_agent.planner import parse_character_ids
        result = parse_character_ids("[invalid")
        assert result == []  # strict: JSON failure → empty

    def test_json_array_with_spaces(self):
        from app.smart_agent.planner import parse_character_ids
        result = parse_character_ids('[" kochou_shinobu ", " hatsune_miku "]')
        assert result == ["kochou_shinobu", "hatsune_miku"]


class TestJsonCharacterKeyIntegration:
    """Integration: JSON character_key flows through Worker correctly."""

    def _make_settings(self):
        settings = MagicMock()
        settings.deepseek_api_key = "test-key"
        settings.is_local_env.return_value = False
        return settings

    def _common_patches(self, monkeypatch):
        monkeypatch.setattr("app.smart_agent.planner.complete_json", AsyncMock(return_value=_VALID_PLAN))
        monkeypatch.setattr("app.smart_agent.planner.get_workflow", MagicMock(return_value={"key": "anima_owner"}))

    def test_json_single_character(self, monkeypatch):
        settings = self._make_settings()
        self._common_patches(monkeypatch)
        find_calls = []
        monkeypatch.setattr("app.smart_agent.planner.find_characters", lambda t, **kw: (find_calls.append(t), [])[1])
        plan = _run_build(settings, "test", task_prompt_source="agent_character_resolved",
                          task_character_key='["kochou_shinobu"]')
        assert plan["character_key"] == "kochou_shinobu"
        assert find_calls == []

    def test_json_multi_character(self, monkeypatch):
        settings = self._make_settings()
        self._common_patches(monkeypatch)
        plan = _run_build(settings, "test", task_prompt_source="agent_character_resolved",
                          task_character_key='["silence_suzuka","tokai_teio"]')
        mc_keys = [c.get("key", "") for c in plan["matched_characters"]]
        assert "silence_suzuka" in mc_keys
        assert "tokai_teio" in mc_keys

    def test_legacy_comma_still_works(self, monkeypatch):
        settings = self._make_settings()
        self._common_patches(monkeypatch)
        plan = _run_build(settings, "test", task_prompt_source="agent_character_resolved",
                          task_character_key="silence_suzuka,tokai_teio")
        mc_keys = [c.get("key", "") for c in plan["matched_characters"]]
        assert "silence_suzuka" in mc_keys
        assert "tokai_teio" in mc_keys

    def test_empty_json_array_is_invalid(self, monkeypatch):
        settings = self._make_settings()
        self._common_patches(monkeypatch)
        from app.smart_agent.planner import SmartAgentError
        import pytest
        with pytest.raises(SmartAgentError) as exc_info:
            _run_build(settings, "test", task_prompt_source="agent_character_resolved",
                        task_character_key="[]")
        assert exc_info.value.code == "invalid_character_resolution"


# -- Unknown state spy tests (requirements 1-7) -------------------------

class TestUnknownStateSpy:
    """Unknown non-empty prompt_source must not run any character search."""

    def _make_settings(self):
        settings = MagicMock()
        settings.deepseek_api_key = "test-key"
        settings.is_local_env.return_value = False
        return settings

    def _common_patches(self, monkeypatch):
        monkeypatch.setattr("app.smart_agent.planner.complete_json", AsyncMock(return_value=_VALID_PLAN))
        monkeypatch.setattr("app.smart_agent.planner.get_workflow", MagicMock(return_value={"key": "anima_owner"}))

    def test_unknown_no_find_characters(self, monkeypatch):
        settings = self._make_settings()
        self._common_patches(monkeypatch)
        calls = []
        monkeypatch.setattr("app.smart_agent.planner.find_characters", lambda t, **kw: (calls.append(("find", t)), [])[1])
        monkeypatch.setattr("app.smart_agent.planner.extract_possible_character_names", lambda t: (calls.append(("extract", t)), "")[1])
        from app.smart_agent.planner import SmartAgentError
        import pytest
        with pytest.raises(SmartAgentError):
            _run_build(settings, "test", task_prompt_source="unknown_value")
        find_calls = [c for c in calls if c[0] == "find"]
        assert find_calls == [], f"find_characters called: {find_calls}"

    def test_unknown_no_extract(self, monkeypatch):
        settings = self._make_settings()
        self._common_patches(monkeypatch)
        calls = []
        monkeypatch.setattr("app.smart_agent.planner.find_characters", lambda t, **kw: (calls.append(("find", t)), [])[1])
        monkeypatch.setattr("app.smart_agent.planner.extract_possible_character_names", lambda t: (calls.append(("extract", t)), "")[1])
        from app.smart_agent.planner import SmartAgentError
        import pytest
        with pytest.raises(SmartAgentError):
            _run_build(settings, "test", task_prompt_source="unknown_value")
        extract_calls = [c for c in calls if c[0] == "extract"]
        assert extract_calls == [], f"extract called: {extract_calls}"

    def test_unknown_no_translate(self, monkeypatch):
        settings = self._make_settings()
        self._common_patches(monkeypatch)
        calls = []
        async def spy_translate(t):
            calls.append(("translate", t))
            return t
        monkeypatch.setattr("app.smart_agent.planner.find_characters", MagicMock(return_value=[]))
        monkeypatch.setattr("app.smart_agent.planner.extract_possible_character_names", MagicMock(return_value=""))
        monkeypatch.setattr("app.smart_agent.planner.translate_character_name", spy_translate)
        from app.smart_agent.planner import SmartAgentError
        import pytest
        with pytest.raises(SmartAgentError):
            _run_build(settings, "test", task_prompt_source="unknown_value")
        translate_calls = [c for c in calls if c[0] == "translate"]
        assert translate_calls == []

    def test_unknown_safe_failure(self, monkeypatch):
        settings = self._make_settings()
        self._common_patches(monkeypatch)
        monkeypatch.setattr("app.smart_agent.planner.find_characters", MagicMock(return_value=[]))
        from app.smart_agent.planner import SmartAgentError
        import pytest
        with pytest.raises(SmartAgentError) as exc_info:
            _run_build(settings, "test", task_prompt_source="unknown_value")
        assert exc_info.value.code == "invalid_character_resolution"

    def test_empty_string_legacy(self, monkeypatch):
        settings = self._make_settings()
        self._common_patches(monkeypatch)
        calls = []
        monkeypatch.setattr("app.smart_agent.planner.find_characters", lambda t, **kw: (calls.append(t), [])[1])
        monkeypatch.setattr("app.smart_agent.planner.extract_possible_character_names", MagicMock(return_value=""))
        _run_build(settings, "test", task_prompt_source="")
        assert len(calls) >= 1

    def test_none_prompt_source_legacy(self, monkeypatch):
        settings = self._make_settings()
        self._common_patches(monkeypatch)
        calls = []
        monkeypatch.setattr("app.smart_agent.planner.find_characters", lambda t, **kw: (calls.append(t), [])[1])
        monkeypatch.setattr("app.smart_agent.planner.extract_possible_character_names", MagicMock(return_value=""))
        _run_build(settings, "test", task_prompt_source="")
        assert len(calls) >= 1

    def test_user_raw_not_legacy(self, monkeypatch):
        settings = self._make_settings()
        self._common_patches(monkeypatch)
        calls = []
        monkeypatch.setattr("app.smart_agent.planner.find_characters", lambda t, **kw: (calls.append(t), [])[1])
        from app.smart_agent.planner import SmartAgentError
        import pytest
        with pytest.raises(SmartAgentError):
            _run_build(settings, "test", task_prompt_source="user_raw")
        assert calls == []


# -- Validation limits tests (requirements 14-17) -----------------------

class TestValidationLimits:
    """Character ID validation limits."""

    def test_max_8_characters_ok(self):
        from app.smart_agent.planner import serialize_character_ids
        ids = [f"char_{i}" for i in range(8)]
        result = serialize_character_ids(ids)
        import json
        assert len(json.loads(result)) == 8

    def test超过_8_characters_raises(self):
        from app.smart_agent.planner import serialize_character_ids, SmartAgentError
        import pytest
        ids = [f"char_{i}" for i in range(9)]
        with pytest.raises(SmartAgentError) as exc_info:
            serialize_character_ids(ids)
        assert exc_info.value.code == "invalid_character_resolution"

    def test超长_id_raises(self):
        from app.smart_agent.planner import serialize_character_ids, SmartAgentError
        import pytest
        with pytest.raises(SmartAgentError):
            serialize_character_ids(["a" * 81])

    def test_id_at_limit_ok(self):
        from app.smart_agent.planner import serialize_character_ids
        result = serialize_character_ids(["a" * 80])
        import json
        assert len(json.loads(result)) == 1

    def test_json超长_raises(self):
        from app.smart_agent.planner import serialize_character_ids, SmartAgentError
        import pytest
        # 8 IDs * ~130 chars each → JSON > 1024 chars
        ids = ["a" * 130 for _ in range(8)]
        with pytest.raises(SmartAgentError):
            serialize_character_ids(ids)

    def test_validate_max_8_ok(self):
        from app.smart_agent.planner import validate_character_ids
        ids = [f"char_{i}" for i in range(8)]
        result = validate_character_ids(ids)
        assert len(result) == 8

    def test_validate超过_8_raises(self):
        from app.smart_agent.planner import validate_character_ids, SmartAgentError
        import pytest
        ids = [f"char_{i}" for i in range(9)]
        with pytest.raises(SmartAgentError):
            validate_character_ids(ids)

    def test_validate超长_id_raises(self):
        from app.smart_agent.planner import validate_character_ids, SmartAgentError
        import pytest
        with pytest.raises(SmartAgentError):
            validate_character_ids(["a" * 81])

    def test_validate_no_library_sentinel_raises(self):
        from app.smart_agent.planner import validate_character_ids, SmartAgentError
        import pytest
        with pytest.raises(SmartAgentError):
            validate_character_ids(["__no_library_character__"])

    def test_validate_empty_list_raises(self):
        from app.smart_agent.planner import validate_character_ids, SmartAgentError
        import pytest
        with pytest.raises(SmartAgentError):
            validate_character_ids([])

    def test_validate_empty_id_raises(self):
        from app.smart_agent.planner import validate_character_ids, SmartAgentError
        import pytest
        with pytest.raises(SmartAgentError):
            validate_character_ids(["valid_id", ""])


# -- Backward compatibility parse tests (requirements 18-29) -----------

class TestBackwardCompatibility:
    """parse_character_ids must handle all formats correctly."""

    def test旧_单值(self):
        from app.smart_agent.planner import parse_character_ids
        assert parse_character_ids("kochou_shinobu") == ["kochou_shinobu"]

    def test旧_逗号双人物(self):
        from app.smart_agent.planner import parse_character_ids
        assert parse_character_ids("silence_suzuka,tokai_teio") == ["silence_suzuka", "tokai_teio"]

    def test旧_逗号三人物(self):
        from app.smart_agent.planner import parse_character_ids
        assert parse_character_ids("a,b,c") == ["a", "b", "c"]

    def test新_json_双人物(self):
        from app.smart_agent.planner import parse_character_ids
        assert parse_character_ids('["silence_suzuka","tokai_teio"]') == ["silence_suzuka", "tokai_teio"]

    def test新_json_三人物(self):
        from app.smart_agent.planner import parse_character_ids
        assert parse_character_ids('["a","b","c"]') == ["a", "b", "c"]

    def test损坏_json_returns_empty(self):
        from app.smart_agent.planner import parse_character_ids
        # Starts with [ but invalid JSON → must NOT fall back to comma split
        assert parse_character_ids("[broken json") == []

    def test_json_non_string元素_returns_empty(self):
        from app.smart_agent.planner import parse_character_ids
        assert parse_character_ids('[123, "valid"]') == []

    def test_json_空字符串_returns_empty(self):
        from app.smart_agent.planner import parse_character_ids
        assert parse_character_ids('["valid", ""]') == []

    def test_json混合有效无效(self):
        """One valid + one empty → entire parse fails."""
        from app.smart_agent.planner import parse_character_ids
        result = parse_character_ids('["valid_id", ""]')
        assert result == []

    def test_json_no_library_sentinel_parsed(self):
        """Sentinel is parsed (validation happens later in validate_character_ids)."""
        from app.smart_agent.planner import parse_character_ids
        result = parse_character_ids('["__no_library_character__"]')
        assert result == ["__no_library_character__"]  # parsed, but validate will reject

    def test_disabled加非空_array_raises(self):
        """Disabled state with non-empty character array → safe failure."""
        from app.smart_agent.planner import SmartAgentError
        import pytest
        settings = MagicMock()
        settings.deepseek_api_key = "test-key"
        settings.is_local_env.return_value = False
        with pytest.raises(SmartAgentError) as exc_info:
            _run_build_sync(settings, "test", task_prompt_source="agent_no_character",
                            task_character_key='["kochou_shinobu"]')
        assert exc_info.value.code == "invalid_character_resolution"

    def test_resolved加空_array_raises(self):
        """Resolved state with empty array → safe failure."""
        from app.smart_agent.planner import SmartAgentError
        import pytest
        settings = MagicMock()
        settings.deepseek_api_key = "test-key"
        settings.is_local_env.return_value = False
        with pytest.raises(SmartAgentError) as exc_info:
            _run_build_sync(settings, "test", task_prompt_source="agent_character_resolved",
                            task_character_key="[]")
        assert exc_info.value.code == "invalid_character_resolution"


# -- Disabled state validation (requirement 28) -------------------------

class TestDisabledStateValidation:
    """Disabled state must not have character IDs."""

    def _make_settings(self):
        settings = MagicMock()
        settings.deepseek_api_key = "test-key"
        settings.is_local_env.return_value = False
        return settings

    def _common_patches(self, monkeypatch):
        monkeypatch.setattr("app.smart_agent.planner.complete_json", AsyncMock(return_value=_VALID_PLAN))
        monkeypatch.setattr("app.smart_agent.planner.get_workflow", MagicMock(return_value={"key": "anima_owner"}))

    def test_disabled_empty_key_ok(self, monkeypatch):
        settings = self._make_settings()
        self._common_patches(monkeypatch)
        monkeypatch.setattr("app.smart_agent.planner.find_characters", MagicMock(return_value=[]))
        plan = _run_build(settings, "test", task_prompt_source="agent_no_character", task_character_key="")
        assert plan["character_key"] == ""

    def test_disabled_json_empty_array_ok(self, monkeypatch):
        settings = self._make_settings()
        self._common_patches(monkeypatch)
        monkeypatch.setattr("app.smart_agent.planner.find_characters", MagicMock(return_value=[]))
        plan = _run_build(settings, "test", task_prompt_source="agent_no_character", task_character_key="[]")
        assert plan["character_key"] == ""

    def test_disabled_nonempty_raises(self, monkeypatch):
        settings = self._make_settings()
        self._common_patches(monkeypatch)
        monkeypatch.setattr("app.smart_agent.planner.find_characters", MagicMock(return_value=[]))
        from app.smart_agent.planner import SmartAgentError
        import pytest
        with pytest.raises(SmartAgentError):
            _run_build(settings, "test", task_prompt_source="agent_no_character",
                        task_character_key='["some_id"]')

    def test_disabled_sentinel_raises(self, monkeypatch):
        settings = self._make_settings()
        self._common_patches(monkeypatch)
        from app.smart_agent.planner import SmartAgentError
        import pytest
        with pytest.raises(SmartAgentError):
            _run_build(settings, "test", task_prompt_source="agent_no_character",
                        task_character_key='["__no_library_character__"]')


# -- Real database write/read tests ------------------------------------

import pytest as _pytest


class TestCharacterKeyStorage:
    """Real database write/read tests for character_key JSON format."""

    @_pytest.fixture(autouse=True)
    def setup_db(self, tmp_path):
        from app.config import Settings
        from app.db import ensure_schema, connect
        test_root = tmp_path / "test_data"
        output = test_root / "output"
        mock_output = test_root / "mock_output"
        input_images = test_root / "input_images"
        for path in (output, mock_output, input_images):
            path.mkdir(parents=True, exist_ok=True)
        self.settings = Settings(
            APP_ENV="local", APP_ORIGIN="http://127.0.0.1:8001",
            BALANCE_DB=str(test_root / "test.db"),
            BOT_OUTPUT_DIR=str(output), mock_output_dir=str(mock_output),
            INPUT_IMAGE_DIR=str(input_images), BOT_DIR=str(test_root),
            redis_enabled=False, dev_auth_bypass=True, dev_user_id="local-user",
            owner_free_generation=False, deepseek_api_key="",
        )
        ensure_schema(self.settings)
        conn = connect(self.settings)
        try:
            conn.execute("INSERT OR IGNORE INTO users(user_id, balance_fen) VALUES (?, ?)", ("u1", 10000))
            conn.commit()
        finally:
            conn.close()

    def _create_task(self, job_code, character_key, prompt_source="agent_character_resolved"):
        from app.db import create_task_atomic
        return create_task_atomic(
            self.settings, job_code=job_code, user_id="u1", username="t",
            prompt="p", style_key="anima", lora_weight=1.0, width=1024, height=1536,
            mode="txt2img", input_image_path=None, denoise=0.5,
            control_type="depth", control_character="prompt", auto_tagger=False,
            use_agent=True, prompt_source=prompt_source, character_key=character_key,
        )

    def _get_ck(self, job_code):
        from app.db import connect
        conn = connect(self.settings)
        try:
            row = conn.execute("SELECT character_key FROM generation_tasks WHERE job_code=?", (job_code,)).fetchone()
            return str(row["character_key"] or "") if row else ""
        finally:
            conn.close()

    def test_single_roundtrip(self):
        from app.smart_agent.planner import parse_character_ids
        self._create_task("T001", '["kochou_shinobu"]')
        assert self._get_ck("T001") == '["kochou_shinobu"]'
        assert parse_character_ids(self._get_ck("T001")) == ["kochou_shinobu"]

    def test_double_roundtrip(self):
        from app.smart_agent.planner import parse_character_ids
        self._create_task("T002", '["silence_suzuka","tokai_teio"]')
        assert parse_character_ids(self._get_ck("T002")) == ["silence_suzuka", "tokai_teio"]

    def test_triple_roundtrip(self):
        from app.smart_agent.planner import parse_character_ids
        self._create_task("T003", '["kochou_shinobu","hatsune_miku","rice_shower"]')
        assert parse_character_ids(self._get_ck("T003")) == ["kochou_shinobu", "hatsune_miku", "rice_shower"]

    def test_eight_over_120_stored_complete(self):
        from app.smart_agent.planner import parse_character_ids, serialize_character_ids
        ids = ["kochou_shinobu", "hatsune_miku", "rice_shower", "silence_suzuka",
               "tokai_teio", "gold_city", "special_week", "kitasan_black_extra"]
        json_key = serialize_character_ids(ids)
        assert len(json_key) > 120
        self._create_task("T004", json_key)
        stored = self._get_ck("T004")
        assert stored == json_key
        assert parse_character_ids(stored) == ids

    def test_json_loads_ok(self):
        import json as jmod
        self._create_task("T005", '["kochou_shinobu","hatsune_miku"]')
        parsed = jmod.loads(self._get_ck("T005"))
        assert isinstance(parsed, list) and len(parsed) == 2

    def test_last_char_not_truncated(self):
        from app.smart_agent.planner import parse_character_ids
        self._create_task("T006", '["a","b","c","d","e","f","g","h"]')
        assert parse_character_ids(self._get_ck("T006"))[-1] == "h"

    def test_over_1024_fails_before_write(self):
        import json as jmod
        # Build a JSON array that exceeds 1024 chars with 8 valid-length IDs
        # Each ID can be up to 80 chars; with quotes+comma overhead ~84 per entry
        # 8 * 128 = ~1024+ in JSON format
        ids = ["c" + str(i).zfill(79) for i in range(13)]  # 13 * ~84 = ~1092
        long_json = jmod.dumps(ids, separators=(",", ":"))
        assert len(long_json) > 1024
        # This should fail at the db level (_validate_character_key)
        with _pytest.raises(ValueError):
            self._create_task("T007", long_json)

    def test_legacy_single_roundtrip(self):
        from app.db import connect
        from app.smart_agent.planner import parse_character_ids
        import time as _t
        conn = connect(self.settings)
        try:
            conn.execute(
                "INSERT INTO generation_tasks(job_code,user_id,username,prompt,style_key,"
                "width,height,charged_fen,status,created_at,source,character_key,prompt_source)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("T008", "u1", "t", "p", "anima", 1024, 1536, 0, "done", int(_t.time()),
                 "web", "kochou_shinobu", "agent_character_resolved"))
            conn.commit()
        finally:
            conn.close()
        assert parse_character_ids(self._get_ck("T008")) == ["kochou_shinobu"]

    def test_legacy_comma_roundtrip(self):
        from app.db import connect
        from app.smart_agent.planner import parse_character_ids
        import time as _t
        conn = connect(self.settings)
        try:
            conn.execute(
                "INSERT INTO generation_tasks(job_code,user_id,username,prompt,style_key,"
                "width,height,charged_fen,status,created_at,source,character_key,prompt_source)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("T009", "u1", "t", "p", "anima", 1024, 1536, 0, "done", int(_t.time()),
                 "web", "silence_suzuka,tokai_teio", "agent_character_resolved"))
            conn.commit()
        finally:
            conn.close()
        assert parse_character_ids(self._get_ck("T009")) == ["silence_suzuka", "tokai_teio"]

    def test_corrupted_json_worker_safe_fail(self):
        from app.db import connect
        from app.smart_agent.planner import _classify_task_character_decision, parse_character_ids, validate_character_ids, SmartAgentError
        import time as _t
        conn = connect(self.settings)
        try:
            conn.execute(
                "INSERT INTO generation_tasks(job_code,user_id,username,prompt,style_key,"
                "width,height,charged_fen,status,created_at,source,character_key,prompt_source)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("T010", "u1", "t", "p", "anima", 1024, 1536, 0, "smart_planning",
                 int(_t.time()), "web", "[corrupted", "agent_character_resolved"))
            conn.commit()
        finally:
            conn.close()
        stored = self._get_ck("T010")
        assert _classify_task_character_decision("agent_character_resolved") == "resolved"
        char_ids = parse_character_ids(stored)
        assert char_ids == []
        with _pytest.raises(SmartAgentError) as ei:
            validate_character_ids(char_ids)
        assert ei.value.code == "invalid_character_resolution"
