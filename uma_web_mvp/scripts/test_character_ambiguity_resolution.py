from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.smart_agent.disambiguation_engine import (
    NO_LIBRARY_CHARACTER_ID,
    analyze_character_mentions,
    characters_from_public_ids,
    validate_character_resolution,
)
from app.smart_agent.character_preferences import _apply_count_tags
from app.agent import _apply_character_registry_to_refined_prompt


def _ids(result: dict) -> list[str]:
    return [item["characterId"] for item in result.get("resolvedCharacters", [])]


def _ambiguous_mentions(result: dict) -> list[str]:
    return [item["rawText"] for item in result.get("mentions", [])]


def main() -> None:
    cases = [
        ("七海麻美穿着校服", "resolved", ["nanami_mami"], []),
        ("巴麻美在教室", "resolved", ["tomoe_mami"], []),
        ("麻美穿着校服", "ambiguous", [], ["麻美"]),
        ("七海麻美和巴麻美", "resolved", ["nanami_mami", "tomoe_mami"], []),
        ("千鹤和麻美在咖啡厅", "mixed", ["chizuru_mizuhara"], ["麻美"]),
        ("七海麻美，麻美穿着裙子", "resolved", ["nanami_mami"], []),
        ("一个叫麻美的原创女生", "not_found", [], []),
        ("不要巴麻美", "not_found", [], []),
        ("参考巴麻美的服装风格", "not_found", [], []),
        ("两个麻美站在一起", "ambiguous", [], ["麻美"]),
        ("七海麻美站在人群中", "resolved", ["nanami_mami"], []),
    ]
    for prompt, expected_status, expected_ids, expected_mentions in cases:
        result = analyze_character_mentions(prompt)
        assert result["status"] == expected_status, (prompt, result["status"], expected_status)
        assert _ids(result) == expected_ids, (prompt, _ids(result), expected_ids)
        assert _ambiguous_mentions(result) == expected_mentions, (prompt, _ambiguous_mentions(result), expected_mentions)

    resolved = validate_character_resolution(
        "麻美穿着校服",
        {"selections": [{"rawText": "麻美", "characterId": "nanami_mami"}]},
    )
    assert _ids(resolved) == ["nanami_mami"]

    skipped = validate_character_resolution(
        "麻美穿着校服",
        {"selections": [{"rawText": "麻美", "characterId": NO_LIBRARY_CHARACTER_ID}]},
    )
    assert skipped["skippedMentions"]
    assert _ids(skipped) == []

    try:
        validate_character_resolution(
            "麻美穿着校服",
            {"selections": [{"rawText": "麻美", "characterId": "chizuru_mizuhara"}]},
        )
    except ValueError as exc:
        assert str(exc) == "invalid_character_resolution"
    else:
        raise AssertionError("forged characterId was accepted")

    two = analyze_character_mentions("七海麻美和巴麻美")
    assert len(two["resolvedCharacters"]) == 2
    mami_chars = characters_from_public_ids(["nanami_mami", "tomoe_mami"])
    nanami = characters_from_public_ids(["nanami_mami"])
    assert _apply_count_tags("classroom", mami_chars).startswith("2girls")
    assert not _apply_count_tags("crowd, classroom", nanami).startswith("1girl")
    selected_prompt = _apply_character_registry_to_refined_prompt(
        "麻美穿着校服",
        "school uniform, classroom",
        resolved_character_ids=["nanami_mami"],
    )
    assert "nanami mami" in selected_prompt.lower()
    assert "tomoe mami" not in selected_prompt.lower()
    multi_prompt = _apply_character_registry_to_refined_prompt(
        "七海麻美和巴麻美",
        "school uniform, classroom",
        resolved_character_ids=["nanami_mami", "tomoe_mami"],
    )
    assert "kanojo okarishimasu" in multi_prompt.lower()
    assert "mahou shoujo madoka magica" in multi_prompt.lower()
    print("character ambiguity resolution tests passed")


if __name__ == "__main__":
    main()
