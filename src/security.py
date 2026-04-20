"""
security.py — User and channel-based access control for Slack interactions.

Supports user allowlists, channel allowlists, admin bypass, flexible/strict
modes, structured rejection logging, and a configurable rejection message.

All settings are loaded from environment variables so no code changes are
needed to enable or reconfigure access control at runtime.

Environment variables
---------------------
SECURITY_ENABLED            Enable access control; default "false".
SECURITY_STRICT_MODE        Deny when a relevant allowlist is empty; default "false".
SECURITY_ALLOWED_USERS      Comma-separated Slack user IDs that may use the bot.
SECURITY_ALLOWED_CHANNELS   Comma-separated Slack channel IDs that the bot responds in.
SECURITY_ADMIN_USERS        Comma-separated user IDs that bypass channel restrictions.
SECURITY_REJECTION_MESSAGE  Message posted to unauthorized users.
SECURITY_LOG_UNAUTHORIZED   Emit a warning log on each denial; default "true".
"""

import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_TRUTHY = {"1", "true", "yes"}
_FALSY = {"0", "false", "no"}


def _parse_bool(value: str, default: bool) -> bool:
    normalized = value.strip().lower()
    if normalized in _TRUTHY:
        return True
    if normalized in _FALSY:
        return False
    return default


def _parse_id_set(value: str) -> set[str]:
    """Split a comma-separated string into a set of non-empty stripped tokens."""
    return {tok.strip() for tok in value.split(",") if tok.strip()}


@dataclass
class SecurityConfig:
    """Parsed access-control settings."""

    enabled: bool = False
    strict_mode: bool = False
    allowed_users: set[str] = field(default_factory=set)
    allowed_channels: set[str] = field(default_factory=set)
    admin_users: set[str] = field(default_factory=set)
    rejection_message: str = "You are not authorized to use this bot."
    log_unauthorized: bool = True

    @classmethod
    def from_env(cls) -> "SecurityConfig":
        """Construct a SecurityConfig by reading environment variables."""
        cfg = cls(
            enabled=_parse_bool(os.environ.get("SECURITY_ENABLED", "false"), default=False),
            strict_mode=_parse_bool(os.environ.get("SECURITY_STRICT_MODE", "false"), default=False),
            allowed_users=_parse_id_set(os.environ.get("SECURITY_ALLOWED_USERS", "")),
            allowed_channels=_parse_id_set(os.environ.get("SECURITY_ALLOWED_CHANNELS", "")),
            admin_users=_parse_id_set(os.environ.get("SECURITY_ADMIN_USERS", "")),
            rejection_message=os.environ.get(
                "SECURITY_REJECTION_MESSAGE", "You are not authorized to use this bot."
            ),
            log_unauthorized=_parse_bool(
                os.environ.get("SECURITY_LOG_UNAUTHORIZED", "true"), default=True
            ),
        )
        if cfg.enabled:
            logger.info(
                "Access control enabled: strict=%s allowed_users=%d allowed_channels=%d admin_users=%d",
                cfg.strict_mode,
                len(cfg.allowed_users),
                len(cfg.allowed_channels),
                len(cfg.admin_users),
            )
        else:
            logger.info("Access control disabled (SECURITY_ENABLED not set).")
        return cfg


class AccessControl:
    """
    Enforces user and channel allowlists.

    Access logic
    ------------
    - Security disabled  → always allow.
    - Admin user         → passes channel check unconditionally; still subject to
                           the user allowlist (admins must be listed there too, or
                           the user allowlist must be empty).
    - Flexible mode      → a missing allowlist dimension is treated as "allow all"
                           for that dimension.
    - Strict mode        → a missing allowlist dimension is treated as "deny all"
                           for that dimension.
    """

    def __init__(self, config: SecurityConfig) -> None:
        self._cfg = config

    def is_allowed(self, user_id: str, channel_id: str) -> bool:
        """
        Return True if the user is permitted to interact in this channel.

        Args:
            user_id:    Slack user ID of the message author.
            channel_id: Slack channel ID where the message was posted.
        """
        cfg = self._cfg

        if not cfg.enabled:
            return True

        is_admin = user_id in cfg.admin_users

        # --- User allowlist check ---
        if cfg.allowed_users:
            if user_id not in cfg.allowed_users:
                self._deny(user_id, channel_id, "user_not_in_allowlist")
                return False
        elif cfg.strict_mode:
            self._deny(user_id, channel_id, "strict_mode_no_user_allowlist")
            return False

        # --- Channel allowlist check (admins are exempt) ---
        if not is_admin:
            if cfg.allowed_channels:
                if channel_id not in cfg.allowed_channels:
                    self._deny(user_id, channel_id, "channel_not_in_allowlist")
                    return False
            elif cfg.strict_mode:
                self._deny(user_id, channel_id, "strict_mode_no_channel_allowlist")
                return False

        logger.debug(
            "Access granted: user_id=%s channel_id=%s is_admin=%s",
            user_id, channel_id, is_admin,
        )
        return True

    def rejection_message(self) -> str:
        """Return the configured rejection message."""
        return self._cfg.rejection_message

    def _deny(self, user_id: str, channel_id: str, reason: str) -> None:
        if self._cfg.log_unauthorized:
            logger.warning(
                "Access denied: user_id=%s channel_id=%s reason=%s",
                user_id, channel_id, reason,
            )
