"""
reloadctl.py — Force the running daemon to reload projects.json, live.

Connects to the daemon's Unix control socket, sends the ``RELOAD`` verb, and
prints the daemon's one-line summary. Use this after editing ``projects.json``
so the channel→project mapping takes effect WITHOUT restarting the daemon: the
Slack Socket Mode connection stays up and in-flight runs are not killed.

Run it via the repo-root wrapper ``./reload-mapping.sh`` or directly:

    .venv/bin/python src/reloadctl.py                       # default socket
    .venv/bin/python src/reloadctl.py --socket /tmp/x.sock

Exit codes: 0 on success, 1 if the daemon is unreachable or returns an error.
SIGHUP is an alternative trigger that needs no socket (``pm2 sendSignal SIGHUP
claude-slack-bridge``) but prints nothing — use this CLI when you want the
confirmation line. See docs/reloading-projects.md.
"""

import argparse
import asyncio
import sys

DEFAULT_SOCKET = "/tmp/slack-bridge.sock"
REPLY_TIMEOUT = 10.0


async def reload(socket_path: str = DEFAULT_SOCKET) -> str:
    """Send ``RELOAD`` over the daemon socket and return its summary reply.

    Raises ``RuntimeError`` if the daemon closes the connection without a reply
    (a build that predates the RELOAD verb), and propagates the connection
    errors (``FileNotFoundError`` / ``ConnectionRefusedError``) when the daemon
    is not reachable.
    """
    reader, writer = await asyncio.open_unix_connection(socket_path)
    try:
        writer.write(b"RELOAD\n")
        await writer.drain()
        reply = await asyncio.wait_for(reader.readline(), timeout=REPLY_TIMEOUT)
        if not reply:
            raise RuntimeError(
                "daemon closed the connection without replying — it is running "
                "a build without RELOAD support. Restart it once (e.g. "
                "pm2 restart claude-slack-bridge); after that, reloads work live."
            )
        return reply.decode().strip()
    finally:
        writer.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="reloadctl",
        description="Reload projects.json in the running daemon without restarting.",
    )
    parser.add_argument(
        "--socket", default=DEFAULT_SOCKET,
        help=f"daemon control socket path (default: {DEFAULT_SOCKET})",
    )
    args = parser.parse_args(argv)

    try:
        summary = asyncio.run(reload(args.socket))
    except (FileNotFoundError, ConnectionRefusedError):
        print(
            f"error: daemon not reachable (no socket at {args.socket}). "
            "Is claude-slack-bridge running?",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:  # noqa: BLE001 — surface any failure to the operator
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
