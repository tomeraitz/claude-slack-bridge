"""
crash_recovery.py — Boot-time recovery for runs interrupted by a daemon restart.

The ``claude -p`` child is a child of the daemon, so on daemon death it orphans
(reparented, may keep running). A live orphan plus the user resending would put
two processes ``--resume``-ing the same JSONL → corruption. On boot we therefore:

  1. Kill any still-alive orphan pid recorded for an interrupted run.
  2. Post a MECHANICAL (no-LLM) notice to that thread.
  3. Clear ``in_flight``/``pid`` so the entry won't be re-recovered.

Harvesting the orphan's actual final reply is out of scope (racy); the resend
path already works and the notice covers the UX.
"""

import logging
import os
import signal
from pathlib import Path
from typing import Any, Callable

import session_store

logger = logging.getLogger(__name__)

INTERRUPTED_NOTICE = (
    "⚠️ Bridge restarted — the run here was interrupted and won't finish. "
    "Resend your message."
)


def is_pid_alive(pid: int) -> bool:
    """True if a process with *pid* currently exists (signal 0 probe)."""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but owned by another user — still "alive".
        return True
    except OSError:
        return False
    return True


def kill_pid(pid: int) -> None:
    """Best-effort SIGTERM to *pid*; ignore an already-gone process."""
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, OSError) as exc:
        logger.debug("kill_pid(%s): %s", pid, exc)


async def recover_interrupted_runs(
    slack_client: Any,
    store_path: Path | None = None,
    *,
    is_alive: Callable[[int], bool] = is_pid_alive,
    kill: Callable[[int], None] = kill_pid,
) -> list[str]:
    """Kill orphans, post mechanical notices, and clear in_flight flags.

    Returns the list of thread_ts that were recovered.
    """
    data = session_store.load(store_path)
    recovered: list[str] = []
    for thread_ts, rec in data.items():
        if not rec.get("in_flight"):
            continue
        recovered.append(thread_ts)

        pid = rec.get("pid")
        if pid and is_alive(pid):
            logger.warning("Killing orphaned claude -p pid=%s for thread %s", pid, thread_ts)
            kill(pid)

        channel = rec.get("channel")
        if channel:
            try:
                await slack_client.chat_postMessage(
                    channel=channel, thread_ts=thread_ts, text=INTERRUPTED_NOTICE,
                )
            except Exception as exc:  # noqa: BLE001 — best-effort notice
                logger.warning("Failed to post interrupted notice to %s: %s", thread_ts, exc)
        else:
            logger.warning(
                "No channel recorded for interrupted thread %s — clearing without notice.",
                thread_ts,
            )

        session_store.upsert(
            thread_ts, in_flight=False, pid=None, path=store_path
        )
    return recovered
