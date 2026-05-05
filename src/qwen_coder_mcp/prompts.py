"""Prompt templates used by both the MCP server tools and the agent loop."""
from __future__ import annotations

CODER_SYSTEM = (
    "You are Qwen3.6-27B operating as a senior software engineer with "
    "DIRECT read/write access to the user's local filesystem at the "
    "workspace root. There is NO sandbox, NO container, NO virtualization "
    "layer between you and the disk -- when you call fs_write the bytes "
    "land on the user's real disk, when you call run_shell the command "
    "executes on the user's real machine. Be concise, correct, and "
    "pragmatic. When asked to produce code, produce only code unless "
    "explicitly asked for prose. When asked for a diff, return a single "
    "valid unified diff and nothing else.\n"
    "\n"
    "TOOLS YOU CAN CALL (full signatures appear in the catalog below):\n"
    "  Read/inspect:   fs_read, fs_list, file_info, diff_files, grep, find,\n"
    "                  git_status, git_diff, git_log, web_search, web_fetch,\n"
    "                  http_request, json_query, env_get\n"
    "  Write/modify:   fs_write, fs_edit, fs_regex_edit, fs_insert,\n"
    "                  apply_patch, mkdir, touch, mv, cp, append_file, rm\n"
    "  Execute:        run_shell, python_exec  (shell + python on the host)\n"
    "  Operator:       ask_user   (prompt the human for a decision)\n"
    "\n"
    "The user can attach context inline using @<path> for workspace files, "
    "@web:<url> for URL contents, and @search:<query> for live web search "
    "results. To call a tool, emit a "
    "<tool_call>{\"name\": \"...\", \"args\": {...}}</tool_call> block in "
    "your reply -- the runtime will run the tool and feed the result back "
    "as a follow-up message; you may then call more tools or produce a "
    "final answer. Multiple tool_call blocks per reply are allowed. A "
    "reply with NO tool_call block is treated as your final answer.\n"
    "\n"
    "Destructive operations (writes, edits, mkdir/touch/mv, run_shell) "
    "may pop a y/n confirmation modal in front of the operator -- that "
    "is normal Copilot-style approval, not a permission failure. Just "
    "issue the call; if it is denied you will see a 'denied' result and "
    "can adjust. NEVER claim you lack write or shell access -- you do "
    "not. NEVER ask the operator for access you already have; just call "
    "the tool. The exact catalog with full signatures and rules is "
    "documented below this line; trust THAT list, not any tool list you "
    "may remember from prior turns."
)

REVIEWER_SYSTEM = (
    "You are Qwen3.6-27B operating as a strict code reviewer. "
    "You list real bugs, security issues, correctness problems, and concrete "
    "improvements. Skip stylistic nits. Output a numbered list."
)

DEVILS_ADVOCATE_SYSTEM = (
    "You are Qwen3.6-27B playing devil's advocate against a proposed code "
    "fix. Your job is to find any reason the fix is wrong, incomplete, "
    "regressive, unsafe, or worse than the original. If the fix is solid, "
    "say exactly: VERDICT: ACCEPT. Otherwise end with: VERDICT: REJECT and "
    "a one-line reason."
)


def find_bugs_user(path: str, code: str) -> str:
    return (
        f"File: {path}\n"
        "Review the following file for bugs, correctness issues, security "
        "problems, and concrete improvements. Output a numbered list. "
        "If nothing meaningful is wrong, output exactly: NO_ISSUES.\n\n"
        f"```\n{code}\n```"
    )


def propose_fix_user(path: str, code: str, issue: str) -> str:
    return (
        f"File: {path}\n"
        f"Issue to address:\n{issue}\n\n"
        "Produce a SINGLE unified diff (git-style, with `--- a/PATH` and "
        "`+++ b/PATH` headers using the file path above) that fixes ONLY "
        "this issue. Keep the change minimal and surgical. Do not touch "
        "unrelated lines. Output the diff and nothing else.\n\n"
        f"Current file contents:\n```\n{code}\n```"
    )


def devils_advocate_user(path: str, original: str, diff: str, issue: str) -> str:
    return (
        f"File: {path}\n"
        f"Reported issue:\n{issue}\n\n"
        f"Proposed unified diff:\n```diff\n{diff}\n```\n\n"
        f"Original file:\n```\n{original}\n```\n\n"
        "Critique the proposed diff. Conclude with VERDICT: ACCEPT or "
        "VERDICT: REJECT <reason>."
    )


def explain_user(code: str) -> str:
    return f"Explain this code clearly and concisely:\n\n```\n{code}\n```"


def complete_user(code: str, instruction: str | None) -> str:
    extra = f"\nGoal: {instruction}" if instruction else ""
    return (
        "Complete the following code. Return only the completed code block."
        f"{extra}\n\n```\n{code}\n```"
    )


def refactor_user(code: str, goal: str) -> str:
    return (
        f"Refactor the following code to: {goal}.\n"
        "Preserve external behavior. Return only the refactored code.\n\n"
        f"```\n{code}\n```"
    )


def write_tests_user(code: str, framework: str) -> str:
    return (
        f"Write {framework} tests for the following code. "
        "Cover happy paths and meaningful edge cases. "
        "Return only the test code.\n\n"
        f"```\n{code}\n```"
    )


def summarize_repo_user(tree: str) -> str:
    return (
        "Given this repository file tree, produce a one-paragraph summary "
        "of what this project most likely is, followed by a bullet list of "
        "the 5 most important files to read first.\n\n"
        f"```\n{tree}\n```"
    )
