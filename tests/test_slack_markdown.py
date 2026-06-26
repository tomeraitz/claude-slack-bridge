"""Unit tests for src/slack_markdown.py — Markdown → chat_postMessage payloads."""

from slack_markdown import (
    build_markdown_payloads,
    MAX_MARKDOWN_CHARS,
    MAX_BLOCKS_PER_MESSAGE,
    MAX_MESSAGE_CHARS,
    _is_table_row,
    _is_table_separator,
)


def _block_text(payload):
    """Return the text of the FIRST block in a payload (for single-block payloads)."""
    return payload["blocks"][0]["text"]


def _all_block_texts(payload):
    """Return all block texts in a payload."""
    return [b["text"] for b in payload["blocks"]]


def _all_blocks_across_payloads(payloads):
    """Return all blocks across all payloads as a flat list."""
    return [b for p in payloads for b in p["blocks"]]


class TestSinglePayload:
    def test_short_body_one_payload(self):
        payloads = build_markdown_payloads("**hi** and a | table |")
        assert len(payloads) == 1
        assert payloads[0]["blocks"][0]["type"] == "markdown"
        assert _block_text(payloads[0]) == "**hi** and a | table |"

    def test_text_fallback_present(self):
        payloads = build_markdown_payloads("## Heading\n\nbody text")
        # Fallback is plain-ish, non-empty, no leading '#'
        assert payloads[0]["text"]
        assert not payloads[0]["text"].startswith("#")

    def test_markdown_passthrough_unchanged(self):
        body = "# H1\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n```py\nx=1\n```"
        payloads = build_markdown_payloads(body)
        assert _block_text(payloads[0]) == body  # block body is verbatim

    def test_empty_body_yields_one_valid_payload(self):
        payloads = build_markdown_payloads("   \n  ")
        assert len(payloads) == 1
        assert _block_text(payloads[0]).strip() != ""


class TestChunking:
    def test_long_body_splits_on_newlines(self):
        body = "\n".join(f"line-{i:03d}" for i in range(50))  # > 200 chars
        payloads = build_markdown_payloads(body, max_len=40)
        all_blocks = _all_blocks_across_payloads(payloads)
        assert len(all_blocks) > 1, "Long body must produce multiple blocks"
        assert all(len(b["text"]) <= 40 for b in all_blocks)
        # Newline-boundary split is lossless: rejoining restores the body
        assert "\n".join(b["text"] for b in all_blocks) == body

    def test_overlong_single_line_hard_split(self):
        body = "x" * 100  # one line, no newline
        payloads = build_markdown_payloads(body, max_len=40)
        all_blocks = _all_blocks_across_payloads(payloads)
        assert all(len(b["text"]) <= 40 for b in all_blocks)
        assert "".join(b["text"] for b in all_blocks) == body


class TestLabelAndBroadcast:
    def test_label_only_on_first_block(self):
        body = "\n".join(f"line-{i:03d}" for i in range(50))
        payloads = build_markdown_payloads(body, label="feat-x", max_len=40)
        all_blocks = _all_blocks_across_payloads(payloads)
        assert len(all_blocks) > 1, "50 lines at max_len=40 must produce multiple blocks"
        assert all_blocks[0]["text"].startswith("**[feat-x]**")
        # Label must not appear in any subsequent block
        for block in all_blocks[1:]:
            assert "[feat-x]" not in block["text"]

    def test_label_keeps_leading_heading_on_its_own_line(self):
        # Regression: an inline label prefix would push '##' off the line start
        # and stop it rendering as a heading. It must stay line-anchored.
        payloads = build_markdown_payloads("## Title\n\nbody", label="x")
        block = _block_text(payloads[0])
        assert block.startswith("**[x]**\n\n")
        assert "\n\n## Title" in block

    def test_fallback_strips_markdown_markers(self):
        # Notification preview should be plain prose, not '## ...' or '**[x]**'.
        payloads = build_markdown_payloads("## Title\n\nbody", label="x")
        fb = payloads[0]["text"]
        assert "#" not in fb and "*" not in fb
        assert fb.startswith("Title")

    def test_broadcast_only_on_first_payload_text(self):
        # Need enough content to force 2+ payloads (not just 2+ blocks in 1 payload).
        # Use max_len=300 with 150 lines of 290 chars each → exceeds MAX_MESSAGE_CHARS.
        para = "A" * 290
        body = "\n".join(para for _ in range(150))
        payloads = build_markdown_payloads(body, broadcast=True, max_len=300)
        assert len(payloads) >= 2, "Content should spill to multiple payloads"
        assert payloads[0]["text"].startswith("<!channel>")
        assert not payloads[1]["text"].startswith("<!channel>")
        # Broadcast token never leaks into the rendered block
        assert "<!channel>" not in _block_text(payloads[0])

    def test_no_broadcast_by_default(self):
        payloads = build_markdown_payloads("hello")
        assert not payloads[0]["text"].startswith("<!channel>")


def test_max_default_under_slack_limit():
    assert MAX_MARKDOWN_CHARS <= 12000


class TestTablePreservation:
    HEADER = "| col | val |"
    SEP = "|---|---|"

    def test_table_split_reemits_header(self):
        rows = [f"| r{i:03d} | {i:03d} |" for i in range(50)]
        body = "\n".join([self.HEADER, self.SEP] + rows)
        payloads = build_markdown_payloads(body, max_len=120)
        all_blocks = _all_blocks_across_payloads(payloads)
        assert len(all_blocks) > 1, "50-row table at max_len=120 must produce multiple blocks"
        # Every block begins with the header + separator so each piece renders
        # as a complete table.
        for block in all_blocks:
            assert block["text"].startswith(f"{self.HEADER}\n{self.SEP}")

    def test_no_table_header_on_post_table_continuation(self):
        body = "\n".join(
            [self.HEADER, self.SEP, "| 1 | 2 |"]
            + [f"para line {i}" for i in range(50)]
        )
        payloads = build_markdown_payloads(body, max_len=80)
        all_blocks = _all_blocks_across_payloads(payloads)
        assert len(all_blocks) > 1, "Table + 50 para lines at max_len=80 must produce multiple blocks"
        # The table ends with "| 1 | 2 |". Continuation blocks for the
        # paragraph tail must not reintroduce the table header.
        for block in all_blocks[1:]:
            assert not block["text"].startswith(f"{self.HEADER}\n{self.SEP}")

    def test_short_table_passes_through(self):
        body = "before\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\nafter"
        payloads = build_markdown_payloads(body)
        assert len(payloads) == 1
        assert _block_text(payloads[0]) == body

    def test_separator_with_alignment_colons_detected(self):
        body = "\n".join(
            ["| a | b |", "|:---|---:|"]
            + [f"| {i} | {i} |" for i in range(30)]
        )
        payloads = build_markdown_payloads(body, max_len=80)
        all_blocks = _all_blocks_across_payloads(payloads)
        assert len(all_blocks) > 1, "30-row table at max_len=80 must produce multiple blocks"
        for block in all_blocks:
            assert block["text"].startswith("| a | b |\n|:---|---:|")


class TestCodeBlockPreservation:
    def test_code_block_split_closes_and_reopens_with_language(self):
        lines = "\n".join(f"line_{i} = {i}" for i in range(50))
        body = f"```python\n{lines}\n```"
        payloads = build_markdown_payloads(body, max_len=120)
        all_blocks = _all_blocks_across_payloads(payloads)
        assert len(all_blocks) > 1, "50-line code block at max_len=120 must produce multiple blocks"
        for block in all_blocks:
            text = block["text"]
            # Each chunk opens with the original fence (language preserved) and
            # ends with a matching closing fence.
            assert text.startswith("```python\n")
            assert text.endswith("\n```")

    def test_tilde_fence_preserved(self):
        lines = "\n".join(f"row {i}" for i in range(50))
        body = f"~~~\n{lines}\n~~~"
        payloads = build_markdown_payloads(body, max_len=80)
        all_blocks = _all_blocks_across_payloads(payloads)
        assert len(all_blocks) > 1, "50-line tilde-fence at max_len=80 must produce multiple blocks"
        for block in all_blocks:
            text = block["text"]
            assert text.startswith("~~~\n")
            assert text.endswith("\n~~~")

    def test_inline_backticks_do_not_open_fence(self):
        body = "\n".join("Use `inline` code." for _ in range(80))
        payloads = build_markdown_payloads(body, max_len=200)
        # No artificial fence injected — single backticks must not be treated
        # as a fence opener.
        for block in _all_blocks_across_payloads(payloads):
            assert "```" not in block["text"]

    def test_short_code_block_passes_through(self):
        body = "before\n\n```py\nx=1\n```\n\nafter"
        payloads = build_markdown_payloads(body)
        assert len(payloads) == 1
        assert _block_text(payloads[0]) == body

    def test_no_fence_outside_code_block(self):
        # A long plain body must not gain any artificial code fences.
        body = "\n".join(f"plain line {i}" for i in range(100))
        payloads = build_markdown_payloads(body, max_len=80)
        all_blocks = _all_blocks_across_payloads(payloads)
        assert len(all_blocks) > 1, "100-line body at max_len=80 must produce multiple blocks"
        for block in all_blocks:
            assert "```" not in block["text"]
            assert "~~~" not in block["text"]


def test_table_split_at_99_rows():
    """A 150-row table splits into >=2 BLOCKS (possibly in 1 message).

    With multi-block packing, the two 99+51-row blocks land in ONE payload.
    We assert across all blocks, not across payloads.
    """
    header = "| # | v |"
    sep = "|---|---|"
    rows = [f"| {i} | x{i} |" for i in range(1, 151)]
    body = "\n".join([header, sep] + rows)

    payloads = build_markdown_payloads(body)

    all_blocks = _all_blocks_across_payloads(payloads)
    assert len(all_blocks) >= 2, "150-row table must produce at least 2 blocks"

    total_data_rows = 0
    for block in all_blocks:
        text = block["text"]
        lines = text.splitlines()
        data_rows = [
            ln for ln in lines
            if _is_table_row(ln) and not _is_table_separator(ln) and ln != header
        ]
        assert len(data_rows) <= 99, f"Block has {len(data_rows)} data rows, expected <=99"
        total_data_rows += len(data_rows)

    assert total_data_rows == 150, f"Expected 150 total data rows, got {total_data_rows}"

    # Every block (beyond the first) must re-emit the header
    for block in all_blocks[1:]:
        text = block["text"]
        lines = [ln for ln in text.splitlines() if ln.strip()]
        table_lines = [ln for ln in lines if _is_table_row(ln)]
        assert table_lines[0] == header, "Continuation block must re-emit header"
        assert _is_table_separator(table_lines[1]), "Continuation block must re-emit separator"


def test_chunks_group_into_one_message():
    """A 120-row table (header + 120 rows) must return 1 payload with 2 blocks.

    The two blocks are the 99-row chunk and the 21-row chunk; both fit in one
    message so no second payload is needed.
    """
    header = "| # | v |"
    sep = "|---|---|"
    rows = [f"| {i} | x{i} |" for i in range(1, 121)]
    body = "\n".join([header, sep] + rows)

    payloads = build_markdown_payloads(body)

    assert len(payloads) == 1, (
        f"120-row table should produce exactly 1 payload, got {len(payloads)}"
    )
    assert len(payloads[0]["blocks"]) == 2, (
        f"120-row table should produce 2 blocks in that 1 payload, "
        f"got {len(payloads[0]['blocks'])}"
    )


def test_message_spills_at_block_or_char_limit():
    """Content exceeding MAX_MESSAGE_CHARS forces a second payload.

    We construct many large paragraphs so the combined chars exceed MAX_MESSAGE_CHARS.
    Every resulting payload must respect MAX_BLOCKS_PER_MESSAGE and MAX_MESSAGE_CHARS.
    """
    # Each paragraph is ~300 chars. We need enough to exceed MAX_MESSAGE_CHARS (38000).
    # 38000 / 300 ≈ 127 paragraphs. Use max_len small enough to get many chunks quickly.
    # Use max_len=300 so each chunk is ~300 chars, and 150 chunks → ~45000 chars total.
    para = "A" * 290  # 290-char line → one chunk per line with max_len=300
    lines = [para for _ in range(150)]
    body = "\n".join(lines)

    payloads = build_markdown_payloads(body, max_len=300)

    assert len(payloads) >= 2, (
        "Content exceeding MAX_MESSAGE_CHARS should spill into at least 2 payloads"
    )
    for i, p in enumerate(payloads):
        assert len(p["blocks"]) <= MAX_BLOCKS_PER_MESSAGE, (
            f"Payload {i} has {len(p['blocks'])} blocks, expected <= {MAX_BLOCKS_PER_MESSAGE}"
        )
        total_chars = sum(len(b["text"]) for b in p["blocks"])
        assert total_chars <= MAX_MESSAGE_CHARS, (
            f"Payload {i} total chars {total_chars} exceeds MAX_MESSAGE_CHARS {MAX_MESSAGE_CHARS}"
        )


def test_short_table_stays_single_payload():
    """A table with 10 rows must remain in a single payload."""
    header = "| # | v |"
    sep = "|---|---|"
    rows = [f"| {i} | x{i} |" for i in range(1, 11)]
    body = "\n".join([header, sep] + rows)

    payloads = build_markdown_payloads(body)

    assert len(payloads) == 1, "10-row table must stay in a single payload"
