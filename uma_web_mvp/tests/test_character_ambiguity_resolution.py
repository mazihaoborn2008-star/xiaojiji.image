from app.smart_agent.disambiguation_engine import analyze_character_mentions


def _ids(items: list[dict]) -> list[str]:
    return sorted(str(item.get("characterId") or "") for item in items)


def test_short_cjk_run_still_detects_mami_ambiguity():
    result = analyze_character_mentions("麻美穿风衣，自己走在街上")

    assert result["status"] == "ambiguous"
    assert [item["rawText"] for item in result["mentions"]] == ["麻美"]
    assert _ids(result["mentions"][0]["candidates"]) == [
        "nanami_mami",
        "tomoe_mami",
    ]


def test_explicit_full_mami_name_remains_resolved():
    result = analyze_character_mentions("七海麻美穿风衣，自己走在街上")

    assert result["status"] == "resolved"
    assert _ids(result["resolvedCharacters"]) == ["nanami_mami"]
    assert result["mentions"] == []


def test_scene_without_known_character_stays_not_found():
    result = analyze_character_mentions("女孩穿风衣，自己走在街上")

    assert result["status"] == "not_found"
    assert result["resolvedCharacters"] == []
    assert result["mentions"] == []
