from __future__ import annotations

import re
import unicodedata
from typing import Any

from .lora_registry import get_lora, sanitize_loras
from .workflow_registry import get_workflow, list_workflows
from .dynamic_workflows import SMART_AGENT_DEFAULT_WORKFLOW_KEY
from .character_search import load_characters


COMMON_NON_IDENTITY_TAGS = {
    "1girl",
    "1boy",
    "solo",
    "umamusume",
    "horse_ears",
    "horse ears",
    "horse_tail",
    "horse tail",
    "animal ears",
    "tail",
    "anime style",
}

APPEARANCE_TAG_KEYS = {
    "black_hair", "white_hair", "silver_hair", "blonde_hair", "brown_hair", "red_hair",
    "blue_hair", "pink_hair", "purple_hair", "green_hair", "aqua_hair", "grey_hair",
    "gray_hair", "orange_hair",
    "long_hair", "short_hair", "medium_hair", "very_long_hair", "bob_cut", "ponytail",
    "twintails", "braid", "braided_hair", "bangs", "side_ponytail",
    "blue_eyes", "red_eyes", "green_eyes", "purple_eyes", "golden_eyes", "yellow_eyes",
    "brown_eyes", "black_eyes", "pink_eyes", "aqua_eyes", "heterochromia",
    "pale_skin", "tan_skin", "dark_skin", "fair_skin", "white_skin",
    "petite", "tall", "short", "slender", "curvy", "muscular", "large_breasts",
    "small_breasts", "medium_breasts", "wide_hips", "thick_thighs",
    "mole", "freckles", "fang", "pointed_ears", "makeup",
}

EXPLICIT_APPEARANCE_PATTERNS = {
    "blue_hair": ("blue hair", "蓝发", "蓝色头发", "蓝色长发"),
    "long_hair": ("long hair", "长发", "长头发", "蓝色长发"),
    "red_eyes": ("red eyes", "红眼", "红色眼睛", "红瞳"),
    "white_hair": ("white hair", "白发", "白色头发"),
    "black_hair": ("black hair", "黑发", "黑色头发"),
    "brown_hair": ("brown hair", "棕发", "棕色头发"),
    "blonde_hair": ("blonde hair", "金发", "金色头发"),
    "short_hair": ("short hair", "短发"),
    "blue_eyes": ("blue eyes", "蓝眼", "蓝色眼睛", "蓝瞳"),
    "green_eyes": ("green eyes", "绿眼", "绿色眼睛", "绿瞳"),
    "purple_eyes": ("purple eyes", "紫眼", "紫色眼睛", "紫瞳"),
    "large_breasts": (
        "large breasts", "large bust", "full bust", "emphasized bust", "prominent bust",
        "巨乳", "大胸", "胸大", "丰满胸部", "胸部丰满", "突出胸大", "胸部明显",
    ),
    "small_breasts": ("small breasts", "贫乳", "小胸"),
}


class CharacterPromptValidationError(ValueError):
    pass


# ── 人数标签 ───────────────────────────────────────────

# 需要从最终 Prompt 中精确删除的冲突人数标签（完整 Tag 匹配）
_COUNT_TAGS_CONFLICT = {
    "1girl", "1boy", "solo", "solo focus", "solo_focus",
    "single girl", "single boy", "single person",
    "2girls", "2boys", "3girls", "3boys",
    "multiple girls", "multiple boys",
}

# 后端性别元数据映射：优先级高于 tags 中的 1girl/1boy 标签
# key 使用 character key（去重后的 stable_character_key）
_CHARACTER_GENDER_MAP: dict[str, str] = {
    # ── VOCALOID ──
    "hatsune_miku": "female",
    "kagamine_rin": "female",
    "kagamine_len": "male",
    "megurine_luka": "female",
    "kasane_teto": "female",
    "meiko": "female",
    "gumi": "female",
    "ia_(vocaloid)": "female",
    "yuzuki_yukari": "female",
    "otomachi_una": "female",
    "luo_tianyi": "female",
    "yuezheng_ling": "female",
    "kafu_(cevio)": "female",
    # ── Genshin Impact ──
    "raiden_shogun": "female",
    "furina": "female",
    "nahida": "female",
    "hu_tao": "female",
    "ganyu": "female",
    "keqing": "female",
    "yae_miko": "female",
    "nilou": "female",
    "mona": "female",
    "kamisato_ayaka": "female",
    "lumine_(genshin_impact)": "female",
    "jean_(genshin_impact)": "female",
    "lisa_(genshin_impact)": "female",
    "barbara_(genshin_impact)": "female",
    "klee_(genshin_impact)": "female",
    "eula": "female",
    "shenhe_(genshin_impact)": "female",
    "sangonomiya_kokomi": "female",
    "yoimiya": "female",
    "navia_(genshin_impact)": "female",
    "arlecchino_(genshin_impact)": "female",
    "clorinde_(genshin_impact)": "female",
    "xianyun_(genshin_impact)": "female",
    "mavuika_(genshin_impact)": "female",
    "citlali_(genshin_impact)": "female",
    "xilonen_(genshin_impact)": "female",
    "chasca_(genshin_impact)": "female",
    "skirk_(genshin_impact)": "female",
    "lynette_(genshin_impact)": "female",
    "kirara_(genshin_impact)": "female",
    # ── Honkai: Star Rail ──
    "kafka_(honkai:_star_rail)": "female",
    "firefly_(honkai:_star_rail)": "female",
    "acheron_(honkai:_star_rail)": "female",
    "sparkle_(honkai:_star_rail)": "female",
    "march_7th_(honkai:_star_rail)": "female",
    "stelle_(honkai:_star_rail)": "female",
    "silver_wolf_(honkai:_star_rail)": "female",
    "herta_(honkai:_star_rail)": "female",
    "bronya_rand": "female",
    "seele_(honkai:_star_rail)": "female",
    "himeko_(honkai:_star_rail)": "female",
    "fu_xuan": "female",
    "qingque": "female",
    "jingliu": "female",
    "topaz_(honkai:_star_rail)": "female",
    "guinaifen": "female",
    "black_swan_(honkai:_star_rail)": "female",
    "ruan_mei": "female",
    "robin_(honkai:_star_rail)": "female",
    "jade_(honkai:_star_rail)": "female",
    "feixiao": "female",
    "lingsha": "female",
    "the_herta": "female",
    "aglaea": "female",
    "castorice": "female",
    "tribbie": "female",
    # ── Lycoris Recoil ──
    "nishikigi_chisato": "female",
    # ── Blue Archive ──
    "sunaookami_shiroko": "female",
    "tendou_aris": "female",
    "hayase_yuuka": "female",
    "misono_mika": "female",
    "ajitani_hifumi": "female",
    "shirasu_azusa": "female",
    "rikuhachima_aru": "female",
    "kurosaki_koyuki": "female",
    "takanashi_hoshino": "female",
    "izayoi_nonomi": "female",
    "kuromi_serika": "female",
    "sorasaki_hina": "female",
    "shiromi_iori": "female",
    "amau_ako": "female",
    "asagi_mutsuki": "female",
    "onikata_kayoko": "female",
    "igusa_haruka": "female",
    "ichinose_asuna": "female",
    "kakudate_karin": "female",
    "mikamo_neru": "female",
    "urawa_hanako": "female",
    "shimoe_koharu": "female",
    "kozeki_ui": "female",
    "kosaka_wakamo": "female",
    # ── Fate ──
    "artoria_pendragon": "female",
    "tohsaka_rin": "female",
    "matou_sakura": "female",
    "mash_kyrielight": "female",
    "jeanne_d'arc_(fate)": "female",
    "nero_claudius_(fate)": "female",
    "scathach_(fate)": "female",
    "illyasviel_von_einzbern": "female",
    "medusa_(fate)": "female",
    "mordred_(fate)": "female",
    "jeanne_d'arc_alter": "female",
    "ishtar_(fate)": "female",
    "ereshkigal_(fate)": "female",
    "morgan_le_fay_(fate)": "female",
    "melusine_(fate)": "female",
    "bb_(fate)": "female",
    "kama_(fate)": "female",
    "okita_souji_(fate)": "female",
    "miyamoto_musashi_(fate)": "female",
    # ── Lycoris Recoil ──
    "nishikigi_chisato": "female",
    "chisato_nishikigi": "female",
    # ── Re:Zero ──
    "ram": "female",
    "rem": "female",
    "emilia": "female",
    "emilia_(re_zero)": "female",
    "beatrice": "female",
    "beatrice_(re_zero)": "female",
    "echidna": "female",
    "echidna_(re_zero)": "female",
    # ── K-On! ──
    "hirasawa_yui": "female",
    "akiyama_mio": "female",
    "nakano_azusa": "female",
    "tainaka_ritsu": "female",
    "kotobuki_tsumugi": "female",
    # ── 其他常见女角色 ──
    "mizuhara_chizuru": "female",
    "chizuru_mizuhara": "female",
    "nanami_mami": "female",
    "mami_nanami": "female",
    "sakurasawa_sumi": "female",
    # ── Umamusume ──
    "vivlos": "female",
    "tokai_teio": "female",
    "mejiro_mcqueen": "female",
    "special_week": "female",
    "silence_suzuka": "female",
    "grass_wonder": "female",
    "gold_ship": "female",
    "oguri_cap": "female",
    "daiwa_scarlet": "female",
    "vodka": "female",
    "symboli_rudolf": "female",
    "air_groove": "female",
    "maruzensky": "female",
    "mihono_bourbon": "female",
    "rice_shower": "female",
    "mejiro_ryan": "female",
    "tamamo_cross": "female",
    "super_creek": "female",
    "biwa_hayahide": "female",
    "twin_turbo": "female",
    "mejiro_palmer": "female",
    "nishino_flower": "female",
    "smart_falcon": "female",
    "curren_chan": "female",
    "kitasan_black": "female",
    "satono_diamond": "female",
    "manhattan_cafe": "female",
    "agnes_tachyon": "female",
    "haru_urara": "female",
    "eishin_flash": "female",
    "meisho_doto": "female",
    "kawakami_princess": "female",
    "inari_one": "female",
    "mister_c.b.": "female",
    "sirius_symboli": "female",
    "winning_ticket": "female",
    "narita_brian": "female",
    "narita_taishin": "female",
    "king_halo": "female",
    "matikanefukukitaru": "female",
    "zenno_rob_roy": "female",
    "admire_vega": "female",
    "mayano_top_gun": "female",
    "hishi_amazon": "female",
    "seeking_the_pearl": "female",
    "sakura_laurel": "female",
    "natures_nature": "female",
    "daitaku_helios": "female",
}


def _infer_character_gender(character: dict[str, Any] | None) -> str:
    """推断人物性别。

    优先级：
    1. 后端性别元数据映射 (_CHARACTER_GENDER_MAP) — 按 key 和 name_en 匹配；
    2. 人物 canonical tags 中是否包含 1girl / 1boy；
    3. 如果人物来自 character-tags.json（有 category_en 且非 agent_fallback），
       且 tags 中不含 1boy，默认推断为 female；
    4. 未知返回 "unknown"。

    不允许根据姓名、外貌或 DS 回复随意猜测。
    """
    if not character:
        return "unknown"
    # 1) 后端映射 — 按 key 或 name_en 查找
    key = str(character_key(character) or "").strip()
    name_en = str(character.get("name_en") or "").strip()
    # 精确 key 匹配
    mapped = _CHARACTER_GENDER_MAP.get(key) or _CHARACTER_GENDER_MAP.get(key.lower())
    if mapped:
        return mapped
    # name_en 规范化后匹配
    if name_en:
        name_key = _canonical_name_key(name_en).replace(" ", "_")
        mapped = _CHARACTER_GENDER_MAP.get(name_key) or _CHARACTER_GENDER_MAP.get(name_key.lower())
        if mapped:
            return mapped
    # 2) 检查 tags 中的 1girl/1boy
    tags_text = str(character.get("tags") or "")
    tags_lower = tags_text.lower()
    if "1girl" in tags_lower:
        return "female"
    if "1boy" in tags_lower:
        return "male"
    # 3) character-tags.json 中的人物（有 category_en 且非 agent_fallback）
    #    默认为 female（该库中 99% 为女性角色）
    category = str(character.get("category_en") or "").strip()
    tag_source = str(character.get("character_tag_source") or "").strip()
    if category and tag_source != "agent_fallback":
        if "1boy" not in tags_lower:
            return "female"
    return "unknown"


def _canonical_name_key(text: str) -> str:
    """将名称规范化为小写、去空格、用于映射匹配的 key。"""
    return str(text or "").strip().lower().replace("  ", " ").replace(" ", "_")


def _compute_count_tag(characters: list[dict[str, Any]]) -> str:
    """根据去重后的明确人物列表计算人数标签。

    返回如 \"2girls\", \"1girl\", \"1girl, 1boy\" 等。
    性别不确定时记录 unknown，不强行假设。
    """
    if not characters:
        return ""
    female_count = 0
    male_count = 0
    for character in characters:
        gender = _infer_character_gender(character)
        if gender == "female":
            female_count += 1
        elif gender == "male":
            male_count += 1
        # unknown 不计入任何性别
    parts: list[str] = []
    if female_count == 1:
        parts.append("1girl")
    elif female_count > 1:
        parts.append(f"{female_count}girls")
    if male_count == 1:
        parts.append("1boy")
    elif male_count > 1:
        parts.append(f"{male_count}boys")
    return ", ".join(parts) if parts else ""


def _strip_count_tags(prompt: str) -> str:
    """从 Prompt 中精确删除所有冲突人数标签（完整 Tag 匹配）。

    不会误删 solo cup、solo leveling 等作品名。
    """
    from .character_preferences import split_prompt_tags as _split
    tags = _split(prompt)
    kept = [tag for tag in tags if tag.lower().replace("_", " ").strip() not in _COUNT_TAGS_CONFLICT]
    return ", ".join(kept)


def _apply_count_tags(prompt: str, characters: list[dict[str, Any]]) -> str:
    """删除冲突人数标签，然后根据明确人物重新加入正确的人数标签。

    返回修正后的 prompt。
    """
    cleaned = _strip_count_tags(prompt)
    if len(characters or []) == 1 and _mentions_background_crowd(cleaned):
        return cleaned
    count_tag = _compute_count_tag(characters)
    if not count_tag:
        return cleaned
    # 人数标签放在 prompt 最前部
    return f"{count_tag}, {cleaned}" if cleaned else count_tag


def _mentions_background_crowd(prompt: str) -> bool:
    text = str(prompt or "").lower()
    return any(term in text for term in (
        "人群", "路人", "群众", "crowd", "crowded", "background people",
        "bystanders", "group of people", "many people",
    ))


def merge_prompt_tags(tag_groups: list[str], prompt: str) -> str:
    tags: list[str] = []
    seen = set()
    for group in tag_groups:
        for raw in str(group or "").split(","):
            tag = raw.strip()
            if tag and tag.lower() not in seen:
                tags.append(tag)
                seen.add(tag.lower())
    for raw in str(prompt or "").split(","):
        tag = raw.strip()
        if tag and tag.lower() not in seen:
            tags.append(tag)
            seen.add(tag.lower())
    return ", ".join(tags)


def split_prompt_tags(value: str) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()
    for raw in str(value or "").replace("，", ",").split(","):
        tag = " ".join(raw.strip().split())
        if not tag:
            continue
        key = _tag_key(tag)
        if key in seen:
            continue
        seen.add(key)
        tags.append(tag)
    return tags


def extract_explicit_appearance_tags(text: str) -> set[str]:
    haystack = normalize_name(text)
    compact = str(text or "").lower().replace(" ", "")
    result: set[str] = set()
    for tag_key, patterns in EXPLICIT_APPEARANCE_PATTERNS.items():
        for pattern in patterns:
            normalized = normalize_name(pattern)
            pattern_compact = str(pattern).lower().replace(" ", "")
            if (normalized and normalized in haystack) or (pattern_compact and pattern_compact in compact):
                result.add(tag_key)
                break
    return result


def sanitize_inferred_appearance_tags(
    prompt: str,
    *,
    user_text: str,
    character: dict[str, Any] | None = None,
) -> tuple[str, int, set[str]]:
    explicit = extract_explicit_appearance_tags(user_text)
    identity = {_tag_key(tag) for tag in locked_character_tags(character)}
    kept: list[str] = []
    removed = 0
    for tag in split_prompt_tags(prompt):
        key = _tag_key(tag)
        if key in APPEARANCE_TAG_KEYS and key not in explicit and key not in identity:
            removed += 1
            continue
        kept.append(tag)
    return ", ".join(kept), removed, explicit


def character_key(character: dict[str, Any] | None) -> str:
    if not character:
        return ""
    raw = str(character.get("key") or "").strip()
    if raw:
        return raw
    return normalize_name(str(character.get("name_en") or character.get("name_zh") or "")).replace(" ", "_")


def locked_character_tags(character: dict[str, Any] | None) -> list[str]:
    if not character:
        return []
    return split_prompt_tags(str(character.get("tags") or ""))


def identity_character_tags(character: dict[str, Any] | None) -> list[str]:
    """Return only character-specific identity tags, excluding franchise/series tags.

    Used for prompt assembly where franchise tags like "lycoris recoil" should
    NOT be locked as identity markers.
    """
    from .character_search import _extract_identity_tags
    if not character:
        return []
    return _extract_identity_tags(character)


def assemble_character_prompt(
    *,
    character: dict[str, Any] | None,
    scene_prompt: str,
    user_text: str = "",
) -> tuple[str, int]:
    locked = locked_character_tags(character)
    appearance_clean, appearance_removed, _ = sanitize_inferred_appearance_tags(
        scene_prompt,
        user_text=user_text,
        character=character,
    )
    scene_clean, removed = remove_foreign_character_tags(appearance_clean, selected_character=character)
    scene_tags = [
        tag for tag in split_prompt_tags(scene_clean)
        if _tag_key(tag) not in {_tag_key(item) for item in locked}
    ]
    return merge_prompt_tags([", ".join(locked)], ", ".join(scene_tags)), removed + appearance_removed


def assemble_character_prompt_with_count(
    *,
    character: dict[str, Any] | None,
    scene_prompt: str,
    user_text: str = "",
) -> tuple[str, int]:
    """单人物 Prompt 组装，自动加入人数标签（1girl/1boy）。

    与 assemble_character_prompt 逻辑一致，但在最终 prompt 最前部加入人数标签。
    """
    base_prompt, removed = assemble_character_prompt(
        character=character,
        scene_prompt=scene_prompt,
        user_text=user_text,
    )
    if not base_prompt:
        return base_prompt, removed
    characters = [character] if character else []
    final = _apply_count_tags(base_prompt, characters)
    return final, removed


def remove_foreign_character_tags(
    prompt: str,
    *,
    selected_character: dict[str, Any] | None,
) -> tuple[str, int]:
    selected_allowed = _selected_identity_keys(selected_character)
    foreign = _known_identity_tag_keys() - selected_allowed
    # 对于 agent_fallback / explicit_user_tag 人物，其 canonical tags
    # 包含用户明确输入的 Tag（可能是库外 identity-looking tag）。
    # 这些 Tag 不应被 _looks_like_identity_tag 误判为外来人物。
    selected_explicit_allowed = _selected_explicit_tag_keys(selected_character)
    kept: list[str] = []
    removed = 0
    for tag in split_prompt_tags(prompt):
        key = _tag_key(tag)
        # 1. 已知的外来人物 Tag（在白名单中但未被选中）
        if key in foreign or _looks_like_foreign_cosplay(key, selected_allowed):
            removed += 1
            continue
        # 2. 不在已知白名单中，但看起来像是结构化人物身份 Tag
        #    格式如 character_name_(franchise) 或 character_name (franchise)
        #    如果不在 selected 中，则视为外人物 Tag 移除
        if (key not in selected_allowed and key not in selected_explicit_allowed
                and _looks_like_identity_tag(tag)):
            removed += 1
            continue
        kept.append(tag)
    return ", ".join(kept), removed


def _looks_like_identity_tag(tag: str) -> bool:
    """检测 tag 是否看起来像结构化人物身份 Tag。

    匹配模式：
    - xxx_(franchise)  如 yukikaze_(azur_lane)
    - xxx (franchise)  如 yukikaze (azur lane)
    - 包含括号且有下划线或空格分隔的Booru风格tag
    """
    raw = str(tag or "").strip()
    # 纯括号格式: name_(franchise) 或 name (franchise)
    if re.search(r'\w+\s*[\(（][^\)）]+[\)）]', raw):
        return True
    # 下划线+括号: name_name_(franchise) 
    if '_' in raw and ('(' in raw or '（' in raw):
        return True
    return False


def sanitize_prompt_library_tags(prompt: str, *, selected_character: dict[str, Any] | None = None) -> str:
    clean, _ = remove_foreign_character_tags(prompt, selected_character=selected_character)
    return clean


def validate_character_prompt(
    *,
    prompt: str,
    character: dict[str, Any] | None,
    workflow_key: str,
    loras: list[dict[str, Any]],
    user_text: str = "",
    all_characters: list[dict[str, Any]] | None = None,
) -> None:
    tags = split_prompt_tags(prompt)
    if not tags:
        raise CharacterPromptValidationError("character_prompt_validation_failed")
    if not character:
        return
    if not character_key(character):
        raise CharacterPromptValidationError("character_prompt_validation_failed")
    locked = locked_character_tags(character)
    prompt_keys = [_tag_key(tag) for tag in tags]
    locked_keys = [_tag_key(tag) for tag in locked]
    # Remove count tags from locked_keys since they are handled separately
    # by _apply_count_tags and stripped from the front during contiguity check.
    # A character's canonical tags may include "1girl" which conflicts.
    _count_tag_keys = {_tag_key(t) for t in ("1girl", "1boy", "2girls", "2boys", "3girls", "3boys", "solo")}
    locked_keys = [k for k in locked_keys if k not in _count_tag_keys]
    locked = [t for t in locked if _tag_key(t) not in _count_tag_keys]
    if any(key not in prompt_keys for key in locked_keys):
        raise CharacterPromptValidationError("character_prompt_validation_failed")
    # Skip count tags (1girl, 2girls, etc.) at the beginning when checking
    # locked tag positions, since _apply_count_tags prepends them.
    prompt_keys_after_count = prompt_keys
    while prompt_keys_after_count and prompt_keys_after_count[0] in _count_tag_keys:
        prompt_keys_after_count = prompt_keys_after_count[1:]
    # For multi-character prompts, other characters' identity tags may also
    # be at the start. Skip any locked tags from OTHER characters too.
    # We just need to verify that THIS character's locked tags are contiguous
    # at the start (after count tags).
    if prompt_keys_after_count[: len(locked_keys)] != locked_keys:
        # Check if other characters' tags are interleaved at the start
        # Find where this character's locked tags start
        first_locked = locked_keys[0] if locked_keys else ""
        start_idx = 0
        for i, pk in enumerate(prompt_keys_after_count):
            if pk == first_locked:
                start_idx = i
                break
        if prompt_keys_after_count[start_idx: start_idx + len(locked_keys)] != locked_keys:
            raise CharacterPromptValidationError("character_prompt_validation_failed")
    _, removed = remove_foreign_character_tags(prompt, selected_character=character)
    if removed:
        # For multi-character prompts, check if removed tags belong to other matched characters
        if all_characters and len(all_characters) > 1:
            all_allowed: set[str] = set()
            for c in all_characters:
                all_allowed.update(_selected_identity_keys(c))
            foreign = _known_identity_tag_keys() - all_allowed
            # Re-check: only fail if tags are truly foreign to ALL characters
            for tag in split_prompt_tags(prompt):
                key = _tag_key(tag)
                if key in foreign:
                    raise CharacterPromptValidationError("character_prompt_validation_failed")
        else:
            raise CharacterPromptValidationError("character_prompt_validation_failed")
    workflow = get_workflow(workflow_key) or {}
    workflow_character_key = str(workflow.get("character_key") or "").strip()
    if workflow_character_key and workflow_character_key != character_key(character):
        raise CharacterPromptValidationError("character_prompt_validation_failed")
    if any(not tag.strip() for tag in str(prompt).split(",")):
        raise CharacterPromptValidationError("character_prompt_validation_failed")
    if ",," in str(prompt):
        raise CharacterPromptValidationError("character_prompt_validation_failed")
    explicit = extract_explicit_appearance_tags(user_text)
    identity = {_tag_key(tag) for tag in locked_character_tags(character)}
    unexpected = [
        tag for tag in tags
        if _tag_key(tag) in APPEARANCE_TAG_KEYS and _tag_key(tag) not in explicit and _tag_key(tag) not in identity
    ]
    if unexpected:
        raise CharacterPromptValidationError("unexpected_inferred_appearance_tags")


def _tag_key(value: str) -> str:
    return normalize_name(str(value or "")).replace(" ", "_")


def _character_identity_keys(character: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for tag in split_prompt_tags(str(character.get("tags") or "")):
        normalized = _tag_key(tag)
        if (
            normalized
            and normalized not in {_tag_key(item) for item in COMMON_NON_IDENTITY_TAGS}
            and normalized not in APPEARANCE_TAG_KEYS
        ):
            keys.add(normalized)
    for term in _character_terms(character):
        normalized = _tag_key(term)
        if (
            normalized
            and normalized not in {_tag_key(item) for item in COMMON_NON_IDENTITY_TAGS}
            and normalized not in APPEARANCE_TAG_KEYS
        ):
            keys.add(normalized)
    key = character_key(character)
    if key:
        keys.add(_tag_key(key))
    return keys


def _selected_identity_keys(character: dict[str, Any] | None) -> set[str]:
    if not character:
        return set()
    return _character_identity_keys(character)


def _selected_explicit_tag_keys(character: dict[str, Any] | None) -> set[str]:
    """返回用户明确选择的库外人物 Tag 的归一化 key 集合。

    对于 agent_fallback / explicit_user_character / explicit_user_tag 人物，
    用户明确输入的 Tag（如 yukikaze_(azur_lane)）不应被 _looks_like_identity_tag
    误判为外来人物。这些 Tag 来自 selected_characters_json 的 canonical_tags。
    """
    if not character:
        return set()
    source = str(character.get("character_tag_source") or character.get("source") or "")
    if source not in ("agent_fallback", "explicit_user_character", "explicit_user_tag"):
        return set()
    keys: set[str] = set()
    for tag in split_prompt_tags(str(character.get("tags") or "")):
        key = _tag_key(tag)
        if key and key not in {_tag_key(item) for item in COMMON_NON_IDENTITY_TAGS}:
            keys.add(key)
    # 也加入 name_en 和 translated_character_name（这些已在 _build_selected_characters_json
    # 中被注入 canonical_tags → tags，但稳妥起见也覆盖原始字段）
    for field in ("name_en", "translated_character_name", "original_character_name"):
        val = str(character.get(field) or "").strip()
        if val:
            keys.add(_tag_key(val))
    # 加入 explicit_tags 列表中的所有 tag
    for et in (character.get("explicit_tags") or character.get("canonical_tags") or []):
        et_str = str(et).strip()
        if et_str:
            keys.add(_tag_key(et_str))
    return keys


def _known_identity_tag_keys() -> set[str]:
    keys: set[str] = set()
    for character in load_characters():
        for tag in split_prompt_tags(str(character.get("tags") or "")):
            normalized = _tag_key(tag)
            if (
                normalized
                and normalized not in {_tag_key(item) for item in COMMON_NON_IDENTITY_TAGS}
                and normalized not in APPEARANCE_TAG_KEYS
            ):
                keys.add(normalized)
    keys.update(
        _tag_key(item)
        for item in (
            "minamoto_no_raikou",
            "minamoto_no_raikou_(fate)",
            "le_malin_(azur_lane)",
            "sirius_(azur_lane)",
            "sirius_(azur_lane)_(cosplay)",
            "sailor_senshi",
            "kirisame_marisa_(cosplay)",
        )
    )
    return keys


def _looks_like_foreign_cosplay(tag_key: str, selected_allowed: set[str]) -> bool:
    if "cosplay" not in tag_key:
        return False
    if tag_key in selected_allowed:
        return False
    return True


def normalize_name(value: str) -> str:
    text = str(value or "")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"\.(json|workflow)$", "", text, flags=re.I)
    text = re.sub(r"[_\\/\-()\[\]{}（）【】「」『』·・:：,，.。]+", " ", text)
    text = " ".join(text.lower().split())
    return text


def _split_csv(value: Any) -> list[str]:
    if isinstance(value, list):
        parts = value
    else:
        parts = str(value or "").replace("，", ",").split(",")
    result = []
    seen = set()
    for raw in parts:
        item = str(raw or "").strip()
        if item and item not in seen:
            result.append(item)
            seen.add(item)
    return result


def _character_terms(character: dict[str, Any]) -> list[str]:
    terms = [
        str(character.get("name_zh") or ""),
        str(character.get("name_en") or ""),
        str(character.get("key") or "").replace("_", " "),
    ]
    terms.extend(_split_csv(character.get("aliases")))
    return [term for term in terms if term.strip()]


def _is_full_name_term(term: str, character: dict[str, Any]) -> bool:
    clean = normalize_name(term)
    return clean in {
        normalize_name(character.get("name_zh")),
        normalize_name(character.get("name_en")),
        normalize_name(str(character.get("key") or "").replace("_", " ")),
    }


def _term_is_reliable(term: str, *, full_name: bool) -> bool:
    stripped = str(term or "").strip()
    if not stripped:
        return False
    if full_name:
        return True
    has_cjk = any("\u3400" <= ch <= "\u9fff" for ch in stripped)
    if has_cjk:
        return sum(1 for ch in stripped if "\u3400" <= ch <= "\u9fff") >= 2
    return len(normalize_name(stripped).replace(" ", "")) >= 4


def _contains_term(candidate: str, term: str) -> bool:
    if not candidate or not term:
        return False
    has_cjk = any("\u3400" <= ch <= "\u9fff" for ch in term)
    if has_cjk:
        return term in candidate
    return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", candidate) is not None


def _workflow_search_text(workflow: dict[str, Any]) -> str:
    parts = [
        str(workflow.get("key") or "").replace("_", " "),
        str(workflow.get("label") or ""),
        str(workflow.get("notes") or ""),
    ]
    parts.extend(str(x) for x in workflow.get("aliases") or [])
    return normalize_name(" ".join(parts))


def _workflow_aliases(workflow: dict[str, Any]) -> set[str]:
    aliases = {
        normalize_name(str(workflow.get("key") or "")),
        normalize_name(str(workflow.get("label") or "")),
    }
    for item in workflow.get("aliases") or []:
        normalized = normalize_name(str(item))
        if normalized:
            aliases.add(normalized)
            compact = normalized.replace(" ", "")
            if compact:
                aliases.add(compact)
    return {item for item in aliases if item}


def _character_lora_keys(character: dict[str, Any]) -> list[str]:
    keys = _split_csv(character.get("preferred_lora_keys"))
    recommended = str(character.get("recommended_lora_key") or "").strip()
    if recommended and recommended not in keys:
        keys.append(recommended)
    return [key for key in keys if key]


def _preferred_workflow_keys(character: dict[str, Any]) -> list[str]:
    keys = _split_csv(character.get("preferred_workflow_keys"))
    legacy = str(character.get("recommended_workflow_key") or "").strip()
    if legacy and legacy not in keys:
        keys.append(legacy)
    return keys


def _score_workflow_for_character(workflow: dict[str, Any], character: dict[str, Any]) -> int:
    character_key = str(character.get("key") or "").strip()
    if character_key and str(workflow.get("character_key") or "").strip() == character_key:
        return 1000
    candidate = _workflow_search_text(workflow)
    best = 0
    for term in _character_terms(character):
        normalized = normalize_name(term)
        if not normalized:
            continue
        full_name = _is_full_name_term(term, character)
        if not _term_is_reliable(term, full_name=full_name):
            continue
        if candidate == normalized:
            best = max(best, 100 if full_name else 80)
        elif candidate.startswith(normalized + " "):
            best = max(best, 90 if full_name else 80)
        elif _contains_term(candidate, normalized):
            best = max(best, 90 if full_name else 80)
    return best


def _request_match_score(workflow: dict[str, Any], request_text: str) -> int:
    haystack = normalize_name(request_text)
    score = 0
    for tag in workflow.get("selection_tags") or []:
        normalized = normalize_name(str(tag))
        if normalized and _contains_term(haystack, normalized):
            score += 1
    return score


def _find_type_workflow(*, request_text: str, is_admin: bool = False) -> dict[str, Any] | None:
    haystack = normalize_name(request_text)
    if not haystack:
        return None
    matches: list[tuple[int, int, str, dict[str, Any]]] = []
    for workflow in list_workflows(is_admin=is_admin):
        category = str(workflow.get("category") or "")
        if category not in {"video", "in_game", "img2img", "txt2img", "debug"}:
            continue
        score = _request_match_score(workflow, request_text)
        if category == "video" and any(term in haystack for term in ("video", "视频", "首尾帧", "帧")):
            score += 10
        if category == "in_game" and any(term in haystack for term in ("in game", "ingame", "游戏")):
            score += 10
        if category == "img2img" and any(term in haystack for term in ("图生图", "重绘", "img2img")):
            score += 10
        if category == "txt2img" and any(term in haystack for term in ("文生图", "txt2img")):
            score += 10
        if category == "debug" and any(term in haystack for term in ("调试", "调流", "debug")):
            score += 10
        if score <= 0:
            continue
        matches.append((score, 1 if workflow.get("preferred") else 0, str(workflow.get("key") or ""), workflow))
    if not matches:
        return None
    matches.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return dict(matches[0][3])


def _find_character_workflow(
    character: dict[str, Any],
    *,
    is_admin: bool = False,
    request_text: str = "",
) -> dict[str, Any] | None:
    for key in _preferred_workflow_keys(character):
        workflow = _find_preferred_workflow_reference(key, character, is_admin=is_admin)
        if workflow:
            workflow["match_score"] = 1000
            return workflow

    matches: list[tuple[int, int, int, str, dict[str, Any]]] = []
    for workflow in list_workflows(is_admin=is_admin):
        score = _score_workflow_for_character(workflow, character)
        if score < 80:
            continue
        preferred = 1 if workflow.get("preferred") else 0
        request_score = _request_match_score(workflow, request_text)
        matches.append((score, preferred, request_score, str(workflow.get("key") or ""), workflow))
    if not matches:
        return None
    matches.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3]))
    best = dict(matches[0][4])
    best["match_score"] = matches[0][0]
    return best


def _find_preferred_workflow_reference(
    reference: str,
    character: dict[str, Any],
    *,
    is_admin: bool = False,
) -> dict[str, Any] | None:
    ref = str(reference or "").strip()
    if not ref:
        return None
    workflow = get_workflow(ref, is_admin=is_admin)
    if workflow:
        return workflow
    ref_norm = normalize_name(ref)
    ref_compact = ref_norm.replace(" ", "")
    for item in list_workflows(is_admin=is_admin):
        aliases = _workflow_aliases(item)
        if ref_norm in aliases or (ref_compact and ref_compact in aliases):
            return dict(item)
    for item in list_workflows(is_admin=is_admin):
        if _score_workflow_for_character(item, character) >= 80 and _contains_term(_workflow_search_text(item), ref_norm):
            return dict(item)
    return None


def _remove_character_loras(loras: list[dict[str, Any]], character_lora_keys: set[str]) -> list[dict[str, Any]]:
    if not character_lora_keys:
        return loras
    return [item for item in loras if str(item.get("key") or "") not in character_lora_keys]


def _workflow_for_lora(current_workflow: str, entry: dict[str, Any], *, is_admin: bool = False) -> str | None:
    compatible = entry.get("compatible_workflows") or []
    if not compatible or current_workflow in compatible:
        return current_workflow
    for key in compatible:
        if get_workflow(str(key), is_admin=is_admin):
            return str(key)
    return None


def enforce_character_preferences(
    *,
    characters: list[dict[str, Any]],
    workflow_key: str,
    positive_prompt: str,
    loras: Any,
    is_admin: bool = False,
    request_text: str = "",
) -> dict[str, Any]:
    # Smart Agent 默认通用工作流作为最终 fallback，不再硬编码 anima_owner
    selected_workflow = str(workflow_key or SMART_AGENT_DEFAULT_WORKFLOW_KEY).strip() or SMART_AGENT_DEFAULT_WORKFLOW_KEY
    raw_loras = list(loras) if isinstance(loras, list) else []
    matched_tags: list[str] = []
    character_lora_keys: set[str] = set()
    forced = False
    fallback_level = "character_tags" if characters else "none"
    character_workflow_key = ""
    allow_external_lora = False
    selected_character = characters[0] if characters else None

    for character in characters:
        matched_tags.append(str(character.get("tags") or ""))
        character_lora_keys.update(_character_lora_keys(character))
        workflow = _find_character_workflow(character, is_admin=is_admin, request_text=request_text or positive_prompt)
        if workflow:
            selected_workflow = str(workflow["key"])
            character_workflow_key = selected_workflow
            allow_external_lora = bool(workflow.get("allow_external_lora"))
            fallback_level = "character_workflow"
            forced = True
            break

    if character_workflow_key:
        clean_loras = sanitize_loras(raw_loras, selected_workflow)
        workflow_meta = get_workflow(character_workflow_key, is_admin=is_admin) or {}
        if not allow_external_lora:
            raw_loras = _remove_character_loras(raw_loras, character_lora_keys)
            clean_loras = sanitize_loras(raw_loras, selected_workflow)
        final_prompt, removed_count = assemble_character_prompt_with_count(
            character=selected_character,
            scene_prompt=positive_prompt,
            user_text=request_text,
        )
        return {
            "workflow_key": selected_workflow,
            "positive_prompt": final_prompt,
            "loras": clean_loras,
            "forced": forced,
            "fallback_level": fallback_level,
            "character_workflow_key": character_workflow_key,
            "allow_external_lora": allow_external_lora,
            "character_tag_injected": bool([tag for tag in matched_tags if str(tag).strip()]),
            "locked_character_tags": locked_character_tags(selected_character),
            "foreign_character_tags_removed_count": removed_count,
            "inferred_appearance_tags_removed_count": removed_count,
            "internal_lora_count": int(workflow_meta.get("embedded_lora_count") or 0),
            "external_lora_count": len(clean_loras),
            "workflow_health_status": str(workflow_meta.get("health_status") or "unknown"),
        }

    type_workflow = _find_type_workflow(request_text=request_text, is_admin=is_admin)
    if type_workflow:
        selected_workflow = str(type_workflow["key"])
        fallback_level = "type_workflow"
        forced = True

    for character in characters:
        for lora_key in _character_lora_keys(character):
            if any(str(item.get("key") or "") == lora_key for item in raw_loras if isinstance(item, dict)):
                continue
            entry = get_lora(lora_key)
            if not entry:
                continue
            workflow_for_lora = _workflow_for_lora(selected_workflow, entry, is_admin=is_admin)
            if not workflow_for_lora:
                continue
            selected_workflow = workflow_for_lora
            try:
                weight = float(character.get("lora_weight") or entry.get("default_weight") or 1.0)
            except (TypeError, ValueError):
                weight = float(entry.get("default_weight") or 1.0)
            raw_loras.append({"key": lora_key, "weight": weight})
            fallback_level = "character_lora"
            forced = True
            break

    # 最终校正：如果未命中人物专属工作流，且当前 workflow 仍是 anima_owner，
    # 则替换为 Smart Agent 默认通用工作流（Agent用.json）
    if not character_workflow_key and selected_workflow == "anima_owner":
        selected_workflow = SMART_AGENT_DEFAULT_WORKFLOW_KEY
        if fallback_level in ("none", "character_tags"):
            forced = True

    clean_loras = sanitize_loras(raw_loras, selected_workflow)
    final_prompt, removed_count = assemble_character_prompt_with_count(
        character=selected_character,
        scene_prompt=positive_prompt,
        user_text=request_text,
    )
    return {
        "workflow_key": selected_workflow,
        "positive_prompt": final_prompt,
        "loras": clean_loras,
        "forced": forced,
        "fallback_level": fallback_level,
        "character_workflow_key": character_workflow_key,
        "allow_external_lora": allow_external_lora,
        "character_tag_injected": bool([tag for tag in matched_tags if str(tag).strip()]),
        "locked_character_tags": locked_character_tags(selected_character),
        "foreign_character_tags_removed_count": removed_count,
        "inferred_appearance_tags_removed_count": removed_count,
        "internal_lora_count": 0,
        "external_lora_count": len(clean_loras),
        "workflow_health_status": str((get_workflow(selected_workflow, is_admin=is_admin) or {}).get("health_status") or "unknown"),
    }


def public_character_matches(characters: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {
            "key": character_key(item),
            "name_zh": str(item.get("name_zh") or ""),
            "name_en": str(item.get("name_en") or ""),
            "category_en": str(item.get("category_en") or ""),
            "tags": str(item.get("tags") or ""),
        }
        for item in characters
    ]
