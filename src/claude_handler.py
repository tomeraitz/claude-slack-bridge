"""
claude_handler.py — Spawns Claude Code CLI subprocesses for Human→Claude tasks.

When a human posts a message in Slack, this handler runs ``claude -p`` to
generate a response.  Thread continuations use ``--resume`` so Claude retains
full context (tool use, reasoning) across messages in the same thread.

If the session ID is lost (e.g. container restart), falls back to a one-shot
``claude -p`` with the formatted thread history as the prompt.

Project detection: reads ``projects.json`` at the repo root to map Slack
channels to project directories.  When a message arrives, the handler resolves
the channel to a project path and runs ``claude -p`` from that directory so
Claude sees the project's CLAUDE.md and codebase.

Each entry in ``projects.json`` can be a plain path string (legacy) or a dict
with ``path`` and optional ``plugin_dir`` / ``worktrees`` fields. When
``plugin_dir`` is set, ``--plugin-dir <dir>`` is prepended to the
``claude -p`` invocation so project-specific skills are loaded automatically.

When ``worktrees`` is a ``{label: path}`` map, users can route a top-level
Slack message to a specific worktree by prefixing the message with
``[label]`` (e.g. ``@Bot [feature-x] refactor session.py``). The label
prefix is stripped before the prompt is sent to Claude. Replies inside the
resulting thread stay in that worktree without re-tagging.
"""

import asyncio
import contextlib
import json
import logging
import os
import re
import session_store
import signal
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Awaitable, Callable, NamedTuple

logger = logging.getLogger(__name__)

# A run is killed only after IDLE_TIMEOUT seconds with NO stream activity
# (genuinely stuck), not on total wall-clock time — so long tasks run to
# completion and the user stops them with 🛑 instead of hitting a fixed cap.
# A long Bash/build emits no events while running, so keep this generous; set
# IDLE_TIMEOUT_SECONDS=0 to disable the watchdog and rely solely on 🛑.
IDLE_TIMEOUT = float(os.environ.get("IDLE_TIMEOUT_SECONDS", "1800"))  # 30 min
# Live progress: aggregate stream events and edit a single Slack status
# message at most once every PROGRESS_INTERVAL seconds (≤0 falls back to 10).
PROGRESS_INTERVAL = float(os.environ.get("PROGRESS_INTERVAL_SECONDS", "10"))
# How many recent actions the live status shows (newest first; ≤0 falls back to 3).
PROGRESS_ACTIONS = int(os.environ.get("PROGRESS_ACTIONS", "3"))

# A progress callback receives an immutable snapshot of run progress and pushes
# it to Slack (create/edit a status message). Owned by the daemon; the handler
# only decides *when* to call it (throttled aggregation + a final summary).
ProgressCb = Callable[["_Progress"], Awaitable[None]]
# Claude CLI in stream-json mode emits one JSON event per line. A single
# event can embed large tool inputs/results (file reads, MCP responses,
# task outputs), easily exceeding asyncio's default 64 KB StreamReader
# buffer and raising ``LimitOverrunError`` ("Separator is found, but chunk
# is longer than limit"). Bump the limit so we can ingest realistic events.
STREAM_BUFFER_LIMIT = 100 * 1024 * 1024  # 100 MB
PROJECTS_CONFIG = Path(__file__).parent.parent / "projects.json"

# Allow Slack's leading bold/italic/strike markers (``*``, ``_``, ``~``)
# before the tag — Slack delivers ``*[label] msg*`` when the user bolds
# the whole line.
_WORKTREE_TAG_RE = re.compile(r"^[\s*_~]*\[([^\]]+)\]\s*")
# Labels become directory names; restrict to a safe alphabet to block
# path-traversal attempts like ``[../etc]``.
_SAFE_LABEL_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# A claude -p run that failed transiently (CLI missing, non-zero rc, or no
# result event) returns one of these fixed user-facing strings from
# _run_claude. We treat any of them as a resume failure rather than parsing
# stderr (brittle). A timeout is deliberately NOT here — see _TIMEOUT_REPLY.
_RUN_FAILURE_SENTINELS = frozenset({
    "Sorry, the Claude CLI is not available.",
    "Sorry, I encountered an error processing your request.",
    "Sorry, I couldn't parse the response.",
})

# A run terminated by a signal (returncode < 0 — e.g. an OOM kill or a
# deliberate stop) is NOT a transient failure: retrying it just re-executes work
# that was intentionally or unavoidably halted. Surface a distinct reply that the
# resume policy does NOT retry. Intentionally NOT in _RUN_FAILURE_SENTINELS.
_INTERRUPTED_REPLY = "Sorry, the run was interrupted before it finished."

# A run the idle watchdog kills (no activity for IDLE_TIMEOUT) is NOT a transient
# failure: it was either making progress and stalled, or genuinely stuck, so
# retrying (then scraping) just serially re-runs the same expensive work while
# the user sees nothing. Surface this immediately and do NOT retry. Intentionally
# NOT in _RUN_FAILURE_SENTINELS.
_TIMEOUT_REPLY = "Sorry, the run stalled with no activity and was stopped. Try again or break it into a smaller step."


def _jsonl_path(cwd: str | None, session_id: str) -> Path:
    """Local JSONL path for a Claude session.

    Claude Code stores sessions at
    ``~/.claude/projects/<encoded-cwd>/<session_id>.jsonl`` where the encoded
    cwd is the absolute project path with every non-alphanumeric character
    replaced by ``-`` (e.g. ``/home/node/proj/x`` → ``-home-node-proj-x``).
    """
    abs_cwd = os.path.abspath(cwd) if cwd else os.getcwd()
    encoded = re.sub(r"[^A-Za-z0-9]", "-", abs_cwd)
    return Path.home() / ".claude" / "projects" / encoded / f"{session_id}.jsonl"


def _parse_worktree_tag(text: str) -> tuple[str | None, str]:
    """Strip a leading ``[label]`` tag from *text*.

    Returns ``(label, remaining_text)``. ``label`` is ``None`` when no tag
    is present or when the label contains unsafe characters. The label is
    what users type in Slack to route a Flow-B message to a specific
    worktree (e.g. ``[claude-slack-test] hi``).
    """
    match = _WORKTREE_TAG_RE.match(text)
    if not match:
        return None, text
    label = match.group(1).strip()
    if not _SAFE_LABEL_RE.match(label):
        return None, text
    remaining = text[match.end() :]
    return label, remaining


def _resolve_dynamic_worktree(default_path: str, label: str) -> str | None:
    """Resolve *label* to a sibling worktree directory of *default_path*.

    Worktrees are typically created with ``git worktree add ../<name>`` so
    they live next to the main checkout. This lets users add/remove
    worktrees without editing ``projects.json``: the daemon checks whether
    a sibling directory named *label* exists and looks like a git checkout
    (has a ``.git`` file or directory).

    Returns the resolved path or ``None`` if no matching directory exists.
    """
    parent = os.path.dirname(default_path)
    candidate = os.path.join(parent, label)
    git_marker = os.path.join(candidate, ".git")
    if os.path.isdir(candidate) and os.path.exists(git_marker):
        return candidate
    return None


def _load_project_map() -> dict[str, Any]:
    """Load channel → project config mapping from projects.json.

    Values may be a plain path string (legacy) or a dict with ``path`` and
    optional ``plugin_dir`` keys (extended format).
    """
    if not PROJECTS_CONFIG.exists():
        logger.warning("No projects.json at %s — project detection disabled.", PROJECTS_CONFIG)
        return {}
    with open(PROJECTS_CONFIG) as f:
        mapping = json.load(f)
    logger.info("Loaded project map with %d entries.", len(mapping))
    return mapping


def _append_attachment_note(prompt: str, files: list[tuple[str, str]]) -> str:
    """Append a ``[User attached: ...]`` note to *prompt* for downloaded files.

    *files* is the ``(local_path, mimetype)`` list from
    ``attachments.download_files``. Claude views image paths visually and
    ``Read``s document paths; only the path + mimetype are passed.
    """
    if not files:
        return prompt
    parts = ", ".join(f"{path} ({mimetype})" for path, mimetype in files)
    return f"{prompt}\n\n[User attached: {parts}]"


def _descendant_pids(pid: int) -> list[int]:
    """All descendant PIDs of *pid*, via the Linux /proc children interface.

    Walked while the tree is still intact (callers MUST snapshot before killing
    the parent — afterwards the children reparent to init and can't be traced
    from *pid*). Returns [] if the children interface is unavailable, degrading
    gracefully to a process-group-only kill.
    """
    result: list[int] = []
    stack = [pid]
    seen = {pid}
    while stack:
        cur = stack.pop()
        try:
            with open(f"/proc/{cur}/task/{cur}/children") as fh:
                kids = [int(x) for x in fh.read().split()]
        except OSError:
            continue
        for kid in kids:
            if kid not in seen:
                seen.add(kid)
                result.append(kid)
                stack.append(kid)
    return result


def _sigkill(pid: int) -> None:
    """SIGKILL one pid and its process group; swallow an already-gone process."""
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _kill_process_tree(process: asyncio.subprocess.Process) -> None:
    """SIGKILL *process* and every descendant.

    Claude Code runs Bash tools and sub-agents in their own sessions/process
    groups, so killing only the main group leaves them running. So:
      1. snapshot the whole descendant tree (while it is still intact);
      2. kill the PARENT first — a SIGKILL'd parent can't spawn anything new;
      3. kill every snapshotted descendant by pid.
    The window between snapshot and parent-kill is a few function calls (no
    awaits), so a child spawned in that gap is vanishingly unlikely.
    """
    pid = process.pid
    descendants = _descendant_pids(pid)   # 1. snapshot first
    _sigkill(pid)                         # 2. parent first (no respawns)
    try:
        process.kill()                   # let asyncio see the process is gone
    except (ProcessLookupError, OSError):
        pass
    for child in descendants:            # 3. the snapshotted children
        _sigkill(child)


# ----------------------------------------------------------------------
# Live progress (derived purely from the stream-json the CLI already emits —
# no extra LLM calls). The tracker accumulates events; the handler renders a
# snapshot at most once per PROGRESS_INTERVAL and a final summary at the end.
# ----------------------------------------------------------------------

# tool name -> (emoji, present-tense verb) for the live status line.
_TOOL_VERB: dict[str, tuple[str, str]] = {
    "Read": ("📖", "Reading"),
    "Edit": ("✏️", "Editing"),
    "Write": ("✏️", "Writing"),
    "MultiEdit": ("✏️", "Editing"),
    "NotebookEdit": ("✏️", "Editing"),
    "Bash": ("⚙️", "Running"),
    "Grep": ("🔍", "Searching"),
    "Glob": ("🔍", "Searching"),
    "WebFetch": ("🌐", "Fetching"),
    "WebSearch": ("🌐", "Searching"),
    "Task": ("🤖", "Delegating to a subagent"),
    "TodoWrite": ("📝", "Planning"),
}
# Tools that mutate files — counted toward the "N files changed" tally.
_EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})


def _fmt_duration(seconds: float) -> str:
    """Compact human duration: ``45s``, ``2m14s``, ``1h03m``."""
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m{total % 60:02d}s"
    return f"{total // 3600}h{(total % 3600) // 60:02d}m"


def _count_lines(text: str) -> int:
    """Line count of *text* (0 for empty). Used for edit churn (+/−)."""
    return text.count("\n") + 1 if text else 0


def _tool_result_text(block: dict) -> str:
    """Flatten a tool_result block's content (string or list of parts) to text."""
    content = block.get("content", "")
    if isinstance(content, list):
        return "".join(c.get("text", "") for c in content if isinstance(c, dict))
    return str(content or "")


def _clip(text: str, limit: int) -> str:
    """Trim *text* to ~*limit* chars on a word boundary, adding '…' when cut."""
    text = text.strip()
    if len(text) <= limit:
        return text
    return (text[:limit].rsplit(" ", 1)[0] or text[:limit]) + "…"


# The live status ticker posts as mrkdwn but shows Claude's own prose, which is
# standard Markdown. mrkdwn renders **bold** / [text](url) literally, so reduce
# inline Markdown to plain text — the ticker is an ephemeral status line, not
# formatted output. Only *paired* markers are stripped, so a lone underscore in
# snake_case or a spaced "*" survives. Order matters: links and code spans (which
# may contain "*") first, then bold before italic so "**" isn't read as two
# italic markers.
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_MD_CODE_RE = re.compile(r"`([^`]+)`")
_MD_BOLD_RE = re.compile(r"(\*\*|__)(.+?)\1")
_MD_STRIKE_RE = re.compile(r"~~(.+?)~~")
_MD_ITALIC_RE = re.compile(r"(?<!\w)([*_])(?!\s)(.+?)(?<!\s)\1(?!\w)")


def _strip_inline_md(text: str) -> str:
    """Reduce inline Markdown in *text* to plain text for the live status ticker."""
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _MD_CODE_RE.sub(r"\1", text)
    text = _MD_BOLD_RE.sub(r"\2", text)
    text = _MD_STRIKE_RE.sub(r"\1", text)
    text = _MD_ITALIC_RE.sub(r"\2", text)
    return text


# Ceiling on action lines in one update. A busy window shows up to this many;
# anything beyond is summarised as "… and N more". Kept under Slack's ~3000-char
# section limit by _ACTIONS_CHARS too (whichever bound hits first).
_ACTIONS_MAX = 100
_ACTIONS_CHARS = 2500
# Max todo lines shown in the live checklist before collapsing the tail.
_TODOS_MAX = 15
# Status icon per todo state (no interactive checkboxes in a posted message).
_TODO_ICON = {"completed": "✅", "in_progress": "🔄", "pending": "⬜"}
# Context-window sizes (max input tokens) for the "ctx %" readout, keyed by
# model substring, per the Claude API model catalog. Current models: Opus
# 4.6/4.7/4.8, Sonnet 4.6, Fable 5, Mythos 5 are 1M; Haiku 4.5 is 200K.
_MODEL_CONTEXT = {
    "haiku": 200_000,      # checked before the 1M families below
    "fable": 1_000_000,
    "mythos": 1_000_000,
    "opus": 1_000_000,
    "sonnet": 1_000_000,
}
_DEFAULT_CONTEXT = 200_000  # conservative fallback for older / unknown models


def _fmt_tokens(n: int) -> str:
    """Compact token count: ``950``, ``4.2k``, ``18k``, ``1.2M``."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1_000:
        return f"{n / 1_000:.1f}k".replace(".0k", "k")
    return str(n)


def _short_model(model: str) -> str:
    """``claude-opus-4-8`` -> ``opus-4-8`` for the status line."""
    return model[len("claude-"):] if model.startswith("claude-") else model


def _model_context(model: str) -> int:
    m = (model or "").lower()
    for key, size in _MODEL_CONTEXT.items():
        if key in m:
            return size
    return _DEFAULT_CONTEXT


class _Progress(NamedTuple):
    """An immutable progress snapshot handed to the daemon's ProgressCb.

    While running the daemon shows ``live`` (the last N actions, newest first)
    with ``meta`` rendered as a muted context line below it. ``summary`` is the
    collapsed end-state (one headline + an auto-collapsed detail block); ``done``
    tells the callback to render ``summary`` instead.
    """

    live: str
    summary: str
    done: bool
    meta: str = ""
    todos: str = ""  # checklist block (statuses), shown as its own section


class _ProgressTracker:
    """Folds a stream of claude events into a renderable progress snapshot.

    Deterministic and side-effect-free (no clock of its own, no I/O): callers
    pass ``now`` so elapsed time is testable. ``dirty`` flags whether anything
    changed since the last ``snapshot`` so the throttle can skip no-op edits.
    Everything is derived from the events the CLI already emits — no LLM calls.
    """

    def __init__(self, start: float) -> None:
        self._start = start
        self._floor = PROGRESS_ACTIONS if PROGRESS_ACTIONS > 0 else 3
        # Full ordered action history (oldest→newest), capped for memory. We keep
        # the whole window rather than a fixed ring so nothing is silently
        # skipped between updates and the order is always strictly newest-first.
        self._actions: deque[str] = deque(maxlen=200)
        self._total = 0                      # actions ever recorded
        self._shown = 0                      # _total at the previous snapshot
        self._tool_count = 0
        self._files: list[str] = []          # ordered, unique edited/written paths
        self._file_set: set[str] = set()
        self._reads = 0
        self._commands: list[str] = []       # first line of each Bash command
        self._added = 0                      # lines added across edits (churn)
        self._removed = 0                    # lines removed across edits (churn)
        self._errors = 0                     # tool_result blocks flagged is_error
        self._last_error = ""                # first line of the latest error result
        self._tool_counts: dict[str, int] = {}  # per-tool-name use counts
        self._subagents = 0                  # Task tool launches
        self._todos: list[tuple[str, str]] = []  # (content, status) latest TodoWrite
        self._turns = 0                      # assistant turns (overridden by result)
        self._model = ""                     # from system:init
        self._tok_in = 0
        self._tok_out = 0
        self._tok_cache_r = 0                # cache READ tokens (reused context)
        self._tok_cache_c = 0                # cache CREATION tokens
        self._ctx_last = 0                   # prompt size of the most recent call
        self._dirty = False

    @property
    def dirty(self) -> bool:
        return self._dirty

    def _push(self, action: str) -> None:
        """Record the newest action, collapsing consecutive duplicates."""
        if self._actions and self._actions[-1] == action:
            return
        self._actions.append(action)
        self._total += 1
        self._dirty = True

    def ingest(self, event: Any) -> None:
        """Update state from one stream-json event."""
        if not isinstance(event, dict):
            return
        etype = event.get("type")
        if etype == "assistant":
            self._turns += 1
            message = event.get("message") or {}
            usage = message.get("usage")
            if isinstance(usage, dict):
                self._add_usage(usage)
            for block in message.get("content", []) or []:
                if isinstance(block, dict):
                    self._ingest_assistant_block(block)
        elif etype == "user":
            # tool_result blocks ride on user events; count the failed ones.
            for block in event.get("message", {}).get("content", []) or []:
                if isinstance(block, dict) and block.get("type") == "tool_result" \
                        and block.get("is_error"):
                    self._errors += 1
                    raw = _tool_result_text(block).strip()
                    if raw:
                        self._last_error = _clip(_strip_inline_md(raw.splitlines()[0]), 80)
                    self._dirty = True
        elif etype == "system":
            if event.get("model"):
                self._model = event["model"]
        elif etype == "result":
            nt = event.get("num_turns")
            if isinstance(nt, int) and nt > 0:
                self._turns = nt
            usage = event.get("usage")
            if isinstance(usage, dict) and self._tok_out == 0:
                self._add_usage(usage)  # fallback when per-turn usage was absent
            self._dirty = True

    def _add_usage(self, u: dict) -> None:
        self._tok_in += u.get("input_tokens") or 0
        self._tok_out += u.get("output_tokens") or 0
        self._tok_cache_r += u.get("cache_read_input_tokens") or 0
        self._tok_cache_c += u.get("cache_creation_input_tokens") or 0
        # Full prompt size of THIS call = fresh input + cached (read + created).
        self._ctx_last = (
            (u.get("input_tokens") or 0)
            + (u.get("cache_read_input_tokens") or 0)
            + (u.get("cache_creation_input_tokens") or 0)
        )
        self._dirty = True

    def _ingest_assistant_block(self, block: dict) -> None:
        btype = block.get("type")
        if btype == "tool_use":
            self._ingest_tool(block)
        elif btype == "text":
            text = (block.get("text") or "").strip()
            if text:
                self._push("💬 " + _clip(_strip_inline_md(text.splitlines()[0]), 400))
        elif btype == "thinking":
            if (block.get("thinking") or "").strip():
                self._push("🤔 Thinking…")

    def _ingest_tool(self, block: dict) -> None:
        name = block.get("name", "") or "Tool"
        inp = block.get("input", {}) or {}
        self._tool_count += 1
        self._tool_counts[name] = self._tool_counts.get(name, 0) + 1
        if name == "Task":
            self._subagents += 1
        elif name == "TodoWrite":
            self._ingest_todos(inp)
        emoji, verb = _TOOL_VERB.get(name, ("🔧", name))
        arg = ""
        if name in _EDIT_TOOLS:
            path = inp.get("file_path") or inp.get("notebook_path") or ""
            if path:
                if path not in self._file_set:
                    self._file_set.add(path)
                    self._files.append(path)
                arg = f"`{os.path.basename(path)}`"
            self._count_edit_churn(name, inp)
        elif name == "Read":
            self._reads += 1
            path = inp.get("file_path") or ""
            arg = f"`{os.path.basename(path)}`" if path else ""
        elif name == "Bash":
            cmd = (inp.get("command") or "").strip()
            if cmd:
                first = cmd.splitlines()[0]
                self._commands.append(first)
                arg = f"`{_clip(first, 70)}`"
        elif name in ("Grep", "Glob"):
            pat = inp.get("pattern") or inp.get("query") or ""
            arg = f"`{_clip(pat, 60)}`" if pat else ""
        elif name in ("WebFetch", "WebSearch"):
            ref = inp.get("url") or inp.get("query") or ""
            arg = f"`{_clip(ref, 60)}`" if ref else ""
        self._push(f"{emoji} {verb} {arg}".strip())

    def _count_edit_churn(self, name: str, inp: dict) -> None:
        """Approximate +/− line churn from an edit tool's input (no real diff)."""
        if name == "MultiEdit":
            for e in inp.get("edits") or []:
                if isinstance(e, dict):
                    self._removed += _count_lines(e.get("old_string") or "")
                    self._added += _count_lines(e.get("new_string") or "")
        elif name == "Write":
            self._added += _count_lines(inp.get("content") or "")
        elif name == "NotebookEdit":
            self._added += _count_lines(inp.get("new_source") or "")
        else:  # Edit
            self._removed += _count_lines(inp.get("old_string") or "")
            self._added += _count_lines(inp.get("new_string") or "")

    def _ingest_todos(self, inp: dict) -> None:
        """Capture the latest TodoWrite list (it replaces the whole list each call)."""
        parsed: list[tuple[str, str]] = []
        for t in inp.get("todos") or []:
            if isinstance(t, dict):
                content = t.get("content") or t.get("activeForm") or ""
                if content:
                    parsed.append((content, t.get("status") or "pending"))
        if parsed:
            self._todos = parsed
            self._dirty = True

    # -- rendering helpers --------------------------------------------------

    def _tool_breakdown(self) -> str:
        """Per-tool counts, busiest first: ``8 read · 5 edit · 3 bash``."""
        if not self._tool_counts:
            return "0 tools"
        ordered = sorted(self._tool_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        return " · ".join(f"{n} {name.lower()}" for name, n in ordered[:6])

    def _files_line(self) -> str:
        """Unmuted line naming the changed files + churn (empty when none)."""
        if not self._files:
            return ""
        churn = f" (+{self._added}/−{self._removed})" if (self._added or self._removed) else ""
        names = [os.path.basename(p) for p in self._files]
        shown = ", ".join(names[:10])
        if len(names) > 10:
            shown += f", +{len(names) - 10} more"
        n = len(self._files)
        return f"📝 *{n} file{'s' if n != 1 else ''} changed*{churn}: {shown}"

    def _todos_block(self) -> str:
        """Checklist of the current todos with status icons (empty when none)."""
        if not self._todos:
            return ""
        done = sum(1 for _, s in self._todos if s == "completed")
        lines = [f"📋 *Todos {done}/{len(self._todos)}*"]
        for content, status in self._todos[:_TODOS_MAX]:
            icon = _TODO_ICON.get(status, "⬜")
            lines.append(f"{icon} {_clip(_strip_inline_md(content), 80)}")
        if len(self._todos) > _TODOS_MAX:
            lines.append(f"…+{len(self._todos) - _TODOS_MAX} more")
        return "\n".join(lines)

    def _tokens_str(self) -> str:
        return (
            f"🪙 ↑{_fmt_tokens(self._tok_in)} ↓{_fmt_tokens(self._tok_out)}"
            f" ⚡{_fmt_tokens(self._tok_cache_r)}"
        )

    def _meta(self, elapsed: str) -> str:
        """Muted context block: activity line + resource line."""
        line_a: list[str] = [self._tool_breakdown()]
        if self._subagents:
            line_a.append(f"🤖 {self._subagents} subagent{'s' if self._subagents != 1 else ''}")
        if self._errors:
            err = f"⚠️ {self._errors} error{'s' if self._errors != 1 else ''}"
            if self._last_error:
                err += f": {self._last_error}"
            line_a.append(err)
        line_a.append(elapsed)

        line_b: list[str] = []
        if self._tok_out or self._tok_in:
            line_b.append(self._tokens_str())
        if self._ctx_last:
            line_b.append(f"ctx {round(self._ctx_last / _model_context(self._model) * 100)}%")
        if self._turns:
            line_b.append(f"{self._turns} turn{'s' if self._turns != 1 else ''}")
        if self._model:
            line_b.append(_short_model(self._model))

        lines = [" · ".join(line_a)]
        if line_b:
            lines.append(" · ".join(line_b))
        return "\n".join(lines)

    def snapshot(self, now: float, *, done: bool = False) -> _Progress:
        """Render the current state; clears ``dirty``.

        Shows every action that happened in this window (since the last
        snapshot); if fewer than the floor (PROGRESS_ACTIONS) occurred, pads
        with the most recent earlier ones so at least the floor is visible.
        Always strictly newest-first.
        """
        self._dirty = False
        elapsed = _fmt_duration(now - self._start)
        window = self._total - self._shown
        self._shown = self._total
        want = max(window, self._floor)          # floor when the window was quiet
        candidates = list(reversed(self._actions))  # newest first
        shown: list[str] = []
        used = 0
        for line in candidates[:min(want, _ACTIONS_MAX)]:
            if shown and used + len(line) + 1 > _ACTIONS_CHARS:
                break                            # keep the section under Slack's limit
            shown.append(line)
            used += len(line) + 1
        hidden = max(0, window - len(shown))     # everything from this window we didn't show
        lines = list(shown)
        if hidden:
            lines.append(f"… and {hidden} more")
        live = "\n".join(lines) if lines else "🔄 Working…"
        files_line = self._files_line()
        if files_line:  # files shown unmuted, in the main section
            live += "\n\n" + files_line
        return _Progress(
            live=live, meta=self._meta(elapsed), todos=self._todos_block(),
            summary=self._render_summary(elapsed), done=done,
        )

    def _render_summary(self, elapsed: str) -> str:
        head_parts: list[str] = []
        if self._files:
            head_parts.append(
                f"{len(self._files)} file{'s' if len(self._files) != 1 else ''} changed"
            )
        if self._added or self._removed:
            head_parts.append(f"+{self._added}/−{self._removed}")
        head_parts.append(f"{self._tool_count} tool{'s' if self._tool_count != 1 else ''}")
        if self._errors:
            head_parts.append(f"⚠️ {self._errors} error{'s' if self._errors != 1 else ''}")
        head_parts.append(elapsed)
        head = "✅ Done · " + " · ".join(head_parts)

        detail: list[str] = []
        if self._files:
            names = [os.path.basename(p) for p in self._files]
            shown = ", ".join(names[:15])
            if len(names) > 15:
                shown += f", +{len(names) - 15} more"
            detail.append(f"Changed: {shown}")
        if self._reads:
            detail.append(f"Read {self._reads} file{'s' if self._reads != 1 else ''}")
        if self._commands:
            cmds = "; ".join(c[:40] for c in self._commands[:5])
            if len(self._commands) > 5:
                cmds += f"; +{len(self._commands) - 5} more"
            detail.append(f"Ran: {cmds}")
        if self._subagents:
            detail.append(f"Subagents: {self._subagents}")
        if self._tool_counts:
            detail.append(f"Tools: {self._tool_breakdown()}")
        if self._tok_out or self._tok_in:
            detail.append(f"Tokens: {self._tokens_str()}")
        resources = []
        if self._turns:
            resources.append(f"{self._turns} turns")
        if self._model:
            resources.append(_short_model(self._model))
        if resources:
            detail.append(" · ".join(resources))
        if not detail:
            return head
        # Fenced block -> Slack auto-collapses it behind "Show more" when long.
        return head + "\n```\n" + "\n".join(detail) + "\n```"


class ClaudeHandler:
    """
    Manages Claude Code CLI invocations for Slack messages.

    Args:
        slack_client: An async Slack WebClient (``self._app.client``).
    """

    def __init__(self, slack_client: Any, *, store_path: "Path | None" = None) -> None:
        self._slack_client = slack_client
        self._store_path = store_path or session_store.SESSIONS_PATH
        self._bot_user_id: str = ""
        self._sessions: dict[str, str] = {}  # thread_ts → session UUID
        self._project_map: dict[str, Any] = _load_project_map()
        # Resolved at startup: channel ID → {"path": str|None, "plugin_dir": str|None,
        #                                    "worktrees": dict[str, str]}
        self._channel_id_to_project: dict[str, dict] = {}
        # thread_ts → (cwd, plugin_dir) chosen when the thread started, so
        # replies stay in the same worktree without re-tagging.
        self._thread_config: dict[str, tuple[str | None, str | None]] = {}
        # Feature C: in-memory tracking of in-flight Flow-B subprocesses so a
        # 🛑 reaction can kill the right run. thread_ts → live subprocess.
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        # thread_ts values the user stopped, so the daemon can suppress the
        # (partial) normal reply for that run.
        self._stopped: set[str] = set()

    async def initialize(self) -> None:
        """Cache the bot's own user ID and resolve channel names to IDs."""
        resp = await self._slack_client.auth_test()
        self._bot_user_id = resp["user_id"]
        logger.info("ClaudeHandler initialized, bot_user_id=%s", self._bot_user_id)

        for thread_ts, rec in session_store.load(self._store_path).items():
            sid = rec.get("session_id")
            if sid:
                self._sessions[thread_ts] = sid
            self._thread_config[thread_ts] = (rec.get("cwd"), rec.get("plugin_dir"))

        if self._project_map:
            await self._resolve_channel_ids()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def handle_message(
        self, channel: str, message_ts: str, text: str,
        files: list[tuple[str, str]] | None = None,
        progress_cb: ProgressCb | None = None,
    ) -> str:
        """Handle a new top-level Slack message (start a new Claude session)."""
        label, text = _parse_worktree_tag(text)
        project_dir, plugin_dir = self._get_project_config(channel, label)

        session_id = str(uuid.uuid4())
        self._sessions[message_ts] = session_id
        self._thread_config[message_ts] = (project_dir, plugin_dir)
        session_store.upsert(
            message_ts, session_id=session_id, cwd=project_dir,
            plugin_dir=plugin_dir, in_flight=False, pid=None,
            path=self._store_path,
        )
        logger.info("New Claude session %s for thread %s", session_id, message_ts)

        prompt = _append_attachment_note(text, files or [])
        cmd = self._build_cmd(session_id=session_id, plugin_dir=plugin_dir)
        return await self._run_claude(
            cmd, prompt, cwd=project_dir, thread_ts=message_ts, channel=channel,
            progress_cb=progress_cb,
        )

    async def handle_thread_reply(
        self, channel: str, thread_ts: str, text: str,
        files: list[tuple[str, str]] | None = None,
        progress_cb: ProgressCb | None = None,
    ) -> str:
        """Handle a threaded reply: resume the real session, else scrape once.

        Policy ("scrape once, then resume forever"):
          1. session_id present AND its jsonl exists  -> --resume; on failure
             retry once; if it still fails -> scrape fallback.
          2. session_id present but jsonl missing      -> scrape fallback.
          3. no session_id                             -> scrape fallback.
        """
        session_id = self._sessions.get(thread_ts)
        # Thread inherits the worktree chosen at start; re-tagging mid-thread
        # would be confusing, so we don't re-parse here. Falls back to default
        # config only if the thread state was lost (container restart).
        project_dir, plugin_dir = self._thread_config.get(thread_ts) or self._get_project_config(channel)

        if session_id and _jsonl_path(project_dir, session_id).exists():
            logger.info("Resuming session %s for thread %s", session_id, thread_ts)
            prompt = _append_attachment_note(text, files or [])
            cmd = self._build_cmd(resume=session_id, plugin_dir=plugin_dir)
            reply = await self._run_claude(
                cmd, prompt, cwd=project_dir, thread_ts=thread_ts, channel=channel,
                progress_cb=progress_cb,
            )
            if reply not in _RUN_FAILURE_SENTINELS:
                return reply
            logger.warning("Resume of %s failed; retrying once.", session_id)
            reply = await self._run_claude(
                cmd, prompt, cwd=project_dir, thread_ts=thread_ts, channel=channel,
                progress_cb=progress_cb,
            )
            if reply not in _RUN_FAILURE_SENTINELS:
                return reply
            logger.warning("Resume of %s failed twice; scraping thread history.", session_id)
        else:
            logger.info(
                "No resumable session for thread %s (session_id=%s) — scraping.",
                thread_ts, session_id,
            )

        return await self._scrape_and_run(
            channel, thread_ts, project_dir, plugin_dir, files, progress_cb=progress_cb,
        )

    async def _scrape_and_run(
        self, channel: str, thread_ts: str, project_dir: str | None, plugin_dir: str | None,
        files: list[tuple[str, str]] | None = None,
        progress_cb: ProgressCb | None = None,
    ) -> str:
        """Last-resort fallback: replay scraped thread history under a NEW session.

        Mints a fresh ``--session-id``, runs the scraped prompt, and persists the
        new id so subsequent replies take the hot --resume path (step 1).
        """
        prompt = await self._build_thread_prompt(channel, thread_ts)
        prompt = _append_attachment_note(prompt, files or [])
        new_id = str(uuid.uuid4())
        cmd = self._build_cmd(session_id=new_id, plugin_dir=plugin_dir)
        reply = await self._run_claude(
            cmd, prompt, cwd=project_dir, thread_ts=thread_ts, channel=channel,
            progress_cb=progress_cb,
        )
        self._sessions[thread_ts] = new_id
        self._thread_config[thread_ts] = (project_dir, plugin_dir)
        session_store.upsert(
            thread_ts, session_id=new_id, cwd=project_dir, plugin_dir=plugin_dir,
            path=self._store_path,
        )
        return reply

    async def stop(self, thread_ts: str) -> bool:
        """Kill the in-flight Flow-B subprocess for *thread_ts*, if any.

        Marks the thread stopped so the daemon suppresses the partial reply.
        Returns True when a tracked process was found and killed, else False.
        """
        process = self._processes.get(thread_ts)
        if process is None:
            return False
        self._stopped.add(thread_ts)
        logger.info("Stopping Claude subprocess for thread %s", thread_ts)
        _kill_process_tree(process)
        await process.wait()
        return True

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_project_config(
        self, channel_id: str, label: str | None = None
    ) -> tuple[str | None, str | None]:
        """Return (project_dir, plugin_dir) for a Slack channel ID.

        When *label* is provided and matches a registered worktree for the
        channel, the worktree path is returned instead of the default. An
        unknown label falls back to the default with a warning so messages
        aren't silently dropped.

        Both values are ``None`` when no mapping exists for the channel.
        """
        config = self._channel_id_to_project.get(channel_id)
        if not config:
            logger.info("No project mapping for channel %s — using default cwd.", channel_id)
            return None, None

        plugin_dir = config["plugin_dir"]
        worktrees: dict[str, str] = config.get("worktrees", {})
        default_path = config["path"]

        if label and label in worktrees:
            return worktrees[label], plugin_dir

        if label and default_path:
            dynamic = _resolve_dynamic_worktree(default_path, label)
            if dynamic:
                return dynamic, plugin_dir

        path = default_path
        logger.info(
            "Channel %s → project %s%s",
            channel_id, path,
            f" (plugin_dir={plugin_dir})" if plugin_dir else "",
        )
        return path, plugin_dir

    async def _resolve_channel_ids(self) -> None:
        """Resolve channel names from project_map to Slack channel IDs."""
        try:
            result = await self._slack_client.conversations_list(
                types="public_channel,private_channel", limit=1000,
            )
            channels = result.get("channels", [])

            name_to_id: dict[str, str] = {}
            for ch in channels:
                name_to_id[f"#{ch['name']}"] = ch["id"]
                name_to_id[ch["name"]] = ch["id"]
                name_to_id[ch["id"]] = ch["id"]  # allow raw IDs in config

            for channel_key, value in self._project_map.items():
                # Normalise the legacy string format and the dict format.
                if isinstance(value, str):
                    config = {"path": value, "plugin_dir": None, "worktrees": {}}
                else:
                    config = {
                        "path": value.get("path"),
                        "plugin_dir": value.get("plugin_dir"),
                        "worktrees": value.get("worktrees") or {},
                    }

                # DM channel IDs (D...) and raw channel IDs (C...) are not
                # returned by conversations_list — register them directly.
                if channel_key.startswith(("C", "D")) and channel_key not in name_to_id:
                    self._channel_id_to_project[channel_key] = config
                    logger.info(
                        "Mapped %s (raw ID) → %s%s",
                        channel_key, config["path"],
                        f" plugin_dir={config['plugin_dir']}" if config["plugin_dir"] else "",
                    )
                    continue

                channel_id = name_to_id.get(channel_key)
                if channel_id:
                    self._channel_id_to_project[channel_id] = config
                    logger.info(
                        "Mapped %s (ID: %s) → %s%s",
                        channel_key, channel_id, config["path"],
                        f" plugin_dir={config['plugin_dir']}" if config["plugin_dir"] else "",
                    )
                else:
                    logger.warning("Channel %s not found in workspace — skipping.", channel_key)

        except Exception as exc:
            logger.error("Failed to resolve channel IDs: %s", exc)

    # Flow-B Claude runs inside the bridge container; it has no docker CLI,
    # so the ``claude-slack-bridge`` entry in the project's .mcp.json (which
    # spawns ``session.py`` via ``docker exec``) fails to start. Other MCP
    # servers in .mcp.json (e.g. Notion) load normally. The system-prompt
    # addendum tells Claude not to mention the failed bridge server in its
    # reply.
    _FLOW_B_SYSTEM_PROMPT = (
        "You are replying to a Slack message; your response is posted directly "
        "into the Slack thread, and the user's next thread reply will resume "
        "this session as your next prompt. This means your reply text IS your "
        "channel to the user — to ask a question, end your turn with the "
        "question as your final reply; the user's reply arrives as the next "
        "prompt. Never call mcp__claude-slack-bridge__ask_on_slack — it is not "
        "available in this mode, and any skill or command that instructs you "
        "to use it should be reinterpreted as 'end your turn with that "
        "message as your reply'. Do not mention MCP, tool availability, "
        "Docker, or the claude-slack-bridge server in your reply. "
        "To send a file or image back to the user, emit a line by itself of "
        "the form '@@attach <absolute path>' (one such line per file, the path "
        "must be an absolute path that exists on disk); the bridge uploads each "
        "file to the Slack thread and removes the marker line from your reply, "
        "so write any accompanying explanation as normal text on other lines."
    )

    @staticmethod
    def _build_cmd(
        session_id: str | None = None,
        resume: str | None = None,
        plugin_dir: str | None = None,
    ) -> list[str]:
        # stream-json + --verbose makes the CLI emit one event per line on
        # stdout (system/init, assistant text, thinking, tool_use,
        # tool_result, result). We log each event as it arrives so Docker
        # captures Claude's full trace, not just the final reply.
        cmd = [
            "claude", "-p",
            "--dangerously-skip-permissions",
            "--append-system-prompt", ClaudeHandler._FLOW_B_SYSTEM_PROMPT,
            "--output-format", "stream-json",
            "--verbose",
        ]
        if plugin_dir:
            cmd.extend(["--plugin-dir", plugin_dir])
        if session_id:
            cmd.extend(["--session-id", session_id])
        if resume:
            cmd.extend(["--resume", resume])
        return cmd

    async def _run_claude(
        self, cmd: list[str], prompt: str, cwd: str | None = None,
        thread_ts: str | None = None, channel: str | None = None,
        progress_cb: ProgressCb | None = None,
    ) -> str:
        """Spawn a ``claude -p`` subprocess, stream-log its events, and return the final reply.

        There is no wall-clock cap: a run lives until it finishes, the user 🛑s
        it, or the idle watchdog kills it after IDLE_TIMEOUT seconds with no
        stream activity. While it runs, *progress_cb* (if given) is invoked with
        an aggregated :class:`_Progress` snapshot at most once per
        PROGRESS_INTERVAL, then once more with ``done=True`` for the summary.
        """
        env = os.environ.copy()
        # Strip tokens that must never be reachable by the Claude subprocess.
        # A prompt-injection attack could otherwise instruct Claude to exfiltrate them.
        for _key in ("CLAUDECODE", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "ANTHROPIC_API_KEY"):
            env.pop(_key, None)

        logger.debug("claude spawn: cwd=%s cmd=%s prompt=%r", cwd, cmd, prompt[:500])

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=cwd,
                limit=STREAM_BUFFER_LIMIT,
                start_new_session=True,
            )
        except FileNotFoundError:
            logger.error("claude CLI not found — is it installed and in PATH?")
            return "Sorry, the Claude CLI is not available."

        if thread_ts is not None:
            # One in-flight process per thread_ts. The daemon's _active_threads
            # guard prevents a second concurrent run for the same thread, so this
            # never overwrites a still-tracked process. If that guard is ever
            # relaxed, this map (and stop()) would need a per-run key instead.
            self._processes[thread_ts] = process
            session_store.upsert(
                thread_ts, in_flight=True, pid=process.pid,
                channel=channel, path=self._store_path,
            )

        def _mark_finished() -> None:
            # The subprocess has terminated (cleanly or with an error) — the run
            # is no longer in flight. Only a run that never reaches a terminal
            # path (the daemon itself dying mid-run) stays in_flight=true, which
            # is exactly what boot crash-recovery should surface.
            if thread_ts is not None:
                session_store.upsert(
                    thread_ts, in_flight=False, pid=None, path=self._store_path
                )

        try:
            # Send prompt and close stdin so claude can begin work.
            assert process.stdin is not None
            process.stdin.write(prompt.encode("utf-8"))
            await process.stdin.drain()
            process.stdin.close()

            final_result: str | None = None
            loop = asyncio.get_running_loop()
            tracker = _ProgressTracker(loop.time())
            last_event = loop.time()
            flushed_once = False
            idle_killed = False

            async def consume_stdout() -> None:
                nonlocal final_result, last_event
                assert process.stdout is not None
                async for raw_line in process.stdout:
                    line = raw_line.decode("utf-8", errors="replace").rstrip()
                    if not line:
                        continue
                    last_event = loop.time()
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        logger.debug("claude stdout (non-json): %s", line[:1000])
                        continue
                    self._log_stream_event(event)
                    tracker.ingest(event)
                    if (
                        isinstance(event, dict)
                        and event.get("type") == "result"
                        and "result" in event
                    ):
                        final_result = event["result"]

            async def consume_stderr() -> None:
                nonlocal last_event
                assert process.stderr is not None
                async for raw_line in process.stderr:
                    line = raw_line.decode("utf-8", errors="replace").rstrip()
                    if line:
                        last_event = loop.time()
                        logger.warning("claude stderr: %s", line[:1000])

            async def monitor() -> None:
                # Wakes every PROGRESS_INTERVAL to (1) kill genuinely-stuck runs
                # (no activity for IDLE_TIMEOUT) and (2) push one aggregated
                # progress update covering everything since the last push.
                nonlocal flushed_once, idle_killed
                interval = PROGRESS_INTERVAL if PROGRESS_INTERVAL > 0 else 10.0
                while True:
                    await asyncio.sleep(interval)
                    now = loop.time()
                    if IDLE_TIMEOUT > 0 and (now - last_event) >= IDLE_TIMEOUT:
                        idle_killed = True
                        logger.error(
                            "Claude run idle for %.0fs (>=%ss) — killing as stuck.",
                            now - last_event, IDLE_TIMEOUT,
                        )
                        _kill_process_tree(process)
                        return
                    if progress_cb is not None and tracker.dirty:
                        try:
                            await progress_cb(tracker.snapshot(now))
                            flushed_once = True
                        except Exception as exc:  # noqa: BLE001 — progress is best-effort
                            logger.warning("progress update failed: %s", exc)

            stdout_task = asyncio.create_task(consume_stdout())
            stderr_task = asyncio.create_task(consume_stderr())
            monitor_task = asyncio.create_task(monitor())

            try:
                await asyncio.gather(stdout_task, stderr_task, process.wait())
            finally:
                monitor_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await monitor_task

            if idle_killed:
                _mark_finished()
                return _TIMEOUT_REPLY

            if process.returncode != 0:
                logger.error(
                    "Claude CLI failed (rc=%d) cmd=%s prompt=%r",
                    process.returncode, cmd, prompt[:200],
                )
                _mark_finished()
                if process.returncode < 0:
                    # Killed by a signal (deliberate stop / OOM), not a transient
                    # error — do not let the resume policy retry it.
                    return _INTERRUPTED_REPLY
                return "Sorry, I encountered an error processing your request."

            if final_result is None:
                logger.warning("Claude stream ended with no result event.")
                _mark_finished()
                return "Sorry, I couldn't parse the response."
            # Collapse the live status into a final summary — but only if we
            # ever posted one. Short runs (finished before the first throttle
            # tick) never created a status message, so stay silent here too.
            if progress_cb is not None and flushed_once:
                with contextlib.suppress(Exception):
                    await progress_cb(tracker.snapshot(loop.time(), done=True))
            _mark_finished()
            return final_result
        finally:
            if thread_ts is not None:
                self._processes.pop(thread_ts, None)

    @staticmethod
    def _log_stream_event(event: Any) -> None:
        """Log a single stream-json event from ``claude -p`` in human-readable form.

        All per-event logs are at DEBUG so the default INFO level matches the
        pre-stream-json behaviour (lifecycle only). Set ``LOG_LEVEL=DEBUG`` to
        see the full trace of Claude's tool calls and reasoning.
        """
        if not isinstance(event, dict):
            return
        etype = event.get("type")
        if etype == "system":
            logger.debug(
                "claude stream: system/%s session=%s cwd=%s tools=%s",
                event.get("subtype", ""),
                event.get("session_id", ""),
                event.get("cwd", ""),
                event.get("tools", ""),
            )
        elif etype == "assistant":
            for block in event.get("message", {}).get("content", []) or []:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text = (block.get("text") or "").strip()
                    if text:
                        logger.debug("claude text: %s", text[:2000])
                elif btype == "thinking":
                    thought = (block.get("thinking") or "").strip()
                    if thought:
                        logger.debug("claude thinking: %s", thought[:2000])
                elif btype == "tool_use":
                    logger.debug(
                        "claude tool_use: %s id=%s input=%s",
                        block.get("name", ""),
                        block.get("id", ""),
                        json.dumps(block.get("input", {}), ensure_ascii=False)[:2000],
                    )
        elif etype == "user":
            for block in event.get("message", {}).get("content", []) or []:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                content = block.get("content", "")
                if isinstance(content, list):
                    content = "".join(
                        c.get("text", "") for c in content if isinstance(c, dict)
                    )
                logger.debug(
                    "claude tool_result%s id=%s: %s",
                    " (error)" if block.get("is_error") else "",
                    block.get("tool_use_id", ""),
                    str(content)[:2000],
                )
        elif etype == "result":
            logger.debug(
                "claude stream: result subtype=%s duration_ms=%s num_turns=%s usage=%s",
                event.get("subtype", ""),
                event.get("duration_ms", ""),
                event.get("num_turns", ""),
                event.get("usage", ""),
            )
        else:
            logger.debug("claude stream: %s %s", etype, json.dumps(event, ensure_ascii=False)[:500])

    async def _build_thread_prompt(self, channel: str, thread_ts: str) -> str:
        """Fetch Slack thread history and format as a conversation prompt."""
        resp = await self._slack_client.conversations_replies(
            channel=channel, ts=thread_ts
        )
        messages = resp.get("messages", [])

        lines = ["The following is a Slack conversation. Continue assisting the user.\n"]
        for msg in messages:
            is_bot = (
                msg.get("user") == self._bot_user_id
                or msg.get("bot_id")
            )
            label = "[Assistant]" if is_bot else "[Human]"
            text = msg.get("text", "")
            lines.append(f"{label}: {text}")

        return "\n".join(lines)
