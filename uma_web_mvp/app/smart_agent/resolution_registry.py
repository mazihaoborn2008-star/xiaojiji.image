"""Allowed resolution presets for Smart Agent image generation.

DS returns a resolution_key; the backend maps it to a concrete width×height.
This prevents arbitrary resolution injection and ensures VRAM-safe dimensions.
"""

from __future__ import annotations

from typing import Any

ALLOWED_RESOLUTIONS: dict[str, dict[str, Any]] = {
    "portrait_1024x1536": {
        "width": 1024,
        "height": 1536,
        "label_zh": "竖图 1024×1536",
        "label_en": "Portrait 1024×1536",
        "description_zh": "适合角色立绘、海报、单人图",
        "description_en": "Best for character art, posters, single-subject",
    },
    "square_1024": {
        "width": 1024,
        "height": 1024,
        "label_zh": "正方形 1024×1024",
        "label_en": "Square 1024×1024",
        "description_zh": "适合头像、构图居中",
        "description_en": "Best for avatars, centered composition",
    },
    "landscape_1536x1024": {
        "width": 1536,
        "height": 1024,
        "label_zh": "横图 1536×1024",
        "label_en": "Landscape 1536×1024",
        "description_zh": "适合场景、横版壁纸",
        "description_en": "Best for landscapes, horizontal wallpapers",
    },
    "vertical_832x1216": {
        "width": 832,
        "height": 1216,
        "label_zh": "竖图 832×1216",
        "label_en": "Vertical 832×1216",
        "description_zh": "适合省资源竖图",
        "description_en": "VRAM-efficient vertical",
    },
    "large_1536x1356": {
        "width": 1536,
        "height": 1356,
        "label_zh": "大图 1536×1356",
        "label_en": "Large 1536×1356",
        "description_zh": "适合高细节场景",
        "description_en": "Best for detailed scenes",
    },
}

DEFAULT_RESOLUTION_KEY = "portrait_1024x1536"


def get_resolution(resolution_key: str) -> dict[str, Any] | None:
    """Return the resolution dict for a given key, or None if invalid."""
    return ALLOWED_RESOLUTIONS.get(resolution_key)


def get_resolution_or_default(resolution_key: str) -> dict[str, Any]:
    """Return the resolution dict, falling back to default if key is invalid."""
    return ALLOWED_RESOLUTIONS.get(resolution_key, ALLOWED_RESOLUTIONS[DEFAULT_RESOLUTION_KEY])


def resolution_key_from_hints(hints: list[str]) -> str:
    """Heuristic: choose a resolution_key based on user hint keywords.

    hints: list of lowercase keywords like ['avatar', 'icon', 'pfp']
    """
    combined = " ".join(hints).lower()

    avatar_keywords = {"avatar", "icon", "pfp", "头像", "profile picture"}
    portrait_keywords = {"portrait", "vertical", "竖", "poster", "海报", "手机壁纸", "phone wallpaper", "mobile wallpaper", "立绘", "单人"}
    landscape_keywords = {"landscape", "horizontal", "横", "scene", "场景", "风景", "wallpaper", "壁纸"}

    if any(kw in combined for kw in avatar_keywords):
        return "square_1024"
    if any(kw in combined for kw in landscape_keywords):
        return "landscape_1536x1024"
    if any(kw in combined for kw in portrait_keywords):
        return "portrait_1024x1536"

    return DEFAULT_RESOLUTION_KEY


def resolution_summaries() -> str:
    """Return a text summary of allowed resolutions for the DS system prompt."""
    lines = []
    for key, res in ALLOWED_RESOLUTIONS.items():
        lines.append(
            f"  {key}: {res['width']}×{res['height']} — {res['description_zh']} / {res['description_en']}"
        )
    return "\n".join(lines)
