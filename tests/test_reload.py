"""Tests for live projects.json reload — reload_projects, the RELOAD verb, reloadctl, SIGHUP."""

import asyncio
import json

import pytest

import claude_handler
import reloadctl
from claude_handler import ClaudeHandler


class FakeSlack:
    """Minimal async Slack client: just the calls reload touches."""

    def __init__(self, channels):
        self._channels = channels

    async def auth_test(self):
        return {"user_id": "U_BOT"}

    async def conversations_list(self, types, limit):
        return {"channels": self._channels}


@pytest.fixture
def projects_file(tmp_path, monkeypatch):
    """Point PROJECTS_CONFIG at a temp file and return a writer for it."""
    path = tmp_path / "projects.json"

    def write(mapping):
        path.write_text(json.dumps(mapping))

    write({"#alpha": "/p/alpha"})
    monkeypatch.setattr(claude_handler, "PROJECTS_CONFIG", path)
    return write


def make_handler(tmp_path, channels):
    store = tmp_path / "sessions.json"
    return ClaudeHandler(slack_client=FakeSlack(channels), store_path=store)


class TestReloadProjects:
    def test_picks_up_added_channel_without_restart(self, tmp_path, projects_file):
        channels = [{"name": "alpha", "id": "C_ALPHA"}, {"name": "beta", "id": "C_BETA"}]
        handler = make_handler(tmp_path, channels)

        async def run():
            await handler._resolve_channel_ids()  # initial resolve (as initialize would)
            assert "C_ALPHA" in handler._channel_id_to_project
            assert "C_BETA" not in handler._channel_id_to_project

            projects_file({"#alpha": "/p/alpha", "#beta": "/p/beta"})
            return await handler.reload_projects()

        count = asyncio.run(run())
        assert count == 2
        assert handler._channel_id_to_project["C_BETA"]["path"] == "/p/beta"

    def test_drops_removed_channel_atomically(self, tmp_path, projects_file):
        channels = [{"name": "alpha", "id": "C_ALPHA"}, {"name": "beta", "id": "C_BETA"}]
        handler = make_handler(tmp_path, channels)

        async def run():
            await handler._resolve_channel_ids()
            projects_file({"#beta": "/p/beta"})  # #alpha removed
            return await handler.reload_projects()

        count = asyncio.run(run())
        assert count == 1
        assert "C_ALPHA" not in handler._channel_id_to_project
        assert "C_BETA" in handler._channel_id_to_project

    def test_empty_map_clears_mapping(self, tmp_path, projects_file):
        handler = make_handler(tmp_path, [{"name": "alpha", "id": "C_ALPHA"}])

        async def run():
            await handler._resolve_channel_ids()
            projects_file({})
            return await handler.reload_projects()

        count = asyncio.run(run())
        assert count == 0
        assert handler._channel_id_to_project == {}

    def test_failed_resolve_keeps_previous_mapping(self, tmp_path, projects_file):
        """A Slack API error during re-resolve must not wipe the live mapping."""
        handler = make_handler(tmp_path, [{"name": "alpha", "id": "C_ALPHA"}])

        async def boom(*a, **k):
            raise RuntimeError("slack down")

        async def run():
            await handler._resolve_channel_ids()
            before = dict(handler._channel_id_to_project)
            handler._slack_client.conversations_list = boom
            projects_file({"#alpha": "/p/alpha", "#beta": "/p/beta"})
            await handler.reload_projects()
            return before

        before = asyncio.run(run())
        assert handler._channel_id_to_project == before  # unchanged on failure


class TestReloadctl:
    def test_round_trips_summary_over_socket(self, tmp_path):
        sock = str(tmp_path / "bridge.sock")

        async def run():
            async def serve(reader, writer):
                line = await reader.readline()
                assert line == b"RELOAD\n"
                writer.write(b"reloaded 7 channel(s)\n")
                await writer.drain()
                writer.close()

            server = await asyncio.start_unix_server(serve, path=sock)
            async with server:
                return await reloadctl.reload(sock)

        assert asyncio.run(run()) == "reloaded 7 channel(s)"

    def test_old_daemon_closing_without_reply_raises(self, tmp_path):
        """A daemon predating the RELOAD verb closes the socket; report it clearly."""
        sock = str(tmp_path / "bridge.sock")

        async def run():
            async def serve(reader, writer):
                await reader.readline()
                writer.close()  # no reply, mimicking the old REGISTER-only server

            server = await asyncio.start_unix_server(serve, path=sock)
            async with server:
                with pytest.raises(RuntimeError, match="without RELOAD support"):
                    await reloadctl.reload(sock)

        asyncio.run(run())

    def test_unreachable_socket_exits_nonzero(self, tmp_path, capsys):
        rc = reloadctl.main(["--socket", str(tmp_path / "absent.sock")])
        assert rc == 1
        assert "not reachable" in capsys.readouterr().err


class TestReloadVerbAndSignal:
    """End-to-end-ish: the daemon's socket handler and SIGHUP both call reload."""

    def test_reload_verb_invokes_reload_and_replies(self, tmp_path):
        from slack_daemon import SlackDaemon

        sock = str(tmp_path / "bridge.sock")

        async def run():
            daemon = SlackDaemon.__new__(SlackDaemon)  # skip Slack network init
            daemon._lock = asyncio.Lock()
            daemon._pending = {}

            class StubClaude:
                async def reload_projects(self):
                    return 5

            daemon._claude = StubClaude()

            server = await asyncio.start_unix_server(
                daemon._handle_session_connection, path=sock
            )
            async with server:
                reader, writer = await asyncio.open_unix_connection(sock)
                writer.write(b"RELOAD\n")
                await writer.drain()
                line = await reader.readline()
                writer.close()
                return line

        assert asyncio.run(run()) == b"reloaded 5 channel(s)\n"

    def test_sighup_handler_triggers_reload(self, tmp_path):
        from slack_daemon import SlackDaemon

        async def run():
            daemon = SlackDaemon.__new__(SlackDaemon)
            calls = []

            class StubClaude:
                async def reload_projects(self):
                    calls.append(1)
                    return 3

            daemon._claude = StubClaude()
            await daemon._reload_projects_on_signal()
            return calls

        assert asyncio.run(run()) == [1]
