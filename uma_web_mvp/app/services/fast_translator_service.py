from __future__ import annotations

import json
import re
import secrets
import string
import time
from dataclasses import dataclass
from typing import Any

from app.agent import _apply_character_registry_to_refined_prompt
from app.config import Settings
from app.db import connect
from app.services.deepseek_service import DeepSeekError, DeepSeekService
from app.smart_agent.character_preferences import split_prompt_tags
from app.smart_agent.disambiguation_engine import (
    NO_LIBRARY_CHARACTER_ID,
    analyze_character_mentions,
    validate_character_resolution,
)

FAST_TRANSLATE_CHARGE_REASON = "fast_translate_charge"
FAST_TRANSLATE_REFUND_REASON = "fast_translate_refund"
PATH_RE = re.compile(r"([A-Za-z]:\\|/mnt/|/home/)")
CHINESE_RE = re.compile(r"[\u3400-\u9fff]")
FIELD_ORDER = ("clothing", "action", "expression", "composition", "scene", "lighting", "mood", "style")


class FastTranslatorError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class CharacterSelectionRequired(FastTranslatorError):
    def __init__(self, resolution: dict[str, Any]):
        super().__init__("character_resolution_required", "请选择具体人物后继续生成")
        self.resolution = resolution


@dataclass
class FastTranslateResult:
    ok: bool
    prompt: str
    translation_mode: str
    character_match_source: str
    character_keys: list[str]
    charged_credits: int
    request_code: str
    status: str = "done"


def make_translation_code() -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "TR-" + "".join(secrets.choice(alphabet) for _ in range(12))


def _safe_tags_from_model(data: dict[str, Any]) -> str:
    tags: list[str] = []
    for field in FIELD_ORDER:
        value = data.get(field)
        if isinstance(value, list):
            raw = ", ".join(str(item) for item in value)
        else:
            raw = str(value or "")
        if PATH_RE.search(raw):
            raise FastTranslatorError("invalid_model_output", "极速翻译返回内容无效，请稍后重试")
        raw = raw.replace("```", "").replace("<think>", "").replace("</think>", "")
        for tag in split_prompt_tags(raw):
            if PATH_RE.search(tag):
                raise FastTranslatorError("invalid_model_output", "极速翻译返回内容无效，请稍后重试")
            if CHINESE_RE.search(tag):
                continue
            tags.append(tag)
    deduped: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        key = tag.lower().replace("_", " ").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(tag)
    prompt = ", ".join(deduped).strip()
    if not prompt:
        raise FastTranslatorError("empty_model_output", "极速翻译暂时没有生成有效 Prompt")
    return prompt[:2000]


def _resolve_characters(prompt: str, resolution: dict[str, Any] | None) -> tuple[list[str], str]:
    if resolution:
        try:
            validated = validate_character_resolution(prompt, resolution)
        except ValueError:
            # validate_character_resolution raises when parser finds mentions
            # but selections don't match. Check if user provided explicit IDs
            # and reject them as invalid.
            selections = resolution.get("selections") or []
            requested_ids = [
                str(s.get("characterId") or s.get("selectedCharacterId") or "").strip()
                for s in selections
                if isinstance(s, dict)
                   and str(s.get("characterId") or s.get("selectedCharacterId") or "").strip()
                   and str(s.get("characterId") or s.get("selectedCharacterId") or "").strip() != NO_LIBRARY_CHARACTER_ID
            ]
            if requested_ids:
                raise FastTranslatorError("invalid_character_resolution", "人物选择结果无效，请重新选择。")
            raise
        ids = [
            str(item.get("characterId") or item.get("key") or "").strip()
            for item in validated.get("resolvedCharacters", []) or []
            if str(item.get("characterId") or item.get("key") or "").strip()
        ]
        skipped = list(validated.get("skippedMentions") or [])
        # Extract requested IDs from selections to validate completeness
        selections = resolution.get("selections") or []
        requested_ids = [
            str(s.get("characterId") or s.get("selectedCharacterId") or "").strip()
            for s in selections
            if isinstance(s, dict)
               and str(s.get("characterId") or s.get("selectedCharacterId") or "").strip()
               and str(s.get("characterId") or s.get("selectedCharacterId") or "").strip() != NO_LIBRARY_CHARACTER_ID
        ]
        requested_ids = list(dict.fromkeys(requested_ids))  # dedupe preserving order
        if requested_ids:
            # User explicitly selected characters — all must be valid
            id_set = set(ids)
            invalid = [rid for rid in requested_ids if rid not in id_set]
            if invalid:
                raise FastTranslatorError("invalid_character_resolution", "人物选择结果无效，请重新选择。")
        if ids:
            return list(dict.fromkeys(ids)), "resolved"
        if skipped:
            return [], "none"
        return [], "none"
    parsed = analyze_character_mentions(prompt)
    if parsed.get("status") in {"ambiguous", "mixed"}:
        raise CharacterSelectionRequired(parsed)
    ids = [
        str(item.get("characterId") or "").strip()
        for item in parsed.get("resolvedCharacters", []) or []
        if str(item.get("characterId") or "").strip()
    ]
    if ids:
        return list(dict.fromkeys(ids)), "library"
    return [], "none"


def _begin_charge(settings: Settings, *, user_id: str, text: str, client_request_id: str | None, character_keys: list[str], source: str) -> dict[str, Any]:
    now = int(time.time())
    request_id = str(client_request_id or "").strip()[:80] or None
    cost = max(0, int(settings.fast_translator_cost_credits))
    request_code = make_translation_code()
    conn = connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if request_id:
            existing = conn.execute(
                "SELECT * FROM translation_requests WHERE user_id=? AND client_request_id=?",
                (user_id, request_id),
            ).fetchone()
            if existing:
                conn.commit()
                return {"existing": dict(existing)}
        conn.execute("INSERT OR IGNORE INTO users(user_id, balance_fen) VALUES (?, 0)", (user_id,))
        ledger_id = None
        if cost:
            cur = conn.execute(
                "UPDATE users SET balance_fen=balance_fen-? WHERE user_id=? AND balance_fen>=?",
                (cost, user_id, cost),
            )
            if cur.rowcount != 1:
                raise FastTranslatorError("insufficient_credits", "Credits 不足，请充值后重试")
            ledger = conn.execute(
                "INSERT INTO balance_ledger(user_id,amount_fen,reason,order_code,operator_id,created_at) VALUES (?,?,?,?,?,?)",
                (user_id, -cost, FAST_TRANSLATE_CHARGE_REASON, request_code, user_id, now),
            )
            ledger_id = int(ledger.lastrowid)
        conn.execute(
            """
            INSERT INTO translation_requests(
                request_code,user_id,client_request_id,translation_mode,model,character_match_source,
                character_keys_json,original_text,charged_credits,ledger_id,status,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                request_code, user_id, request_id, "fast", settings.deepseek_model, source,
                json.dumps(character_keys, ensure_ascii=False), text, cost, ledger_id, "processing", now,
            ),
        )
        conn.commit()
        return {"request_code": request_code, "charged_credits": cost}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _finish(settings: Settings, *, request_code: str, prompt: str, status: str = "done", error_code: str = "") -> None:
    conn = connect(settings)
    try:
        conn.execute(
            "UPDATE translation_requests SET refined_prompt=?, status=?, error_code=?, finished_at=? WHERE request_code=?",
            (prompt, status, error_code, int(time.time()), request_code),
        )
        conn.commit()
    finally:
        conn.close()


def _refund(settings: Settings, *, request_code: str, error_code: str) -> None:
    now = int(time.time())
    conn = connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT user_id, charged_credits, status FROM translation_requests WHERE request_code=?", (request_code,)).fetchone()
        if not row:
            conn.rollback()
            return
        charged = int(row["charged_credits"] or 0)
        if charged:
            existing = conn.execute(
                "SELECT id FROM balance_ledger WHERE order_code=? AND reason=? LIMIT 1",
                (request_code, FAST_TRANSLATE_REFUND_REASON),
            ).fetchone()
            if not existing:
                conn.execute("UPDATE users SET balance_fen=balance_fen+? WHERE user_id=?", (charged, row["user_id"]))
                conn.execute(
                    "INSERT INTO balance_ledger(user_id,amount_fen,reason,order_code,operator_id,created_at) VALUES (?,?,?,?,?,?)",
                    (row["user_id"], charged, FAST_TRANSLATE_REFUND_REASON, request_code, "fast_translator", now),
                )
        conn.execute(
            "UPDATE translation_requests SET status='failed_refunded', error_code=?, finished_at=? WHERE request_code=?",
            (error_code, now, request_code),
        )
        conn.commit()
    finally:
        conn.close()


def _result_from_row(row: dict[str, Any]) -> FastTranslateResult:
    return FastTranslateResult(
        ok=str(row.get("status") or "") == "done",
        prompt=str(row.get("refined_prompt") or ""),
        translation_mode="fast",
        character_match_source=str(row.get("character_match_source") or "none"),
        character_keys=json.loads(row.get("character_keys_json") or "[]"),
        charged_credits=int(row.get("charged_credits") or 0),
        request_code=str(row.get("request_code") or ""),
        status=str(row.get("status") or ""),
    )


async def fast_refine_prompt(
    settings: Settings,
    *,
    user_id: str,
    text: str,
    client_request_id: str | None = None,
    character_resolution: dict[str, Any] | None = None,
    deepseek: DeepSeekService | None = None,
) -> FastTranslateResult:
    if not settings.fast_translator_enabled:
        raise FastTranslatorError("fast_translator_disabled", "极速翻译当前未启用")
    raw = str(text or "").strip()
    if not raw:
        raise FastTranslatorError("prompt_required", "请填写描述")
    if len(raw) > 3000:
        raise FastTranslatorError("prompt_too_long", "描述过长，请控制在 3000 字符以内")
    character_keys, source = _resolve_characters(raw, character_resolution)
    charge = _begin_charge(
        settings,
        user_id=user_id,
        text=raw,
        client_request_id=client_request_id,
        character_keys=character_keys,
        source=source,
    )
    if "existing" in charge:
        return _result_from_row(charge["existing"])
    request_code = str(charge["request_code"])
    try:
        ds = deepseek or DeepSeekService(settings)
        data = await ds.complete_json(
            system_prompt=FAST_TRANSLATOR_SYSTEM_PROMPT,
            user_prompt=raw,
            temperature=0.15,
            max_tokens=1200,
            timeout_seconds=settings.deepseek_timeout_seconds,
            purpose="fast_translator",
        )
        scene_prompt = _safe_tags_from_model(data)
        final_prompt = _apply_character_registry_to_refined_prompt(
            raw,
            scene_prompt,
            resolved_character_ids=character_keys,
            disable_character_library=source == "none" and bool(character_resolution),
        )
        if not final_prompt:
            raise FastTranslatorError("empty_prompt", "极速翻译暂时没有生成有效 Prompt")
        _finish(settings, request_code=request_code, prompt=final_prompt)
        return FastTranslateResult(
            ok=True,
            prompt=final_prompt,
            translation_mode="fast",
            character_match_source=source,
            character_keys=character_keys,
            charged_credits=int(charge["charged_credits"]),
            request_code=request_code,
        )
    except (DeepSeekError, FastTranslatorError) as exc:
        code = getattr(exc, "code", "deepseek_failed")
        _refund(settings, request_code=request_code, error_code=str(code))
        if isinstance(exc, FastTranslatorError):
            raise
        raise FastTranslatorError("deepseek_failed", "极速翻译暂时不可用，请稍后重试") from exc
    except Exception as exc:
        _refund(settings, request_code=request_code, error_code="fast_translate_failed")
        raise FastTranslatorError("fast_translate_failed", "极速翻译暂时不可用，请稍后重试") from exc


FAST_TRANSLATOR_SYSTEM_PROMPT = """
You convert non-character visual details into JSON for anime image generation.
The server handles character identity tags. Do not output character names.
Return JSON only with keys:
clothing, action, expression, composition, scene, lighting, mood, style.
Use concise English booru-style tags separated by commas inside each value.
Do not output markdown, explanations, paths, secrets, Chinese text, or file names.
Do not infer physical appearance unless the user explicitly described it.
""".strip()
