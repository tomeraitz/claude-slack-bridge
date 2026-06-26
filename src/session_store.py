"""
session_store.py — Atomic JSON persistence of per-thread Claude session records.

The Slack↔Claude bridge keeps all session state in memory, so a daemon
restart/crash loses the thread→session map and any in-flight run state. This
module persists a small record per thread to a gitignored ``data/sessions.json``
so ``ClaudeHandler`` can ``--resume`` the real Claude session across restarts
and so the daemon can detect interrupted runs on boot.

Record shape per thread_ts::

    {
        "session_id": "uuid" | null,
        "cwd": "/abs/project/path" | null,
        "plugin_dir": "/abs/plugin/path" | null,
        "in_flight": false,
        "pid": null
    }

All writes are atomic: a temp file in the same directory is written and fsync'd,
then ``os.replace``'d over the target so readers never see a partial file.
"""

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SESSIONS_PATH = Path(__file__).parent.parent / "data" / "sessions.json"

# Sentinel so callers can distinguish "don't touch pid" from "set pid to None".
_UNSET: Any = object()


def _default_record() -> dict[str, Any]:
    return {
        "session_id": None,
        "cwd": None,
        "plugin_dir": None,
        "in_flight": False,
        "pid": None,
        "channel": None,
    }


def load(path: Path | None = None) -> dict[str, dict]:
    """Load the thread→record map. Returns ``{}`` if missing or unreadable."""
    path = path or SESSIONS_PATH
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("session_store: could not read %s (%s) — starting empty.", path, exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("session_store: %s is not a JSON object — starting empty.", path)
        return {}
    return data


def save(data: dict[str, dict], path: Path | None = None) -> None:
    """Atomically write *data* as JSON (temp file + os.replace)."""
    path = path or SESSIONS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".sessions-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        # Don't leave a temp file behind if the write/replace fails.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def upsert(
    thread_ts: str,
    *,
    session_id: str | None = None,
    cwd: str | None = None,
    plugin_dir: str | None = None,
    channel: str | None = None,
    in_flight: bool | None = None,
    pid: Any = _UNSET,
    path: Path | None = None,
) -> dict[str, dict]:
    """Merge the provided fields into ``thread_ts``'s record and persist.

    Only fields you pass are changed; omitted fields keep their stored value.
    ``pid`` uses a sentinel default so ``pid=None`` can be written explicitly
    (e.g. to clear a finished run) while omitting ``pid`` leaves it untouched.

    Returns the full updated map.
    """
    path = path or SESSIONS_PATH
    data = load(path)
    record = data.get(thread_ts) or _default_record()
    if session_id is not None:
        record["session_id"] = session_id
    if cwd is not None:
        record["cwd"] = cwd
    if plugin_dir is not None:
        record["plugin_dir"] = plugin_dir
    if channel is not None:
        record["channel"] = channel
    if in_flight is not None:
        record["in_flight"] = in_flight
    if pid is not _UNSET:
        record["pid"] = pid
    data[thread_ts] = record
    save(data, path)
    return data
