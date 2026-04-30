"""Loop 130: Textual TUI for qwen-coder-mcp.

A claude-code / ml-intern style chat interface with slash commands. The
slash-command parser is pure (no Textual dependency) so it can be unit
tested without spinning up an App. The Textual layer wires user input
to the parser and dispatches to the existing MCP tool helpers
(`web_tools`, `fs_tools`, `qwen_client`).

Slash commands implemented:
  /help                         Show command list
  /search <query>               Run web_search and render results
  /fetch <url>                  Fetch a URL's text body
  /read <path>                  Read a file from the repo root
  /ls [path]                    List a directory
  /find_bugs <path>             Run Qwen find_bugs on a file's contents
  /explain <path>               Run Qwen explain_code on a file's contents
  /apply <path>                 Write the assistant's last reply to <path>
                                  as a unified diff via apply_patch (preview
                                  with check_only first; actual apply only if
                                  preview succeeds)
  /history [n]                  Show the last N (default 10) chat turns
  /quit                         Exit the TUI

A line that does NOT start with `/` is treated as a free-form chat
message and routed to the QwenClient with the coder system prompt.

The TUI keeps a running list of `ChatMessage` entries so multi-turn
conversation memory is preserved within a single session.
"""
from __future__ import annotations

import os
import json
import sys
import signal
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import agent_loop, fs_tools, prompts, shell_tools, web_tools
from .qwen_client import ChatMessage, QwenClient


def _safe_markup(text: object) -> str:
    """Escape dynamic content before interpolating it into a Rich markup
    string. Without this, model output containing literal ``[/x]`` (e.g.
    box-drawing progress characters like ``[/▍]`` or angle-bracketed
    pseudo-tags) crashes the RichLog with ``MarkupError``. We import
    ``rich.markup.escape`` lazily so headless/pure-API usages of this
    module don't require Rich.
    """
    s = text if isinstance(text, str) else str(text)
    try:
        from rich.markup import escape
    except ImportError:
        return s
    return escape(s)


def _safe_log_write(log: Any, content: Any) -> None:
    """Defensive ``RichLog.write`` wrapper.

    Loops 262/263: even after escaping the obvious dynamic-content
    sites, model-emitted tool args / tool output / exception messages
    can still find their way into a markup-templated status line. Rather
    than add a `_safe_markup` call to every interpolation point and
    forget one in the next refactor, this helper tries the markup write
    and -- on ``rich.errors.MarkupError`` only -- falls back to writing
    the same content fully escaped. The markup styling on the prefix is
    lost in the fallback, but the line still renders and the TUI keeps
    running. Any other exception (renderer crash, log unmounted) is
    swallowed because logging must never break the agent loop.
    """
    try:
        from rich.errors import MarkupError
    except ImportError:  # pragma: no cover - rich always present at runtime
        MarkupError = Exception  # type: ignore[assignment,misc]
    try:
        log.write(content)
    except MarkupError:
        # Re-try with the entire content escaped. Prefix styling is
        # lost in the fallback, but the line still renders.
        try:
            log.write(_safe_markup(content if isinstance(content, str) else str(content)))
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        # Log unmounted, IO error, etc -- never crash the caller.
        pass


@dataclass
class SlashCommand:
    name: str
    args: list[str] = field(default_factory=list)
    rest: str = ""


def format_engine_probe_lines(probe: dict | None) -> list[str]:
    """Format the loop-219/220 engine /health probe result into the
    optional second-line banner the TUI shows underneath the
    /v1/models check.

    Returns a list of pre-styled (Rich-markup) lines:
      * empty list when ``probe`` is None or already healthy -- the
        first banner line already covered the happy path
      * one or two lines describing the divergence when probe ok=False

    Pure function so the test suite can pin every branch without
    instantiating the Textual App.
    """
    if not probe:
        return []
    if probe.get("ok"):
        # Both probes agreed -- the API-side line is enough.
        return []
    err = probe.get("error") or f"status {probe.get('status')!r}"
    hint = probe.get("hint")
    suffix = f"  [dim]({hint})[/dim]" if hint else ""
    return [f"[yellow]⚠ engine not ready:[/yellow] {err}{suffix}"]


def parse_slash(line: str) -> SlashCommand | None:
    """Return a `SlashCommand` if `line` begins with `/`, else `None`.

    `args` is the whitespace-split tail; `rest` is the raw tail (kept
    for commands like `/search` whose query may contain spaces).
    """
    if not line or not line.startswith("/"):
        return None
    body = line[1:].strip()
    if not body:
        return SlashCommand(name="")
    parts = body.split(None, 1)
    name = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""
    args = rest.split() if rest else []
    return SlashCommand(name=name, args=args, rest=rest)


SLASH_COMMANDS: tuple[str, ...] = (
    "/help",
    "/search",
    "/fetch",
    "/read",
    "/view",
    "/ls",
    "/find_bugs",
    "/explain",
    "/apply",
    "/history",
    "/diff",
    "/run",
    "/run_on",
    "/run_off",
    "/runs",
    "/yes",
    "/no",
    "/grep",
    "/find",
    "/clear",
    "/resume",
    "/checkpoints",
    "/save",
    "/git",
    "/tests",
    "/tokens",
    "/lat",
    "/sysprompt",
    "/model",
    "/undo",
    "/retry",
    "/sysinfo",
    "/memory",
    "/export",
    "/pin",
    "/unpin",
    "/pinned",
    "/open",
    "/cd",
    "/quit",
    "/agent",
    "/agent_off",
    "/agent_on",
    "/agent_write_off",
    "/agent_write_on",
    "/confirm_writes_off",
    "/confirm_writes_on",
    "/allow_all",
    "/safe_mode",
    "/mouse",
    "/select",
    "/loop",
    "/tools",
)


def slash_completions(prefix: str) -> list[str]:
    """Return slash-command names whose name starts with `prefix`.

    Empty input or input without a leading slash returns []. Used by the
    Textual Input suggester to drive tab completion. Pure so unit tests
    can pin behaviour without booting the App.
    """
    if not prefix or not prefix.startswith("/"):
        return []
    head = prefix.split()[0] if prefix else prefix
    return [name for name in SLASH_COMMANDS if name.startswith(head)]


HELP_TEXT = """\
Slash commands:
  /help [term]         Show this help; optional substring filter
  /search <query>      DuckDuckGo web search
  /fetch <url>         Fetch a URL's text body
  /read <path>         Read a file from the repo root
  /view <path> [s] [e] [--plain]   Read a 1-based inclusive line range
                                   with "<n> | " prefixes (loop 253)
  /ls [path]           List a directory
  /find_bugs <path>    Qwen review for bugs
  /explain <path>      Qwen explanation of a file
  /apply               Apply the last assistant reply as a unified diff
  /history [n|clear]   Show the last N chat turns (default 10) or clear them
  /diff <a> <b>        Unified diff between two files (or /diff <path> vs HEAD)
  /run [--yes] <cmd>   Run a shell command (10s timeout, deny list).
                       Default behaviour (loop 266): two-phase preview.
                       /run <cmd> stages the command with a stage_id
                       and shows a preview; confirm with /yes <id> or
                       cancel with /no <id>. Add --yes to bypass
                       staging and execute immediately, or /run_on
                       to auto-approve every /run this session.
  /yes [stage_id]      Execute a staged /run (latest if id omitted)
  /no  [stage_id]      Cancel a staged /run (latest if id omitted)
  /run_on              Auto-approve every /run for this session (loop 250)
  /run_off             Disable /run auto-approve (default; --yes still works)
  /runs [N|--json]     Show last N (default 10) /run audit records (loop 251)
  /grep <pat> [path] [--ext]
                       Recursive regex search; --py/--md/--json filters by suffix
  /find <glob> [path]  Glob search through the repo
  /clear               Clear chat history
  /resume [--preview]  Reload .agent/agent_state.json into chat history;
                       `--preview` (also `--dry-run`) shows the diff and
                       leaves history untouched
  /checkpoints [load N|prune K|diff N [--inline]|diff --since-resume [--inline]|export N path [--gzip]]
                       List rotated agent-state snapshots; `load N` rehydrates
                       snapshot N (1-based, oldest first) into history;
                       `prune K` deletes all but the newest K snapshots;
                       `diff N` compares current history vs snapshot N;
                       `diff --since-resume` diffs against whatever /resume
                       would load (`--inline` adds per-message unified diffs)
  /save <path>         Save the current chat transcript to a file
  /git <subcmd>        Read-only git status / log / diff / show / branch
  /tests [args]        Run pytest in the repo
  /tokens [--json [--top K]]  Estimate total tokens in current chat history
  /lat [N|reset] [--json]
                       Show the last N agent turns' timing breakdown
                       (TTFT, per-tool latencies, summary). Default N=1.
  /sysprompt [text]    Show or replace the system prompt
  /model [id]          Show or switch the served model id
  /undo                Pop the last user/assistant exchange
  /retry               Re-send the last user message
  /sysinfo [--json] [--probe]    Snapshot of backend health, model, root, history
  /memory [show|--json|task <text>|todo add <id> <desc>|todo done <id>|
           todo del <id>|fact <key> <value>|decision <text>|clear]
                       Inspect or manage the persistent task memory injected
                       into every chat (loop 244). Requires QWEN_TASK_MEMORY=1.
  /export <path>       Export full chat as Markdown
  /pin <path> [path...]
                       Attach one or more files to the system prompt for the
                       rest of the session
  /unpin               Clear all pinned files from the system prompt
  /pinned              List currently pinned files
  /open <path>         Launch $EDITOR on a file in the repo
  /cd [path]           Show or change the fs sandbox root for the session
  /agent <task>        Run an agentic tool-calling turn (one-off, read-only)
  /agent --write <task> Same, but with fs_write + apply_patch enabled
  /agent --max N <task> Override the 6-step cap (1..50); combinable with --write
  /agent --resume <task> Pre-load the latest agent checkpoint into history
                       before running the turn; combinable with --write/--max
  /agent_on            Make all subsequent chats agentic by default
  /agent_off           Disable default agent mode (back to plain chat)
  /agent_write_on      Enable fs_write + apply_patch in default agent mode
  /agent_write_off     Disable write tools in default agent mode
  /confirm_writes_on   Pop a y/n modal before each destructive tool call (default)
  /confirm_writes_off  Auto-approve destructive tool calls (audit-log only)
  /allow_all           One-shot: agent_on + agent_write_on + confirm_writes_off
                       + run_auto_approve. Maximum autonomy; use with care.
  /safe_mode           One-shot inverse: agent_off + agent_write_off
                       + confirm_writes_on + run_auto_approve_off.
  /mouse [on|off|toggle]
                       Toggle Textual's mouse capture. /mouse off lets
                       you click-drag to select text in the response
                       region and copy with the terminal's own keys
                       (Cmd-C / Ctrl-Shift-C / right-click). /mouse on
                       restores Textual's mouse handling.
  /select              Alias for /mouse off (enable terminal-native
                       text selection on the response region).
  /loop [start|stop|kill|status|tail [N]]
                       Manage the autonomous self-improvement loop
                       (`agent/loop.py`) as a detached subprocess.
                       /loop with no arg = /loop status.
  /tools               List the read-only and write tool registries
  /quit                Exit

@<path> tokens in plain chat are expanded inline as file contents.
@@<path> tokens inline the FULL file (no 8KB cap, sandbox limits still apply).
@web:<url> tokens fetch a URL and inline its body.
@search:<query> tokens run a DuckDuckGo search and inline the top results.
Anything not starting with `/` is sent to Qwen as a chat message.
"""


def _render_search(query: str, max_results: int = 5) -> str:
    try:
        results = web_tools.web_search(query, max_results=max_results)
    except Exception as exc:  # noqa: BLE001
        return f"web_search error: {type(exc).__name__}: {exc}"
    return web_tools.format_search_results(results)


def _render_fetch(url: str) -> str:
    try:
        res = web_tools.fetch_url(url)
    except Exception as exc:  # noqa: BLE001
        return f"fetch_url error: {type(exc).__name__}: {exc}"
    if res.get("error") == "non_text_content":
        return f"refused non-text: {res.get('content_type')}"
    head = f"# {res['url']} (status={res['status']})\n"
    return head + str(res.get("text", ""))[:8000]


def _render_read(cfg: fs_tools.FsConfig, path: str) -> str:
    try:
        res = fs_tools.read_file(cfg, path)
    except fs_tools.FsError as exc:
        return f"read_file error: {exc}"
    return fs_tools.format_read(res)


def _render_view(cfg: fs_tools.FsConfig, args: list[str]) -> str:
    """Loop 253: line-range reader with optional line numbers.

    Usage:
      /view <path>                       full file with "<n> | " prefixes
      /view <path> <start>               start..start+50 lines
      /view <path> <start> <end>         inclusive 1-based range
      /view <path> <start> <end> --plain drop the line-number prefix
    """
    if not args:
        return "usage: /view <path> [start] [end] [--plain]"
    plain = False
    pos: list[str] = []
    for a in args:
        if a == "--plain":
            plain = True
        else:
            pos.append(a)
    if not pos:
        return "usage: /view <path> [start] [end] [--plain]"
    path = pos[0]
    start: int | None = None
    end: int | None = None
    if len(pos) >= 2:
        try:
            start = int(pos[1])
        except ValueError:
            return f"view error: invalid start line {pos[1]!r}"
    if len(pos) >= 3:
        try:
            end = int(pos[2])
        except ValueError:
            return f"view error: invalid end line {pos[2]!r}"
    elif start is not None:
        end = start + 49
    try:
        res = fs_tools.read_file(
            cfg,
            path,
            start_line=start,
            end_line=end,
            line_numbers=not plain,
        )
    except fs_tools.FsError as exc:
        return f"view error: {exc}"
    rng = res.get("range")
    total = res.get("total_lines", "?")
    if rng:
        head = f"# {path} lines {rng['start']}-{rng['end']} of {total}\n"
    else:
        head = f"# {path} ({total} lines)\n"
    return head + str(res.get("text", ""))


def _render_ls(cfg: fs_tools.FsConfig, path: str) -> str:
    try:
        res = fs_tools.list_dir(cfg, path or ".")
    except fs_tools.FsError as exc:
        return f"list_dir error: {exc}"
    return fs_tools.format_list(res)


def _render_find_bugs(client: QwenClient, cfg: fs_tools.FsConfig, path: str) -> str:
    try:
        res = fs_tools.read_file(cfg, path)
    except fs_tools.FsError as exc:
        return f"read_file error: {exc}"
    return client.system_user(
        prompts.REVIEWER_SYSTEM,
        prompts.find_bugs_user(path, str(res["text"])),
    )


def _render_explain(client: QwenClient, cfg: fs_tools.FsConfig, path: str) -> str:
    try:
        res = fs_tools.read_file(cfg, path)
    except fs_tools.FsError as exc:
        return f"read_file error: {exc}"
    return client.system_user(
        prompts.CODER_SYSTEM, prompts.explain_user(str(res["text"]))
    )


def extract_diff(text: str) -> str | None:
    """Return the first unified-diff block found in `text`.

    Recognises a fenced code block whose language hint is `diff` or
    `patch`, otherwise looks for a `diff --git` header anywhere in the
    text and returns from that point onward (stripping a trailing fence
    if present). Returns `None` if no diff is found.
    """
    if not text:
        return None
    for tag in ("```diff", "```patch"):
        idx = text.find(tag)
        if idx >= 0:
            start = idx + len(tag)
            if start < len(text) and text[start] == "\n":
                start += 1
            end = text.find("```", start)
            if end < 0:
                return text[start:].strip("\n") + "\n"
            return text[start:end].strip("\n") + "\n"
    git_idx = text.find("diff --git")
    if git_idx >= 0:
        body = text[git_idx:]
        end = body.find("```")
        if end >= 0:
            body = body[:end]
        return body.strip("\n") + "\n"
    return None


def _last_assistant_reply(history: list[ChatMessage]) -> str | None:
    for msg in reversed(history):
        if msg.role == "assistant":
            return msg.content
    return None


def _format_checkpoint_listing(
    snapshots: list[Path], *, width: int | None = None
) -> str:
    """Render rotated agent-state snapshots as a one-per-line listing.

    Lines are 1-indexed, oldest-first, and include the file's mtime in
    UTC ISO-8601 plus its size in bytes. Pure so /checkpoints tests
    don't need to spin up the App.

    ``width=None`` derives the column count from the current terminal
    via ``shutil.get_terminal_size`` and truncates the snapshot name
    with a trailing ``…`` when the row would otherwise overflow. Floored
    at 40 cols so absurdly narrow terminals still render something
    legible; capped only by the longest fixed prefix so ridiculously
    wide terminals don't pad. Pass an explicit integer to override.
    """
    if not snapshots:
        return "(no rotated checkpoints found)"
    from datetime import datetime, timezone

    if width is None:
        import shutil

        try:
            cols = shutil.get_terminal_size((80, 24)).columns
        except Exception:  # noqa: BLE001
            cols = 80
        width = max(40, cols)

    # Fixed prefix: " NNN. YYYY-MM-DD HH:MM:SSZ  NNNNNNNB  "
    #               4 + 2 + 20 + 2 + 7 + 2          = 37 chars
    prefix_width = 37
    name_budget = max(8, width - prefix_width)

    def _truncate(name: str) -> str:
        if len(name) <= name_budget:
            return name
        return name[: name_budget - 1] + "…"

    rows: list[str] = []
    for idx, snap in enumerate(snapshots, start=1):
        try:
            stat = snap.stat()
            mtime = datetime.fromtimestamp(
                stat.st_mtime, tz=timezone.utc
            ).strftime("%Y-%m-%d %H:%M:%SZ")
            size = stat.st_size
            rows.append(
                f"{idx:>3}. {mtime}  {size:>7}B  {_truncate(snap.name)}"
            )
        except OSError as exc:
            rows.append(f"{idx:>3}. <stat failed: {exc}>  {_truncate(snap.name)}")
    return "\n".join(rows)


def format_history_diff(
    current: list[ChatMessage],
    snapshot: list[ChatMessage],
    *,
    snapshot_label: str = "snapshot",
    preview_chars: int | None = 60,
    inline_diff: bool = False,
    inline_diff_max_lines: int = 12,
) -> str:
    """Render a paired diff of two histories by index.

    The two histories are walked in parallel up to the longer length.
    For each index we report one of:
      ``=  i. role  (preview)``    same role and identical content
      ``~  i. role  (preview)``    same role but content differs
      ``≠  i. cur != snap``        role disagrees at this index
      ``+  i. role  (preview)``    only present in current (snapshot ran out)
      ``-  i. role  (preview)``    only present in snapshot (current ran out)

    The header line summarises totals. Pure: no I/O, deterministic
    output for a given pair, callers preview characters at a fixed
    cap to keep the rendering compact in the TUI log.

    ``preview_chars=None`` derives the cap from the current terminal
    width (via ``shutil.get_terminal_size``) so wide terminals get
    longer previews and narrow ones don't overflow. Floored at 20 and
    capped at 200 so neither extreme breaks the layout.
    """
    if preview_chars is None:
        import shutil

        try:
            cols = shutil.get_terminal_size((80, 24)).columns
        except Exception:  # noqa: BLE001
            cols = 80
        # Row prefix "~  999. assistant  (" + ")" is ~22 chars.
        # Reserve that and pad a few more for safety.
        preview_chars = max(20, min(200, cols - 28))

    def _preview(text: str) -> str:
        flat = text.replace("\n", " ").strip()
        if len(flat) <= preview_chars:
            return flat
        return flat[: preview_chars - 1] + "…"

    n = max(len(current), len(snapshot))
    if n == 0:
        return f"(both current and {snapshot_label} are empty)"

    same = changed = role_mismatch = added = dropped = 0
    rows: list[str] = []
    for i in range(n):
        cur = current[i] if i < len(current) else None
        snp = snapshot[i] if i < len(snapshot) else None
        if cur is not None and snp is None:
            added += 1
            rows.append(f"+  {i+1:>3}. {cur.role}  ({_preview(cur.content)})")
        elif cur is None and snp is not None:
            dropped += 1
            rows.append(f"-  {i+1:>3}. {snp.role}  ({_preview(snp.content)})")
        elif cur is not None and snp is not None:
            if cur.role != snp.role:
                role_mismatch += 1
                rows.append(
                    f"≠  {i+1:>3}. {cur.role} != {snp.role}"
                )
            elif cur.content == snp.content:
                same += 1
                rows.append(f"=  {i+1:>3}. {cur.role}  ({_preview(cur.content)})")
            else:
                changed += 1
                rows.append(
                    f"~  {i+1:>3}. {cur.role}  "
                    f"({_preview(cur.content)})"
                )
                if inline_diff:
                    import difflib

                    diff_lines = list(
                        difflib.unified_diff(
                            snp.content.splitlines(),
                            cur.content.splitlines(),
                            fromfile=f"{snapshot_label}#{i+1}",
                            tofile=f"current#{i+1}",
                            n=1,
                            lineterm="",
                        )
                    )
                    truncated = False
                    if len(diff_lines) > inline_diff_max_lines:
                        diff_lines = diff_lines[:inline_diff_max_lines]
                        truncated = True
                    for dl in diff_lines:
                        rows.append(f"     {dl}")
                    if truncated:
                        rows.append(
                            f"     … (diff truncated to "
                            f"{inline_diff_max_lines} lines)"
                        )

    header = (
        f"diff vs {snapshot_label}: "
        f"current={len(current)} {snapshot_label}={len(snapshot)} "
        f"same={same} changed={changed} "
        f"role_mismatch={role_mismatch} "
        f"added={added} dropped={dropped}"
    )
    return header + "\n" + "\n".join(rows)


DEFAULT_ROTATION_KEEP = 5
_ROTATION_KEEP_ENV = "QWEN_AGENT_ROTATION_KEEP"


def resolve_rotation_keep(env: dict[str, str] | None = None) -> int:
    """Resolve the rotation-keep count from the environment.

    Reads ``QWEN_AGENT_ROTATION_KEEP``; falls back to
    ``DEFAULT_ROTATION_KEEP`` (5) when unset, empty, or unparseable.
    Negative values are clamped to 0 (= "retain everything"). Pure so
    unit tests can pass an isolated dict instead of mutating
    ``os.environ``.
    """
    import os

    src = os.environ if env is None else env
    raw = src.get(_ROTATION_KEEP_ENV)
    if raw is None or raw.strip() == "":
        return DEFAULT_ROTATION_KEEP
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_ROTATION_KEEP
    return max(value, 0)


def render_checkpoint_hint(fs_cfg: fs_tools.FsConfig) -> str | None:
    """When a usable agent checkpoint exists at ``.agent/agent_state.json``
    (or its rotations), return a single-line hint suggesting ``/resume``.
    Returns ``None`` when no checkpoint is loadable, so callers can
    short-circuit. Pure / non-raising — used at TUI boot to surface a
    crash-recovery affordance without silently loading state across
    storage layers.
    """
    try:
        target = fs_cfg.root / ".agent" / "agent_state.json"
        history, source = agent_loop.load_latest_checkpoint(target)
    except Exception:  # noqa: BLE001
        return None
    if not history or source is None:
        return None
    return (
        f"[yellow]·[/yellow] agent checkpoint with {len(history)} messages "
        f"found ({source.name}); type [bold]/resume[/bold] to load it"
    )


def _role_counts(history: list[ChatMessage]) -> dict[str, int]:
    """Count messages by role for status renderings (e.g. /resume)."""
    counts: dict[str, int] = {}
    for msg in history:
        counts[msg.role] = counts.get(msg.role, 0) + 1
    return counts


def _render_apply(
    cfg: fs_tools.FsConfig, history: list[ChatMessage]
) -> str:
    reply = _last_assistant_reply(history)
    if reply is None:
        return "no assistant reply to apply"
    diff = extract_diff(reply)
    if diff is None:
        return "no unified diff found in last reply"
    check = fs_tools.apply_patch(cfg, diff, check_only=True)
    if not check["ok"]:
        return f"check failed (not applied):\n{check['message']}"
    res = fs_tools.apply_patch(cfg, diff)
    tag = "ok" if res["ok"] else "failed"
    return f"apply: {tag}\n{res['message']}"


def _render_history(history: list[ChatMessage], n: int = 10) -> str:
    pairs: list[ChatMessage] = [m for m in history if m.role != "system"]
    take = pairs[-n:]
    if not take:
        return "(no history yet)"
    out: list[str] = []
    for m in take:
        prefix = "you" if m.role == "user" else "qwen"
        body = m.content if len(m.content) <= 400 else m.content[:400] + "..."
        out.append(f"{prefix}> {body}")
    return "\n".join(out)


def _render_diff(cfg: fs_tools.FsConfig, path_a: str, path_b: str) -> str:
    """Return a unified diff between two files inside the repo root."""
    import difflib

    try:
        a = fs_tools.read_file(cfg, path_a)
        b = fs_tools.read_file(cfg, path_b)
    except fs_tools.FsError as exc:
        return f"diff error: {exc}"
    a_lines = str(a["text"]).splitlines(keepends=True)
    b_lines = str(b["text"]).splitlines(keepends=True)
    out = list(
        difflib.unified_diff(
            a_lines, b_lines, fromfile=path_a, tofile=path_b, n=3
        )
    )
    if not out:
        return f"(files identical: {path_a} == {path_b})"
    return "".join(out)


def _render_diff_head(cfg: fs_tools.FsConfig, path: str) -> str:
    """Return a unified diff of `path` against the git HEAD version.

    Shells out via `shell_tools.run_shell` so the deny list and cwd
    sandbox apply. Falls back to a friendly message if the path is not
    tracked or HEAD does not exist (fresh repo with no commits).
    """
    try:
        # Validate path is inside root before running git.
        fs_tools._resolve_inside_root(cfg, path)
    except fs_tools.FsError as exc:
        return f"diff error: {exc}"
    try:
        res = shell_tools.run_shell(
            cfg, f"git --no-pager diff HEAD -- {path}"
        )
    except shell_tools.ShellError as exc:
        return f"diff error: {exc}"
    body = (res.stdout or "").rstrip()
    if not body and res.returncode == 0:
        return f"(no changes vs HEAD: {path})"
    if res.returncode != 0:
        err = (res.stderr or "").strip() or "git diff failed"
        return f"diff error: {err}"
    return body


def _audit_run_path(cfg: fs_tools.FsConfig) -> Path:
    """Return the audit-log path for /run attempts (loop 251)."""
    return Path(cfg.root) / ".agent" / "runs.log"


def _loop_pid_path(cfg: fs_tools.FsConfig) -> Path:
    """Loop 258: PID file for the autonomous agent loop subprocess."""
    return Path(cfg.root) / ".agent" / "loop.pid"


def _loop_runtime_log_path(cfg: fs_tools.FsConfig) -> Path:
    """Loop 258: runtime log written by ``agent.loop`` (matches LOG_FILE
    in agent/loop.py)."""
    return Path(cfg.root) / ".loop" / "runtime.log"


def _loop_pid_alive(pid: int) -> bool:
    """Loop 258: True if a process with this pid is alive (best-effort).

    On POSIX, ``os.kill(pid, 0)`` raises if the pid is gone or denied.
    Stale PIDs (file points at a dead process) return False so the
    caller can restart cleanly.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _loop_read_pid(cfg: fs_tools.FsConfig) -> int | None:
    """Loop 258: read the PID file, return ``None`` if missing/invalid."""
    p = _loop_pid_path(cfg)
    if not p.exists():
        return None
    try:
        raw = p.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _loop_write_pid(cfg: fs_tools.FsConfig, pid: int) -> None:
    """Loop 258: persist the autonomous-loop pid for later /loop stop."""
    p = _loop_pid_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(int(pid)), encoding="utf-8")


def _loop_clear_pid(cfg: fs_tools.FsConfig) -> None:
    """Loop 258: best-effort PID-file cleanup; ignores missing file."""
    p = _loop_pid_path(cfg)
    try:
        p.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _audit_run_max_bytes() -> int:
    """Loop 257: size cap before rotating ``.agent/runs.log``.

    Default 1 MiB. When the live log exceeds this it's renamed to
    ``runs.log.1`` (overwriting any prior rotation), and a fresh
    ``runs.log`` is started. Set to 0 to disable rotation. Override
    via ``QWEN_RUNS_LOG_MAX_BYTES``.
    """
    raw = os.environ.get("QWEN_RUNS_LOG_MAX_BYTES", str(1024 * 1024))
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return 1024 * 1024
    return max(0, v)


def _maybe_rotate_runs_log(path: Path, cap: int) -> None:
    """Loop 257: best-effort size-based rotation. Renames the current
    log to ``<name>.1`` when its size exceeds ``cap``. Failures are
    swallowed -- audit must never crash the chat session."""
    if cap <= 0:
        return
    try:
        if not path.exists():
            return
        if path.stat().st_size <= cap:
            return
        backup = path.with_name(path.name + ".1")
        try:
            if backup.exists():
                backup.unlink()
        except OSError:
            return
        try:
            path.rename(backup)
        except OSError:
            pass
    except Exception:  # noqa: BLE001
        pass


def _audit_run(
    cfg: fs_tools.FsConfig,
    *,
    cmd: str,
    approved: bool,
    source: str,
    returncode: int | None = None,
) -> None:
    """Append one JSONL record describing a /run attempt.

    Loop 251 added this to give operators a forensic trail of every
    command execution (or denial) that traversed the slash dispatcher.
    Loop 257 added size-based rotation so the log can't grow unbounded
    on long-lived sessions. Best-effort: any IO failure is swallowed
    because audit logging must never break the operator's chat session.
    """
    try:
        path = _audit_run_path(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        _maybe_rotate_runs_log(path, _audit_run_max_bytes())
        record = {
            "ts": time.time(),
            "cmd": cmd,
            "approved": bool(approved),
            "source": source,
        }
        if returncode is not None:
            record["returncode"] = int(returncode)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=True) + "\n")
    except Exception:  # noqa: BLE001 -- audit must never crash a turn
        pass


def _render_loop(cfg: fs_tools.FsConfig, args: list[str]) -> str:
    """Loop 258: manage the autonomous agent loop (`agent/loop.py`)
    from the TUI.

    Subcommands:
      /loop                  -> alias for /loop status
      /loop start            -> spawn `python -m agent.loop` detached;
                                writes pid to .agent/loop.pid
      /loop stop             -> SIGTERM the recorded pid (graceful)
      /loop kill             -> SIGKILL (force) if SIGTERM didn't take
      /loop status           -> running? pid? runtime.log size?
      /loop tail [N]         -> tail N lines of .loop/runtime.log
                                (default 30, max 500)
    """
    sub = args[0].lower() if args else "status"
    if sub == "start":
        existing = _loop_read_pid(cfg)
        if existing is not None and _loop_pid_alive(existing):
            return f"loop already running (pid {existing}); /loop stop first"
        # Stale PID file? Clear it so the subprocess spawn is clean.
        if existing is not None:
            _loop_clear_pid(cfg)
        try:
            import subprocess
            # Detach: setsid on POSIX so SIGINT in the TUI doesn't
            # cascade to the loop. stdout/stderr -> the loop's own
            # runtime.log via the agent.loop logger; we just discard
            # what hits the FDs.
            kwargs: dict = {
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "stdin": subprocess.DEVNULL,
                "cwd": str(cfg.root),
                "close_fds": True,
            }
            if hasattr(os, "setsid"):
                kwargs["start_new_session"] = True
            proc = subprocess.Popen(
                [sys.executable, "-m", "agent.loop"], **kwargs
            )
        except OSError as exc:
            return f"loop start failed: {exc}"
        _loop_write_pid(cfg, proc.pid)
        return (
            f"loop started (pid {proc.pid}); "
            f"tail with /loop tail or watch .loop/runtime.log"
        )
    if sub in ("stop", "kill"):
        pid = _loop_read_pid(cfg)
        if pid is None:
            return "loop not running (no .agent/loop.pid)"
        if not _loop_pid_alive(pid):
            _loop_clear_pid(cfg)
            return f"loop pid {pid} not alive; cleared stale pid file"
        sig = signal.SIGKILL if sub == "kill" else signal.SIGTERM
        try:
            os.kill(pid, sig)
        except OSError as exc:
            return f"loop {sub} failed: {exc}"
        if sub == "kill":
            _loop_clear_pid(cfg)
            return f"loop pid {pid} killed (SIGKILL)"
        return (
            f"loop pid {pid} sent SIGTERM; check /loop status to confirm exit"
        )
    if sub == "status":
        pid = _loop_read_pid(cfg)
        log_path = _loop_runtime_log_path(cfg)
        log_info = (
            f"runtime.log: {log_path.stat().st_size} bytes"
            if log_path.exists()
            else "runtime.log: (not yet created)"
        )
        if pid is None:
            return f"loop: stopped (no pid file)\n{log_info}"
        if _loop_pid_alive(pid):
            return f"loop: running (pid {pid})\n{log_info}"
        return (
            f"loop: stopped (pid {pid} not alive; "
            f"pid file is stale)\n{log_info}"
        )
    if sub == "tail":
        n = 30
        if len(args) > 1:
            try:
                n = max(1, min(500, int(args[1])))
            except ValueError:
                return "usage: /loop tail [N]"
        log_path = _loop_runtime_log_path(cfg)
        if not log_path.exists():
            return f"runtime.log not found at {log_path}"
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"runtime.log read error: {exc}"
        lines = text.splitlines()[-n:]
        if not lines:
            return "(runtime.log is empty)"
        return "\n".join(lines)
    return (
        "usage: /loop [start|stop|kill|status|tail [N]]\n"
        "       /loop with no arg is /loop status"
    )


def _render_runs_audit(cfg: fs_tools.FsConfig, args: list[str]) -> str:
    """Render the tail of the /run audit log (loop 251).

    Usage:
      /runs              -> last 10 records, human-readable
      /runs 25           -> last 25 records
      /runs --json       -> last 10 records as JSONL
      /runs 25 --json    -> last 25 records as JSONL
    """
    want_json = False
    n = 10
    for tok in args:
        if tok == "--json":
            want_json = True
            continue
        if tok.isdigit():
            try:
                n = max(1, min(1000, int(tok)))
            except ValueError:
                pass
    path = _audit_run_path(cfg)
    rotated = path.with_name(path.name + ".1")
    sources: list[Path] = []
    if rotated.exists():
        sources.append(rotated)
    if path.exists():
        sources.append(path)
    if not sources:
        return "(no /run audit records yet)"
    lines: list[str] = []
    for src in sources:
        try:
            lines.extend(src.read_text(encoding="utf-8").splitlines())
        except OSError as exc:
            return f"audit read error: {exc}"
    tail = [ln for ln in lines if ln.strip()][-n:]
    if want_json:
        return "\n".join(tail) if tail else "(no /run audit records yet)"
    if not tail:
        return "(no /run audit records yet)"
    out: list[str] = []
    for ln in tail:
        try:
            rec = json.loads(ln)
        except (json.JSONDecodeError, ValueError):
            out.append(f"  ?? {ln}")
            continue
        ts_iso = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(rec.get("ts", 0))))
        approved = "OK " if rec.get("approved") else "DEN"
        rc = rec.get("returncode")
        rc_str = f" rc={rc}" if rc is not None else ""
        src = rec.get("source", "?")
        out.append(f"  {ts_iso} {approved} [{src}]{rc_str} :: {rec.get('cmd','')}")
    return "\n".join(out)



def _render_run(
    cfg: fs_tools.FsConfig,
    cmd: str,
    *,
    confirm: Callable[[str], bool] | None = None,
    audit_source: str | None = None,
) -> str:
    """Run a shell command inside the workspace sandbox.

    Loop 250 added the optional ``confirm`` gate: when provided, it's
    called with the literal command string and must return True before
    we shell out. A False return yields a friendly "denied" message
    that the operator can see in the chat log. ``confirm=None`` is the
    legacy auto-execute path (kept for backward-compat with anyone
    calling ``_render_run`` directly without a session app).

    Loop 251 added ``audit_source``: when non-None, every approve/deny
    decision and every executed return code is appended as one JSONL
    record to ``<workspace>/.agent/runs.log``. ``None`` skips logging
    so the original test fixtures don't accidentally start producing
    files in tmp_path.
    """
    if confirm is not None:
        try:
            ok = bool(confirm(cmd))
        except Exception as exc:  # noqa: BLE001 -- approval must never crash
            if audit_source is not None:
                _audit_run(cfg, cmd=cmd, approved=False, source=audit_source)
            return f"run denied: confirm hook raised {type(exc).__name__}: {exc}"
        if not ok:
            if audit_source is not None:
                _audit_run(cfg, cmd=cmd, approved=False, source=audit_source)
            return (
                f"run denied (no approval): {cmd}\n"
                "  hint: add --yes to one-shot approve, or /run_on to "
                "auto-approve all /run for this session."
            )
    try:
        res = shell_tools.run_shell(cfg, cmd)
    except shell_tools.ShellError as exc:
        if audit_source is not None:
            _audit_run(cfg, cmd=cmd, approved=True, source=audit_source)
        return f"run error: {exc}"
    if audit_source is not None:
        _audit_run(
            cfg,
            cmd=cmd,
            approved=True,
            source=audit_source,
            returncode=getattr(res, "returncode", None),
        )
    return shell_tools.format_run_result(res)


# Loop 250: parse the literal ``/run`` body into ``(approve_inline, cmd)``
# so the dispatcher and unit tests share one definition of "did the
# operator type --yes / -y". Returns ``(approve, body_without_flag)``.
# The flag must appear at the *start* of the body so a stray ``--yes``
# inside e.g. a sed expression isn't accidentally consumed.

def _parse_run_body(body: str) -> tuple[bool, str]:
    raw = (body or "").lstrip()
    if not raw:
        return False, ""
    head, _, tail = raw.partition(" ")
    if head in {"--yes", "-y"}:
        return True, tail.lstrip()
    return False, raw


# ----------------------------------------------------------- two-phase /run
# Loop 266: stage the command, show a preview, require /yes <stage_id>.
# Pure data + helpers so unit tests can drive them without a TUI App.

RUN_STAGE_TTL_S: int = 600  # 10 minutes is plenty; a stale stage is worthless
RUN_STAGE_MAX: int = 16  # cap pending queue so a runaway agent can't OOM us


@dataclass
class _StagedRun:
    stage_id: str
    cmd: str
    created_at: float


def _new_stage_id(cmd: str, now: float, table: dict[str, "_StagedRun"]) -> str:
    """Return a 6-char hex id derived from cmd + timestamp.

    Collisions are avoided by extending the slice if the prefix is
    already in ``table``. Pure / deterministic given inputs.
    """
    import hashlib

    digest = hashlib.sha256(f"{cmd}|{now:.6f}".encode()).hexdigest()
    for n in (6, 8, 10, 12, 16, 64):
        sid = digest[:n]
        if sid not in table:
            return sid
    return digest  # full hex -- astronomically unlikely


def _stage_run_command(
    table: dict[str, "_StagedRun"],
    cmd: str,
    *,
    now: float | None = None,
    ttl_s: int = RUN_STAGE_TTL_S,
    cap: int = RUN_STAGE_MAX,
) -> tuple[str, str]:
    """Insert ``cmd`` into ``table`` and return ``(stage_id, preview)``.

    Side effect: prunes expired entries first, evicts oldest if the
    queue is at ``cap``. Returns the freshly minted stage_id and a
    plain-text preview block ready to show the operator. The preview
    is intentionally NOT styled with markup -- the caller pipes it
    through ``_safe_log_write`` so any brackets in ``cmd`` (regex
    arguments, JSON args, etc.) don't crash the RichLog.
    """
    if now is None:
        now = time.time()
    # Prune expired stages.
    expired = [sid for sid, st in table.items() if now - st.created_at > ttl_s]
    for sid in expired:
        table.pop(sid, None)
    # Evict oldest if at cap.
    if len(table) >= cap:
        oldest = min(table.values(), key=lambda s: s.created_at)
        table.pop(oldest.stage_id, None)
    sid = _new_stage_id(cmd, now, table)
    table[sid] = _StagedRun(stage_id=sid, cmd=cmd, created_at=now)
    preview = _format_run_preview(sid, cmd, ttl_s)
    return sid, preview


def _format_run_preview(stage_id: str, cmd: str, ttl_s: int) -> str:
    """Render the operator-facing preview block for a staged /run.

    Contains: stage_id, the literal command, the TTL window, and the
    confirm/cancel hint. No Rich markup tags so the dynamic ``cmd``
    can't accidentally close a styled tag mid-string.
    """
    lines = [
        f"staged /run [stage={stage_id}]  (expires in {ttl_s}s)",
        f"  cmd: {cmd}",
        f"  to execute: /yes {stage_id}",
        f"  to cancel:  /no  {stage_id}",
        "  hint: /run --yes <cmd> bypasses staging; /run_on auto-approves all.",
    ]
    return "\n".join(lines)


def _consume_stage(
    table: dict[str, "_StagedRun"],
    stage_id: str | None,
    *,
    now: float | None = None,
    ttl_s: int = RUN_STAGE_TTL_S,
) -> tuple[str, str | None]:
    """Pop a staged run by id (or most-recent if id is None/empty).

    Returns ``(status, cmd_or_none)`` where status is one of
    ``"ok" | "missing" | "expired" | "empty"``. ``"ok"`` means the
    caller should now execute ``cmd``. The entry is removed from
    ``table`` only on ``"ok"`` and ``"expired"`` (so a missing id
    doesn't accidentally clobber unrelated stages).
    """
    if now is None:
        now = time.time()
    if not table:
        return "empty", None
    if not stage_id:
        # Most-recent stage.
        latest = max(table.values(), key=lambda s: s.created_at)
        stage_id = latest.stage_id
    st = table.get(stage_id)
    if st is None:
        return "missing", None
    if now - st.created_at > ttl_s:
        table.pop(stage_id, None)
        return "expired", None
    table.pop(stage_id, None)
    return "ok", st.cmd


def _cancel_stage(
    table: dict[str, "_StagedRun"],
    stage_id: str | None,
) -> tuple[str, str | None]:
    """Remove a staged run from the queue without executing it."""
    if not table:
        return "empty", None
    if not stage_id:
        latest = max(table.values(), key=lambda s: s.created_at)
        stage_id = latest.stage_id
    st = table.pop(stage_id, None)
    if st is None:
        return "missing", None
    return "ok", st.cmd


def _render_open(cfg: fs_tools.FsConfig, path: str) -> str:
    """Resolve a path inside the sandbox and launch ``$EDITOR`` on it.

    Returns a status string. The path is resolved through fs_tools so a
    relative dot-dot escape never reaches the editor. The editor command
    is split on whitespace so callers can set ``EDITOR='code -w'`` etc.
    Subprocess invocation is shell-free to avoid command injection from
    a chat-supplied path. A missing ``$EDITOR`` falls back to ``vi``
    which mirrors POSIX convention.
    """
    import shlex
    import subprocess

    try:
        resolved = fs_tools._resolve_inside_root(cfg, path)
    except fs_tools.FsError as exc:
        return f"open error: {exc}"
    editor_cmd = os.environ.get("EDITOR") or "vi"
    parts = shlex.split(editor_cmd) + [str(resolved)]
    try:
        proc = subprocess.run(parts, check=False)
    except FileNotFoundError:
        return f"open error: editor not found: {parts[0]}"
    except OSError as exc:
        return f"open error: {exc}"
    if proc.returncode == 0:
        return f"(opened {path} in {parts[0]})"
    return f"(editor {parts[0]} exited with {proc.returncode})"


_CD_SENTINEL = "__CD__"
_AGENT_SENTINEL = "__AGENT__"
_AGENT_WRITE_SENTINEL = "__AGENTW__"
_AGENT_TOGGLE_SENTINEL = "__AGENT_TOGGLE__"


def _decode_agent_body(body: str) -> tuple[str, int | None, bool]:
    """Pull leading flag lines (``--max=N`` and/or ``--resume``) off an
    agent sentinel body.

    Returns ``(task, max_steps, resume)``. Flag lines may appear in any
    order at the head of the body; parsing stops as soon as a non-flag
    line is encountered. ``max_steps`` is ``None`` when no ``--max`` was
    supplied; ``resume`` is ``True`` when ``--resume`` was supplied.
    Mirrors the encoder in ``dispatch_slash``.
    """
    max_steps: int | None = None
    resume = False
    while True:
        head, sep, rest = body.partition("\n")
        if head.startswith("--max="):
            try:
                max_steps = int(head[len("--max="):])
            except ValueError:
                # Unparseable --max — treat the rest as task body so
                # users see *something* run rather than silently lose
                # their request.
                return body, None, resume
            body = rest if sep else ""
            continue
        if head == "--resume":
            resume = True
            body = rest if sep else ""
            continue
        break
    return body, max_steps, resume


def _render_cd(cfg: fs_tools.FsConfig, path: str) -> str:
    """Validate ``path`` resolves to an existing directory and return a
    sentinel string the App layer recognises to swap its FsConfig root.

    Both absolute paths and paths relative to the current sandbox root are
    accepted. Symlinks are followed and the final destination must exist
    and be a directory. The dispatcher returns the sentinel as ``text`` so
    the App's ``on_input_submitted`` can intercept and re-bind ``self.fs_cfg``
    to a new FsConfig with the same byte / entry limits but a new root.
    Tests that call ``dispatch_slash`` directly can assert on the sentinel
    prefix without needing a live App.
    """
    raw = Path(path).expanduser()
    if raw.is_absolute():
        target = raw.resolve()
    else:
        target = (cfg.root / raw).resolve()
    if not target.exists():
        return f"cd error: no such directory: {path}"
    if not target.is_dir():
        return f"cd error: not a directory: {path}"
    return f"{_CD_SENTINEL}{target}"


def _render_memory(client: QwenClient, args: list[str]) -> str:
    """Loop 245: operator-facing surface for the loop-244 TaskMemory.

    Subcommands:
      (no args) | show       Pretty-print current task / todos / facts / decisions.
      --json                  Same, JSON form for piping.
      task <text>             Set the current task description.
      todo add <id> <desc>    Add an open todo. Status defaults to "open".
      todo done <id>          Mark todo as done.
      todo block <id>         Mark todo as blocked.
      todo del <id>           Remove a todo by id.
      fact <key> <value>      Record a key→value fact.
      decision <text>         Append a decision entry (FIFO-capped).
      clear                   Wipe the entire memory (including persisted file).

    Memory must be enabled (``QWEN_TASK_MEMORY=1``) before calls to
    ``client.task_memory`` exist; otherwise we return a hint.
    """
    tm = getattr(client, "task_memory", None)
    if tm is None:
        return (
            "task memory disabled. enable with QWEN_TASK_MEMORY=1 "
            "(see README env table for related knobs)."
        )

    if not args or args[0] in {"show"}:
        snap = tm.snapshot()
        if not (
            snap.get("current_task")
            or snap.get("todos")
            or snap.get("facts")
            or snap.get("decisions")
        ):
            return "task memory: empty"
        lines = ["task memory:"]
        ct = snap.get("current_task") or ""
        if ct:
            lines.append(f"  current task: {ct}")
        todos = snap.get("todos") or []
        if todos:
            lines.append(f"  todos ({len(todos)}):")
            for t in todos:
                lines.append(
                    f"    - [{t.get('status', '?')}] "
                    f"{t.get('id', '?')}: {t.get('description', '')}"
                )
        facts = snap.get("facts") or {}
        if facts:
            lines.append(f"  facts ({len(facts)}):")
            for k, v in facts.items():
                lines.append(f"    - {k}: {v}")
        decisions = snap.get("decisions") or []
        if decisions:
            lines.append(f"  decisions ({len(decisions)}):")
            for d in decisions:
                lines.append(f"    - {d}")
        return "\n".join(lines)

    if args[0] in {"--json", "--format=json"}:
        return json.dumps(tm.snapshot(), indent=2, sort_keys=True)

    sub = args[0]
    rest = args[1:]

    if sub == "task":
        if not rest:
            return "usage: /memory task <description>"
        desc = " ".join(rest).strip()
        if not desc:
            return "usage: /memory task <description>"
        tm.set_current_task(desc)
        return f"task memory: current_task set to: {desc}"

    if sub == "todo":
        if not rest:
            return "usage: /memory todo add <id> <desc> | done <id> | block <id> | del <id>"
        action = rest[0]
        rest2 = rest[1:]
        if action == "add":
            if len(rest2) < 2:
                return "usage: /memory todo add <id> <desc>"
            todo_id = rest2[0]
            todo_desc = " ".join(rest2[1:]).strip()
            if not todo_desc:
                return "usage: /memory todo add <id> <desc>"
            tm.add_todo(todo_id, todo_desc)
            return f"task memory: todo added: {todo_id}"
        if action in {"done", "block"}:
            if not rest2:
                return f"usage: /memory todo {action} <id>"
            status = "done" if action == "done" else "blocked"
            ok = tm.update_todo_status(rest2[0], status)
            if not ok:
                return f"task memory: no such todo: {rest2[0]}"
            return f"task memory: todo {rest2[0]} → {status}"
        if action == "del":
            if not rest2:
                return "usage: /memory todo del <id>"
            ok = tm.remove_todo(rest2[0])
            if not ok:
                return f"task memory: no such todo: {rest2[0]}"
            return f"task memory: todo deleted: {rest2[0]}"
        return f"unknown /memory todo action: {action}"

    if sub == "fact":
        if len(rest) < 2:
            return "usage: /memory fact <key> <value>"
        key = rest[0]
        value = " ".join(rest[1:]).strip()
        if not value:
            return "usage: /memory fact <key> <value>"
        tm.record_fact(key, value)
        return f"task memory: fact recorded: {key}={value}"

    if sub == "decision":
        if not rest:
            return "usage: /memory decision <text>"
        text = " ".join(rest).strip()
        if not text:
            return "usage: /memory decision <text>"
        tm.record_decision(text)
        return f"task memory: decision recorded: {text}"

    if sub == "clear":
        tm.clear()
        return "task memory: cleared"

    return f"unknown /memory subcommand: {sub}"


def _render_sysinfo_json(
    client: QwenClient,
    cfg: fs_tools.FsConfig,
    history: list[ChatMessage] | None,
    *,
    probe: bool = False,
) -> str:
    """JSON counterpart to ``_render_sysinfo`` for downstream tooling.

    Same data; structured shape. Health failures surface as
    ``{"ok": false, "error": ..., "hint": ...}`` rather than a free-form
    string. Tokens and message counts are integers. When ``probe`` is
    True, an additional ``engine_health`` field carries the result of
    vLLM's ``/health`` endpoint — the active readiness signal that
    distinguishes "args accepted" (loops 205/211 fixed this) from
    "engine actually ready to serve a chat request".
    """
    import json

    settings = getattr(client, "settings", None)
    model = getattr(settings, "model", None) or None
    base_url = getattr(settings, "base_url", None) or None
    try:
        check = client.health_check()
    except Exception as exc:  # noqa: BLE001
        check = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    msgs = len(history) if history is not None else 0
    tokens = (
        sum(estimate_tokens(m.content) for m in history)
        if history is not None
        else 0
    )
    payload = {
        "model": model,
        "base_url": base_url,
        "fs_root": str(cfg.root),
        "history": {"messages": msgs, "tokens_estimated": tokens},
        "health": check,
    }
    # Loop 242: surface most recent client-side compression event so
    # operators can see when (and how aggressively) the client is
    # dropping history to fit the server's context cap.
    last_comp = getattr(client, "_last_compression", None)
    if last_comp is not None:
        payload["last_compression"] = dict(last_comp)
    if probe:
        try:
            payload["engine_health"] = client.vllm_health_probe()
        except Exception as exc:  # noqa: BLE001
            payload["engine_health"] = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
    # Loop 247: surface the persistent task-memory snapshot so operators
    # can see at-a-glance what the model has been told about the current
    # task, open todos, facts, and recent decisions. Omitted when memory
    # is disabled (None) or empty (so /sysinfo --json stays compact).
    tm = getattr(client, "task_memory", None)
    if tm is not None:
        try:
            snap = tm.snapshot()
            if (
                snap.get("current_task")
                or snap.get("todos")
                or snap.get("facts")
                or snap.get("decisions")
            ):
                payload["task_memory"] = {
                    "current_task": snap.get("current_task") or "",
                    "todos": list(snap.get("todos") or []),
                    "facts": dict(snap.get("facts") or {}),
                    "decisions": list(snap.get("decisions") or []),
                }
        except Exception:  # noqa: BLE001
            pass
    return json.dumps(payload, indent=2)


def _render_sysinfo(    client: QwenClient,
    cfg: fs_tools.FsConfig,
    history: list[ChatMessage] | None,
    *,
    probe: bool = False,
) -> str:
    """Return a one-shot snapshot of backend health, model, root, and
    history token estimate. Designed for users to copy into a bug report.

    When ``probe`` is True, also probe vLLM's ``/health`` and surface
    "engine ready" / "engine warming up" as a separate line.
    """
    settings = getattr(client, "settings", None)
    model = getattr(settings, "model", None) or "(unknown)"
    base_url = getattr(settings, "base_url", None) or "(unknown)"
    try:
        check = client.health_check()
    except Exception as exc:  # noqa: BLE001
        check = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if check.get("ok"):
        models = ", ".join((check.get("models") or [])[:3]) or "(none)"
        health_line = f"backend ok  models: {models}"
    else:
        err = check.get("error") or "unknown"
        hint = check.get("hint")
        health_line = f"backend unavailable: {err}"
        if hint:
            health_line = f"{health_line}\n  hint:     {hint}"
    msgs = len(history) if history is not None else 0
    tokens = (
        sum(estimate_tokens(m.content) for m in history)
        if history is not None
        else 0
    )
    lines = [
        "qwen-coder-tui sysinfo",
        f"  model:    {model}",
        f"  base_url: {base_url}",
        f"  fs_root:  {cfg.root}",
        f"  history:  {msgs} messages, ~{tokens} tokens",
        f"  health:   {health_line}",
    ]
    # Loop 242: when the client most recently dropped history to fit
    # the server cap, surface the stat so operators don't get silently
    # truncated context.
    last_comp = getattr(client, "_last_compression", None)
    if last_comp:
        dropped = last_comp.get("dropped", 0)
        kept = last_comp.get("kept", 0)
        prompt_t = last_comp.get("prompt_tokens", 0)
        max_t = last_comp.get("max_tokens", 0)
        cap = last_comp.get("cap", 0)
        if dropped > 0:
            comp_line = (
                f"dropped {dropped} oldest msg(s); kept {kept}; "
                f"prompt~{prompt_t} + completion={max_t} of cap {cap}"
            )
        else:
            comp_line = (
                f"no drops; kept {kept}; prompt~{prompt_t} + "
                f"completion={max_t} of cap {cap}"
            )
        lines.append(f"  last_chat:{comp_line}")
    if probe:
        try:
            engine = client.vllm_health_probe()
        except Exception as exc:  # noqa: BLE001
            engine = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if engine.get("ok"):
            engine_line = "engine ready (vLLM /health 200)"
        else:
            err = engine.get("error") or "unknown"
            engine_line = f"engine not ready: {err}"
            hint = engine.get("hint")
            if hint:
                engine_line = f"{engine_line}\n  hint:     {hint}"
        lines.append(f"  engine:   {engine_line}")
    # Loop 247: surface the persistent task-memory snapshot. Compact one-
    # liner: current task + todo counts. Operators can run /memory show
    # for the full breakdown; this is just the breadcrumb in /sysinfo.
    tm = getattr(client, "task_memory", None)
    if tm is not None:
        try:
            snap = tm.snapshot()
            ct = snap.get("current_task") or ""
            todos = snap.get("todos") or []
            facts = snap.get("facts") or {}
            decisions = snap.get("decisions") or []
            if ct or todos or facts or decisions:
                open_n = sum(1 for t in todos if t.get("status") == "open")
                ip_n = sum(1 for t in todos if t.get("status") == "in_progress")
                done_n = sum(1 for t in todos if t.get("status") == "done")
                blocked_n = sum(1 for t in todos if t.get("status") == "blocked")
                task_line = f"task='{ct[:60]}'" if ct else "task=(none)"
                todo_line = (
                    f"todos: {open_n} open / {ip_n} in_progress / "
                    f"{done_n} done / {blocked_n} blocked"
                )
                lines.append(f"  memory:   {task_line}")
                lines.append(f"            {todo_line}")
                if facts:
                    lines.append(f"            facts: {len(facts)}")
                if decisions:
                    lines.append(f"            decisions: {len(decisions)}")
        except Exception:  # noqa: BLE001
            pass
    return "\n".join(lines)


def _split_grep_flags(args: list[str]) -> tuple[list[str], str | None, bool]:
    """Strip leading-dash flags from ``args`` and return positionals,
    optional language suffix, and a ``count_only`` flag.

    Recognised flags: ``--<lang>`` for ext filter; ``--count`` / ``-c``
    to render a per-file hit count summary instead of every match.
    Unrecognised long flags are dropped so a typo like ``--pyhton``
    does not silently match every line as a pattern.
    """
    positionals: list[str] = []
    suffix: str | None = None
    count_only = False
    for arg in args:
        if arg in ("--count", "-c"):
            count_only = True
        elif arg.startswith("--") and len(arg) > 2:
            suffix = arg[2:]
        elif arg.startswith("-") and len(arg) > 1 and not arg[1].isdigit():
            continue
        else:
            positionals.append(arg)
    return positionals, suffix, count_only


def _render_grep(
    cfg: fs_tools.FsConfig,
    pattern: str,
    path: str = ".",
    suffix: str | None = None,
    *,
    count_only: bool = False,
) -> str:
    """Recursive grep with optional file-suffix filter.

    ``suffix`` is a bare extension like ``"py"`` or ``"md"`` (no dot).
    When supplied, hits on files whose path does not end with
    ``"." + suffix`` are dropped after the search runs. The filter is
    applied here rather than in shell_tools.grep so the public grep API
    stays minimal -- the TUI is the only caller that needs language
    filtering today.

    When ``count_only`` is true, return a per-file count summary like
    ``src/foo.py: 7`` lines, sorted descending, plus a total tail.
    """
    try:
        hits = shell_tools.grep(cfg, pattern, path=path)
    except shell_tools.ShellError as exc:
        return f"grep error: {exc}"
    except fs_tools.FsError as exc:
        return f"grep error: {exc}"
    if suffix:
        suffix_dot = "." + suffix.lstrip(".")
        hits = [h for h in hits if h.path.endswith(suffix_dot)]
    if count_only:
        counts: dict[str, int] = {}
        for h in hits:
            counts[h.path] = counts.get(h.path, 0) + 1
        if not counts:
            return "(no matches)"
        ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        lines = [f"{p}: {n}" for p, n in ordered]
        lines.append(f"-- {sum(counts.values())} matches across {len(counts)} files")
        return "\n".join(lines)
    return shell_tools.format_grep(hits)


def _render_find(
    cfg: fs_tools.FsConfig, glob: str, path: str = "."
) -> str:
    try:
        hits = shell_tools.find(cfg, glob, path=path)
    except shell_tools.ShellError as exc:
        return f"find error: {exc}"
    except fs_tools.FsError as exc:
        return f"find error: {exc}"
    return shell_tools.format_find(hits)


def _render_save(
    cfg: fs_tools.FsConfig, history: list[ChatMessage], path: str
) -> str:
    """Persist the chat transcript to a file in the repo sandbox."""
    pairs = [m for m in history if m.role != "system"]
    if not pairs:
        return "no chat to save"
    body_parts: list[str] = []
    for m in pairs:
        prefix = "you" if m.role == "user" else "qwen"
        body_parts.append(f"{prefix}>\n{m.content}\n")
    body = "\n".join(body_parts)
    try:
        fs_tools.write_file(cfg, path, body, create_parents=True)
    except fs_tools.FsError as exc:
        return f"save error: {exc}"
    return f"saved {len(pairs)} turns to {path}"


def _render_export(
    cfg: fs_tools.FsConfig, history: list[ChatMessage], path: str
) -> str:
    """Export the full chat transcript as Markdown.

    Unlike `_render_save` which uses a flat `you>` / `qwen>` log shape
    suitable for re-reading, this helper produces proper Markdown with
    `## you` / `## qwen` headings and triple-backtick fenced bodies so
    the file renders as a readable transcript in any markdown viewer.
    The system prompt is included as a leading `> system: ...` blockquote.
    """
    if not history:
        return "no chat to export"
    lines: list[str] = ["# qwen-coder-tui chat transcript", ""]
    for m in history:
        if m.role == "system":
            for sl in m.content.splitlines() or [""]:
                lines.append(f"> system: {sl}")
            lines.append("")
            continue
        heading = "you" if m.role == "user" else "qwen"
        lines.append(f"## {heading}")
        lines.append("")
        lines.append(m.content)
        lines.append("")
    body = "\n".join(lines)
    try:
        fs_tools.write_file(cfg, path, body, create_parents=True)
    except fs_tools.FsError as exc:
        return f"export error: {exc}"
    msgs = sum(1 for m in history if m.role != "system")
    return f"exported {msgs} turns as markdown to {path}"


_PIN_MARKER = "\n\n--- pinned files ---\n"


def _render_pin(
    cfg: fs_tools.FsConfig, history: list[ChatMessage], path: str
) -> str:
    """Append a file's contents to the system prompt under a pinned block.

    The content is appended to (or attached to) the system message at
    history index zero so the model sees the file on every subsequent
    turn without the user having to re-attach it via the @file mention.
    Existing pinned blocks are preserved; files are appended in pin order.
    The file is read through fs_tools so the sandbox applies, and a
    failure leaves the system prompt untouched.
    """
    try:
        info = fs_tools.read_file(cfg, path)
    except fs_tools.FsError as exc:
        return f"pin error: {exc}"
    body = str(info["text"])
    snippet = body[:8000]
    truncated = "\n[truncated]\n" if len(body) > 8000 else ""
    block = f"\n# {path}\n```\n{snippet}{truncated}\n```\n"
    if not history or history[0].role != "system":
        history.insert(0, ChatMessage(role="system", content=prompts.CODER_SYSTEM))
    sys_msg = history[0]
    if _PIN_MARKER in sys_msg.content:
        new_content = sys_msg.content + block
    else:
        new_content = sys_msg.content + _PIN_MARKER + block
    history[0] = ChatMessage(role="system", content=new_content)
    return f"(pinned {path}, {len(snippet)} bytes)"


def _render_unpin(history: list[ChatMessage]) -> str:
    """Strip the pinned-files block from the system prompt."""
    if not history or history[0].role != "system":
        return "(no system prompt)"
    sys_msg = history[0]
    if _PIN_MARKER not in sys_msg.content:
        return "(nothing pinned)"
    head = sys_msg.content.split(_PIN_MARKER, 1)[0]
    history[0] = ChatMessage(role="system", content=head)
    return "(pinned files cleared)"


def _render_pinned(history: list[ChatMessage]) -> str:
    """List file paths currently pinned in the system prompt.

    The pin helper writes each file under a hash space heading so we can
    parse the pinned block back into a path list without storing extra
    state. Returns a friendly status string when nothing is pinned.
    """
    if not history or history[0].role != "system":
        return "(no system prompt)"
    content = history[0].content
    if _PIN_MARKER not in content:
        return "(nothing pinned)"
    block = content.split(_PIN_MARKER, 1)[1]
    paths: list[str] = []
    for line in block.splitlines():
        if line.startswith("# "):
            paths.append(line[2:].strip())
    if not paths:
        return "(nothing pinned)"
    return "pinned files:\n" + "\n".join(f"  - {p}" for p in paths)


_GIT_ALLOWED = {"status", "log", "diff", "show", "branch", "remote", "rev-parse"}


def _render_git(cfg: fs_tools.FsConfig, args: list[str]) -> str:
    """Run a read-only git subcommand inside the repo root.

    Allowed subcommands: status / log / diff / show / branch / remote /
    rev-parse. Anything else is rejected so /git cannot mutate the
    working tree from the chat box. Trailing arguments are passed
    through after the same deny list scan run_shell applies, so a
    /git log followed by an embedded shell metachar is still bounded.
    """
    if not args:
        return "usage: /git <status|log|diff|show|branch|remote|rev-parse> [args]"
    sub = args[0]
    if sub not in _GIT_ALLOWED:
        return (
            f"git error: subcommand '{sub}' not allowed. "
            f"allowed: {sorted(_GIT_ALLOWED)}"
        )
    # Bound log/diff to a small page by default so they fit in the TUI.
    extra = list(args[1:])
    if sub == "log" and not any(a.startswith("-n") or a == "--max-count" for a in extra):
        extra = ["-n", "20", "--oneline"] + extra
    cmd = "git --no-pager " + " ".join([sub] + extra)
    try:
        res = shell_tools.run_shell(cfg, cmd, timeout=15.0)
    except shell_tools.ShellError as exc:
        return f"git error: {exc}"
    return shell_tools.format_run_result(res)


def _render_tests(cfg: fs_tools.FsConfig, args: list[str]) -> str:
    """Run pytest in the repo. Optional args are appended verbatim."""
    extra = " ".join(args) if args else "-q"
    try:
        res = shell_tools.run_shell(
            cfg, f"python -m pytest {extra}", timeout=120.0
        )
    except shell_tools.ShellError as exc:
        return f"tests error: {exc}"
    return shell_tools.format_run_result(res)


# ----------------------------------------------------------- @file expansion
_AT_FILE_RE = __import__("re").compile(r"(?<!@)@([\w./\-]+)")
_AT_FULL_FILE_RE = __import__("re").compile(r"@@([\w./\-]+)")
_AT_WEB_RE = __import__("re").compile(r"@web:(\S+)")
_AT_SEARCH_RE = __import__("re").compile(r"@search:([^\s][^\n]*?)(?=\s+@|\s*$)")


def expand_at_mentions(
    cfg: fs_tools.FsConfig,
    text: str,
    *,
    max_files: int = 5,
    max_bytes_each: int = 8000,
    max_web: int = 2,
    web_byte_cap: int = 8000,
    web_search_fn: object | None = None,
    web_fetch_fn: object | None = None,
) -> str:
    """Replace `@path`, `@web:<url>`, and `@search:<query>` mentions
    with inline content.

    `@<path>`           inlines a workspace file (sandboxed)
    `@web:<url>`        fetches a URL and inlines its body
    `@search:<query>`   runs a web search and inlines top results

    Web fetches use `web_tools.fetch_url` / `web_tools.web_search` by
    default; tests can inject stubs via `web_search_fn` / `web_fetch_fn`.
    Failures are silent so a typo or network blip never blocks the
    user's actual prompt.
    """
    if "@" not in text:
        return text
    appended: list[str] = []

    if web_search_fn is None:
        try:
            from . import web_tools as _wt  # type: ignore
            web_search_fn = _wt.web_search
        except Exception:  # noqa: BLE001
            web_search_fn = None
    if web_fetch_fn is None:
        try:
            from . import web_tools as _wt  # type: ignore
            web_fetch_fn = _wt.fetch_url
        except Exception:  # noqa: BLE001
            web_fetch_fn = None

    web_count = 0
    if web_fetch_fn is not None:
        for m in _AT_WEB_RE.finditer(text):
            if web_count >= max_web:
                break
            url = m.group(1).rstrip(".,);]")
            try:
                res = web_fetch_fn(url)  # type: ignore[misc]
            except Exception:  # noqa: BLE001
                continue
            body = str(res.get("text", "") if isinstance(res, dict) else res)
            if len(body) > web_byte_cap:
                body = body[:web_byte_cap] + "\n... [truncated]"
            appended.append(f"\n# @web:{url}\n```\n{body}\n```")
            web_count += 1

    search_count = 0
    if web_search_fn is not None:
        for m in _AT_SEARCH_RE.finditer(text):
            if search_count >= max_web:
                break
            query = m.group(1).strip()
            if not query:
                continue
            try:
                results = web_search_fn(query, max_results=5)  # type: ignore[misc]
            except Exception:  # noqa: BLE001
                continue
            try:
                from . import web_tools as _wt  # type: ignore
                rendered = _wt.format_search_results(results)
            except Exception:  # noqa: BLE001
                rendered = "\n".join(
                    str(getattr(r, "title", r)) for r in (results or [])
                )
            appended.append(f"\n# @search:{query}\n```\n{rendered}\n```")
            search_count += 1

    seen: list[str] = []
    full_seen: list[str] = []
    for m in _AT_FULL_FILE_RE.finditer(text):
        token = m.group(1)
        if token in full_seen:
            continue
        full_seen.append(token)
        if len(full_seen) >= max_files:
            break
    for token in full_seen:
        try:
            res = fs_tools.read_file(cfg, token)
        except fs_tools.FsError:
            continue
        body = str(res.get("text", ""))
        # @@<path> means "the whole file" -- no truncation. We still
        # respect FsConfig.max_read_bytes (enforced inside read_file)
        # so this can't be used to dodge the sandbox.
        appended.append(f"\n# @@{token} (full)\n```\n{body}\n```")
    for m in _AT_FILE_RE.finditer(text):
        token = m.group(1)
        if token.startswith("web:") or token.startswith("search:"):
            continue
        if token in seen or token in full_seen:
            continue
        seen.append(token)
        if len(seen) >= max_files:
            break
    for token in seen:
        try:
            res = fs_tools.read_file(cfg, token)
        except fs_tools.FsError:
            continue
        body = str(res.get("text", ""))
        if len(body) > max_bytes_each:
            body = body[:max_bytes_each] + "\n... [truncated]"
        appended.append(f"\n# {token}\n```\n{body}\n```")
    if not appended:
        return text
    return text + "\n\n--- attached context ---" + "".join(appended)


def dispatch_slash(
    cmd: SlashCommand,
    *,
    client: QwenClient,
    fs_cfg: fs_tools.FsConfig,
    history: list[ChatMessage] | None = None,
    app: Any = None,
) -> tuple[str, bool]:
    """Run a slash command. Returns `(rendered_text, should_quit)`.

    Pure(-ish): does not depend on Textual. Side effects are bounded to
    the injected client and fs_cfg, so tests can inject stubs.
    """
    name = cmd.name
    if name in {"", "help"}:
        if cmd.rest:
            raw = cmd.rest.strip()
            # `--regex <pattern>` or `<pattern> --regex` toggles regex mode.
            use_regex = False
            tokens = raw.split()
            if "--regex" in tokens:
                use_regex = True
                tokens = [t for t in tokens if t != "--regex"]
            term_raw = " ".join(tokens).strip()
            term = term_raw.lower()
            if term:
                if use_regex:
                    import re as _re

                    try:
                        # Substring match by default (no anchoring); use
                        # IGNORECASE so /help foo --regex behaves like the
                        # plain substring path on simple inputs.
                        pattern = _re.compile(term_raw, _re.IGNORECASE)
                    except _re.error as exc:
                        return f"/help: invalid regex {term_raw!r}: {exc}", False

                    def _matches(parts: list[str]) -> bool:
                        return any(pattern.search(p) for p in parts)
                else:

                    def _matches(parts: list[str]) -> bool:
                        return any(term in p.lower() for p in parts)

                lines = HELP_TEXT.splitlines()
                header_idx = next(
                    (i for i, ln in enumerate(lines) if ln.startswith("Slash commands:")),
                    -1,
                )
                if header_idx == -1:
                    return HELP_TEXT, False
                kept: list[str] = []
                # The help table is a sequence of entries: "  /cmd ..."
                # optionally followed by one or more indented continuation
                # lines. We group an entry with all of its continuations.
                i = header_idx + 1
                while i < len(lines):
                    ln = lines[i]
                    is_entry = ln.startswith("  /")
                    if not is_entry:
                        i += 1
                        continue
                    block = [ln]
                    j = i + 1
                    while j < len(lines):
                        cont = lines[j]
                        if (
                            cont.startswith(" ")
                            and not cont.startswith("  /")
                            and cont.strip() != ""
                        ):
                            block.append(cont)
                            j += 1
                        else:
                            break
                    i = j
                    if _matches(block):
                        kept.extend(block)
                if not kept:
                    label = term_raw if use_regex else term
                    return f"/help: no commands match {label!r}", False
                label = f"regex {term_raw!r}" if use_regex else repr(term)
                return (
                    f"Slash commands matching {label}:\n" + "\n".join(kept)
                ), False
        return HELP_TEXT, False
    if name == "quit" or name == "exit":
        return "bye", True
    if name == "tools":
        # Two registries: the always-on read tools and the opt-in writes.
        read_names = sorted(agent_loop.DEFAULT_TOOLS.keys())
        write_names = sorted(agent_loop.WRITE_TOOLS.keys())
        lines = [
            "[bold]read-only tools[/bold] (always available in agent mode):",
            "  " + ", ".join(read_names),
            "[bold]write/exec tools[/bold] (active when write=on, which is the default):",
            "  " + ", ".join(write_names),
            "Use /agent_write_off to disable writes, /agent_write_on to re-enable.",
            f"destructive (Copilot-style confirm modal): {', '.join(sorted(agent_loop.DESTRUCTIVE_TOOLS))}",
        ]
        return "\n".join(lines), False
    if name == "agent":
        if not cmd.rest:
            return "usage: /agent [--write] [--max N] [--resume] <task>", False
        rest = cmd.rest
        write_mode = False
        max_steps: int | None = None
        resume = False
        # Parse leading flags. Order doesn't matter; loop until we hit
        # something that isn't a flag.
        while True:
            toks = rest.split(None, 1)
            if not toks:
                break
            head = toks[0]
            if head in ("--write", "-w"):
                write_mode = True
                rest = toks[1] if len(toks) > 1 else ""
            elif head == "--resume":
                resume = True
                rest = toks[1] if len(toks) > 1 else ""
            elif head == "--max" or head.startswith("--max="):
                if head.startswith("--max="):
                    val = head[len("--max="):]
                    rest = toks[1] if len(toks) > 1 else ""
                else:
                    if len(toks) < 2:
                        return "usage: /agent --max N <task>", False
                    val_toks = toks[1].split(None, 1)
                    val = val_toks[0]
                    rest = val_toks[1] if len(val_toks) > 1 else ""
                try:
                    parsed = int(val)
                except ValueError:
                    return f"--max expects an integer, got {val!r}", False
                if parsed < 1 or parsed > 50:
                    return "--max must be between 1 and 50", False
                max_steps = parsed
            else:
                break
        if not rest.strip():
            return "usage: /agent [--write] [--max N] [--resume] <task>", False
        prefix = _AGENT_WRITE_SENTINEL if write_mode else _AGENT_SENTINEL
        # Encode flags as leading lines so _decode_agent_body can pull
        # them off in any order. Keep --max= first for back-compat with
        # tests that pin the wire format.
        body_lines: list[str] = []
        if max_steps is not None:
            body_lines.append(f"--max={max_steps}")
        if resume:
            body_lines.append("--resume")
        body_lines.append(rest)
        body = "\n".join(body_lines)
        return prefix + body, False
    if name == "agent_on":
        return _AGENT_TOGGLE_SENTINEL + "on", False
    if name == "agent_off":
        return _AGENT_TOGGLE_SENTINEL + "off", False
    if name == "agent_write_on":
        return _AGENT_TOGGLE_SENTINEL + "write_on", False
    if name == "agent_write_off":
        return _AGENT_TOGGLE_SENTINEL + "write_off", False
    if name == "confirm_writes_on":
        return _AGENT_TOGGLE_SENTINEL + "confirm_on", False
    if name == "confirm_writes_off":
        return _AGENT_TOGGLE_SENTINEL + "confirm_off", False
    if name == "allow_all":
        return _AGENT_TOGGLE_SENTINEL + "allow_all", False
    if name == "safe_mode":
        return _AGENT_TOGGLE_SENTINEL + "safe_mode", False
    if name == "mouse":
        # Loop 280: toggle Textual's mouse capture so the host
        # terminal can do native click-drag selection on the RichLog.
        # /mouse off  -- release capture (terminal-native select+copy)
        # /mouse on   -- restore Textual's mouse handling
        # /mouse      -- toggle
        sub = (cmd.rest or "").strip().lower()
        if sub in ("", "toggle"):
            return _AGENT_TOGGLE_SENTINEL + "mouse_toggle", False
        if sub in ("off", "0", "false", "release"):
            return _AGENT_TOGGLE_SENTINEL + "mouse_off", False
        if sub in ("on", "1", "true", "capture"):
            return _AGENT_TOGGLE_SENTINEL + "mouse_on", False
        return f"usage: /mouse [on|off|toggle]", False
    if name == "select":
        # Loop 280: alias -- "/select" is the obvious verb for "let me
        # select text". Same as /mouse off.
        return _AGENT_TOGGLE_SENTINEL + "mouse_off", False
    if name == "loop":
        return _render_loop(fs_cfg, list(cmd.args)), False
    if name == "search":
        if not cmd.rest:
            return "usage: /search [--max N] <query>", False
        # Accept a leading --max <n> / --max=<n> flag.
        rest = cmd.rest
        max_results = 5
        toks = rest.split()
        if toks and toks[0].startswith("--max"):
            head = toks[0]
            if "=" in head:
                _, _, val = head.partition("=")
                rest = " ".join(toks[1:])
            elif len(toks) >= 2:
                val = toks[1]
                rest = " ".join(toks[2:])
            else:
                return "usage: /search --max <n> <query>", False
            try:
                max_results = max(1, min(20, int(val)))
            except ValueError:
                return f"/search: --max needs an integer, got {val!r}", False
            if not rest.strip():
                return "usage: /search --max <n> <query>", False
        return _render_search(rest, max_results=max_results), False
    if name == "fetch":
        if not cmd.args:
            return "usage: /fetch <url>", False
        return _render_fetch(cmd.args[0]), False
    if name == "read":
        if not cmd.args:
            return "usage: /read <path>", False
        return _render_read(fs_cfg, cmd.args[0]), False
    if name == "view":
        return _render_view(fs_cfg, list(cmd.args)), False
    if name == "ls":
        path = cmd.args[0] if cmd.args else "."
        return _render_ls(fs_cfg, path), False
    if name == "find_bugs":
        if not cmd.args:
            return "usage: /find_bugs <path>", False
        return _render_find_bugs(client, fs_cfg, cmd.args[0]), False
    if name == "explain":
        if not cmd.args:
            return "usage: /explain <path>", False
        return _render_explain(client, fs_cfg, cmd.args[0]), False
    if name == "apply":
        if history is None:
            return "no history available", False
        try:
            return _render_apply(fs_cfg, history), False
        except fs_tools.FsError as exc:
            return f"apply error: {exc}", False
    if name == "history":
        if history is None:
            return "no history available", False
        if cmd.args and cmd.args[0] == "clear":
            kept = [m for m in history if m.role == "system"]
            history.clear()
            history.extend(kept)
            cleared_disk = False
            if fs_cfg is not None:
                path = history_file_path(fs_cfg)
                try:
                    if path.exists():
                        path.unlink()
                        cleared_disk = True
                except OSError:
                    pass
            suffix = " (and deleted persistence file)" if cleared_disk else ""
            return f"(history cleared){suffix}", False
        n = 10
        if cmd.args:
            try:
                n = max(1, int(cmd.args[0]))
            except ValueError:
                return "usage: /history [n|clear]", False
        return _render_history(history, n), False
    if name == "diff":
        if len(cmd.args) == 1:
            return _render_diff_head(fs_cfg, cmd.args[0]), False
        if len(cmd.args) < 2:
            return "usage: /diff <path>  or  /diff <a> <b>", False
        return _render_diff(fs_cfg, cmd.args[0], cmd.args[1]), False
    if name == "run":
        if not cmd.rest:
            return "usage: /run [--yes] <cmd>", False
        approve_inline, body = _parse_run_body(cmd.rest)
        if not body:
            return "usage: /run [--yes] <cmd>", False
        # Session-level auto-approve flag, optional. The TUI App sets
        # ``run_auto_approve`` on itself; tests can pass any object
        # with that attribute, or omit ``app`` entirely (defaults to
        # *deny* so a stray /run from a chat-injected user_text can't
        # silently shell out).
        session_auto = bool(getattr(app, "run_auto_approve", False))
        if approve_inline or session_auto:
            confirm: Callable[[str], bool] | None = lambda _c: True
            return _render_run(fs_cfg, body, confirm=confirm, audit_source="slash"), False
        # Loop 266: two-phase preview. If the app exposes a
        # ``pending_runs`` dict we stage the command and show a
        # preview; operator confirms with ``/yes <stage_id>``. When
        # ``pending_runs`` is missing (legacy callers, stub Apps in
        # older tests), fall back to the loop-250 immediate-deny path
        # so prior contracts hold.
        stage_table = getattr(app, "pending_runs", None)
        if isinstance(stage_table, dict):
            sid, preview = _stage_run_command(stage_table, body)
            return preview, False
        confirm = lambda _c: False
        return _render_run(fs_cfg, body, confirm=confirm, audit_source="slash"), False
    if name == "yes":
        stage_table = getattr(app, "pending_runs", None)
        if not isinstance(stage_table, dict):
            return "no /run staging on this session", False
        sid = cmd.args[0] if cmd.args else None
        status, staged_cmd = _consume_stage(stage_table, sid)
        if status == "empty":
            return "no staged /run to confirm (use /run <cmd> first)", False
        if status == "missing":
            return f"no staged run with id '{sid}'", False
        if status == "expired":
            return f"staged run '{sid}' expired (re-run /run <cmd>)", False
        # status == "ok"
        return _render_run(
            fs_cfg, staged_cmd or "", confirm=lambda _c: True, audit_source="slash"
        ), False
    if name == "no":
        stage_table = getattr(app, "pending_runs", None)
        if not isinstance(stage_table, dict):
            return "no /run staging on this session", False
        sid = cmd.args[0] if cmd.args else None
        status, staged_cmd = _cancel_stage(stage_table, sid)
        if status == "empty":
            return "no staged /run to cancel", False
        if status == "missing":
            return f"no staged run with id '{sid}'", False
        # Audit the explicit cancellation so it shows up in /runs.
        if staged_cmd is not None:
            _audit_run(fs_cfg, cmd=staged_cmd, approved=False, source="slash")
        return f"cancelled staged /run: {staged_cmd}", False
    if name == "run_on":
        if app is not None:
            try:
                app.run_auto_approve = True
            except Exception:  # noqa: BLE001
                pass
        return "/run auto-approve: ON (every /run shells out without prompting)", False
    if name == "run_off":
        if app is not None:
            try:
                app.run_auto_approve = False
            except Exception:  # noqa: BLE001
                pass
        return "/run auto-approve: OFF (each /run requires --yes or /run_on)", False
    if name == "runs":
        return _render_runs_audit(fs_cfg, list(cmd.args)), False
    if name == "grep":
        if not cmd.args:
            return "usage: /grep <pattern> [path] [--ext] [--count]", False
        positionals, suffix, count_only = _split_grep_flags(list(cmd.args))
        if not positionals:
            return "usage: /grep <pattern> [path] [--ext] [--count]", False
        pattern = positionals[0]
        path = positionals[1] if len(positionals) >= 2 else "."
        return (
            _render_grep(
                fs_cfg, pattern, path, suffix=suffix, count_only=count_only
            ),
            False,
        )
    if name == "find":
        if not cmd.args:
            return "usage: /find <glob> [path]", False
        path = cmd.args[1] if len(cmd.args) >= 2 else "."
        return _render_find(fs_cfg, cmd.args[0], path), False
    if name == "clear":
        if history is None:
            return "no history available", False
        history.clear()
        return "(history cleared)", False
    if name == "resume":
        if history is None:
            return "no history available", False
        target = fs_cfg.root / ".agent" / "agent_state.json"
        # --preview / --dry-run: render the diff vs current history
        # without mutating it. Lets users decide before pulling the
        # trigger on a destructive in-place replacement.
        preview = any(a in {"--preview", "--dry-run"} for a in cmd.args)
        if preview:
            loaded, source = agent_loop.load_latest_checkpoint(target)
            if source is None:
                return (
                    f"no checkpoint found at {target} or any rotation "
                    f"under {target.parent / 'checkpoints'}",
                    False,
                )
            return (
                "[dim]· preview only — history unchanged[/dim]\n"
                + format_history_diff(
                    list(history),
                    loaded,
                    snapshot_label=source.name,
                    preview_chars=None,
                ),
                False,
            )
        loaded, source = agent_loop.load_latest_checkpoint(target)
        if not loaded:
            return (
                f"no checkpoint found at {target} or any rotation under "
                f"{target.parent / 'checkpoints'}",
                False,
            )
        # Replace in-place so the caller's reference stays the same.
        history.clear()
        history.extend(loaded)
        roles = ", ".join(
            f"{r}={c}" for r, c in _role_counts(loaded).items()
        )
        last_assistant = next(
            (m.content for m in reversed(loaded) if m.role == "assistant"),
            "",
        )
        snippet = last_assistant[:200].replace("\n", " ")
        src_name = source.name if source is not None else "?"
        return (
            f"resumed {len(loaded)} messages from {src_name} ({roles})\n"
            f"last assistant: {snippet}…"
            if snippet else
            f"resumed {len(loaded)} messages from {src_name} ({roles})"
        ), False
    if name == "checkpoints":
        target = fs_cfg.root / ".agent" / "agent_state.json"
        snaps = agent_loop.list_agent_checkpoints(target)
        if not cmd.args:
            return _format_checkpoint_listing(snaps), False
        sub = cmd.args[0].lower()
        if sub == "load":
            if history is None:
                return "no history available", False
            if len(cmd.args) < 2:
                return "usage: /checkpoints load <N>", False
            try:
                idx = int(cmd.args[1])
            except ValueError:
                return f"invalid index: {cmd.args[1]!r}", False
            if not snaps:
                return "(no rotated checkpoints to load)", False
            if idx < 1 or idx > len(snaps):
                return (
                    f"index {idx} out of range (have {len(snaps)} snapshots)",
                    False,
                )
            chosen = snaps[idx - 1]
            loaded = agent_loop.load_agent_checkpoint(chosen)
            if not loaded:
                return f"checkpoint at {chosen.name} is empty/corrupt", False
            history.clear()
            history.extend(loaded)
            roles = ", ".join(
                f"{r}={c}" for r, c in _role_counts(loaded).items()
            )
            return (
                f"loaded {len(loaded)} messages from {chosen.name} ({roles})",
                False,
            )
        if sub == "diff":
            if history is None:
                return "no history available", False
            # Strip flags wherever they appear in the args.
            rest_args = list(cmd.args[1:])
            inline = False
            since_resume = False
            if "--inline" in rest_args:
                inline = True
                rest_args = [a for a in rest_args if a != "--inline"]
            if "--since-resume" in rest_args:
                since_resume = True
                rest_args = [a for a in rest_args if a != "--since-resume"]
            if since_resume:
                primary = fs_cfg.root / ".agent" / "agent_state.json"
                loaded, source = agent_loop.load_latest_checkpoint(primary)
                if source is None:
                    return "(no checkpoint that /resume could load)", False
                return (
                    format_history_diff(
                        list(history),
                        loaded,
                        snapshot_label=source.name,
                        inline_diff=inline,
                        preview_chars=None,
                    ),
                    False,
                )
            if not rest_args:
                return (
                    "usage: /checkpoints diff <N> [--inline] | "
                    "/checkpoints diff --since-resume [--inline]",
                    False,
                )
            try:
                idx = int(rest_args[0])
            except ValueError:
                return f"invalid index: {rest_args[0]!r}", False
            if not snaps:
                return "(no rotated checkpoints to diff)", False
            if idx < 1 or idx > len(snaps):
                return (
                    f"index {idx} out of range (have {len(snaps)} snapshots)",
                    False,
                )
            chosen = snaps[idx - 1]
            loaded = agent_loop.load_agent_checkpoint(chosen)
            return (
                format_history_diff(
                    list(history),
                    loaded,
                    snapshot_label=chosen.name,
                    inline_diff=inline,
                    preview_chars=None,
                ),
                False,
            )
        if sub == "export":
            # /checkpoints export <N> <path> [--gzip] — copy snapshot N to <path>
            # without mutating history. Lets users archive a snapshot
            # before /checkpoints prune removes it. Loop 269: --gzip flag
            # writes a gzip-compressed copy and auto-suffixes ``.gz`` if
            # the destination doesn't already end in ``.gz``.
            args = list(cmd.args[1:])
            gzip_flag = False
            if "--gzip" in args:
                gzip_flag = True
                args = [a for a in args if a != "--gzip"]
            if len(args) < 2:
                return "usage: /checkpoints export <N> <path> [--gzip]", False
            try:
                idx = int(args[0])
            except ValueError:
                return f"invalid index: {args[0]!r}", False
            if not snaps:
                return "(no rotated checkpoints to export)", False
            if idx < 1 or idx > len(snaps):
                return (
                    f"index {idx} out of range (have {len(snaps)} snapshots)",
                    False,
                )
            chosen = snaps[idx - 1]
            dest_arg = args[1]
            if gzip_flag and not dest_arg.endswith(".gz"):
                dest_arg = dest_arg + ".gz"
            try:
                dest = fs_tools._resolve_inside_root(fs_cfg, dest_arg)
            except fs_tools.FsError as exc:
                return f"export failed: {exc}", False
            try:
                data = chosen.read_bytes()
            except OSError as exc:
                return f"export failed: cannot read {chosen.name}: {exc}", False
            if gzip_flag:
                import gzip as _gzip
                data = _gzip.compress(data)
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_suffix(dest.suffix + ".tmp")
                with open(tmp, "wb") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, dest)
            except OSError as exc:
                try:
                    tmp.unlink()
                except (OSError, NameError, UnboundLocalError):
                    pass
                return f"export failed: {exc}", False
            suffix_note = " gzip" if gzip_flag else ""
            return (
                f"exported{suffix_note} {chosen.name} ({len(data)} bytes) to {dest_arg}",
                False,
            )
        if sub == "prune":
            if len(cmd.args) < 2:
                return "usage: /checkpoints prune <K>", False
            try:
                keep = int(cmd.args[1])
            except ValueError:
                return f"invalid count: {cmd.args[1]!r}", False
            if keep < 0:
                return "keep count must be >= 0", False
            removed = 0
            if keep == 0:
                victims = list(snaps)
            else:
                victims = snaps[:-keep] if len(snaps) > keep else []
            for v in victims:
                try:
                    v.unlink()
                    removed += 1
                except OSError:
                    pass
            return (
                f"pruned {removed} snapshot(s); {len(snaps) - removed} remain",
                False,
            )
        return f"unknown subcommand: {sub!r} (expected load|prune|diff|export)", False
    if name == "save":
        if history is None:
            return "no history available", False
        if not cmd.args:
            return "usage: /save <path>", False
        return _render_save(fs_cfg, history, cmd.args[0]), False
    if name == "git":
        return _render_git(fs_cfg, cmd.args), False
    if name == "tests":
        return _render_tests(fs_cfg, cmd.args), False
    if name == "tokens":
        if history is None:
            return "no history available", False
        as_json = "--json" in cmd.args or "--format=json" in cmd.args
        # Parse optional --top K (or --top=K) and --by-role.
        top_k: int | None = None
        by_role = False
        rest = [a for a in cmd.args if a not in {"--json", "--format=json"}]
        i = 0
        while i < len(rest):
            tok = rest[i]
            if tok == "--top":
                if i + 1 >= len(rest):
                    return "usage: /tokens --json --top K", False
                try:
                    top_k = int(rest[i + 1])
                except ValueError:
                    return f"--top: not an integer: {rest[i + 1]!r}", False
                i += 2
                continue
            if tok.startswith("--top="):
                try:
                    top_k = int(tok.split("=", 1)[1])
                except ValueError:
                    return f"--top: not an integer: {tok!r}", False
                i += 1
                continue
            if tok == "--by-role":
                by_role = True
                i += 1
                continue
            return f"/tokens: unknown argument: {tok!r}", False
        if top_k is not None and top_k < 0:
            return "--top: must be non-negative", False
        if top_k is not None and not as_json:
            return "--top requires --json", False
        if by_role and not as_json:
            return "--by-role requires --json", False
        if by_role and top_k is None:
            return "--by-role requires --top", False
        per_message = [estimate_tokens(m.content) for m in history]
        total = sum(per_message)
        msgs = len(history)
        if as_json:
            import json as _json

            entries = [
                {"index": i, "role": m.role, "tokens_estimated": t}
                for i, (m, t) in enumerate(zip(history, per_message))
            ]
            payload: dict = {
                "messages": msgs,
                "tokens_estimated": total,
                "estimator": "four-chars-per-token",
                "per_message": entries,
            }
            if top_k is not None:
                if by_role:
                    # Bucket entries by role, take top-K per bucket.
                    by: dict[str, list[dict]] = {}
                    for e in entries:
                        by.setdefault(str(e["role"]), []).append(e)
                    top_by_role = {
                        role: sorted(
                            items,
                            key=lambda x: (-x["tokens_estimated"], x["index"]),
                        )[:top_k]
                        for role, items in by.items()
                    }
                    payload["top_by_role"] = top_by_role
                    payload["top_k"] = top_k
                else:
                    # Stable sort: highest tokens first, original index breaks ties.
                    top = sorted(
                        entries, key=lambda e: (-e["tokens_estimated"], e["index"])
                    )[:top_k]
                    payload["top"] = top
                    payload["top_k"] = top_k
            return _json.dumps(payload, indent=2), False
        return (
            f"~{total} tokens across {msgs} messages "
            f"(rough estimate, four characters per token)"
        ), False
    if name == "lat":
        # Strip --format=json / --json and parse --top K wherever they appear.
        rest_args = list(cmd.args)
        as_json = False
        top_k: int | None = None
        new_rest: list[str] = []
        i = 0
        while i < len(rest_args):
            tok = rest_args[i]
            if tok in {"--json", "--format=json"}:
                as_json = True
                i += 1
                continue
            if tok == "--top":
                if i + 1 >= len(rest_args):
                    return "usage: /lat [N] --json --top K", False
                try:
                    top_k = int(rest_args[i + 1])
                except ValueError:
                    return f"--top: not an integer: {rest_args[i + 1]!r}", False
                i += 2
                continue
            if tok.startswith("--top="):
                try:
                    top_k = int(tok.split("=", 1)[1])
                except ValueError:
                    return f"--top: not an integer: {tok!r}", False
                i += 1
                continue
            new_rest.append(tok)
            i += 1
        rest_args = new_rest
        if top_k is not None and top_k < 0:
            return "--top: must be non-negative", False
        if top_k is not None and not as_json:
            return "--top requires --json", False
        # Optional first argument: integer N (turn count) or "reset".
        n = 1
        if rest_args:
            arg0 = rest_args[0]
            if arg0.lower() == "reset":
                cleared = 0
                if app is not None:
                    cleared = len(getattr(app, "turn_profiles", []) or [])
                    try:
                        app.turn_profiles.clear()
                    except AttributeError:
                        # Older stubs without the buffer attribute.
                        try:
                            app.turn_profiles = []  # type: ignore[attr-defined]
                        except Exception:  # noqa: BLE001
                            pass
                    try:
                        app.last_turn_profile = None  # type: ignore[attr-defined]
                    except Exception:  # noqa: BLE001
                        pass
                return (
                    f"/lat: cleared {cleared} turn profile(s)",
                    False,
                )
            try:
                n = int(arg0)
            except ValueError:
                return f"/lat: expected integer, got {arg0!r}", False
            if n < 1:
                return "/lat: count must be >= 1", False
        profiles: list[TurnProfile] = []
        if app is not None:
            profiles = list(getattr(app, "turn_profiles", []) or [])
        if not profiles:
            # Back-compat: if the App only exposes last_turn_profile
            # (e.g. older tests / stubs), fall through to single-turn.
            single = (
                getattr(app, "last_turn_profile", None) if app is not None else None
            )
            if as_json:
                return turn_profiles_as_json([single] if single else []), False
            return format_turn_profile(single), False
        if as_json:
            sliced = profiles[-n:]
            if top_k is not None:
                # Sort desc by total time; stable sort preserves order on ties.
                sliced = sorted(
                    sliced, key=lambda p: p.total_s(), reverse=True
                )[:top_k]
            return turn_profiles_as_json(sliced), False
        return format_turn_profiles(profiles, n=n), False
    if name == "sysprompt":
        if history is None:
            return "no history available", False
        if not cmd.rest:
            cur = history[0].content if history and history[0].role == "system" else "(none)"
            return f"current system prompt:\n{cur}", False
        new_text = cmd.rest
        if history and history[0].role == "system":
            history[0] = ChatMessage(role="system", content=new_text)
        else:
            history.insert(0, ChatMessage(role="system", content=new_text))
        return f"(system prompt set, {len(new_text)} chars)", False
    if name == "model":
        settings = getattr(client, "settings", None)
        cur = getattr(settings, "model", None) or "(unknown)"
        if not cmd.args:
            return f"current model: {cur}", False
        new_model = cmd.args[0]
        if settings is None:
            return "client has no settings; cannot set model", False
        try:
            object.__setattr__(settings, "model", new_model)
        except Exception as exc:  # noqa: BLE001
            return f"could not set model: {type(exc).__name__}: {exc}", False
        return f"(model set: {new_model})", False
    if name == "undo":
        if history is None:
            return "no history available", False
        # Drop trailing assistant if present, then trailing user.
        # System message is preserved. Returns count popped.
        popped = 0
        if history and history[-1].role == "assistant":
            history.pop()
            popped += 1
        if history and history[-1].role == "user":
            history.pop()
            popped += 1
        if popped == 0:
            return "(nothing to undo)", False
        return f"(popped {popped} message(s))", False
    if name == "retry":
        if history is None:
            return "no history available", False
        # Find the most recent user message; strip everything after it
        # (including any assistant reply that followed) so a re-send
        # produces a fresh answer to the same prompt. Returns the
        # restored prompt; the caller is expected to actually re-send.
        last_user_idx: int | None = None
        for i in range(len(history) - 1, -1, -1):
            if history[i].role == "user":
                last_user_idx = i
                break
        if last_user_idx is None:
            return "(no prior user message to retry)", False
        prompt = history[last_user_idx].content
        # Strip the user message and any assistant reply that came after.
        del history[last_user_idx:]
        return f"__RETRY__{prompt}", False
    if name == "sysinfo":
        probe = "--probe" in cmd.args
        if "--json" in cmd.args or "--format=json" in cmd.args:
            return _render_sysinfo_json(client, fs_cfg, history, probe=probe), False
        return _render_sysinfo(client, fs_cfg, history, probe=probe), False
    if name == "memory":
        return _render_memory(client, cmd.args), False
    if name == "export":
        if history is None:
            return "no history available", False
        if not cmd.args:
            return "usage: /export <path>", False
        return _render_export(fs_cfg, history, cmd.args[0]), False
    if name == "pin":
        if history is None:
            return "no history available", False
        if not cmd.args:
            return "usage: /pin <path> [path...]", False
        results = [_render_pin(fs_cfg, history, p) for p in cmd.args]
        return "\n".join(results), False
    if name == "unpin":
        if history is None:
            return "no history available", False
        return _render_unpin(history), False
    if name == "pinned":
        if history is None:
            return "no history available", False
        return _render_pinned(history), False
    if name == "open":
        if not cmd.args:
            return "usage: /open <path>", False
        return _render_open(fs_cfg, cmd.args[0]), False
    if name == "cd":
        if fs_cfg is None:
            return "no fs context available", False
        if not cmd.args:
            return f"(cwd) {fs_cfg.root}", False
        return _render_cd(fs_cfg, cmd.args[0]), False
    return f"unknown command: /{name}  (try /help)", False


def estimate_tokens(text: str) -> int:
    """Cheap byte-pair-ish token estimate: roughly four characters per token.

    This is intentionally crude. The real served model has its own
    tokenizer but pulling it in just for a TUI footer would mean an
    extra heavyweight dep. Four-chars-per-token is the standard
    rule-of-thumb for English source code and prose and is good enough
    for a "you are using N tokens" status line.
    """
    if not text:
        return 0
    return max(1, len(text) // 4)


def render_stream_tail(accum: str, budget: int = 2000) -> str:
    """Return the last ``budget`` characters of ``accum``, but if the
    cut lands mid-word, snap forward to the next whitespace so the
    rendered tail starts on a clean token boundary.

    Why: the streaming widget redraws on every chunk; without alignment
    long tails like ``...he agent loop is a finite state ma|chine that…``
    flicker mid-word as the cut point drifts. Snapping to whitespace
    keeps the head of the visible tail readable.

    Edge cases:
    - ``len(accum) <= budget`` returns ``accum`` unchanged.
    - ``budget <= 0`` returns the empty string.
    - If the last ``budget`` characters contain no whitespace at all
      (e.g. one giant base64 blob), we keep the raw cut rather than
      collapse to empty.
    - We snap forward by at most 64 characters so the tail never
      shrinks below ~``budget - 64``.
    """
    if budget <= 0:
        return ""
    if len(accum) <= budget:
        return accum
    cut = len(accum) - budget
    window_end = min(cut + 64, len(accum))
    for i in range(cut, window_end):
        if accum[i].isspace():
            return accum[i + 1 :]
    return accum[cut:]


@dataclass
class TurnProfile:
    """Timing profile of a single agent turn — drives ``/lat``.

    Captures the data that loops 175-178 emit as ``AgentEvent``s
    (per-tool latency, TTFT, aggregate summary) plus the wall-clock
    span of the whole turn. ``tool_calls`` is ordered by call site so
    the rendered breakdown reads top-to-bottom like the transcript.
    """

    started_at: float = 0.0
    ended_at: float | None = None
    ttft_s: float | None = None
    tool_calls: list[tuple[str, float | None]] = field(default_factory=list)
    summary_text: str | None = None
    summary_total_s: float | None = None

    def total_s(self) -> float | None:
        if self.ended_at is None:
            return None
        return self.ended_at - self.started_at


def format_turn_profile(
    profile: TurnProfile | None, *, width: int | None = None
) -> str:
    """Render a ``TurnProfile`` as a human-readable timing breakdown.

    Layout::

        last turn:
          total:       2.3s
          first token: 0.4s
          tools (3):
            1. fs_read           (12ms)
            2. fs_grep           (45ms)
            3. shell_run         (1.8s)
          summary: 3 tool calls, 1.857s total

    ``None`` and "no calls yet" both render as a single status line so
    callers don't have to special-case the empty path. Pure so unit
    tests can pin behaviour without booting the App.
    """
    if profile is None:
        return "no agent turn has run yet"
    # Effective width: explicit kwarg wins, else current terminal,
    # else 80 as a long-standing safe default. Floor at 40 so the
    # tool-name column (capped at 20 + " 1. " + " (1.2s)") still fits.
    if width is None:
        import shutil

        try:
            width = shutil.get_terminal_size((80, 24)).columns
        except Exception:  # noqa: BLE001
            width = 80
    width = max(40, int(width))
    lines: list[str] = ["last turn:"]
    total = profile.total_s()
    if total is not None:
        lines.append(f"  total:       {format_tool_latency(total)[1:-1]}")
    if profile.ttft_s is not None:
        lines.append(
            f"  first token: {format_tool_latency(profile.ttft_s)[1:-1]}"
        )
    if profile.tool_calls:
        lines.append(f"  tools ({len(profile.tool_calls)}):")
        # Width of the tool-name column = longest name, capped at 20,
        # but shrunk further on truly narrow terminals so the latency
        # column still has room. Reserve 6 for "    1." prefix, 1 for
        # space, 8 for "(123ms)" / "(1m23s)".
        budget = max(8, width - 6 - 1 - 8)
        name_cap = min(20, budget)
        col_w = min(name_cap, max(len(name) for name, _ in profile.tool_calls))
        for idx, (name, lat) in enumerate(profile.tool_calls, start=1):
            lat_str = format_tool_latency(lat) if lat is not None else "(?)"
            disp = name if len(name) <= col_w else name[: col_w - 1] + "…"
            lines.append(f"    {idx}. {disp:<{col_w}} {lat_str}")
    else:
        lines.append("  tools (0): (no tool calls)")
    if profile.summary_text:
        # Wrap the summary across the available terminal width with a
        # hanging indent that lines up under the colon, so on narrow
        # terminals long summaries don't overflow.
        import textwrap

        prefix = "  summary: "
        wrapped = textwrap.fill(
            profile.summary_text,
            width=max(20, width),
            initial_indent=prefix,
            subsequent_indent=" " * len(prefix),
            break_long_words=False,
            break_on_hyphens=False,
        )
        lines.append(wrapped)
    return "\n".join(lines)


DEFAULT_TURN_PROFILE_HISTORY = 20


def format_turn_profiles(
    profiles: list[TurnProfile], n: int = 1, *, width: int | None = None
) -> str:
    """Render the last ``n`` ``TurnProfile``s as a stacked listing.

    Each profile is rendered via ``format_turn_profile`` and prefixed
    with a ``=== turn -k ===`` header where ``k`` counts back from the
    most recent (``-1`` is the most recent turn). ``n`` is clamped to
    ``[1, len(profiles)]``; ``n <= 0`` is treated as 1. Empty input
    falls back to ``format_turn_profile(None)`` so the empty path
    matches the single-turn API.
    """
    if not profiles:
        return format_turn_profile(None, width=width)
    if n <= 0:
        n = 1
    n = min(n, len(profiles))
    selected = profiles[-n:]
    if n == 1:
        return format_turn_profile(selected[-1], width=width)
    blocks: list[str] = []
    for offset, prof in enumerate(reversed(selected), start=1):
        blocks.append(f"=== turn -{offset} ===")
        blocks.append(format_turn_profile(prof, width=width))
    return "\n".join(blocks)


def turn_profiles_as_json(profiles: list[TurnProfile]) -> str:
    """Serialise ``profiles`` as a JSON array suitable for downstream
    tooling (jq, log shippers, custom dashboards). Each tool-call tuple
    is flattened to ``{"name": ..., "elapsed_s": ...}``. Empty input
    returns ``"[]"``."""
    import json

    rows: list[dict[str, object]] = []
    for prof in profiles:
        rows.append(
            {
                "started_at": prof.started_at,
                "ended_at": prof.ended_at,
                "ttft_s": prof.ttft_s,
                "summary_text": prof.summary_text,
                "summary_total_s": prof.summary_total_s,
                "total_s": prof.total_s(),
                "tool_calls": [
                    {"name": name, "elapsed_s": elapsed}
                    for name, elapsed in prof.tool_calls
                ],
            }
        )
    return json.dumps(rows, indent=2)


def format_tool_latency(elapsed_s: float) -> str:
    """Render a per-tool elapsed time the way the agent transcript wants
    it: ``(123ms)`` for sub-second, ``(2.4s)`` for ≥1s, ``(1m04s)`` for
    ≥60s. Negative inputs render as ``(?)`` rather than raise — wall-clock
    weirdness shouldn't tank the UI."""
    if elapsed_s < 0:
        return "(?)"
    if elapsed_s < 1.0:
        return f"({int(elapsed_s * 1000)}ms)"
    if elapsed_s < 60.0:
        return f"({elapsed_s:.1f}s)"
    minutes = int(elapsed_s // 60)
    seconds = int(elapsed_s - minutes * 60)
    return f"({minutes}m{seconds:02d}s)"


_MARKDOWN_HINTS = (
    "```",
    "\n# ",
    "\n## ",
    "\n### ",
    "\n- ",
    "\n* ",
    "\n1. ",
    "\n2. ",
    "\n> ",
    "\n|",
    "**",
    "__",
)


_MARKDOWN_INLINE_LINK_RE = None  # lazy-compile in looks_like_markdown


def looks_like_markdown(text: str) -> bool:
    """Heuristic: true when the assistant reply contains markdown structure
    that benefits from rich rendering rather than being treated as plain text.

    Used by the App layer to decide whether to wrap a reply in
    ``rich.markdown.Markdown`` before writing it to the RichLog. Kept pure
    so unit tests can exercise it without a Textual app.

    Loop 281: extended to recognise inline backtick code spans (when at
    least two backticks appear), markdown links of the form ``[text](url)``,
    and table rows starting with ``|``. This helps short replies like
    ``Use the `find` command.`` render with monospace highlighting instead
    of as flat plain text.
    """
    if not text:
        return False
    haystack = "\n" + text
    if any(hint in haystack for hint in _MARKDOWN_HINTS):
        return True
    # Inline code: at least two backticks (a paired span). Single stray
    # backticks shouldn't trigger Markdown rendering -- they often appear
    # in shell output.
    if text.count("`") >= 2:
        return True
    global _MARKDOWN_INLINE_LINK_RE
    if _MARKDOWN_INLINE_LINK_RE is None:
        import re as _re

        _MARKDOWN_INLINE_LINK_RE = _re.compile(r"\[[^\]\n]+\]\([^)\n]+\)")
    if _MARKDOWN_INLINE_LINK_RE.search(text):
        return True
    return False


def chat_turn(
    history: list[ChatMessage],
    user_text: str,
    *,
    client: QwenClient,
    system: str = prompts.CODER_SYSTEM,
    fs_cfg: fs_tools.FsConfig | None = None,
) -> str:
    """Append `user_text` to history and return the assistant reply.

    `history` is mutated in place: user message added, then assistant
    reply appended on success. If `fs_cfg` is provided, `@path` tokens
    in `user_text` are expanded inline before sending to the model.
    """
    if not history or history[0].role != "system":
        history.insert(0, ChatMessage(role="system", content=system))
    expanded = (
        expand_at_mentions(fs_cfg, user_text)
        if fs_cfg is not None
        else user_text
    )
    history.append(ChatMessage(role="user", content=expanded))
    try:
        reply = client.chat(history)
    except Exception as exc:  # noqa: BLE001
        return _friendly_chat_error(exc)
    history.append(ChatMessage(role="assistant", content=reply))
    return reply


def _friendly_chat_error(exc: BaseException) -> str:
    """Render a chat exception as a user-facing string with a hint
    when the failure is a backend connection problem rather than a
    model-side error. Imports httpx lazily so callers without the
    dependency installed do not pay the cost.
    """
    name = type(exc).__name__
    text = str(exc)
    try:
        import httpx as _httpx
        connect_types: tuple[type, ...] = (
            _httpx.ConnectError,
            _httpx.ConnectTimeout,
        )
    except ImportError:
        connect_types = ()
    text_lower = text.lower()
    looks_connect = isinstance(exc, connect_types) or (
        "ConnectError" in name
        or "connection refused" in text_lower
        or "connection reset" in text_lower
        or "connecttimeout" in text_lower
    )
    if looks_connect:
        return (
            f"chat error: {name}: {text}\n"
            "hint: is the qwen server running? "
            "start it with scripts/serve_qwen.sh and wait for "
            "'application startup complete' in .loop/serve.log"
        )
    return f"chat error: {name}: {text}"


def chat_turn_stream(
    history: list[ChatMessage],
    user_text: str,
    *,
    client: QwenClient,
    system: str = prompts.CODER_SYSTEM,
    fs_cfg: fs_tools.FsConfig | None = None,
):
    """Streaming counterpart of `chat_turn`. Yields `(chunk, accum)` tuples
    where `accum` is the full reply assembled so far. On stream end the
    final assistant reply is appended to `history`. On error a single
    final chunk with the error message is yielded and history rolled back
    (the trailing user message stays so the user can retry, but no
    partial assistant message is committed). When `fs_cfg` is provided,
    `@path` tokens in `user_text` are expanded inline before sending.
    """
    if not history or history[0].role != "system":
        history.insert(0, ChatMessage(role="system", content=system))
    expanded = (
        expand_at_mentions(fs_cfg, user_text)
        if fs_cfg is not None
        else user_text
    )
    history.append(ChatMessage(role="user", content=expanded))
    accum_parts: list[str] = []
    try:
        for chunk in client.chat_stream(history):
            accum_parts.append(chunk)
            yield chunk, "".join(accum_parts)
    except Exception as exc:  # noqa: BLE001
        err = f"\n[stream error: {type(exc).__name__}: {exc}]"
        accum_parts.append(err)
        yield err, "".join(accum_parts)
        return
    final = "".join(accum_parts)
    history.append(ChatMessage(role="assistant", content=final))


def _default_fs_cfg() -> fs_tools.FsConfig:
    root_str = os.environ.get("QWEN_MCP_FS_ROOT") or os.getcwd()
    return fs_tools.FsConfig(root=Path(root_str))


def history_file_path(cfg: fs_tools.FsConfig) -> Path:
    """Resolve the per-root jsonl history file inside `.agent/`.

    Lives at `<root>/.agent/tui_history.jsonl`. The directory is created
    on demand by save_history_jsonl. We deliberately keep this under the
    repo root so it is gitignored in the same way the rest of `.agent/`
    is, rather than leaking into a shared `~/.config` location.
    """
    return cfg.root / ".agent" / "tui_history.jsonl"


def save_history_jsonl(
    history: list[ChatMessage], path: Path, *, max_messages: int = 500
) -> int:
    """Write the trailing `max_messages` of history as one JSON object per line.

    Returns the number of messages written. Silently no-ops if the
    parent directory cannot be created or the file cannot be written
    (the TUI should never crash on save failures during shutdown).
    """
    import json

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return 0
    tail = history[-max_messages:]
    # Atomic write: serialise to a sibling .tmp, fsync, then os.replace
    # so a crash mid-write can never leave the on-disk history in a
    # half-written state. Mirrors save_agent_checkpoint.
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            for msg in tail:
                fh.write(
                    json.dumps({"role": msg.role, "content": msg.content})
                    + "\n"
                )
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return 0
    return len(tail)


def load_history_jsonl(path: Path) -> list[ChatMessage]:
    """Read jsonl history written by save_history_jsonl. Missing or
    malformed lines are skipped silently; a missing file returns [].
    """
    import json

    if not path.exists():
        return []
    out: list[ChatMessage] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    continue
                role = obj.get("role")
                content = obj.get("content")
                if role in {"system", "user", "assistant"} and isinstance(
                    content, str
                ):
                    out.append(ChatMessage(role=role, content=content))
    except OSError:
        return []
    return out


def _resolve_inherit_write(agent_write_default: bool) -> bool:
    """Loop 286: tiny helper that makes the write-flag inheritance rule
    explicit and directly testable.  ``_start_agent_turn`` calls this
    when ``write=None`` (its new default) so any call-site that omits
    ``write=`` automatically follows the app-level ``agent_write_default``
    toggle instead of silently falling back to read-only False.
    """
    return bool(agent_write_default)


def _build_app(
    client_factory: Callable[[], QwenClient] | None = None,
    fs_cfg: fs_tools.FsConfig | None = None,
):
    """Construct the Textual App. Imported lazily so the `tui` extra
    is only required when running the TUI."""
    from textual.app import App, ComposeResult
    from textual.containers import Vertical
    from textual.screen import ModalScreen  # type: ignore
    from textual.widgets import Footer, Header, Input, RichLog, Static
    try:
        from textual import work  # type: ignore
        _HAS_WORK = True
    except ImportError:
        _HAS_WORK = False
    try:
        from textual.suggester import SuggestFromList  # type: ignore
        _suggester: object | None = SuggestFromList(SLASH_COMMANDS, case_sensitive=False)
    except ImportError:
        _suggester = None

    cfg = fs_cfg or _default_fs_cfg()
    factory = client_factory or QwenClient

    class _ConfirmScreen(ModalScreen[bool]):  # type: ignore[misc, type-arg]
        """Tiny modal that asks the user to approve a destructive tool
        call. Yes / No are bound to ``y`` / ``n``; ``escape`` denies."""

        BINDINGS = [
            ("y", "approve", "Yes"),
            ("n", "deny", "No"),
            ("escape", "deny", "No"),
        ]

        CSS = """
        _ConfirmScreen {
            align: center middle;
        }
        #confirm-box {
            width: 70%;
            max-width: 100;
            border: thick $warning;
            padding: 1 2;
            background: $panel;
        }
        #confirm-title {
            color: $warning;
            text-style: bold;
        }
        #confirm-detail {
            margin: 1 0;
        }
        #confirm-help {
            color: $text-muted;
        }
        """

        def __init__(self, prompt: str, detail: str) -> None:
            super().__init__()
            self._prompt = prompt
            self._detail = detail

        def compose(self) -> ComposeResult:  # type: ignore[override]
            with Vertical(id="confirm-box"):
                yield Static(f"⚠  {self._prompt}", id="confirm-title")
                yield Static(self._detail, id="confirm-detail")
                yield Static(
                    "[y] approve    [n] deny    [esc] deny",
                    id="confirm-help",
                )

        def action_approve(self) -> None:
            self.dismiss(True)

        def action_deny(self) -> None:
            self.dismiss(False)

    class _AskScreen(ModalScreen[str]):  # type: ignore[misc, type-arg]
        """Loop 284: modal that asks the operator to answer the model's
        ``ask_user`` tool call. Shows the question text plus either a
        list of numbered choices (1..9 hot-keys) or a free-form Input.
        ``escape`` cancels (resolves to empty string)."""

        BINDINGS = [
            ("escape", "cancel", "Cancel"),
        ]

        CSS = """
        _AskScreen {
            align: center middle;
        }
        #ask-box {
            width: 80%;
            max-width: 110;
            border: thick $accent;
            padding: 1 2;
            background: $panel;
        }
        #ask-title {
            color: $accent;
            text-style: bold;
        }
        #ask-question {
            margin: 1 0;
        }
        #ask-help {
            color: $text-muted;
            margin-top: 1;
        }
        #ask-input {
            margin-top: 1;
        }
        """

        def __init__(self, question: str, choices: list[str]) -> None:
            super().__init__()
            self._question = question
            self._choices = list(choices)

        def compose(self) -> ComposeResult:  # type: ignore[override]
            with Vertical(id="ask-box"):
                yield Static("? agent asks", id="ask-title")
                yield Static(_safe_markup(self._question), id="ask-question")
                if self._choices:
                    lines = []
                    for i, c in enumerate(self._choices[:9], start=1):
                        lines.append(f"  [{i}] {_safe_markup(c)}")
                    yield Static("\n".join(lines))
                    yield Static(
                        "[1-9] pick a choice    [esc] cancel",
                        id="ask-help",
                    )
                else:
                    yield Static(
                        "type your answer below and press Enter; [esc] cancels",
                        id="ask-help",
                    )
                    yield Input(placeholder="answer…", id="ask-input")

        def on_mount(self) -> None:  # type: ignore[override]
            if not self._choices:
                try:
                    self.query_one("#ask-input", Input).focus()
                except Exception:  # noqa: BLE001
                    pass

        def on_key(self, event) -> None:  # type: ignore[override]
            if self._choices and event.key in {
                "1", "2", "3", "4", "5", "6", "7", "8", "9"
            }:
                idx = int(event.key) - 1
                if 0 <= idx < len(self._choices):
                    self.dismiss(self._choices[idx])
                    event.stop()

        def on_input_submitted(self, event) -> None:  # type: ignore[override]
            self.dismiss(event.value)

        def action_cancel(self) -> None:
            self.dismiss("")

    class QwenTUI(App):  # type: ignore[misc]
        CSS = """
        Screen {
            layout: vertical;
            background: $surface;
        }
        Header {
            background: $primary-background;
            color: $text;
            text-style: bold;
        }
        #log {
            height: 1fr;
            border: round $primary;
            padding: 0 1;
            background: $surface-darken-1;
            scrollbar-background: $surface;
            scrollbar-color: $primary;
        }
        #stream {
            display: none;
            height: auto;
            max-height: 12;
            padding: 0 1;
            margin: 0 0 0 0;
            border-left: thick $accent;
            color: $text;
            background: $surface-darken-2;
        }
        #stream.live {
            display: block;
        }
        #status {
            dock: bottom;
            height: 1;
            padding: 0 1;
            background: $primary-background;
            color: $text-muted;
            text-style: italic;
        }
        Input {
            dock: bottom;
            border: tall $accent;
            background: $surface-darken-1;
        }
        Input:focus {
            border: tall $primary;
        }
        Footer {
            background: $primary-background;
        }
        """
        BINDINGS = [
            ("ctrl+c", "quit", "Quit"),
            ("ctrl+l", "clear_log", "Clear screen"),
            ("ctrl+r", "redraw", "Redraw"),
            ("ctrl+s", "save_history", "Save history"),
        ]

        def __init__(self) -> None:
            super().__init__()
            self.client = factory()
            self.history: list[ChatMessage] = []
            self.fs_cfg = cfg
            self.last_turn_tokens: int = 0
            self.last_turn_seconds: float = 0.0
            self.last_turn_profile: TurnProfile | None = None
            self.turn_profiles: list[TurnProfile] = []
            self.total_tokens: int = 0
            self.total_turns: int = 0
            self._streaming: bool = False
            # Agent default: when True, normal chat goes through the
            # tool-calling loop. Loop 283: flipped to True so the model
            # always has the full tool registry visible and the system
            # prompt's catalog matches what's actually wired. Toggle
            # back off with /agent_off if you want plain streaming.
            self.agent_default: bool = True
            # Write mode adds fs_write + fs_edit + apply_patch + mkdir
            # + touch + mv + run_shell to the registry. Loop 283:
            # flipped to True by default. Each destructive call still
            # pops the y/n modal because agent_confirm_writes defaults
            # to True; use /allow_all to bypass prompts for a session.
            self.agent_write_default: bool = True
            # When True, every destructive tool call pops a y/n modal
            # before firing. When False, calls are auto-approved (still
            # logged via the audit hook). Default is to ask.
            self.agent_confirm_writes: bool = True
            # Loop 250: default-DENY for /run. Operator must either
            # type ``/run --yes <cmd>`` for one-shot approval or run
            # ``/run_on`` to auto-approve every /run for the session.
            self.run_auto_approve: bool = False
            # Loop 266: two-phase /run preview. When non-empty (a dict
            # attribute, even if empty), the slash-dispatcher stages
            # /run <cmd> commands here with a short stage_id and shows
            # a dry-run preview; operator confirms with /yes <id> or
            # cancels with /no <id>. Auto-vivifying the dict here is
            # what tells dispatch_slash to use staging rather than the
            # legacy "denied" path. Tests that don't want staging just
            # don't expose this attribute on their stub app.
            self.pending_runs: dict[str, _StagedRun] = {}

        def compose(self) -> ComposeResult:  # type: ignore[override]
            yield Header(show_clock=True)
            with Vertical():
                yield RichLog(id="log", highlight=True, markup=True, wrap=True)
                yield Static("", id="stream")
            if _suggester is not None:
                yield Input(
                    placeholder=">  message, /help, or @path",
                    id="entry",
                    suggester=_suggester,  # type: ignore[arg-type]
                )
            else:
                yield Input(placeholder=">  message, /help, or @path", id="entry")
            yield Static("", id="status")
            yield Footer()

        def on_mount(self) -> None:  # type: ignore[override]
            self.title = "qwen-coder-tui"
            settings = getattr(self.client, "settings", None)
            self.sub_title = (
                getattr(settings, "model", None) or "qwen"
            ) + " @ " + (
                getattr(settings, "base_url", None) or "(unset)"
            )
            log = self.query_one("#log", RichLog)
            log.write("[bold cyan]qwen-coder-tui[/bold cyan]  type [bold]/help[/bold] for slash commands or [bold]@path[/bold] to attach a file")
            self._render_health_banner(log)
            try:
                prior = load_history_jsonl(history_file_path(self.fs_cfg))
            except Exception:  # noqa: BLE001
                prior = []
            if prior:
                self.history.extend(prior)
                log.write(f"[dim]restored {len(prior)} prior messages[/dim]")
            else:
                hint = render_checkpoint_hint(self.fs_cfg)
                if hint:
                    log.write(hint)
            self._refresh_status()

        def on_unmount(self) -> None:  # type: ignore[override]
            try:
                save_history_jsonl(
                    self.history, history_file_path(self.fs_cfg)
                )
            except Exception:  # noqa: BLE001
                pass

        def action_clear_log(self) -> None:  # type: ignore[override]
            self.query_one("#log", RichLog).clear()

        def action_redraw(self) -> None:  # type: ignore[override]
            self.refresh(layout=True)

        def action_save_history(self) -> None:  # type: ignore[override]
            """Persist chat history to disk on demand.

            on_unmount also saves, but that won't fire if Textual crashes
            mid-turn -- this gives the user a manual flush via Ctrl+S.
            """
            try:
                path = history_file_path(self.fs_cfg)
                save_history_jsonl(self.history, path)
                msg = f"[green]✓ saved {len(self.history)} messages[/green] → {path}"
            except Exception as exc:  # noqa: BLE001
                msg = f"[red]save failed:[/red] {_safe_markup(type(exc).__name__)}: {_safe_markup(exc)}"
            try:
                self.query_one("#log", RichLog).write(msg)
            except Exception:  # noqa: BLE001
                pass

        def _refresh_status(self, *, streaming: bool = False) -> None:
            try:
                status = self.query_one("#status", Static)
            except Exception:  # noqa: BLE001
                return
            settings = getattr(self.client, "settings", None)
            model = getattr(settings, "model", None) or "qwen"
            msgs = len(self.history)
            ttok = self.total_tokens
            prefix = "[yellow]● streaming…[/yellow]  " if streaming else "  "
            agent_mode = (
                f"[green]agent+write[/green]" if (self.agent_default and self.agent_write_default)
                else f"[cyan]agent[/cyan]" if self.agent_default
                else "[dim]plain[/dim]"
            )
            confirm_tag = " [yellow]confirm[/yellow]" if (self.agent_confirm_writes and self.agent_write_default) else ""
            line = (
                f"{prefix}{model}  ·  {agent_mode}{confirm_tag}  ·  "
                f"{msgs} msg  ·  ~{ttok} tok total  ·  "
                f"last turn ~{self.last_turn_tokens} tok in "
                f"{self.last_turn_seconds:.1f}s"
            )
            status.update(line)

        def _render_health_banner(self, log) -> None:  # type: ignore[no-untyped-def]
            """Probe the backend and write a status banner.

            Two probes, two questions:
              * ``health_check()`` asks "is the OpenAI-compatible API
                server answering on /v1/models?". Catches connection
                failure and 4xx auth issues.
              * ``vllm_health_probe()`` asks "did the engine actually
                finish initialising?". Catches the loops 211 and 216
                bug class -- the API server can answer 200 on
                /v1/models long before the engine reaches readiness,
                or after the engine has crashed but the API server
                hasn't shut down yet.

            When both succeed only the API-side line is shown to keep
            the banner concise. When they diverge, the engine line is
            the actionable signal and is appended in red.
            """
            try:
                check = self.client.health_check()
            except Exception as exc:  # noqa: BLE001
                log.write(
                    f"[red]✗ health check raised:[/red] "
                    f"{_safe_markup(type(exc).__name__)}: {_safe_markup(exc)}"
                )
                return
            if check.get("ok"):
                models = check.get("models") or []
                tag = ", ".join(models[:3]) or "(no models reported)"
                log.write(f"[green]✓ backend ok[/green]  models: {_safe_markup(tag)}")
                self._render_engine_probe_line(log)
                return
            err = check.get("error") or "unknown error"
            hint = check.get("hint")
            log.write(f"[red]✗ backend unavailable:[/red] {_safe_markup(err)}")
            if hint:
                log.write(f"[yellow]→ hint:[/yellow] {_safe_markup(hint)}")

        def _render_engine_probe_line(self, log) -> None:  # type: ignore[no-untyped-def]
            """If the engine /health probe is available and disagrees
            with the API-server probe, surface the divergence.

            Silent on the happy path (both ok) -- the API-side line
            already told the user everything is fine. Silent when the
            client has no probe method (test stubs, older clients).
            """
            probe_fn = getattr(self.client, "vllm_health_probe", None)
            if not callable(probe_fn):
                return
            try:
                probe = probe_fn() or {}
            except Exception as exc:  # noqa: BLE001
                # Probe is observability; never crash the banner.
                log.write(
                    f"[yellow]⚠ engine probe raised:[/yellow] "
                    f"{_safe_markup(type(exc).__name__)}: {_safe_markup(exc)}"
                )
                return
            for line in format_engine_probe_lines(probe):
                log.write(line)

        def on_input_submitted(self, event: Input.Submitted) -> None:  # type: ignore[override]
            if self._streaming:
                # Ignore submits while a reply is still streaming so
                # users can't double-fire and corrupt history.
                return
            line = event.value.strip()
            if not line:
                return
            entry = self.query_one("#entry", Input)
            entry.value = ""
            log = self.query_one("#log", RichLog)
            log.write(f"[bold cyan]you›[/bold cyan] {_safe_markup(line)}")
            cmd = parse_slash(line)
            if cmd is not None:
                text, quit_now = dispatch_slash(
                    cmd,
                    client=self.client,
                    fs_cfg=self.fs_cfg,
                    history=self.history,
                    app=self,
                )
                if isinstance(text, str) and text.startswith("__RETRY__"):
                    line = text[len("__RETRY__"):]
                    log.write(f"[yellow](retrying)[/yellow] {_safe_markup(line)}")
                elif isinstance(text, str) and text.startswith(_AGENT_SENTINEL):
                    task = text[len(_AGENT_SENTINEL):]
                    task, max_steps, resume = _decode_agent_body(task)
                    if resume:
                        self._apply_agent_resume(log)
                    self._streaming = True
                    self._start_agent_turn(
                        task,
                        write=self.agent_write_default,
                        max_steps=max_steps,
                    )
                    return
                elif isinstance(text, str) and text.startswith(_AGENT_WRITE_SENTINEL):
                    task = text[len(_AGENT_WRITE_SENTINEL):]
                    task, max_steps, resume = _decode_agent_body(task)
                    if resume:
                        self._apply_agent_resume(log)
                    self._streaming = True
                    self._start_agent_turn(
                        task, write=True, max_steps=max_steps
                    )
                    return
                elif isinstance(text, str) and text.startswith(_AGENT_TOGGLE_SENTINEL):
                    flag = text[len(_AGENT_TOGGLE_SENTINEL):]
                    if flag == "on":
                        self.agent_default = True
                    elif flag == "off":
                        self.agent_default = False
                    elif flag == "write_on":
                        self.agent_write_default = True
                    elif flag == "write_off":
                        self.agent_write_default = False
                    elif flag == "confirm_on":
                        self.agent_confirm_writes = True
                    elif flag == "confirm_off":
                        self.agent_confirm_writes = False
                    elif flag == "allow_all":
                        self.agent_default = True
                        self.agent_write_default = True
                        self.agent_confirm_writes = False
                        self.run_auto_approve = True
                        log.write(
                            "[bold red]/allow_all: agent + writes + "
                            "auto-approve all ON. No confirmation prompts. "
                            "Operator owns the blast radius.[/bold red]"
                        )
                    elif flag == "safe_mode":
                        self.agent_default = False
                        self.agent_write_default = False
                        self.agent_confirm_writes = True
                        self.run_auto_approve = False
                        log.write(
                            "[bold green]/safe_mode: agent + writes off, "
                            "all confirmations re-enabled.[/bold green]"
                        )
                    elif flag in ("mouse_off", "mouse_on", "mouse_toggle"):
                        # Loop 280: toggle Textual mouse capture so the
                        # host terminal can do native click-drag select
                        # (and copy via the terminal's own UI / Cmd-C).
                        # We emit the standard XTerm mouse-tracking
                        # disable/enable escape codes through sys.stdout;
                        # while disabled, drag selection is captured by
                        # the terminal emulator instead of by Textual.
                        import sys as _sys

                        try:
                            current = bool(getattr(self, "_mouse_released", False))
                            if flag == "mouse_off":
                                want_off = True
                            elif flag == "mouse_on":
                                want_off = False
                            else:
                                want_off = not current
                            if want_off:
                                _sys.stdout.write(
                                    "\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?1015l"
                                )
                                _sys.stdout.flush()
                                self._mouse_released = True
                                log.write(
                                    "[bold yellow]/mouse off: terminal-native "
                                    "selection enabled. Drag to select; copy via "
                                    "your terminal (Cmd/Ctrl+Shift+C, or right-click). "
                                    "Run /mouse on to restore Textual mouse.[/bold yellow]"
                                )
                            else:
                                _sys.stdout.write("\x1b[?1000h\x1b[?1003h\x1b[?1006h")
                                _sys.stdout.flush()
                                self._mouse_released = False
                                log.write(
                                    "[dim]/mouse on: Textual mouse capture restored.[/dim]"
                                )
                        except Exception as exc:  # pragma: no cover
                            log.write(
                                f"[red]/mouse toggle failed: "
                                f"{_safe_markup(type(exc).__name__)}: "
                                f"{_safe_markup(exc)}[/red]"
                            )
                    state = (
                        f"agent={'on' if self.agent_default else 'off'} "
                        f"write={'on' if self.agent_write_default else 'off'} "
                        f"confirm={'on' if self.agent_confirm_writes else 'off'}"
                    )
                    log.write(f"[dim]{state}[/dim]")
                    return
                elif isinstance(text, str) and text.startswith(_CD_SENTINEL):
                    new_root = text[len(_CD_SENTINEL):]
                    self.fs_cfg = fs_tools.FsConfig(
                        root=Path(new_root),
                        max_read_bytes=self.fs_cfg.max_read_bytes,
                        max_write_bytes=self.fs_cfg.max_write_bytes,
                        max_list_entries=self.fs_cfg.max_list_entries,
                    )
                    log.write(f"[dim](cwd)[/dim] {_safe_markup(new_root)}")
                    self._refresh_status()
                    return
                else:
                    _safe_log_write(log, text)
                    self._refresh_status()
                    if quit_now:
                        self.exit()
                    return
            self._streaming = True
            if self.agent_default:
                self._start_agent_turn(line, write=self.agent_write_default)
            else:
                self._start_streaming_turn(line)

        def _start_streaming_turn(self, line: str) -> None:
            t0 = time.monotonic()
            stream = self.query_one("#stream", Static)
            stream.update("")
            stream.add_class("live")
            self._refresh_status(streaming=True)

            def runner() -> None:
                full = ""
                try:
                    for chunk, accum in chat_turn_stream(
                        self.history,
                        line,
                        client=self.client,
                        fs_cfg=self.fs_cfg,
                    ):
                        full = accum
                        self.call_from_thread(self._on_stream_chunk, accum)
                except AttributeError:
                    # Client doesn't implement chat_stream; fall back.
                    try:
                        full = chat_turn(
                            self.history,
                            line,
                            client=self.client,
                            fs_cfg=self.fs_cfg,
                        )
                    except Exception as exc:  # noqa: BLE001
                        full = f"[stream error: {type(exc).__name__}: {exc}]"
                except Exception as exc:  # noqa: BLE001
                    full = f"[stream error: {type(exc).__name__}: {exc}]"
                self.call_from_thread(
                    self._finalize_stream, line, full, time.monotonic() - t0
                )

            if _HAS_WORK:
                self.run_worker(runner, thread=True, exclusive=True)
            else:  # pragma: no cover - very old textual
                import threading

                threading.Thread(target=runner, daemon=True).start()

        def _on_stream_chunk(self, accum: str) -> None:
            try:
                stream = self.query_one("#stream", Static)
            except Exception:  # noqa: BLE001
                return
            tail = render_stream_tail(accum, 2000)
            stream.update(f"[green]qwen›[/green] {tail}▍")

        def _reset_stream_buffer(self) -> None:
            """Clear the live stream widget between agent turns."""
            try:
                stream = self.query_one("#stream", Static)
                stream.update("[yellow]…[/yellow]")
            except Exception:  # noqa: BLE001
                return

        def _finalize_stream(self, prompt: str, reply: str, elapsed: float) -> None:
            try:
                stream = self.query_one("#stream", Static)
                stream.update("")
                stream.remove_class("live")
                log = self.query_one("#log", RichLog)
            except Exception:  # noqa: BLE001
                self._streaming = False
                return
            # If the streamed reply contains tool calls, the model is
            # asking for tool execution. Roll back the streaming-only
            # bookkeeping (chat_turn_stream already appended the reply
            # to history) and run the agent loop to resolve them.
            if agent_loop.parse_tool_calls(reply):
                # chat_turn_stream appended user+assistant; pop the
                # assistant so run_agent re-issues the same prompt as
                # a fresh user turn (history stays clean).
                if self.history and self.history[-1].role == "assistant":
                    self.history.pop()
                if self.history and self.history[-1].role == "user":
                    self.history.pop()
                log.write(
                    "[dim](tool calls detected — switching to agent mode)[/dim]"
                )
                self._start_agent_turn(prompt)
                return
            self._record_turn(prompt, reply, elapsed)
            self._post_assistant(log, reply)
            log.write(self._telemetry_line())
            self._refresh_status()
            self._streaming = False

        def _apply_agent_resume(self, log: Any) -> None:
            """Pre-load the latest agent checkpoint into ``self.history``
            in-place. Used by ``/agent --resume``. Logs a one-line status
            so the user knows what was restored (or that nothing was)."""
            try:
                target = self.fs_cfg.root / ".agent" / "agent_state.json"
                loaded, source = agent_loop.load_latest_checkpoint(target)
            except Exception as exc:  # noqa: BLE001
                log.write(f"[yellow]⚠ resume failed: {_safe_markup(exc)}[/yellow]")
                return
            if not loaded or source is None:
                log.write("[yellow]·[/yellow] /agent --resume: no checkpoint to load")
                return
            self.history.clear()
            self.history.extend(loaded)
            log.write(
                f"[dim]· resumed {len(loaded)} messages from "
                f"{source.name} before agent turn[/dim]"
            )

        def _start_agent_turn(
            self,
            task: str,
            *,
            write: bool | None = None,
            max_steps: int | None = None,
        ) -> None:
            """Run an agentic tool-calling turn in a worker thread.

            Reuses the streaming Static widget for live status of which
            tool is firing; final answer is rendered via _post_assistant
            once the loop ends. ``write=True`` exposes fs_write +
            apply_patch tools, allowing the agent to edit the workspace.
            ``write=None`` (default) follows ``self.agent_write_default``
            -- this is the safety net behind loop 286's bug where plain
            user input ran with write=False even though /allow_all and
            agent_write_default were on.
            ``max_steps`` overrides the default 6-step cap (1..50).
            """
            if write is None:
                write = _resolve_inherit_write(self.agent_write_default)
            t0 = time.monotonic()
            try:
                stream = self.query_one("#stream", Static)
                badge = "agent+write" if write else "agent"
                if max_steps is not None:
                    badge = f"{badge}/{max_steps}"
                stream.update(f"[yellow]{badge}: thinking…[/yellow]")
                stream.add_class("live")
            except Exception:  # noqa: BLE001
                pass
            self._refresh_status(streaming=True)
            tools = agent_loop.ALL_TOOLS if write else agent_loop.DEFAULT_TOOLS

            # Audit-trail + optional blocking confirmation. The audit
            # line always lands in the log; if agent_confirm_writes is
            # True we additionally pop a modal and block the worker
            # thread on a threading.Event until the user answers (or
            # the 30s default-deny timeout elapses).
            def _confirm_write(call: agent_loop.ToolCall) -> bool:
                if call.name == "fs_write":
                    p = call.args.get("path", "?")
                    n = len(str(call.args.get("content", "")))
                    summary = f"path={p!r} bytes={n}"
                elif call.name == "apply_patch":
                    diff = str(call.args.get("diff", ""))
                    lines = diff.count("\n")
                    check = call.args.get("check_only", False)
                    summary = f"diff_lines={lines} check_only={check}"
                elif call.name == "run_shell":
                    cmd = str(call.args.get("cmd") or call.args.get("command") or "")
                    if len(cmd) > 200:
                        cmd = cmd[:200] + "…"
                    summary = f"$ {cmd}"
                else:
                    summary = repr(call.args)[:120]
                self.call_from_thread(
                    self._agent_status,
                    f"[yellow]✎ write[/yellow] {_safe_markup(call.name)} {_safe_markup(summary)}",
                )
                if not self.agent_confirm_writes:
                    return True
                evt = threading.Event()
                holder: list[bool] = [False]

                def _resolve(value: bool | None) -> None:
                    holder[0] = bool(value)
                    evt.set()

                self.call_from_thread(
                    self._push_confirm,
                    f"agent wants to run {call.name}",
                    summary,
                    _resolve,
                )
                # Default-deny if the user takes too long.
                if not evt.wait(timeout=30.0):
                    self.call_from_thread(
                        self._agent_status,
                        "[red]✗ confirm timeout (30s) — denied[/red]",
                    )
                    return False
                return holder[0]

            def _agent_checkpoint(hist: list[ChatMessage], step: int) -> None:
                """Persist mid-agent transcript so a TUI crash mid-multi-step
                run is recoverable. Writes to ``.agent/agent_state.json``
                relative to the fs sandbox root. Failures are swallowed by
                the run_agent contract — we just note them in the log."""
                try:
                    target = self.fs_cfg.root / ".agent" / "agent_state.json"
                    agent_loop.rotate_agent_checkpoints(
                        target, hist, keep=resolve_rotation_keep()
                    )
                except Exception as exc:  # noqa: BLE001
                    self.call_from_thread(
                        self._agent_status,
                        f"[yellow]⚠ checkpoint failed at step {step}: {_safe_markup(exc)}[/yellow]",
                    )

            def _ask_user_handler(question: str, choices: list[str]) -> str:
                """Loop 284: bridge ``ask_user`` tool calls into the
                _AskScreen modal. Runs in the agent worker thread; uses
                ``call_from_thread`` to push the screen and a
                ``threading.Event`` to wait for the operator's reply.
                """
                evt = threading.Event()
                holder: list[str] = [""]

                def _resolve(value: str | None) -> None:
                    holder[0] = "" if value is None else str(value)
                    evt.set()

                self.call_from_thread(
                    self._push_ask, question, list(choices), _resolve
                )
                if not evt.wait(timeout=120.0):
                    return "timeout"
                ans = holder[0]
                if ans == "":
                    return "user_canceled"
                return ans

            def runner() -> None:
                final_text = ""
                live_buf: list[str] = []
                profile = TurnProfile(started_at=t0)
                # Track the wall-clock start of the most recent tool_call
                # so we can render a (123ms) suffix once its tool_result
                # lands. Calls are sequential in run_agent so a single
                # slot is enough.
                tool_started_at: float | None = None
                pending_tool_name: str | None = None
                kwargs: dict[str, Any] = {
                    "client": self.client,
                    "fs_cfg": self.fs_cfg,
                    "tools": tools,
                    "confirm": _confirm_write,
                    "checkpoint": _agent_checkpoint,
                }
                if max_steps is not None:
                    kwargs["max_steps"] = max_steps
                # Loop 284: install ask_user handler scoped to this
                # worker thread so the model's ``<tool_call>ask_user``
                # entries pop the _AskScreen modal.
                _prev_ask = agent_loop.set_ask_user_handler(_ask_user_handler)
                try:
                    for ev in agent_loop.run_agent(
                        self.history,
                        task,
                        **kwargs,
                    ):
                        if ev.kind == "chunk":
                            live_buf.append(ev.text)
                            self.call_from_thread(
                                self._on_stream_chunk, "".join(live_buf)
                            )
                        elif ev.kind == "assistant":
                            # End of one model turn -- reset live buffer
                            # so the next turn's chunks render fresh.
                            live_buf.clear()
                            self.call_from_thread(self._reset_stream_buffer)
                        elif ev.kind == "tool_call":
                            args_repr = ""
                            if ev.args:
                                bits = []
                                for k, v in ev.args.items():
                                    s = str(v)
                                    if len(s) > 60:
                                        s = s[:60] + "…"
                                    bits.append(f"{k}={s!r}")
                                args_repr = " " + ", ".join(bits)
                            tool_started_at = time.monotonic()
                            pending_tool_name = ev.tool or "?"
                            self.call_from_thread(
                                self._agent_status,
                                f"[cyan]→ tool[/cyan] {_safe_markup(ev.tool)}{_safe_markup(args_repr)}",
                            )
                        elif ev.kind == "tool_result":
                            head = ev.text.splitlines()[0] if ev.text else ""
                            if len(head) > 200:
                                head = head[:200] + "…"
                            # Prefer the authoritative latency from
                            # run_agent; fall back to wall-clock delta
                            # when an older client emits no field.
                            if ev.latency_s is not None:
                                lat_s: float | None = ev.latency_s
                            elif tool_started_at is not None:
                                lat_s = time.monotonic() - tool_started_at
                            else:
                                lat_s = None
                            lat = format_tool_latency(lat_s) if lat_s is not None else ""
                            profile.tool_calls.append(
                                (pending_tool_name or (ev.tool or "?"), lat_s)
                            )
                            tool_started_at = None
                            pending_tool_name = None
                            suffix = f" {lat}" if lat else ""
                            self.call_from_thread(
                                self._agent_status,
                                f"[green]← {_safe_markup(ev.tool)}[/green]{suffix} {_safe_markup(head)}",
                            )
                        elif ev.kind == "final":
                            final_text = ev.text
                        elif ev.kind == "limit":
                            final_text = ev.text
                        elif ev.kind == "summary":
                            profile.summary_text = ev.text
                            profile.summary_total_s = ev.latency_s
                            self.call_from_thread(
                                self._agent_status,
                                f"[dim]· {_safe_markup(ev.text)}[/dim]",
                            )
                        elif ev.kind == "ttft":
                            if ev.latency_s is not None:
                                if profile.ttft_s is None:
                                    profile.ttft_s = ev.latency_s
                                self.call_from_thread(
                                    self._agent_status,
                                    f"[dim]· first token in {format_tool_latency(ev.latency_s)}[/dim]",
                                )
                except Exception as exc:  # noqa: BLE001
                    # Plain text -- _post_assistant will escape it before
                    # interpolating into a markup template. Avoid wrapping
                    # in literal "[...]" because that itself is ambiguous
                    # markup once concatenated with the qwen> prefix.
                    final_text = f"agent error: {type(exc).__name__}: {exc}"
                finally:
                    # Loop 284: always clear the ask_user handler so a
                    # later run_agent in this thread without a host
                    # falls through to the placeholder.
                    agent_loop.set_ask_user_handler(_prev_ask)
                profile.ended_at = time.monotonic()
                self.last_turn_profile = profile
                self.turn_profiles.append(profile)
                if len(self.turn_profiles) > DEFAULT_TURN_PROFILE_HISTORY:
                    # Trim from the front so the buffer stays bounded.
                    del self.turn_profiles[
                        : len(self.turn_profiles) - DEFAULT_TURN_PROFILE_HISTORY
                    ]
                self.call_from_thread(
                    self._finalize_agent, task, final_text, time.monotonic() - t0
                )

            if _HAS_WORK:
                self.run_worker(runner, thread=True, exclusive=True)
            else:  # pragma: no cover
                import threading

                threading.Thread(target=runner, daemon=True).start()

        def _agent_status(self, line: str) -> None:
            try:
                _safe_log_write(self.query_one("#log", RichLog), line)
            except Exception:  # noqa: BLE001
                pass

        def _push_confirm(
            self,
            prompt: str,
            detail: str,
            resolve: Callable[[bool | None], None],
        ) -> None:
            """Pop the y/n modal. Called from the worker via
            call_from_thread; resolves the worker's threading.Event
            via ``resolve`` once the user answers."""
            try:
                self.push_screen(_ConfirmScreen(prompt, detail), resolve)
            except Exception:  # noqa: BLE001
                # If the screen can't be pushed for any reason, deny
                # by default -- safer than silently approving.
                resolve(False)

        def _push_ask(
            self,
            question: str,
            choices: list[str],
            resolve: Callable[[str | None], None],
        ) -> None:
            """Loop 284: pop the ask_user modal."""
            try:
                self.push_screen(_AskScreen(question, choices), resolve)
            except Exception:  # noqa: BLE001
                resolve("")

        def _finalize_agent(self, prompt: str, reply: str, elapsed: float) -> None:
            try:
                stream = self.query_one("#stream", Static)
                stream.update("")
                stream.remove_class("live")
                log = self.query_one("#log", RichLog)
            except Exception:  # noqa: BLE001
                self._streaming = False
                return
            self._record_turn(prompt, reply, elapsed)
            self._post_assistant(log, reply)
            log.write(self._telemetry_line() + "  [dim](agent)[/dim]")
            self._refresh_status()
            self._streaming = False

        def _post_assistant(self, log, reply: str) -> None:
            """Write an assistant reply to the RichLog, rendering markdown
            structure when the reply looks like markdown so fenced code,
            headings, lists and bold text render with syntax highlighting.

            Plain-text replies fall through to the original prefixed write
            so short answers stay on one line.
            """
            if looks_like_markdown(reply):
                try:
                    from rich.markdown import Markdown
                except ImportError:
                    log.write(f"[green]qwen>[/green] {_safe_markup(reply)}")
                    return
                log.write("[green]qwen>[/green]")
                log.write(Markdown(reply))
            else:
                log.write(f"[green]qwen>[/green] {_safe_markup(reply)}")


        def _record_turn(self, prompt: str, reply: str, elapsed: float) -> None:
            tok = estimate_tokens(prompt) + estimate_tokens(reply)
            self.last_turn_tokens = tok
            self.last_turn_seconds = elapsed
            self.total_tokens += tok
            self.total_turns += 1

        def _telemetry_line(self) -> str:
            return (
                f"[dim]~{self.last_turn_tokens} tok  "
                f"{self.last_turn_seconds:.2f}s[/dim]"
            )

    return QwenTUI


def main(argv: list[str] | None = None) -> None:
    """Console entry point. Requires the `tui` extra installed.

    Accepts ``--help`` and ``--version`` without launching the Textual app
    so users can probe the binary in a CI shell or sanity-check that the
    extra is wired up. Any unknown flag is forwarded to argparse and
    yields the usual ``error: unrecognized arguments`` message rather than
    being swallowed by Textual.
    """
    import argparse
    from . import __version__

    parser = argparse.ArgumentParser(
        prog="qwen-coder-tui",
        description=(
            "Interactive Textual chat UI for the qwen-coder-mcp project. "
            "Connects to a local OpenAI-compatible Qwen server (default "
            "http://127.0.0.1:8000/v1) and exposes slash commands for "
            "shell, fs, grep, diff, pin, history, and more."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"qwen-coder-tui {__version__}",
    )
    parser.parse_args(argv)

    try:
        AppCls = _build_app()
    except ImportError as exc:
        print(
            f"qwen-coder-tui requires the `tui` extra: pip install 'qwen-coder-mcp[tui]'\n{exc}",
            file=sys.stderr,
        )
        sys.exit(2)
    AppCls().run()


if __name__ == "__main__":
    main()
