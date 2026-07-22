"""
人物歧义引擎 — 基于全库索引的通用、可扩展人物歧义识别与确认机制。

核心：
- 使用 character_index 中的预建索引进行 O(1) mention lookup
- 返回 mention_groups 结构，区分同 span 歧义与不同 span 合法多人
- 作品上下文消歧
- 长名称优先但不吞同名歧义
"""

from __future__ import annotations

import hashlib
import re
import time
from typing import Any

from .character_index import (
    find_characters_by_mention_v2,
    resolve_mention_groups_with_franchise,
    extract_franchise_hints,
    get_identity,
    get_index_stats,
    get_duplicate_groups,
    get_zh_substring_groups,
    get_all_identities,
)


NO_LIBRARY_CHARACTER_ID = "__no_library_character__"


def analyze_character_mentions(text: str) -> dict[str, Any]:
    """Return shared character resolution state for both Agent entry points.

    This wraps the existing Smart Agent ambiguity analyzer but exposes a stable
    distinction between user mention slots, candidate characters, and confirmed
    characters.  Only resolvedCharacters are safe to use for tag injection.
    """
    raw = str(text or "").strip()
    if not raw:
        return {
            "status": "not_found",
            "mentions": [],
            "resolvedCharacters": [],
            "unresolvedMentions": [],
        }
    if _looks_like_original_character_request(raw):
        return {
            "status": "not_found",
            "mentions": [],
            "resolvedCharacters": [],
            "unresolvedMentions": [],
        }

    analysis = analyze_user_request(raw)
    mentions: list[dict[str, Any]] = []
    resolved: list[dict[str, Any]] = []

    resolved_seen: set[str] = set()
    for item in analysis.get("resolved_characters", []) or []:
        if _is_non_subject_character_reference(raw, item):
            continue
        public = _candidate_to_public_character(item)
        cid = public.get("characterId")
        if cid and cid not in resolved_seen:
            resolved.append(public)
            resolved_seen.add(cid)

    for group in analysis.get("groups", []) or []:
        candidates = [
            _candidate_to_public_character(c)
            for c in group.get("candidates", []) or []
            if not _is_non_subject_character_reference(raw, c)
        ]
        candidates = [c for c in candidates if c.get("characterId")]
        if len(candidates) >= 2:
            mention_id = str(group.get("group_id") or _generate_group_id())
            mentions.append({
                "mentionId": mention_id,
                "rawText": str(group.get("mention") or ""),
                "start": int((group.get("span") or [0, 0])[0] or 0),
                "end": int((group.get("span") or [0, 0])[1] or 0),
                "status": "ambiguous",
                "candidates": _dedupe_public_candidates(candidates),
                "resolvedCharacterId": None,
            })
        elif len(candidates) == 1:
            cid = candidates[0].get("characterId")
            if cid and cid not in resolved_seen:
                resolved.append(candidates[0])
                resolved_seen.add(cid)

    unresolved = [m["mentionId"] for m in mentions if m.get("status") == "ambiguous"]
    if unresolved and resolved:
        status = "mixed"
    elif unresolved:
        status = "ambiguous"
    elif resolved:
        status = "resolved"
    else:
        status = "not_found"
    return {
        "status": status,
        "mentions": mentions,
        "resolvedCharacters": resolved,
        "unresolvedMentions": unresolved,
    }


def validate_character_resolution(
    prompt: str,
    resolution: dict[str, Any] | None,
) -> dict[str, Any]:
    """Validate user selections against the current parser result.

    Frontend-provided IDs are never trusted directly.  The prompt is re-parsed
    and every selected character must belong to the corresponding candidate set.
    """
    parsed = analyze_character_mentions(prompt)
    selections = []
    if isinstance(resolution, dict):
        raw_selections = resolution.get("selections") or []
        if isinstance(raw_selections, list):
            selections = [x for x in raw_selections if isinstance(x, dict)]

    by_id = {str(m.get("mentionId") or ""): m for m in parsed.get("mentions", [])}
    by_mention = {str(m.get("rawText") or ""): m for m in parsed.get("mentions", [])}
    resolved_characters = list(parsed.get("resolvedCharacters") or [])
    selected_ids: set[str] = {str(c.get("characterId") or "") for c in resolved_characters}
    skipped_mentions: list[str] = []

    for mention in parsed.get("mentions", []) or []:
        selection = _find_selection_for_mention(selections, mention, by_id, by_mention)
        if not selection:
            raise ValueError("character_resolution_required")
        selected_id = str(selection.get("characterId") or selection.get("selectedCharacterId") or "").strip()
        skip_library = bool(selection.get("skipCharacterLibrary")) or selected_id == NO_LIBRARY_CHARACTER_ID
        if skip_library:
            skipped_mentions.append(str(mention.get("mentionId") or ""))
            continue
        candidates = list(mention.get("candidates") or [])
        matched = next((c for c in candidates if str(c.get("characterId") or "") == selected_id), None)
        if not matched:
            raise ValueError("invalid_character_resolution")
        if selected_id not in selected_ids:
            resolved_characters.append(matched)
            selected_ids.add(selected_id)

    # Fallback: parser found no mentions but user provided selections.
    # Directly validate selected character IDs against the library.
    if not resolved_characters and selections:
        from .character_search import load_characters
        library = {str(_public_character_id(c)): c for c in load_characters()}
        for sel in selections:
            sid = str(sel.get("characterId") or sel.get("selectedCharacterId") or "").strip()
            if not sid or sid == NO_LIBRARY_CHARACTER_ID:
                continue
            if sid in library and sid not in selected_ids:
                resolved_characters.append(library[sid])
                selected_ids.add(sid)

    return {
        "status": "resolved" if resolved_characters else "not_found",
        "resolvedCharacters": resolved_characters,
        "skippedMentions": skipped_mentions,
        "parsed": parsed,
    }


def characters_from_public_ids(character_ids: list[str]) -> list[dict[str, Any]]:
    """Load full character records for validated public character IDs."""
    if not character_ids:
        return []
    wanted = {str(x or "").strip() for x in character_ids if str(x or "").strip()}
    if not wanted:
        return []
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    from .character_search import load_characters

    for item in load_characters():
        cid = _public_character_id(item)
        if cid in wanted and cid not in seen:
            records.append(dict(item))
            seen.add(cid)
    return records


def _find_selection_for_mention(
    selections: list[dict[str, Any]],
    mention: dict[str, Any],
    by_id: dict[str, dict[str, Any]],
    by_mention: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    mention_id = str(mention.get("mentionId") or "")
    raw_text = str(mention.get("rawText") or "")
    for selection in selections:
        sid = str(selection.get("mentionId") or selection.get("group_id") or "").strip()
        sraw = str(selection.get("rawText") or selection.get("mention") or "").strip()
        if sid and sid in by_id and sid == mention_id:
            return selection
        if sraw and sraw in by_mention and sraw == raw_text:
            return selection
    return None


def _candidate_to_public_character(candidate: dict[str, Any]) -> dict[str, Any]:
    cid = _public_character_id(candidate)
    return {
        "characterId": cid,
        "character_key": cid,
        "name": candidate.get("name_zh") or candidate.get("name_en") or cid,
        "name_zh": candidate.get("name_zh") or "",
        "name_en": candidate.get("name_en") or "",
        "series": _candidate_franchise_display(candidate),
        "franchise": _candidate_franchise_display(candidate),
    }


def _public_character_id(candidate: dict[str, Any]) -> str:
    raw = str(
        candidate.get("characterId")
        or candidate.get("character_key")
        or candidate.get("key")
        or candidate.get("identity_key")
        or candidate.get("name_en")
        or candidate.get("name_zh")
        or ""
    ).strip()
    return _normalize_candidate_key(raw).replace(" ", "_")


def _dedupe_public_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        cid = str(candidate.get("characterId") or "")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        result.append(candidate)
    display_priority = {
        "nanami_mami": 10,
        "tomoe_mami": 20,
    }
    result.sort(key=lambda item: (
        display_priority.get(str(item.get("characterId") or ""), 100),
        str(item.get("name") or item.get("name_zh") or item.get("name_en") or ""),
    ))
    return result


def _looks_like_original_character_request(text: str) -> bool:
    raw = str(text or "").lower()
    return any(marker in raw for marker in (
        "原创角色", "原创人物", "原创女生", "原创女孩", "oc ", " oc", "不是动漫角色", "不是已有角色",
        "original character", "my oc",
    ))


def _is_non_subject_character_reference(text: str, candidate: dict[str, Any]) -> bool:
    raw = str(text or "")
    variants = [
        str(candidate.get("matched_term") or ""),
        str(candidate.get("name_zh") or ""),
        str(candidate.get("name_en") or ""),
    ]
    variants.extend(str(candidate.get("aliases") or "").replace("，", ",").split(","))
    variants = [v.strip() for v in variants if v and v.strip()]
    if not variants:
        return False
    for variant in variants:
        if _has_negative_context(raw, variant) or _has_style_reference_context(raw, variant):
            return True
    return False


def _has_negative_context(text: str, term: str) -> bool:
    escaped = re.escape(str(term).strip())
    if not escaped:
        return False
    patterns = [
        rf"(?:不要|不出现|别出现|除了|排除|不是)\s*.{{0,10}}{escaped}",
        rf"(?:without|exclude|no)\s+.{{0,24}}{escaped}",
    ]
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _has_style_reference_context(text: str, term: str) -> bool:
    escaped = re.escape(str(term).strip())
    if not escaped:
        return False
    patterns = [
        rf"(?:参考|类似|像)\s*.{{0,12}}{escaped}\s*.{{0,12}}(?:风格|服装|衣服|穿搭)",
        rf"{escaped}\s*.{{0,10}}(?:风格|服装风格|穿搭风格)",
        rf"(?:style|outfit|clothing)\s+reference\s+.{{0,24}}{escaped}",
        rf"{escaped}\s+.{{0,24}}(?:style|outfit|clothing)\s+reference",
    ]
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def analyze_user_request(text: str) -> dict[str, Any]:
    """分析用户请求中的人物引用。

    Returns:
        {
            "groups": [
                {
                    "group_id": "DG-xxxxxx",
                    "span": [start, end],
                    "mention": "爱丽丝",
                    "candidates": [...],
                    "status": "ambiguous",   # ambiguous | resolved | filtered
                    "match_type": "zh_substring",
                },
                ...
            ],
            "resolved_characters": [...],  # 已确定的角色（单人独有完整名）
            "franchise_hints": ["nikke"],
            "is_ambiguous": True,          # 是否存在任何未解决的歧义
            "total_mentions": 3,
            "ambiguous_count": 1,
        }
    """
    if not text or not text.strip():
        return {"groups": [], "resolved_characters": [], "franchise_hints": [], "is_ambiguous": False}
    if _looks_like_original_character_request(text):
        return {"groups": [], "resolved_characters": [], "franchise_hints": [], "is_ambiguous": False}

    # 1. 提取作品上下文
    franchise_hints = extract_franchise_hints(text)

    # 2. 使用索引进行 mention 匹配
    mention_groups = find_characters_by_mention_v2(text)

    # 3. 作品上下文消歧
    if franchise_hints:
        mention_groups = resolve_mention_groups_with_franchise(mention_groups, franchise_hints)

    # 4. 分类：resolved（1个候选）vs ambiguous（多个候选）
    resolved: list[dict[str, Any]] = []
    ambiguous_groups: list[dict[str, Any]] = []

    for g in mention_groups:
        g["candidates"] = [
            c for c in g.get("candidates", [])
            if not _is_non_subject_character_reference(text, c)
        ]
        if len(g["candidates"]) == 0:
            continue
        if len(g["candidates"]) == 1:
            # 唯一候选 → 自动确定
            resolved.append(g["candidates"][0])
        else:
            # 多候选 → 需要用户确认
            g["group_id"] = _generate_group_id()
            g["status"] = "ambiguous"
            ambiguous_groups.append(g)

    # 5. 去重：已 resolved 的 identity 不应再出现在 ambiguous 中
    resolved_iks = {c.get("identity_key", "") for c in resolved if c.get("identity_key")}
    for g in ambiguous_groups:
        g["candidates"] = [
            c for c in g["candidates"]
            if c.get("identity_key") not in resolved_iks
        ]

    # 清理空组
    ambiguous_groups = [g for g in ambiguous_groups if len(g["candidates"]) >= 2]

    is_ambiguous = len(ambiguous_groups) > 0

    return {
        "groups": ambiguous_groups,
        "resolved_characters": resolved,
        "franchise_hints": franchise_hints,
        "is_ambiguous": is_ambiguous,
        "total_mentions": len(mention_groups),
        "ambiguous_count": len(ambiguous_groups),
    }


def _generate_group_id() -> str:
    """生成唯一的歧义组 ID。"""
    return f"DG-{hashlib.sha256(str(time.time()).encode()).hexdigest()[:8]}"


def create_pending_disambiguation_json(
    analysis: dict[str, Any],
    original_request: str,
    constraints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """创建 pending_disambiguation JSON 结构。

    格式：
    {
        "request_id": "...",
        "original_request": "...",
        "resolved_characters": [...],
        "groups": [...],
        "constraints": {},
        "created_at": timestamp,
        "version": 1
    }
    """
    import json as _json

    return {
        "request_id": hashlib.sha256(f"{original_request}:{time.time()}".encode()).hexdigest()[:16],
        "original_request": original_request,
        "resolved_characters": [
            {
                "identity_key": c.get("identity_key", ""),
                "character_key": c.get("character_key", ""),
                "name_zh": c.get("name_zh", ""),
                "name_en": c.get("name_en", ""),
                "franchise": c.get("franchise", ""),
            }
            for c in analysis.get("resolved_characters", [])
        ],
        "groups": [
            {
                "group_id": g.get("group_id", ""),
                "mention": g.get("mention", ""),
                "span": g.get("span", [0, 0]),
                "candidates": [
                    {
                        "identity_key": c.get("identity_key", ""),
                        "character_key": c.get("character_key", ""),
                        "name_zh": c.get("name_zh", ""),
                        "name_en": c.get("name_en", ""),
                        "franchise": c.get("franchise", ""),
                        "franchise_en": c.get("franchise_en", ""),
                        "franchise_zh": c.get("franchise_zh", ""),
                    }
                    for c in g.get("candidates", [])
                ],
                "status": "pending",
            }
            for g in analysis.get("groups", [])
        ],
        "constraints": constraints or {},
        "created_at": int(time.time()),
        "version": 1,
    }


def is_new_generation_request(message: str) -> bool:
    """检测用户是否发了新的完整生成请求（应 supersede 旧 pending）。"""
    raw = str(message or "").strip()
    if not raw:
        return False

    # 生成请求关键词
    gen_keywords = ("生成", "帮我画", "给我画", "画一个", "画一张", "来一个", "来一张",
                    "重新生成", "给我生成", "帮我生成", "画图", "出图", "开始生成")
    for kw in gen_keywords:
        if raw.startswith(kw) and len(raw) > len(kw) + 1:
            return True

    return False


def is_disambiguation_choice(message: str, candidates: list[dict[str, Any]]) -> bool:
    """检测用户回复是否为有效的歧义选择。"""
    raw = str(message or "").strip().lower()
    if not raw or not candidates:
        return False

    # 序号
    ordinal_map = {
        "一": 0, "二": 1, "三": 2, "四": 3, "五": 4,
        "1": 0, "2": 1, "3": 2, "4": 3, "5": 4,
        "first": 0, "second": 1, "third": 2, "fourth": 3, "fifth": 4,
        "第一个": 0, "第二个": 1, "第三个": 2, "选第一个": 0, "选第二个": 1,
    }
    for word, idx in ordinal_map.items():
        if word in raw and 0 <= idx < len(candidates):
            return True

    # 角色名/作品名匹配
    for candidate in candidates:
        name_zh = str(candidate.get("name_zh") or "").strip().lower()
        name_en = str(candidate.get("name_en") or "").strip().lower()
        franchise = str(candidate.get("franchise") or "").strip().lower()
        if name_zh and name_zh in raw:
            return True
        if name_en and name_en in raw:
            return True
        if franchise and franchise in raw:
            return True

    return False


def is_scene_supplement(message: str) -> bool:
    """检测用户回复是否为场景/服装等补充描述（不是新请求也不是选择）。"""
    raw = str(message or "").strip()
    if not raw:
        return False
    if is_new_generation_request(raw):
        return False

    supplement_keywords = (
        "场景", "穿", "服装", "背景", "卧室", "教室", "客厅", "户外",
        "室内", "夜景", "白天", "傍晚", "校服", "制服", "便服", "泳装",
        "姿势", "表情", "动作", "构图", "氛围", "光线",
    )
    for kw in supplement_keywords:
        if kw in raw:
            return True
    return False


def resolve_group(
    pending: dict[str, Any],
    group_id: str,
    selected_identity_key: str,
) -> dict[str, Any]:
    """解决指定歧义组。"""
    updated_groups = []
    for g in pending.get("groups", []):
        if g.get("group_id") == group_id:
            g["status"] = "resolved"
            g["selected_identity_key"] = selected_identity_key
        updated_groups.append(g)
    pending["groups"] = updated_groups
    return pending


def all_groups_resolved(pending: dict[str, Any]) -> bool:
    """检查是否所有歧义组都已解决。"""
    groups = pending.get("groups", [])
    if not groups:
        return True
    return all(g.get("status") == "resolved" for g in groups)


def pending_to_public(pending: dict[str, Any]) -> list[dict[str, Any]]:
    """将 pending_disambiguation JSON 转为前端可用的白名单数据。"""
    result = []
    for g in pending.get("groups", []):
        if g.get("status") == "resolved":
            continue
        public_candidates = []
        for c in g.get("candidates", []):
            # 作品显示优先级：franchise_zh > franchise_en（含正式映射） > franchise（已验证非人物名）
            franchise_display = _candidate_franchise_display(c)
            public_candidates.append({
                "character_key": c.get("character_key", ""),
                "display_name": c.get("name_zh", "") or c.get("name_en", ""),
                "display_name_en": c.get("name_en", ""),
                "franchise": franchise_display,
            })
        result.append({
            "group_id": g.get("group_id", ""),
            "mention": g.get("mention", ""),
            "candidates": public_candidates,
            "status": g.get("status", "pending"),
        })
    return result


def _candidate_franchise_display(candidate: dict[str, Any]) -> str:
    """返回候选人物的作品显示名称。

    优先级：
    1. franchise_zh（中文作品名）
    2. franchise_en（英文作品名）
    3. franchise（已验证非人物名、非 identity tag）
    4. category_zh（仅当不是"其他动漫"等通用分类）
    5. category_en

    不得把人物 identity tag / name_en / canonical tag 误当作作品显示。
    """
    generic_values = {"其他动漫", "其他", "anime", "other anime", "其他游戏", "other", "vocaloid"}

    display_overrides = {
        "kanojo okarishimasu": "租借女友",
        "rent-a-girlfriend": "租借女友",
        "rent a girlfriend": "租借女友",
        "mahou shoujo madoka magica": "魔法少女小圆",
        "puella magi madoka magica": "魔法少女小圆",
    }

    # 1. explicit franchise extracted by the index
    franchise = str(candidate.get("franchise") or "").strip()
    if franchise and franchise.lower() not in generic_values:
        name_en = str(candidate.get("name_en") or "").strip()
        name_zh = str(candidate.get("name_zh") or "").strip()
        if (_normalize_candidate_key(franchise) != _normalize_candidate_key(name_en)
                and _normalize_candidate_key(franchise) != _normalize_candidate_key(name_zh)):
            return display_overrides.get(franchise.lower(), franchise)

    # 2. franchise_zh
    fz_zh = str(candidate.get("franchise_zh") or "").strip()
    if fz_zh and fz_zh.lower() not in generic_values:
        return fz_zh

    # 3. franchise_en
    fz_en = str(candidate.get("franchise_en") or "").strip()
    if fz_en and fz_en.lower() not in generic_values:
        return display_overrides.get(fz_en.lower(), fz_en)

    # 4. category_zh
    cat_zh = str(candidate.get("category_zh") or "").strip()
    if cat_zh and cat_zh.lower() not in generic_values:
        return cat_zh

    # 5. category_en
    cat_en = str(candidate.get("category_en") or "").strip()
    return "" if cat_en.lower() in generic_values else cat_en


def _normalize_candidate_key(value: str) -> str:
    """将候选名称规范化为可比较的 key。"""
    import re as _re
    text = str(value or "").lower().strip()
    text = _re.sub(r"[_\\/\-()（）：:,，.。]+", " ", text)
    return " ".join(text.split())


# ── 测试/审计 ──

def run_full_audit() -> dict[str, Any]:
    """运行全库审计，返回审计报告。"""
    stats = get_index_stats()
    duplicates = get_duplicate_groups()
    substrings = get_zh_substring_groups()
    all_ids = get_all_identities()

    # 缺失作品信息
    missing_franchise = [
        rid["identity_key"]
        for rid in all_ids
        if not rid.get("franchise_zh") and not rid.get("franchise_en")
    ]

    # 作品可消歧的组
    franchisable_groups = 0
    for mention, identities in duplicates.items():
        # 检查这些 identity 是否有不同作品
        franchises = set()
        for ik in identities:
            rid = get_identity(ik)
            if rid:
                f = rid.get("franchise_en", "") or rid.get("franchise_zh", "")
                if f:
                    franchises.add(f)
        if len(franchises) >= 2:
            franchisable_groups += 1

    # 自检：每个人物的完整名应能匹配自身
    self_check_fails = []
    for rid in all_ids:
        ik = rid["identity_key"]
        name_zh = rid.get("name_zh", "")
        name_en = rid.get("name_en", "")

        # 测试 name_zh
        if name_zh:
            result = find_characters_by_mention_v2(name_zh)
            found_ik = False
            for g in result:
                for c in g["candidates"]:
                    if c.get("identity_key") == ik:
                        found_ik = True
                        break
            if not found_ik:
                self_check_fails.append({
                    "identity_key": ik,
                    "name_zh": name_zh,
                    "test": "name_zh_self_match",
                })

    return {
        **stats,
        "missing_franchise_count": len(missing_franchise),
        "missing_franchise_examples": missing_franchise[:10],
        "franchisable_duplicate_groups": franchisable_groups,
        "self_check_fails": len(self_check_fails),
        "self_check_fail_examples": self_check_fails[:10],
    }
