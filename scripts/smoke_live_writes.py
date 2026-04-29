"""Loop 285: live-vLLM smoke test that the model believes it can write.

Connects to the already-running vLLM server (``--base-url``,
default ``http://127.0.0.1:8000/v1``), assembles the EXACT same
``(history, system, fs_cfg, tools)`` the TUI does for an agent turn
with writes enabled (loop 283 default), then asks the real Qwen3.6
to:

  1. ``fs_write`` a file under a temp workspace root.
  2. ``run_shell`` ``ls -la`` on it.

After ``run_agent`` returns, the script asserts the file actually
exists on disk -- proving end-to-end that the prompt rewrite from
loop 285 lifts the model out of "I'm read-only" hallucination and
that the local filesystem is genuinely writable through the tool
catalog (no sandbox abstraction, per the operator's instruction).

Run:
    .venv-serve/bin/python scripts/smoke_live_writes.py \\
        --base-url http://127.0.0.1:8000/v1 --tag loop285

Exit 0 iff the file was created. Logs each agent event to stdout so
the operator can read what the model decided to call.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qwen_coder_mcp import agent_loop, fs_tools, prompts  # noqa: E402
from qwen_coder_mcp.qwen_client import (  # noqa: E402
    ChatMessage,
    QwenClient,
)
from qwen_coder_mcp.config import load_settings  # noqa: E402


PROOF_NAME = "live_smoke_proof.md"
PROOF_BODY = "loop285 was here"


def _build_writable_registry() -> dict[str, agent_loop.ToolFn]:
    """Mirror the TUI's loop-283 default: writes ON, all tools available."""
    reg: dict[str, agent_loop.ToolFn] = dict(agent_loop.DEFAULT_TOOLS)
    # Fold in every WRITE_TOOLS entry so fs_write/run_shell/etc. are exposed.
    for name in getattr(agent_loop, "WRITE_TOOLS", ()):
        fn = getattr(agent_loop, "ALL_TOOLS", {}).get(name)
        if fn is not None:
            reg[name] = fn
    # Fold in shell + everything in ALL_TOOLS so the model has the same
    # surface area the TUI grants under /allow_all + agent_write_default.
    for name, fn in getattr(agent_loop, "ALL_TOOLS", {}).items():
        reg.setdefault(name, fn)
    return reg


def _auto_confirm(_call: Any) -> bool:
    """Stand-in for the TUI's /allow_all sticky path: green-light all writes."""
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.environ.get("QWEN_BASE_URL", "http://127.0.0.1:8000/v1"),
    )
    parser.add_argument(
        "--api-key", default=os.environ.get("QWEN_API_KEY", "EMPTY"),
    )
    parser.add_argument(
        "--model", default=os.environ.get("QWEN_MODEL", "qwen3.6-27b"),
    )
    parser.add_argument("--tag", default=f"loop285-{int(time.time())}")
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument(
        "--out",
        default=str(ROOT / ".agent" / "benchmarks" / "live-writes-{tag}.json"),
    )
    args = parser.parse_args()

    workspace = Path(tempfile.mkdtemp(prefix=f"{args.tag}-"))
    proof = workspace / PROOF_NAME

    # Apply CLI overrides via env so load_settings() picks them up.
    os.environ["QWEN_BASE_URL"] = args.base_url
    os.environ["QWEN_API_KEY"] = args.api_key
    os.environ["QWEN_MODEL"] = args.model
    settings = load_settings()
    client = QwenClient(settings)
    fs_cfg = fs_tools.FsConfig(root=workspace)
    tools = _build_writable_registry()

    user_text = (
        f"Create a file at the workspace root named {PROOF_NAME!r} "
        f"with exactly this content (no trailing newline, no quotes): "
        f"{PROOF_BODY!r}. Then run a shell command to ls -la that file. "
        "Use the tools you have. Do not ask the operator -- just do it."
    )

    history: list[ChatMessage] = []
    events: list[dict[str, Any]] = []
    started = time.time()

    print(f"[smoke] workspace: {workspace}")
    print(f"[smoke] base_url:  {args.base_url}")
    print(f"[smoke] model:     {args.model}")
    print(f"[smoke] tools:     {sorted(tools.keys())}")
    print(f"[smoke] tag:       {args.tag}")
    print()
    print("=== USER TURN ===")
    print(user_text)
    print()

    final_text = ""
    try:
        for ev in agent_loop.run_agent(
            history,
            user_text,
            client=client,
            fs_cfg=fs_cfg,
            tools=tools,
            max_steps=args.max_steps,
            stream=False,
            confirm=_auto_confirm,
        ):
            row = {"kind": ev.kind}
            if getattr(ev, "tool", None):
                row["tool"] = ev.tool
            if getattr(ev, "args", None):
                row["args"] = ev.args
            text = getattr(ev, "text", "") or ""
            if text:
                row["text_head"] = text[:400]
            events.append(row)
            print(f"[event] {row}")
            if ev.kind == "final":
                final_text = text
            if ev.kind == "limit":
                print("[smoke] hit max_steps limit")
    except Exception:
        print("[smoke] run_agent raised:")
        traceback.print_exc()

    elapsed = time.time() - started
    proof_exists = proof.exists()
    proof_body = proof.read_text(errors="replace") if proof_exists else ""

    record = {
        "tag": args.tag,
        "workspace": str(workspace),
        "proof_path": str(proof),
        "proof_exists": proof_exists,
        "proof_body": proof_body,
        "proof_body_matches": PROOF_BODY in proof_body,
        "elapsed_s": elapsed,
        "events": events,
        "final_text_head": final_text[:1200],
        "system_prompt_advertises_fs_write": "fs_write" in prompts.CODER_SYSTEM,
        "system_prompt_advertises_run_shell": "run_shell" in prompts.CODER_SYSTEM,
    }

    out_path = Path(args.out.format(tag=args.tag))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(record, indent=2, default=str))

    print()
    print(f"[smoke] elapsed:        {elapsed:.2f}s")
    print(f"[smoke] proof exists:   {proof_exists}")
    print(f"[smoke] proof contents: {proof_body!r}")
    print(f"[smoke] record written: {out_path}")

    return 0 if proof_exists else 2


if __name__ == "__main__":
    sys.exit(main())
