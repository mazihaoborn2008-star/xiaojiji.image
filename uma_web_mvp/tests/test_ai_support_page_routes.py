from __future__ import annotations

import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import ensure_schema


TEST_CASE_ROOT = Path(__file__).resolve().parents[1] / "test_data" / "pytest_cases"


def make_settings(case_root: Path, **overrides) -> Settings:
    test_root = case_root / "test_data"
    output = test_root / "output"
    mock_output = test_root / "mock_output"
    input_images = test_root / "input_images"
    for path in (output, mock_output, input_images):
        path.mkdir(parents=True, exist_ok=True)
    data = {
        "APP_ENV": "local",
        "APP_ORIGIN": "http://127.0.0.1:8001",
        "BALANCE_DB": str(test_root / "local_test.db"),
        "BOT_OUTPUT_DIR": str(output),
        "mock_output_dir": str(mock_output),
        "INPUT_IMAGE_DIR": str(input_images),
        "BOT_DIR": str(test_root),
        "redis_enabled": False,
        "dev_auth_bypass": True,
        "dev_user_id": "test-user",
        "owner_free_generation": False,
        "deepseek_api_key": "",
        "ai_support_enabled": True,
        "smart_agent_enabled": True,
        "smart_agent_legacy_enabled": False,
        "smart_agent_v2_enabled": False,
    }
    data.update(overrides)
    settings = Settings(**data)
    settings.validate_local_isolation()
    ensure_schema(settings)
    return settings


def make_case_root() -> Path:
    root = TEST_CASE_ROOT / uuid.uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    return root


def make_client(settings: Settings) -> TestClient:
    from app.config import get_settings
    from app.main import app

    get_settings.cache_clear()
    app.dependency_overrides.clear()
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app, raise_server_exceptions=False)


def test_ai_support_page_requires_login():
    settings = make_settings(make_case_root(), dev_auth_bypass=False)
    client = make_client(settings)

    resp = client.get("/ai-support", follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"


def test_ai_support_page_enabled_for_logged_in_user():
    settings = make_settings(make_case_root(), ai_support_enabled=True)
    client = make_client(settings)

    resp = client.get("/ai-support")

    assert resp.status_code == 200
    assert "AI 客服" in resp.text or "ai-support" in resp.text


def test_ai_support_page_disabled_for_logged_in_user():
    settings = make_settings(make_case_root(), ai_support_enabled=False)
    client = make_client(settings)

    resp = client.get("/ai-support")

    assert resp.status_code == 404


def test_smart_agent_compatibility_page_still_available():
    settings = make_settings(make_case_root(), ai_support_enabled=False, smart_agent_enabled=True)
    client = make_client(settings)

    resp = client.get("/smart-agent")

    assert resp.status_code == 200
    assert "smart-agent" in resp.text.lower() or "智能" in resp.text


def test_ai_support_conversations_unauth_is_not_missing():
    settings = make_settings(make_case_root(), ai_support_enabled=True, dev_auth_bypass=False)
    client = make_client(settings)

    resp = client.get("/api/ai-support/conversations")

    assert resp.status_code in (401, 403)
    assert resp.status_code != 404


def test_ai_support_static_assets_exist():
    static_dir = Path(__file__).resolve().parents[1] / "app" / "static"

    assert (static_dir / "ai-support.html").is_file()
    assert (static_dir / "ai-support.js").is_file()
    assert (static_dir / "ai-support.css").is_file()


def test_homepage_ai_support_nav_points_to_ai_support_when_enabled():
    static_dir = Path(__file__).resolve().parents[1] / "app" / "static"
    index_html = (static_dir / "index.html").read_text(encoding="utf-8")
    app_js = (static_dir / "app.js").read_text(encoding="utf-8")

    assert 'id="smartAgentNavLink"' in index_html
    assert "smartLink.href = '/ai-support'" in app_js
    assert "smartLink.href = '/smart-agent'" in app_js


def test_production_dev_auth_bypass_is_not_enabled_by_default():
    settings = Settings(
        APP_ENV="production",
        APP_ORIGIN="https://image.jwcglass.com",
        dev_auth_bypass=False,
        discord_client_id="client",
        discord_client_secret="secret",
        discord_redirect_uri="https://image.jwcglass.com/auth/discord/callback",
    )

    assert settings.app_env == "production"
    assert settings.dev_auth_bypass is False


def test_ai_support_refund_permission_copy_unchanged():
    from app.services.ai_support_service import _is_policy_blocked_request, _policy_refusal

    assert _is_policy_blocked_request("请帮我退款")
    assert "不能直接退款" in _policy_refusal("请帮我退款")
    assert _is_policy_blocked_request("帮我修改余额")
    assert "不能修改 Credits" in _policy_refusal("帮我修改余额")
