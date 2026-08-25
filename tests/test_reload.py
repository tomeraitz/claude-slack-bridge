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


class TestReloadPreflight:
    def test_missing_project_dirs_flags_only_the_bad_path(self, tmp_path, projects_file):
        # #alpha → a real dir, #beta → a path that doesn't exist. Both resolve
        # to channel IDs, so preflight (not resolution) is what catches #beta.
        channels = [{"name": "alpha", "id": "C_ALPHA"}, {"name": "beta", "id": "C_BETA"}]
        handler = make_handler(tmp_path, channels)
        good = str(tmp_path)

        async def run():
            projects_file({"#alpha": good, "#beta": "/no/such/dir"})
            await handler.reload_projects()

        asyncio.run(run())
        missing = handler.missing_project_dirs()
        assert missing == [("C_BETA", "/no/such/dir")]

    def test_all_dirs_present_reports_none(self, tmp_path, projects_file):
        handler = make_handler(tmp_path, [{"name": "alpha", "id": "C_ALPHA"}])

        async def run():
            projects_file({"#alpha": str(tmp_path)})
            await handler.reload_projects()

        asyncio.run(run())
        assert handler.missing_project_dirs() == []


class TestSelfHealThreadDir:
    def test_reply_reresolves_when_cached_dir_is_missing(self, tmp_path, monkeypatch):
        # A thread cached a dir that has since gone missing (bad path, later
        # fixed + reloaded). The next reply must re-resolve to the current
        # mapping instead of spawning into the dead dir.
        handler = make_handler(tmp_path, [])
        good = str(tmp_path)
        handler._channel_id_to_project["C1"] = {
            "path": good, "plugin_dir": None, "worktrees": {},
        }
        handler._thread_config["T1"] = ("/no/such/dir", None)  # stale, missing
        handler._sessions["T1"] = "sess"

        # Force the hot --resume path and capture the cwd _run_claude receives.
        jsonl = tmp_path / "sess.jsonl"
        jsonl.write_text("{}")
        monkeypatch.setattr(claude_handler, "_jsonl_path", lambda cwd, sid: jsonl)

        seen = {}

        async def fake_run(cmd, prompt, cwd=None, thread_ts=None, channel=None, **kwargs):
            seen["cwd"] = cwd
            return "ok"

        monkeypatch.setattr(handler, "_run_claude", fake_run)

        reply = asyncio.run(handler.handle_thread_reply("C1", "T1", "hi"))
        assert reply == "ok"
        assert seen["cwd"] == good                       # healed, not the dead dir
        assert handler._thread_config["T1"] == (good, None)

    def test_reply_keeps_cached_dir_when_still_valid(self, tmp_path, monkeypatch):
        # A healthy cached dir is left untouched (no needless re-resolve).
        handler = make_handler(tmp_path, [])
        handler._thread_config["T1"] = (str(tmp_path), None)
        handler._sessions["T1"] = "sess"
        jsonl = tmp_path / "sess.jsonl"
        jsonl.write_text("{}")
        monkeypatch.setattr(claude_handler, "_jsonl_path", lambda cwd, sid: jsonl)

        seen = {}

        async def fake_run(cmd, prompt, cwd=None, thread_ts=None, channel=None, **kwargs):
            seen["cwd"] = cwd
            return "ok"

        monkeypatch.setattr(handler, "_run_claude", fake_run)
        asyncio.run(handler.handle_thread_reply("C1", "T1", "hi"))
        assert seen["cwd"] == str(tmp_path)


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

                def missing_project_dirs(self):
                    return []

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


class TestMissingProjectsFile:
    """projects.json is bind-mounted, and Docker mounts a directory when the
    host file is absent. Both shapes of "no mapping" must disable project
    detection rather than raise out of the loader and kill the daemon."""

    def test_absent_file_disables_project_detection(self, tmp_path, monkeypatch):
        monkeypatch.setattr(claude_handler, "PROJECTS_CONFIG", tmp_path / "nope.json")
        assert claude_handler._load_project_map() == {}

    def test_directory_at_the_mount_point_disables_project_detection(
        self, tmp_path, monkeypatch
    ):
        # Exactly what `docker compose up` leaves behind when the bind source
        # does not exist on the host. exists() is true for it; open() is not.
        mount_point = tmp_path / "projects.json"
        mount_point.mkdir()
        monkeypatch.setattr(claude_handler, "PROJECTS_CONFIG", mount_point)
        assert claude_handler._load_project_map() == {}
