"""
全库人物歧义索引 — 启动时构建，支持 O(1) mention → identity_key 查询。

索引类型：
1. 中文完整名索引
2. 中文 alias 索引
3. 英文完整名索引
4. 英文 alias 索引
5. 名称包含关系索引（中文短名 → 多 identity）
6. 英文短名索引（词序变化）

规则：
- 同一人物多数据源已在 load_characters() 中合并
- 索引值 = set[identity_key]（一个 mention 可对应多个角色）
- 长名称优先但不吞同名歧义
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import defaultdict
from typing import Any

from .character_search import load_characters, _canonical_name_key, _normalize_cjk_text, _split_aliases

# ── 人物统一身份记录 ──
_IDENTITY_RECORDS: list[dict[str, Any]] = []
_IDENTITY_MAP: dict[str, dict[str, Any]] = {}  # identity_key → record


def _build_identity_records() -> list[dict[str, Any]]:
    """从 load_characters() 建立统一身份记录列表。"""
    chars = load_characters()
    records: list[dict[str, Any]] = []
    seen_identity: dict[str, int] = {}

    for item in chars:
        key = str(item.get("key") or "")
        name_en = str(item.get("name_en") or "")
        name_zh = str(item.get("name_zh") or "")
        identity_key = _canonical_name_key(key or name_en).replace(" ", "_")

        if identity_key in seen_identity:
            # 合并 alias/tags 到已有记录
            idx = seen_identity[identity_key]
            existing = records[idx]
            existing_aliases = set(_split_aliases(existing.get("aliases", "")))
            for alias in _split_aliases(item.get("aliases", "")):
                existing_aliases.add(alias)
            if existing_aliases:
                existing["aliases"] = ",".join(existing_aliases)
            continue

        idx = len(records)
        seen_identity[identity_key] = idx

        category_zh = str(item.get("category_zh") or "")
        category_en = str(item.get("category_en") or "")
        aliases = _split_aliases(item.get("aliases", ""))
        tags = str(item.get("tags") or "")

        # 提取 identity_tags（角色专有 tag，非作品/外观/通用 tag）
        identity_tags: list[str] = []
        from .character_search import _extract_identity_tags
        identity_tags = _extract_identity_tags(item)

        record = {
            "identity_key": identity_key,
            "character_key": key,
            "name_zh": name_zh,
            "name_en": name_en,
            "aliases": aliases,
            "identity_tags": identity_tags,
            "franchise_zh": category_zh,
            "franchise_en": category_en,
            "category_key": _canonical_name_key(category_en).replace(" ", "_"),
            "source": item.get("source", ""),
            "tags": tags,
        }
        records.append(record)

    return records


# ── 索引结构 ──
# 所有索引都是 mention_key → set[identity_key]
# CJK 索引用 _normalize_cjk_text 作为键
# 英文索引用 _canonical_name_key 作为键

_ZH_NAME_INDEX: dict[str, set[str]] = defaultdict(set)       # name_zh → identities
_ZH_ALIAS_INDEX: dict[str, set[str]] = defaultdict(set)      # zh_alias → identities
_EN_NAME_INDEX: dict[str, set[str]] = defaultdict(set)       # name_en → identities
_EN_ALIAS_INDEX: dict[str, set[str]] = defaultdict(set)      # en_alias → identities
_IDENTITY_TAG_INDEX: dict[str, set[str]] = defaultdict(set)  # identity_tag → identities
_FRANCHISE_INDEX: dict[str, set[str]] = defaultdict(set)     # franchise → identities

# 名称包含关系索引：短名 → 包含该短名的长名 identity
_ZH_SUBSTRING_INDEX: dict[str, set[str]] = defaultdict(set)  # short_cjk → identities

# 英文短名/词序索引
_EN_SHORT_INDEX: dict[str, set[str]] = defaultdict(set)      # single_word → identities

# 完全重名组：mention → [identity_keys]
_DUPLICATE_GROUPS: dict[str, list[str]] = {}

# 全库统计
_INDEX_STATS: dict[str, Any] = {}


def _normalize_zh_key(text: str) -> str:
    """CJK 标准化：去标点、去空格、小写"""
    return _normalize_cjk_text(str(text or ""))


def _normalize_en_key(text: str) -> str:
    """英文标准化"""
    return _canonical_name_key(str(text or ""))


def _build_all_indexes() -> None:
    """启动时调用一次，从统一人物记录构建所有索引。"""
    global _ZH_NAME_INDEX, _ZH_ALIAS_INDEX, _EN_NAME_INDEX, _EN_ALIAS_INDEX
    global _IDENTITY_TAG_INDEX, _FRANCHISE_INDEX
    global _ZH_SUBSTRING_INDEX, _EN_SHORT_INDEX
    global _DUPLICATE_GROUPS, _INDEX_STATS, _IDENTITY_RECORDS

    _IDENTITY_RECORDS = _build_identity_records()
    records = _IDENTITY_RECORDS

    # 重置
    _ZH_NAME_INDEX = defaultdict(set)
    _ZH_ALIAS_INDEX = defaultdict(set)
    _EN_NAME_INDEX = defaultdict(set)
    _EN_ALIAS_INDEX = defaultdict(set)
    _IDENTITY_TAG_INDEX = defaultdict(set)
    _FRANCHISE_INDEX = defaultdict(set)
    _ZH_SUBSTRING_INDEX = defaultdict(set)
    _EN_SHORT_INDEX = defaultdict(set)
    _DUPLICATE_GROUPS = {}
    _INDEX_STATS = {}

    for rec in records:
        ik = rec["identity_key"]

        # 1. 中文完整名索引
        if rec["name_zh"]:
            key = _normalize_zh_key(rec["name_zh"])
            _ZH_NAME_INDEX[key].add(ik)

        # 2. 英文完整名索引
        if rec["name_en"]:
            key = _normalize_en_key(rec["name_en"])
            _EN_NAME_INDEX[key].add(ik)

        # 3. Alias 索引（区分中英文）
        for alias in rec["aliases"]:
            alias_str = str(alias).strip()
            if not alias_str:
                continue
            if any("\u4e00" <= ch <= "\u9fff" for ch in alias_str):
                key = _normalize_zh_key(alias_str)
                if key:
                    _ZH_ALIAS_INDEX[key].add(ik)
            else:
                key = _normalize_en_key(alias_str)
                if key:
                    _EN_ALIAS_INDEX[key].add(ik)

        # 4. Identity tag 索引
        for tag in rec["identity_tags"]:
            key = _normalize_en_key(tag)
            if key:
                _IDENTITY_TAG_INDEX[key].add(ik)

        # 5. 作品索引
        for field_val in (rec["franchise_zh"], rec["franchise_en"]):
            if field_val:
                key = _normalize_en_key(field_val)
                if key:
                    _FRANCHISE_INDEX[key].add(ik)

    # 6. 中文名称包含关系索引
    # 从所有 zh_keys 中提取 2-4 字短子串，查询哪些 key 包含该子串
    all_zh_keys = set()
    all_zh_keys.update(_ZH_NAME_INDEX.keys())
    all_zh_keys.update(_ZH_ALIAS_INDEX.keys())

    # 生成所有可能的 2-4 字短子串
    sub_candidates: dict[str, set[str]] = {}
    for zh_key in all_zh_keys:
        if len(zh_key) < 2:
            continue
        for win_size in (2, 3, 4):
            for i in range(len(zh_key) - win_size + 1):
                sub = zh_key[i:i + win_size]
                if len(sub) >= 2:
                    sub_candidates.setdefault(sub, set()).add(zh_key)

    # 检查每个短子串是否匹配到多个不同的 zh_key（来自不同 identity）
    for short_zh, matching_keys in sub_candidates.items():
        identities_found: set[str] = set()
        for full_zh in matching_keys:
            identities_found.update(_ZH_NAME_INDEX.get(full_zh, set()))
            identities_found.update(_ZH_ALIAS_INDEX.get(full_zh, set()))
        if len(identities_found) >= 2:
            _ZH_SUBSTRING_INDEX[short_zh] = identities_found

    # 7. 英文短名索引（单英文词 → 多词人物名中的该词）
    all_en_keys: list[str] = []
    all_en_keys.extend(_EN_NAME_INDEX.keys())
    all_en_keys.extend(_EN_ALIAS_INDEX.keys())
    for en_key in all_en_keys:
        words = en_key.split()
        for word in words:
            if len(word) >= 3:
                _EN_SHORT_INDEX[word].add(en_key)

    # 8. 完全重名组检测
    for key, identities in _ZH_NAME_INDEX.items():
        if len(identities) >= 2:
            _DUPLICATE_GROUPS[key] = sorted(identities)
    for key, identities in _EN_NAME_INDEX.items():
        if len(identities) >= 2:
            _DUPLICATE_GROUPS[key] = sorted(identities)
    for key, identities in _ZH_ALIAS_INDEX.items():
        if len(identities) >= 2:
            if key not in _DUPLICATE_GROUPS:
                _DUPLICATE_GROUPS[key] = sorted(identities)
    for key, identities in _EN_ALIAS_INDEX.items():
        if len(identities) >= 2:
            if key not in _DUPLICATE_GROUPS:
                _DUPLICATE_GROUPS[key] = sorted(identities)

    # 9. 统计
    total_records = len(records)
    total_identities = len(set(r["identity_key"] for r in records))
    dup_count = len(_DUPLICATE_GROUPS)
    alias_conflict_count = sum(1 for k, v in _ZH_ALIAS_INDEX.items() if len(v) >= 2) + \
                           sum(1 for k, v in _EN_ALIAS_INDEX.items() if len(v) >= 2)
    substring_groups = len(_ZH_SUBSTRING_INDEX)
    en_short_conflicts = sum(1 for k, v in _EN_SHORT_INDEX.items() if len(v) >= 2)

    max_dup_size = max((len(v) for v in _DUPLICATE_GROUPS.values()), default=0)
    max_sub_size = max((len(v) for v in _ZH_SUBSTRING_INDEX.values()), default=0)

    _INDEX_STATS = {
        "total_characters": total_records,
        "total_identities": total_identities,
        "duplicate_exact_groups": dup_count,
        "alias_conflict_groups": alias_conflict_count,
        "zh_substring_groups": substring_groups,
        "en_short_conflict_groups": en_short_conflicts,
        "max_duplicate_size": max_dup_size,
        "max_substring_size": max_sub_size,
        "duplicate_examples": [
            {"mention": k, "identities": list(v)}
            for k, v in sorted(_DUPLICATE_GROUPS.items(), key=lambda x: -len(x[1]))[:5]
        ],
        "substring_examples": [
            {"mention": k, "identities": list(v)}
            for k, v in sorted(_ZH_SUBSTRING_INDEX.items(), key=lambda x: -len(x[1]))[:5]
        ],
    }


def get_identity(identity_key: str) -> dict[str, Any] | None:
    """根据 identity_key 获取统一身份记录。"""
    if not _INDEX_STATS:
        _build_all_indexes()
    for rec in _IDENTITY_RECORDS:
        if rec.get("identity_key") == identity_key:
            return dict(rec)
    return None


def get_index_stats() -> dict[str, Any]:
    """获取全库索引统计。"""
    if not _INDEX_STATS:
        _build_all_indexes()
    return dict(_INDEX_STATS)


def get_duplicate_groups() -> dict[str, list[str]]:
    """获取所有完全重名/alias 冲突组。"""
    if not _INDEX_STATS:
        _build_all_indexes()
    return dict(_DUPLICATE_GROUPS)


def get_zh_substring_groups() -> dict[str, set[str]]:
    """获取所有中文名称包含关系组。"""
    if not _INDEX_STATS:
        _build_all_indexes()
    return dict(_ZH_SUBSTRING_INDEX)


def get_all_identities() -> list[dict[str, Any]]:
    """获取所有统一身份记录。"""
    if not _INDEX_STATS:
        _build_all_indexes()
    return list(_IDENTITY_RECORDS)


# ── mention 匹配 ──

def find_characters_by_mention_v2(text: str, *, limit: int = 12) -> list[dict[str, Any]]:
    """从输入文本中匹配人物（新版：基于全库索引）。

    返回 mention groups 结构：
    [
      {
        "span": [start, end],
        "mention": "麻美",
        "candidates": [{identity_key, character_key, name_zh, name_en, franchise, ...}]
      },
      ...
    ]
    """
    if not _INDEX_STATS:
        _build_all_indexes()

    raw_text = str(text or "").strip()
    if not raw_text:
        return []

    haystack_zh = _normalize_zh_key(raw_text)
    haystack_en = _normalize_en_key(raw_text)

    # 收集所有匹配的 mention groups
    mention_groups: list[dict[str, Any]] = []
    occupied_spans: list[tuple[int, int, str]] = []

    # ── 步骤 1：精确完整名/alias 匹配（CJK 优先） ──
    _match_exact_full_names(raw_text, haystack_zh, haystack_en, mention_groups, occupied_spans)

    # ── 步骤 2：身份 tag 匹配 ──
    _match_identity_tags(raw_text, haystack_en, mention_groups, occupied_spans)

    # ── 步骤 3：短名包含匹配（CJK 子串） ──
    _match_cjk_substrings(raw_text, haystack_zh, mention_groups, occupied_spans)

    # ── 步骤 4：英文短名匹配 ──
    _match_en_short_names(raw_text, haystack_en, mention_groups, occupied_spans)

    # ── 排序：长 span 优先，多候选优先 ──
    # ── 合并重叠/包含的 span groups ──
    mention_groups = _merge_overlapping_groups(mention_groups)

    mention_groups.sort(key=lambda g: (-(g["span"][1] - g["span"][0]), -len(g["candidates"])))

    # ── 限制返回数 ──
    return mention_groups[:limit]


def _merge_overlapping_groups(
    groups: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge groups whose spans overlap (same start or containment).

    When a shorter mention at the same start position matches different
    candidates than the longer mention, merge them into one group.
    Example: "爱丽丝" [2,5] matches {alice, iris}, "爱丽" [2,4] matches
    {tendou_aris, alice_margatroid} → merge to one group with all 4.
    """
    if len(groups) <= 1:
        return groups

    merged: list[dict[str, Any]] = []
    used: set[int] = set()

    for i, g1 in enumerate(groups):
        if i in used:
            continue
        s1, e1 = g1["span"]
        m1 = g1.get("mention", "")
        merged_group = dict(g1)
        merged_candidates: dict[str, dict[str, Any]] = {}
        for c in g1.get("candidates", []):
            ik = c.get("identity_key", "")
            if ik:
                merged_candidates[ik] = c

        for j, g2 in enumerate(groups):
            if j <= i or j in used:
                continue
            s2, e2 = g2["span"]
            m2 = g2.get("mention", "")
            # Same start → merge, but only add candidates that actually
            # contain the LONGER mention in their name/aliases
            if s1 == s2:
                used.add(j)
                longer = m1 if len(m1) >= len(m2) else m2
                for c in g2.get("candidates", []):
                    ik = c.get("identity_key", "")
                    if not ik or ik in merged_candidates:
                        continue
                    # Only merge if the longer mention is relevant to this candidate
                    zh = _normalize_zh_key(c.get("name_zh", ""))
                    if longer in zh:
                        merged_candidates[ik] = c

        merged_group["candidates"] = list(merged_candidates.values())
        merged.append(merged_group)

    return merged


def _match_exact_full_names(
    raw_text: str,
    haystack_zh: str,
    haystack_en: str,
    groups: list[dict[str, Any]],
    occupied_spans: list[tuple[int, int, str]],
) -> None:
    """匹配完整中文名/英文名/alias（精确在输入中出现）。"""
    # CJK 名称
    all_zh_mentions: dict[str, set[str]] = defaultdict(set)
    for key_name in _ZH_NAME_INDEX:
        all_zh_mentions[key_name].update(_ZH_NAME_INDEX[key_name])
    for key_name in _ZH_ALIAS_INDEX:
        all_zh_mentions[key_name].update(_ZH_ALIAS_INDEX[key_name])

    for mention_zh, identities in sorted(all_zh_mentions.items(), key=lambda x: -len(x[0])):
        if len(mention_zh) < 2:
            continue
        if mention_zh not in haystack_zh:
            continue
        start = haystack_zh.find(mention_zh)
        end = start + len(mention_zh)

        # 检查是否被已占用 span 完全覆盖且不属于同 span 歧义
        is_covered = False
        for occ_start, occ_end, occ_mention in occupied_spans:
            if occ_start <= start and end <= occ_end:
                # 同一起始位置 + 更短 → 歧义候选，允许
                if start == occ_start and len(mention_zh) < len(occ_mention):
                    continue
                is_covered = True
                break
        if is_covered:
            continue

        occupied_spans.append((start, end, mention_zh))
        candidates = _build_candidates(identities, mention_zh, "zh_name", groups)
        if candidates:
            groups.append({
                "span": [start, end],
                "mention": mention_zh,
                "candidates": candidates,
                "match_type": "exact_zh",
            })

    # 英文名称
    all_en_mentions: dict[str, set[str]] = defaultdict(set)
    for key_name in _EN_NAME_INDEX:
        all_en_mentions[key_name].update(_EN_NAME_INDEX[key_name])
    for key_name in _EN_ALIAS_INDEX:
        all_en_mentions[key_name].update(_EN_ALIAS_INDEX[key_name])

    for mention_en, identities in sorted(all_en_mentions.items(), key=lambda x: -len(x[0])):
        if len(mention_en) < 2:
            continue
        m = re.search(rf"(?<![a-z0-9]){re.escape(mention_en)}(?![a-z0-9])", haystack_en)
        if not m:
            continue
        start = m.start()
        end = m.end()

        is_covered = False
        for occ_start, occ_end, occ_mention in occupied_spans:
            if occ_start <= start and end <= occ_end:
                if start == occ_start and (end - start) < (occ_end - occ_start):
                    continue
                is_covered = True
                break
        if is_covered:
            continue

        occupied_spans.append((start, end, mention_en))
        candidates = _build_candidates(identities, mention_en, "en_name", groups)
        if candidates:
            groups.append({
                "span": [start, end],
                "mention": mention_en,
                "candidates": candidates,
                "match_type": "exact_en",
            })


def _match_identity_tags(
    raw_text: str,
    haystack_en: str,
    groups: list[dict[str, Any]],
    occupied_spans: list[tuple[int, int, str]],
) -> None:
    """匹配角色专有 identity tag。"""
    for tag, identities in _IDENTITY_TAG_INDEX.items():
        if len(identities) < 1:
            continue
        m = re.search(rf"(?<![a-z0-9]){re.escape(tag)}(?![a-z0-9])", haystack_en)
        if not m:
            # 词序变化：检查所有词是否出现
            words = tag.split()
            if len(words) >= 2 and all(len(w) >= 3 for w in words):
                all_found = all(
                    re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", haystack_en)
                    for w in words
                )
                if not all_found:
                    continue
                start = 0
                end = len(haystack_en)
            else:
                continue
        else:
            start = m.start()
            end = m.end()

        is_covered = False
        for occ_start, occ_end, _ in occupied_spans:
            if occ_start <= start and end <= occ_end:
                is_covered = True
                break
        if is_covered:
            continue

        occupied_spans.append((start, end, tag))
        candidates = _build_candidates(identities, tag, "identity_tag", groups)
        if candidates:
            groups.append({
                "span": [start, end],
                "mention": tag,
                "candidates": candidates,
                "match_type": "tag",
            })


def _match_cjk_substrings(
    raw_text: str,
    haystack_zh: str,
    groups: list[dict[str, Any]],
    occupied_spans: list[tuple[int, int, str]],
) -> None:
    """CJK 短名包含匹配：直接扫描已建立的歧义短名索引。"""
    if not haystack_zh or len(haystack_zh) < 2:
        return

    # Keep complete runs for the exact-name fallback below, then add every
    # known ambiguous short mention that actually occurs in the normalized
    # request.  The old sliding-window path only ran for CJK runs of six or
    # more characters, so a prompt such as "麻美穿风衣" (five characters)
    # silently missed the "麻美" ambiguity.
    cjk_runs = re.findall(r"[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff]{2,}", raw_text)
    cjk_parts: set[str] = set()
    for run in cjk_runs:
        norm = _normalize_zh_key(run)
        if len(norm) >= 2:
            cjk_parts.add(norm)

    for mention in _ZH_SUBSTRING_INDEX:
        if mention and mention in haystack_zh:
            cjk_parts.add(mention)

    # 检查每个 CJK 片段是否对应到子串索引中的 ment
    for cjk_part in sorted(cjk_parts, key=len, reverse=True):
        # 在 _ZH_SUBSTRING_INDEX 中查询
        if cjk_part in _ZH_SUBSTRING_INDEX:
            identities = _ZH_SUBSTRING_INDEX[cjk_part]
            if len(identities) >= 2:
                start = haystack_zh.find(cjk_part)
                if start < 0:
                    continue
                end = start + len(cjk_part)

                # 检查 span 占用
                is_covered = False
                for occ_start, occ_end, _ in occupied_spans:
                    if occ_start <= start and end <= occ_end:
                        if start == occ_start and len(cjk_part) < (occ_end - occ_start):
                            continue
                        is_covered = True
                        break
                if is_covered:
                    continue

                occupied_spans.append((start, end, cjk_part))
                candidates = _build_candidates(identities, cjk_part, "zh_substring", groups)
                if candidates:
                    groups.append({
                        "span": [start, end],
                        "mention": cjk_part,
                        "candidates": candidates,
                        "match_type": "zh_substring",
                    })
            continue

        # 如果不在子串索引中，也检查是否是某个角色名的等长子串
        # （如 "爱丽丝" 直接是一个角色 name_zh，同时也在其他角色名中）
        if cjk_part in _ZH_NAME_INDEX or cjk_part in _ZH_ALIAS_INDEX:
            identities = _ZH_NAME_INDEX.get(cjk_part, set()) | _ZH_ALIAS_INDEX.get(cjk_part, set())
            # 同时检查子串索引：同名 mention 也可能匹配长名角色
            if cjk_part in _ZH_SUBSTRING_INDEX:
                identities = identities | _ZH_SUBSTRING_INDEX[cjk_part]
            start = haystack_zh.find(cjk_part)
            if start < 0 or len(identities) < 1:
                continue
            end = start + len(cjk_part)

            is_covered = False
            for occ_start, occ_end, _ in occupied_spans:
                if occ_start <= start and end <= occ_end:
                    if start == occ_start and len(cjk_part) < (occ_end - occ_start):
                        continue
                    is_covered = True
                    break
            if is_covered:
                continue

            occupied_spans.append((start, end, cjk_part))
            candidates = _build_candidates(identities, cjk_part, "zh_index", groups)
            if candidates:
                groups.append({
                    "span": [start, end],
                    "mention": cjk_part,
                    "candidates": candidates,
                    "match_type": "zh_index",
                })


def _match_en_short_names(
    raw_text: str,
    haystack_en: str,
    groups: list[dict[str, Any]],
    occupied_spans: list[tuple[int, int, str]],
) -> None:
    """英文短名匹配：单英文词 → 多词人物名。"""
    # 提取输入中的英文单词
    words = re.findall(r"[a-zA-Z]{3,}", haystack_en)
    seen_words: set[str] = set()

    for word in words:
        word_lower = word.lower()
        if word_lower in seen_words:
            continue
        seen_words.add(word_lower)

        if word_lower not in _EN_SHORT_INDEX:
            continue
        if len(_EN_SHORT_INDEX[word_lower]) < 2:
            continue

        # 找到该词对应的所有完整 identity
        identities: set[str] = set()
        for full_name in _EN_SHORT_INDEX[word_lower]:
            for idx_map in (_EN_NAME_INDEX, _EN_ALIAS_INDEX):
                identities.update(idx_map.get(full_name, set()))

        if len(identities) < 2:
            continue

        # 在 haystack_en 中定位该词
        m = re.search(rf"(?<![a-z0-9]){re.escape(word_lower)}(?![a-z0-9])", haystack_en)
        if not m:
            continue
        start = m.start()
        end = m.end()

        is_covered = False
        for occ_start, occ_end, _ in occupied_spans:
            if occ_start <= start and end <= occ_end:
                is_covered = True
                break
        if is_covered:
            continue

        occupied_spans.append((start, end, word_lower))
        candidates = _build_candidates(identities, word_lower, "en_short", groups)
        if candidates:
            groups.append({
                "span": [start, end],
                "mention": word_lower,
                "candidates": candidates,
                "match_type": "en_short",
            })


def _build_candidates(
    identity_keys: set[str],
    mention: str,
    match_type: str,
    existing_groups: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """从 identity_keys 构建候选列表（去重已有 groups）。"""
    candidates: list[dict[str, Any]] = []
    seen_ik: set[str] = set()

    # 检查是否已被已有 groups 完全覆盖
    for g in existing_groups:
        for c in g["candidates"]:
            ik = c.get("identity_key", "")
            if ik:
                seen_ik.add(ik)

    for ik in identity_keys:
        if ik in seen_ik:
            continue
        identity = get_identity(ik)
        if not identity:
            continue

        franchise_display = identity.get("franchise_zh", "") or identity.get("franchise_en", "")
        # 过滤通用分类名（不是真实作品名）
        _GENERIC_FRANCHISE = {"其他动漫", "anime", "其他", "other", "vocaloid", "赛马娘"}
        if franchise_display.lower() in _GENERIC_FRANCHISE:
            franchise_display = ""
        if not franchise_display:
            # 尝试从 tags 提取具体的作品/系列名
            tags = identity.get("tags", "")
            identity_tag_keys = {_normalize_en_key(tag) for tag in identity.get("identity_tags", [])}
            identity_tag_keys.add(_normalize_en_key(identity.get("name_en", "")))
            for tag in tags.split(","):
                tag = tag.strip()
                tag_lower = tag.lower().replace("_", " ")
                if tag_lower in {"umamusume", "horse ears", "horse tail", "1girl", "1boy", "solo",
                                  "anime style", "looking at viewer", "animal ears", "tail"}:
                    continue
                if _normalize_en_key(tag) in identity_tag_keys:
                    continue
                # 跳过人物身份 tag（包含括号结构）
                if "(" in tag and ")" in tag:
                    continue
                if tag and len(tag) >= 2:
                    franchise_display = tag
                    break
            if not franchise_display and tags:
                parts = [p.strip() for p in tags.split(",") if p.strip()]
                for p in parts:
                    if "(" not in p and _normalize_en_key(p) not in identity_tag_keys:
                        franchise_display = p
                        break
                if not franchise_display and parts:
                    franchise_display = parts[0]

        candidates.append({
            "identity_key": ik,
            "character_key": identity.get("character_key", ik),
            "name_zh": identity.get("name_zh", ""),
            "name_en": identity.get("name_en", ""),
            "franchise": franchise_display,
            "franchise_en": identity.get("franchise_en", ""),
            "franchise_zh": identity.get("franchise_zh", ""),
            "matched_term": mention,
            "match_type": match_type,
        })
        seen_ik.add(ik)

    return candidates


def resolve_mention_groups_with_franchise(
    groups: list[dict[str, Any]],
    franchise_hints: list[str],
) -> list[dict[str, Any]]:
    """用作品上下文过滤 mention groups 中的候选。

    franchise_hints: 从用户输入提取的作品英文 key 列表
    """
    if not franchise_hints or not groups:
        return groups

    franchise_keys = {_normalize_en_key(f) for f in franchise_hints}
    franchise_keys_no_space = {f.replace(" ", "") for f in franchise_keys}

    result = []
    for g in groups:
        if len(g["candidates"]) <= 1:
            result.append(g)
            continue

        filtered = []
        for c in g["candidates"]:
            f_en = _normalize_en_key(c.get("franchise_en", ""))
            f_zh = _normalize_en_key(c.get("franchise_zh", ""))
            # 检查标签中的作品信息
            tags = str(c.get("tags", "")).lower()
            for fk in franchise_keys | franchise_keys_no_space:
                if fk in f_en or fk in f_zh or fk in tags:
                    filtered.append(c)
                    break
            else:
                # 也检查 franchise_zh 的中文匹配
                fzh_raw = (c.get("franchise_zh", "") or "").lower()
                for hint in franchise_hints:
                    if hint.lower() in fzh_raw:
                        filtered.append(c)
                        break

        if filtered and len(filtered) < len(g["candidates"]):
            g["candidates"] = filtered
        result.append(g)

    return result


def extract_franchise_hints(text: str) -> list[str]:
    """从用户输入中提取作品上下文提示。"""
    if not _INDEX_STATS:
        _build_all_indexes()

    raw = str(text or "").lower()
    hints: list[str] = []

    # 检查已知作品关键词
    _KNOWN_FRANCHISE_ZH = {
        "nikke": "nikke",
        "妮姬": "nikke",
        "胜利女神": "nikke",
        "蔚蓝档案": "blue archive",
        "碧蓝档案": "blue archive",
        "ba": "blue archive",
        "blue archive": "blue archive",
        "东方": "touhou",
        "touhou": "touhou",
        "东方project": "touhou",
        "genshin": "genshin impact",
        "原神": "genshin impact",
        "崩坏": "honkai",
        "honkai": "honkai",
        "租借女友": "kanojo okarishimasu",
        "赛马娘": "umamusume",
        "umamusume": "umamusume",
        "魔法少女小圆": "mahou shoujo madoka magica",
        "小圆": "mahou shoujo madoka magica",
        "madoka": "mahou shoujo madoka magica",
        "fate": "fate",
        "明日方舟": "arknights",
        "arknights": "arknights",
        "vocaloid": "vocaloid",
    }

    for pattern, key in sorted(_KNOWN_FRANCHISE_ZH.items(), key=lambda x: -len(x[0])):
        if pattern in raw:
            hints.append(key)
            # 从 raw 中移除已匹配的部分（避免重复）
            raw = raw.replace(pattern, " ", 1)

    return hints


# ── 初始化 ──
_build_all_indexes()
