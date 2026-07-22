from __future__ import annotations

from typing import Any

from app.config import Settings
from app.smart_agent.deepseek_client import DeepSeekError, complete_json


class DeepSeekService:
    def __init__(self, settings: Settings, *, mock_response: dict[str, Any] | None = None):
        self.settings = settings
        self.mock_response = mock_response

    async def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        timeout_seconds: int | None = None,
        purpose: str = "generic",
        mock_response: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        mock = mock_response or self.mock_response
        local_placeholder = str(self.settings.deepseek_api_key or "").startswith("TEST_ONLY")
        if mock is None and self.settings.is_local_env() and (not self.settings.deepseek_api_key or local_placeholder):
            mock = _local_mock_response(user_prompt)
        return await complete_json(
            self.settings,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
            purpose=purpose,
            mock_response=mock,
        )


def _local_mock_response(user_prompt: str) -> dict[str, Any]:
    text = str(user_prompt or "").lower()
    result: dict[str, Any] = {
        "clothing": "",
        "action": "",
        "expression": "",
        "composition": "",
        "scene": "",
        "lighting": "",
        "mood": "",
        "style": "anime style",
    }
    if "校服" in user_prompt or "school uniform" in text:
        result["clothing"] = "school uniform, skirt, white shirt, tie"
    if "教室" in user_prompt or "classroom" in text:
        result["scene"] = "classroom, desk, window"
        result["lighting"] = "daylight"
    if "卧室" in user_prompt or "bedroom" in text:
        result["scene"] = "bedroom"
        result["lighting"] = "soft lighting"
    if "坐" in user_prompt or "sitting" in text:
        result["action"] = "sitting"
    elif "躺" in user_prompt or "lying" in text:
        result["action"] = "lying"
    else:
        result["action"] = "standing"
    if "嫌弃" in user_prompt:
        result["expression"] = "annoyed expression"
    if "镜头" in user_prompt:
        result["composition"] = "looking at viewer"
    return result


__all__ = ["DeepSeekError", "DeepSeekService"]
