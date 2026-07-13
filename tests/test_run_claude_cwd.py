"""Regression: a missing project dir must not be reported as a missing CLI.

``asyncio.create_subprocess_exec`` raises ``FileNotFoundError`` both when the
``claude`` binary isn't on PATH *and* when ``cwd`` (the project directory from
projects.json) doesn't exist. The daemon used to blame the CLI for both, which
misdirected debugging — e.g. editing projects.json to a wrong path, reloading,
and getting "Sorry, the Claude CLI is not available."
"""

import asyncio

import pytest

from claude_handler import ClaudeHandler


def _raise_fnf(*args, **kwargs):
    raise FileNotFoundError(2, "No such file or directory")


@pytest.fixture
def handler(monkeypatch):
    monkeypatch.setattr(
        "claude_handler.asyncio.create_subprocess_exec", _raise_fnf
    )
    return ClaudeHandler(slack_client=object())


class TestRunClaudeCwd:
    def test_missing_project_dir_names_the_path_not_the_cli(self, handler):
        reply = asyncio.run(
            handler._run_claude(["claude"], "hi", cwd="/does/not/exist")
        )
        assert "project directory doesn't exist" in reply
        assert "/does/not/exist" in reply
        assert "CLI is not available" not in reply

    def test_valid_cwd_still_blames_the_cli(self, handler, tmp_path):
        # cwd exists, so a FileNotFoundError really is a missing binary.
        reply = asyncio.run(
            handler._run_claude(["claude"], "hi", cwd=str(tmp_path))
        )
        assert reply == "Sorry, the Claude CLI is not available."

    def test_no_cwd_still_blames_the_cli(self, handler):
        reply = asyncio.run(handler._run_claude(["claude"], "hi", cwd=None))
        assert reply == "Sorry, the Claude CLI is not available."
