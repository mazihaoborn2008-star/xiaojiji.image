from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.main import (
    _apply_prompt_core_fidelity,
    _build_selected_characters_json,
    _build_local_positive_prompt,
    _tags_from_snippets,
)
from app.smart_agent.character_search import load_characters
from app.smart_agent.disambiguation_engine import analyze_character_mentions
from app.smart_agent.prompt_library import search_prompt_snippets


def _character(key: str) -> dict[str, Any]:
    for item in load_characters():
        if str(item.get("key") or "") == key:
            return dict(item)
    raise AssertionError(f"missing test character: {key}")


def _selected_json(*keys: str) -> str:
    return _build_selected_characters_json([_character(key) for key in keys])


def _assert_contains(prompt: str, *needles: str) -> None:
    lowered = prompt.lower()
    compact = lowered.replace("_", " ")
    missing = [needle for needle in needles if needle.lower().replace("_", " ") not in compact]
    assert not missing, (prompt, missing)


def _assert_not_contains(prompt: str, *needles: str) -> None:
    lowered = prompt.lower()
    compact = lowered.replace("_", " ")
    found = [needle for needle in needles if needle.lower().replace("_", " ") in compact]
    assert not found, (prompt, found)


def _prompt(user_text: str, result: dict[str, str], *character_keys: str) -> str:
    characters = [_character(key) for key in character_keys]
    return _build_local_positive_prompt(
        result=result,
        request_text=user_text,
        characters=characters,
        snippets=search_prompt_snippets(user_text, limit=10),
        selected_characters_json=_selected_json(*character_keys) if character_keys else "",
    )


def main() -> None:
    p1 = _prompt(
        "生成大胸铃鹿，上半身穿偏紧身的白色短袖，要突出胸大。",
        {
            "scene": "simple_background",
            "style": "general_anime_illustration, impossible_dress",
            "clothing": "white_tight_short_sleeve_shirt",
            "expression": "smile",
            "action": "standing",
            "composition": "upper_body, cowboy_shot",
            "mood": "cute",
        },
        "silence_suzuka",
    )
    _assert_contains(p1, "silence suzuka", "horse ears", "horse tail", "large breasts", "emphasized bust", "upper body")
    _assert_contains(p1, "white short sleeve shirt", "short sleeves")
    _assert_not_contains(p1, "impossible_dress", "dress", "skirt")

    p2, _ = _apply_prompt_core_fidelity(
        "1girl, bedroom, smile, cute, looking at viewer",
        "麻美穿在卧室，一脸嫌弃地看着镜头",
    )
    _assert_contains(p2, "bedroom", "annoyed expression", "disdainful look", "looking at viewer")
    _assert_not_contains(p2, "smile", "happy expression")

    p3 = _prompt(
        "千鹤穿校服坐在教室里，害羞地看着镜头",
        {"scene": "", "clothing": "", "expression": "", "action": "", "composition": ""},
        "chizuru_mizuhara",
    )
    _assert_contains(p3, "school uniform", "classroom", "sitting", "shy expression", "blush", "looking at viewer")

    p4 = _prompt(
        "铃鹿穿白色短袖站在简单背景前",
        {"scene": "simple background", "clothing": "white short sleeve shirt", "action": "standing"},
        "silence_suzuka",
    )
    _assert_contains(p4, "white short sleeve shirt", "standing")
    _assert_not_contains(p4, "large breasts", "large bust", "full bust", "emphasized bust")

    p5 = _prompt(
        "铃鹿上半身，白色紧身短袖，胸部丰满，正面视角",
        {"clothing": "white short sleeve shirt", "composition": "upper body"},
        "silence_suzuka",
    )
    _assert_contains(p5, "upper body", "white short sleeve shirt", "tight clothing", "full bust", "front view")

    p6, _ = _apply_prompt_core_fidelity(
        "1girl, smile, cute, looking at viewer",
        "不要笑，一脸冷淡地看着镜头",
    )
    _assert_contains(p6, "cold expression", "expressionless", "neutral expression", "looking at viewer")
    _assert_not_contains(p6, "smile")

    p7 = analyze_character_mentions("穿着类似巴麻美风格的服装，但不要出现巴麻美本人")
    assert "tomoe_mami" not in [c.get("characterId") for c in p7.get("resolvedCharacters", [])], p7

    p8 = analyze_character_mentions("一个叫麻美的原创女生，穿白色短袖，卧室里看镜头")
    assert p8["status"] == "not_found", p8

    snippet_tags = _tags_from_snippets(
        search_prompt_snippets("生成大胸铃鹿，上半身穿偏紧身的白色短袖，要突出胸大。", limit=10),
        "生成大胸铃鹿，上半身穿偏紧身的白色短袖，要突出胸大。",
    )
    assert "impossible_dress" not in [tag.lower() for tag in snippet_tags], snippet_tags

    print("smart agent prompt core fidelity tests passed")
    for idx, prompt in enumerate([p1, p2, p3, p4, p5, p6], 1):
        print(f"case {idx}: {prompt}")


if __name__ == "__main__":
    main()
