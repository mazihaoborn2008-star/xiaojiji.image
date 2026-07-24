from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_worker_parses_web_json_character_key_field():
    from app.agent import parse_generation_task_character_resolution

    ids, no_library = parse_generation_task_character_resolution(
        '["vivlos"]',
        prompt_source="agent_character_resolved",
    )

    assert ids == ["vivlos"]
    assert no_library is False


def test_worker_parses_legacy_comma_character_key_field():
    from app.agent import parse_generation_task_character_resolution

    ids, no_library = parse_generation_task_character_resolution(
        "nanami_mami,tomoe_mami",
        prompt_source="agent_character_resolved",
    )

    assert ids == ["nanami_mami", "tomoe_mami"]
    assert no_library is False


def test_worker_parses_no_library_prompt_source():
    from app.agent import parse_generation_task_character_resolution

    ids, no_library = parse_generation_task_character_resolution(
        "[]",
        prompt_source="agent_character_no_library",
    )

    assert ids == []
    assert no_library is True


def test_worker_import_from_non_repo_cwd_uses_explicit_code_root(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    script = (
        "import os,sys,json;"
        f"os.chdir({str(tmp_path)!r});"
        f"sys.path.insert(0,{str(repo_root)!r});"
        "import app,app.agent;"
        "ids,no_lib=app.agent.parse_generation_task_character_resolution('[\"vivlos\"]',"
        "prompt_source='agent_character_resolved');"
        "print(json.dumps({'app':app.__file__,'agent':app.agent.__file__,'ids':ids,'no_lib':no_lib}))"
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        text=True,
        capture_output=True,
        check=True,
    )
    data = json.loads(completed.stdout)

    assert str(repo_root / "app") in data["app"]
    assert str(repo_root / "app" / "agent.py") == data["agent"]
    assert data["ids"] == ["vivlos"]
    assert data["no_lib"] is False
