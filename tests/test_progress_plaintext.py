"""The live status ticker is plain text.

Action lines posted to the mrkdwn status block include Claude's own prose (the
💬 lines). Claude emits standard Markdown, so **bold** would show up as literal
asterisks. The ticker is an ephemeral status line, not formatted output, so we
reduce inline Markdown to plain text at the source instead of translating
dialects downstream.
"""

from claude_handler import _strip_inline_md, _ProgressTracker


class TestStripInlineMd:
    def test_bold_stars_removed(self):
        assert _strip_inline_md("**Done** building") == "Done building"

    def test_bold_underscores_removed(self):
        assert _strip_inline_md("__Done__ now") == "Done now"

    def test_two_bolds_in_line(self):
        assert _strip_inline_md("**one** and **two**") == "one and two"

    def test_snake_case_preserved(self):
        # A lone underscore inside a word is not italic — must survive.
        assert _strip_inline_md("editing user_name parser") == "editing user_name parser"

    def test_inline_code_unwrapped(self):
        assert _strip_inline_md("call `do_it()` soon") == "call do_it() soon"

    def test_link_becomes_text(self):
        assert _strip_inline_md("see [the docs](https://x/y)") == "see the docs"

    def test_italic_unwrapped(self):
        assert _strip_inline_md("*really* fast") == "really fast"

    def test_strikethrough_unwrapped(self):
        assert _strip_inline_md("~~old~~ gone") == "old gone"

    def test_stray_asterisk_preserved(self):
        # Surrounded by spaces → not an emphasis marker.
        assert _strip_inline_md("2 * 3 = 6") == "2 * 3 = 6"

    def test_plain_unchanged(self):
        assert _strip_inline_md("just plain words") == "just plain words"


class TestTickerHasNoMarkdown:
    @staticmethod
    def _assistant_text(text):
        return {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": text}]},
        }

    def test_claude_bold_not_in_live_snapshot(self):
        tr = _ProgressTracker(0.0)
        tr.ingest(self._assistant_text("**Plan:** build the thing"))
        snap = tr.snapshot(1.0)
        assert "**" not in snap.live
        assert "💬 Plan: build the thing" in snap.live
