"""Tool-calling agent loop.

The model emits structured tool calls in its reply; this module parses
them, executes them against a sandboxed registry, and feeds the results
back as a follow-up user message so the model can incorporate them. The
loop terminates when the model produces a reply with no tool calls or
when ``max_steps`` is exhausted.

Design notes:
- Protocol is intentionally text-based (XML-ish tags wrapping JSON) so
  it works with any vLLM-served chat model regardless of whether the
  server advertises function-calling. Raw JSON inside ``<tool_call>``
  tags is robust against the model emitting prose around the call.
- All tools route through the same FsConfig sandbox the rest of the
  TUI uses; web tools are bounded by ``web_tools.fetch_url``'s own
  size cap. Nothing here ever shells out to user-controlled commands.
- This module has zero Textual dependency and zero direct httpx
  dependency: it only takes a ``QwenClient``-shaped object and a
  ``FsConfig``. Tests inject fakes for both.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from . import fs_tools, shell_tools, web_tools
from .qwen_client import ChatMessage, QwenClient

TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL
)
# Some smaller / instruction-tuned models prefer ```tool_call fenced blocks.
TOOL_CALL_FENCE_RE = re.compile(
    r"```tool_call\s*\n?(\{.*?\})\s*\n?```", re.DOTALL
)

# Models routinely guess synonyms instead of the exact tool name (e.g.
# "run_command" / "bash" instead of "run_shell"). Rather than fail the
# call with "unknown tool", we normalise common aliases so the dispatch
# still works. Keep this conservative -- only obvious 1:1 synonyms.
TOOL_NAME_ALIASES: dict[str, str] = {
    "run_command": "run_shell",
    "shell": "run_shell",
    "bash": "run_shell",
    "sh": "run_shell",
    "exec": "run_shell",
    "read_file": "fs_read",
    "write_file": "fs_write",
    "edit_file": "fs_edit",
    "insert_file": "fs_insert",
    "list_dir": "fs_list",
    "ls": "fs_list",
    "search": "grep",
    "rg": "grep",
    "glob": "find",
    "fs_edit_regex": "fs_regex_edit",
    "regex_edit": "fs_regex_edit",
    "edit_regex": "fs_regex_edit",
}


def _canonical_tool_name(name: str) -> str:
    """Return the canonical tool name after applying alias normalisation
    (case-insensitive). Unknown names pass through unchanged so the
    dispatcher can still emit its "unknown tool" error.
    """
    if not isinstance(name, str):
        return name
    return TOOL_NAME_ALIASES.get(name.strip().lower(), name)


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]
    raw: str


@dataclass
class ToolResult:
    name: str
    output: str
    error: bool = False


@dataclass
class AgentEvent:
    """Yielded by ``run_agent`` so the UI can render progress live.

    ``latency_s`` is populated on ``tool_result`` events with the wall-
    clock elapsed time between the matching ``tool_call`` event and the
    result landing. ``None`` on every other event kind. Consumers can
    treat it as advisory — it's monotonic-clock-based so it's safe to
    sum, but a process suspended mid-call will report inflated values.
    """
    kind: str  # "assistant" | "tool_call" | "tool_result" | "limit" | "final" | "chunk"
    text: str = ""
    tool: str = ""
    args: dict[str, Any] | None = None
    latency_s: float | None = None


# ------------------------------------------------------------- tool registry
ToolFn = Callable[[dict[str, Any], fs_tools.FsConfig], str]


def _tool_web_search(args: dict[str, Any], _cfg: fs_tools.FsConfig) -> str:
    query = str(args.get("query", "")).strip()
    if not query:
        return "error: web_search needs a 'query' arg"
    n = int(args.get("max_results", 5))
    n = max(1, min(20, n))
    results = web_tools.web_search(query, max_results=n)
    return web_tools.format_search_results(results) or "(no results)"


def _tool_web_fetch(args: dict[str, Any], _cfg: fs_tools.FsConfig) -> str:
    url = str(args.get("url", "")).strip()
    if not url:
        return "error: web_fetch needs a 'url' arg"
    res = web_tools.fetch_url(url)
    if isinstance(res, dict) and res.get("error") == "non_text_content":
        return f"refused non-text: {res.get('content_type')}"
    body = str(res.get("text", "") if isinstance(res, dict) else res)
    cap = int(args.get("max_bytes", 8000))
    cap = max(256, min(64000, cap))
    if len(body) > cap:
        body = body[:cap] + "\n... [truncated]"
    status = res.get("status") if isinstance(res, dict) else "?"
    return f"# {url} (status={status})\n{body}"


def _tool_fs_read(args: dict[str, Any], cfg: fs_tools.FsConfig) -> str:
    path = str(args.get("path", "")).strip()
    if not path:
        return "error: fs_read needs a 'path' arg"
    # Loop 252: optional line-range / line-numbered reads. The model
    # can ask for a tiny slice of a 100k-line file without burning
    # context on the rest. Numeric args are coerced defensively so a
    # stringified "10" from a JSON-tool-call still works.
    def _maybe_int(key: str) -> int | None:
        if key not in args or args[key] is None:
            return None
        try:
            return int(args[key])
        except (TypeError, ValueError):
            return None

    start_line = _maybe_int("start_line")
    end_line = _maybe_int("end_line")
    max_lines = _maybe_int("max_lines")
    line_numbers = bool(args.get("line_numbers", False))
    # Loop 256: regex-based slicing. Composes with start_line/end_line
    # to restrict the search window.
    pattern = args.get("pattern")
    if pattern is not None and not isinstance(pattern, str):
        return "error: fs_read 'pattern' must be a string"
    before = _maybe_int("before") or 0
    after = _maybe_int("after") or 0
    max_matches = _maybe_int("max_matches")
    ignore_case = bool(args.get("ignore_case", False))
    res = fs_tools.read_file(
        cfg,
        path,
        start_line=start_line,
        end_line=end_line,
        max_lines=max_lines,
        line_numbers=line_numbers,
        pattern=pattern,
        before=before,
        after=after,
        max_matches=max_matches,
        ignore_case=ignore_case,
    )
    text = str(res.get("text", ""))
    cap = int(args.get("max_bytes", 16000))
    if len(text) > cap:
        text = text[:cap] + "\n... [truncated]"
    rng = res.get("range")
    match_lines = res.get("match_lines")
    if match_lines is not None:
        total = res.get("total_lines", "?")
        n = len(match_lines) if isinstance(match_lines, list) else 0
        header = (
            f"# {path} pattern={pattern!r} matches={n} "
            f"of {total} lines (before={before}, after={after})\n"
        )
        if n == 0:
            return header + "(no matches)"
        return header + text
    if rng:
        total = res.get("total_lines", "?")
        header = f"# {path} lines {rng['start']}-{rng['end']} of {total}\n"
        return header + text
    return text


def _tool_fs_edit(args: dict[str, Any], cfg: fs_tools.FsConfig) -> str:
    """Surgical str-replace edit (loop 252; dry_run added loop 253)."""
    path = str(args.get("path", "")).strip()
    if not path:
        return "error: fs_edit needs a 'path' arg"
    old = args.get("old", "")
    new = args.get("new", "")
    if not isinstance(old, str) or not isinstance(new, str):
        return "error: fs_edit needs string 'old' and 'new' args"
    count_arg = args.get("count", 1)
    if count_arg is None:
        count: int | None = None
    else:
        try:
            count = int(count_arg)
        except (TypeError, ValueError):
            count = 1
    dry_run = bool(args.get("dry_run", False))
    res = fs_tools.edit_file(cfg, path, old, new, count=count, dry_run=dry_run)
    mode = "dry-run" if res.get("dry_run") else "edited"
    return (
        f"{mode} {res.get('path')}: {res.get('replacements')} "
        f"replacement(s), size {res.get('before_size')} -> {res.get('size')} bytes"
    )


def _tool_fs_regex_edit(args: dict[str, Any], cfg: fs_tools.FsConfig) -> str:
    """Whitespace-tolerant str-replace edit (loop 267).

    Like fs_edit but every run of whitespace in ``old`` matches any
    run of whitespace in the file, so the model's "right code,
    slightly different indent or newline" emissions still apply.
    Pass ``raw_regex=true`` to treat ``old`` as a literal regex
    (advanced).
    """
    path = str(args.get("path", "")).strip()
    if not path:
        return "error: fs_regex_edit needs a 'path' arg"
    old = args.get("old", "")
    new = args.get("new", "")
    if not isinstance(old, str) or not isinstance(new, str):
        return "error: fs_regex_edit needs string 'old' and 'new' args"
    count_arg = args.get("count", 1)
    if count_arg is None:
        count: int | None = None
    else:
        try:
            count = int(count_arg)
        except (TypeError, ValueError):
            count = 1
    dry_run = bool(args.get("dry_run", False))
    raw_regex = bool(args.get("raw_regex", False))
    res = fs_tools.regex_edit_file(
        cfg, path, old, new, count=count, dry_run=dry_run, raw_regex=raw_regex
    )
    mode = "dry-run" if res.get("dry_run") else "edited"
    return (
        f"{mode} {res.get('path')} (regex): {res.get('replacements')} "
        f"replacement(s), size {res.get('before_size')} -> {res.get('size')} bytes"
    )


def _tool_fs_insert(args: dict[str, Any], cfg: fs_tools.FsConfig) -> str:
    """Insert content at a specific line position (loop 252)."""
    path = str(args.get("path", "")).strip()
    if not path:
        return "error: fs_insert needs a 'path' arg"
    content = args.get("content", "")
    if not isinstance(content, str):
        content = str(content)

    def _maybe_int(key: str) -> int | None:
        if key not in args or args[key] is None:
            return None
        try:
            return int(args[key])
        except (TypeError, ValueError):
            return None

    after = _maybe_int("after_line")
    before = _maybe_int("before_line")
    res = fs_tools.insert_lines(
        cfg, path, after_line=after, before_line=before, content=content
    )
    return (
        f"inserted into {res.get('path')} at index {res.get('inserted_at')}, "
        f"size {res.get('before_size')} -> {res.get('size')} bytes"
    )


def _tool_fs_list(args: dict[str, Any], cfg: fs_tools.FsConfig) -> str:
    path = str(args.get("path", ".")).strip() or "."
    res = fs_tools.list_dir(cfg, path)
    entries = res.get("entries", []) if isinstance(res, dict) else []
    if not entries:
        return "(empty)"
    lines = []
    for e in entries:
        kind = e.get("type", "?")
        name = e.get("name", "?")
        size = e.get("size", "")
        lines.append(f"{kind}\t{size}\t{name}")
    return "\n".join(lines)


def _tool_grep(args: dict[str, Any], cfg: fs_tools.FsConfig) -> str:
    pattern = str(args.get("pattern", ""))
    if not pattern:
        return "error: grep needs a 'pattern' arg"
    path = str(args.get("path", ".")) or "."
    hits = shell_tools.grep(cfg, pattern, path=path)
    suffix = args.get("ext")
    if suffix:
        suf = "." + str(suffix).lstrip(".")
        hits = [h for h in hits if h.path.endswith(suf)]
    return shell_tools.format_grep(hits) or "(no matches)"


def _tool_find(args: dict[str, Any], cfg: fs_tools.FsConfig) -> str:
    glob = str(args.get("glob", ""))
    if not glob:
        return "error: find needs a 'glob' arg"
    path = str(args.get("path", ".")) or "."
    hits = shell_tools.find(cfg, glob, path=path)
    return shell_tools.format_find(hits) or "(no matches)"


def _tool_fs_write(args: dict[str, Any], cfg: fs_tools.FsConfig) -> str:
    path = str(args.get("path", "")).strip()
    if not path:
        return "error: fs_write needs a 'path' arg"
    content = args.get("content", "")
    if not isinstance(content, str):
        content = str(content)
    create_parents = bool(args.get("create_parents", False))
    res = fs_tools.write_file(
        cfg, path, content, create_parents=create_parents
    )
    return f"wrote {res.get('size')} bytes to {res.get('path')}"


def _tool_apply_patch(args: dict[str, Any], cfg: fs_tools.FsConfig) -> str:
    diff = args.get("diff", "")
    if not isinstance(diff, str) or not diff.strip():
        return "error: apply_patch needs a 'diff' arg (unified diff text)"
    check_only = bool(args.get("check_only", False))
    res = fs_tools.apply_patch(cfg, diff, check_only=check_only)
    if isinstance(res, dict):
        ok = res.get("ok", False)
        msg = res.get("message", "")
        prefix = "ok" if ok else "FAILED"
        mode = "check" if check_only else "applied"
        return f"{prefix} ({mode}): {msg}"
    return str(res)


def _tool_run_shell(args: dict[str, Any], cfg: fs_tools.FsConfig) -> str:
    cmd = args.get("cmd") or args.get("command") or ""
    if not isinstance(cmd, str) or not cmd.strip():
        return "error: run_shell needs a 'cmd' arg"
    timeout = args.get("timeout", 30.0)
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        timeout = 30.0
    cwd = args.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        cwd = None
    try:
        res = shell_tools.run_shell(cfg, cmd, timeout=timeout, cwd=cwd)
    except shell_tools.ShellError as exc:
        return f"denied: {exc}"
    return shell_tools.format_run_result(res)


DEFAULT_TOOLS: dict[str, ToolFn] = {
    "web_search": _tool_web_search,
    "web_fetch": _tool_web_fetch,
    "fs_read": _tool_fs_read,
    "fs_list": _tool_fs_list,
    "grep": _tool_grep,
    "find": _tool_find,
}

# Write tools are opt-in -- they can mutate the workspace, so the
# caller has to explicitly enable them via ``run_agent(..., write=True)``
# or pass ``tools=ALL_TOOLS``. The default registry is read-only.
WRITE_TOOLS: dict[str, ToolFn] = {
    "fs_write": _tool_fs_write,
    "fs_edit": _tool_fs_edit,
    "fs_regex_edit": _tool_fs_regex_edit,
    "fs_insert": _tool_fs_insert,
    "apply_patch": _tool_apply_patch,
    "run_shell": _tool_run_shell,
}

ALL_TOOLS: dict[str, ToolFn] = {**DEFAULT_TOOLS, **WRITE_TOOLS}

# Tools that mutate the workspace -- the loop driver routes calls to
# these through an optional confirmation hook so a TUI/CLI can prompt
# the user before the write actually happens.
DESTRUCTIVE_TOOLS: frozenset[str] = frozenset(WRITE_TOOLS.keys())


# ---------------------------------------------------- memory tools (loop 246)
# Tools that let the model self-manage the persistent TaskMemory
# (loop 244). They are *additive*: when the QwenClient has a
# ``task_memory`` attached, ``run_agent`` merges these into the
# active registry so the model can keep its own context across
# iterations and process restarts. Each tool is a closure over the
# bound TaskMemory so the existing ToolFn signature (args, fs_cfg)
# is preserved -- callers don't have to thread state through.

def build_memory_tools(memory: Any) -> dict[str, ToolFn]:
    """Return a dict of memory-management tools bound to ``memory``.

    The model uses these to persist task state across turns so it can
    survive context compression (loop 240/243) and process restarts
    (loop 244 already auto-injects on read; these add the *write* path).

    Returned tools (all best-effort, never raise into the loop):
      * ``set_current_task(description: str)``
      * ``add_todo(id: str, description: str, status: str="open")``
      * ``update_todo(id: str, status: str)``
      * ``complete_todo(id: str)`` -- shorthand for status="done"
      * ``remove_todo(id: str)``
      * ``record_fact(key: str, value: str)``
      * ``record_decision(text: str)``
      * ``recall_state()`` -- returns a JSON snapshot
    """
    if memory is None:
        return {}

    def _need(args: dict[str, Any], *keys: str) -> str | None:
        for k in keys:
            v = args.get(k)
            if v is None or (isinstance(v, str) and not v.strip()):
                return f"error: missing required arg {k!r}"
        return None

    def _set_current_task(args: dict[str, Any], _cfg: fs_tools.FsConfig) -> str:
        err = _need(args, "description")
        if err:
            return err
        memory.set_current_task(str(args["description"]).strip())
        return f"ok: current_task set to {args['description']!s}"

    def _add_todo(args: dict[str, Any], _cfg: fs_tools.FsConfig) -> str:
        err = _need(args, "id", "description")
        if err:
            return err
        status = str(args.get("status") or "open").strip() or "open"
        memory.add_todo(str(args["id"]).strip(), str(args["description"]).strip(), status=status)
        return f"ok: todo added: {args['id']} ({status})"

    def _update_todo(args: dict[str, Any], _cfg: fs_tools.FsConfig) -> str:
        err = _need(args, "id", "status")
        if err:
            return err
        ok = memory.update_todo_status(str(args["id"]).strip(), str(args["status"]).strip())
        if not ok:
            return f"error: no such todo: {args['id']}"
        return f"ok: todo {args['id']} -> {args['status']}"

    def _complete_todo(args: dict[str, Any], _cfg: fs_tools.FsConfig) -> str:
        err = _need(args, "id")
        if err:
            return err
        ok = memory.update_todo_status(str(args["id"]).strip(), "done")
        if not ok:
            return f"error: no such todo: {args['id']}"
        return f"ok: todo {args['id']} -> done"

    def _remove_todo(args: dict[str, Any], _cfg: fs_tools.FsConfig) -> str:
        err = _need(args, "id")
        if err:
            return err
        ok = memory.remove_todo(str(args["id"]).strip())
        if not ok:
            return f"error: no such todo: {args['id']}"
        return f"ok: todo removed: {args['id']}"

    def _record_fact(args: dict[str, Any], _cfg: fs_tools.FsConfig) -> str:
        err = _need(args, "key", "value")
        if err:
            return err
        memory.record_fact(str(args["key"]).strip(), str(args["value"]).strip())
        return f"ok: fact recorded: {args['key']}"

    def _record_decision(args: dict[str, Any], _cfg: fs_tools.FsConfig) -> str:
        err = _need(args, "text")
        if err:
            return err
        memory.record_decision(str(args["text"]).strip())
        return f"ok: decision recorded"

    def _recall_state(_args: dict[str, Any], _cfg: fs_tools.FsConfig) -> str:
        import json as _json
        return _json.dumps(memory.snapshot(), indent=2, sort_keys=True)

    return {
        "set_current_task": _set_current_task,
        "add_todo": _add_todo,
        "update_todo": _update_todo,
        "complete_todo": _complete_todo,
        "remove_todo": _remove_todo,
        "record_fact": _record_fact,
        "record_decision": _record_decision,
        "recall_state": _recall_state,
    }


MEMORY_TOOL_NAMES: frozenset[str] = frozenset({
    "set_current_task",
    "add_todo",
    "update_todo",
    "complete_todo",
    "remove_todo",
    "record_fact",
    "record_decision",
    "recall_state",
})


MEMORY_TOOL_PROTOCOL_DOC = """\
Memory tools (use to persist state across turns -- survives context
compression and process restarts):
- set_current_task(description: str) -- replace the current task line
- add_todo(id: str, description: str, status: str="open") -- track work
- update_todo(id: str, status: str) -- statuses: open, in_progress, done, blocked
- complete_todo(id: str) -- shorthand for status="done"
- remove_todo(id: str)
- record_fact(key: str, value: str) -- pin a key->value fact
- record_decision(text: str) -- append a free-form decision entry
- recall_state() -- return the full memory snapshot as JSON

Use these proactively. When the user gives you a task, call
set_current_task. When you discover a sub-task, add_todo it. When you
finish one, complete_todo it. The next turn starts with this state
already in your system prompt -- so you literally cannot forget.
"""


ConfirmFn = Callable[["ToolCall"], bool]


def always_allow(call: "ToolCall") -> bool:  # noqa: ARG001
    """Default confirm hook: every tool runs without prompting. Suitable
    for non-interactive use and unit tests."""
    return True


def always_deny(call: "ToolCall") -> bool:  # noqa: ARG001
    """Confirm hook: refuse every destructive call. Useful as a safe
    default when the host process can't surface a user prompt (e.g. the
    MCP server before a client wires up the approval flow)."""
    return False


def make_sticky_confirm(
    inner: ConfirmFn,
    *,
    sticky_per_tool: bool = True,
) -> ConfirmFn:
    """Wrap ``inner`` so that once the user approves a particular tool,
    further calls of THAT tool in the same turn skip the prompt.

    Mirrors the Copilot CLI / VS Code pattern where a single "Always
    allow run_shell" decision applies for the rest of the session
    instead of pestering the user on every command.

    Set ``sticky_per_tool=False`` to scope stickiness per (name, args)
    pair instead -- only repeats of the *same exact call* are skipped,
    which is safer but more pestering.
    """
    granted: set[Any] = set()

    def _wrapped(call: "ToolCall") -> bool:
        if sticky_per_tool:
            key: Any = call.name
        else:
            try:
                key = (call.name, json.dumps(call.args, sort_keys=True))
            except Exception:  # noqa: BLE001
                key = (call.name, str(call.args))
        if key in granted:
            return True
        ok = bool(inner(call))
        if ok:
            granted.add(key)
        return ok

    return _wrapped


TOOL_BLURBS: dict[str, str] = {
    "web_search": "- web_search(query: str, max_results: int=5) -- DuckDuckGo web search",
    "web_fetch": "- web_fetch(url: str, max_bytes: int=8000) -- fetch a URL's text body",
    "fs_read": (
        "- fs_read(path: str, start_line: int|None=None, end_line: int|None=None,\n"
        "  max_lines: int|None=None, line_numbers: bool=false, pattern: str|None=None,\n"
        "  before: int=0, after: int=0, max_matches: int|None=None,\n"
        "  ignore_case: bool=false, max_bytes: int=16000)\n"
        "  -- read a file (or a 1-based inclusive line range; negative indices\n"
        "  count from the end; pass line_numbers=true to get \"<n> | \" prefixes\n"
        "  so subsequent fs_edit calls can quote exact context). When `pattern`\n"
        "  is supplied, only lines matching that regex are returned, padded by\n"
        "  `before`/`after` context lines (grep -A/-B style). Non-contiguous\n"
        "  matches are separated by \"--\" lines and line numbers are always\n"
        "  emitted. Use this for navigating huge files without slurping them."
    ),
    "fs_list": "- fs_list(path: str=\".\") -- list a workspace directory",
    "grep": "- grep(pattern: str, path: str=\".\", ext: str|None=None) -- regex search",
    "find": "- find(glob: str, path: str=\".\") -- glob search",
    "fs_write": (
        "- fs_write(path: str, content: str, create_parents: bool=False) -- write a\n"
        "  whole file. Prefer fs_edit for surgical changes."
    ),
    "fs_edit": (
        "- fs_edit(path: str, old: str, new: str, count: int|null=1,\n"
        "  dry_run: bool=false) -- surgical\n"
        "  string-replace in an existing file. count=1 enforces\n"
        "  a unique match (safest); count=null replaces every occurrence; any\n"
        "  other integer requires that exact occurrence count. dry_run=true\n"
        "  validates the match WITHOUT mutating, so you can preview before\n"
        "  committing. If 'old' is not uniquely matched the call fails with a\n"
        "  helpful error so you can re-read with more surrounding context and retry."
    ),
    "fs_regex_edit": (
        "- fs_regex_edit(path: str, old: str, new: str, count: int|null=1,\n"
        "  dry_run: bool=false, raw_regex: bool=false) -- whitespace-tolerant\n"
        "  variant of fs_edit. Every run of whitespace in 'old'\n"
        "  matches any run of whitespace in the file, so slight indentation or\n"
        "  newline differences don't break the edit. Prefer this when fs_edit\n"
        "  fails with \"old not found\" for code that looks textually correct.\n"
        "  Set raw_regex=true to treat 'old' as a literal Python regex (advanced)."
    ),
    "fs_insert": (
        "- fs_insert(path: str, content: str, after_line: int|None=None,\n"
        "  before_line: int|None=None) -- insert content at a specific 1-based\n"
        "  line position. Exactly one of after_line/before_line must be provided."
    ),
    "apply_patch": (
        "- apply_patch(diff: str, check_only: bool=False) -- apply a unified diff\n"
        "  via git apply (prefer check_only=true first)"
    ),
    "run_shell": (
        "- run_shell(cmd: str, timeout: float=30, cwd: str|None=None) -- run a\n"
        "  shell command inside the workspace sandbox. The command is screened\n"
        "  against a denylist; rm/mv/curl/etc are blocked by default. The user\n"
        "  is asked to approve each command before it runs (Copilot-style)."
    ),
}

TOOL_PROTOCOL_HEADER = """\
You have access to the following tools. To use one, emit a tool_call
block in your reply. The runtime will execute it and feed the result
back as a follow-up user message; you may then call more tools or
produce your final answer.

Format (case-sensitive, JSON inside the tags):
<tool_call>
{"name": "<tool_name>", "args": {"<arg>": "<value>"}}
</tool_call>

Multiple tool_call blocks per reply are allowed and run in order.
A reply with NO tool_call block is treated as your final answer.

Available tools:
"""

TOOL_PROTOCOL_FOOTER = """\

Rules:
- Use tools when you need information you don't already have. Do not
  guess web facts; web_search them.
- Keep arguments minimal -- the runtime caps body sizes for you.
- After tool results land, ALWAYS produce a final answer that
  synthesizes them. Do not loop forever calling tools.
- If you have enough information, just answer directly.
"""


def build_tool_protocol_doc(tools: dict[str, ToolFn] | None) -> str:
    """Assemble the tool-protocol blurb listing ONLY the tools that are
    actually registered for the current turn.

    Loop 272: previously the protocol doc was a static string listing
    every tool the codebase knows about, which lied to the model when
    the caller passed a read-only registry (the model would try to use
    fs_write and get "unknown tool" errors). Now we render only what
    the dispatcher will actually accept. Unknown registry entries fall
    back to a minimal stub line so even custom tools added at runtime
    appear in the prompt.
    """
    if tools is None:
        tools = DEFAULT_TOOLS
    lines: list[str] = []
    for name in tools:
        blurb = TOOL_BLURBS.get(name)
        if blurb is None:
            blurb = f"- {name}(...) -- (custom tool registered at runtime)"
        lines.append(blurb)
    return TOOL_PROTOCOL_HEADER + "\n".join(lines) + TOOL_PROTOCOL_FOOTER


# Backward-compat alias: old tests and external callers reference
# TOOL_PROTOCOL_DOC. Keep it as the full-registry doc so anything that
# greps for tool names still finds them.
TOOL_PROTOCOL_DOC = build_tool_protocol_doc(ALL_TOOLS)


# ------------------------------------------------------------- parser
def parse_tool_calls(text: str) -> list[ToolCall]:
    """Extract tool calls from an assistant reply.

    Recognises both ``<tool_call>...</tool_call>`` and triple-backtick
    fenced ``tool_call`` blocks. Malformed JSON is silently dropped --
    we'd rather the model see "no tool result" and re-try than crash
    the loop. Returns an empty list when no calls are present (which
    the loop driver interprets as "final answer").
    """
    raw_blocks = TOOL_CALL_RE.findall(text) + TOOL_CALL_FENCE_RE.findall(text)
    out: list[ToolCall] = []
    for block in raw_blocks:
        try:
            obj = json.loads(block)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        name = obj.get("name")
        if not isinstance(name, str) or not name:
            continue
        name = _canonical_tool_name(name)
        args = obj.get("args") or obj.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}
        out.append(ToolCall(name=name, args=args, raw=block))
    return out


def run_tool(
    call: ToolCall,
    *,
    fs_cfg: fs_tools.FsConfig,
    tools: dict[str, ToolFn] | None = None,
    confirm: ConfirmFn | None = None,
) -> ToolResult:
    """Dispatch a single tool call to the registry. Catches every
    exception so the agent loop is never broken by a tool failure --
    the error becomes the tool result and the model can react.

    ``confirm`` is consulted for tools listed in ``DESTRUCTIVE_TOOLS``;
    if it returns False, the call is rejected with a friendly message
    that the model can read and adapt to.
    """
    registry = tools if tools is not None else DEFAULT_TOOLS
    # Loop 267: also apply alias normalisation here so callers that
    # bypass parse_tool_calls (direct ToolCall construction in tests
    # or external integrations) still get the same dispatch as model
    # output that goes through the parser.
    canonical_name = _canonical_tool_name(call.name)
    fn = registry.get(canonical_name)
    if fn is None:
        avail = ", ".join(sorted(registry.keys()))
        return ToolResult(
            name=call.name,
            output=f"error: unknown tool {call.name!r}. Available: {avail}",
            error=True,
        )
    if canonical_name in DESTRUCTIVE_TOOLS and confirm is not None:
        try:
            if not confirm(call):
                return ToolResult(
                    name=call.name,
                    output=(
                        f"denied: user rejected {call.name} call. "
                        "Try a different approach or ask the user first."
                    ),
                    error=True,
                )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                name=call.name,
                output=f"error: confirm hook raised {type(exc).__name__}: {exc}",
                error=True,
            )
    try:
        out = fn(call.args, fs_cfg)
    except Exception as exc:  # noqa: BLE001
        return ToolResult(
            name=call.name,
            output=f"error: {type(exc).__name__}: {exc}",
            error=True,
        )
    return ToolResult(name=call.name, output=str(out))


def format_tool_results(results: list[ToolResult]) -> str:
    """Render tool results for feeding back to the model. Uses a
    matching ``<tool_result>`` tag so the model can pattern-match on
    them in long traces."""
    parts: list[str] = []
    for r in results:
        parts.append(
            f'<tool_result name="{r.name}">\n{r.output}\n</tool_result>'
        )
    return "\n\n".join(parts)


def strip_tool_calls(text: str) -> str:
    """Remove all tool_call blocks from a reply for display purposes.

    The reply that lands in the user-visible log shouldn't carry the
    raw tool_call JSON -- the user already sees `[tool: name]` lines.
    """
    cleaned = TOOL_CALL_RE.sub("", text)
    cleaned = TOOL_CALL_FENCE_RE.sub("", cleaned)
    return cleaned.strip()


def serialize_agent_state(history: list[ChatMessage]) -> dict[str, Any]:
    """Pack ``history`` into a JSON-safe dict for checkpointing.

    The shape is ``{"version": 1, "messages": [{"role": ..., "content": ...}, ...]}``
    so older checkpoints stay readable when we add fields later.
    """
    return {
        "version": 1,
        "messages": [
            {"role": m.role, "content": m.content} for m in history
        ],
    }


def deserialize_agent_state(data: dict[str, Any]) -> list[ChatMessage]:
    """Reverse of ``serialize_agent_state``. Tolerates missing version
    and silently skips entries with non-string fields rather than
    raising — a corrupt checkpoint shouldn't block recovery."""
    msgs: list[ChatMessage] = []
    for entry in data.get("messages", []):
        if not isinstance(entry, dict):
            continue
        role = entry.get("role")
        content = entry.get("content")
        if not isinstance(role, str) or not isinstance(content, str):
            continue
        msgs.append(ChatMessage(role=role, content=content))
    return msgs


def save_agent_checkpoint(path: Any, history: list[ChatMessage]) -> None:
    """Atomically write ``history`` to ``path`` as JSON.

    Writes via a ``.tmp`` sibling + ``os.replace`` so a crash mid-write
    can never leave a half-written checkpoint on disk.
    """
    import os
    from pathlib import Path

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(
        json.dumps(serialize_agent_state(history), ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(tmp, p)


def rotate_agent_checkpoints(
    primary: Any,
    history: list[ChatMessage],
    *,
    keep: int = 5,
) -> "Path":
    """Save ``history`` under a timestamped sibling of ``primary`` and
    refresh ``primary`` itself, retaining the most recent ``keep``
    timestamped snapshots.

    Layout::

        .agent/agent_state.json                       (primary, always latest)
        .agent/checkpoints/agent_state-<ts>.json      (rotated history)

    Where ``<ts>`` is ``YYYYmmddTHHMMSSffffff`` UTC so lexicographic
    sort matches chronological order. Returns the path of the rotated
    snapshot. ``keep <= 0`` keeps all snapshots forever.

    Older snapshots beyond the cap are deleted; failures during prune
    are swallowed so a single unwriteable file doesn't lose the new
    checkpoint that already landed.
    """
    from datetime import datetime, timezone
    from pathlib import Path

    p = Path(primary)
    save_agent_checkpoint(p, history)

    rot_dir = p.parent / "checkpoints"
    rot_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    snapshot = rot_dir / f"{p.stem}-{ts}{p.suffix}"
    save_agent_checkpoint(snapshot, history)

    if keep > 0:
        existing = sorted(
            rot_dir.glob(f"{p.stem}-*{p.suffix}"),
            key=lambda q: q.name,
        )
        excess = existing[:-keep] if len(existing) > keep else []
        for old in excess:
            try:
                old.unlink()
            except OSError:
                pass

    return snapshot


def list_agent_checkpoints(primary: Any) -> list["Path"]:
    """Return the rotated snapshots for ``primary`` sorted oldest-first.

    Returns an empty list when the rotation directory doesn't exist
    yet. Never raises — used by ``/checkpoints``-style UIs."""
    from pathlib import Path

    p = Path(primary)
    rot_dir = p.parent / "checkpoints"
    if not rot_dir.is_dir():
        return []
    return sorted(
        rot_dir.glob(f"{p.stem}-*{p.suffix}"),
        key=lambda q: q.name,
    )


def load_agent_checkpoint(path: Any) -> list[ChatMessage]:
    """Load a checkpoint written by ``save_agent_checkpoint``. Returns
    an empty list if the file is missing or unparseable; never raises."""
    from pathlib import Path

    p = Path(path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, dict):
        return []
    return deserialize_agent_state(data)


def load_latest_checkpoint(primary: Any) -> tuple[list[ChatMessage], "Path | None"]:
    """Load the freshest readable checkpoint for ``primary``.

    Tries ``primary`` first; on missing/corrupt/empty, falls back to the
    newest rotated snapshot in ``checkpoints/``, then the next newest,
    and so on, until a non-empty deserialised history is found or the
    list is exhausted. Returns ``(history, source_path)`` where
    ``source_path`` is the file the history actually came from, or
    ``None`` if nothing was loadable. Never raises.
    """
    from pathlib import Path

    p = Path(primary)
    primary_loaded = load_agent_checkpoint(p)
    if primary_loaded:
        return primary_loaded, p
    for snap in reversed(list_agent_checkpoints(p)):
        loaded = load_agent_checkpoint(snap)
        if loaded:
            return loaded, snap
    return [], None


# ------------------------------------------------------------- driver
def run_agent(
    history: list[ChatMessage],
    user_text: str,
    *,
    client: QwenClient,
    fs_cfg: fs_tools.FsConfig,
    system: str | None = None,
    max_steps: int = 6,
    tools: dict[str, ToolFn] | None = None,
    stream: bool = True,
    confirm: ConfirmFn | None = None,
    checkpoint: Callable[[list[ChatMessage], int], None] | None = None,
) -> Iterator[AgentEvent]:
    """Run an agentic turn against ``client``, yielding events as they
    happen.

    Mutates ``history`` so the caller can show the same chat log they
    do for non-agent turns. The user message is appended once; each
    iteration appends one assistant message and (if tools were called)
    one user message containing the tool results.

    Yields:
      AgentEvent(kind="ttft", latency_s=...)         time-to-first-token (streaming only, once per model turn)
      AgentEvent(kind="chunk", text=...)             token-by-token
      AgentEvent(kind="assistant", text=...)         each model turn (full)
      AgentEvent(kind="tool_call", tool=..., args=...)
      AgentEvent(kind="tool_result", tool=..., text=..., latency_s=...)
      AgentEvent(kind="summary", text="N tools, ...", latency_s=total)
      AgentEvent(kind="final", text=...)             terminal reply
      AgentEvent(kind="limit", text=...)             max_steps hit

    A ``summary`` event is emitted exactly once, right before the
    terminating ``final`` or ``limit`` event. Its ``latency_s`` is the
    total wall-clock time spent inside ``run_tool`` across the whole
    turn; ``text`` is a human-readable one-liner suitable for a status
    log (e.g. ``"3 tool calls, 1.2s total"``).

    When ``stream=True`` (default) and the client exposes
    ``chat_stream``, the loop streams each model turn and emits
    per-chunk events. Falls back to the blocking ``chat`` API
    otherwise (e.g. test stubs).
    """
    from . import prompts as _prompts  # avoid import cycle at module top

    # Loop 246: when the client has a TaskMemory, merge memory-management
    # tools into whatever registry the caller passed (or the default) so
    # the model can persist state across turns. The memory protocol blurb
    # is appended to the tool protocol doc so the model knows the names.
    memory = getattr(client, "task_memory", None)
    memory_tools = build_memory_tools(memory) if memory is not None else {}
    if memory_tools:
        if tools is None:
            tools = {**DEFAULT_TOOLS, **memory_tools}
        else:
            tools = {**tools, **memory_tools}
        # Loop 272: render the protocol doc against the *actual* registry
        # so the model never sees tools it can't call (or vice-versa).
        tool_doc = build_tool_protocol_doc(tools) + "\n" + MEMORY_TOOL_PROTOCOL_DOC
    else:
        tool_doc = build_tool_protocol_doc(tools)

    sys_text = system if system is not None else (
        _prompts.CODER_SYSTEM + "\n\n" + tool_doc
    )
    if not history or history[0].role != "system":
        history.insert(0, ChatMessage(role="system", content=sys_text))
    elif tool_doc[:40] not in history[0].content:
        history[0] = ChatMessage(
            role="system", content=history[0].content + "\n\n" + tool_doc
        )

    history.append(ChatMessage(role="user", content=user_text))

    # Loop 248: auto-seed current_task from the user's request on every
    # agent turn so the model literally cannot forget what it was asked.
    # We only set it when the user actually said something (skip empty
    # prompts) and we always overwrite — the user's most recent request
    # IS the current task. Truncated to 240 chars to keep the injected
    # system block compact; the full text still lives in history.
    # Bounded inside try/except so a memory glitch can't break the turn.
    if memory is not None:
        try:
            seed = (user_text or "").strip()
            if seed:
                if len(seed) > 240:
                    seed = seed[:237] + "..."
                memory.set_current_task(seed)
        except Exception:  # noqa: BLE001
            pass

    use_stream = stream and hasattr(client, "chat_stream")

    tool_count = 0
    tool_time_total = 0.0

    def _summary_text() -> str:
        if tool_count == 0:
            return "0 tool calls"
        plural = "" if tool_count == 1 else "s"
        return f"{tool_count} tool call{plural}, {tool_time_total:.2f}s total"

    for _step in range(max_steps):
        try:
            if use_stream:
                buf: list[str] = []
                _turn_started_at = time.monotonic()
                _ttft_emitted = False
                for chunk in client.chat_stream(history):
                    if not chunk:
                        continue
                    if not _ttft_emitted:
                        yield AgentEvent(
                            kind="ttft",
                            latency_s=time.monotonic() - _turn_started_at,
                        )
                        _ttft_emitted = True
                    buf.append(chunk)
                    yield AgentEvent(kind="chunk", text=chunk)
                reply = "".join(buf)
            else:
                reply = client.chat(history)
        except Exception as exc:  # noqa: BLE001
            # Plain-text wrapping (loop 263). The previous "[agent error: ...]"
            # form was ambiguous markup once a renderer concatenated it
            # with a styled prefix like "[green]qwen>[/green] ", so any
            # bracketed payload inside the message (e.g. "[/▍]" progress
            # chars from tool stdout) crashed the TUI's RichLog with
            # MarkupError. Plain text passes through escape cleanly.
            err = f"agent error: {type(exc).__name__}: {exc}"
            history.append(ChatMessage(role="assistant", content=err))
            yield AgentEvent(kind="assistant", text=err)
            yield AgentEvent(kind="final", text=err)
            return

        history.append(ChatMessage(role="assistant", content=reply))
        yield AgentEvent(kind="assistant", text=reply)

        calls = parse_tool_calls(reply)
        if not calls:
            yield AgentEvent(
                kind="summary",
                text=_summary_text(),
                latency_s=tool_time_total,
            )
            yield AgentEvent(kind="final", text=strip_tool_calls(reply) or reply)
            return

        results: list[ToolResult] = []
        for call in calls:
            yield AgentEvent(
                kind="tool_call", tool=call.name, args=dict(call.args)
            )
            _t0 = time.monotonic()
            result = run_tool(call, fs_cfg=fs_cfg, tools=tools, confirm=confirm)
            elapsed = time.monotonic() - _t0
            tool_count += 1
            tool_time_total += elapsed
            results.append(result)
            yield AgentEvent(
                kind="tool_result",
                tool=result.name,
                text=result.output,
                latency_s=elapsed,
            )

        feedback = format_tool_results(results)
        history.append(ChatMessage(role="user", content=feedback))

        if checkpoint is not None:
            try:
                checkpoint(list(history), _step + 1)
            except Exception:  # noqa: BLE001
                # Checkpoint failure must never abort an in-flight agent
                # turn — disk full, permission errors, etc. are recoverable.
                pass

    cap_msg = (
        f"[agent stopped after {max_steps} steps without final answer]"
    )
    history.append(ChatMessage(role="assistant", content=cap_msg))
    yield AgentEvent(
        kind="summary",
        text=_summary_text(),
        latency_s=tool_time_total,
    )
    yield AgentEvent(kind="limit", text=cap_msg)
    yield AgentEvent(kind="final", text=cap_msg)
