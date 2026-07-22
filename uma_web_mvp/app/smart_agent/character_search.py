from __future__ import annotations

import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Any

CHARACTER_TAGS_PATH = Path(__file__).resolve().parents[1] / "static" / "character-tags.json"
CHARACTER_REGISTRY_PATH = Path(__file__).resolve().parents[1] / "data" / "character_registry.json"
UMAMUSUME_ZH_NAMES = {
    "silence suzuka": "无声铃鹿",
    "tokai teio": "东海帝王",
    "mejiro mcqueen": "目白麦昆",
    "rice shower": "米浴",
    "special week": "特别周",
    "kitasan black": "北部玄驹",
    "satono diamond": "里见光钻",
    "oguri cap": "小栗帽",
    "daiwa scarlet": "大和赤骥",
    "vodka": "伏特加",
    "gold ship": "黄金船",
    "mihono bourbon": "美浦波旁",
    "manhattan cafe": "曼城茶座",
    "agnes tachyon": "爱丽速子",
    "nice nature": "优秀素质",
    "twin turbo": "双涡轮",
    "smart falcon": "醒目飞鹰",
    "curren chan": "真机伶",
    "mayano top gun": "摩耶重炮",
    "copano rickey": "小林历奇",
    "daiichi ruby": "第一红宝石",
    "still in love": "爱如往昔",
    "venus paques": "芙卓",
    "vivlos": "强击",
    "neo universe": "新宇宙",
    "gold city": "黄金城市",
    "maruzensky": "丸善斯基",
    "gentildonna": "贵妇人",
    "verxina": "极峰",
    "symboli rudolf": "鲁道夫象征",
    "grass wonder": "草上飞",
    "mejiro bright": "目白光明",
    "daring tact": "谋勇兼备",
    "daitaku helios": "大拓太阳神",
    "mejiro ramonu": "目白高峰",
    "super creek": "超级小海湾",
    "agnes digital": "爱丽数码",
    "fine motion": "美妙姿势",
    "meisho doto": "名将怒涛",
    "hokko tarumae": "北幸樽前",
    "tamamo cross": "玉藻十字",
    "nishino flower": "西野花",
    "seiun sky": "青云天空",
    "cheval grand": "高尚骏逸",
    "eishin flash": "荣进闪耀",
}

UMAMUSUME_ZH_ALIASES = {
    "tokai teio": ["东海帝王", "帝王"],
    "still in love": ["爱如往昔", "至爱"],
    "venus paques": ["芙卓", "维纳斯帕克斯", "维纳斯帕克"],
    "smart falcon": ["醒目飞鹰", "飞鹰"],
    "vivlos": ["强击", "维布洛斯", "ヴィブロス"],
    "gold city": ["黄金城"],
}

UMAMUSUME_IDENTITY_TAGS = {
    "umamusume",
    "horse ears",
    "horse tail",
    "race uniform",
    "uma musume",
    "umamusume_(series)",
}

UMAMUSUME_CATEGORY_IDENTITY_HINT = {
    "umamusume",
    "(umamusume)",
}

GENERIC_PERSON_TAGS = {
    "1girl",
    "1boy",
    "solo",
    "multiple girls",
    "multiple boys",
}

GENERIC_MATCH_TAGS = {
    "umamusume",
    "horse ears",
    "horse tail",
    "1girl",
    "1boy",
    "solo",
    "black hair",
    "white hair",
    "silver hair",
    "blonde hair",
    "brown hair",
    "red hair",
    "blue hair",
    "pink hair",
    "purple hair",
    "green hair",
    "long hair",
    "short hair",
    "medium hair",
    "very long hair",
    "bob cut",
    "ponytail",
    "twintails",
    "braid",
    "bangs",
    "side ponytail",
    "blue eyes",
    "red eyes",
    "green eyes",
    "purple eyes",
    "golden eyes",
    "brown eyes",
    "heterochromia",
    "pale skin",
    "tan skin",
    "dark skin",
    "fair skin",
    "petite",
    "tall",
    "short",
    "slender",
    "curvy",
    "muscular",
    "large breasts",
    "small breasts",
    "medium breasts",
    "wide hips",
    "thick thighs",
    "mole",
    "freckles",
    "fang",
    "pointed ears",
    "makeup",
}

# ── Artist / style / non-character term detection ──────────────────────
# These terms must NOT trigger character matching.  They include artist
# tags, style descriptors, technical terms, and other non-character content.

_ARTIST_LIKE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^artist[:\s]", re.IGNORECASE),        # artist:name, artist: xxx
    re.compile(r"^by\s+[a-z]", re.IGNORECASE),          # by artist_name
    re.compile(r"^style[:\s]", re.IGNORECASE),          # style:anime, style: xxx
    re.compile(r"^source[:\s]", re.IGNORECASE),         # source:xxx
    re.compile(r"^rating[:\s]", re.IGNORECASE),         # rating:safe
    re.compile(r"^meta[:\s]", re.IGNORECASE),           # meta:xxx
    re.compile(r"^copyright[:\s]", re.IGNORECASE),      # copyright:xxx
    re.compile(r"^character[:\s]", re.IGNORECASE),      # character:xxx (booru tag)
    re.compile(r"^general[:\s]", re.IGNORECASE),        # general:xxx (booru tag)
    re.compile(r"\.(ckpt|safetensors|pt|bin)$", re.IGNORECASE),  # model files
)

# Known non-character tag prefixes (Booru-style)
_ARTIST_TAG_PREFIXES = (
    "artist:", "artist_", "by ", "style:", "style_",
    "source:", "rating:", "meta:", "copyright:",
    "character:", "general:",
)


def _is_artist_like_term(term: str) -> bool:
    """Return True if *term* looks like an artist, style, or non-character tag.

    This prevents artist names / style words from being treated as character
    candidates during matching.
    """
    clean = str(term or "").strip()
    if not clean:
        return False
    lower = clean.lower()
    # 1. Booru-style prefixes
    for prefix in _ARTIST_TAG_PREFIXES:
        if lower.startswith(prefix):
            return True
    # 2. Regex patterns
    for pat in _ARTIST_LIKE_PATTERNS:
        if pat.search(clean):
            return True
    # 3. Single-character English terms (likely noise, not character names).
    if " " not in clean and len(clean) <= 1 and clean.isascii() and clean.isalpha():
        return True
    return False


# ── Franchise / non-identity tag detection ──────────────────────────────
# Tags that belong to a franchise/series (shared by multiple characters)
# must NOT independently trigger character matching. They can only be used
# for verification/disambiguation AFTER a character name is already matched.

_FRANCHISE_TAGS_CACHE: set[str] | None = None


def _build_franchise_tags() -> set[str]:
    """Auto-detect franchise/series tags from the character database.

    A tag is a franchise tag if:
    - It matches a category_en or category_zh value (normalized), OR
    - It appears in >= 3 characters across the entire database
      (after excluding character-specific identity tags).

    These tags are shared across multiple characters within the same
    franchise and should NOT independently trigger character matching.
    """
    from collections import Counter

    characters = load_characters()
    franchise: set[str] = set()

    # 1. All category names are franchise tags
    for c in characters:
        for field in ("category_en", "category_zh"):
            val = str(c.get(field) or "").strip()
            if val:
                franchise.add(_canonical_name_key(val))
                franchise.add(_canonical_name_key(val).replace(" ", ""))
                # Also add the raw lowercased form for CJK
                franchise.add(val.lower())

    # 2. Tags appearing in >= 3 characters (excluding char's own name)
    tag_counter: Counter = Counter()
    for c in characters:
        char_name = _canonical_name_key(
            str(c.get("name_en") or c.get("key") or "")
        )
        for tag in _split_prompt_tags(str(c.get("tags") or "")):
            tag_key = _canonical_name_key(tag)
            if not tag_key:
                continue
            # Skip if tag looks like the character's own identity
            if char_name and tag_key == char_name:
                continue
            if char_name and tag_key == char_name.replace(" ", ""):
                continue
            # Skip already-known generic tags
            if tag_key in {_canonical_name_key(g) for g in GENERIC_MATCH_TAGS}:
                continue
            tag_counter[tag_key] += 1

    for tag_key, count in tag_counter.items():
        if count >= 3:
            franchise.add(tag_key)
            franchise.add(tag_key.replace(" ", ""))

    return franchise


def _get_franchise_tags() -> set[str]:
    """Return the cached set of franchise/series tags."""
    global _FRANCHISE_TAGS_CACHE
    if _FRANCHISE_TAGS_CACHE is None:
        _FRANCHISE_TAGS_CACHE = _build_franchise_tags()
    return _FRANCHISE_TAGS_CACHE


def load_characters() -> list[dict[str, str]]:
    try:
        raw = json.loads(CHARACTER_TAGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    result: list[dict[str, str]] = []
    categories = raw if isinstance(raw, list) else []
    for category in categories:
        if not isinstance(category, dict):
            continue
        category_zh = str(category.get("category_zh") or "")
        category_en = str(category.get("category_en") or "")
        for item in category.get("items") or []:
            if not isinstance(item, dict):
                continue
            tags = str(item.get("tags") or "").strip()
            if not tags:
                continue
            result.append(_standardize_character({
                "key": _canonical_name_key(str(item.get("name_en") or item.get("name_zh") or "")).replace(" ", "_"),
                "name_zh": str(item.get("name_zh") or ""),
                "name_en": str(item.get("name_en") or ""),
                "aliases": ",".join(str(x) for x in (item.get("aliases") or [])),
                "category_zh": category_zh,
                "category_en": category_en,
                "tags": tags,
            }))
    result.extend(_load_desktop_umamusume())
    result.extend(_load_character_registry())
    return _dedupe_characters(result)


def _load_character_registry() -> list[dict[str, str]]:
    if not CHARACTER_REGISTRY_PATH.exists():
        return []
    try:
        raw = json.loads(CHARACTER_REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items = raw if isinstance(raw, list) else raw.get("items", [])
    result: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        base_tags = item.get("base_tags")
        if isinstance(base_tags, list):
            base_tag_text = ", ".join(str(x).strip() for x in base_tags if str(x).strip())
        else:
            base_tag_text = str(base_tags or "").strip()
        tags = str(item.get("tags") or base_tag_text).strip()
        if not tags:
            continue
        aliases = item.get("aliases") or []
        if isinstance(aliases, str):
            alias_text = aliases
        else:
            alias_text = ",".join(str(x) for x in aliases)
        preferred_workflows = item.get("preferred_workflow_keys") or []
        if isinstance(preferred_workflows, str):
            preferred_workflows_text = preferred_workflows
        else:
            preferred_workflows_text = ",".join(str(x) for x in preferred_workflows)
        preferred_loras = item.get("preferred_lora_keys") or []
        if isinstance(preferred_loras, str):
            preferred_loras_text = preferred_loras
        else:
            preferred_loras_text = ",".join(str(x) for x in preferred_loras)
        result.append(_standardize_character(
            {
                "key": str(item.get("key") or ""),
                "name_zh": str(item.get("name_zh") or ""),
                "name_en": str(item.get("name_en") or ""),
                "aliases": alias_text,
                "category_zh": str(item.get("category_zh") or ""),
                "category_en": str(item.get("category_en") or ""),
                "tags": tags,
                "recommended_lora_key": str(item.get("recommended_lora_key") or ""),
                "recommended_workflow_key": str(item.get("recommended_workflow_key") or ""),
                "preferred_workflow_keys": preferred_workflows_text,
                "preferred_lora_keys": preferred_loras_text,
                "lora_weight": str(item.get("lora_weight") or ""),
            }
        ))
    return result


def _load_desktop_umamusume() -> list[dict[str, str]]:
    path = Path(os.path.expanduser("~")) / "Desktop" / "umamusume.txt"
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except UnicodeDecodeError:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    items = []
    for line in lines:
        text = " ".join(line.strip().split())
        if not text or text.startswith("#"):
            continue
        name_en = text.split(",", 1)[0].strip()
        key = _canonical_name_key(name_en)
        aliases = [name_en, key, f"{key} (umamusume)", *UMAMUSUME_ZH_ALIASES.get(key, [])]
        items.append(_standardize_character({
            "key": key.replace(" ", "_"),
            "name_zh": UMAMUSUME_ZH_NAMES.get(key, name_en),
            "name_en": name_en,
            "aliases": ",".join(aliases),
            "category_zh": "赛马娘",
            "category_en": "Umamusume",
            "tags": _umamusume_identity_tags(key),
        }))
    return items


def _standardize_character(item: dict[str, str]) -> dict[str, str]:
    category_en = str(item.get("category_en") or "")
    category_zh = str(item.get("category_zh") or "")
    if _canonical_name_key(category_en) == "umamusume" or category_zh == "赛马娘":
        item["category_zh"] = "赛马娘"
        item["category_en"] = "Umamusume"
        name_key = _canonical_name_key(str(item.get("name_en") or item.get("key") or ""))
        if name_key:
            existing_name_zh = str(item.get("name_zh") or "").strip()
            item["key"] = name_key.replace(" ", "_")
            item["name_zh"] = existing_name_zh or UMAMUSUME_ZH_NAMES.get(name_key, str(item.get("name_en") or ""))
            aliases = _split_aliases(item.get("aliases"))
            aliases.extend(UMAMUSUME_ZH_ALIASES.get(name_key, []))
            aliases.extend([name_key, name_key.replace(" ", "_"), f"{name_key} (umamusume)"])
            if name_key == "tokai teio":
                aliases.append("tokai teiou")
            item["aliases"] = ",".join(_dedupe_text(aliases))
            item["tags"] = _umamusume_identity_tags(name_key)
    return item


def _split_aliases(value: Any) -> list[str]:
    if isinstance(value, list):
        parts = value
    else:
        parts = re.split(r"[,,\n]+", str(value or ""))
    return [str(part).strip() for part in parts if str(part).strip()]


def _dedupe_text(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        key = item.strip().lower()
        if item and key not in seen:
            result.append(item)
            seen.add(key)
    return result


def _umamusume_identity_tags(name_key: str) -> str:
    return f"umamusume, {name_key}, {name_key} (umamusume), horse ears, horse tail"


def _canonical_name_key(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name)
    ascii_name = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return " ".join(
        ascii_name.lower()
        .replace("_", " ")
        .replace("-", " ")
        .replace("(", " ")
        .replace(")", " ")
        .split()
    )


def _dedupe_characters(items: list[dict[str, str]]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    strong_seen: dict[str, int] = {}
    weak_seen: dict[tuple[str, str], int] = {}
    for item in items:
        strong_keys = _character_strong_identity_keys(item)
        weak_keys = _character_weak_identity_keys(item)
        duplicate_index = None
        for key in strong_keys:
            if key in strong_seen:
                duplicate_index = strong_seen[key]
                break
        if duplicate_index is None:
            for key in weak_keys:
                if key in weak_seen:
                    duplicate_index = weak_seen[key]
                    break
        if duplicate_index is not None:
            _merge_character_metadata(result[duplicate_index], item)
            continue
        index = len(result)
        result.append(item)
        for key in strong_keys:
            strong_seen.setdefault(key, index)
        for key in weak_keys:
            weak_seen.setdefault(key, index)
    return result


def _character_strong_identity_keys(item: dict[str, str]) -> set[str]:
    keys: set[str] = set()
    for value in (item.get("key", ""), item.get("name_en", "")):
        key = _canonical_name_key(value).replace(" ", "")
        if key:
            keys.add(key)
    for tag in _split_prompt_tags(str(item.get("tags") or "")):
        tag_key = _identity_tag_key(tag)
        if tag_key:
            keys.add(tag_key)
            break
    return keys


def _character_weak_identity_keys(item: dict[str, str]) -> set[tuple[str, str]]:
    name_zh = _normalize_cjk_text(str(item.get("name_zh") or ""))
    if not name_zh:
        return set()
    category = _canonical_name_key(str(item.get("category_en") or item.get("category_zh") or "")).replace(" ", "")
    return {(category, name_zh)}


def _identity_tag_key(tag: str) -> str:
    without_suffix = re.sub(r"\([^)]*\)", "", str(tag or "")).strip()
    key = _canonical_name_key(without_suffix).replace(" ", "")
    generic = {item.replace(" ", "") for item in GENERIC_MATCH_TAGS}
    if not key or key in generic:
        return ""
    return key


def _extract_identity_tags(character: dict[str, str]) -> list[str]:
    """Extract canonical character identity tags from a character's tags.

    Returns tags that identify the CHARACTER (not franchise, appearance,
    generic, style, etc.).  These are used for matching user input like
    Booru identity tags (e.g. "inoue takina", "nishikigi chisato").
    """
    tags_text = str(character.get("tags") or "")
    franchise = _get_franchise_tags()
    generic_keys = {_canonical_name_key(g).replace(" ", "") for g in GENERIC_MATCH_TAGS}
    identity: list[str] = []
    for tag in _split_prompt_tags(tags_text):
        clean = tag.strip()
        if not clean:
            continue
        tag_key = _canonical_name_key(clean).replace(" ", "")
        if not tag_key or tag_key in generic_keys:
            continue
        # Skip franchise/series tags (lycoris recoil, umamusume, etc.)
        canon = _canonical_name_key(clean)
        if tag_key in franchise or canon in franchise:
            continue
        # Skip appearance tags
        compact = tag_key.replace("_", "")
        if compact in _APPEARANCE_TAG_COMPACT:
            continue
        identity.append(clean)
    return identity


# Appearance tag canonical compact keys for filtering (same as character_preferences.APPEARANCE_TAG_KEYS)
_APPEARANCE_TAG_COMPACT: set[str] = {
    "blackhair", "whitehair", "silverhair", "blondehair", "brownhair",
    "redhair", "bluehair", "pinkhair", "purplehair", "greenhair",
    "aquahair", "greyhair", "grayhair", "orangehair",
    "longhair", "shorthair", "mediumhair", "verylonghair", "bobcut",
    "ponytail", "twintails", "braid", "braidedhair", "bangs",
    "sideponytail",
    "blueeyes", "redeyes", "greeneyes", "purpleeyes", "goldeneyes",
    "yelloweyes", "browneyes", "blackeyes", "pinkeyes", "aquayes",
    "heterochromia",
    "paleskin", "tanskin", "darkskin", "fairskin", "whiteskin",
    "petite", "tall", "short", "slender", "curvy", "muscular",
    "largebreasts", "smallbreasts", "mediumbreasts", "widehips",
    "thickthighs", "mole", "freckles", "fang", "pointedears", "makeup",
}


def _match_identity_tag_wordset(identity_tag: str, input_text: str) -> bool:
    """Check if input contains all meaningful words of an identity tag.

    Handles word-order differences like:
      "inoue takina" matches "Takina Inoue"
      "nishikigi chisato" matches "Chisato Nishikigi"

    Requires all words >= 3 chars to be present as whole words.
    """
    tag_lower = _canonical_name_key(identity_tag)
    tag_words = [w for w in tag_lower.split() if len(w) >= 3]
    if len(tag_words) < 2:
        return False
    hay = _canonical_name_key(input_text)
    return all(
        re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", hay)
        for w in tag_words
    )


def _merge_character_metadata(target: dict[str, str], incoming: dict[str, str]) -> None:
    aliases = _dedupe_text([*_split_aliases(target.get("aliases")), *_split_aliases(incoming.get("aliases"))])
    if aliases:
        target["aliases"] = ",".join(aliases)
    for field in (
        "recommended_lora_key",
        "recommended_workflow_key",
        "preferred_workflow_keys",
        "preferred_lora_keys",
        "lora_weight",
        "translated_character_name",
        "original_character_name",
    ):
        if incoming.get(field) and not target.get(field):
            target[field] = incoming[field]


def _strip_artist_spans(text: str) -> str:
    """Remove artist-like spans from input text before character matching.

    Patterns like ``artist:xxx``, ``by xxx``, ``style:xxx`` are stripped so
    that the tokens inside them cannot trigger character matching.
    """
    clean = str(text or "")
    # Strip Booru-style tagged spans: artist:xxx, style:xxx, source:xxx, etc.
    clean = re.sub(r"\b(?:artist|style|source|rating|meta|copyright|character|general)\s*:[^,;\n]+", " ", clean, flags=re.IGNORECASE)
    # Strip "by <name>" patterns (artist credits)
    clean = re.sub(r"\bby\s+[A-Za-z][A-Za-z0-9_-]+(?:\s+[A-Za-z][A-Za-z0-9_-]+)*", " ", clean, flags=re.IGNORECASE)
    # Strip model file references
    clean = re.sub(r"\b[\w.-]+\.(?:ckpt|safetensors|pt|bin)\b", " ", clean, flags=re.IGNORECASE)
    return clean


def find_characters(text: str, *, limit: int = 8) -> list[dict[str, str]]:
    """从输入文本中匹配人物。

    核心规则:
    1. 长匹配优先 - 完整人物名匹配后,其子串不得再匹配其他人物;
    2. 同一人物只返回一次(按 key 去重);
    3. 按 (score, best_term_length) 降序排列。
    4. 当正向匹配无结果时,自动使用反向 CJK 子串匹配('麻美' → '七海麻美')。
    """
    # Strip artist-like spans from input BEFORE matching
    raw_text = _strip_artist_spans(text or "")
    haystack_zh = _normalize_cjk_text(raw_text)
    haystack_en = _canonical_name_key(raw_text)

    # ── 第一遍:收集所有可能的 (character, term, score, term_len, match_position) ──
    # match_position: 在 haystack_en 中的字符索引,用于 span 占位
    candidate_matches: list[tuple[int, int, int, int, dict[str, str]]] = []  # (score, term_len, match_pos, char_idx, item)
    characters = load_characters()
    for char_idx, item in enumerate(characters):
        # name_zh, name_en, aliases, and canonical identity tags can create
        # character candidates.  Only character-specific identity tags
        # (e.g. "inoue takina") are used, NOT franchise or generic tags.
        terms = [item.get("name_zh", ""), item.get("name_en", "")]
        terms.extend(_split_aliases(item.get("aliases")))
        # Add canonical identity tags (Booru-style character identity tags)
        identity_tags = _extract_identity_tags(item)
        for id_tag in identity_tags:
            if id_tag not in terms:
                terms.append(id_tag)
        # Filter out artist-like terms
        terms = [
            t for t in terms
            if t and not _is_artist_like_term(t)
        ]
        for term in terms:
            clean = str(term or "").strip()
            if not clean:
                continue
            is_full = clean in {item.get("name_zh", ""), item.get("name_en", "")}
            has_cjk = any("\u3400" <= ch <= "\u9fff" for ch in clean)
            match_pos = -1
            if has_cjk:
                clean_zh = _normalize_cjk_text(clean)
                if clean_zh:
                    # Single CJK char: boundary-aware to avoid "琴" matching "御坂美琴"
                    if len(clean_zh) == 1:
                        idx = haystack_zh.find(clean_zh)
                        while idx >= 0:
                            before_ok = (idx == 0) or not ("\u3400" <= haystack_zh[idx - 1] <= "\u9fff")
                            after_pos = idx + len(clean_zh)
                            after_ok = (after_pos >= len(haystack_zh)) or not ("\u3400" <= haystack_zh[after_pos] <= "\u9fff")
                            if before_ok and after_ok:
                                match_pos = idx
                                break
                            idx = haystack_zh.find(clean_zh, idx + 1)
                    elif clean_zh in haystack_zh:
                        match_pos = haystack_zh.find(clean_zh)
            else:
                normalized = _canonical_name_key(clean)
                if normalized:
                    m = re.search(rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])", haystack_en)
                    if m:
                        match_pos = m.start()
            if match_pos >= 0:
                term_len = len(_normalize_cjk_text(clean) or _canonical_name_key(clean))
                # name_zh/name_en = score 5, aliases/identity tags = score 3, partial = score 2
                is_identity_tag = clean in identity_tags
                score = 5 if is_full else (3 if is_identity_tag else 2)
                candidate_matches.append((score, term_len, match_pos, char_idx, item))
            else:
                # Word-set fallback: identity tags where word order differs
                # e.g. "inoue takina" (Booru) vs "Takina Inoue" (name_en)
                is_identity_tag = clean in identity_tags
                if is_identity_tag and _match_identity_tag_wordset(clean, raw_text):
                    tag_key = _canonical_name_key(clean)
                    term_len = len(tag_key)
                    candidate_matches.append((3, term_len, 0, char_idx, item))

    # ── 排序:score 降序 → term_len 降序 ──
    candidate_matches.sort(key=lambda t: (-t[0], -t[1]))

    # ── 第二遍:按优先级分配,长匹配占位后短匹配跳过 ──
    result: list[dict[str, str]] = []
    seen_keys: set[str] = set()
    occupied_spans: list[tuple[int, int, str]] = []  # (start, end, term_canonical)

    for score, term_len, match_pos, char_idx, item in candidate_matches:
        item_key = str(item.get("key") or "") or str(item.get("name_en") or "")
        if item_key in seen_keys:
            continue

        # 计算该 term 在 haystack_en 中的覆盖范围
        term = str(item.get("name_en") or "")
        for t in [item.get("name_zh", ""), item.get("name_en", "")] + _split_aliases(item.get("aliases")):
            tc = str(t or "").strip()
            if tc:
                has_cjk_t = any("\u3400" <= ch <= "\u9fff" for ch in tc)
                if has_cjk_t:
                    tc_zh = _normalize_cjk_text(tc)
                    if tc_zh and tc_zh in haystack_zh and haystack_zh.find(tc_zh) == match_pos:
                        term = tc
                        break
                else:
                    tc_norm = _canonical_name_key(tc)
                    if tc_norm:
                        m = re.search(rf"(?<![a-z0-9]){re.escape(tc_norm)}(?![a-z0-9])", haystack_en)
                        if m and m.start() == match_pos:
                            term = tc
                            break

        has_cjk = any("\u3400" <= ch <= "\u9fff" for ch in term)
        if has_cjk:
            norm = _normalize_cjk_text(term)
            start = match_pos
            end = match_pos + len(norm)
        else:
            norm = _canonical_name_key(term)
            start = match_pos
            end = match_pos + len(norm)

        # 检查是否完全被已占用 span 覆盖
        # 特殊情况:允许多个角色共享同一输入子串作为歧义候选:
        # 1. 新匹配的 term 更短且同一起始位置(如 '爱丽丝' vs '天童爱丽丝')
        # 2. 新匹配的 term 完全相同(不同角色同名,如两个 '爱丽丝' 来自不同作品)
        # 这些情况下需要返回所有候选供用户确认。
        fully_occupied = False
        for occ_start, occ_end, occ_term in occupied_spans:
            if occ_start <= start and end <= occ_end:
                # 同一起始位置 + 新 term 更短或相同 → 歧义候选,不跳过
                if start == occ_start and len(norm) <= len(occ_term):
                    continue
                fully_occupied = True
                break
        if fully_occupied:
            continue

        # 记录占用 span
        occupied_spans.append((start, end, norm))
        seen_keys.add(item_key)

        tags = _normalize_tags(item["tags"], item.get("category_en", ""))
        clean_item = dict(item)
        clean_item["tags"] = tags
        result.append(clean_item)

        if len(result) >= limit:
            break

    # ── 反向 CJK 子串匹配:从用户输入中提取 CJK 片段,匹配角色名 ──
    # 例如用户输入 '生成几个麻美的图',提取 2-4 字 CJK 片段,
    # 然后发现 '麻美' 是 '七海麻美' 和 '巴麻美' 的子串。
    # 注意:始终执行(不仅限于 result 为空时),以便歧义候选能被完整收集。
    if haystack_zh and len(haystack_zh) >= 2:
        has_cjk_input = any("\u3400" <= ch <= "\u9fff" for ch in haystack_zh)
        if has_cjk_input:
            # 提取所有 CJK 连续片段,以及滑动窗口 2-4 字子串
            all_cjk_parts: set[str] = set()
            # 完整 CJK runs
            for run in re.findall(r"[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff]{2,}", raw_text):
                norm_run = _normalize_cjk_text(run)
                if norm_run and len(norm_run) >= 2:
                    all_cjk_parts.add(norm_run)
            # 滑动窗口提取 2-4 字子串（用于长句中的短名字匹配）
            # 仅当输入长度 >= 4 时才使用滑动窗口，避免纯名字输入（如"天童爱丽丝"）
            # 产生短子串误匹配（如"爱丽"匹配到爱丽速子）
            clean_zh = _normalize_cjk_text(raw_text)
            if clean_zh and len(clean_zh) >= 4:
                # Extract CJK-only runs for sliding window (ASCII substrings from
                # alphanumeric input like "yukikazeazurlane" must NOT match character
                # names like "MEIKO" via substring "ik").
                cjk_only = ''.join(ch for ch in clean_zh if '㐀' <= ch <= '鿿')
                for win_size in (2, 3, 4):
                    for i in range(len(cjk_only) - win_size + 1):
                        sub = cjk_only[i:i + win_size]
                        if len(sub) >= 2:
                            all_cjk_parts.add(sub)
            for item in characters:
                item_key = str(item.get("key") or "") or str(item.get("name_en") or "")
                if item_key in seen_keys:
                    continue
                name_zh_norm = _normalize_cjk_text(str(item.get("name_zh") or ""))
                if not name_zh_norm:
                    continue
                # 检查是否有任何 CJK 片段是角色名或别名的子串
                matched = False
                name_zh_in_haystack = name_zh_norm in haystack_zh
                alias_zh_norms = [_normalize_cjk_text(a) for a in _split_aliases(item.get("aliases")) if any("\u3400" <= ch <= "\u9fff" for ch in str(a))]
                alias_in_haystack = any(a in haystack_zh for a in alias_zh_norms if a)
                for candidate in all_cjk_parts:
                    if len(candidate) < 2:
                        continue
                    if candidate not in name_zh_norm and not any(candidate in a for a in alias_zh_norms if a):
                        continue
                    # 子串匹配时，要求角色全名或别名全名出现在输入中，
                    # 或者子串是角色名/别名的前缀（从位置0开始）
                    if name_zh_in_haystack or alias_in_haystack:
                        matched = True
                        break
                    # 前缀匹配：子串是角色名的前缀且出现在输入开头附近
                    # 要求子串至少占角色名长度的一半，避免“黄金”匹配“黄金船”
                    if (name_zh_norm.startswith(candidate)
                            and len(candidate) * 2 >= len(name_zh_norm)
                            and haystack_zh.find(candidate) == 0):
                        matched = True
                        break
                    for a in alias_zh_norms:
                        if (a and a.startswith(candidate)
                                and len(candidate) * 2 >= len(a)
                                and haystack_zh.find(candidate) == 0):
                            matched = True
                            break
                    if matched:
                        break
                if matched:
                    seen_keys.add(item_key)
                    tags = _normalize_tags(item["tags"], item.get("category_en", ""))
                    clean_item = dict(item)
                    clean_item["tags"] = tags
                    result.append(clean_item)
                    if len(result) >= limit:
                        break

    return result


def find_character_after_translation(original_text: str, translated_text: str, *, limit: int = 8) -> list[dict[str, str]]:
    first = find_characters(original_text, limit=limit)
    if first:
        for item in first:
            item["character_tag_source"] = "character_registry"
            item["match_stage"] = "original"
        return first
    second = find_characters(translated_text, limit=limit)
    if second:
        for item in second:
            item["character_tag_source"] = "character_registry"
            item["match_stage"] = "translated"
        return second
    return []


def _normalize_cjk_text(value: str) -> str:
    return re.sub(r"[\s,,。.!!??::;;、_\\/\-()\[\]{}()【】「」『』·・]+", "", str(value or "").lower())


def _contains_character_term(haystack_zh: str, haystack_en: str, term: str) -> bool:
    has_cjk = any("\u3400" <= ch <= "\u9fff" for ch in term)
    if has_cjk:
        clean = _normalize_cjk_text(term)
        if not clean:
            return False
        # Single CJK character must not match inside a longer CJK sequence.
        # "琴" should NOT match "御坂美琴" (Misaka Mikoto), only standalone "琴" (Jean).
        if len(clean) == 1:
            idx = haystack_zh.find(clean)
            while idx >= 0:
                before_ok = (idx == 0) or not ("\u3400" <= haystack_zh[idx - 1] <= "\u9fff")
                after_pos = idx + len(clean)
                after_ok = (after_pos >= len(haystack_zh)) or not ("\u3400" <= haystack_zh[after_pos] <= "\u9fff")
                if before_ok and after_ok:
                    return True
                idx = haystack_zh.find(clean, idx + 1)
            return False
        return clean in haystack_zh
    normalized = _canonical_name_key(term)
    if not normalized:
        return False
    # Use ONLY boundary-aware regex matching.
    # The old "compact in hay_compact" fallback removed spaces and did plain
    # substring search, which caused short names like "ram" to match inside
    # longer English words (e.g. "framing", "dramatic", "program", "panorama")
    # after space removal.  Relying on the regex boundary check alone prevents
    # these false positives while still matching legitimate comma-separated tags
    # and standalone names.
    return re.search(rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])", haystack_en) is not None


def _split_prompt_tags(value: str) -> list[str]:
    return [part.strip() for part in str(value or "").replace(",", ",").split(",") if part.strip()]


def _normalize_tags(tags: str, category_en: str) -> str:
    parts = []
    seen = set()
    for raw in tags.split(","):
        tag = raw.strip()
        if not tag or tag.lower() in seen:
            continue
        seen.add(tag.lower())
        parts.append(tag)
    if category_en.lower() == "umamusume":
        for required in ("umamusume", "horse ears", "horse tail"):
            if required not in seen:
                parts.append(required)
                seen.add(required)
    return ", ".join(parts)


def extract_possible_character_names(text: str) -> str:
    """从中文文本中提取可能的人物名(用于翻译后再匹配)"""
    raw = str(text or "")
    # 移除常见指令词和标点
    stop_words = (
        "给我生成", "帮我生成", "生成", "画一个", "画一张", "来一个", "来一张",
        "场景随便", "场景", "风格的", "风格", "穿", "服装", "背景", "动作",
        "帮我", "给我", "画", "的",
    )
    for word in sorted(stop_words, key=len, reverse=True):
        raw = raw.replace(word, " ")
    # 找到最长的纯 CJK 连续片段作为候选人物名
    cjk_runs = re.findall(r"[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff]{2,}", raw)
    if cjk_runs:
        return max(cjk_runs, key=len)
    return ""


async def translate_character_name(character_text: str) -> str:
    """调用 DeepSeek/Ollama 翻译中文人物名为英文。

    翻译失败时保留原人物名,绝不抛出异常。
    """
    if not character_text or not any("\u4e00" <= ch <= "\u9fff" for ch in character_text):
        return character_text
    try:
        from translator import translate_text
        translated = await translate_text(character_text)
        result = str(translated or "").strip().split("\n")[0].strip()
        if result and result != character_text:
            # 清理翻译结果中的残留 CJK 和多余空白
            result = re.sub(r"[\s,,。.!!??::;;]+", " ", result).strip()
            if result:
                return result
    except Exception as exc:
        print(f"[TRANSLATOR] character_name_translate_failed text={character_text[:20]} error={type(exc).__name__}", flush=True)
    # 翻译失败:返回原名,后续流程会用 original CJK 做匹配或 fallback
    return character_text


def build_agent_fallback_character(translated_name: str, original_name: str) -> dict[str, str]:
    """为完全未匹配的人物构建 agent_fallback 记录"""
    display_name = str(translated_name or original_name or "").strip()
    if not display_name:
        return {}
    clean_tags = ["1girl", "solo"]
    return {
        "key": "",
        "name_zh": str(original_name or "").strip(),
        "name_en": display_name,
        "aliases": "",
        "category_zh": "",
        "category_en": "",
        "tags": ", ".join(clean_tags),
        "character_tag_source": "agent_fallback",
        "match_stage": "fallback",
        "translated_character_name": display_name,
        "original_character_name": str(original_name or "").strip(),
    }


def strip_umamusume_identity_tags(text: str) -> str:
    """从 tag 文本中移除赛马娘身份标签和分类暗示"""
    parts = []
    for raw in str(text or "").split(","):
        tag = raw.strip()
        if not tag:
            continue
        lower = tag.lower().replace("_", " ")
        # 移除赛马娘身份 tag
        if lower in UMAMUSUME_IDENTITY_TAGS:
            continue
        # 移除分类暗示 tag,比如 "xxx (umamusume)" → 整体移除
        if any(hint in lower for hint in UMAMUSUME_CATEGORY_IDENTITY_HINT):
            continue
        # 移除 race uniform 变体
        if "race uniform" in lower or lower in {"racing", "jockey uniform"}:
            continue
        parts.append(tag)
    return ", ".join(parts)


def find_characters_substring(text: str, *, limit: int = 8) -> list[dict[str, str]]:
    """Reverse CJK substring matching: 用户输入是角色名的子串时也能匹配。

    例如 '麻美' 能匹配 '七海麻美' 和 '巴麻美'。
    仅对 CJK 文本做子串匹配,英文不做反向子串匹配(误报率太高)。
    """
    raw_text = _strip_artist_spans(text or "")
    haystack_zh = _normalize_cjk_text(raw_text)
    if not haystack_zh or len(haystack_zh) < 2:
        return []
    # 只对 CJK 文本做反向子串匹配
    has_cjk = any("\u3400" <= ch <= "\u9fff" for ch in haystack_zh)
    if not has_cjk:
        return []
    characters = load_characters()
    result: list[dict[str, str]] = []
    seen_keys: set[str] = set()
    for item in characters:
        item_key = str(item.get("key") or "") or str(item.get("name_en") or "")
        if item_key in seen_keys:
            continue
        name_zh = _normalize_cjk_text(str(item.get("name_zh") or ""))
        # 反向匹配:用户输入是角色名的子串
        if name_zh and len(haystack_zh) >= 2 and haystack_zh in name_zh:
            seen_keys.add(item_key)
            tags = _normalize_tags(item["tags"], item.get("category_en", ""))
            clean_item = dict(item)
            clean_item["tags"] = tags
            result.append(clean_item)
            if len(result) >= limit:
                break
    return result


def detect_character_disambiguation(
    characters: list[dict[str, str]],
    input_text: str,
) -> dict[str, Any]:
    """检测人物匹配是否需要用户确认。

    当同一输入词匹配到多个不同 identity_key 时,需要歧义确认。
    返回 {"ambiguous": True, "term": ..., "candidates": [...]} 或 {"ambiguous": False}。

    重要区分:
    - 同一个输入词匹配多个角色 → 歧义,需要确认
    - 用户明确输入两个不同角色名 → 合法多人物,不是歧义
    """
    if not characters or len(characters) < 2:
        return {"ambiguous": False}
    raw_text = _strip_artist_spans(input_text or "")
    haystack_zh = _normalize_cjk_text(raw_text)
    if not haystack_zh:
        return {"ambiguous": False}

    # 按 input_span 分组:哪些角色被同一个输入词匹配到
    # 使用 (normalized_term) 作为 key
    span_groups: dict[str, list[dict[str, str]]] = {}
    # 预提取输入中的 CJK 片段(用于反向子串匹配)
    cjk_candidates = set()
    for run in re.findall(r"[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff]{2,}", raw_text):
        norm_run = _normalize_cjk_text(run)
        if norm_run and len(norm_run) >= 2:
            cjk_candidates.add(norm_run)
    # 滑动窗口提取 2-4 字子串
    if haystack_zh and len(haystack_zh) > 4:
        for win_size in (2, 3, 4):
            for i in range(len(haystack_zh) - win_size + 1):
                sub = haystack_zh[i:i + win_size]
                if len(sub) >= 2:
                    cjk_candidates.add(sub)
    if haystack_zh and len(haystack_zh) >= 2:
        cjk_candidates.add(haystack_zh)

    for character in characters:
        char_name_zh = _normalize_cjk_text(str(character.get("name_zh") or ""))
        char_name_en = _canonical_name_key(str(character.get("name_en") or ""))
        # 确定是哪个输入词匹配到了这个角色
        matched_term = ""
        # 正向匹配:角色名在输入中
        if char_name_zh and char_name_zh in haystack_zh:
            matched_term = char_name_zh
        elif char_name_en and char_name_en in _canonical_name_key(raw_text):
            matched_term = char_name_en
        else:
            # 反向子串匹配:输入的 CJK 片段是角色名的子串
            # 例如输入 '生成几个麻美的图',提取 '麻美',发现是 '七海麻美' 的子串
            if char_name_zh:
                for cjk_candidate in cjk_candidates:
                    if len(cjk_candidate) >= 2 and cjk_candidate in char_name_zh:
                        matched_term = cjk_candidate
                        break
        if not matched_term:
            # 尝试从 aliases 和 tags 中找匹配的 term
            for alias in _split_aliases(character.get("aliases")):
                alias_zh = _normalize_cjk_text(alias)
                if alias_zh and alias_zh in haystack_zh:
                    matched_term = alias_zh
                    break
                alias_en = _canonical_name_key(alias)
                if alias_en and re.search(rf"(?<![a-z0-9]){re.escape(alias_en)}(?![a-z0-9])", _canonical_name_key(raw_text)):
                    matched_term = alias_en
                    break
        if not matched_term:
            matched_term = "_unknown_"
        span_groups.setdefault(matched_term, []).append(character)

    # 检查是否有任何组包含多个不同 identity_key 的角色
    for term, group in span_groups.items():
        if term.startswith("_unknown_"):
            continue
        identity_keys: set[str] = set()
        for c in group:
            key = str(c.get("key") or "") or str(c.get("name_en") or "")
            if key:
                identity_keys.add(key)
        if len(identity_keys) >= 2:
            # 同一输入词匹配到多个不同角色 → 歧义
            candidates = []
            for c in group:
                key = str(c.get("key") or "") or str(c.get("name_en") or "")
                name_zh = str(c.get("name_zh") or "")
                name_en = str(c.get("name_en") or "")
                category_zh = str(c.get("category_zh") or "")
                category_en = str(c.get("category_en") or "")
                tags = str(c.get("tags") or "")
                # 从 tags 中提取作品信息
                franchise_zh = ""
                franchise_en = ""
                for tag in _split_prompt_tags(tags):
                    tag_lower = tag.lower().replace("_", " ")
                    if tag_lower not in UMAMUSUME_IDENTITY_TAGS and \
                       not any(hint in tag_lower for hint in UMAMUSUME_CATEGORY_IDENTITY_HINT):
                        # 尝试用 category 作为 franchise
                        pass
                if category_zh:
                    franchise_zh = category_zh
                if category_en:
                    franchise_en = category_en
                # 从 tags 中提取作品/系列信息(当 category 太泛时)
                tag_franchise_parts = []
                for tag in _split_prompt_tags(tags):
                    tag_clean = tag.strip()
                    if not tag_clean:
                        continue
                    tag_lower = tag_clean.lower().replace("_", " ")
                    # 跳过通用标签和角色名
                    if tag_lower in UMAMUSUME_IDENTITY_TAGS:
                        continue
                    if any(hint in tag_lower for hint in UMAMUSUME_CATEGORY_IDENTITY_HINT):
                        continue
                    if tag_lower == _canonical_name_key(name_en):
                        continue
                    if tag_lower in {_canonical_name_key(g) for g in GENERIC_MATCH_TAGS}:
                        continue
                    tag_franchise_parts.append(tag_clean)
                if tag_franchise_parts:
                    if not franchise_en or franchise_en.lower() in {"anime", "other anime"}:
                        franchise_en = ", ".join(tag_franchise_parts)
                candidates.append({
                    "character_key": key,
                    "name_zh": name_zh,
                    "name_en": name_en,
                    "franchise_zh": franchise_zh,
                    "franchise_en": franchise_en,
                    "matched_term": term,
                    "tags": tags,
                })
            return {
                "ambiguous": True,
                "term": term,
                "candidates": candidates,
            }
    return {"ambiguous": False}


def resolve_character_from_candidates(
    candidates: list[dict[str, Any]],
    user_response: str,
) -> dict[str, Any] | None:
    """从候选列表中解析用户的选择。

    支持:
    - 序号: '第一个', '1', '第一个'
    - 角色名: '七海麻美', 'Nanami Mami'
    - 作品名: '租借女友', 'Kanojo Okarishimasu'
    - 作品名+角色名: '租借女友的麻美'

    返回选中的候选 dict,或 None。
    """
    if not candidates or not user_response:
        return None
    raw = str(user_response).strip()
    if not raw:
        return None
    lowered = raw.lower()

    # 常见中文作品名 → 英文 tag 映射
    _FRANCHISE_ZH_TO_EN = {
        "租借女友": "kanojo okarishimasu",
        "魔法少女小圆": "mahou shoujo madoka magica",
        "魔法少女まどか☆マギカ": "mahou shoujo madoka magica",
        "小圆": "madoka",
        "进击的巨人": "shingeki no kyojin",
        "鬼灭之刃": "kimetsu no yaiba",
        "我的英雄学院": "boku no hero academia",
        "间谍过家家": "spy x family",
        "赛马娘": "umamusume",
        "原神": "genshin impact",
        "崩坏星穹铁道": "honkai star rail",
        "蔚蓝档案": "blue archive",
        "碧蓝档案": "blue archive",
        "命运": "fate",
        "刀剑神域": "sword art online",
        "约会大作战": "date a live",
        "五等分的新娘": "go toubun no hanayome",
        "Lycoris Recoil": "lycoris recoil",
        "孤独摇滚": "bocchi the rock",
        "【我推的孩子】": "oshi no ko",
        "推的孩子": "oshi no ko",
        "Re:Zero": "re zero",
        "从零开始": "re zero",
        "K-On": "k on",
        "轻音少女": "k on",
        "东方": "touhou",
        "东方Project": "touhou project",
        "NIKKE": "nikke",
        "胜利女神": "nikke",
    }

    # 1. 序号匹配
    ordinal_map = {
        "一": 0, "二": 1, "三": 2, "四": 3, "五": 4,
        "1": 0, "2": 1, "3": 2, "4": 3, "5": 4,
        "first": 0, "second": 1, "third": 2, "fourth": 3, "fifth": 4,
    }
    for word, index in ordinal_map.items():
        if word in lowered and 0 <= index < len(candidates):
            return candidates[index]

    # 2. 按角色名精确匹配(优先最长匹配,如"天童爱丽丝"优先于"爱丽丝")
    best_name_match = None
    best_name_len = 0
    for candidate in candidates:
        name_zh = str(candidate.get("name_zh") or "").strip()
        name_en = str(candidate.get("name_en") or "").strip().lower()
        if name_zh and name_zh in raw:
            if len(name_zh) > best_name_len:
                best_name_match = candidate
                best_name_len = len(name_zh)
        if name_en and name_en in lowered:
            if len(name_en) > best_name_len:
                best_name_match = candidate
                best_name_len = len(name_en)
    if best_name_match:
        return best_name_match

    # 3. 按作品名匹配(中英文 + tag)
    for candidate in candidates:
        franchise_zh = str(candidate.get("franchise_zh") or "").strip()
        franchise_en = str(candidate.get("franchise_en") or "").strip().lower()
        tags = str(candidate.get("tags") or "").strip().lower()
        # 直接中文匹配
        if franchise_zh and franchise_zh in raw:
            return candidate
        # 直接英文匹配
        if franchise_en and franchise_en in lowered:
            return candidate
        # 中文作品名 → 英文 tag 翻译匹配
        for zh_name, en_tag in _FRANCHISE_ZH_TO_EN.items():
            if zh_name in raw:
                # 检查候选的 tags/franchise_en 是否包含翻译后的英文
                en_tag_lower = en_tag.lower()
                if en_tag_lower in tags or en_tag_lower in franchise_en:
                    return candidate
                # 也检查 tags 的各个部分
                for tag_part in _split_prompt_tags(str(candidate.get("franchise_en") or "")):
                    if en_tag_lower in tag_part.strip().lower():
                        return candidate
        # 匹配 tags 中的各个部分
        for tag_part in _split_prompt_tags(str(candidate.get("franchise_en") or "")):
            tag_norm = tag_part.strip().lower()
            if tag_norm and tag_norm in lowered:
                return candidate
        for tag_part in _split_prompt_tags(tags):
            tag_norm = tag_part.strip().lower()
            if tag_norm and len(tag_norm) >= 3 and tag_norm in lowered:
                return candidate

    # 3b. 中文部分匹配:用户输入是作品名的前缀/子串(如"东方"匹配"东方Project")
    for candidate in candidates:
        franchise_zh = str(candidate.get("franchise_zh") or "").strip()
        franchise_en = str(candidate.get("franchise_en") or "").strip().lower()
        if franchise_zh and len(raw) >= 2:
            # 用户输入是作品中文名的子串
            if raw in franchise_zh or franchise_zh in raw:
                return candidate
        if franchise_en and len(lowered) >= 3:
            # 用户输入是作品英文名的前缀
            if franchise_en.startswith(lowered) or lowered in franchise_en:
                return candidate

    # 4. character_key 匹配
    for candidate in candidates:
        key = str(candidate.get("character_key") or "").strip().lower()
        if key and key in lowered:
            return candidate

    return None


# ── 全局人物身份Tag索引 ──────────────────────────────────────────────────

_GLOBAL_IDENTITY_INDEX: dict[str, set[str]] | None = None


def build_global_identity_index() -> dict[str, set[str]]:
    """建立全库人物身份Tag索引。

    Returns:
        {
            "identity_tags": set[str],      # 所有人物身份tag(canonical name tags)
            "franchise_tags": set[str],     # 所有作品/franchise tag
            "category_tags": set[str],      # 分类公共tag(umamusume, vocaloid等)
            "all_foreign_tags": set[str],   # 以上三者合集(用于过滤外来tag)
        }
    """
    global _GLOBAL_IDENTITY_INDEX
    if _GLOBAL_IDENTITY_INDEX is not None:
        return _GLOBAL_IDENTITY_INDEX

    chars = load_characters()
    identity: set[str] = set()
    franchise: set[str] = set()
    category: set[str] = set()

    # 通用非身份tag(外貌、表情等永远不应被视为人物身份tag)
    GENERIC_APPEARANCE = {
        "1girl", "1boy", "2girls", "2boys", "3girls", "3boys",
        "solo", "multiple girls", "multiple boys",
        "looking at viewer", "smile", "standing", "sitting",
        "long hair", "short hair", "blonde hair", "black hair", "brown hair",
        "blue eyes", "red eyes", "green eyes",
        "school uniform", "swimsuit", "bikini", "casual",
        "anime style", "high quality", "best quality", "masterpiece",
    }

    for c in chars:
        tags = _split_prompt_tags(str(c.get("tags") or ""))
        name_en = _canonical_name_key(str(c.get("name_en") or ""))

        for tag in tags:
            tag_key = _canonical_name_key(tag)
            if tag_key and tag_key not in GENERIC_APPEARANCE and len(tag_key) >= 3:
                if "(" in tag or tag_key == name_en:
                    identity.add(tag_key)
                else:
                    franchise_en = _canonical_name_key(str(c.get("franchise_en") or ""))
                    if tag_key == franchise_en:
                        franchise.add(tag_key)
                    else:
                        category.add(tag_key)

        franchise_en = _canonical_name_key(str(c.get("franchise_en") or ""))
        if franchise_en:
            franchise.add(franchise_en)

    # 从 _get_franchise_tags 补充作品标签
    try:
        ft_set = _get_franchise_tags()
        for ft in ft_set:
            franchise.add(_canonical_name_key(ft))
    except Exception:
        pass

    _GLOBAL_IDENTITY_INDEX = {
        "identity_tags": identity,
        "franchise_tags": franchise,
        "category_tags": category,
        "all_foreign_tags": identity | franchise | category,
    }
    return _GLOBAL_IDENTITY_INDEX


def clear_identity_index_cache() -> None:
    """清除全局身份索引缓存(用于测试或重载后清除)。"""
    global _GLOBAL_IDENTITY_INDEX
    _GLOBAL_IDENTITY_INDEX = None
