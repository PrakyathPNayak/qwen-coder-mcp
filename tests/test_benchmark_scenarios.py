"""Loop 265 -- gate the real-model benchmark's scenario list and
``_bench_agent`` write-mode wiring without spinning up vLLM.

These tests don't load Qwen. They lock in:

* the bench script exposes the bracket-heavy chat scenario AND new
  write+run-shell scenarios so future loops can't silently drop them;
* ``_bench_agent(..., writes=True)`` actually wires ``ALL_TOOLS``
  + ``always_allow`` into ``run_agent`` so destructive paths aren't
  bypassed by the read-only default registry;
* ``_summarise`` aggregates tool-result markup safety so a leak in
  run_shell stdout rendering would show up in the JSON summary.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "benchmark_real_model.py"


@pytest.fixture(scope="module")
def bench_module():
    """Load scripts/benchmark_real_model.py as a module without running main."""
    spec = importlib.util.spec_from_file_location("_bench_real", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_bench_real"] = mod
    spec.loader.exec_module(mod)
    return mod


class TestScenarioInventory:
    def test_bracket_heavy_present(self, bench_module):
        names = {sc["name"] for sc in bench_module.SCENARIOS}
        assert "bracket_heavy_output" in names

    def test_write_scenario_present(self, bench_module):
        names = {sc["name"] for sc in bench_module.SCENARIOS}
        assert "agent_write_bracket_file" in names

    def test_run_shell_scenario_present(self, bench_module):
        names = {sc["name"] for sc in bench_module.SCENARIOS}
        assert "agent_run_shell_bracket" in names

    def test_regex_edit_scenario_present(self, bench_module):
        # Loop 270: ensures the fs_regex_edit (loop 267) exercise is wired
        # into the bench so future loops can't silently drop coverage of
        # the whitespace-tolerant edit path.
        names = {sc["name"] for sc in bench_module.SCENARIOS}
        assert "agent_regex_edit_indent_drift" in names

    def test_regex_edit_scenario_is_writes_enabled(self, bench_module):
        for sc in bench_module.SCENARIOS:
            if sc["name"] == "agent_regex_edit_indent_drift":
                assert sc.get("writes") is True
                assert sc.get("kind") == "agent"
                # Task must mention the tool name so the model is steered
                # toward fs_regex_edit (and not just fs_edit).
                assert "fs_regex_edit" in sc["task"]
                return
        pytest.fail("agent_regex_edit_indent_drift scenario missing")

    def test_write_scenarios_marked_writes(self, bench_module):
        for sc in bench_module.SCENARIOS:
            if sc["name"] in (
                "agent_write_bracket_file",
                "agent_run_shell_bracket",
                "agent_regex_edit_indent_drift",
            ):
                assert sc.get("writes") is True, f"{sc['name']} missing writes=True"
            elif sc["kind"] == "agent":
                assert not sc.get("writes"), f"{sc['name']} should not be writes-enabled"


class TestBenchAgentWiring:
    def test_bench_chat_uses_coder_system_prompt(self, bench_module):
        captured: dict[str, Any] = {}

        class _Client:
            def chat_stream(self, history, **kwargs):
                captured["history"] = history
                captured["kwargs"] = kwargs
                yield "ok"

        out = bench_module._bench_chat(_Client(), "write code", 123)
        assert "error" not in out
        hist = captured["history"]
        assert hist[0].role == "system"
        assert "TOOLS YOU CAN CALL" in hist[0].content
        assert hist[1].role == "user"
        assert hist[1].content == "write code"
        assert captured["kwargs"]["max_tokens"] == 123

    def test_bench_chat_reports_final_sanitized_stream(self, bench_module):
        class _Client:
            def chat_stream(self, history, **kwargs):
                yield "hidden analysis"
                yield "</think>\n\n"
                yield "visible final"

        out = bench_module._bench_chat(_Client(), "write code", 123)
        assert out["reply_head"] == "visible final"
        assert out["completion_chars"] == len("visible final")

    def test_writes_true_passes_all_tools_and_always_allow(self, bench_module, monkeypatch):
        captured: dict[str, Any] = {}

        def fake_run_agent(
            history: list, user_text: str, **kwargs: Any
        ) -> Iterator[Any]:
            captured["kwargs"] = kwargs
            captured["task"] = user_text
            yield SimpleNamespace(kind="final", text="done")

        monkeypatch.setattr(bench_module.agent_loop, "run_agent", fake_run_agent)
        out = bench_module._bench_agent(
            client=None, task="do thing", max_steps=3, writes=True
        )
        assert "error" not in out
        kw = captured["kwargs"]
        assert kw.get("tools") is bench_module.agent_loop.ALL_TOOLS
        assert kw.get("confirm") is bench_module.agent_loop.always_allow
        assert kw.get("max_steps") == 3

    def test_writes_false_omits_destructive_kwargs(self, bench_module, monkeypatch):
        captured: dict[str, Any] = {}

        def fake_run_agent(history, user_text, **kwargs):
            captured["kwargs"] = kwargs
            yield SimpleNamespace(kind="final", text="done")

        monkeypatch.setattr(bench_module.agent_loop, "run_agent", fake_run_agent)
        bench_module._bench_agent(
            client=None, task="read", max_steps=2, writes=False
        )
        kw = captured["kwargs"]
        assert "tools" not in kw
        assert "confirm" not in kw

    def test_tool_result_markup_safety_collected(self, bench_module, monkeypatch):
        def fake_run_agent(history, user_text, **kwargs):
            # Bracket-heavy stdout that would crash an unguarded RichLog
            # interpolation and must round-trip the safe path cleanly.
            yield SimpleNamespace(
                kind="tool_result",
                tool="run_shell",
                text="[INFO] [/▍] progress 50%\n[ERROR] closing tag",
                latency_s=0.01,
            )
            yield SimpleNamespace(kind="final", text="ran")

        monkeypatch.setattr(bench_module.agent_loop, "run_agent", fake_run_agent)
        out = bench_module._bench_agent(
            client=None, task="run", max_steps=2, writes=True
        )
        ts = out["tool_result_markup_safe"]
        assert len(ts) == 1
        assert ts[0]["checked"] is True
        assert ts[0]["raw_would_raise"] is True
        assert ts[0]["safe_path_renders"] is True


class TestSummariseAggregation:
    def test_tool_result_safety_rolls_up(self, bench_module):
        results = [
            {
                "kind": "agent",
                "wall_s": 1.0,
                "markup_safe": {"checked": True, "raw_would_raise": False, "safe_path_renders": True},
                "tool_result_markup_safe": [
                    {"checked": True, "raw_would_raise": True, "safe_path_renders": True},
                    {"checked": True, "raw_would_raise": False, "safe_path_renders": True},
                ],
            },
            {
                "kind": "chat",
                "wall_s": 0.5,
                "ttft_s": 0.05,
                "words_per_s": 20.0,
                "markup_safe": {"checked": True, "raw_would_raise": True, "safe_path_renders": True},
            },
        ]
        s = bench_module._summarise(results)
        assert s["n_scenarios"] == 2
        assert s["n_replies_unprotected_would_crash"] == 1
        assert s["n_replies_safe_path_rendered"] == 2
        assert s["n_tool_results_checked"] == 2
        assert s["n_tool_results_unprotected_would_crash"] == 1
        assert s["n_tool_results_safe_path_rendered"] == 2

    def test_summarise_handles_no_tool_results(self, bench_module):
        results = [
            {
                "kind": "chat",
                "wall_s": 0.5,
                "ttft_s": 0.05,
                "words_per_s": 20.0,
                "markup_safe": {"checked": True, "raw_would_raise": False, "safe_path_renders": True},
            }
        ]
        s = bench_module._summarise(results)
        assert s["n_tool_results_checked"] == 0
        assert s["n_tool_results_unprotected_would_crash"] == 0
