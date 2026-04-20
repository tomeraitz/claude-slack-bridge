"""Unit tests for src/security.py — covers every SECURITY_* knob."""

import logging

import pytest

from security import (
    AccessControl,
    SecurityConfig,
    _parse_bool,
    _parse_id_set,
)


# ---------- parser helpers ----------

class TestParseBool:
    @pytest.mark.parametrize("value", ["true", "True", "TRUE", "yes", "1", " true "])
    def test_truthy(self, value):
        assert _parse_bool(value, default=False) is True

    @pytest.mark.parametrize("value", ["false", "False", "no", "0", " false "])
    def test_falsy(self, value):
        assert _parse_bool(value, default=True) is False

    @pytest.mark.parametrize("value", ["maybe", "", "junk"])
    def test_invalid_returns_default(self, value):
        assert _parse_bool(value, default=True) is True
        assert _parse_bool(value, default=False) is False


class TestParseIdSet:
    def test_empty(self):
        assert _parse_id_set("") == set()

    def test_single(self):
        assert _parse_id_set("U1") == {"U1"}

    def test_multiple(self):
        assert _parse_id_set("U1,U2,U3") == {"U1", "U2", "U3"}

    def test_trims_whitespace(self):
        assert _parse_id_set("  U1 , U2  ,U3  ") == {"U1", "U2", "U3"}

    def test_dedupes(self):
        assert _parse_id_set("U1,U1,U2") == {"U1", "U2"}

    def test_skips_empty_tokens(self):
        assert _parse_id_set(",,U1,,,U2,") == {"U1", "U2"}


# ---------- SecurityConfig.from_env ----------

SECURITY_ENV_VARS = [
    "SECURITY_ENABLED",
    "SECURITY_STRICT_MODE",
    "SECURITY_ALLOWED_USERS",
    "SECURITY_ALLOWED_CHANNELS",
    "SECURITY_ADMIN_USERS",
    "SECURITY_REJECTION_MESSAGE",
    "SECURITY_LOG_UNAUTHORIZED",
]


class TestSecurityConfigFromEnv:
    def test_all_defaults_when_env_empty(self, monkeypatch):
        for key in SECURITY_ENV_VARS:
            monkeypatch.delenv(key, raising=False)
        cfg = SecurityConfig.from_env()
        assert cfg.enabled is False
        assert cfg.strict_mode is False
        assert cfg.allowed_users == set()
        assert cfg.allowed_channels == set()
        assert cfg.admin_users == set()
        assert cfg.rejection_message == "You are not authorized to use this bot."
        assert cfg.log_unauthorized is True

    def test_reads_all_values(self, monkeypatch):
        monkeypatch.setenv("SECURITY_ENABLED", "true")
        monkeypatch.setenv("SECURITY_STRICT_MODE", "true")
        monkeypatch.setenv("SECURITY_ALLOWED_USERS", "U1, U2")
        monkeypatch.setenv("SECURITY_ALLOWED_CHANNELS", "C1")
        monkeypatch.setenv("SECURITY_ADMIN_USERS", "U9")
        monkeypatch.setenv("SECURITY_REJECTION_MESSAGE", "Nope.")
        monkeypatch.setenv("SECURITY_LOG_UNAUTHORIZED", "false")
        cfg = SecurityConfig.from_env()
        assert cfg.enabled is True
        assert cfg.strict_mode is True
        assert cfg.allowed_users == {"U1", "U2"}
        assert cfg.allowed_channels == {"C1"}
        assert cfg.admin_users == {"U9"}
        assert cfg.rejection_message == "Nope."
        assert cfg.log_unauthorized is False


# ---------- AccessControl.is_allowed ----------

def _ac(**kwargs) -> AccessControl:
    return AccessControl(SecurityConfig(**kwargs))


class TestDisabled:
    def test_disabled_always_allows(self):
        assert _ac(enabled=False).is_allowed("U-anyone", "C-anything") is True

    def test_disabled_ignores_strict_and_empty_lists(self):
        assert _ac(enabled=False, strict_mode=True).is_allowed("U1", "C1") is True


class TestFlexibleMode:
    def test_empty_lists_allow_all(self):
        assert _ac(enabled=True).is_allowed("U-random", "C-random") is True

    def test_user_listed_channel_empty_allows(self):
        ac = _ac(enabled=True, allowed_users={"U1"})
        assert ac.is_allowed("U1", "C-random") is True

    def test_user_not_listed_denies(self):
        ac = _ac(enabled=True, allowed_users={"U1"})
        assert ac.is_allowed("U-other", "C1") is False

    def test_channel_not_listed_denies(self):
        ac = _ac(enabled=True, allowed_channels={"C1"})
        assert ac.is_allowed("U1", "C-other") is False

    def test_both_listed_and_matching_allows(self):
        ac = _ac(enabled=True, allowed_users={"U1"}, allowed_channels={"C1"})
        assert ac.is_allowed("U1", "C1") is True


class TestStrictMode:
    def test_empty_user_allowlist_denies(self):
        ac = _ac(enabled=True, strict_mode=True)
        assert ac.is_allowed("U1", "C1") is False

    def test_user_listed_but_empty_channel_allowlist_denies(self):
        ac = _ac(enabled=True, strict_mode=True, allowed_users={"U1"})
        assert ac.is_allowed("U1", "C1") is False

    def test_both_populated_and_matching_allows(self):
        ac = _ac(
            enabled=True, strict_mode=True,
            allowed_users={"U1"}, allowed_channels={"C1"},
        )
        assert ac.is_allowed("U1", "C1") is True

    def test_unlisted_user_denied(self):
        ac = _ac(
            enabled=True, strict_mode=True,
            allowed_users={"U1"}, allowed_channels={"C1"},
        )
        assert ac.is_allowed("U2", "C1") is False

    def test_unlisted_channel_denied(self):
        ac = _ac(
            enabled=True, strict_mode=True,
            allowed_users={"U1"}, allowed_channels={"C1"},
        )
        assert ac.is_allowed("U1", "C2") is False


class TestAdminUsers:
    def test_admin_bypasses_channel_allowlist(self):
        ac = _ac(
            enabled=True,
            allowed_users={"U-admin"},
            allowed_channels={"C1"},
            admin_users={"U-admin"},
        )
        assert ac.is_allowed("U-admin", "C-other") is True

    def test_admin_still_subject_to_user_allowlist(self):
        ac = _ac(
            enabled=True,
            allowed_users={"U-someone-else"},
            admin_users={"U-admin"},
        )
        assert ac.is_allowed("U-admin", "C1") is False

    def test_admin_flexible_mode_empty_user_list_allows(self):
        # Flexible + empty user list = allow-all for users, so admin passes.
        ac = _ac(enabled=True, admin_users={"U-admin"})
        assert ac.is_allowed("U-admin", "C-any") is True

    def test_admin_strict_mode_empty_user_list_denied(self):
        ac = _ac(enabled=True, strict_mode=True, admin_users={"U-admin"})
        assert ac.is_allowed("U-admin", "C1") is False

    def test_admin_strict_mode_listed_user_empty_channel_list_allows(self):
        # Channel check is skipped for admins, so empty strict channel list
        # does not deny the admin.
        ac = _ac(
            enabled=True, strict_mode=True,
            allowed_users={"U-admin"}, admin_users={"U-admin"},
        )
        assert ac.is_allowed("U-admin", "C-anything") is True

    def test_non_admin_unaffected_by_admin_list(self):
        ac = _ac(
            enabled=True,
            allowed_channels={"C1"},
            admin_users={"U-admin"},
        )
        assert ac.is_allowed("U-random", "C-other") is False


# ---------- rejection_message ----------

class TestRejectionMessage:
    def test_default(self):
        assert _ac(enabled=True).rejection_message() == \
            "You are not authorized to use this bot."

    def test_custom(self):
        assert _ac(enabled=True, rejection_message="Go away.").rejection_message() \
            == "Go away."


# ---------- denial logging ----------

class TestDenialLogging:
    def test_logs_when_enabled(self, caplog):
        ac = _ac(enabled=True, strict_mode=True, log_unauthorized=True)
        with caplog.at_level(logging.WARNING, logger="security"):
            ac.is_allowed("U1", "C1")
        assert any("Access denied" in r.message for r in caplog.records)

    def test_silent_when_log_disabled(self, caplog):
        ac = _ac(enabled=True, strict_mode=True, log_unauthorized=False)
        with caplog.at_level(logging.WARNING, logger="security"):
            ac.is_allowed("U1", "C1")
        assert not any("Access denied" in r.message for r in caplog.records)
