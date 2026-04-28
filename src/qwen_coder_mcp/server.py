"""MCP server exposing Qwen3.6-27B coding tools over stdio."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .qwen_client import ChatMessage, QwenClient
from . import prompts


def _build_server(client: QwenClient | None = None) -> tuple[Server, QwenClient]:
    server: Server = Server("qwen-coder-mcp")
    if client is None:
        client = QwenClient()

    @server.list_tools()
    async def list_tools() -> list[Tool]:  # type: ignore[override]
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
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:  # type: ignore[override]
        text = await asyncio.to_thread(_dispatch, client, name, arguments)
        return [TextContent(type="text", text=text)]

    return server, client


def _dispatch(client: QwenClient, name: str, args: dict[str, Any]) -> str:
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


def main() -> None:  # entry point
    asyncio.run(_run())


if __name__ == "__main__":  # pragma: no cover
    main()
