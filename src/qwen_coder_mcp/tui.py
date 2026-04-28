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
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import agent_loop, fs_tools, prompts, shell_tools, web_tools
from .qwen_client import ChatMessage, QwenClient


@dataclass
class SlashCommand:
    name: str
    args: list[str] = field(default_factory=list)
    rest: str = ""


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
    "/ls",
    "/find_bugs",
    "/explain",
    "/apply",
    "/history",
    "/diff",
    "/run",
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
  /help                Show this help
  /search <query>      DuckDuckGo web search
  /fetch <url>         Fetch a URL's text body
  /read <path>         Read a file from the repo root
  /ls [path]           List a directory
  /find_bugs <path>    Qwen review for bugs
  /explain <path>      Qwen explanation of a file
  /apply               Apply the last assistant reply as a unified diff
  /history [n|clear]   Show the last N chat turns (default 10) or clear them
  /diff <a> <b>        Unified diff between two files (or /diff <path> vs HEAD)
  /run <cmd>           Run a shell command (10s timeout, deny list)
  /grep <pat> [path] [--ext]
                       Recursive regex search; --py/--md/--json filters by suffix
  /find <glob> [path]  Glob search through the repo
  /clear               Clear chat history
  /resume              Reload .agent/agent_state.json into chat history
  /checkpoints [load N|prune K]
                       List rotated agent-state snapshots; `load N` rehydrates
                       snapshot N (1-based, oldest first) into history;
                       `prune K` deletes all but the newest K snapshots
  /save <path>         Save the current chat transcript to a file
  /git <subcmd>        Read-only git status / log / diff / show / branch
  /tests [args]        Run pytest in the repo
  /tokens              Estimate total tokens in current chat history
  /lat [N]             Show the last N agent turns' timing breakdown
                       (TTFT, per-tool latencies, summary). Default N=1.
  /sysprompt [text]    Show or replace the system prompt
  /model [id]          Show or switch the served model id
  /undo                Pop the last user/assistant exchange
  /retry               Re-send the last user message
  /sysinfo             Snapshot of backend health, model, root, history
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


def _format_checkpoint_listing(snapshots: list[Path]) -> str:
    """Render rotated agent-state snapshots as a one-per-line listing.

    Lines are 1-indexed, oldest-first, and include the file's mtime in
    UTC ISO-8601 plus its size in bytes. Pure so /checkpoints tests
    don't need to spin up the App.
    """
    if not snapshots:
        return "(no rotated checkpoints found)"
    from datetime import datetime, timezone

    rows: list[str] = []
    for idx, snap in enumerate(snapshots, start=1):
        try:
            stat = snap.stat()
            mtime = datetime.fromtimestamp(
                stat.st_mtime, tz=timezone.utc
            ).strftime("%Y-%m-%d %H:%M:%SZ")
            size = stat.st_size
            rows.append(f"{idx:>3}. {mtime}  {size:>7}B  {snap.name}")
        except OSError as exc:
            rows.append(f"{idx:>3}. <stat failed: {exc}>  {snap.name}")
    return "\n".join(rows)


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


def _render_run(cfg: fs_tools.FsConfig, cmd: str) -> str:
    try:
        res = shell_tools.run_shell(cfg, cmd)
    except shell_tools.ShellError as exc:
        return f"run error: {exc}"
    return shell_tools.format_run_result(res)


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


def _render_sysinfo(    client: QwenClient,
    cfg: fs_tools.FsConfig,
    history: list[ChatMessage] | None,
) -> str:
    """Return a one-shot snapshot of backend health, model, root, and
    history token estimate. Designed for users to copy into a bug report.
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
        return HELP_TEXT, False
    if name == "quit" or name == "exit":
        return "bye", True
    if name == "tools":
        # Two registries: the always-on read tools and the opt-in writes.
        read_names = sorted(agent_loop.DEFAULT_TOOLS.keys())
        write_names = sorted(agent_loop.WRITE_TOOLS.keys())
        lines = [
            "[bold]read-only tools[/bold] (always available):",
            "  " + ", ".join(read_names),
            "[bold]write tools[/bold] (need /agent --write or /agent_write_on):",
            "  " + ", ".join(write_names),
            f"destructive (modal-gated): {', '.join(sorted(agent_loop.DESTRUCTIVE_TOOLS))}",
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
            return "usage: /run <cmd>", False
        return _render_run(fs_cfg, cmd.rest), False
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
        return f"unknown subcommand: {sub!r} (expected load|prune)", False
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
        total = sum(estimate_tokens(m.content) for m in history)
        msgs = len(history)
        return (
            f"~{total} tokens across {msgs} messages "
            f"(rough estimate, four characters per token)"
        ), False
    if name == "lat":
        # Optional N argument: number of recent turns to display.
        n = 1
        if cmd.args:
            try:
                n = int(cmd.args[0])
            except ValueError:
                return f"/lat: expected integer, got {cmd.args[0]!r}", False
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
            return format_turn_profile(single), False
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
        return _render_sysinfo(client, fs_cfg, history), False
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


def format_turn_profile(profile: TurnProfile | None) -> str:
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
        # Width of the tool-name column = longest name, capped at 20.
        width = min(20, max(len(name) for name, _ in profile.tool_calls))
        for idx, (name, lat) in enumerate(profile.tool_calls, start=1):
            lat_str = format_tool_latency(lat) if lat is not None else "(?)"
            lines.append(f"    {idx}. {name:<{width}} {lat_str}")
    else:
        lines.append("  tools (0): (no tool calls)")
    if profile.summary_text:
        lines.append(f"  summary: {profile.summary_text}")
    return "\n".join(lines)


DEFAULT_TURN_PROFILE_HISTORY = 20


def format_turn_profiles(
    profiles: list[TurnProfile], n: int = 1
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
        return format_turn_profile(None)
    if n <= 0:
        n = 1
    n = min(n, len(profiles))
    selected = profiles[-n:]
    if n == 1:
        return format_turn_profile(selected[-1])
    blocks: list[str] = []
    for offset, prof in enumerate(reversed(selected), start=1):
        blocks.append(f"=== turn -{offset} ===")
        blocks.append(format_turn_profile(prof))
    return "\n".join(blocks)


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
    "\n> ",
    "**",
    "__",
)


def looks_like_markdown(text: str) -> bool:
    """Heuristic: true when the assistant reply contains markdown structure
    that benefits from rich rendering rather than being treated as plain text.

    Used by the App layer to decide whether to wrap a reply in
    ``rich.markdown.Markdown`` before writing it to the RichLog. Kept pure
    so unit tests can exercise it without a Textual app.
    """
    if not text:
        return False
    haystack = "\n" + text
    return any(hint in haystack for hint in _MARKDOWN_HINTS)


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
    looks_connect = isinstance(exc, connect_types) or (
        "ConnectError" in name
        or "Connection refused" in text
        or "Connection reset" in text
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
    try:
        with path.open("w", encoding="utf-8") as fh:
            for msg in tail:
                fh.write(
                    json.dumps({"role": msg.role, "content": msg.content})
                    + "\n"
                )
    except OSError:
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
            # tool-calling loop. Off by default so simple turns stay
            # streamed and cheap. Toggle with /agent_on /agent_off.
            self.agent_default: bool = False
            # Write mode adds fs_write + apply_patch to the agent's tool
            # registry. Off by default since these mutate the workspace.
            self.agent_write_default: bool = False
            # When True, every destructive tool call pops a y/n modal
            # before firing. When False, calls are auto-approved (still
            # logged via the audit hook). Default is to ask.
            self.agent_confirm_writes: bool = True

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
                msg = f"[red]save failed:[/red] {type(exc).__name__}: {exc}"
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
            line = (
                f"{prefix}{model}  ·  {msgs} msg  ·  ~{ttok} tok total  ·  "
                f"last turn ~{self.last_turn_tokens} tok in "
                f"{self.last_turn_seconds:.1f}s"
            )
            status.update(line)

        def _render_health_banner(self, log) -> None:  # type: ignore[no-untyped-def]
            """Probe the backend and write a status banner."""
            try:
                check = self.client.health_check()
            except Exception as exc:  # noqa: BLE001
                log.write(
                    f"[red]✗ health check raised:[/red] "
                    f"{type(exc).__name__}: {exc}"
                )
                return
            if check.get("ok"):
                models = check.get("models") or []
                tag = ", ".join(models[:3]) or "(no models reported)"
                log.write(f"[green]✓ backend ok[/green]  models: {tag}")
                return
            err = check.get("error") or "unknown error"
            hint = check.get("hint")
            log.write(f"[red]✗ backend unavailable:[/red] {err}")
            if hint:
                log.write(f"[yellow]→ hint:[/yellow] {hint}")

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
            log.write(f"[bold cyan]you›[/bold cyan] {line}")
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
                    log.write(f"[yellow](retrying)[/yellow] {line}")
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
                    log.write(f"[dim](cwd)[/dim] {new_root}")
                    self._refresh_status()
                    return
                else:
                    log.write(text)
                    self._refresh_status()
                    if quit_now:
                        self.exit()
                    return
            self._streaming = True
            if self.agent_default:
                self._start_agent_turn(line)
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
                log.write(f"[yellow]⚠ resume failed: {exc}[/yellow]")
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
            write: bool = False,
            max_steps: int | None = None,
        ) -> None:
            """Run an agentic tool-calling turn in a worker thread.

            Reuses the streaming Static widget for live status of which
            tool is firing; final answer is rendered via _post_assistant
            once the loop ends. ``write=True`` exposes fs_write +
            apply_patch tools, allowing the agent to edit the workspace.
            ``max_steps`` overrides the default 6-step cap (1..50).
            """
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
                    f"[yellow]✎ write[/yellow] {call.name} {summary}",
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
                        f"[yellow]⚠ checkpoint failed at step {step}: {exc}[/yellow]",
                    )

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
                                f"[cyan]→ tool[/cyan] {ev.tool}{args_repr}",
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
                                f"[green]← {ev.tool}[/green]{suffix} {head}",
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
                                f"[dim]· {ev.text}[/dim]",
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
                    final_text = f"[agent error: {type(exc).__name__}: {exc}]"
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
                self.query_one("#log", RichLog).write(line)
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
                    log.write(f"[green]qwen>[/green] {reply}")
                    return
                log.write("[green]qwen>[/green]")
                log.write(Markdown(reply))
            else:
                log.write(f"[green]qwen>[/green] {reply}")


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
