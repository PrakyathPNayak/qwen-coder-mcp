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
from .qwen_client import ChatMessage, QwenClient, _strip_think_blocks

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
    kind: str  # "model_start" | "assistant" | "tool_call" | "tool_result" | "empty_retry" | "limit" | "final" | "chunk"
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


import threading as _threading_for_ask  # noqa: E402

# Loop 284: ask_user tool. The tool itself is a simple callable that
# reads a thread-local handler; TUI hosts install a handler in
# ``run_agent`` worker context, then the model's `<tool_call>` for
# `ask_user` pops a modal and blocks until the operator responds.
# CLI/tests with no handler installed get a clear placeholder message.
_ASK_USER_TLS = _threading_for_ask.local()


def set_ask_user_handler(
    handler: Callable[[str, list[str]], str] | None,
) -> Callable[[str, list[str]], str] | None:
    """Install a thread-local ``ask_user`` handler. Returns the previous
    value so callers can restore it (use try/finally). Pass ``None`` to
    clear. Handler signature: ``handler(question, choices) -> str``.
    Choices may be empty (free-form input expected); the returned
    string is the operator's reply verbatim.
    """
    prev = getattr(_ASK_USER_TLS, "handler", None)
    _ASK_USER_TLS.handler = handler
    return prev


def _tool_ask_user(args: dict[str, Any], cfg: fs_tools.FsConfig) -> str:
    """Loop 284: prompt the operator for a decision.

    Args:
      * ``question`` (required str): the prompt shown to the operator.
      * ``choices`` (optional list[str]): when provided, the operator
        is asked to pick one of these labels. When omitted, free-form
        text input is accepted.
      * ``timeout`` (optional float, default 120s): max wait. Returns
        ``"timeout"`` if the operator doesn't respond in time.

    Without a host-installed handler the tool returns a clear marker so
    the model can still make progress (e.g. fall back to a default).
    """
    q = str(args.get("question", "")).strip()
    if not q:
        return "error: ask_user needs a 'question' arg"
    raw_choices = args.get("choices") or []
    choices: list[str] = []
    if isinstance(raw_choices, list):
        for c in raw_choices:
            if isinstance(c, str) and c.strip():
                choices.append(c.strip())
    handler = getattr(_ASK_USER_TLS, "handler", None)
    if handler is None:
        return (
            "ask_user: no interactive operator available in this context. "
            "Pick a sensible default and proceed; if the decision is "
            "irreversible, ask in the assistant's reply text instead."
        )
    try:
        reply = handler(q, choices)
    except Exception as exc:  # noqa: BLE001
        return f"error: ask_user handler failed: {type(exc).__name__}: {exc}"
    if reply is None:
        return "user_canceled"
    return str(reply)


def _tool_diff_files(args: dict[str, Any], cfg: fs_tools.FsConfig) -> str:
    """Loop 282: read-only unified diff between two workspace files.

    Args: ``a`` and ``b`` are workspace-relative paths. Optional
    ``context`` int (default 3) controls unified-diff context lines.
    Both files must be inside the workspace root and at most
    ``cfg.max_read_bytes`` in size. Returns the diff text or an
    "error: ..." string. An empty diff is reported as
    "files are identical".
    """
    import difflib

    a = str(args.get("a", "")).strip()
    b = str(args.get("b", "")).strip()
    if not a or not b:
        return "error: diff_files needs both 'a' and 'b' path args"
    try:
        ctx = int(args.get("context", 3))
    except (TypeError, ValueError):
        ctx = 3
    ctx = max(0, min(ctx, 50))
    try:
        ap = fs_tools._resolve_inside_root(cfg, a)
        bp = fs_tools._resolve_inside_root(cfg, b)
    except fs_tools.FsError as exc:
        return f"error: {exc}"
    for label, p in (("a", ap), ("b", bp)):
        if not p.exists():
            return f"error: not found: {a if label == 'a' else b}"
        if not p.is_file():
            return f"error: not a file: {a if label == 'a' else b}"
        if p.stat().st_size > cfg.max_read_bytes:
            return (
                f"error: {label} too large "
                f"({p.stat().st_size} > {cfg.max_read_bytes} bytes)"
            )
    try:
        a_text = ap.read_text(errors="replace").splitlines(keepends=True)
        b_text = bp.read_text(errors="replace").splitlines(keepends=True)
    except OSError as exc:
        return f"error: {exc}"
    diff = list(
        difflib.unified_diff(a_text, b_text, fromfile=a, tofile=b, n=ctx)
    )
    if not diff:
        return "files are identical"
    out = "".join(diff)
    cap = cfg.max_read_bytes
    if len(out) > cap:
        out = out[:cap] + f"\n... [diff truncated at {cap} bytes]\n"
    return out


def _tool_file_info(args: dict[str, Any], cfg: fs_tools.FsConfig) -> str:
    """Loop 275: read-only stat tool. Returns size, mtime, mode, kind."""
    import hashlib

    path = str(args.get("path", "")).strip()
    if not path:
        return "error: file_info needs a 'path' arg"
    try:
        p = fs_tools._resolve_inside_root(cfg, path)
    except fs_tools.FsError as exc:
        return f"error: {exc}"
    if not p.exists():
        return f"error: not found: {path}"
    st = p.stat()
    kind = "dir" if p.is_dir() else ("symlink" if p.is_symlink() else "file")
    parts = [
        f"path: {path}",
        f"kind: {kind}",
        f"size: {st.st_size}",
        f"mode: {oct(st.st_mode & 0o777)}",
        f"mtime: {int(st.st_mtime)}",
    ]
    if p.is_file() and bool(args.get("sha256", False)) and st.st_size <= 50_000_000:
        h = hashlib.sha256()
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        parts.append(f"sha256: {h.hexdigest()}")
    if p.is_file():
        try:
            with p.open("rb") as f:
                head = f.read(2048)
            nl = head.count(b"\n")
            parts.append(f"lines: ~{nl} (first 2KB)")
        except OSError:
            pass
    return "\n".join(parts)


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


def _tool_mkdir(args: dict[str, Any], cfg: fs_tools.FsConfig) -> str:
    """Loop 277: create a directory inside the workspace."""
    path = str(args.get("path", "")).strip()
    if not path:
        return "error: mkdir needs a 'path' arg"
    parents = bool(args.get("parents", True))
    exist_ok = bool(args.get("exist_ok", True))
    try:
        target = fs_tools._resolve_inside_root(cfg, path)
    except fs_tools.FsError as exc:
        return f"error: {exc}"
    try:
        target.mkdir(parents=parents, exist_ok=exist_ok)
    except FileExistsError:
        return f"error: already exists: {path}"
    except OSError as exc:
        return f"error: {exc}"
    return f"created dir {path}"


def _tool_touch(args: dict[str, Any], cfg: fs_tools.FsConfig) -> str:
    """Loop 277: create an empty file (or update mtime if exists)."""
    path = str(args.get("path", "")).strip()
    if not path:
        return "error: touch needs a 'path' arg"
    create_parents = bool(args.get("create_parents", False))
    try:
        target = fs_tools._resolve_inside_root(cfg, path)
    except fs_tools.FsError as exc:
        return f"error: {exc}"
    if create_parents:
        target.parent.mkdir(parents=True, exist_ok=True)
    try:
        if target.exists():
            target.touch(exist_ok=True)
            return f"touched (existing) {path}"
        target.touch()
        return f"created empty file {path}"
    except OSError as exc:
        return f"error: {exc}"


def _git_run(cfg: fs_tools.FsConfig, cmd: str) -> str:
    """Helper: run a git subcommand via shell_tools and return formatted output."""
    try:
        res = shell_tools.run_shell(cfg, cmd, timeout=15.0)
    except shell_tools.ShellError as exc:
        return f"denied: {exc}"
    return shell_tools.format_run_result(res)


def _tool_git_status(args: dict[str, Any], cfg: fs_tools.FsConfig) -> str:
    return _git_run(cfg, "git status --short --branch")


def _tool_git_diff(args: dict[str, Any], cfg: fs_tools.FsConfig) -> str:
    path = args.get("path")
    staged = bool(args.get("staged", False))
    cmd = "git --no-pager diff"
    if staged:
        cmd += " --staged"
    if isinstance(path, str) and path.strip():
        # Use shlex-safe quoting -- path is workspace-relative, no shell special chars
        # tolerated. Strip quotes to avoid breaking the command.
        safe = path.replace("'", "").replace('"', "")
        cmd += f" -- '{safe}'"
    return _git_run(cfg, cmd)


def _tool_git_log(args: dict[str, Any], cfg: fs_tools.FsConfig) -> str:
    n = args.get("n", 10)
    try:
        n = max(1, min(int(n), 200))
    except (TypeError, ValueError):
        n = 10
    return _git_run(cfg, f"git --no-pager log --oneline -n {n}")


def _tool_mv(args: dict[str, Any], cfg: fs_tools.FsConfig) -> str:
    """Loop 278: rename/move a file or directory inside the workspace."""
    src = str(args.get("src", "")).strip()
    dst = str(args.get("dst", "")).strip()
    if not src or not dst:
        return "error: mv needs 'src' and 'dst' args"
    overwrite = bool(args.get("overwrite", False))
    try:
        s = fs_tools._resolve_inside_root(cfg, src)
        d = fs_tools._resolve_inside_root(cfg, dst)
    except fs_tools.FsError as exc:
        return f"error: {exc}"
    if not s.exists():
        return f"error: not found: {src}"
    if d.exists() and not overwrite:
        return f"error: dst already exists: {dst} (set overwrite=true)"
    try:
        d.parent.mkdir(parents=True, exist_ok=True)
        if d.exists() and overwrite:
            if d.is_dir():
                import shutil
                shutil.rmtree(d)
            else:
                d.unlink()
        s.rename(d)
    except OSError as exc:
        return f"error: {exc}"
    return f"moved {src} -> {dst}"


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


def _tool_python_exec(args: dict[str, Any], cfg: fs_tools.FsConfig) -> str:
    """Loop 286: pipe `code` to a fresh python -I and return stdout/stderr.

    Cheaper than run_shell for scratch computation: structured timeout,
    no shell-quoting hell, no argv-length cap. Same chokepoints though
    -- it is a destructive tool (subprocess) and the run_agent confirm
    hook will gate it just like run_shell.
    """
    code = args.get("code") or args.get("source") or ""
    if not isinstance(code, str) or not code.strip():
        return "error: python_exec needs a 'code' arg"
    timeout = args.get("timeout", 15.0)
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        timeout = 15.0
    cwd = args.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        cwd = None
    try:
        res = shell_tools.run_python(cfg, code, timeout=timeout, cwd=cwd)
    except shell_tools.ShellError as exc:
        return f"denied: {exc}"
    return shell_tools.format_run_result(res)


# ---------------------------------------- loop 290: utility tools --------

_HTTP_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _tool_http_request(args: dict[str, Any], _cfg: fs_tools.FsConfig) -> str:
    """Loop 290: make an HTTP request and return the response body.

    GET and HEAD are read-only; PUT/POST/DELETE/PATCH are destructive
    and require write-mode (the confirm hook gates them in the TUI).
    Response body is truncated at 32KB.
    """
    import urllib.request
    import urllib.error

    url = str(args.get("url", "")).strip()
    if not url:
        return "error: http_request needs a 'url' arg"
    method = str(args.get("method", "GET")).upper()
    allowed_methods = {"GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"}
    if method not in allowed_methods:
        return f"error: unsupported method {method!r}; choose from {sorted(allowed_methods)}"
    headers: dict[str, str] = {}
    raw_headers = args.get("headers") or {}
    if isinstance(raw_headers, dict):
        for k, v in raw_headers.items():
            if isinstance(k, str) and isinstance(v, str):
                headers[k] = v
    body_str = args.get("body") or args.get("data") or ""
    body_bytes: bytes | None = None
    if body_str and isinstance(body_str, str):
        body_bytes = body_str.encode()
    elif isinstance(body_str, bytes):
        body_bytes = body_str
    timeout = args.get("timeout", 20.0)
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        timeout = 20.0
    timeout = max(1.0, min(60.0, timeout))
    max_bytes = 32_768
    req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            status = resp.status
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read(max_bytes)
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code} {exc.reason}: {url}"
    except urllib.error.URLError as exc:
        return f"error: {exc.reason}"
    except OSError as exc:
        return f"error: {exc}"
    body_text = raw.decode("utf-8", errors="replace")
    truncated = " [truncated]" if len(raw) >= max_bytes else ""
    return f"status={status} content-type={content_type!r}{truncated}\n{body_text}"


def _tool_http_request_readonly(
    args: dict[str, Any], cfg: fs_tools.FsConfig
) -> str:
    """Read-only registry wrapper for http_request.

    Loop 293: the public blurb said mutating HTTP verbs required write-mode,
    but DEFAULT_TOOLS exposed the full implementation. Keep GET/HEAD/OPTIONS
    available to read-only agents and reserve POST/PUT/PATCH/DELETE for the
    write registry where confirmation can run.
    """
    method = str(args.get("method", "GET")).upper()
    if method not in _HTTP_SAFE_METHODS:
        return (
            f"error: http_request method {method!r} requires write-mode "
            "approval; use GET, HEAD, or OPTIONS in read-only mode"
        )
    return _tool_http_request(args, cfg)


def _tool_json_query(args: dict[str, Any], _cfg: fs_tools.FsConfig) -> str:
    """Loop 290: extract a value from a JSON string using a dotted path.

    path uses dot notation: "items.0.name" or "meta.total".
    Returns the extracted value as JSON-serialized text.
    If path is empty or "." the entire document is pretty-printed.
    """
    import json as _json

    raw = args.get("json") or args.get("data") or ""
    if not isinstance(raw, str) or not raw.strip():
        return "error: json_query needs a 'json' arg with JSON text"
    path = str(args.get("path", "")).strip()
    try:
        doc = _json.loads(raw)
    except _json.JSONDecodeError as exc:
        return f"error: invalid JSON: {exc}"
    if not path or path == ".":
        return _json.dumps(doc, indent=2, ensure_ascii=False)
    parts = path.split(".")
    current = doc
    for part in parts:
        if part == "":
            continue
        if isinstance(current, list):
            try:
                idx = int(part)
            except ValueError:
                return f"error: expected integer index for list, got {part!r}"
            if idx < 0 or idx >= len(current):
                return f"error: list index {idx} out of range (len={len(current)})"
            current = current[idx]
        elif isinstance(current, dict):
            if part not in current:
                return f"error: key {part!r} not found; available: {list(current.keys())}"
            current = current[part]
        else:
            return f"error: cannot index into {type(current).__name__} with {part!r}"
    return _json.dumps(current, indent=2, ensure_ascii=False)


# Sensitive env var prefixes to redact from env_get output.
_ENV_DENYLIST = frozenset({
    "AWS_SECRET", "AWS_ACCESS", "GITHUB_TOKEN", "GH_TOKEN",
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY",
    "HUGGINGFACE_TOKEN", "HF_TOKEN", "DATABRICKS_TOKEN",
    "DATABASE_URL", "DATABASE_PASSWORD", "DB_PASSWORD",
    "SECRET_KEY", "PRIVATE_KEY", "PEM_KEY", "PASSPHRASE",
    "NPM_TOKEN", "PYPI_TOKEN", "DOCKERHUB_PASSWORD",
    "SLACK_TOKEN", "DISCORD_TOKEN", "TELEGRAM_BOT_TOKEN",
})


def _is_sensitive_env(name: str) -> bool:
    up = name.upper()
    return any(blocked in up for blocked in _ENV_DENYLIST)


def _tool_env_get(args: dict[str, Any], _cfg: fs_tools.FsConfig) -> str:
    """Loop 290: read one or more environment variables.

    Pass name="VAR" for a single variable or names=["A","B","C"] for
    multiple. Sensitive variables (API keys, tokens, passwords, etc.)
    are redacted. Use this to inspect PATH, VIRTUAL_ENV, HOME, etc.
    """
    import os as _os
    import json as _json

    single = args.get("name") or args.get("var") or ""
    multi: list[str] = []
    raw_names = args.get("names") or args.get("vars") or []
    if isinstance(raw_names, list):
        multi = [str(n) for n in raw_names if n]
    if single:
        multi = [str(single)] + multi
    if not multi:
        return "error: env_get needs 'name' or 'names' arg"
    result: dict[str, str] = {}
    for var in multi:
        if not var or not var.replace("_", "").isalnum():
            result[var] = f"[error: invalid env name {var!r}]"
        elif _is_sensitive_env(var):
            result[var] = "[REDACTED: sensitive variable]"
        else:
            val = _os.environ.get(var)
            result[var] = val if val is not None else "[not set]"
    if len(result) == 1:
        return next(iter(result.values()))
    return _json.dumps(result, indent=2)


def _tool_cp(args: dict[str, Any], cfg: fs_tools.FsConfig) -> str:
    """Loop 290: copy a file within the workspace sandbox."""
    import shutil as _shutil

    src = str(args.get("src", "")).strip()
    dst = str(args.get("dst", "")).strip()
    if not src or not dst:
        return "error: cp needs 'src' and 'dst' args"
    overwrite = bool(args.get("overwrite", False))
    try:
        s = fs_tools._resolve_inside_root(cfg, src)
        d = fs_tools._resolve_inside_root(cfg, dst)
    except fs_tools.FsError as exc:
        return f"error: {exc}"
    if not s.exists():
        return f"error: not found: {src}"
    if s.is_dir():
        return "error: cp only supports files; use run_shell for directory copies"
    if d.exists() and not overwrite:
        return f"error: dst already exists: {dst} (set overwrite=true)"
    try:
        d.parent.mkdir(parents=True, exist_ok=True)
        _shutil.copy2(s, d)
    except OSError as exc:
        return f"error: {exc}"
    return f"copied {src} -> {dst}"


def _tool_append_file(args: dict[str, Any], cfg: fs_tools.FsConfig) -> str:
    """Append text to a workspace file without replacing existing content."""
    path = str(args.get("path", "")).strip()
    content = args.get("content", "")
    if not path:
        return "error: append_file needs a 'path' arg"
    if not isinstance(content, str):
        return "error: append_file needs string 'content' arg"
    encoded = content.encode("utf-8")
    if len(encoded) > cfg.max_write_bytes:
        return (
            f"error: appended content too large "
            f"({len(encoded)} > {cfg.max_write_bytes})"
        )
    create_parents = bool(args.get("create_parents", False))
    try:
        target = fs_tools._resolve_inside_root(cfg, path)
    except fs_tools.FsError as exc:
        return f"error: {exc}"
    if target.exists() and target.is_dir():
        return f"error: cannot append to directory: {path}"
    try:
        if create_parents:
            target.parent.mkdir(parents=True, exist_ok=True)
        elif not target.parent.exists():
            root = cfg.root.resolve(strict=False)
            return (
                f"error: parent directory does not exist: "
                f"{target.parent.relative_to(root)}"
            )
        with target.open("a", encoding="utf-8") as fh:
            fh.write(content)
    except (OSError, ValueError) as exc:
        return f"error: {exc}"
    return f"appended {len(encoded)} bytes to {path}"


def _tool_rm(args: dict[str, Any], cfg: fs_tools.FsConfig) -> str:
    """Delete a workspace file, or a directory when recursive=true."""
    import shutil as _shutil

    path = str(args.get("path", "")).strip()
    if not path:
        return "error: rm needs a 'path' arg"
    recursive = bool(args.get("recursive", False))
    missing_ok = bool(args.get("missing_ok", False))
    root = cfg.root.resolve(strict=False)
    try:
        # Resolve the parent, not the final path, so deleting a symlink
        # removes the link itself instead of following it to the target.
        target = root / path
        if target == root:
            return "error: refusing to remove workspace root"
        target.parent.resolve(strict=False).relative_to(root)
    except ValueError:
        return f"error: path escapes repo root: {path}"
    if target.is_symlink():
        try:
            target.unlink()
            return f"removed symlink {path}"
        except OSError as exc:
            return f"error: {exc}"
    try:
        resolved_target = target.resolve(strict=False)
        resolved_target.relative_to(root)
    except ValueError:
        return f"error: path escapes repo root: {path}"
    if resolved_target == root:
        return "error: refusing to remove workspace root"
    if not target.exists():
        if missing_ok:
            return f"not found (ignored): {path}"
        return f"error: not found: {path}"
    try:
        if target.is_dir():
            if not recursive:
                return "error: path is a directory; set recursive=true"
            _shutil.rmtree(target)
            return f"removed directory {path}"
        target.unlink()
        return f"removed file {path}"
    except OSError as exc:
        return f"error: {exc}"


DEFAULT_TOOLS: dict[str, ToolFn] = {
    "web_search": _tool_web_search,
    "web_fetch": _tool_web_fetch,
    "http_request": _tool_http_request_readonly,
    "json_query": _tool_json_query,
    "env_get": _tool_env_get,
    "fs_read": _tool_fs_read,
    "fs_list": _tool_fs_list,
    "file_info": _tool_file_info,
    "diff_files": _tool_diff_files,
    "ask_user": _tool_ask_user,
    "grep": _tool_grep,
    "find": _tool_find,
    "git_status": _tool_git_status,
    "git_diff": _tool_git_diff,
    "git_log": _tool_git_log,
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
    "mkdir": _tool_mkdir,
    "touch": _tool_touch,
    "mv": _tool_mv,
    "cp": _tool_cp,
    "append_file": _tool_append_file,
    "rm": _tool_rm,
    "http_request": _tool_http_request,
    "run_shell": _tool_run_shell,
    "python_exec": _tool_python_exec,
}

ALL_TOOLS: dict[str, ToolFn] = {**DEFAULT_TOOLS, **WRITE_TOOLS}

# Tools that mutate the workspace -- the loop driver routes calls to
# these through an optional confirmation hook so a TUI/CLI can prompt
# the user before the write actually happens.
DESTRUCTIVE_TOOLS: frozenset[str] = frozenset(WRITE_TOOLS.keys())


def _tool_call_requires_confirm(name: str, args: dict[str, Any]) -> bool:
    if name == "http_request":
        method = str(args.get("method", "GET")).upper()
        return method not in _HTTP_SAFE_METHODS
    return name in DESTRUCTIVE_TOOLS


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
    "file_info": (
        "- file_info(path: str, sha256: bool=false) -- stat a file/dir: size,\n"
        "  mode, mtime, kind, optional sha256 hash for files <= 50MB. Read-only."
    ),
    "diff_files": (
        "- diff_files(a: str, b: str, context: int=3) -- unified diff between\n"
        "  two workspace files (read-only). Reports 'files are identical' on\n"
        "  match. Context lines clamped to 0-50."
    ),
    "ask_user": (
        "- ask_user(question: str, choices: list[str]|null=null,\n"
        "  timeout: float=120) -- prompt the human operator for a decision.\n"
        "  When choices are given, returns the chosen label; otherwise\n"
        "  returns the operator's free-form reply. Returns 'user_canceled'\n"
        "  if dismissed, 'timeout' if no answer in time, or a 'no\n"
        "  interactive operator available' marker in headless contexts."
    ),
    "grep": "- grep(pattern: str, path: str=\".\", ext: str|None=None) -- regex search",
    "find": "- find(glob: str, path: str=\".\") -- glob search",
    "git_status": "- git_status() -- short git status with branch info (read-only)",
    "git_diff": (
        "- git_diff(path: str|None=None, staged: bool=false) -- show unified diff\n"
        "  of unstaged (default) or staged changes; optionally scoped to a path."
    ),
    "git_log": "- git_log(n: int=10) -- last N commits (--oneline). Capped at 200.",
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
    "mkdir": (
        "- mkdir(path: str, parents: bool=true, exist_ok: bool=true) -- create a\n"
        "  directory inside the workspace. Idempotent by default."
    ),
    "touch": (
        "- touch(path: str, create_parents: bool=false) -- create an empty file\n"
        "  or update mtime if it exists."
    ),
    "mv": (
        "- mv(src: str, dst: str, overwrite: bool=false) -- rename/move a file\n"
        "  or directory within the workspace. Creates dst's parent dirs as needed."
    ),
    "run_shell": (
        "- run_shell(cmd: str, timeout: float=30, cwd: str|None=None) -- run a\n"
        "  shell command inside the workspace sandbox. The command is screened\n"
        "  against a denylist; rm/mv/curl/etc are blocked by default. The user\n"
        "  is asked to approve each command before it runs (Copilot-style)."
    ),
    "python_exec": (
        "- python_exec(code: str, timeout: float=15, cwd: str|None=None) --\n"
        "  pipe `code` to a fresh `python -I` interpreter via stdin and return\n"
        "  exit code, stdout, and stderr. Use this for scratch computation,\n"
        "  AST checks, JSON munging, or any quick calculation -- cleaner than\n"
        "  building a `run_shell` command with shell-escaped python -c '...'.\n"
        "  Same Copilot-style approval as run_shell; same workspace cwd lock."
    ),
    "http_request": (
        "- http_request(url: str, method: str='GET', headers: dict={},\n"
        "  body: str='', timeout: float=20) -- make an HTTP request and\n"
        "  return status + response body (truncated at 32KB). GET/HEAD/OPTIONS\n"
        "  are read-only; POST/PUT/DELETE/PATCH require write-mode and Copilot\n"
        "  approval. Use for calling REST APIs, health checks, webhooks."
    ),
    "json_query": (
        "- json_query(json: str, path: str='.') -- extract a value from JSON\n"
        "  text using dot notation (e.g. 'items.0.name'). Path '.' or empty\n"
        "  returns the whole document pretty-printed. Useful for parsing\n"
        "  http_request responses or large config files."
    ),
    "env_get": (
        "- env_get(name: str='', names: list[str]=[]) -- read one or more\n"
        "  environment variables. Sensitive names (API keys, tokens, passwords)\n"
        "  are automatically redacted. Use to inspect PATH, VIRTUAL_ENV,\n"
        "  HOME, QWEN_*, etc."
    ),
    "cp": (
        "- cp(src: str, dst: str, overwrite: bool=false) -- copy a file\n"
        "  inside the workspace sandbox. Creates dst parent dirs as needed.\n"
        "  Requires write-mode."
    ),
    "append_file": (
        "- append_file(path: str, content: str, create_parents: bool=false) --\n"
        "  append text to a workspace file without replacing existing content.\n"
        "  Creates parent dirs only when create_parents=true. Requires write-mode."
    ),
    "rm": (
        "- rm(path: str, recursive: bool=false, missing_ok: bool=false) --\n"
        "  delete a workspace file. Directories require recursive=true.\n"
        "  Refuses to remove the workspace root. Requires write-mode."
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
            try:
                # Qwen sometimes emits literal newlines inside JSON string
                # values (especially fs_write content). Python's JSON parser
                # can accept that common non-standard shape with strict=False.
                obj = json.loads(block, strict=False)
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
    if (
        _tool_call_requires_confirm(canonical_name, call.args)
        and confirm is not None
    ):
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
    max_steps: int = 20,
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

    # Build a sentinel that's unique to the exact tool set for this turn.
    # Use sorted tool names so we can detect when the registry changed
    # (e.g. read-only first turn → write-enabled second turn) and replace
    # the stale catalog in the system message rather than silently leaving
    # the old read-only list in place. The sentinel is embedded as a
    # comment line that the model will never read but the injector checks.
    _tool_keys = sorted((tools or DEFAULT_TOOLS).keys())
    _tool_sentinel = f"<!-- tools:{','.join(_tool_keys)} -->"

    sys_text = system if system is not None else (
        _prompts.CODER_SYSTEM + "\n\n" + _tool_sentinel + "\n" + tool_doc
    )
    if not history or history[0].role != "system":
        history.insert(0, ChatMessage(role="system", content=sys_text))
    elif _tool_sentinel not in history[0].content:
        # Either no catalog yet, or the catalog is stale (different tool
        # set). Strip any previous tool sentinel+catalog block and replace
        # with the fresh one so the model always sees the correct list.
        base = history[0].content
        # Remove any previous <!-- tools:... --> sentinel and everything
        # after it (which is the old catalog).
        _prev_sentinel_start = base.find("<!-- tools:")
        if _prev_sentinel_start != -1:
            base = base[:_prev_sentinel_start].rstrip()
        history[0] = ChatMessage(
            role="system",
            content=base + "\n\n" + _tool_sentinel + "\n" + tool_doc,
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
        if _step > 0:
            yield AgentEvent(kind="model_start", text=f"step {_step + 1}")
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
                # chat_stream strips normal wrapped <think> blocks while
                # chunks arrive, but Qwen3.6 often emits unwrapped reasoning
                # followed by a dangling </think>. Apply the batch stripper
                # to the full turn before parsing tool calls or persisting
                # history so speculative tool calls inside hidden reasoning
                # cannot execute and final answers don't leak thoughts.
                reply = _strip_think_blocks("".join(buf))
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

        if not reply.strip():
            nudge = (
                "The previous assistant response contained no visible "
                "content after hidden reasoning was removed. Continue now "
                "with either a valid <tool_call> block or a concise final "
                "answer; do not emit only hidden reasoning."
            )
            history.append(ChatMessage(role="user", content=nudge))
            yield AgentEvent(kind="empty_retry", text=nudge)
            if checkpoint is not None:
                try:
                    checkpoint(list(history), _step + 1)
                except Exception:  # noqa: BLE001
                    pass
            continue

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
