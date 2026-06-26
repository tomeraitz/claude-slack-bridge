"""
slack_markdown.py — Build chat_postMessage payloads that render Markdown in Slack.

Slack's ``markdown`` Block Kit block renders standard/GitHub Markdown — bold,
headings, lists, links, code, and **tables** — natively. This differs from the
``mrkdwn`` text dialect (``*bold*``, ``<url|text>``), which cannot represent
tables or headings at all. Claude emits standard Markdown, so we post its output
inside a markdown block rather than as ``mrkdwn`` text.

A short plain-text ``text`` fallback accompanies the block: Slack uses it for
notification previews when ``blocks`` are present.

When a body exceeds Slack's per-message limit, the chunker is structure-aware:
it closes and reopens fenced code blocks across chunk boundaries (preserving the
language tag), and re-emits Markdown table headers on every continuation chunk —
so each piece stays valid Markdown on its own.

This module is pure — it builds payload dicts and performs no I/O — so the
posting logic can be unit-tested without a Slack client.
"""

import re

# Slack caps each ``markdown`` block at 12,000 characters. Stay below it with
# headroom so structural reopen (close+reopen fence, re-emit table header) never
# pushes a chunk over the wire limit.
MAX_MARKDOWN_CHARS = 11500

# Slack renders a Markdown table as a native ``table`` block, capped at 100 rows
# (1 header + 99 data) — extra rows are SILENTLY dropped. Split a long table into
# multiple table blocks of at most this many DATA rows; the chunker re-emits the
# header on each continuation so every piece still renders as a table.
MAX_TABLE_DATA_ROWS = 99

# Slack allows at most 50 blocks per message; keep headroom.
MAX_BLOCKS_PER_MESSAGE = 48
# Conservative total characters per message (under Slack's 40k text limit) so a
# message with several blocks isn't rejected for size. Each block is already
# <= MAX_MARKDOWN_CHARS, so a single block always fits.
MAX_MESSAGE_CHARS = 38000

_FALLBACK_LIMIT = 150
_EMPTY_PLACEHOLDER = "_(no content)_"
# Inline Markdown markers stripped when deriving a plain notification preview.
_MD_MARKERS = re.compile(r"[*_`#>]+")

# Structural detectors. Block-level Markdown allows up to 3 leading spaces of
# indent; 4+ leading spaces denote an indented code block (no fence to track).
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP_CELL_RE = re.compile(r"^\s*:?-+:?\s*$")


def _fence_open_char(line: str) -> str | None:
    """Return '`' or '~' if *line* opens a fenced code block, else None."""
    m = _FENCE_RE.match(line)
    return m.group(1)[0] if m else None


def _is_close_fence(line: str, char: str) -> bool:
    """True if *line* is a closing fence (3+ of *char*, no language)."""
    return bool(re.match(rf"^ {{0,3}}{re.escape(char)}{{3,}}\s*$", line))


def _close_fence_for(open_line: str) -> str:
    """Build a closing fence matching the opener's run length and char family."""
    m = _FENCE_RE.match(open_line)
    return m.group(1) if m else "```"


def _is_table_row(line: str) -> bool:
    return bool(_TABLE_ROW_RE.match(line))


def _is_table_separator(line: str) -> bool:
    if not _is_table_row(line):
        return False
    inner = line.strip().strip("|")
    cells = inner.split("|")
    return bool(cells) and all(_TABLE_SEP_CELL_RE.match(c) for c in cells)


def _annotate(lines: list[str]) -> list[tuple[str, str]]:
    """Tag each line with its structural role.

    Roles: ``text``, ``code_fence_open``, ``code_content``, ``code_fence_close``,
    ``table_header``, ``table_sep``, ``table_row``.
    """
    out: list[tuple[str, str]] = []
    fence_char: str | None = None  # set while inside a code block
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if fence_char is not None:
            if _is_close_fence(line, fence_char):
                out.append((line, "code_fence_close"))
                fence_char = None
            else:
                out.append((line, "code_content"))
            i += 1
            continue

        opener = _fence_open_char(line)
        if opener is not None:
            out.append((line, "code_fence_open"))
            fence_char = opener
            i += 1
            continue

        if (
            _is_table_row(line)
            and i + 1 < n
            and _is_table_separator(lines[i + 1])
        ):
            out.append((line, "table_header"))
            out.append((lines[i + 1], "table_sep"))
            i += 2
            while i < n and _is_table_row(lines[i]):
                out.append((lines[i], "table_row"))
                i += 1
            continue

        out.append((line, "text"))
        i += 1
    return out


def _chunk(body: str, max_len: int) -> list[str]:
    """Split *body* into pieces no longer than *max_len*, preserving structure.

    Splits on newline boundaries; hard-splits a single line that exceeds
    *max_len*. When the boundary falls inside a fenced code block, the chunk is
    closed with a matching fence and the next reopens with the original opener
    (so the language tag is preserved). When inside a Markdown table, each
    continuation chunk re-emits the header + separator rows so each piece still
    renders as a table.
    """
    annotations = _annotate(body.split("\n"))

    # Short-circuit when the body fits in one chunk AND no table exceeds the
    # Slack 100-row cap.  We must count table_row annotations rather than relying
    # only on character length, because a table with 150 short rows can be well
    # under the char limit yet still get silently truncated by Slack.
    if len(body) <= max_len:
        table_data_rows = sum(1 for _, role in annotations if role == "table_row")
        if table_data_rows <= MAX_TABLE_DATA_ROWS:
            return [body]
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    active_fence: str | None = None       # opening-fence line, e.g. "```python"
    active_table: list[str] | None = None  # [header, sep]
    table_rows_in_chunk = 0  # data rows emitted in the CURRENT chunk's table

    def append_line(text: str) -> None:
        nonlocal cur_len
        if cur:
            cur_len += 1  # newline between previous line and this one
        cur.append(text)
        cur_len += len(text)

    def flush() -> None:
        """Emit current chunk (with sync fence if open) and reopen context."""
        nonlocal cur, cur_len, table_rows_in_chunk
        if not cur:
            return
        if active_fence is not None:
            append_line(_close_fence_for(active_fence))
        chunks.append("\n".join(cur))
        cur = []
        cur_len = 0
        table_rows_in_chunk = 0
        if active_fence is not None:
            cur.append(active_fence)
            cur_len = len(active_fence)
        if active_table is not None:
            for h in active_table:
                append_line(h)

    for line, role in annotations:
        # Leaving the table region — stop carrying the header on continuations.
        if role not in ("table_header", "table_sep", "table_row"):
            active_table = None

        # Keep each table block under Slack's 100-row cap (1 header + 99 data):
        # break the chunk before the 100th data row so the next chunk re-emits
        # the header as a fresh table block.
        if (
            role == "table_row"
            and active_table is not None
            and table_rows_in_chunk >= MAX_TABLE_DATA_ROWS
        ):
            flush()

        cost = (1 if cur else 0) + len(line)
        if cur and cur_len + cost > max_len:
            flush()

        # Even after flushing, the line may not fit (overlong single line, or
        # reopen overhead consumed most of the chunk) — hard-split it.
        room = max_len - cur_len - (1 if cur else 0)
        if len(line) > room:
            remaining = line
            while remaining:
                room = max_len - cur_len - (1 if cur else 0)
                if room <= 0:
                    flush()
                    room = max_len - cur_len - (1 if cur else 0)
                    if room <= 0:
                        # Reopen overhead alone fills max_len — drop the
                        # remaining tail of this line rather than loop forever.
                        break
                take = remaining[:room]
                append_line(take)
                remaining = remaining[room:]
                if remaining:
                    flush()
        else:
            append_line(line)

        if role == "code_fence_open":
            active_fence = line
        elif role == "code_fence_close":
            active_fence = None
        elif role == "table_header":
            active_table = [line]
            table_rows_in_chunk = 0
        elif role == "table_sep" and active_table is not None and len(active_table) == 1:
            active_table.append(line)
        elif role == "table_row" and active_table is not None:
            table_rows_in_chunk += 1

    if cur:
        chunks.append("\n".join(cur))
    return chunks


def _fallback(text: str) -> str:
    """Derive a short, plain notification preview from Markdown text.

    Uses the first non-empty line with inline Markdown markers stripped, so the
    notification reads as plain prose rather than ``## Heading`` / ``**bold**``.
    """
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    first = _MD_MARKERS.sub("", first).lstrip("-• ").strip()
    if not first:
        return "New message"
    return f"{first[:_FALLBACK_LIMIT]}…" if len(first) > _FALLBACK_LIMIT else first


def build_markdown_payloads(
    body: str,
    *,
    broadcast: bool = False,
    label: str | None = None,
    max_len: int = MAX_MARKDOWN_CHARS,
) -> list[dict]:
    """Build ``chat_postMessage`` kwargs that render *body* as Markdown in Slack.

    Args:
        body: Markdown text (Claude's output).
        broadcast: When True, prepend ``<!channel>`` to the FIRST payload's
            ``text`` fallback (preserves the bridge's @channel notification).
        label: When set, prepend ``**[label]**`` to the FIRST payload's block
            (preserves the worktree-label prefix).
        max_len: Max characters per markdown block.

    Returns:
        A list of kwargs dicts, each with a ``text`` fallback and a single
        ``markdown`` block. Post them in order, threading later ones under the
        first.
    """
    content = body if body and body.strip() else _EMPTY_PLACEHOLDER
    # Label goes on its own line so a body that starts with a block-level element
    # (heading, list, table, fence) still parses — an inline prefix would push a
    # leading ``##`` off the line start and break it.
    block_body = f"**[{label}]**\n\n{content}" if label else content

    payloads: list[dict] = []
    chunks = _chunk(block_body, max_len)

    # Group chunks into messages, packing as many markdown blocks as possible into
    # each message before spilling to a new one.
    cur_blocks: list[dict] = []
    cur_chars = 0
    is_first_payload = True

    def flush_message(first_chunk_of_message: str) -> None:
        nonlocal cur_blocks, cur_chars, is_first_payload
        if not cur_blocks:
            return
        if is_first_payload:
            text = _fallback(content)
            if broadcast:
                text = f"<!channel> {text}"
            is_first_payload = False
        else:
            text = _fallback(first_chunk_of_message)
        payloads.append({"text": text, "blocks": cur_blocks})
        cur_blocks = []
        cur_chars = 0

    first_chunk_in_msg: str = ""
    for chunk in chunks:
        chunk_len = len(chunk)
        # If adding this chunk would exceed limits and there are already blocks in
        # the current message, flush first. A single chunk always fits in a fresh
        # message (each chunk is <= MAX_MARKDOWN_CHARS < MAX_MESSAGE_CHARS).
        if cur_blocks and (
            len(cur_blocks) >= MAX_BLOCKS_PER_MESSAGE
            or cur_chars + chunk_len > MAX_MESSAGE_CHARS
        ):
            flush_message(first_chunk_in_msg)
            first_chunk_in_msg = ""

        if not cur_blocks:
            first_chunk_in_msg = chunk
        cur_blocks.append({"type": "markdown", "text": chunk})
        cur_chars += chunk_len

    flush_message(first_chunk_in_msg)
    return payloads
