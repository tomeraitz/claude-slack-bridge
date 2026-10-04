"""Unit tests for src/cli.py — the native, daemon-free CLI."""

import asyncio

import pytest

import cli


@pytest.fixture
def clean_env(monkeypatch):
    for var in ("SLACK_BOT_TOKEN", "SLACK_CHANNEL", "SLACK_POLL_INTERVAL", "CLAUDE_SLACK_BRIDGE_ENV"):
        monkeypatch.delenv(var, raising=False)


class TestEnvFile:
    def test_explicit_flag_wins(self, clean_env, monkeypatch, tmp_path):
        monkeypatch.setenv("CLAUDE_SLACK_BRIDGE_ENV", "/other/.env")
        assert cli.resolve_env_file(str(tmp_path / "x.env")) == tmp_path / "x.env"

    def test_env_var_next(self, clean_env, monkeypatch):
        monkeypatch.setenv("CLAUDE_SLACK_BRIDGE_ENV", "/other/.env")
        assert str(cli.resolve_env_file(None)) == "/other/.env"

    def test_defaults_to_repo_root_not_cwd(self, clean_env, monkeypatch, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".env").write_text("SLACK_BOT_TOKEN=xoxb-repo\n")
        monkeypatch.setattr(cli, "REPO_ROOT", repo)
        monkeypatch.chdir(tmp_path)  # a project dir, as when Claude Code launches `mcp`
        assert cli.resolve_env_file(None) == repo / ".env"

    def test_none_when_repo_has_no_env(self, clean_env, monkeypatch, tmp_path):
        monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
        assert cli.resolve_env_file(None) is None


class TestLoadConfig:
    def test_reads_env_file_without_app_token(self, clean_env, tmp_path):
        env = tmp_path / ".env"
        env.write_text("SLACK_BOT_TOKEN=xoxb-file\nSLACK_CHANNEL=#dev\nSLACK_POLL_INTERVAL=5\n")
        cfg = cli.load_config(env)
        assert (cfg.slack_bot_token, cfg.slack_channel, cfg.slack_poll_interval) == ("xoxb-file", "#dev", 5.0)

    def test_environment_overrides_file(self, clean_env, monkeypatch, tmp_path):
        env = tmp_path / ".env"
        env.write_text("SLACK_BOT_TOKEN=xoxb-file\nSLACK_CHANNEL=#dev\n")
        monkeypatch.setenv("SLACK_CHANNEL", "#from-mcp-config")
        assert cli.load_config(env).slack_channel == "#from-mcp-config"

    def test_missing_token_exits(self, clean_env):
        with pytest.raises(SystemExit, match="SLACK_BOT_TOKEN"):
            cli.load_config(None)

    def test_missing_env_file_exits(self, clean_env, tmp_path):
        with pytest.raises(SystemExit, match="not found"):
            cli.load_config(tmp_path / "nope.env")


class TestMakeBroker:
    def test_channel_flag_overrides_config(self, clean_env, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-t")
        monkeypatch.setenv("SLACK_CHANNEL", "#default")
        broker = cli.make_broker(cli.load_config(None), "#flag", "123.4")
        assert broker.channel == "#flag"
        assert broker.thread_ts == "123.4"

    def test_no_channel_exits(self, clean_env, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-t")
        with pytest.raises(SystemExit, match="no channel"):
            cli.make_broker(cli.load_config(None), None, None)


class FakeBroker:
    def __init__(self, reply: str = "yes", delay: float = 0.0) -> None:
        self.reply, self.delay = reply, delay
        self.thread_ts = "1700000001.000000"
        self.sent: list[str] = []

    async def send_only(self, message, label=None):
        self.sent.append(message)
        return "Notification sent."

    async def send_and_wait(self, message, label=None):
        self.sent.append(message)
        await asyncio.sleep(self.delay)
        return self.reply


class TestCommands:
    def test_send_prints_thread_ts(self, capsys):
        broker = FakeBroker()
        assert asyncio.run(cli.cmd_send(broker, "done")) == 0
        assert capsys.readouterr().out.strip() == "1700000001.000000"
        assert broker.sent == ["done"]

    def test_ask_prints_reply(self, capsys):
        assert asyncio.run(cli.cmd_ask(FakeBroker(reply="ship it"), "deploy?", None)) == 0
        assert capsys.readouterr().out.strip() == "ship it"

    def test_ask_timeout_exits_2(self, capsys):
        code = asyncio.run(cli.cmd_ask(FakeBroker(delay=1.0), "deploy?", 0.01))
        assert code == 2
        assert "no reply" in capsys.readouterr().err

    def test_read_message_from_stdin(self, monkeypatch):
        import io
        monkeypatch.setattr("sys.stdin", io.StringIO("from a pipe\n"))
        assert cli.read_message("-") == "from a pipe\n"

    def test_empty_message_exits(self):
        with pytest.raises(SystemExit, match="empty"):
            cli.read_message("   ")


class TestMain:
    def test_send_end_to_end(self, clean_env, monkeypatch, capsys):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-t")
        broker = FakeBroker()
        monkeypatch.setattr(cli, "make_broker", lambda cfg, ch, th: broker)

        assert cli.main(["send", "--channel", "#dev", "hello"]) == 0
        assert broker.sent == ["hello"]
        assert capsys.readouterr().out.strip() == broker.thread_ts

    def test_slack_error_is_a_clean_message(self, clean_env, monkeypatch, capsys):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-t")

        class Boom(FakeBroker):
            async def send_only(self, message, label=None):
                raise RuntimeError("Slack API error: not_in_channel")

        monkeypatch.setattr(cli, "make_broker", lambda cfg, ch, th: Boom())
        assert cli.main(["send", "--channel", "#dev", "hello"]) == 1
        assert "not_in_channel" in capsys.readouterr().err

    def test_command_is_required(self):
        with pytest.raises(SystemExit):
            cli.main([])
