from app.smart_agent.v2_client import has_visual_plan, to_legacy_prompt_fields, validate_agent_v2_response


def test_validate_response_normalizes_tags_and_step():
    result = validate_agent_v2_response({
        "reply": "已经整理好了",
        "next_step": "generate",
        "scene": "park, park, sunset",
        "pose_action": "sitting, looking at viewer",
        "lighting": "warm sunlight",
        "mood": "calm",
        "resolution_hint": "portrait_1024x1536",
    })
    assert result["scene"] == "park, sunset"
    assert result["next_step"] == "generate"
    assert has_visual_plan(result)


def test_legacy_mapping_preserves_pose_and_lighting():
    mapped = to_legacy_prompt_fields({
        "reply": "",
        "scene": "classroom",
        "style": "anime screencap",
        "clothing": "school uniform",
        "expression": "shy",
        "pose_action": "sitting",
        "composition": "front view",
        "lighting": "window light",
        "mood": "quiet",
        "resolution_hint": "portrait_1024x1536",
        "memory_update": "",
        "next_step": "prepare",
    })
    assert mapped["action"] == "sitting"
    assert mapped["mood"] == "window light, quiet"


def test_external_character_validation(monkeypatch):
    import asyncio
    import app.smart_agent.v2_client as client

    async def fake_complete(*args, **kwargs):
        return {"found": True, "original_name": "Alfania", "identity_tag": "Alfania"}

    monkeypatch.setattr(client, "complete_json", fake_complete)
    result = asyncio.run(client.infer_external_character(object(), "Alfania in a castle"))
    assert result == {"original_name": "Alfania", "identity_tag": "alfania"}


def test_external_character_rejects_generic(monkeypatch):
    import asyncio
    import app.smart_agent.v2_client as client

    async def fake_complete(*args, **kwargs):
        return {"found": True, "original_name": "女孩", "identity_tag": "girl"}

    monkeypatch.setattr(client, "complete_json", fake_complete)
    assert asyncio.run(client.infer_external_character(object(), "一个女孩在公园")) is None
