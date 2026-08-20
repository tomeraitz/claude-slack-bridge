"""Unit tests for skills/slack-webhook/send.py.

The script ships to Claude's sandbox and posts over plain HTTPS, so the tests
stub the transport. What matters is the payload Slack receives — above all that
a mention lands in a block type that actually pings.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).parent.parent / "skills" / "slack-webhook" / "send.py"

URL = "https://hooks.slack.com/services/T1/B1/secret"


def load_module(tmp_path: Path):
    """Import send.py fresh, with its config file inside *tmp_path*."""
    spec = importlib.util.spec_from_file_location(f"send_{tmp_path.name}", SKILL)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.HERE = tmp_path
    module.CONFIG_PATH = tmp_path / "config.json"
    return module


@pytest.fixture
def send(tmp_path, monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    return load_module(tmp_path)


def write_config(send, webhooks=None):
    send.CONFIG_PATH.write_text(
        json.dumps({"webhooks": webhooks or {"default": URL}}), encoding="utf-8"
    )


def stub_transport(send, result="ok"):
    """Replace send() with a recorder; returns the list of (url, payload)."""
    sent: list[tuple[str, dict]] = []

    def _send(url, payload):
        sent.append((url, payload))
        return result

    send.send = _send
    return sent


# ---------- config ----------

class TestLoadWebhooks:
    def test_reads_the_map(self, send):
        write_config(send, {"default": URL, "design": URL + "2"})
        assert set(send.load_webhooks()) == {"default", "design"}

    def test_accepts_a_bare_url_as_default(self, send):
        send.CONFIG_PATH.write_text(json.dumps({"webhooks": URL}), encoding="utf-8")
        assert send.load_webhooks()["default"] == URL

    def test_accepts_a_flat_object(self, send):
        """A config written without the "webhooks" wrapper still works."""
        send.CONFIG_PATH.write_text(json.dumps({"default": URL}), encoding="utf-8")
        assert send.load_webhooks()["default"] == URL

    def test_env_overrides_default(self, send, monkeypatch):
        write_config(send)
        monkeypatch.setenv("SLACK_WEBHOOK_URL", URL + "-env")
        assert send.load_webhooks()["default"] == URL + "-env"

    def test_env_alone_is_enough(self, send, monkeypatch):
        monkeypatch.setenv("SLACK_WEBHOOK_URL", URL)
        assert send.load_webhooks() == {"default": URL}

    def test_empty_entries_are_dropped(self, send):
        write_config(send, {"default": URL, "dead": "  "})
        assert set(send.load_webhooks()) == {"default"}

    def test_broken_json_is_explained(self, send):
        send.CONFIG_PATH.write_text("{nope", encoding="utf-8")
        with pytest.raises(send.SendError, match="not valid JSON"):
            send.load_webhooks()


class TestPickWebhook:
    def test_picks_by_name(self, send):
        hooks = {"default": URL, "design": URL + "2"}
        assert send.pick_webhook(hooks, "design") == URL + "2"

    def test_no_config_at_all_is_explained(self, send):
        with pytest.raises(send.SendError, match="No webhook configured"):
            send.pick_webhook({}, "default")

    def test_unknown_name_lists_what_exists(self, send):
        with pytest.raises(send.SendError, match="design"):
            send.pick_webhook({"design": URL}, "release")

    def test_rejects_something_that_isnt_a_webhook(self, send):
        with pytest.raises(send.SendError, match="incoming webhook URL"):
            send.pick_webhook({"default": "https://example.com/hook"}, "default")


# ---------- payload ----------

class TestBuildPayload:
    def test_body_is_a_markdown_block(self, send):
        payload = send.build_payload("# Title\n\n- a\n- b")
        assert payload["blocks"][0]["type"] == "markdown"
        assert payload["blocks"][0]["text"].startswith("# Title")

    def test_mention_is_its_own_section_block(self, send):
        """<@U…> inside a markdown block renders as text and pings nobody."""
        payload = send.build_payload("hi", mention="U1")
        assert payload["blocks"][0] == {
            "type": "section", "text": {"type": "mrkdwn", "text": "<@U1>"},
        }
        assert payload["blocks"][1]["type"] == "markdown"

    def test_fallback_text_carries_the_mention(self, send):
        assert send.build_payload("hi", mention="U1")["text"].startswith("<@U1> ")

    def test_fallback_previews_the_first_line(self, send):
        assert send.build_payload("\n\nfirst\nsecond")["text"] == "first"

    def test_plain_sends_text_only(self, send):
        payload = send.build_payload(":tada:", plain=True)
        assert payload == {"text": ":tada:"}

    def test_plain_still_mentions(self, send):
        assert send.build_payload("hi", mention="U1", plain=True)["text"] == "<@U1> hi"

    def test_empty_body_gets_a_placeholder(self, send):
        assert send.build_payload("   ")["blocks"][0]["text"] == "_(no content)_"

    def test_long_body_is_truncated_under_slacks_limit(self, send):
        block = send.build_payload("x" * 20000)["blocks"][0]["text"]
        assert len(block) <= send.MAX_BLOCK_CHARS + 20
        assert block.endswith("_…truncated._")


# ---------- CLI ----------

class TestMain:
    def test_sends_and_reports(self, send, capsys):
        write_config(send)
        sent = stub_transport(send)

        assert send.main(["hello"]) == 0
        assert sent[0][0] == URL
        assert "sent to default" in capsys.readouterr().out

    def test_routes_to_a_named_webhook(self, send):
        write_config(send, {"default": URL, "design": URL + "2"})
        sent = stub_transport(send)

        assert send.main(["mockups", "--to", "design"]) == 0
        assert sent[0][0] == URL + "2"

    def test_mention_flag_reaches_the_payload(self, send):
        write_config(send)
        sent = stub_transport(send)

        send.main(["decide?", "--mention", "@U7"])

        assert sent[0][1]["blocks"][0]["text"]["text"] == "<@U7>"

    def test_plain_flag_sends_text_only(self, send):
        write_config(send)
        sent = stub_transport(send)

        send.main([":tada:", "--plain"])

        assert "blocks" not in sent[0][1]

    def test_list_prints_names_without_sending(self, send, capsys):
        write_config(send, {"default": URL, "design": URL})
        sent = stub_transport(send)

        assert send.main(["--list"]) == 0
        assert capsys.readouterr().out.split() == ["default", "design"]
        assert sent == []

    def test_no_message_is_reported(self, send, capsys):
        write_config(send)
        assert send.main([]) == send.EXIT_ERROR
        assert "nothing to send" in capsys.readouterr().err

    def test_missing_config_is_reported_not_raised(self, send, capsys):
        assert send.main(["hello"]) == send.EXIT_ERROR
        assert "No webhook configured" in capsys.readouterr().err

    def test_transport_failure_is_reported(self, send, capsys):
        write_config(send)

        def _boom(url, payload):
            raise send.SendError("HTTP 404: no_service")

        send.send = _boom

        assert send.main(["hello"]) == send.EXIT_ERROR
        assert "no_service" in capsys.readouterr().err
