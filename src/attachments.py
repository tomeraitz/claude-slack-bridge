"""
attachments.py — Slack file download/upload helpers for the bridge.

The daemon (which holds the bot token) uses these helpers to:
  * INBOUND: download files a user attached in Slack (``url_private``) to a
    per-thread temp dir, so the local paths can be injected into Claude's prompt.
  * OUTBOUND: parse the agent's ``@@attach <abs_path>`` markers out of its reply,
    upload each existing path into the thread, and post the remaining text.

The ``claude -p`` subprocess never has the bot token (it is stripped from its
env in ``claude_handler._run_claude``), so ALL Slack file I/O lives here and is
driven by the daemon, not the subprocess.
"""

import logging
import os
import re
from typing import Any, Callable

import aiohttp

logger = logging.getLogger(__name__)

DEFAULT_INBOUND_DIR = "/tmp/slack-bridge/in"
_DEFAULT_MIMETYPE = "application/octet-stream"


async def download_files(
    files: list[dict],
    thread_ts: str,
    bot_token: str,
    *,
    base_dir: str = DEFAULT_INBOUND_DIR,
    session_factory: Callable[[], Any] | None = None,
) -> list[tuple[str, str]]:
    """Download each Slack file's ``url_private`` into ``base_dir/<thread_ts>/<name>``.

    Returns a list of ``(local_path, mimetype)`` for files that downloaded OK.
    Failures (non-200, network error) are logged and skipped, not raised, so one
    bad file never blocks the run.
    """
    if not files:
        return []

    dest_dir = os.path.join(base_dir, thread_ts)
    os.makedirs(dest_dir, exist_ok=True)

    make_session = session_factory or aiohttp.ClientSession
    headers = {"Authorization": f"Bearer {bot_token}"}
    results: list[tuple[str, str]] = []

    real_dest = os.path.realpath(dest_dir)

    async with make_session() as session:
        for f in files:
            url = f.get("url_private")
            # The Slack-provided name is attacker-influenced (a workspace member
            # chooses the uploaded filename). Reduce it to a bare basename so a
            # name like "../../etc/x" or "/etc/x" cannot escape the thread dir.
            name = os.path.basename(f.get("name") or "")
            if name in ("", ".", ".."):
                name = "attachment"
            mimetype = f.get("mimetype") or _DEFAULT_MIMETYPE
            if not url:
                logger.warning("Slack file %r has no url_private — skipping.", name)
                continue
            try:
                async with session.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        logger.warning(
                            "Download of %s failed (status %s) — skipping.", name, resp.status
                        )
                        continue
                    data = await resp.read()
            except Exception as exc:
                logger.warning("Download of %s failed (%s) — skipping.", name, exc)
                continue

            local_path = os.path.join(dest_dir, name)
            # Defence in depth: never write outside the per-thread dir even if
            # the basename sanitisation above is somehow bypassed.
            if os.path.realpath(local_path) != os.path.join(real_dest, name):
                logger.warning("Refusing to write %s outside %s — skipping.", name, real_dest)
                continue
            with open(local_path, "wb") as out:
                out.write(data)
            results.append((local_path, mimetype))
            logger.info("Downloaded Slack file %s -> %s (%s)", name, local_path, mimetype)

    return results


# A line that is exactly ``@@attach <path>`` (leading/trailing whitespace ok).
_ATTACH_RE = re.compile(r"^\s*@@attach\s+(.+?)\s*$")


def parse_attach_markers(reply: str) -> tuple[str, list[str]]:
    """Split *reply* into (clean_text, attach_paths).

    ``@@attach <absolute_path>`` lines are removed and their paths collected in
    order; the remaining text is rejoined and stripped of surrounding blank lines.
    """
    kept: list[str] = []
    paths: list[str] = []
    for line in reply.splitlines():
        m = _ATTACH_RE.match(line)
        if m:
            paths.append(m.group(1))
        else:
            kept.append(line)
    return "\n".join(kept).strip(), paths


async def upload_files(
    slack_client: Any,
    channel: str,
    thread_ts: str,
    paths: list[str],
) -> list[str]:
    """``files_upload_v2`` each EXISTING path into the thread.

    Non-existent paths are skipped (logged). Returns the paths actually uploaded.
    Per-file upload errors are logged and skipped, not raised.
    """
    uploaded: list[str] = []
    for path in paths:
        if not os.path.isfile(path):
            logger.warning("@@attach path does not exist — skipping: %s", path)
            continue
        try:
            await slack_client.files_upload_v2(
                channel=channel,
                thread_ts=thread_ts,
                file=path,
                filename=os.path.basename(path),
            )
        except Exception as exc:
            logger.warning("Upload of %s failed (%s) — skipping.", path, exc)
            continue
        uploaded.append(path)
        logger.info("Uploaded %s to thread %s", path, thread_ts)
    return uploaded
