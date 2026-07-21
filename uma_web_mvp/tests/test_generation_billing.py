"""Tests for generation billing logic."""
from __future__ import annotations

from unittest.mock import MagicMock

from app.db import calculate_generation_charge


def _settings(**overrides):
    s = MagicMock()
    s.owner_free_generation = overrides.get("owner_free_generation", False)
    s.owner_user_id = overrides.get("owner_user_id", "owner-123")
    s.price_fen_per_image = overrides.get("price_fen_per_image", 1)
    s.agent_surcharge_credits = overrides.get("agent_surcharge_credits", 1)
    return s


class TestCalculateGenerationCharge:
    def test_normal_no_agent(self):
        s = _settings()
        assert calculate_generation_charge(s, user_id="user1", style_key="style_a", use_agent=False) == 1

    def test_normal_with_agent(self):
        s = _settings()
        assert calculate_generation_charge(s, user_id="user1", style_key="style_a", use_agent=True) == 2

    def test_anima_no_agent(self):
        s = _settings()
        assert calculate_generation_charge(s, user_id="user1", style_key="anima_owner", use_agent=False) == 2

    def test_anima_with_agent(self):
        s = _settings()
        assert calculate_generation_charge(s, user_id="user1", style_key="anima_owner", use_agent=True) == 3

    def test_owner_free_with_agent(self):
        s = _settings(owner_free_generation=True, owner_user_id="owner-123")
        assert calculate_generation_charge(s, user_id="owner-123", style_key="style_a", use_agent=True) == 0

    def test_owner_free_anima_with_agent(self):
        s = _settings(owner_free_generation=True, owner_user_id="owner-123")
        assert calculate_generation_charge(s, user_id="owner-123", style_key="anima_owner", use_agent=True) == 0
