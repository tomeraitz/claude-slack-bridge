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

    Optional variables for Cursor IDE dual-bot support:
      - CLIENT_ID:               Selects the bot identity for a session. Defaults to "claude".
                                 Set to "cursor" in docker exec -e to use cursor-bot credentials.
      - CURSOR_SLACK_BOT_TOKEN:  Bot OAuth token for the cursor-bot Slack App (xoxb-...).
                                 Required when CLIENT_ID=cursor.
      - CURSOR_SLACK_APP_TOKEN:  App-level token for cursor-bot Socket Mode (xapp-...).
                                 Required when CLIENT_ID=cursor.
    """

    slack_bot_token: str
    slack_app_token: str
    slack_channel: str = ""  # Not used by daemon; overridden per-session via docker exec -e
    timeout_limit_minutes: int = 5

    # Cursor dual-bot support
    client_id: str = "claude"
    cursor_slack_bot_token: str = ""
    cursor_slack_app_token: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}
