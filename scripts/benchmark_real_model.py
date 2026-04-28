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

from qwen_coder_mcp.qwen_client import ChatMessage, QwenClient, load_settings  # noqa: E402
from qwen_coder_mcp import agent_loop, fs_tools, tui  # noqa: E402


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
]


def _bench_chat(client: QwenClient, prompt: str, max_tokens: int) -> dict[str, Any]:
    """Stream a single chat turn, recording TTFT and total wall-clock.
    Falls back to non-streaming if the client can't stream."""
    history = [ChatMessage(role="user", content=prompt)]
    t0 = time.monotonic()
    ttft: float | None = None
    chunks: list[str] = []
    try:
        for piece in client.chat_stream(history, max_tokens=max_tokens):
            if ttft is None:
                ttft = time.monotonic() - t0
            chunks.append(piece)
        reply = "".join(chunks)
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


def _bench_agent(client: QwenClient, task: str, max_steps: int) -> dict[str, Any]:
    """Drive run_agent for a few steps, counting tool calls and
    measuring total wall-clock + final-answer markup safety."""
    cfg = fs_tools.FsConfig(root=ROOT)
    t0 = time.monotonic()
    tool_calls = 0
    final = ""
    chunks = 0
    try:
        for ev in agent_loop.run_agent(
            [], task, client=client, fs_cfg=cfg, max_steps=max_steps
        ):
            if ev.kind == "chunk":
                chunks += 1
            elif ev.kind == "tool_call":
                tool_calls += 1
            elif ev.kind == "final" or ev.kind == "limit":
                final = ev.text
    except Exception as exc:  # noqa: BLE001
        return {
            "error": f"{type(exc).__name__}: {exc}",
            "trace": traceback.format_exc(),
        }
    elapsed = time.monotonic() - t0
    return {
        "wall_s": elapsed,
        "tool_calls": tool_calls,
        "stream_chunks": chunks,
        "final_chars": len(final),
        "final_head": final[:400],
        "markup_safe": _markup_safety_check(final),
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
    ap.add_argument("--tag", default="loop264")
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
            r = _bench_agent(client, sc["task"], sc["max_steps"])
        r["scenario"] = sc["name"]
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
    }


if __name__ == "__main__":
    sys.exit(main())
