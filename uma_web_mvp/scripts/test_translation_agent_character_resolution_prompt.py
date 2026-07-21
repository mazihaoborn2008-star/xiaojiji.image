from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.agent import _apply_character_registry_to_refined_prompt
from app.smart_agent.disambiguation_engine import (
    analyze_character_mentions,
    characters_from_public_ids,
    validate_character_resolution,
)


SCENE_PROMPT = "school uniform, skirt, white shirt, tie, standing, classroom, desk, window, daylight"


def _ids(items: list[dict]) -> list[str]:
    return [str(item.get("characterId") or item.get("key") or "").strip() for item in items]


def _run_single_choice(character_id: str) -> str:
    user_prompt = "麻美穿校服在教室"
    parsed = analyze_character_mentions(user_prompt)
    mention = (parsed.get("mentions") or [])[0]
    selection = {
        "mentionId": mention.get("mentionId"),
        "rawText": mention.get("rawText"),
        "characterId": character_id,
    }
    validated = validate_character_resolution(user_prompt, {"status": "resolved", "selections": [selection]})
    resolved_ids = _ids(validated.get("resolvedCharacters") or [])
    characters = characters_from_public_ids(resolved_ids)
    final_prompt = _apply_character_registry_to_refined_prompt(
        user_prompt,
        SCENE_PROMPT,
        resolved_character_ids=resolved_ids,
    )

    print("---- single choice ----")
    print(f"original_prompt={user_prompt}")
    print(f"initial_status={parsed.get('status')}")
    print("candidates=" + ", ".join(
        str(candidate.get("characterId") or "")
        for candidate in mention.get("candidates") or []
    ))
    print(f"selection={selection}")
    print(f"resolvedCharacters={resolved_ids}")
    print("count_tag_input=" + ", ".join(
        str(character.get("key") or "")
        for character in characters
    ))
    print("tag_injection_input=" + ", ".join(
        str(character.get("key") or "")
        for character in characters
    ))
    print(f"final_prompt={final_prompt}")
    return final_prompt


def test_nanami_mami_choice_is_single_character() -> None:
    final_prompt = _run_single_choice("nanami_mami")
    assert final_prompt.startswith("1girl, ")
    assert "nanami mami" in final_prompt
    assert "kanojo okarishimasu" in final_prompt
    assert "tomoe mami" not in final_prompt
    assert "mahou shoujo madoka magica" not in final_prompt
    assert "2girls" not in final_prompt


def test_tomoe_mami_choice_is_single_character() -> None:
    final_prompt = _run_single_choice("tomoe_mami")
    assert final_prompt.startswith("1girl, ")
    assert "tomoe mami" in final_prompt
    assert "mahou shoujo madoka magica" in final_prompt
    assert "nanami mami" not in final_prompt
    assert "kanojo okarishimasu" not in final_prompt
    assert "2girls" not in final_prompt


def test_explicit_two_characters_keeps_two_girls() -> None:
    user_prompt = "七海麻美和巴麻美穿校服在教室"
    parsed = analyze_character_mentions(user_prompt)
    resolved_ids = _ids(parsed.get("resolvedCharacters") or [])
    final_prompt = _apply_character_registry_to_refined_prompt(
        user_prompt,
        SCENE_PROMPT,
        resolved_character_ids=resolved_ids,
    )
    print("---- explicit two characters ----")
    print(f"original_prompt={user_prompt}")
    print(f"initial_status={parsed.get('status')}")
    print(f"resolvedCharacters={resolved_ids}")
    print(f"final_prompt={final_prompt}")
    assert final_prompt.startswith("2girls, ")
    assert "nanami mami" in final_prompt
    assert "tomoe mami" in final_prompt


def test_two_mami_still_requires_confirmation() -> None:
    parsed = analyze_character_mentions("两个麻美穿校服在教室")
    print("---- two ambiguous mami ----")
    print(f"initial_status={parsed.get('status')}")
    print("candidates=" + ", ".join(
        str(candidate.get("characterId") or "")
        for mention in parsed.get("mentions") or []
        for candidate in mention.get("candidates") or []
    ))
    assert parsed.get("status") == "ambiguous"
    assert parsed.get("mentions")


def test_explicit_resolution_never_falls_back_to_candidates() -> None:
    try:
        _apply_character_registry_to_refined_prompt(
            "麻美穿校服在教室",
            SCENE_PROMPT,
            resolved_character_ids=["not_a_real_character"],
        )
    except RuntimeError as exc:
        assert "人物选择结果无效" in str(exc)
    else:
        raise AssertionError("invalid explicit character id should not fall back to ambiguous candidates")


if __name__ == "__main__":
    test_nanami_mami_choice_is_single_character()
    test_tomoe_mami_choice_is_single_character()
    test_explicit_two_characters_keeps_two_girls()
    test_two_mami_still_requires_confirmation()
    test_explicit_resolution_never_falls_back_to_candidates()
    print("ok")
