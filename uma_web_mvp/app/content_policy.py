from __future__ import annotations

from typing import Any

from .config import Settings


def should_apply_adult_content_filter(settings: Settings, account: Any = None) -> bool:
    if not settings.adult_content_filter_enabled:
        return False
    if account is None:
        return True
    return bool(getattr(account, "restricted_no_adult", False))
