"""Loop 264: real-model end-to-end benchmark.

Spins up against an already-running vLLM server (defaults to
``http://127.0.0.1:8000``) and drives the actual ``QwenClient`` through
a fixed scenario suite. Captures TTFT, total wall-clock, completion
tokens, tokens/sec, and -- critically for loops 262/263 -- whether any
``rich.errors.MarkupError`` would fire when the assistant reply is fed
through the same TUI render path as a live session.

Why a script and not a pytest case:
- It needs a real model and ~30s+ of wall-clock per scenario; pytest
  would be the wrong place for that.
- Results land in ``.agent/benchmarks/loop-NNN.json`` so successive
  loops can diff perf and catch regressions.
- Keeps the static markup-parser tests (loop 263) for fast CI but
  closes the operator's "load the model and test with that" loop.

Run:
    .venv-serve/bin/python scripts/benchmark_real_model.py \
        --base-url http://127.0.0.1:8000/v1 --tag loop264

"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qwen_coder_mcp.qwen_client import (  # noqa: E402
    ChatMessage,
    QwenClient,
    _strip_think_blocks,
    load_settings,
)
from qwen_coder_mcp import agent_loop, fs_tools, prompts, tui  # noqa: E402


# Scenarios deliberately mix:
#   * a plain coding ask (warmup + tokens-per-second baseline)
#   * a multi-step agent task (exercises run_agent + tool dispatch)
#   * a bracket-heavy prompt (asks the model to emit progress bars,
#     regex-with-brackets, traceback-style output -- the EXACT shape
#     that crashed the TUI in the operator's traceback)
SCENARIOS: list[dict[str, Any]] = [
    {
        "name": "warmup_plain_python",
        "kind": "chat",
        "prompt": (
            "Write a 5-line Python function `fizzbuzz(n)` that prints "
            "FizzBuzz for 1..n. No explanation, just the code."
        ),
        "max_tokens": 256,
    },
    {
        "name": "tokens_per_second_long",
        "kind": "chat",
        "prompt": (
            "Write a Python class `LRUCache` with get/put methods using "
            "an OrderedDict. Include a brief docstring. Output ONLY code."
        ),
        "max_tokens": 600,
    },
    {
        "name": "bracket_heavy_output",
        "kind": "chat",
        "prompt": (
            "Print exactly this text verbatim, character-for-character, "
            "wrapped in ```text fences:\n\n"
            "[INFO] [/▍] progress 50%\n"
            "regex matched: /\\[(.*?)\\]/g\n"
            "Traceback (most recent call last):\n"
            "  File '[/x]', line 1\n"
            "[ERROR] closing tag '[/▍]' does not match\n"
        ),
        "max_tokens": 300,
    },
    {
        "name": "agent_one_step_fs_read",
        "kind": "agent",
        "task": (
            "Read the file README.md (just the first 30 lines) using the "
            "fs_read tool. Then summarise what the project is in 1 sentence."
        ),
        "max_steps": 4,
    },
    # Loop 265 -- write-mode scenario. Exercises fs_write through the
    # full registry with confirmation auto-allowed, so we catch any
    # markup leaks in the write-confirmation preview path AND verify
    # multi-step tool dispatch survives bracket-laden file content.
    {
        "name": "agent_write_bracket_file",
        "kind": "agent",
        "task": (
            "Use the fs_write tool to create a file at "
            "'.agent/benchmarks/_bench_write_probe.txt' with this exact "
            "content:\n"
            "[INFO] [/▍] progress\n"
            "[ERROR] closing tag '[/▍]'\n"
            "Then use fs_read to read it back and confirm in 1 sentence."
        ),
        "max_steps": 5,
        "writes": True,
    },
    # Loop 265 -- run_shell scenario. The operator's earlier failure
    # mode ('run_shell missing'/'run_command not in parser') makes
    # this dispatch path worth gating: alias resolution + bracket-heavy
    # stdout going through the tool-result render path.
    # Loop 265 -- run_shell scenario. The operator's earlier failure
    # mode ('run_shell missing'/'run_command not in parser') makes
    # this dispatch path worth gating: alias resolution + bracket-heavy
    # stdout going through the tool-result render path.
    {
        "name": "agent_run_shell_bracket",
        "kind": "agent",
        "task": (
            "Use the run_shell tool to run exactly: "
            "echo '[INFO] [/▍] progress 50%' && echo '[ERROR] closing tag'\n"
            "Then state in one sentence what the command printed."
        ),
        "max_steps": 4,
        "writes": True,
    },
    # Loop 270 -- fs_regex_edit scenario. Exercises the new (loop 267)
    # whitespace-tolerant regex-edit tool through the full registry
    # with confirmation auto-allowed. Catches: alias resolution at
    # dispatch (`regex_edit` -> `fs_regex_edit`), whitespace-tolerant
    # matching when the model's quoted snippet has different indent
    # from the file, and that the tool participates in the destructive-
    # confirm gate without false-allow.
    {
        "name": "agent_regex_edit_indent_drift",
        "kind": "agent",
        "task": (
            "First use fs_write to create '.agent/benchmarks/_bench_regex_probe.py' "
            "with these exact 3 lines (note the 4-space indent):\n"
            "def greet(name):\n"
            "    print('hello, ' + name)\n"
            "    return None\n\n"
            "Then use fs_regex_edit to change the print line so it says "
            "'HI, ' instead of 'hello, '. Quote the old snippet without "
            "worrying about exact whitespace. Then fs_read the file and "
            "confirm the change in one sentence."
        ),
        "max_steps": 6,
        "writes": True,
    },
]


def _bench_chat(client: QwenClient, prompt: str, max_tokens: int) -> dict[str, Any]:
    """Stream a single chat turn, recording TTFT and total wall-clock.
    Falls back to non-streaming if the client can't stream."""
    # Mirror the TUI/plain-chat path, not a bare user-only request. The
    # coder system prompt is what tells Qwen not to expose reasoning and
    # makes benchmark output comparable with what operators actually see.
    history = [
        ChatMessage(role="system", content=prompts.CODER_SYSTEM),
        ChatMessage(role="user", content=prompt),
    ]
    t0 = time.monotonic()
    ttft: float | None = None
    chunks: list[str] = []
    try:
        for piece in client.chat_stream(history, max_tokens=max_tokens):
            if ttft is None:
                ttft = time.monotonic() - t0
            chunks.append(piece)
        reply = _strip_think_blocks("".join(chunks))
    except Exception as exc:  # noqa: BLE001
        return {
            "error": f"{type(exc).__name__}: {exc}",
            "trace": traceback.format_exc(),
        }
    elapsed = time.monotonic() - t0
    completion_tokens = len(reply.split())  # rough -- vLLM doesn't surface
    return {
        "ttft_s": ttft,
        "wall_s": elapsed,
        "completion_chars": len(reply),
        "completion_words": completion_tokens,
        "words_per_s": (completion_tokens / elapsed) if elapsed > 0 else None,
        "reply_head": reply[:400],
        "markup_safe": _markup_safety_check(reply),
    }


def _bench_agent(
    client: QwenClient,
    task: str,
    max_steps: int,
    *,
    writes: bool = False,
) -> dict[str, Any]:
    """Drive run_agent for a few steps, counting tool calls and
    measuring total wall-clock + final-answer markup safety.

    When ``writes=True`` the full ``ALL_TOOLS`` registry is exposed
    and ``always_allow`` is passed as the confirmation hook so the
    benchmark can exercise destructive paths (fs_write, run_shell)
    without an interactive prompt. Tool-result strings are also
    fed through the markup-safety check so a leak in run_shell
    stdout rendering would surface here.
    """
    cfg = fs_tools.FsConfig(root=ROOT)
    t0 = time.monotonic()
    tool_calls = 0
    final = ""
    chunks = 0
    tool_results: list[str] = []
    kwargs: dict[str, Any] = {
        "client": client,
        "fs_cfg": cfg,
        "max_steps": max_steps,
    }
    if writes:
        kwargs["tools"] = agent_loop.ALL_TOOLS
        kwargs["confirm"] = agent_loop.always_allow
    try:
        for ev in agent_loop.run_agent([], task, **kwargs):
            if ev.kind == "chunk":
                chunks += 1
            elif ev.kind == "tool_call":
                tool_calls += 1
            elif ev.kind == "tool_result":
                tool_results.append(getattr(ev, "text", "") or "")
            elif ev.kind == "final" or ev.kind == "limit":
                final = ev.text
    except Exception as exc:  # noqa: BLE001
        return {
            "error": f"{type(exc).__name__}: {exc}",
            "trace": traceback.format_exc(),
        }
    elapsed = time.monotonic() - t0
    tool_safety = [_markup_safety_check(t) for t in tool_results[:8]]
    return {
        "wall_s": elapsed,
        "tool_calls": tool_calls,
        "stream_chunks": chunks,
        "final_chars": len(final),
        "final_head": final[:400],
        "markup_safe": _markup_safety_check(final),
        "tool_result_markup_safe": tool_safety,
        "tool_result_heads": [t[:200] for t in tool_results[:8]],
    }


def _markup_safety_check(text: str) -> dict[str, Any]:
    """Send the reply through the exact code path the TUI uses to
    render a plain-text assistant message. Returns whether
    ``rich.text.Text.from_markup`` raised, AND whether the loop-263
    ``_safe_log_write`` fallback would have caught it. Both fields are
    needed to confirm the fix:

    * ``raw_raises``: did the unguarded interpolation crash?
    * ``safe_renders``: did the loop-262/263 escape make it survive?

    A model-emitted reply that triggers ``raw_raises=True`` AND
    ``safe_renders=True`` is the smoking-gun proof that loops 262/263
    actually fixed the operator's reported failure.
    """
    try:
        from rich.errors import MarkupError
        from rich.text import Text
    except ImportError:
        return {"checked": False}
    raw_raises = False
    safe_renders = True
    # Path 1: original (broken) interpolation -- f"[green]qwen>[/green] {reply}".
    try:
        Text.from_markup(f"[green]qwen>[/green] {text}")
    except MarkupError:
        raw_raises = True
    except Exception:  # noqa: BLE001
        pass
    # Path 2: loop-262/263 protected interpolation.
    try:
        Text.from_markup(f"[green]qwen>[/green] {tui._safe_markup(text)}")
    except MarkupError:
        safe_renders = False
    except Exception:  # noqa: BLE001
        safe_renders = False
    return {
        "checked": True,
        "raw_would_raise": raw_raises,
        "safe_path_renders": safe_renders,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=os.environ.get("QWEN_BASE_URL", "http://127.0.0.1:8000/v1"))
    ap.add_argument("--api-key", default=os.environ.get("QWEN_API_KEY", "EMPTY"))
    ap.add_argument("--model", default=os.environ.get("QWEN_MODEL"))
    ap.add_argument("--tag", default="loop270")
    ap.add_argument("--out-dir", default=str(ROOT / ".agent" / "benchmarks"))
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{args.tag}.json"

    # QwenClient picks up base_url/api_key/model from env via load_settings.
    os.environ["QWEN_BASE_URL"] = args.base_url
    os.environ["QWEN_API_KEY"] = args.api_key
    if args.model:
        os.environ["QWEN_MODEL"] = args.model
    client = QwenClient()

    print(f"[bench] base_url={args.base_url}")
    print(f"[bench] probing /v1/models ...")
    try:
        check = client.health_check()
    except Exception as exc:  # noqa: BLE001
        print(f"[bench] health probe raised: {exc}")
        return 2
    if not check.get("ok"):
        print(f"[bench] backend not ok: {check}")
        return 2
    print(f"[bench] models: {check.get('models')}")

    # Auto-detect the served model if one wasn't pinned.
    served_model = check.get("models", [None])[0] if check.get("models") else None
    if served_model and not args.model:
        os.environ["QWEN_MODEL"] = served_model
        client = QwenClient()
        print(f"[bench] pinned model -> {served_model}")

    results: list[dict[str, Any]] = []
    for sc in SCENARIOS:
        print(f"\n[bench] >>> {sc['name']} ({sc['kind']})")
        t0 = time.monotonic()
        if sc["kind"] == "chat":
            r = _bench_chat(client, sc["prompt"], sc["max_tokens"])
        else:
            r = _bench_agent(
                client,
                sc["task"],
                sc["max_steps"],
                writes=bool(sc.get("writes")),
            )
        r["scenario"] = sc["name"]
        r["name"] = sc["name"]
        r["kind"] = sc["kind"]
        r["bench_wall_s"] = time.monotonic() - t0
        results.append(r)
        # Per-scenario summary line.
        if "error" in r:
            print(f"[bench]     ERROR: {r['error']}")
        else:
            ttft = r.get("ttft_s")
            wall = r.get("wall_s")
            wps = r.get("words_per_s")
            ms = r.get("markup_safe", {})
            print(
                f"[bench]     wall={wall:.2f}s  ttft={ttft}  "
                f"words/s={wps}  markup_safe={ms}"
            )

    payload = {
        "tag": args.tag,
        "base_url": args.base_url,
        "model": served_model,
        "wall_s": sum((r.get("bench_wall_s") or 0) for r in results),
        "scenarios": results,
        "summary": _summarise(results),
    }
    out_path.write_text(json.dumps(payload, indent=2, default=str))
    print(f"\n[bench] wrote {out_path}")
    print(f"[bench] summary: {payload['summary']}")

    # Exit non-zero on any scenario error so CI / `/loop` notices.
    if any("error" in r for r in results):
        return 1
    # Exit non-zero if any reply would have crashed the unprotected
    # path AND the safe path rendered: this is the smoking-gun proof
    # the fix matters and is working.
    return 0


def _summarise(results: list[dict[str, Any]]) -> dict[str, Any]:
    total_wall = sum((r.get("wall_s") or 0) for r in results)
    chat_results = [r for r in results if r.get("kind") == "chat" and "error" not in r]
    ttfts = [r["ttft_s"] for r in chat_results if r.get("ttft_s") is not None]
    wps_values = [r["words_per_s"] for r in chat_results if r.get("words_per_s") is not None]
    # Loop 265 -- aggregate tool-result markup safety across agent
    # scenarios so a regression in run_shell/fs_write rendering
    # surfaces in the top-level summary.
    tool_safe_total = 0
    tool_safe_raw_would_crash = 0
    tool_safe_path_rendered = 0
    for r in results:
        for ts in r.get("tool_result_markup_safe", []) or []:
            if not ts.get("checked"):
                continue
            tool_safe_total += 1
            if ts.get("raw_would_raise"):
                tool_safe_raw_would_crash += 1
            if ts.get("safe_path_renders"):
                tool_safe_path_rendered += 1
    return {
        "n_scenarios": len(results),
        "n_errors": sum(1 for r in results if "error" in r),
        "total_wall_s": total_wall,
        "median_ttft_s": (sorted(ttfts)[len(ttfts) // 2] if ttfts else None),
        "median_words_per_s": (sorted(wps_values)[len(wps_values) // 2] if wps_values else None),
        "n_replies_unprotected_would_crash": sum(
            1 for r in results
            if r.get("markup_safe", {}).get("raw_would_raise")
        ),
        "n_replies_safe_path_rendered": sum(
            1 for r in results
            if r.get("markup_safe", {}).get("safe_path_renders")
        ),
        "n_tool_results_checked": tool_safe_total,
        "n_tool_results_unprotected_would_crash": tool_safe_raw_would_crash,
        "n_tool_results_safe_path_rendered": tool_safe_path_rendered,
    }


if __name__ == "__main__":
    sys.exit(main())
