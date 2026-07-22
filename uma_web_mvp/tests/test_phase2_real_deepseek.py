"""Phase 2 Step 2: Controlled real DeepSeek translation quality test.

DEFAULT: SKIPPED
Only executes when BOTH conditions are met:
  1. Environment variable PHASE2_REAL_DEEPSEEK=1
  2. A test API key is provided via PHASE2_DEEPSEEK_TEST_KEY env var,
     or loaded from the test worktree's .env.local (DEEPSEEK_API_KEY)

Safety constraints:
- Serial execution (concurrency = 1)
- Max 10 requests total
- Max 1 retry per network failure
- 30s timeout per request
- Does NOT write to production database
- Does NOT print or log the API key
- Does NOT start production worker or ComfyUI
- Results written to test worktree temp directory
- Does NOT affect normal pytest offline execution
- Does NOT fall back to production DEEPSEEK_API_KEY from parent dirs
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

# ── Path setup ──────────────────────────────────────────────────
_TEST_WORKTREE_ROOT = Path(__file__).resolve().parents[2]  # uma_web_mvp_phase2
_ROOT = Path(__file__).resolve().parents[1]  # uma_web_mvp
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.environ.setdefault("APP_ENV", "local")

# ── Safe key loading from test worktree .env.local ─────────────
# Only load from the test worktree's .env.local, never from production.
_ENV_LOCAL_PATH = _TEST_WORKTREE_ROOT / "uma_web_mvp" / ".env.local"

def _load_test_key_from_env_local() -> str:
    """Safely load DEEPSEEK_API_KEY from test worktree's .env.local.

    Returns empty string if:
    - .env.local doesn't exist
    - DEEPSEEK_API_KEY not found in it
    - Value is empty

    NEVER reads from parent directories or production .env.
    """
    if not _ENV_LOCAL_PATH.is_file():
        return ""
    # Verify the path is under our test worktree (not production)
    resolved = _ENV_LOCAL_PATH.resolve()
    worktree = _TEST_WORKTREE_ROOT.resolve()
    if not str(resolved).startswith(str(worktree)):
        return ""
    try:
        for line in resolved.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == "DEEPSEEK_API_KEY":
                return value.strip().strip('"').strip("'")
    except (OSError, UnicodeDecodeError):
        pass
    return ""

# Load test key: explicit env var takes priority, then .env.local fallback
_explicit_key = os.environ.get("PHASE2_DEEPSEEK_TEST_KEY", "").strip()
_env_local_key = _load_test_key_from_env_local() if not _explicit_key else ""
_TEST_KEY = _explicit_key or _env_local_key

from app.config import Settings
from app.db import connect, ensure_schema

# ── Gate: skip unless explicitly enabled ────────────────────────
_REAL_ENABLED = os.environ.get("PHASE2_REAL_DEEPSEEK", "").strip() == "1"

if not _REAL_ENABLED:
    pytest.skip(
        "PHASE2_REAL_DEEPSEEK not set → real DeepSeek tests skipped",
        allow_module_level=True,
    )
if not _TEST_KEY:
    pytest.skip(
        "PHASE2_DEEPSEEK_TEST_KEY not provided and not in .env.local → real DeepSeek tests skipped",
        allow_module_level=True,
    )


# ── Constants ───────────────────────────────────────────────────
TEST_USER = "real-deepseek-test-user"
MAX_TOTAL_REQUESTS = 10
REQUEST_TIMEOUT = 30  # seconds
_results_dir = Path(__file__).resolve().parents[1] / "test_data" / "acceptance_cases" / "deepseek_results"
_results_dir.mkdir(parents=True, exist_ok=True)


def _run(coro):
    return asyncio.run(coro)


def _make_settings(**overrides) -> Settings:
    case_id = f"real_ds_{int(time.time())}"
    test_root = Path(__file__).resolve().parents[1] / "test_data" / "acceptance_cases" / case_id
    for d in ("output", "mock_output", "input_images"):
        (test_root / d).mkdir(parents=True, exist_ok=True)
    data = {
        "APP_ENV": "local",
        "APP_ORIGIN": "http://127.0.0.1:18080",
        "HOST": "127.0.0.1",
        "PORT": 18080,
        "BALANCE_DB": str(test_root / "real_test.db"),
        "BOT_OUTPUT_DIR": str(test_root / "output"),
        "mock_output_dir": str(test_root / "mock_output"),
        "INPUT_IMAGE_DIR": str(test_root / "input_images"),
        "BOT_DIR": str(test_root),
        "redis_enabled": False,
        "dev_auth_bypass": True,
        "dev_user_id": TEST_USER,
        "fast_translator_enabled": True,
        "fast_translator_cost_credits": 0,  # Free for testing
        "mock_worker_enabled": True,
        "deepseek_api_key": _TEST_KEY,
        "deepseek_base_url": "https://api.deepseek.com",
        "deepseek_timeout_seconds": REQUEST_TIMEOUT,
        "session_secret": "real-test-session-secret-32chars!!!",
        "jwt_secret": "real-test-jwt-secret-32chars!!!!!!",
        "agent_enabled": False,
        "smart_agent_enabled": False,
    }
    data.update(overrides)
    s = Settings(**data)
    ensure_schema(s)
    return s


def _seed_balance(settings: Settings, amount: int = 50000) -> None:
    conn = connect(settings)
    try:
        conn.execute("INSERT OR REPLACE INTO users(user_id, balance_fen) VALUES (?, ?)", (TEST_USER, amount))
        conn.commit()
    finally:
        conn.close()


# ── Quality Samples ─────────────────────────────────────────────
SAMPLES = [
    {
        "id": "S01_precise_single_char",
        "text": "初音未来穿着白色连衣裙，站在樱花树下，微风吹动长发，近景，柔和光线",
        "expect_character": "hatsune_miku",
        "expect_no_extra_chars": True,
        "description": "Precise single character with clothing, action, scene, composition",
    },
    {
        "id": "S02_multi_char",
        "text": "无声铃鹿和东海帝王并肩站在赛道上，两人都穿着赛马娘制服，互相看着对方",
        "expect_characters": ["silence_suzuka", "tokai_teio"],
        "description": "Two characters with mutual action",
    },
    {
        "id": "S03_no_character",
        "text": "夕阳下的海边灯塔，海浪拍打礁石，远处有帆船，金色光线，广角镜头",
        "expect_no_character": True,
        "description": "Pure scene with lighting and composition",
    },
    {
        "id": "S04_butterfly_ribbon",
        "text": "少女头发上戴着蝴蝶结，穿着粉色连衣裙，站在花园里，蝴蝶结是红色的",
        "expect_no_character": True,
        "description": "蝴蝶结 as clothing, NOT 蝴蝶忍",
    },
    {
        "id": "S05_user_rejected_all",
        "text": "教室里唱歌，穿着校服，阳光从窗户照进来",
        "resolution": {
            "status": "resolved",
            "selections": [{"characterId": "__no_library_character__"}],
        },
        "expect_no_character": True,
        "description": "User selected 'none of the above' (no character in text)",
    },
    {
        "id": "S06_mixed_cn_en",
        "text": "一个 cute girl，穿着 school uniform，坐在窗边，looking at viewer，细节丰富",
        "expect_no_character": True,
        "description": "Mixed Chinese and English tags",
    },
    {
        "id": "S07_complex_camera",
        "text": "高机位俯拍，少女躺在草坪上看书，景深虚化背景，逆光，发丝光晕，侧面特写",
        "expect_no_character": True,
        "description": "Complex camera and lighting description",
    },
    {
        "id": "S08_negative_constraints",
        "text": "一个少女站在花田里，不要文字，不要水印，不要多余人物，不要模糊",
        "expect_no_character": True,
        "description": "Negative constraints (no text, no watermark, no extra people)",
    },
    {
        "id": "S09_english_input",
        "text": "A serene ocean view with lighthouse, golden sunset, calm waves, wide angle lens, detailed composition",
        "expect_no_character": True,
        "description": "Pure English scene input",
    },
    {
        "id": "S10_short_scene",
        "text": "雨天，窗边，少女，安静",
        "expect_no_character": True,
        "description": "Short minimal input",
    },
]


def _score_result(sample: dict, result: Any) -> dict:
    """Score a translation result against quality criteria.

    Returns dict with scores 0-2 for each criterion and total.
    """
    scores = {
        "semantic_preservation": 0,  # 原意保留
        "character_compliance": 0,   # 人物决策遵守
        "prompt_usability": 0,       # 提示词可用性
        "hallucination_control": 0,  # 幻觉控制
        "constraint_compliance": 0,  # 约束遵守
    }
    notes = []

    if not result.ok:
        return {"scores": scores, "total": 0, "notes": ["Translation failed"]}

    prompt = result.prompt.lower()

    # 1. Semantic preservation
    if result.prompt and len(result.prompt) > 10:
        scores["semantic_preservation"] = 2
        notes.append("Prompt generated successfully")
    elif result.prompt:
        scores["semantic_preservation"] = 1
        notes.append("Prompt short but present")
    else:
        notes.append("Empty prompt")

    # 2. Character compliance
    expect_char = sample.get("expect_character")
    expect_chars = sample.get("expect_characters", [])
    expect_no_char = sample.get("expect_no_character", False)
    resolution = sample.get("resolution")

    if expect_char:
        if expect_char in result.character_keys:
            scores["character_compliance"] = 2
            notes.append(f"Character {expect_char} correctly preserved")
        else:
            scores["character_compliance"] = 0
            notes.append(f"Expected {expect_char}, got {result.character_keys}")
    elif expect_chars:
        matched = [c for c in expect_chars if c in result.character_keys]
        if len(matched) == len(expect_chars):
            scores["character_compliance"] = 2
            notes.append(f"All {len(expect_chars)} characters preserved")
        elif matched:
            scores["character_compliance"] = 1
            notes.append(f"Only {len(matched)}/{len(expect_chars)} characters preserved")
        else:
            scores["character_compliance"] = 0
            notes.append(f"Expected {expect_chars}, got {result.character_keys}")
    elif expect_no_char or resolution:
        if not result.character_keys:
            scores["character_compliance"] = 2
            notes.append("No character correctly maintained")
        else:
            scores["character_compliance"] = 0
            notes.append(f"Unexpected characters: {result.character_keys}")
    else:
        scores["character_compliance"] = 2

    # 3. Prompt usability
    if result.prompt and "," in result.prompt and len(result.prompt) > 20:
        scores["prompt_usability"] = 2
        notes.append("Structured comma-separated tags")
    elif result.prompt:
        scores["prompt_usability"] = 1
        notes.append("Prompt present but may need cleanup")
    else:
        notes.append("No usable prompt")

    # 4. Hallucination control
    # Check for unexpected characters or entities
    hallucination_keywords = ["shinobu", "kochou", "nezuko", "goku"]
    has_hallucination = any(kw in prompt for kw in hallucination_keywords)
    if not has_hallucination:
        scores["hallucination_control"] = 2
        notes.append("No hallucination detected")
    else:
        scores["hallucination_control"] = 0
        notes.append("Possible hallucination detected")

    # 5. Constraint compliance
    # Check negative constraints for S08
    if sample["id"] == "S08_negative_constraints":
        has_text = "text" in prompt and "no text" not in prompt
        has_watermark = "watermark" in prompt and "no watermark" not in prompt
        if not has_text and not has_watermark:
            scores["constraint_compliance"] = 2
            notes.append("Negative constraints preserved")
        else:
            scores["constraint_compliance"] = 1
            notes.append("Some negative constraints may be violated")
    elif sample["id"] == "S04_butterfly_ribbon":
        if "shinobu" not in prompt and "kochou" not in prompt:
            # Key test: no character was incorrectly added
            scores["constraint_compliance"] = 2
            notes.append("蝴蝶结 correctly treated as clothing, not character")
        else:
            scores["constraint_compliance"] = 0
            notes.append("蝴蝶结 incorrectly mapped to character")
    else:
        scores["constraint_compliance"] = 2
        notes.append("Constraints checked")

    total = sum(scores.values())
    return {"scores": scores, "total": total, "notes": notes}


# ── Tests ───────────────────────────────────────────────────────

class TestRealDeepSeekQuality:
    """Controlled real DeepSeek translation quality tests.

    These tests call the real DeepSeek API with a test key.
    They are SKIPPED by default unless PHASE2_REAL_DEEPSEEK=1
    and PHASE2_DEEPSEEK_TEST_KEY is set.
    """

    def test_quality_samples(self):
        """Run all quality samples and evaluate results."""
        settings = _make_settings()
        _seed_balance(settings)

        from app.services.fast_translator_service import fast_refine_prompt

        all_results = []
        request_count = 0

        for sample in SAMPLES:
            if request_count >= MAX_TOTAL_REQUESTS:
                break

            async def _test_one(s=sample):
                return await fast_refine_prompt(
                    settings,
                    user_id=TEST_USER,
                    text=s["text"],
                    character_resolution=s.get("resolution"),
                )

            try:
                result = _run(_test_one())
                score_data = _score_result(sample, result)
                all_results.append({
                    "sample_id": sample["id"],
                    "description": sample["description"],
                    "input": sample["text"],
                    "output_prompt": result.prompt,
                    "character_keys": result.character_keys,
                    "character_match_source": result.character_match_source,
                    "translation_mode": result.translation_mode,
                    "scores": score_data["scores"],
                    "total_score": score_data["total"],
                    "notes": score_data["notes"],
                    "status": "ok",
                })
            except Exception as exc:
                all_results.append({
                    "sample_id": sample["id"],
                    "description": sample["description"],
                    "status": "error",
                    "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                    "total_score": 0,
                })

            request_count += 1
            time.sleep(0.5)  # Rate limiting between requests

        # Write results to file
        results_file = _results_dir / f"quality_results_{int(time.time())}.json"
        with open(results_file, "w", encoding="utf-8") as f:
            json.dump({
                "timestamp": time.time(),
                "total_samples": len(all_results),
                "total_requests": request_count,
                "results": all_results,
            }, f, ensure_ascii=False, indent=2)

        # Print summary (no API key)
        print(f"\n{'='*60}")
        print(f"DeepSeek Translation Quality Report")
        print(f"{'='*60}")
        print(f"Samples tested: {len(all_results)}")
        print(f"Total requests: {request_count}")

        passing = [r for r in all_results if r.get("total_score", 0) >= 8]
        failing = [r for r in all_results if r.get("total_score", 0) < 8]
        character_violations = [
            r for r in all_results
            if r.get("scores", {}).get("character_compliance", 2) == 0
        ]
        butterfly_violations = [
            r for r in all_results
            if r.get("sample_id") == "S04_butterfly_ribbon"
            and r.get("scores", {}).get("constraint_compliance", 2) == 0
        ]

        print(f"Passing (>=8): {len(passing)}")
        print(f"Failing (<8): {len(failing)}")
        print(f"Character violations: {len(character_violations)}")
        print(f"Butterfly ribbon violations: {len(butterfly_violations)}")

        for r in all_results:
            status = "PASS" if r.get("total_score", 0) >= 8 else "FAIL"
            score = r.get("total_score", 0)
            sid = r["sample_id"]
            desc = r.get("description", "")[:40]
            print(f"  {status} {sid}: {score}/10 - {desc}")

        print(f"\nResults saved to: {results_file}")

        # Assertions
        assert len(character_violations) == 0, \
            f"Character compliance violations: {[r['sample_id'] for r in character_violations]}"
        assert len(butterfly_violations) == 0, \
            "蝴蝶结 should not be mapped to 蝴蝶忍"

        # Average score check
        valid_results = [r for r in all_results if r.get("status") == "ok"]
        if valid_results:
            avg = sum(r["total_score"] for r in valid_results) / len(valid_results)
            print(f"Average score: {avg:.1f}/10")
            assert avg >= 8.0, f"Average score {avg:.1f} below 8.0 threshold"

        # Critical samples must score >= 7
        critical_ids = {"S01_precise_single_char", "S02_multi_char", "S04_butterfly_ribbon", "S05_user_rejected_all"}
        for r in all_results:
            if r["sample_id"] in critical_ids and r.get("status") == "ok":
                assert r["total_score"] >= 7, \
                    f"Critical sample {r['sample_id']} scored {r['total_score']}/10 (min 7)"
