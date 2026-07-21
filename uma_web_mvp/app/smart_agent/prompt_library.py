from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

DATA_PATH = Path(__file__).resolve().parents[1] / "data" / "agent_prompt_library.json"


def load_prompt_library() -> list[dict[str, Any]]:
    if not DATA_PATH.exists():
        return []
    try:
        raw = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items = raw.get("items", []) if isinstance(raw, dict) else raw
    return [item for item in items if isinstance(item, dict)]


def search_prompt_snippets(text: str, *, limit: int = 10) -> list[dict[str, Any]]:
    terms = _terms(text)
    if not terms:
        return []
    scored: list[tuple[int, dict[str, Any]]] = []
    for item in load_prompt_library():
        blob = " ".join(str(item.get(key) or "") for key in ("category", "scene", "style", "prompt", "tags", "notes")).lower()
        score = sum(1 for term in terms if term in blob)
        if score:
            scored.append((score, item))
    scored.sort(key=lambda pair: (-pair[0], str(pair[1].get("id") or "")))
    return [item for _, item in scored[:limit]]


def snippets_for_prompt(items: list[dict[str, Any]]) -> str:
    if not items:
        return "- none"
    lines = []
    for item in items[:10]:
        lines.append(
            "- id={id}; category={category}; scene={scene}; style={style}; tags={tags}; prompt={prompt}".format(
                id=str(item.get("id") or ""),
                category=str(item.get("category") or "")[:80],
                scene=str(item.get("scene") or "")[:80],
                style=str(item.get("style") or "")[:80],
                tags=str(item.get("tags") or "")[:160],
                prompt=str(item.get("prompt") or "")[:420],
            )
        )
    return "\n".join(lines)


def _terms(text: str) -> set[str]:
    clean = (text or "").lower()
    latin = re.findall(r"[a-z0-9_]{2,}", clean)
    cjk = re.findall(r"[\u4e00-\u9fff]{2,}", clean)
    short_cjk = []
    for chunk in cjk:
        short_cjk.extend(chunk[i : i + 2] for i in range(max(0, len(chunk) - 1)))
    return set(latin + cjk + short_cjk)
