"""
config.py — Application configuration.

Loads and validates all required environment variables using pydantic-settings.
This is the single source of truth for settings throughout the application.
"""

from pydantic_settings import BaseSettings


class Config(BaseSettings):
    """
    Validated configuration loaded from environment variables.

    Required variables (must be set in the environment or a .env file):
      - SLACK_BOT_TOKEN: Bot OAuth token (xoxb-...)
      - SLACK_APP_TOKEN: App-level token for Socket Mode (xapp-...)
      - SLACK_CHANNEL:   Channel name or ID where messages are posted (e.g. #general)
    """

    slack_bot_token: str
    slack_app_token: str
    slack_channel: str = ""  # Not used by daemon; overridden per-session via docker exec -e
    timeout_limit_minutes: int = 5

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}
