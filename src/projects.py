"""
projects.py — Channel→project resolution shared by ClaudeHandler and the
workflow engine.

Loads ``projects.json`` at the repo root and resolves Slack channel names
(or raw IDs) to project directory + optional ``plugin_dir`` configs.

Each entry in ``projects.json`` can be a plain path string (legacy) or a
dict with ``path`` and an optional ``plugin_dir`` field.  When ``plugin_dir``
is set, callers can prepend ``--plugin-dir <dir>`` to ``claude -p`` so that
project-specific skills are loaded automatically.
"""

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PROJECTS_CONFIG = Path(__file__).parent.parent / "projects.json"


def load_project_map() -> dict[str, Any]:
    """Load channel → project config mapping from projects.json.

    Values may be a plain path string (legacy) or a dict with ``path`` and
    optional ``plugin_dir`` keys (extended format).
    """
    if not PROJECTS_CONFIG.exists():
        logger.warning("No projects.json at %s — project detection disabled.", PROJECTS_CONFIG)
        return {}
    with open(PROJECTS_CONFIG) as f:
        mapping = json.load(f)
    logger.info("Loaded project map with %d entries.", len(mapping))
    return mapping


class ProjectResolver:
    """Resolve Slack channels to project configs.

    State is the resolved ``channel_id → {"path", "plugin_dir"}`` map,
    populated by :meth:`resolve` against a Slack WebClient.

    Args:
        project_map: Optional pre-loaded mapping (primarily for testing). When
            omitted, loads from ``projects.json`` via :func:`load_project_map`.
    """

    def __init__(self, project_map: dict[str, Any] | None = None) -> None:
        self._project_map: dict[str, Any] = (
            project_map if project_map is not None else load_project_map()
        )
        # Resolved at startup: channel ID → {"path": str|None, "plugin_dir": str|None}
        self._channel_id_to_project: dict[str, dict] = {}

    @property
    def project_map(self) -> dict[str, Any]:
        """Raw mapping loaded from ``projects.json``."""
        return self._project_map

    def get_project_config(self, channel_id: str) -> tuple[str | None, str | None]:
        """Return (project_dir, plugin_dir) for a Slack channel ID.

        Both values are ``None`` when no mapping exists for the channel.
        """
        config = self._channel_id_to_project.get(channel_id)
        if config:
            path = config["path"]
            plugin_dir = config["plugin_dir"]
            logger.info(
                "Channel %s → project %s%s",
                channel_id, path,
                f" (plugin_dir={plugin_dir})" if plugin_dir else "",
            )
            return path, plugin_dir
        logger.info("No project mapping for channel %s — using default cwd.", channel_id)
        return None, None

    async def resolve(self, slack_client: Any) -> None:
        """Resolve channel names from project_map to Slack channel IDs."""
        if not self._project_map:
            return
        try:
            result = await slack_client.conversations_list(
                types="public_channel,private_channel", limit=1000,
            )
            channels = result.get("channels", [])

            name_to_id: dict[str, str] = {}
            for ch in channels:
                name_to_id[f"#{ch['name']}"] = ch["id"]
                name_to_id[ch["name"]] = ch["id"]
                name_to_id[ch["id"]] = ch["id"]  # allow raw IDs in config

            for channel_key, value in self._project_map.items():
                # Normalise both the legacy string format and the new dict format.
                if isinstance(value, str):
                    config = {"path": value, "plugin_dir": None}
                else:
                    config = {"path": value.get("path"), "plugin_dir": value.get("plugin_dir")}

                # DM channel IDs (D...) and raw channel IDs (C...) are not
                # returned by conversations_list — register them directly.
                if channel_key.startswith(("C", "D")) and channel_key not in name_to_id:
                    self._channel_id_to_project[channel_key] = config
                    logger.info(
                        "Mapped %s (raw ID) → %s%s",
                        channel_key, config["path"],
                        f" plugin_dir={config['plugin_dir']}" if config["plugin_dir"] else "",
                    )
                    continue

                channel_id = name_to_id.get(channel_key)
                if channel_id:
                    self._channel_id_to_project[channel_id] = config
                    logger.info(
                        "Mapped %s (ID: %s) → %s%s",
                        channel_key, channel_id, config["path"],
                        f" plugin_dir={config['plugin_dir']}" if config["plugin_dir"] else "",
                    )
                else:
                    logger.warning("Channel %s not found in workspace — skipping.", channel_key)

        except Exception as exc:
            logger.error("Failed to resolve channel IDs: %s", exc)
