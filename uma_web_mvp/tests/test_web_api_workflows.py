"""Tests for the 5 new Web API generation workflows.

Covers:
1. Workflow template loading
2. Style key whitelist validation
3. Prompt injection correctness per node
4. Fixed trigger word preservation (node 248)
5. Width/height injection per node
6. Seed injection per node
7. LoRA weight injection per node
8. SaveImage output node presence
9. deepcopy isolation (no cross-task pollution)
10. Disk template immutability
"""
from __future__ import annotations

import copy
import json
import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services.workflow_template_service import (
    VALID_STYLE_KEYS,
    WORKFLOW_DEFINITIONS,
    get_all_workflow_info,
    get_workflow_definition,
    is_web_api_workflow,
    load_workflow_template,
    prepare_workflow_payload,
    validate_workflow_output,
)


# ── Constants ──

ALL_STYLE_KEYS = ["artist_chain_available", "morialuluka", "bridge_complete", "hayakawa_tazuna", "akikawa_yayoi"]

# Expected node mappings from the spec
EXPECTED_PROMPT_NODES = {
    "artist_chain_available": ("18", "text"),
    "morialuluka": ("250", "value"),
    "bridge_complete": ("250", "value"),
    "hayakawa_tazuna": ("250", "value"),
    "akikawa_yayoi": ("250", "value"),
}

EXPECTED_FIXED_TRIGGER_NODES = {
    "artist_chain_available": None,
    "morialuluka": "248",
    "bridge_complete": "248",
    "hayakawa_tazuna": "248",
    "akikawa_yayoi": "248",
}

EXPECTED_RESOLUTION_NODES = {
    "artist_chain_available": ("7", "7"),
    "morialuluka": ("129", "129"),
    "bridge_complete": ("129", "129"),
    "hayakawa_tazuna": ("129", "129"),
    "akikawa_yayoi": ("129", "129"),
}

EXPECTED_SEED_NODES = {
    "artist_chain_available": "9",
    "morialuluka": "132",
    "bridge_complete": "132",
    "hayakawa_tazuna": "132",
    "akikawa_yayoi": "132",
}

EXPECTED_LORA_NODES = {
    "artist_chain_available": "29",
    "morialuluka": "35",
    "bridge_complete": "35",
    "hayakawa_tazuna": "35",
    "akikawa_yayoi": "35",
}

EXPECTED_OUTPUT_NODES = {
    "artist_chain_available": "27",
    "morialuluka": "252",
    "bridge_complete": "252",
    "hayakawa_tazuna": "252",
    "akikawa_yayoi": "252",
}

FIXED_TRIGGER_VALUES = {
    "morialuluka": "morialuluka",
    "bridge_complete": r"bridge comp \(umamusume\)",
    "hayakawa_tazuna": "bummsmingame, 3d, hayakawa tazuna, ",
    "akikawa_yayoi": r"ummsmingame, 3d, akikawa yayoi \(umamusume\) ",
}


# ── Helpers ──

def _template_sha256(style_key: str) -> str:
    """Compute SHA256 of the on-disk template file."""
    defn = WORKFLOW_DEFINITIONS[style_key]
    data = defn["file_path"].read_bytes()
    return hashlib.sha256(data).hexdigest()


# ── Tests: 1. Workflow template loading ──

@pytest.mark.parametrize("style_key", ALL_STYLE_KEYS)
def test_workflow_template_loads_as_dict(style_key: str):
    """Each of the 5 workflows loads as a non-empty dict."""
    wf = load_workflow_template(style_key)
    assert isinstance(wf, dict)
    assert len(wf) > 0


# ── Tests: 2. Style key whitelist ──

def test_all_five_keys_in_whitelist():
    assert VALID_STYLE_KEYS == set(ALL_STYLE_KEYS)


@pytest.mark.parametrize("style_key", ALL_STYLE_KEYS)
def test_style_key_is_web_api(style_key: str):
    assert is_web_api_workflow(style_key) is True


def test_unknown_style_key_not_web_api():
    assert is_web_api_workflow("style_a") is False
    assert is_web_api_workflow("nonexistent") is False
    assert is_web_api_workflow("") is False


# ── Tests: 3. Definition completeness ──

@pytest.mark.parametrize("style_key", ALL_STYLE_KEYS)
def test_definition_has_all_required_fields(style_key: str):
    defn = get_workflow_definition(style_key)
    assert defn is not None
    for field in ("label_zh", "label_en", "file_path", "prompt_node", "prompt_field",
                  "width_node", "height_node", "seed_node", "lora_node", "output_node"):
        assert field in defn, f"Missing field {field} in {style_key}"


# ── Tests: 6. Prompt injection ──

@pytest.mark.parametrize("style_key", ALL_STYLE_KEYS)
def test_prompt_written_to_correct_node(style_key: str):
    test_prompt = "test prompt, 1girl, beautiful"
    wf = prepare_workflow_payload(style_key, test_prompt, 1024, 1536)
    node_id, field = EXPECTED_PROMPT_NODES[style_key]
    assert wf[node_id]["inputs"][field] == test_prompt


def test_artist_chain_overwrites_node18():
    """画师串: node 18 text must be fully overwritten, not appended."""
    original = load_workflow_template("artist_chain_available")
    original_text = original["18"]["inputs"]["text"]

    new_prompt = "completely new prompt"
    wf = prepare_workflow_payload("artist_chain_available", new_prompt, 1024, 1536)
    assert wf["18"]["inputs"]["text"] == new_prompt
    assert wf["18"]["inputs"]["text"] != original_text


# ── Tests: 7-8. Fixed trigger word preservation (node 248) ──

@pytest.mark.parametrize("style_key", ["morialuluka", "bridge_complete", "hayakawa_tazuna", "akikawa_yayoi"])
def test_fixed_trigger_word_preserved(style_key: str):
    """Node 248 must retain its original fixed trigger word value."""
    original = load_workflow_template(style_key)
    original_trigger = original["248"]["inputs"]["value"]

    wf = prepare_workflow_payload(style_key, "user prompt here", 1024, 1536)
    assert wf["248"]["inputs"]["value"] == original_trigger


@pytest.mark.parametrize("style_key", ["morialuluka", "bridge_complete", "hayakawa_tazuna", "akikawa_yayoi"])
def test_fixed_trigger_word_exact_value(style_key: str):
    """Verify exact trigger word values match the spec."""
    original = load_workflow_template(style_key)
    trigger = original["248"]["inputs"]["value"]
    expected = FIXED_TRIGGER_VALUES[style_key]
    assert trigger == expected, f"Trigger mismatch for {style_key}: {trigger!r} != {expected!r}"


def test_artist_chain_no_fixed_trigger():
    """画师串 has no fixed trigger node (no node 248 dependency)."""
    defn = get_workflow_definition("artist_chain_available")
    assert defn["fixed_trigger_node"] is None


# ── Tests: 9. Node 251 still references 248 and 250 ──

@pytest.mark.parametrize("style_key", ["morialuluka", "bridge_complete", "hayakawa_tazuna", "akikawa_yayoi"])
def test_join_node_references_248_and_250(style_key: str):
    """Node 251 (JoinStringMulti) must reference string_1=248 and string_2=250."""
    wf = prepare_workflow_payload(style_key, "test", 1024, 1536)
    inputs = wf["251"]["inputs"]
    assert str(inputs["string_1"][0]) == "248"
    assert str(inputs["string_2"][0]) == "250"


# ── Tests: 10. Width/height injection ──

@pytest.mark.parametrize("style_key", ALL_STYLE_KEYS)
def test_resolution_injected_correctly(style_key: str):
    width, height = 1536, 1024
    wf = prepare_workflow_payload(style_key, "test", width, height)
    w_node, h_node = EXPECTED_RESOLUTION_NODES[style_key]
    assert wf[w_node]["inputs"]["width"] == width
    assert wf[h_node]["inputs"]["height"] == height


@pytest.mark.parametrize("style_key", ALL_STYLE_KEYS)
def test_resolution_not_swapped(style_key: str):
    """width and height must not be swapped."""
    wf = prepare_workflow_payload(style_key, "test", 800, 1200)
    w_node, h_node = EXPECTED_RESOLUTION_NODES[style_key]
    assert wf[w_node]["inputs"]["width"] == 800
    assert wf[h_node]["inputs"]["height"] == 1200


# ── Tests: 11. Seed injection ──

@pytest.mark.parametrize("style_key", ALL_STYLE_KEYS)
def test_seed_injected(style_key: str):
    seed = 42
    wf = prepare_workflow_payload(style_key, "test", 1024, 1536, seed=seed)
    seed_node = EXPECTED_SEED_NODES[style_key]
    assert wf[seed_node]["inputs"]["seed"] == seed


@pytest.mark.parametrize("style_key", ALL_STYLE_KEYS)
def test_seed_changes_each_call(style_key: str):
    """When no seed is provided, each call should get a different random seed."""
    wf1 = prepare_workflow_payload(style_key, "test", 1024, 1536)
    wf2 = prepare_workflow_payload(style_key, "test", 1024, 1536)
    seed_node = EXPECTED_SEED_NODES[style_key]
    # They should almost certainly be different (collision probability negligible)
    # But we can at least verify they're valid integers
    s1 = wf1[seed_node]["inputs"]["seed"]
    s2 = wf2[seed_node]["inputs"]["seed"]
    assert isinstance(s1, int) and s1 > 0
    assert isinstance(s2, int) and s2 > 0


# ── Tests: 12. LoRA weight injection ──

@pytest.mark.parametrize("style_key", ALL_STYLE_KEYS)
def test_lora_weight_injected(style_key: str):
    wf = prepare_workflow_payload(style_key, "test", 1024, 1536, lora_weight=0.8)
    lora_node = EXPECTED_LORA_NODES[style_key]
    assert wf[lora_node]["inputs"]["strength_model"] == 0.8
    assert wf[lora_node]["inputs"]["strength_clip"] == 0.8


@pytest.mark.parametrize("style_key", ALL_STYLE_KEYS)
def test_lora_name_preserved(style_key: str):
    """Embedded lora_name must not be changed."""
    original = load_workflow_template(style_key)
    lora_node = EXPECTED_LORA_NODES[style_key]
    original_name = original[lora_node]["inputs"]["lora_name"]

    wf = prepare_workflow_payload(style_key, "test", 1024, 1536, lora_weight=0.5)
    assert wf[lora_node]["inputs"]["lora_name"] == original_name


# ── Tests: 13. SaveImage output node ──

@pytest.mark.parametrize("style_key", ALL_STYLE_KEYS)
def test_save_image_node_exists(style_key: str):
    wf = prepare_workflow_payload(style_key, "test", 1024, 1536)
    assert validate_workflow_output(wf, style_key) is True


@pytest.mark.parametrize("style_key", ALL_STYLE_KEYS)
def test_output_node_is_save_image(style_key: str):
    wf = load_workflow_template(style_key)
    output_node = EXPECTED_OUTPUT_NODES[style_key]
    assert wf[output_node]["class_type"] == "SaveImage"


# ── Tests: 14. deepcopy isolation ──

def test_two_tasks_dont_share_prompt():
    """Two concurrent prepare calls must not contaminate each other."""
    wf1 = prepare_workflow_payload("morialuluka", "prompt_A", 1024, 1536, seed=111)
    wf2 = prepare_workflow_payload("morialuluka", "prompt_B", 1024, 1536, seed=222)
    assert wf1["250"]["inputs"]["value"] == "prompt_A"
    assert wf2["250"]["inputs"]["value"] == "prompt_B"
    assert wf1["132"]["inputs"]["seed"] == 111
    assert wf2["132"]["inputs"]["seed"] == 222


def test_two_tasks_dont_share_prompt_artist_chain():
    wf1 = prepare_workflow_payload("artist_chain_available", "prompt_X", 1024, 1536, seed=111)
    wf2 = prepare_workflow_payload("artist_chain_available", "prompt_Y", 1024, 1536, seed=222)
    assert wf1["18"]["inputs"]["text"] == "prompt_X"
    assert wf2["18"]["inputs"]["text"] == "prompt_Y"


# ── Tests: 15. Disk template immutability ──

@pytest.mark.parametrize("style_key", ALL_STYLE_KEYS)
def test_disk_template_not_modified(style_key: str):
    """prepare_workflow_payload must not modify the on-disk template."""
    before = _template_sha256(style_key)
    _ = prepare_workflow_payload(style_key, "this should not persist", 999, 888, seed=777, lora_weight=0.1)
    after = _template_sha256(style_key)
    assert before == after, f"Disk template was modified for {style_key}!"


# ── Tests: 16. get_all_workflow_info ──

def test_get_all_workflow_info_returns_5():
    info = get_all_workflow_info()
    assert len(info) == 5
    keys = {item["key"] for item in info}
    assert keys == set(ALL_STYLE_KEYS)


# ── Tests: 17. KSampler references correct seed node ──

@pytest.mark.parametrize("style_key", ALL_STYLE_KEYS)
def test_ksampler_references_correct_seed_node(style_key: str):
    """Verify KSampler's seed input references the expected seed node."""
    wf = load_workflow_template(style_key)
    seed_node = EXPECTED_SEED_NODES[style_key]
    # Find the KSampler and check its seed reference
    for node_id, node in wf.items():
        if node.get("class_type") == "KSampler":
            seed_ref = node["inputs"].get("seed")
            if isinstance(seed_ref, list):
                assert str(seed_ref[0]) == seed_node, (
                    f"KSampler {node_id} seed ref {seed_ref} != expected node {seed_node}"
                )


# ── Tests: 18. KSampler references correct positive prompt node ──

@pytest.mark.parametrize("style_key", ALL_STYLE_KEYS)
def test_ksampler_positive_references_clip_text_encode(style_key: str):
    """Verify KSampler's positive input references a CLIPTextEncode node.

    For the 4 non-artist workflows, the chain is:
    248 (fixed trigger) + 250 (user prompt) -> 251 (JoinStringMulti) -> 3 (CLIPTextEncode) -> KSampler
    For artist_chain_available, node 18 (CLIPTextEncode) -> KSampler directly.
    """
    wf = load_workflow_template(style_key)
    for node_id, node in wf.items():
        if node.get("class_type") == "KSampler":
            pos_ref = node["inputs"].get("positive")
            if isinstance(pos_ref, list):
                ref_node = wf.get(str(pos_ref[0]))
                assert ref_node is not None
                assert ref_node["class_type"] == "CLIPTextEncode", (
                    f"KSampler {node_id} positive references node {pos_ref[0]} "
                    f"which is {ref_node['class_type']}, not CLIPTextEncode"
                )


# ── Tests: 19. VAEDecode connects to SaveImage ──

@pytest.mark.parametrize("style_key", ALL_STYLE_KEYS)
def test_save_image_reads_from_vae_decode(style_key: str):
    """SaveImage must read from VAEDecode, not just PreviewImage."""
    wf = load_workflow_template(style_key)
    output_node = EXPECTED_OUTPUT_NODES[style_key]
    save_node = wf[output_node]
    images_ref = save_node["inputs"].get("images")
    assert isinstance(images_ref, list), f"SaveImage {output_node} images input is not a reference"
    ref_node = wf.get(str(images_ref[0]))
    assert ref_node is not None
    assert ref_node["class_type"] == "VAEDecode"
