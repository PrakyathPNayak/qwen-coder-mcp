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


def _default_fs_config() -> fs_tools.FsConfig:
    root_str = os.environ.get("QWEN_MCP_FS_ROOT") or os.getcwd()
    return fs_tools.FsConfig(root=Path(root_str))


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
    if name in {"read_file", "list_dir", "write_file", "apply_patch"}:
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
