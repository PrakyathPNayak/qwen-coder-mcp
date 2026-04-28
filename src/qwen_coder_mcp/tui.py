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
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import fs_tools, prompts, shell_tools, web_tools
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
    "/save",
    "/git",
    "/tests",
    "/tokens",
    "/sysprompt",
    "/model",
    "/undo",
    "/retry",
    "/sysinfo",
    "/export",
    "/quit",
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
  /history [n]         Show the last N chat turns (default 10)
  /diff <a> <b>        Unified diff between two files (or /diff <path> vs HEAD)
  /run <cmd>           Run a shell command (10s timeout, deny list)
  /grep <pat> [path]   Recursive regex search through the repo
  /find <glob> [path]  Glob search through the repo
  /clear               Clear chat history
  /save <path>         Save the current chat transcript to a file
  /git <subcmd>        Read-only git status / log / diff / show / branch
  /tests [args]        Run pytest in the repo
  /tokens              Estimate total tokens in current chat history
  /sysprompt [text]    Show or replace the system prompt
  /model [id]          Show or switch the served model id
  /undo                Pop the last user/assistant exchange
  /retry               Re-send the last user message
  /sysinfo             Snapshot of backend health, model, root, history
  /export <path>       Export full chat as Markdown
  /quit                Exit

@<path> tokens in plain chat are expanded inline as file contents.
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
        health_line = (
            f"backend unavailable: {check.get('error') or 'unknown'}"
        )
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


def _render_grep(
    cfg: fs_tools.FsConfig, pattern: str, path: str = "."
) -> str:
    try:
        hits = shell_tools.grep(cfg, pattern, path=path)
    except shell_tools.ShellError as exc:
        return f"grep error: {exc}"
    except fs_tools.FsError as exc:
        return f"grep error: {exc}"
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
_AT_FILE_RE = __import__("re").compile(r"@([\w./\-]+)")


def expand_at_mentions(
    cfg: fs_tools.FsConfig,
    text: str,
    *,
    max_files: int = 5,
    max_bytes_each: int = 8000,
) -> str:
    """Replace `@path` mentions in `text` with inline file contents.

    Recognises tokens of the form `@<path>` where path is a sequence of
    word characters, dots, slashes, and hyphens. Each resolved file is
    appended below the original text under a `# <path>` heading,
    truncated to `max_bytes_each` characters. Up to `max_files`
    expansions per call. Files that cannot be read (sandbox escape,
    binary, missing, oversize) are silently skipped so a typo does not
    block the user's actual prompt -- the original `@token` stays as
    a literal in the prompt and the model can ask about it.
    """
    if "@" not in text:
        return text
    seen: list[str] = []
    for m in _AT_FILE_RE.finditer(text):
        token = m.group(1)
        if token in seen:
            continue
        seen.append(token)
        if len(seen) >= max_files:
            break
    appended: list[str] = []
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
    return text + "\n\n--- attached files ---" + "".join(appended)


def dispatch_slash(
    cmd: SlashCommand,
    *,
    client: QwenClient,
    fs_cfg: fs_tools.FsConfig,
    history: list[ChatMessage] | None = None,
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
    if name == "search":
        if not cmd.rest:
            return "usage: /search <query>", False
        return _render_search(cmd.rest), False
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
        n = 10
        if cmd.args:
            try:
                n = max(1, int(cmd.args[0]))
            except ValueError:
                return "usage: /history [n]", False
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
            return "usage: /grep <pattern> [path]", False
        path = cmd.args[1] if len(cmd.args) >= 2 else "."
        return _render_grep(fs_cfg, cmd.args[0], path), False
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
    from textual.widgets import Footer, Header, Input, RichLog
    try:
        from textual.suggester import SuggestFromList  # type: ignore
        _suggester: object | None = SuggestFromList(SLASH_COMMANDS, case_sensitive=False)
    except ImportError:
        _suggester = None

    cfg = fs_cfg or _default_fs_cfg()
    factory = client_factory or QwenClient

    class QwenTUI(App):  # type: ignore[misc]
        CSS = """
        Screen { layout: vertical; }
        RichLog { height: 1fr; border: round $accent; }
        Input { dock: bottom; }
        """
        BINDINGS = [("ctrl+c", "quit", "Quit")]

        def __init__(self) -> None:
            super().__init__()
            self.client = factory()
            self.history: list[ChatMessage] = []
            self.fs_cfg = cfg
            self.last_turn_tokens: int = 0
            self.last_turn_seconds: float = 0.0
            self.total_tokens: int = 0
            self.total_turns: int = 0

        def compose(self) -> ComposeResult:  # type: ignore[override]
            yield Header()
            with Vertical():
                yield RichLog(id="log", highlight=True, markup=True)
            if _suggester is not None:
                yield Input(
                    placeholder="message or /help",
                    id="entry",
                    suggester=_suggester,  # type: ignore[arg-type]
                )
            else:
                yield Input(placeholder="message or /help", id="entry")
            yield Footer()

        def on_mount(self) -> None:  # type: ignore[override]
            log = self.query_one("#log", RichLog)
            log.write("[bold]qwen-coder-tui[/bold]  type /help")
            self._render_health_banner(log)
            # Restore prior chat from disk so the user can continue
            # conversations across restarts. Failures are silent.
            try:
                prior = load_history_jsonl(history_file_path(self.fs_cfg))
            except Exception:  # noqa: BLE001
                prior = []
            if prior:
                self.history.extend(prior)
                log.write(f"[dim](restored {len(prior)} prior messages)[/dim]")

        def on_unmount(self) -> None:  # type: ignore[override]
            try:
                save_history_jsonl(
                    self.history, history_file_path(self.fs_cfg)
                )
            except Exception:  # noqa: BLE001
                pass

        def _render_health_banner(self, log) -> None:  # type: ignore[no-untyped-def]
            """Probe the backend and write a status banner.

            Catches all exceptions so a missing httpx, missing settings,
            or any other startup problem cannot crash the App before
            the user has typed anything.
            """
            try:
                check = self.client.health_check()
            except Exception as exc:  # noqa: BLE001
                log.write(
                    f"[red]health check raised:[/red] "
                    f"{type(exc).__name__}: {exc}"
                )
                return
            if check.get("ok"):
                models = check.get("models") or []
                tag = ", ".join(models[:3]) or "(no models reported)"
                log.write(f"[green]backend ok[/green]  models: {tag}")
                return
            err = check.get("error") or "unknown error"
            hint = check.get("hint")
            log.write(f"[red]backend unavailable:[/red] {err}")
            if hint:
                log.write(f"[yellow]hint:[/yellow] {hint}")

        def on_input_submitted(self, event: Input.Submitted) -> None:  # type: ignore[override]
            line = event.value.strip()
            if not line:
                return
            entry = self.query_one("#entry", Input)
            entry.value = ""
            log = self.query_one("#log", RichLog)
            log.write(f"[cyan]you>[/cyan] {line}")
            cmd = parse_slash(line)
            if cmd is not None:
                text, quit_now = dispatch_slash(
                    cmd,
                    client=self.client,
                    fs_cfg=self.fs_cfg,
                    history=self.history,
                )
                if isinstance(text, str) and text.startswith("__RETRY__"):
                    # Retry sentinel: the dispatcher already stripped the
                    # last turn off history; replay the prompt as if the
                    # user typed it again.
                    line = text[len("__RETRY__"):]
                    log.write(f"[yellow](retrying)[/yellow] {line}")
                else:
                    log.write(text)
                    if quit_now:
                        self.exit()
                    return
            reply_parts: list[str] = []
            t0 = time.monotonic()
            try:
                for chunk, _accum in chat_turn_stream(
                    self.history, line, client=self.client, fs_cfg=self.fs_cfg
                ):
                    reply_parts.append(chunk)
            except AttributeError:
                # Client may not implement chat_stream -- fall back.
                reply = chat_turn(
                    self.history, line, client=self.client, fs_cfg=self.fs_cfg
                )
                self._record_turn(line, reply, time.monotonic() - t0)
                log.write(f"[green]qwen>[/green] {reply}")
                log.write(self._telemetry_line())
                return
            full_reply = "".join(reply_parts)
            self._record_turn(line, full_reply, time.monotonic() - t0)
            log.write(f"[green]qwen>[/green] {full_reply}")
            log.write(self._telemetry_line())

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


def main() -> None:
    """Console entry point. Requires the `tui` extra installed."""
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
