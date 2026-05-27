"""Regression tests for src/config.py — timeout_limit_minutes was removed."""

import pytest

from config import Config


@pytest.fixture
def required_env(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    # Force env-only loading: ignore any .env that happens to be on disk.
    monkeypatch.setattr(Config, "model_config", {"extra": "ignore"})


class TestConfig:
    def test_loads_required_tokens(self, required_env):
        cfg = Config()
        assert cfg.slack_bot_token == "xoxb-test"
        assert cfg.slack_app_token == "xapp-test"

    def test_no_timeout_limit_minutes_attribute(self, required_env):
        cfg = Config()
        assert not hasattr(cfg, "timeout_limit_minutes")

    def test_no_timeout_limit_minutes_field(self):
        assert "timeout_limit_minutes" not in Config.model_fields
