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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import fs_tools, prompts, web_tools
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
  /diff <a> <b>        Show a unified diff between two files in the repo
  /quit                Exit

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
        if len(cmd.args) < 2:
            return "usage: /diff <pathA> <pathB>", False
        return _render_diff(fs_cfg, cmd.args[0], cmd.args[1]), False
    return f"unknown command: /{name}  (try /help)", False


def chat_turn(
    history: list[ChatMessage],
    user_text: str,
    *,
    client: QwenClient,
    system: str = prompts.CODER_SYSTEM,
) -> str:
    """Append `user_text` to history and return the assistant reply.

    `history` is mutated in place: user message added, then assistant
    reply appended on success.
    """
    if not history or history[0].role != "system":
        history.insert(0, ChatMessage(role="system", content=system))
    history.append(ChatMessage(role="user", content=user_text))
    try:
        reply = client.chat(history)
    except Exception as exc:  # noqa: BLE001
        return f"chat error: {type(exc).__name__}: {exc}"
    history.append(ChatMessage(role="assistant", content=reply))
    return reply


def chat_turn_stream(
    history: list[ChatMessage],
    user_text: str,
    *,
    client: QwenClient,
    system: str = prompts.CODER_SYSTEM,
):
    """Streaming counterpart of `chat_turn`. Yields `(chunk, accum)` tuples
    where `accum` is the full reply assembled so far. On stream end the
    final assistant reply is appended to `history`. On error a single
    final chunk with the error message is yielded and history rolled back
    (the trailing user message stays so the user can retry, but no
    partial assistant message is committed).
    """
    if not history or history[0].role != "system":
        history.insert(0, ChatMessage(role="system", content=system))
    history.append(ChatMessage(role="user", content=user_text))
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


def _build_app(
    client_factory: Callable[[], QwenClient] | None = None,
    fs_cfg: fs_tools.FsConfig | None = None,
):
    """Construct the Textual App. Imported lazily so the `tui` extra
    is only required when running the TUI."""
    from textual.app import App, ComposeResult
    from textual.containers import Vertical
    from textual.widgets import Footer, Header, Input, RichLog

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

        def compose(self) -> ComposeResult:  # type: ignore[override]
            yield Header()
            with Vertical():
                yield RichLog(id="log", highlight=True, markup=True)
            yield Input(placeholder="message or /help", id="entry")
            yield Footer()

        def on_mount(self) -> None:  # type: ignore[override]
            log = self.query_one("#log", RichLog)
            log.write("[bold]qwen-coder-tui[/bold]  type /help")

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
                log.write(text)
                if quit_now:
                    self.exit()
                return
            reply_parts: list[str] = []
            try:
                for chunk, _accum in chat_turn_stream(
                    self.history, line, client=self.client
                ):
                    reply_parts.append(chunk)
            except AttributeError:
                # Client may not implement chat_stream -- fall back.
                reply = chat_turn(self.history, line, client=self.client)
                log.write(f"[green]qwen>[/green] {reply}")
                return
            log.write(f"[green]qwen>[/green] {''.join(reply_parts)}")

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
