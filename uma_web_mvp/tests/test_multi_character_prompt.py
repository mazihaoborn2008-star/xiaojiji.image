"""Tests for multi-character prompt assembly and validation."""
from __future__ import annotations

import pytest

from app.smart_agent.character_preferences import (
    assemble_multi_character_prompt,
    enforce_character_preferences,
    locked_character_tags,
    split_prompt_tags,
    validate_character_prompt,
    validate_multi_character_prompt,
    _tag_key,
    _compute_count_tag,
)
from app.smart_agent.disambiguation_engine import characters_from_public_ids


@pytest.fixture
def vivlos():
    chars = characters_from_public_ids(["vivlos"])
    assert len(chars) == 1
    return chars[0]


@pytest.fixture
def verxina():
    chars = characters_from_public_ids(["verxina"])
    assert len(chars) == 1
    return chars[0]


@pytest.fixture
def kitasan_black():
    chars = characters_from_public_ids(["kitasan_black"])
    assert len(chars) == 1
    return chars[0]


@pytest.fixture
def satono_diamond():
    chars = characters_from_public_ids(["satono_diamond"])
    assert len(chars) == 1
    return chars[0]


class TestSingleCharacterRegression:
    """Ensure single-character behavior is unchanged."""

    def test_vivlos_single_pass(self, vivlos):
        result = enforce_character_preferences(
            characters=[vivlos],
            workflow_key="anima_owner",
            positive_prompt="1girl, standing, classroom, smile",
            loras=[],
            request_text="a girl standing in classroom",
        )
        prompt = result["positive_prompt"]
        assert len(prompt) > 0
        locked = locked_character_tags(vivlos)
        for tag in locked:
            assert _tag_key(tag) in {_tag_key(t) for t in split_prompt_tags(prompt)}

    def test_verxina_single_pass(self, verxina):
        result = enforce_character_preferences(
            characters=[verxina],
            workflow_key="anima_owner",
            positive_prompt="1girl, standing, classroom, smile",
            loras=[],
            request_text="a girl standing in classroom",
        )
        prompt = result["positive_prompt"]
        assert len(prompt) > 0
        locked = locked_character_tags(verxina)
        for tag in locked:
            assert _tag_key(tag) in {_tag_key(t) for t in split_prompt_tags(prompt)}


class TestMultiCharacterAssembly:
    """Test assemble_multi_character_prompt."""

    def test_two_characters_both_tags_present(self, vivlos, verxina):
        prompt, removed = assemble_multi_character_prompt(
            characters=[vivlos, verxina],
            scene_prompt="1girl, standing, classroom, smile",
            user_text="two girls standing in classroom",
        )
        tags_keys = {_tag_key(t) for t in split_prompt_tags(prompt)}
        vivlos_keys = {_tag_key(t) for t in locked_character_tags(vivlos)}
        verxina_keys = {_tag_key(t) for t in locked_character_tags(verxina)}
        assert vivlos_keys.issubset(tags_keys)
        assert verxina_keys.issubset(tags_keys)

    def test_two_characters_get_count_tag(self, vivlos, verxina):
        prompt, _ = assemble_multi_character_prompt(
            characters=[vivlos, verxina],
            scene_prompt="standing, classroom, smile",
            user_text="two girls",
        )
        assert "2girls" in prompt.lower()

    def test_four_characters_get_4girls(self, vivlos, verxina, kitasan_black, satono_diamond):
        prompt, _ = assemble_multi_character_prompt(
            characters=[vivlos, verxina, kitasan_black, satono_diamond],
            scene_prompt="standing, classroom",
            user_text="four girls",
        )
        assert "4girls" in prompt.lower()

    def test_single_character_uses_single_path(self, vivlos):
        prompt_multi, _ = assemble_multi_character_prompt(
            characters=[vivlos],
            scene_prompt="1girl, standing, classroom",
            user_text="a girl",
        )
        from app.smart_agent.character_preferences import assemble_character_prompt_with_count
        prompt_single, _ = assemble_character_prompt_with_count(
            character=vivlos,
            scene_prompt="1girl, standing, classroom",
            user_text="a girl",
        )
        assert prompt_multi == prompt_single


class TestMultiCharacterValidation:
    """Test validate_multi_character_prompt."""

    def test_two_characters_pass(self, vivlos, verxina):
        prompt, _ = assemble_multi_character_prompt(
            characters=[vivlos, verxina],
            scene_prompt="standing, classroom, smile",
            user_text="two girls",
        )
        validate_multi_character_prompt(
            prompt=prompt,
            characters=[vivlos, verxina],
            workflow_key="anima_owner",
            loras=[],
            user_text="two girls",
        )

    def test_two_characters_missing_tag_fails(self, vivlos, verxina):
        # Prompt with only vivlos tags should fail for multi-character
        prompt, _ = assemble_multi_character_prompt(
            characters=[vivlos],
            scene_prompt="standing, classroom",
            user_text="a girl",
        )
        with pytest.raises(Exception):
            validate_multi_character_prompt(
                prompt=prompt,
                characters=[vivlos, verxina],
                workflow_key="anima_owner",
                loras=[],
                user_text="two girls",
            )

    def test_single_character_uses_single_validation(self, vivlos):
        prompt, _ = assemble_multi_character_prompt(
            characters=[vivlos],
            scene_prompt="standing, classroom",
            user_text="a girl",
        )
        # Should not raise
        validate_multi_character_prompt(
            prompt=prompt,
            characters=[vivlos],
            workflow_key="anima_owner",
            loras=[],
            user_text="a girl",
        )

    def test_unselected_character_tag_rejected(self, vivlos, verxina, kitasan_black):
        # Prompt with vivlos+verxina tags should fail when kitasan_black is also expected
        prompt, _ = assemble_multi_character_prompt(
            characters=[vivlos, verxina],
            scene_prompt="standing, classroom",
            user_text="two girls",
        )
        with pytest.raises(Exception):
            validate_multi_character_prompt(
                prompt=prompt,
                characters=[vivlos, verxina, kitasan_black],
                workflow_key="anima_owner",
                loras=[],
                user_text="three girls",
            )


class TestCountTags:
    """Test count tag computation."""

    def test_two_females(self, vivlos, verxina):
        tag = _compute_count_tag([vivlos, verxina])
        assert "2girls" in tag.lower()

    def test_one_female(self, vivlos):
        tag = _compute_count_tag([vivlos])
        assert "1girl" in tag.lower()

    def test_four_females(self, vivlos, verxina, kitasan_black, satono_diamond):
        tag = _compute_count_tag([vivlos, verxina, kitasan_black, satono_diamond])
        assert "4girls" in tag.lower()


class TestTwoCharacterEndToEnd:
    """End-to-end test with enforce_character_preferences."""

    def test_two_characters_enforce_pass(self, vivlos, verxina):
        result = enforce_character_preferences(
            characters=[vivlos, verxina],
            workflow_key="anima_owner",
            positive_prompt="standing, classroom, smile",
            loras=[],
            request_text="two girls standing in classroom",
        )
        prompt = result["positive_prompt"]
        tags_keys = {_tag_key(t) for t in split_prompt_tags(prompt)}
        vivlos_keys = {_tag_key(t) for t in locked_character_tags(vivlos)}
        verxina_keys = {_tag_key(t) for t in locked_character_tags(verxina)}
        assert vivlos_keys.issubset(tags_keys)
        assert verxina_keys.issubset(tags_keys)
        assert len(result["locked_character_tags"]) == 10

    def test_two_characters_enforce_validation_pass(self, vivlos, verxina):
        result = enforce_character_preferences(
            characters=[vivlos, verxina],
            workflow_key="anima_owner",
            positive_prompt="standing, classroom, smile",
            loras=[],
            request_text="two girls standing in classroom",
        )
        validate_multi_character_prompt(
            prompt=result["positive_prompt"],
            characters=[vivlos, verxina],
            workflow_key=result["workflow_key"],
            loras=result["loras"],
            user_text="two girls standing in classroom",
        )

    def test_two_characters_reversed_order(self, vivlos, verxina):
        result = enforce_character_preferences(
            characters=[verxina, vivlos],
            workflow_key="anima_owner",
            positive_prompt="standing, classroom, smile",
            loras=[],
            request_text="two girls standing in classroom",
        )
        prompt = result["positive_prompt"]
        tags_keys = {_tag_key(t) for t in split_prompt_tags(prompt)}
        vivlos_keys = {_tag_key(t) for t in locked_character_tags(vivlos)}
        verxina_keys = {_tag_key(t) for t in locked_character_tags(verxina)}
        assert vivlos_keys.issubset(tags_keys)
        assert verxina_keys.issubset(tags_keys)
