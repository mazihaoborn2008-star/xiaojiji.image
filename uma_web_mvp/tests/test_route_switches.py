"""Tests for /smart-agent, /smart-agent-legacy routing and navigation config."""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import ensure_schema

TEST_CASE_ROOT = Path(__file__).resolve().parents[1] / "test_data" / "pytest_cases"


def make_case_root() -> Path:
    root = TEST_CASE_ROOT / uuid.uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    return root


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
        "ai_support_enabled": False,
        "smart_agent_enabled": False,
        "smart_agent_legacy_enabled": False,
        "smart_agent_v2_enabled": False,
    }
    data.update(overrides)
    settings = Settings(**data)
    settings.validate_local_isolation()
    ensure_schema(settings)
    return settings


def _make_client(settings: Settings):
    """Create a TestClient with overridden settings."""
    from app.main import app
    from app.config import get_settings
    get_settings.cache_clear()
    app.dependency_overrides.clear()
    app.dependency_overrides[get_settings] = lambda: settings
    client = TestClient(app, raise_server_exceptions=False)
    return client


# ── /smart-agent route tests ──


def test_smart_agent_ai_support_on():
    """AI support on → AI 客服 page"""
    case_root = make_case_root()
    settings = make_settings(case_root, ai_support_enabled=True, smart_agent_enabled=True)
    client = _make_client(settings)
    resp = client.get("/smart-agent")
    assert resp.status_code == 200
    assert "AI 客服" in resp.text or "ai-support" in resp.text


def test_smart_agent_ai_support_off_smart_agent_on():
    """AI support off + smart agent on → old Smart Agent page"""
    case_root = make_case_root()
    settings = make_settings(case_root, ai_support_enabled=False, smart_agent_enabled=True)
    client = _make_client(settings)
    resp = client.get("/smart-agent")
    assert resp.status_code == 200
    assert "smart-agent" in resp.text.lower() or "智能" in resp.text


def test_smart_agent_both_off():
    """Both off → 404"""
    case_root = make_case_root()
    settings = make_settings(case_root, ai_support_enabled=False, smart_agent_enabled=False)
    client = _make_client(settings)
    resp = client.get("/smart-agent")
    assert resp.status_code == 404


def test_smart_agent_ai_support_on_production():
    """production + AI support on → AI 客服 page (not limited to local)"""
    case_root = make_case_root()
    settings = make_settings(case_root, APP_ENV="production", ai_support_enabled=True, smart_agent_enabled=True)
    client = _make_client(settings)
    resp = client.get("/smart-agent")
    assert resp.status_code == 200
    assert "AI 客服" in resp.text or "ai-support" in resp.text


def test_smart_agent_ai_support_on_overrides_smart_agent():
    """Both on → AI support takes priority"""
    case_root = make_case_root()
    settings = make_settings(case_root, ai_support_enabled=True, smart_agent_enabled=True)
    client = _make_client(settings)
    resp = client.get("/smart-agent")
    assert resp.status_code == 200
    assert "AI 客服" in resp.text or "ai-support" in resp.text


def test_smart_agent_requires_login():
    """Not logged in → redirect to login"""
    case_root = make_case_root()
    settings = make_settings(case_root, ai_support_enabled=True, dev_auth_bypass=False)
    client = _make_client(settings)
    resp = client.get("/smart-agent", follow_redirects=False)
    # Should redirect to login
    assert resp.status_code in (302, 307, 401)


# ── /smart-agent-legacy route tests ──


def test_legacy_local_dev_bypass():
    """local + dev_auth_bypass + legacy enabled → accessible"""
    case_root = make_case_root()
    settings = make_settings(case_root, smart_agent_legacy_enabled=True, smart_agent_enabled=True)
    client = _make_client(settings)
    resp = client.get("/smart-agent-legacy")
    assert resp.status_code == 200
    assert "smart-agent" in resp.text.lower()


def test_legacy_production():
    """production → 404"""
    case_root = make_case_root()
    settings = make_settings(case_root, APP_ENV="production", smart_agent_legacy_enabled=True, dev_auth_bypass=True)
    client = _make_client(settings)
    resp = client.get("/smart-agent-legacy")
    assert resp.status_code == 404


def test_legacy_disabled():
    """legacy disabled → 404"""
    case_root = make_case_root()
    settings = make_settings(case_root, smart_agent_legacy_enabled=False, smart_agent_enabled=True)
    client = _make_client(settings)
    resp = client.get("/smart-agent-legacy")
    assert resp.status_code == 404


def test_legacy_requires_login():
    """Not logged in → redirect to login"""
    case_root = make_case_root()
    settings = make_settings(case_root, smart_agent_legacy_enabled=True, dev_auth_bypass=False)
    client = _make_client(settings)
    resp = client.get("/smart-agent-legacy", follow_redirects=False)
    assert resp.status_code in (302, 307, 401)


# ── API config tests ──


def test_api_me_returns_smart_agent_enabled():
    """/api/me returns smart_agent_enabled field"""
    case_root = make_case_root()
    settings = make_settings(case_root, smart_agent_enabled=True)
    client = _make_client(settings)
    resp = client.get("/api/me")
    assert resp.status_code == 200
    data = resp.json()
    assert "smart_agent_enabled" in data
    assert data["smart_agent_enabled"] is True


def test_api_me_returns_ai_support_enabled():
    """/api/me returns ai_support_enabled field"""
    case_root = make_case_root()
    settings = make_settings(case_root, ai_support_enabled=True)
    client = _make_client(settings)
    resp = client.get("/api/me")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ai_support_enabled"] is True


# ── AI support API switch tests ──


def test_ai_support_api_disabled():
    """AI support disabled → API returns 404"""
    case_root = make_case_root()
    settings = make_settings(case_root, ai_support_enabled=False)
    client = _make_client(settings)
    resp = client.post("/api/ai-support/conversations", json={})
    assert resp.status_code == 404


def test_ai_support_api_enabled():
    """AI support enabled → API enters auth flow (may 403/401 but not 404 for switch)"""
    case_root = make_case_root()
    settings = make_settings(case_root, ai_support_enabled=True)
    client = _make_client(settings)
    resp = client.post("/api/ai-support/conversations", json={})
    # With dev_auth_bypass, it should work (200) or at least not be 404
    assert resp.status_code != 404


# ── Smart Agent API switch tests ──


def test_smart_agent_api_disabled():
    """Smart agent disabled → API returns 403"""
    case_root = make_case_root()
    settings = make_settings(case_root, smart_agent_enabled=False)
    client = _make_client(settings)
    resp = client.post("/api/smart-agent/tasks", json={"request": "test"})
    assert resp.status_code == 403


def test_smart_agent_api_enabled():
    """Smart agent enabled → API enters normal flow"""
    case_root = make_case_root()
    settings = make_settings(case_root, smart_agent_enabled=True)
    client = _make_client(settings)
    resp = client.post("/api/smart-agent/tasks", json={"request": "test"})
    # Should not be 403 (may be 400/402/503 for other reasons)
    assert resp.status_code != 403


def test_ai_support_on_does_not_enable_smart_agent_api():
    """AI support on does NOT auto-enable old Smart Agent API"""
    case_root = make_case_root()
    settings = make_settings(case_root, ai_support_enabled=True, smart_agent_enabled=False)
    client = _make_client(settings)
    resp = client.post("/api/smart-agent/tasks", json={"request": "test"})
    assert resp.status_code == 403


def test_smart_agent_on_does_not_enable_ai_support_api():
    """Smart agent on does NOT auto-enable AI support API"""
    case_root = make_case_root()
    settings = make_settings(case_root, smart_agent_enabled=True, ai_support_enabled=False)
    client = _make_client(settings)
    resp = client.post("/api/ai-support/conversations", json={})
    assert resp.status_code == 404
