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
    """Yielded by ``run_agent`` so the UI can render progress live."""
    kind: str  # "assistant" | "tool_call" | "tool_result" | "limit" | "final"
    text: str = ""
    tool: str = ""
    args: dict[str, Any] | None = None


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
    res = fs_tools.read_file(cfg, path)
    text = str(res.get("text", ""))
    cap = int(args.get("max_bytes", 16000))
    if len(text) > cap:
        text = text[:cap] + "\n... [truncated]"
    return text


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
    "apply_patch": _tool_apply_patch,
}

ALL_TOOLS: dict[str, ToolFn] = {**DEFAULT_TOOLS, **WRITE_TOOLS}


TOOL_PROTOCOL_DOC = """\
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
- web_search(query: str, max_results: int=5) -- DuckDuckGo web search
- web_fetch(url: str, max_bytes: int=8000) -- fetch a URL's text body
- fs_read(path: str, max_bytes: int=16000) -- read a file from the workspace
- fs_list(path: str=".") -- list a workspace directory
- grep(pattern: str, path: str=".", ext: str|None=None) -- regex search
- find(glob: str, path: str=".") -- glob search
- fs_write(path: str, content: str, create_parents: bool=False) -- write a
  file (only available when the user has opted into write mode)
- apply_patch(diff: str, check_only: bool=False) -- apply a unified diff
  via git apply (only available in write mode; prefer check_only=true first)

Rules:
- Use tools when you need information you don't already have. Do not
  guess web facts; web_search them.
- Keep arguments minimal -- the runtime caps body sizes for you.
- After tool results land, ALWAYS produce a final answer that
  synthesizes them. Do not loop forever calling tools.
- If you have enough information, just answer directly.
"""


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
) -> ToolResult:
    """Dispatch a single tool call to the registry. Catches every
    exception so the agent loop is never broken by a tool failure --
    the error becomes the tool result and the model can react."""
    registry = tools if tools is not None else DEFAULT_TOOLS
    fn = registry.get(call.name)
    if fn is None:
        avail = ", ".join(sorted(registry.keys()))
        return ToolResult(
            name=call.name,
            output=f"error: unknown tool {call.name!r}. Available: {avail}",
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
) -> Iterator[AgentEvent]:
    """Run an agentic turn against ``client``, yielding events as they
    happen.

    Mutates ``history`` so the caller can show the same chat log they
    do for non-agent turns. The user message is appended once; each
    iteration appends one assistant message and (if tools were called)
    one user message containing the tool results.

    Yields:
      AgentEvent(kind="chunk", text=...)             token-by-token
      AgentEvent(kind="assistant", text=...)         each model turn (full)
      AgentEvent(kind="tool_call", tool=..., args=...)
      AgentEvent(kind="tool_result", tool=..., text=...)
      AgentEvent(kind="final", text=...)             terminal reply
      AgentEvent(kind="limit", text=...)             max_steps hit

    When ``stream=True`` (default) and the client exposes
    ``chat_stream``, the loop streams each model turn and emits
    per-chunk events. Falls back to the blocking ``chat`` API
    otherwise (e.g. test stubs).
    """
    from . import prompts as _prompts  # avoid import cycle at module top

    sys_text = system if system is not None else (
        _prompts.CODER_SYSTEM + "\n\n" + TOOL_PROTOCOL_DOC
    )
    if not history or history[0].role != "system":
        history.insert(0, ChatMessage(role="system", content=sys_text))
    elif TOOL_PROTOCOL_DOC[:40] not in history[0].content:
        history[0] = ChatMessage(
            role="system", content=history[0].content + "\n\n" + TOOL_PROTOCOL_DOC
        )

    history.append(ChatMessage(role="user", content=user_text))

    use_stream = stream and hasattr(client, "chat_stream")

    for _step in range(max_steps):
        try:
            if use_stream:
                buf: list[str] = []
                for chunk in client.chat_stream(history):
                    if not chunk:
                        continue
                    buf.append(chunk)
                    yield AgentEvent(kind="chunk", text=chunk)
                reply = "".join(buf)
            else:
                reply = client.chat(history)
        except Exception as exc:  # noqa: BLE001
            err = f"[agent error: {type(exc).__name__}: {exc}]"
            history.append(ChatMessage(role="assistant", content=err))
            yield AgentEvent(kind="assistant", text=err)
            yield AgentEvent(kind="final", text=err)
            return

        history.append(ChatMessage(role="assistant", content=reply))
        yield AgentEvent(kind="assistant", text=reply)

        calls = parse_tool_calls(reply)
        if not calls:
            yield AgentEvent(kind="final", text=strip_tool_calls(reply) or reply)
            return

        results: list[ToolResult] = []
        for call in calls:
            yield AgentEvent(
                kind="tool_call", tool=call.name, args=dict(call.args)
            )
            result = run_tool(call, fs_cfg=fs_cfg, tools=tools)
            results.append(result)
            yield AgentEvent(
                kind="tool_result", tool=result.name, text=result.output
            )

        feedback = format_tool_results(results)
        history.append(ChatMessage(role="user", content=feedback))

    cap_msg = (
        f"[agent stopped after {max_steps} steps without final answer]"
    )
    history.append(ChatMessage(role="assistant", content=cap_msg))
    yield AgentEvent(kind="limit", text=cap_msg)
    yield AgentEvent(kind="final", text=cap_msg)
