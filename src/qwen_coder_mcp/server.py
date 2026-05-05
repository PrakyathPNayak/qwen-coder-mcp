"""MCP server exposing Qwen3.6-27B coding tools over stdio."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .qwen_client import ChatMessage, QwenClient
from . import prompts
from . import web_tools
from . import fs_tools
from . import shell_tools
from . import perplexity_tools


def _default_fs_config() -> fs_tools.FsConfig:
    root_str = os.environ.get("QWEN_MCP_FS_ROOT") or os.getcwd()
    return fs_tools.FsConfig(root=Path(root_str))


def _perplexity_chat_input_schema(
    s: dict[str, str],
    *,
    with_strip_thinking: bool = False,
    with_async_extras: bool = False,
) -> dict[str, Any]:
    """Build the JSON-Schema for the perplexity chat tools.

    Centralised because three tools share an identical option surface;
    duplicating the literal at three call sites was the original cause
    of recurring drift between this server and the perplexity-py SDK.
    Variants:

    * ``with_strip_thinking`` -- adds the boolean toggle that drops
      ``<think>...</think>`` blocks (only meaningful for the deep-research
      and reasoning models).
    * ``with_async_extras`` -- adds ``model`` (required) and
      ``idempotency_key`` for the async-create surface, where the model
      isn't fixed by the tool name."""
    props: dict[str, Any] = {
        "messages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"role": s, "content": s},
                "required": ["role", "content"],
            },
        },
        # Search/web options
        "search_recency_filter": {
            "type": "string",
            "enum": list(perplexity_tools.VALID_RECENCY),
        },
        "search_domain_filter": {"type": "array", "items": s},
        "search_language_filter": {"type": "array", "items": s},
        "search_mode": {
            "type": "string",
            "enum": list(perplexity_tools.VALID_SEARCH_MODE),
        },
        "search_after_date_filter": s,
        "search_before_date_filter": s,
        "last_updated_after_filter": s,
        "last_updated_before_filter": s,
        "disable_search": {"type": "boolean"},
        "return_related_questions": {"type": "boolean"},
        "return_images": {"type": "boolean"},
        # web_search_options sub-object
        "search_context_size": {
            "type": "string",
            "enum": list(perplexity_tools.VALID_CONTEXT_SIZE),
        },
        "search_type": {
            "type": "string",
            "enum": list(perplexity_tools.VALID_SEARCH_TYPE),
        },
        "user_location": {
            "type": "object",
            "properties": {
                "city": s,
                "country": s,
                "region": s,
                "latitude": {"type": "number"},
                "longitude": {"type": "number"},
            },
        },
        "image_results_enhanced_relevance": {"type": "boolean"},
        # Generation knobs
        "reasoning_effort": {
            "type": "string",
            "enum": list(perplexity_tools.VALID_REASONING_EFFORT),
        },
        "temperature": {"type": "number"},
        "top_p": {"type": "number"},
        "top_k": {"type": "integer"},
        "max_tokens": {"type": "integer", "minimum": 1},
        "frequency_penalty": {"type": "number"},
        "presence_penalty": {"type": "number"},
        "stop": {
            "oneOf": [s, {"type": "array", "items": s}],
        },
        "country": s,
        "response_format": {"type": "object"},
    }
    required: list[str] = ["messages"]
    if with_strip_thinking:
        props["strip_thinking"] = {"type": "boolean"}
    if with_async_extras:
        props["model"] = s
        props["idempotency_key"] = s
        required.append("model")
    return {"type": "object", "properties": props, "required": required}


# Keys forwarded to perplexity_chat / perplexity_async_create when set.
# Listed once here so the dispatcher and the schema stay aligned.
_PERPLEXITY_CHAT_OPTION_KEYS = (
    "search_recency_filter",
    "search_domain_filter",
    "search_language_filter",
    "search_mode",
    "search_after_date_filter",
    "search_before_date_filter",
    "last_updated_after_filter",
    "last_updated_before_filter",
    "disable_search",
    "return_related_questions",
    "return_images",
    "search_context_size",
    "search_type",
    "user_location",
    "image_results_enhanced_relevance",
    "reasoning_effort",
    "temperature",
    "top_p",
    "top_k",
    "max_tokens",
    "frequency_penalty",
    "presence_penalty",
    "stop",
    "country",
    "response_format",
)


def _extract_chat_options(args: dict[str, Any]) -> dict[str, Any]:
    """Pluck the perplexity-chat options the caller actually set.

    Avoids forwarding ``None`` for unset keys so we don't accidentally
    override a server-side default. Booleans are passed through as-is
    so ``disable_search=False`` is distinguishable from "unset"."""
    out: dict[str, Any] = {}
    for k in _PERPLEXITY_CHAT_OPTION_KEYS:
        if k in args and args[k] is not None:
            out[k] = args[k]
    return out


def _list_tools() -> list[Tool]:
    """Return the static tool registry exposed over MCP. Loop 260
    extracted this from the inner closure so the registry can be
    introspected by tests without spinning up an asyncio handler.
    """
    s = {"type": "string"}
    return [
        Tool(
            name="chat",
            description="Free-form chat with Qwen3.6-27B (coding-tuned).",
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": s,
                    "system": s,
                    "temperature": {"type": "number", "default": 0.2},
                },
                "required": ["prompt"],
            },
        ),
        Tool(
            name="complete_code",
            description="Complete a code snippet (optionally with goal).",
            inputSchema={
                "type": "object",
                "properties": {"code": s, "instruction": s},
                "required": ["code"],
            },
        ),
        Tool(
            name="explain_code",
            description="Explain a code snippet in natural language.",
            inputSchema={
                "type": "object",
                "properties": {"code": s},
                "required": ["code"],
            },
        ),
        Tool(
            name="find_bugs",
            description="List bugs/issues/improvements for a file.",
            inputSchema={
                "type": "object",
                "properties": {"path": s, "code": s},
                "required": ["path", "code"],
            },
        ),
        Tool(
            name="propose_fix",
            description="Generate a unified diff fixing a specific issue.",
            inputSchema={
                "type": "object",
                "properties": {"path": s, "code": s, "issue": s},
                "required": ["path", "code", "issue"],
            },
        ),
        Tool(
            name="devils_advocate",
            description="Critique a proposed fix; ACCEPT/REJECT verdict.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": s, "original": s, "diff": s, "issue": s,
                },
                "required": ["path", "original", "diff", "issue"],
            },
        ),
        Tool(
            name="refactor",
            description="Refactor code toward a stated goal.",
            inputSchema={
                "type": "object",
                "properties": {"code": s, "goal": s},
                "required": ["code", "goal"],
            },
        ),
        Tool(
            name="write_tests",
            description="Generate tests for code (framework defaults to pytest).",
            inputSchema={
                "type": "object",
                "properties": {"code": s, "framework": s},
                "required": ["code"],
            },
        ),
        Tool(
            name="summarize_repo",
            description="Summarize a repository given its file tree text.",
            inputSchema={
                "type": "object",
                "properties": {"tree": s},
                "required": ["tree"],
            },
        ),
        Tool(
            name="web_search",
            description=(
                "Search the web via DuckDuckGo HTML. Returns a numbered "
                "list of {title, url, snippet}. No API key required."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": s,
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 25},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="fetch_url",
            description=(
                "Fetch a URL and return its text body (truncated to a "
                "byte cap). Refuses non-text content types."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url": s,
                    "max_bytes": {"type": "integer", "minimum": 1024, "maximum": 5_000_000},
                },
                "required": ["url"],
            },
        ),
        Tool(
            name="read_file",
            description="Read a file from the configured repo root (utf-8, byte-capped).",
            inputSchema={
                "type": "object",
                "properties": {"path": s},
                "required": ["path"],
            },
        ),
        Tool(
            name="list_dir",
            description="List a directory inside the configured repo root.",
            inputSchema={
                "type": "object",
                "properties": {"path": s},
            },
        ),
        Tool(
            name="write_file",
            description="Write a file inside the configured repo root (utf-8).",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": s,
                    "content": s,
                    "create_parents": {"type": "boolean"},
                },
                "required": ["path", "content"],
            },
        ),
        Tool(
            name="apply_patch",
            description=(
                "Apply a unified diff via `git apply`. Set check_only=true "
                "to test applicability without mutating the tree."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "diff": s,
                    "check_only": {"type": "boolean"},
                },
                "required": ["diff"],
            },
        ),
        Tool(
            name="run_shell",
            description=(
                "Run a shell command via /bin/sh inside the configured "
                "repo root. Sandboxed (cwd cannot escape root), wall-clock "
                "capped, and output byte-capped. A built-in deny list "
                "rejects destructive patterns (rm -rf /, dd of=/dev/, "
                "mkfs, fork bombs, curl|sh, etc). Returns a formatted "
                "block with returncode, stdout, stderr, and "
                "truncated/timeout markers."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "cmd": s,
                    "timeout": {"type": "number"},
                    "cwd": s,
                },
                "required": ["cmd"],
            },
        ),
        Tool(
            name="patch_anchor",
            description=(
                "Apply a single anchor-based string replacement to a file "
                "inside the repo root. Replaces exactly one occurrence of "
                "old_str with new_str; rejects 0 or >1 matches. "
                "Complements apply_patch (unified diff) for files not in "
                "git or when only a unique surrounding context is known."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": s,
                    "old_str": s,
                    "new_str": s,
                },
                "required": ["path", "old_str", "new_str"],
            },
        ),
        Tool(
            name="perplexity_search",
            description=(
                "Search the web via the Perplexity Search API. Returns a "
                "ranked list of {title, url, snippet, date, score?}. "
                "Requires PERPLEXITY_API_KEY. For AI-synthesised answers "
                "use perplexity_ask instead. Supports the full SearchCreateParams "
                "surface from the perplexity-py SDK -- recency / domain / "
                "language filters, date filters, academic / SEC search modes."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": s,
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 20},
                    "max_tokens_per_page": {
                        "type": "integer",
                        "minimum": 256,
                        "maximum": 2048,
                    },
                    "max_tokens": {"type": "integer", "minimum": 1},
                    "country": s,
                    "search_mode": {
                        "type": "string",
                        "enum": list(perplexity_tools.VALID_SEARCH_MODE),
                    },
                    "search_recency_filter": {
                        "type": "string",
                        "enum": list(perplexity_tools.VALID_RECENCY),
                    },
                    "search_domain_filter": {"type": "array", "items": s},
                    "search_language_filter": {"type": "array", "items": s},
                    "last_updated_after_filter": s,
                    "last_updated_before_filter": s,
                    "search_after_date_filter": s,
                    "search_before_date_filter": s,
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="perplexity_ask",
            description=(
                "Quick web-grounded Q&A via Perplexity sonar-pro. Accepts a "
                "messages array (role/content) and returns the assistant "
                "reply with a numbered citations footer. Requires "
                "PERPLEXITY_API_KEY. Supports the full perplexity-py SDK "
                "option surface: search filters, date filters, "
                "web_search_options (search_context_size / search_type / "
                "user_location), generation knobs (temperature / top_p / "
                "max_tokens), and structured-output response_format."
            ),
            inputSchema=_perplexity_chat_input_schema(s),
        ),
        Tool(
            name="perplexity_research",
            description=(
                "Deep multi-source research via Perplexity sonar-deep-research "
                "(slow, 30s+). SSE-streamed under the hood. Set "
                "strip_thinking=true to remove <think>...</think> tags. "
                "Requires PERPLEXITY_API_KEY. Accepts the full chat option "
                "surface plus reasoning_effort."
            ),
            inputSchema=_perplexity_chat_input_schema(s, with_strip_thinking=True),
        ),
        Tool(
            name="perplexity_reason",
            description=(
                "Step-by-step reasoning via Perplexity sonar-reasoning-pro. "
                "Best for math, logic, and complex analysis. Set "
                "strip_thinking=true to drop <think>...</think> tags. "
                "Requires PERPLEXITY_API_KEY. Accepts the full chat option "
                "surface."
            ),
            inputSchema=_perplexity_chat_input_schema(s, with_strip_thinking=True),
        ),
        Tool(
            name="perplexity_embed",
            description=(
                "Generate vector embeddings via Perplexity /v1/embeddings. "
                "Accepts a single string or up to 512 strings in one call. "
                "Pick model from "
                + ", ".join(perplexity_tools.EMBED_MODELS)
                + ". Optional dimensions truncates the output (Matryoshka). "
                "Optional encoding_format compresses to base64_int8 / "
                "base64_binary. Requires PERPLEXITY_API_KEY."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "input": {
                        "oneOf": [
                            s,
                            {"type": "array", "items": s, "minItems": 1, "maxItems": 512},
                        ]
                    },
                    "model": {
                        "type": "string",
                        "enum": list(perplexity_tools.EMBED_MODELS),
                    },
                    "dimensions": {"type": "integer", "minimum": 1},
                    "encoding_format": {
                        "type": "string",
                        "enum": list(perplexity_tools.VALID_EMBED_ENCODING),
                    },
                },
                "required": ["input", "model"],
            },
        ),
        Tool(
            name="perplexity_async_create",
            description=(
                "Submit an async chat-completions job. Returns immediately "
                "with {id, status, created_at, ...}; poll with "
                "perplexity_async_get. Use for long-running deep-research "
                "queries that exceed your client timeout. Accepts the full "
                "chat option surface plus optional idempotency_key. "
                "Requires PERPLEXITY_API_KEY."
            ),
            inputSchema=_perplexity_chat_input_schema(
                s, with_async_extras=True
            ),
        ),
        Tool(
            name="perplexity_async_get",
            description=(
                "Poll one async chat-completions job by id. Returns the full "
                "record including status (CREATED / IN_PROGRESS / COMPLETED "
                "/ FAILED) and the assistant response when COMPLETED. "
                "Requires PERPLEXITY_API_KEY."
            ),
            inputSchema={
                "type": "object",
                "properties": {"api_request_id": s},
                "required": ["api_request_id"],
            },
        ),
        Tool(
            name="perplexity_async_list",
            description=(
                "List all async chat-completions jobs. Returns the API's "
                "envelope with one record per job. Requires "
                "PERPLEXITY_API_KEY."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


def _build_server(client: QwenClient | None = None, fs_config: fs_tools.FsConfig | None = None) -> tuple[Server, QwenClient]:
    server: Server = Server("qwen-coder-mcp")
    if client is None:
        client = QwenClient()
    fs_cfg = fs_config or _default_fs_config()

    @server.list_tools()
    async def list_tools() -> list[Tool]:  # type: ignore[override]
        return _list_tools()

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:  # type: ignore[override]
        text = await asyncio.to_thread(_dispatch, client, name, arguments, fs_cfg)
        return [TextContent(type="text", text=text)]

    return server, client


def _dispatch(client: QwenClient, name: str, args: dict[str, Any], fs_cfg: fs_tools.FsConfig | None = None) -> str:
    if name == "chat":
        sys_msg = args.get("system") or prompts.CODER_SYSTEM
        return client.system_user(
            sys_msg,
            str(args["prompt"]),
            temperature=float(args.get("temperature", 0.2)),
        )
    if name == "complete_code":
        return client.system_user(
            prompts.CODER_SYSTEM,
            prompts.complete_user(str(args["code"]), args.get("instruction")),
        )
    if name == "explain_code":
        return client.system_user(
            prompts.CODER_SYSTEM, prompts.explain_user(str(args["code"]))
        )
    if name == "find_bugs":
        return client.system_user(
            prompts.REVIEWER_SYSTEM,
            prompts.find_bugs_user(str(args["path"]), str(args["code"])),
        )
    if name == "propose_fix":
        return client.system_user(
            prompts.CODER_SYSTEM,
            prompts.propose_fix_user(
                str(args["path"]), str(args["code"]), str(args["issue"])
            ),
            temperature=0.1,
        )
    if name == "devils_advocate":
        return client.system_user(
            prompts.DEVILS_ADVOCATE_SYSTEM,
            prompts.devils_advocate_user(
                str(args["path"]),
                str(args["original"]),
                str(args["diff"]),
                str(args["issue"]),
            ),
            temperature=0.0,
        )
    if name == "refactor":
        return client.system_user(
            prompts.CODER_SYSTEM,
            prompts.refactor_user(str(args["code"]), str(args["goal"])),
        )
    if name == "write_tests":
        return client.system_user(
            prompts.CODER_SYSTEM,
            prompts.write_tests_user(
                str(args["code"]), str(args.get("framework") or "pytest")
            ),
        )
    if name == "summarize_repo":
        return client.system_user(
            prompts.CODER_SYSTEM, prompts.summarize_repo_user(str(args["tree"]))
        )
    if name == "web_search":
        try:
            results = web_tools.web_search(
                str(args["query"]),
                max_results=int(args.get("max_results", 5)),
            )
        except (ValueError, Exception) as exc:  # noqa: BLE001
            return f"web_search error: {type(exc).__name__}: {exc}"
        return web_tools.format_search_results(results)
    if name == "fetch_url":
        try:
            res = web_tools.fetch_url(
                str(args["url"]),
                max_bytes=int(args.get("max_bytes", 200_000)),
            )
        except (ValueError, Exception) as exc:  # noqa: BLE001
            return f"fetch_url error: {type(exc).__name__}: {exc}"
        if res.get("error") == "non_text_content":
            return (
                f"fetch_url: refused non-text content "
                f"({res.get('content_type')}) from {res.get('url')}"
            )
        prefix = f"# {res['url']} (status={res['status']}, ct={res['content_type']})\n"
        if res.get("truncated"):
            prefix += "# truncated\n"
        return prefix + str(res.get("text", ""))
    if name in {"read_file", "list_dir", "write_file", "apply_patch", "patch_anchor"}:
        cfg = fs_cfg or _default_fs_config()
        try:
            if name == "read_file":
                res = fs_tools.read_file(cfg, str(args["path"]))
                return fs_tools.format_read(res)
            if name == "list_dir":
                res = fs_tools.list_dir(cfg, str(args.get("path") or "."))
                return fs_tools.format_list(res)
            if name == "write_file":
                res = fs_tools.write_file(
                    cfg,
                    str(args["path"]),
                    str(args["content"]),
                    create_parents=bool(args.get("create_parents", False)),
                )
                return f"wrote {res['path']} ({res['size']} bytes)"
            if name == "apply_patch":
                res = fs_tools.apply_patch(
                    cfg,
                    str(args["diff"]),
                    check_only=bool(args.get("check_only", False)),
                )
                tag = "ok" if res["ok"] else "failed"
                msg = res.get("message") or ""
                kind = "check" if res["check_only"] else "apply"
                return f"{kind}: {tag}\n{msg}"
            if name == "patch_anchor":
                res = fs_tools.patch_anchor(
                    cfg,
                    str(args["path"]),
                    str(args["old_str"]),
                    str(args["new_str"]),
                )
                return (
                    f"patched {res['path']} "
                    f"({res['size_before']} -> {res['size_after']} bytes)"
                )
        except (fs_tools.FsError, Exception) as exc:  # noqa: BLE001
            return f"{name} error: {type(exc).__name__}: {exc}"
    if name == "run_shell":
        cfg = fs_cfg or _default_fs_config()
        cmd = str(args.get("cmd", "")).strip()
        if not cmd:
            return "run_shell error: ValueError: cmd is required"
        try:
            kwargs: dict[str, Any] = {}
            if "timeout" in args and args["timeout"] is not None:
                kwargs["timeout"] = float(args["timeout"])
            if args.get("cwd"):
                kwargs["cwd"] = str(args["cwd"])
            res = shell_tools.run_shell(cfg, cmd, **kwargs)
        except (shell_tools.ShellError, fs_tools.FsError) as exc:
            return f"run_shell error: {type(exc).__name__}: {exc}"
        except Exception as exc:  # noqa: BLE001
            return f"run_shell error: {type(exc).__name__}: {exc}"
        return shell_tools.format_run_result(res)
    if name == "perplexity_search":
        try:
            results = perplexity_tools.perplexity_search(
                str(args["query"]),
                max_results=int(args.get("max_results", 10)),
                max_tokens_per_page=int(args.get("max_tokens_per_page", 1024)),
                max_tokens=(int(args["max_tokens"]) if args.get("max_tokens") else None),
                country=(str(args["country"]) if args.get("country") else None),
                search_mode=args.get("search_mode"),
                search_recency_filter=args.get("search_recency_filter"),
                search_domain_filter=args.get("search_domain_filter"),
                search_language_filter=args.get("search_language_filter"),
                last_updated_after_filter=args.get("last_updated_after_filter"),
                last_updated_before_filter=args.get("last_updated_before_filter"),
                search_after_date_filter=args.get("search_after_date_filter"),
                search_before_date_filter=args.get("search_before_date_filter"),
            )
        except (perplexity_tools.PerplexityError, Exception) as exc:  # noqa: BLE001
            return f"perplexity_search error: {type(exc).__name__}: {exc}"
        return perplexity_tools.format_search_results(results)
    if name in {"perplexity_ask", "perplexity_research", "perplexity_reason"}:
        msgs = args.get("messages")
        # Forward every documented chat option that was actually set on
        # the call. We intentionally drop unset / None keys so the
        # request body stays minimal.
        chat_opts = _extract_chat_options(args)
        try:
            if name == "perplexity_ask":
                return perplexity_tools.perplexity_ask(  # type: ignore[return-value]
                    msgs,  # type: ignore[arg-type]
                    **chat_opts,
                )
            if name == "perplexity_research":
                return perplexity_tools.perplexity_research(  # type: ignore[return-value]
                    msgs,  # type: ignore[arg-type]
                    strip_thinking=bool(args.get("strip_thinking", False)),
                    **chat_opts,
                )
            return perplexity_tools.perplexity_reason(  # type: ignore[return-value]
                msgs,  # type: ignore[arg-type]
                strip_thinking=bool(args.get("strip_thinking", False)),
                **chat_opts,
            )
        except (perplexity_tools.PerplexityError, Exception) as exc:  # noqa: BLE001
            return f"{name} error: {type(exc).__name__}: {exc}"
    if name == "perplexity_embed":
        try:
            res = perplexity_tools.perplexity_embed(
                args["input"],
                model=str(args["model"]),
                dimensions=(int(args["dimensions"]) if args.get("dimensions") else None),
                encoding_format=args.get("encoding_format"),
            )
        except (perplexity_tools.PerplexityError, Exception) as exc:  # noqa: BLE001
            return f"perplexity_embed error: {type(exc).__name__}: {exc}"
        return perplexity_tools.format_embeddings_result(res)
    if name == "perplexity_async_create":
        msgs = args.get("messages")
        chat_opts = _extract_chat_options(args)
        try:
            payload = perplexity_tools.perplexity_async_create(
                msgs,  # type: ignore[arg-type]
                model=str(args["model"]),
                idempotency_key=(
                    str(args["idempotency_key"])
                    if args.get("idempotency_key") else None
                ),
                **chat_opts,
            )
        except (perplexity_tools.PerplexityError, Exception) as exc:  # noqa: BLE001
            return f"perplexity_async_create error: {type(exc).__name__}: {exc}"
        return perplexity_tools.format_async_record(payload)
    if name == "perplexity_async_get":
        try:
            payload = perplexity_tools.perplexity_async_get(
                str(args["api_request_id"])
            )
        except (perplexity_tools.PerplexityError, Exception) as exc:  # noqa: BLE001
            return f"perplexity_async_get error: {type(exc).__name__}: {exc}"
        return perplexity_tools.format_async_record(payload)
    if name == "perplexity_async_list":
        try:
            payload = perplexity_tools.perplexity_async_list()
        except (perplexity_tools.PerplexityError, Exception) as exc:  # noqa: BLE001
            return f"perplexity_async_list error: {type(exc).__name__}: {exc}"
        return perplexity_tools.format_async_list(payload)
    raise ValueError(f"unknown tool: {name}")


async def _run() -> None:
    server, client = _build_server()
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
    finally:
        client.close()


def main(argv: list[str] | None = None) -> None:  # entry point
    """Run the qwen-coder MCP stdio server.

    Accepts ``--help`` / ``--version`` without spinning up the asyncio
    runtime so a user can probe the binary in a shell. Any unknown flag
    yields argparse's standard ``error: unrecognized arguments`` rather
    than being swallowed by the asyncio loop.
    """
    import argparse
    from . import __version__

    parser = argparse.ArgumentParser(
        prog="qwen-coder-mcp",
        description=(
            "qwen-coder-mcp stdio MCP server. Exposes shell, fs, grep, "
            "diff, web search, and chat tools backed by a local Qwen "
            "OpenAI-compatible endpoint to MCP clients."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"qwen-coder-mcp {__version__}",
    )
    parser.parse_args(argv)
    asyncio.run(_run())


if __name__ == "__main__":  # pragma: no cover
    main()
