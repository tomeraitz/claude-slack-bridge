"""Unit tests for src/attachments.py — Slack file download/upload + marker parsing."""

import asyncio
import os

import attachments
from attachments import download_files
from claude_handler import _append_attachment_note


# ---------- fake aiohttp session ----------

class _FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Stand-in for aiohttp.ClientSession. Records GET calls; returns queued responses."""

    def __init__(self, responses: dict[str, _FakeResponse]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, headers: dict | None = None):
        self.calls.append((url, headers or {}))
        return self._responses[url]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _factory(session: FakeSession):
    def _make():
        return session
    return _make


# ---------- download_files ----------

class TestDownloadFiles:
    def test_downloads_to_per_thread_dir_and_returns_path_mimetype(self, tmp_path):
        files = [
            {"name": "diagram.png", "url_private": "https://files.slack/u1", "mimetype": "image/png"},
        ]
        session = FakeSession({"https://files.slack/u1": _FakeResponse(200, b"PNGDATA")})

        result = asyncio.run(
            download_files(
                files, thread_ts="T-1", bot_token="xoxb-secret",
                base_dir=str(tmp_path), session_factory=_factory(session),
            )
        )

        expected_path = str(tmp_path / "T-1" / "diagram.png")
        assert result == [(expected_path, "image/png")]
        assert os.path.exists(expected_path)
        with open(expected_path, "rb") as fh:
            assert fh.read() == b"PNGDATA"

    def test_sends_bearer_authorization_header(self, tmp_path):
        files = [{"name": "a.pdf", "url_private": "https://files.slack/u2", "mimetype": "application/pdf"}]
        session = FakeSession({"https://files.slack/u2": _FakeResponse(200, b"%PDF")})

        asyncio.run(
            download_files(
                files, thread_ts="T-2", bot_token="xoxb-tok",
                base_dir=str(tmp_path), session_factory=_factory(session),
            )
        )

        url, headers = session.calls[0]
        assert url == "https://files.slack/u2"
        assert headers["Authorization"] == "Bearer xoxb-tok"

    def test_skips_failed_download_gracefully(self, tmp_path):
        files = [
            {"name": "ok.png", "url_private": "https://files.slack/ok", "mimetype": "image/png"},
            {"name": "bad.png", "url_private": "https://files.slack/bad", "mimetype": "image/png"},
        ]
        session = FakeSession({
            "https://files.slack/ok": _FakeResponse(200, b"OK"),
            "https://files.slack/bad": _FakeResponse(403, b"forbidden"),
        })

        result = asyncio.run(
            download_files(
                files, thread_ts="T-3", bot_token="t",
                base_dir=str(tmp_path), session_factory=_factory(session),
            )
        )

        assert result == [(str(tmp_path / "T-3" / "ok.png"), "image/png")]

    def test_defaults_mimetype_when_absent(self, tmp_path):
        files = [{"name": "blob.bin", "url_private": "https://files.slack/b"}]
        session = FakeSession({"https://files.slack/b": _FakeResponse(200, b"x")})

        result = asyncio.run(
            download_files(
                files, thread_ts="T-4", bot_token="t",
                base_dir=str(tmp_path), session_factory=_factory(session),
            )
        )

        assert result == [(str(tmp_path / "T-4" / "blob.bin"), "application/octet-stream")]

    def test_empty_files_returns_empty(self, tmp_path):
        result = asyncio.run(
            download_files([], thread_ts="T-5", bot_token="t", base_dir=str(tmp_path))
        )
        assert result == []

    def test_traversal_name_is_contained_to_basename(self, tmp_path):
        # A malicious Slack filename must not escape the per-thread dir.
        files = [
            {"name": "../../evil.png", "url_private": "https://files.slack/ev", "mimetype": "image/png"},
        ]
        session = FakeSession({"https://files.slack/ev": _FakeResponse(200, b"X")})

        result = asyncio.run(
            download_files(
                files, thread_ts="T-6", bot_token="t",
                base_dir=str(tmp_path), session_factory=_factory(session),
            )
        )

        # Written as the basename inside the thread dir, NOT outside it.
        expected = str(tmp_path / "T-6" / "evil.png")
        assert result == [(expected, "image/png")]
        assert os.path.exists(expected)
        assert not os.path.exists(str(tmp_path / "evil.png"))

    def test_absolute_name_is_contained_to_basename(self, tmp_path):
        # An absolute filename must NOT be written to that absolute location.
        files = [
            {"name": "/etc/pwned.png", "url_private": "https://files.slack/ab", "mimetype": "image/png"},
        ]
        session = FakeSession({"https://files.slack/ab": _FakeResponse(200, b"X")})

        result = asyncio.run(
            download_files(
                files, thread_ts="T-7", bot_token="t",
                base_dir=str(tmp_path), session_factory=_factory(session),
            )
        )

        expected = str(tmp_path / "T-7" / "pwned.png")
        assert result == [(expected, "image/png")]
        assert os.path.exists(expected)


class TestAppendAttachmentNote:
    def test_no_files_returns_prompt_unchanged(self):
        assert _append_attachment_note("hello", []) == "hello"

    def test_single_file_appended(self):
        out = _append_attachment_note("hi", [("/tmp/x/diagram.png", "image/png")])
        assert out == "hi\n\n[User attached: /tmp/x/diagram.png (image/png)]"

    def test_multiple_files_comma_joined(self):
        out = _append_attachment_note(
            "do it",
            [("/tmp/x/diagram.png", "image/png"), ("/tmp/x/spec.pdf", "application/pdf")],
        )
        assert out == (
            "do it\n\n[User attached: /tmp/x/diagram.png (image/png), "
            "/tmp/x/spec.pdf (application/pdf)]"
        )


# ---------- parse_attach_markers + upload_files ----------

from unittest.mock import AsyncMock, call

from attachments import parse_attach_markers, upload_files


class TestParseAttachMarkers:
    def test_no_markers_returns_text_and_empty(self):
        text, paths = parse_attach_markers("just a normal reply")
        assert text == "just a normal reply"
        assert paths == []

    def test_extracts_marker_and_strips_line(self):
        reply = "Here is the chart.\n@@attach /tmp/out/chart.png\nLet me know."
        text, paths = parse_attach_markers(reply)
        assert paths == ["/tmp/out/chart.png"]
        assert "@@attach" not in text
        assert text == "Here is the chart.\nLet me know."

    def test_multiple_markers_in_order(self):
        reply = "@@attach /a/one.png\nbody\n@@attach /b/two.pdf"
        text, paths = parse_attach_markers(reply)
        assert paths == ["/a/one.png", "/b/two.pdf"]
        assert text == "body"

    def test_marker_with_surrounding_whitespace(self):
        reply = "text\n   @@attach   /tmp/x/file.png   \nmore"
        text, paths = parse_attach_markers(reply)
        assert paths == ["/tmp/x/file.png"]
        assert text == "text\nmore"

    def test_only_markers_yields_empty_text(self):
        reply = "@@attach /tmp/x/only.png"
        text, paths = parse_attach_markers(reply)
        assert text == ""
        assert paths == ["/tmp/x/only.png"]


class TestUploadFiles:
    def test_uploads_existing_path_into_thread(self, tmp_path):
        p = tmp_path / "chart.png"
        p.write_bytes(b"PNG")
        client = AsyncMock()

        uploaded = asyncio.run(
            upload_files(client, channel="C1", thread_ts="T-1", paths=[str(p)])
        )

        assert uploaded == [str(p)]
        client.files_upload_v2.assert_awaited_once()
        kwargs = client.files_upload_v2.await_args.kwargs
        assert kwargs["channel"] == "C1"
        assert kwargs["thread_ts"] == "T-1"
        assert kwargs["file"] == str(p)
        assert kwargs["filename"] == "chart.png"

    def test_skips_nonexistent_path(self, tmp_path):
        good = tmp_path / "good.png"
        good.write_bytes(b"X")
        missing = str(tmp_path / "nope.png")
        client = AsyncMock()

        uploaded = asyncio.run(
            upload_files(client, channel="C1", thread_ts="T", paths=[missing, str(good)])
        )

        assert uploaded == [str(good)]
        assert client.files_upload_v2.await_count == 1

    def test_empty_paths_no_upload(self):
        client = AsyncMock()
        uploaded = asyncio.run(upload_files(client, channel="C1", thread_ts="T", paths=[]))
        assert uploaded == []
        client.files_upload_v2.assert_not_awaited()
