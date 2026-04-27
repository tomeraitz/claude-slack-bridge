"""
workflow.py — Daemon-side workflow engine for the ``/process`` plugin.

Owns the state machine that drives a feature from clarification → step
execution → approval → next step → done.  Spawns step sub-Claudes (one per
step), attaches exit handlers, mutates ``process.json`` (the per-worktree
state file), posts approval prompts to Slack, and handles the
``/next-task`` / ``/reject`` / ``/clean-process`` control commands.

The clarification sub-Claude (which writes ``process.json`` and
``.claude/processes/active``) is spawned by the daemon, NOT by this engine.
The engine takes over once the clarification skill posts ``START
<worktree-path>`` over the Unix socket — see :meth:`WorkflowEngine.handle_start_verb`.

Public API surface (called by ``slack_daemon.py``):

- :meth:`is_active_thread`, :meth:`get_active_worktree_for_thread`,
  :meth:`get_active_phase` — registry queries used by the Slack router.
- :meth:`admit_process_start` — atomic "is a process active?" check before
  the daemon spawns the clarification sub-Claude.
- :meth:`handle_start_verb` — called when ``START <worktree>`` arrives over
  the Unix socket.
- :meth:`handle_next_task`, :meth:`handle_reject`, :meth:`handle_clean_process`,
  :meth:`handle_thread_message` — control-command dispatch.
- :meth:`recover_on_startup` — rebuilds in-memory registry from disk after
  a daemon restart.

Mutation ownership: the engine is the **only** writer of ``process.json``
post-clarification.  Sub-Claudes read it and emit a one-line stdout summary;
the engine parses the summary and updates the file.

Active-marker race note: ``admit_process_start`` only checks that
``.claude/processes/active`` does not exist; the clarification sub-Claude
performs the atomic ``os.replace`` write itself.  Two simultaneous
``/process`` posts could in theory both pass the check; the second writer's
``os.replace`` would clobber the first's marker.  v1 accepts this tiny
window — worst case is one orphan worktree the user can manually
``/clean-process``.
"""

import asyncio
import json
import logging
import os
import re
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_handler import ClaudeHandler
from projects import ProjectResolver

logger = logging.getLogger(__name__)


# ============================================================================
# Constants
# ============================================================================

SUPPORTED_TEMPLATE_VERSION = 1

ACTIVE_MARKER_RELPATH = Path(".claude/processes/active")
ACTIVE_LOCK_RELPATH = Path(".claude/processes/active.lock")
PROCESS_JSON_RELPATH = Path(".claude/process.json")
LOGS_RELDIR = Path(".claude/logs")

DEFAULT_STEP_TIMEOUT_MINUTES = 60

# Phase values written to process.json["phase"].
PHASE_CLARIFYING = "clarifying"
PHASE_READY_FOR_NEXT_STEP = "ready_for_next_step"
PHASE_RUNNING_STEP = "running_step"
PHASE_WAITING_APPROVAL = "waiting_approval"
PHASE_DONE = "done"
PHASE_FAILED = "failed"

# Step status values.
STATUS_NOT_STARTED = "not started"
STATUS_IN_PROGRESS = "in progress"
STATUS_WAITING_APPROVAL = "waiting for approval"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"

# Env vars stripped before passing to sub-Claudes (mirrors ClaudeHandler).
_STRIPPED_ENV_VARS = (
    "CLAUDECODE",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "ANTHROPIC_API_KEY",
)

# Tail size of step log to embed in rejection_reason on failure.
_LOG_TAIL_BYTES = 1024

# KEY=VALUE marker pattern for stdout summary parsing.
_MARKER_RE = re.compile(r"^([A-Z_]+)=(.+)$")


# ============================================================================
# In-memory state record
# ============================================================================


@dataclass
class ActiveProcess:
    """Per-feature record cached by the engine.  The on-disk
    ``.claude/processes/active`` marker plus ``<worktree>/.claude/process.json``
    are the source of truth; this dataclass is the cache used for fast routing.
    """

    thread_ts: str
    channel: str
    project_dir: Path
    worktree: Path
    plugin_dir: str | None = None
    # Pending records staged by ``admit_process_start`` (before the
    # clarification skill has actually written process.json) have
    # ``worktree`` set to the project_dir as a placeholder; the field is
    # repointed in ``handle_start_verb``.
    pending: bool = False


# ============================================================================
# Pure helpers (module-level for testability)
# ============================================================================


def render_step_prompt(step: dict[str, Any], queued_input: list[str]) -> str:
    """Render the §7.1 step-spawn prompt.

    Substitutes ``step.name`` and ``step.command``; renders the
    ``pending_user_input`` block as one item per line, or ``(none)`` if the
    queue is empty.
    """
    step_name = step.get("name", "")
    step_command = step.get("command", "")
    if queued_input:
        queued_block = "\n".join(queued_input)
    else:
        queued_block = "(none)"

    return (
        f"You are running step **`{step_name}`** of an active `/process` workflow.\n"
        "\n"
        "**Context.** Read `.claude/process.json` first — it contains "
        "`task_description`, `slack_thread_ts`, prior step artifacts, and the "
        "feature slug. Do not modify it; the daemon owns it.\n"
        "\n"
        "**Pending user input.** While previous steps were running, the user "
        "posted the following messages (oldest first). Read them, factor them "
        "into your work, and call them out in your final summary if any change "
        "your behavior. The daemon has already cleared the queue — do not "
        "write back to it:\n"
        "\n"
        "```\n"
        f"{queued_block}\n"
        "```\n"
        "\n"
        f"**Your job.** Run the slash command `{step_command}` for this "
        "feature. Save any artifacts you produce inside this worktree (your "
        "`cwd`).\n"
        "\n"
        "**If you need clarification from the user.** Call `ask_on_slack`. "
        "Your message will land in the existing Slack thread automatically, "
        f"and the bridge will auto-prefix it with `[Step: {step_name}]` — you "
        "do not need to add the prefix yourself. Do not call `ask_on_slack` "
        "for things you can decide yourself.\n"
        "\n"
        "**Stdout contract.** On completion, print a single-line summary, "
        "then exit zero. The daemon parses your `result` for these optional "
        "`KEY=value` markers (one per line is fine):\n"
        "- `PR_URL=<url>` — recorded into `process.json.pr_link` (use this in "
        "the create-pr step).\n"
        "- Any other `KEY=value` is logged but ignored.\n"
        "\n"
        "On failure, exit non-zero with a one-line error message describing "
        "what went wrong. Do not exit zero on partial success."
    )


def scrape_kv_lines(text: str) -> dict[str, str]:
    """Extract ``KEY=value`` markers from a step's result text.

    Permissive: scans every line; captures any ``^[A-Z_]+=.+$`` match.  Only
    ``PR_URL`` is acted on by the engine in v1; all others are returned for
    logging.
    """
    markers: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        match = _MARKER_RE.match(line)
        if match:
            markers[match.group(1)] = match.group(2).strip()
    return markers


# ============================================================================
# WorkflowEngine
# ============================================================================


class WorkflowEngine:
    """Daemon-side state machine for the ``/process`` workflow.

    Args:
        slack_client: The daemon's async Slack WebClient (``app.client``).
        post_response: Async callable used to post messages to Slack with
            chunking.  Signature: ``(channel, thread_ts, text) -> None``.
            (Typically ``slack_daemon._post_response``.)
        resolver: Channel→project resolver (shared with ``ClaudeHandler``).
        in_container_mcp_config: Path to the in-container MCP config file
            consumed by sub-Claudes.  Defaults to ``/app/mcp.in-container.json``.
        step_timeout_minutes: Per-step wallclock timeout.  Falls back to the
            ``PROCESS_STEP_TIMEOUT_MINUTES`` env var, then to 60.
    """

    def __init__(
        self,
        slack_client: Any,
        post_response: Callable[[str, str, str], Awaitable[None]],
        resolver: ProjectResolver,
        in_container_mcp_config: str = "/app/mcp.in-container.json",
        step_timeout_minutes: int | None = None,
    ) -> None:
        self._slack_client = slack_client
        self._post_response = post_response
        self._resolver = resolver
        self._in_container_mcp_config = in_container_mcp_config
        if step_timeout_minutes is None:
            try:
                step_timeout_minutes = int(
                    os.getenv("PROCESS_STEP_TIMEOUT_MINUTES", str(DEFAULT_STEP_TIMEOUT_MINUTES))
                )
            except ValueError:
                step_timeout_minutes = DEFAULT_STEP_TIMEOUT_MINUTES
        self._step_timeout_seconds = step_timeout_minutes * 60

        # thread_ts → ActiveProcess.  Source of truth on disk; this is a cache.
        self._active: dict[str, ActiveProcess] = {}
        # Guards admit/start/cleanup.
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Registry queries
    # ------------------------------------------------------------------

    def is_active_thread(self, thread_ts: str) -> bool:
        """True if ``thread_ts`` belongs to a finalized active /process."""
        record = self._active.get(thread_ts)
        return record is not None and not record.pending

    def get_active_worktree_for_thread(self, thread_ts: str) -> Path | None:
        """Return the worktree path for an active thread, or ``None``."""
        record = self._active.get(thread_ts)
        if record is None or record.pending:
            return None
        return record.worktree

    def get_active_phase(self, thread_ts: str) -> str | None:
        """Read the current ``phase`` from the worktree's ``process.json``.

        Returns ``None`` if no active record exists or the file is missing.
        """
        record = self._active.get(thread_ts)
        if record is None or record.pending:
            return None
        try:
            state = self._read_process(record.worktree)
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            logger.warning("Could not read process.json for thread %s: %s", thread_ts, exc)
            return None
        return state.get("phase")

    # ------------------------------------------------------------------
    # Admission + start
    # ------------------------------------------------------------------

    async def admit_process_start(
        self,
        channel: str,
        project_dir: str,
        thread_ts: str,
    ) -> bool:
        """Reserve the active-process slot for a new ``/process``.

        Returns ``True`` when the caller should proceed to spawn the
        clarification sub-Claude; ``False`` when an existing process blocks
        admission (caller should post a refusal).

        Note: this performs only a check-and-stage of the in-memory record.
        The clarification sub-Claude is responsible for the atomic
        ``os.replace`` of ``.claude/processes/active`` (see module docstring).
        """
        async with self._lock:
            project_path = Path(project_dir)
            active_marker = project_path / ACTIVE_MARKER_RELPATH

            if active_marker.exists():
                logger.info(
                    "Refusing /process: marker already exists at %s", active_marker,
                )
                return False

            # Refuse if any in-memory record points at this project (covers
            # the race where two /process posts arrive before either has
            # written the marker).
            for record in self._active.values():
                if record.project_dir == project_path:
                    logger.info(
                        "Refusing /process: in-memory record exists for %s", project_path,
                    )
                    return False

            _, plugin_dir = self._resolver.get_project_config(channel)

            # Stage a pending record keyed by thread_ts.  The worktree path
            # is unknown until clarification finishes; placeholder = project_dir.
            self._active[thread_ts] = ActiveProcess(
                thread_ts=thread_ts,
                channel=channel,
                project_dir=project_path,
                worktree=project_path,
                plugin_dir=plugin_dir,
                pending=True,
            )
            logger.info(
                "Admitted /process for thread %s (project=%s, plugin_dir=%s).",
                thread_ts, project_path, plugin_dir,
            )
            return True

    async def handle_start_verb(self, worktree_path: str) -> None:
        """Engine entry from the Unix-socket ``START <worktree-path>`` verb.

        Loads ``process.json``, finalizes the in-memory record (matching it
        to a pending record by ``slack_thread_ts``), and kicks off the step
        loop.
        """
        worktree = Path(worktree_path).resolve()
        try:
            state = self._read_process(worktree)
        except FileNotFoundError:
            logger.error("START %s: process.json not found.", worktree)
            return
        except json.JSONDecodeError as exc:
            logger.error("START %s: process.json invalid: %s", worktree, exc)
            return

        if not self._check_template_version(state, worktree):
            return

        thread_ts = state.get("slack_thread_ts")
        channel = state.get("slack_channel")
        if not thread_ts or not channel:
            logger.error(
                "START %s: process.json missing slack_thread_ts/slack_channel.", worktree,
            )
            return

        async with self._lock:
            pending = self._active.get(thread_ts)
            if pending is not None and pending.pending:
                pending.worktree = worktree
                pending.pending = False
                record = pending
            else:
                # Recovery path or out-of-band /process (no admit step).
                # Resolve plugin_dir best-effort from the channel.
                _, plugin_dir = self._resolver.get_project_config(channel)
                project_dir = self._project_dir_for_worktree(worktree, channel)
                record = ActiveProcess(
                    thread_ts=thread_ts,
                    channel=channel,
                    project_dir=project_dir,
                    worktree=worktree,
                    plugin_dir=plugin_dir,
                    pending=False,
                )
                self._active[thread_ts] = record

        logger.info("START registered: thread=%s worktree=%s", thread_ts, worktree)
        await self._run_step_loop(record)

    # ------------------------------------------------------------------
    # Control commands
    # ------------------------------------------------------------------

    async def handle_next_task(self, thread_ts: str) -> None:
        """User typed ``/next-task``.  If currently waiting for approval,
        mark approved and advance; otherwise explain there's nothing to do.
        """
        record = self._active.get(thread_ts)
        if record is None or record.pending:
            return
        try:
            state = self._read_process(record.worktree)
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            logger.warning("/next-task: cannot read state: %s", exc)
            return

        if state.get("phase") != PHASE_WAITING_APPROVAL:
            await self._post(record, "Nothing to approve right now.")
            return

        idx = state.get("current_step_index", 0)
        steps = state.get("steps", [])
        if 0 <= idx < len(steps):
            steps[idx]["status"] = STATUS_APPROVED
            steps[idx]["rejection_reason"] = None

        next_idx = idx + 1
        if next_idx < len(steps):
            state["current_step_index"] = next_idx
            state["phase"] = PHASE_READY_FOR_NEXT_STEP
            self._write_process(record.worktree, state)
            await self._run_step_loop(record)
        else:
            state["phase"] = PHASE_DONE
            state["current_step_pid"] = None
            self._write_process(record.worktree, state)
            msg = "Process complete."
            if state.get("pr_link"):
                msg += f" PR: {state['pr_link']}"
            await self._post(record, msg)
            # Free up the active slot for the next /process; worktree stays
            # on disk for inspection (user can /clean-process to remove it).
            self._remove_active_marker(record.project_dir)
            self._active.pop(thread_ts, None)

    async def handle_reject(self, thread_ts: str, reason: str) -> None:
        """User typed ``/reject <reason>``.  Marks current step rejected."""
        record = self._active.get(thread_ts)
        if record is None or record.pending:
            return
        try:
            state = self._read_process(record.worktree)
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            logger.warning("/reject: cannot read state: %s", exc)
            return

        if state.get("phase") != PHASE_WAITING_APPROVAL:
            await self._post(record, "Nothing to reject right now.")
            return

        reason = reason.strip() or "(no reason given)"
        idx = state.get("current_step_index", 0)
        steps = state.get("steps", [])
        step_name = ""
        if 0 <= idx < len(steps):
            steps[idx]["status"] = STATUS_REJECTED
            steps[idx]["rejection_reason"] = reason
            step_name = steps[idx].get("name", "")
        state["phase"] = PHASE_FAILED
        self._write_process(record.worktree, state)
        await self._post(
            record,
            f"Step {step_name} rejected: {reason}. Halted. Reply "
            "/clean-process or describe what to do next.",
        )

    async def handle_clean_process(self, thread_ts: str) -> None:
        """User typed ``/clean-process``.  Kill running step, remove
        worktree, drop the active marker, clear the in-memory record.
        """
        record = self._active.get(thread_ts)
        if record is None:
            return

        worktree = record.worktree
        project_dir = record.project_dir

        # Kill the running step subprocess if any.
        pid = None
        if not record.pending:
            try:
                state = self._read_process(worktree)
                pid = state.get("current_step_pid")
            except (FileNotFoundError, json.JSONDecodeError):
                pass

        if pid:
            await self._kill_pid(pid)

        # Remove the worktree.  Best-effort: if git refuses (branch checked
        # out elsewhere, etc.), log + post but still drop the marker so the
        # user is not permanently locked out.
        worktree_err: str | None = None
        if not record.pending and worktree != project_dir:
            worktree_err = await self._git_worktree_remove(project_dir, worktree)

        # Always drop the active marker + lockfile and the in-memory record.
        self._remove_active_marker(project_dir)
        self._active.pop(thread_ts, None)

        if worktree_err:
            await self._post(
                record,
                f"Cleaned up (active marker dropped). git worktree remove "
                f"reported: {worktree_err}",
            )
        else:
            await self._post(record, "Cleaned up. Ready for the next /process.")

        # NOTE: optional ``git branch -D <branch>`` mentioned in §9.3 is
        # deferred to a future iteration — TODO.

    async def handle_thread_message(self, thread_ts: str, text: str) -> None:
        """Free-text message in an active /process thread.

        Caller (slack_daemon) has already filtered out:
        - threads with a pending MCP session (handled directly in §9.2 Case 1)
        - the control commands ``/next-task``, ``/reject``, ``/clean-process``
        """
        record = self._active.get(thread_ts)
        if record is None or record.pending:
            return
        try:
            state = self._read_process(record.worktree)
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            logger.warning("free-text: cannot read state: %s", exc)
            return

        phase = state.get("phase")
        if phase == PHASE_WAITING_APPROVAL:
            await self._post(
                record,
                "Use `/next-task` to approve, `/reject <reason>` to reject. "
                "Free-text discussion is not yet routed during approval (will "
                "be in v2). The step output is still in this thread above.",
            )
            return

        if phase == PHASE_RUNNING_STEP:
            queue = list(state.get("pending_user_input", []))
            queue.append(text)
            state["pending_user_input"] = queue
            self._write_process(record.worktree, state)
            await self._post(
                record,
                "Noted — will pass to the next step (it'll see this in its "
                "prompt). If you need it acted on now, wait for the current "
                "step to ask, or `/clean-process`.",
            )
            return

        if phase == PHASE_FAILED:
            queue = list(state.get("pending_user_input", []))
            queue.append(text)
            state["pending_user_input"] = queue
            self._write_process(record.worktree, state)
            await self._post(
                record,
                "This process has halted. Reply `/clean-process` to wipe it, "
                "or describe what you want next and I'll surface it on cleanup.",
            )
            return

        if phase == PHASE_DONE:
            # Should have been removed from registry already; defensive.
            await self._post(record, "Process is complete.")
            return

        # phase in (clarifying, ready_for_next_step) or anything unexpected:
        # don't mutate state; log only.
        logger.info(
            "free-text in thread %s with phase=%s — no action.", thread_ts, phase,
        )

    # ------------------------------------------------------------------
    # Restart recovery
    # ------------------------------------------------------------------

    async def recover_on_startup(self) -> None:
        """Rebuild in-memory state from each project's ``.claude/processes/active``.

        Called once during daemon startup before serving requests.  If a
        running step's pid is dead, marks the step rejected and posts a
        notice (per §11.2 #5 "Daemon crash recovery").
        """
        for channel_key, value in self._resolver.project_map.items():
            project_dir_str: str | None
            plugin_dir: str | None
            if isinstance(value, str):
                project_dir_str, plugin_dir = value, None
            elif isinstance(value, dict):
                project_dir_str = value.get("path")
                plugin_dir = value.get("plugin_dir")
            else:
                continue
            if not project_dir_str:
                continue

            project_dir = Path(project_dir_str)
            marker = project_dir / ACTIVE_MARKER_RELPATH
            if not marker.exists():
                continue

            try:
                worktree_path_text = marker.read_text(encoding="utf-8").strip()
            except OSError as exc:
                logger.warning("Could not read %s: %s", marker, exc)
                continue
            if not worktree_path_text:
                logger.warning("Empty active marker at %s — ignoring.", marker)
                continue

            worktree = Path(worktree_path_text)
            process_json = worktree / PROCESS_JSON_RELPATH
            if not process_json.exists():
                logger.warning(
                    "Active marker %s points at %s but %s missing — leaving as-is.",
                    marker, worktree, process_json,
                )
                continue

            try:
                state = self._read_process(worktree)
            except json.JSONDecodeError as exc:
                logger.error("Recovery: %s invalid JSON: %s", process_json, exc)
                continue

            thread_ts = state.get("slack_thread_ts")
            channel = state.get("slack_channel")
            if not thread_ts or not channel:
                logger.warning(
                    "Recovery: %s missing slack metadata — skipping.", process_json,
                )
                continue

            record = ActiveProcess(
                thread_ts=thread_ts,
                channel=channel,
                project_dir=project_dir,
                worktree=worktree,
                plugin_dir=plugin_dir,
                pending=False,
            )
            self._active[thread_ts] = record
            logger.info(
                "Recovered active process: thread=%s worktree=%s phase=%s",
                thread_ts, worktree, state.get("phase"),
            )

            if state.get("phase") == PHASE_RUNNING_STEP:
                pid = state.get("current_step_pid")
                if pid and self._pid_alive(pid):
                    logger.warning(
                        "Recovery: thread=%s step pid=%s is still alive; v1 "
                        "limitation — original engine is gone but subprocess "
                        "is running. Leaving alone.",
                        thread_ts, pid,
                    )
                else:
                    idx = state.get("current_step_index", 0)
                    steps = state.get("steps", [])
                    step_name = ""
                    if 0 <= idx < len(steps):
                        steps[idx]["status"] = STATUS_REJECTED
                        steps[idx]["rejection_reason"] = (
                            f"daemon restarted while step was running — "
                            f"pid {pid} gone"
                        )
                        step_name = steps[idx].get("name", "")
                    state["phase"] = PHASE_FAILED
                    state["current_step_pid"] = None
                    self._write_process(worktree, state)
                    try:
                        await self._post(
                            record,
                            f"Daemon restarted while step {step_name} was "
                            f"running (pid {pid} no longer alive). Step "
                            "marked rejected. Reply /clean-process or "
                            "describe what to do next.",
                        )
                    except Exception as exc:
                        logger.warning(
                            "Recovery notice post failed for thread %s: %s",
                            thread_ts, exc,
                        )

    # ------------------------------------------------------------------
    # Step loop (§9.1 core)
    # ------------------------------------------------------------------

    async def _run_step_loop(self, record: ActiveProcess) -> None:
        """Drive one transition of the §9.1 state machine.

        Spawns the next step sub-Claude when ``phase == ready_for_next_step``;
        otherwise no-ops (terminal phases, in-flight step, awaiting approval).
        """
        try:
            state = self._read_process(record.worktree)
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            logger.error("step loop: cannot read state for %s: %s", record.worktree, exc)
            return

        if not self._check_template_version(state, record.worktree, record=record):
            return

        phase = state.get("phase")
        if phase in (PHASE_DONE, PHASE_FAILED, PHASE_CLARIFYING):
            return
        if phase == PHASE_RUNNING_STEP:
            return  # exit handler will fire
        if phase == PHASE_WAITING_APPROVAL:
            return

        # phase should be PHASE_READY_FOR_NEXT_STEP.
        idx = state.get("current_step_index", 0)
        steps = state.get("steps", [])
        if not (0 <= idx < len(steps)):
            logger.error(
                "step loop: current_step_index=%s out of range (len=%d) for %s",
                idx, len(steps), record.worktree,
            )
            return
        step = steps[idx]

        queued_input = list(state.get("pending_user_input", []))
        state["pending_user_input"] = []
        prompt = render_step_prompt(step, queued_input)

        step["status"] = STATUS_IN_PROGRESS
        step["rejection_reason"] = None
        state["phase"] = PHASE_RUNNING_STEP
        self._write_process(record.worktree, state)

        log_path = record.worktree / LOGS_RELDIR / f"{step.get('name', 'step')}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        # Build env: copy parent, strip exfil-targets, then add /process vars.
        env = os.environ.copy()
        for key in _STRIPPED_ENV_VARS:
            env.pop(key, None)
        env["SLACK_THREAD_TS"] = state.get("slack_thread_ts", "")
        env["SLACK_CHANNEL"] = state.get("slack_channel", "")
        env["STEP_NAME"] = step.get("name", "")

        cmd = self._build_step_cmd(record.plugin_dir)

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=str(record.worktree),
            )
        except FileNotFoundError:
            logger.error("step loop: claude CLI not found — is it installed?")
            await self._fail_step(
                record, state, step, "claude CLI not found in PATH", log_path, exit_code=-1,
            )
            return

        # Persist pid so /clean-process and recovery can act on it.
        state["current_step_pid"] = process.pid
        self._write_process(record.worktree, state)

        # Spawn the exit-handler task and return; the engine is event-driven.
        asyncio.create_task(
            self._await_step_exit(record, step, process, prompt, log_path)
        )

    async def _await_step_exit(
        self,
        record: ActiveProcess,
        step: dict[str, Any],
        process: asyncio.subprocess.Process,
        prompt: str,
        log_path: Path,
    ) -> None:
        """Wait for the step subprocess to exit; update state and post."""
        timed_out = False
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(input=prompt.encode("utf-8")),
                timeout=self._step_timeout_seconds,
            )
        except asyncio.TimeoutError:
            timed_out = True
            process.kill()
            try:
                stdout_bytes, stderr_bytes = await process.communicate()
            except Exception:
                stdout_bytes, stderr_bytes = b"", b""

        stdout_text = stdout_bytes.decode("utf-8", errors="replace")
        stderr_text = stderr_bytes.decode("utf-8", errors="replace")
        rc = process.returncode if process.returncode is not None else -1

        # Tee combined output into the log for debuggability.
        try:
            log_path.write_text(
                f"=== stdout ===\n{stdout_text}\n=== stderr ===\n{stderr_text}\n",
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("Could not write step log %s: %s", log_path, exc)

        # Re-load state so we don't clobber any concurrent updates (e.g.
        # /clean-process flipping things while we were running).
        try:
            state = self._read_process(record.worktree)
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            logger.error("post-step: cannot read state: %s", exc)
            return

        # If /clean-process already wiped the registry, bail.
        if record.thread_ts not in self._active:
            logger.info("post-step: thread %s already cleaned up.", record.thread_ts)
            return

        if timed_out or rc != 0:
            err_summary = (
                f"timed out after {self._step_timeout_seconds}s"
                if timed_out else f"exit code {rc}"
            )
            await self._fail_step(
                record, state, self._current_step_in_state(state),
                err_summary, log_path, exit_code=rc,
            )
            return

        # Clean exit — parse result and mark waiting_approval.
        result_text = ClaudeHandler._parse_response(stdout_text.strip())
        markers = scrape_kv_lines(result_text)
        if markers:
            logger.info("step %s markers: %s", step.get("name"), markers)
        if "PR_URL" in markers:
            state["pr_link"] = markers["PR_URL"]

        current_step = self._current_step_in_state(state)
        if current_step is not None:
            current_step["status"] = STATUS_WAITING_APPROVAL
            current_step["rejection_reason"] = None
        state["phase"] = PHASE_WAITING_APPROVAL
        state["current_step_pid"] = None
        self._write_process(record.worktree, state)

        step_name = step.get("name", "")
        await self._post(
            record,
            f"Step {step_name} done. Reply /next-task to approve, or "
            f"/reject <reason> to reject.\n\n{result_text}",
        )

    async def _fail_step(
        self,
        record: ActiveProcess,
        state: dict[str, Any],
        step: dict[str, Any] | None,
        err_summary: str,
        log_path: Path,
        exit_code: int,
    ) -> None:
        """Mark the current step rejected, set phase=failed, post failure."""
        log_tail = ""
        try:
            with open(log_path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - _LOG_TAIL_BYTES))
                log_tail = fh.read().decode("utf-8", errors="replace")
        except OSError:
            pass

        rejection_reason = (
            f"{err_summary}\n--- last {_LOG_TAIL_BYTES}B of log ---\n{log_tail}"
        )

        step_name = ""
        if step is not None:
            step["status"] = STATUS_REJECTED
            step["rejection_reason"] = rejection_reason
            step_name = step.get("name", "")
        state["phase"] = PHASE_FAILED
        state["current_step_pid"] = None
        self._write_process(record.worktree, state)

        await self._post(
            record,
            f"Step {step_name} failed ({err_summary}). See {log_path}. "
            "Reply /clean-process or describe what to do next.",
        )

    # ------------------------------------------------------------------
    # I/O helpers
    # ------------------------------------------------------------------

    def _read_process(self, worktree: Path) -> dict[str, Any]:
        path = worktree / PROCESS_JSON_RELPATH
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def _write_process(self, worktree: Path, state: dict[str, Any]) -> None:
        """Atomic write: tmp + ``os.replace``."""
        path = worktree / PROCESS_JSON_RELPATH
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    def _build_step_cmd(self, plugin_dir: str | None) -> list[str]:
        """Mirror ``ClaudeHandler._build_cmd`` shape for step sub-Claudes."""
        cmd = ["claude", "-p"]
        if plugin_dir:
            cmd.extend(["--plugin-dir", plugin_dir])
        cmd.extend([
            "--mcp-config", self._in_container_mcp_config,
            "--strict-mcp-config",
            "--dangerously-skip-permissions",
            "--output-format", "json",
        ])
        return cmd

    def _check_template_version(
        self,
        state: dict[str, Any],
        worktree: Path,
        record: ActiveProcess | None = None,
    ) -> bool:
        """Verify ``state["version"]`` is supported.  Returns ``True`` to
        continue, ``False`` after posting a Slack upgrade message.
        """
        version = state.get("version", 1)  # legacy default
        if version == SUPPORTED_TEMPLATE_VERSION:
            return True

        msg = (
            f"process-template.json has version {version}, but this daemon "
            f"supports version {SUPPORTED_TEMPLATE_VERSION}. Re-run "
            "/process-setup or upgrade the bridge."
        )
        logger.error("Version mismatch in %s: %s", worktree, msg)
        if record is not None:
            asyncio.create_task(self._post(record, msg))
        else:
            channel = state.get("slack_channel")
            thread_ts = state.get("slack_thread_ts")
            if channel and thread_ts:
                asyncio.create_task(
                    self._post_response(channel, thread_ts, msg)
                )
        return False

    async def _post(self, record: ActiveProcess, text: str) -> None:
        """Post a Slack message to the active thread."""
        try:
            await self._post_response(record.channel, record.thread_ts, text)
        except Exception as exc:
            logger.warning(
                "Failed to post to channel=%s thread=%s: %s",
                record.channel, record.thread_ts, exc,
            )

    @staticmethod
    def _current_step_in_state(state: dict[str, Any]) -> dict[str, Any] | None:
        idx = state.get("current_step_index", 0)
        steps = state.get("steps", [])
        if 0 <= idx < len(steps):
            return steps[idx]
        return None

    def _project_dir_for_worktree(
        self, worktree: Path, channel: str,
    ) -> Path:
        """Best-effort: find the project dir for a worktree.

        Used in recovery / out-of-band START.  Falls back to the resolver's
        channel mapping; if that fails, walks up from the worktree looking
        for ``.claude/processes/active`` matching this worktree.
        """
        project_dir, _ = self._resolver.get_project_config(channel)
        if project_dir:
            return Path(project_dir)
        # Worktree convention is ``<project>/.claude/worktrees/<feature>``.
        for parent in worktree.parents:
            marker = parent / ACTIVE_MARKER_RELPATH
            if marker.exists():
                return parent
        # Last resort: assume one level up from .claude/worktrees/<f>.
        try:
            return worktree.parents[2]
        except IndexError:
            return worktree

    @staticmethod
    def _remove_active_marker(project_dir: Path) -> None:
        """Delete ``.claude/processes/active`` and ``active.lock`` if present."""
        for rel in (ACTIVE_MARKER_RELPATH, ACTIVE_LOCK_RELPATH):
            path = project_dir / rel
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning("Could not remove %s: %s", path, exc)

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        """Return True if ``pid`` is alive (``os.kill(pid, 0)`` semantics)."""
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    async def _kill_pid(self, pid: int) -> None:
        """Send SIGTERM, wait briefly, then SIGKILL if still alive."""
        if not self._pid_alive(pid):
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as exc:
            logger.warning("SIGTERM pid=%s failed: %s", pid, exc)
            return
        # Short grace period.
        for _ in range(20):
            await asyncio.sleep(0.1)
            if not self._pid_alive(pid):
                return
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError as exc:
            logger.warning("SIGKILL pid=%s failed: %s", pid, exc)

    @staticmethod
    async def _git_worktree_remove(project_dir: Path, worktree: Path) -> str | None:
        """Run ``git worktree remove --force <worktree>`` from project_dir.

        Returns ``None`` on success, or an error string for posting back to
        Slack.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "worktree", "remove", "--force", str(worktree),
                cwd=str(project_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=30.0,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return "git worktree remove timed out after 30s"
        except FileNotFoundError:
            return "git CLI not found"
        except OSError as exc:
            return f"git worktree remove spawn failed: {exc}"
        if proc.returncode != 0:
            return stderr_bytes.decode("utf-8", errors="replace").strip() or (
                f"git worktree remove exited {proc.returncode}"
            )
        return None
