"""Loop 112: tests for `agent.timing_analyze` CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _write_log(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )


class TestParseRecords:
    def test_parses_well_formed_lines(self):
        from agent.timing_analyze import parse_records
        recs = parse_records(['{"a": 1}', '{"b": 2}'])
        assert recs == [{"a": 1}, {"b": 2}]

    def test_skips_blank_lines(self):
        from agent.timing_analyze import parse_records
        assert parse_records(["", "  ", '{"a": 1}', "\n"]) == [{"a": 1}]

    def test_skips_malformed_json(self):
        from agent.timing_analyze import parse_records
        assert parse_records(['{"a": 1}', "not-json", '{"b":', '{"c": 3}']) == [
            {"a": 1},
            {"c": 3},
        ]

    def test_skips_non_dict_payloads(self):
        from agent.timing_analyze import parse_records
        # Lists, strings, numbers are valid JSON but not dicts.
        assert parse_records(["[1,2,3]", '"hello"', "42", '{"d": 4}']) == [
            {"d": 4}
        ]


class TestAnalyze:
    def test_groups_by_category_and_phase(self):
        from agent.timing_analyze import analyze
        recs = [
            {"category": "applied", "wall_s": 10.0, "phases": {"find_bugs": 3.0, "discovery": 0.1}},
            {"category": "applied", "wall_s": 20.0, "phases": {"find_bugs": 5.0, "discovery": 0.2}},
            {"category": "clean", "wall_s": 8.0, "phases": {"find_bugs": 2.0}},
        ]
        out = analyze(recs)
        assert out["total_records"] == 3
        assert out["category_counts"] == {"applied": 2, "clean": 1}
        assert out["category_wall_s"]["applied"]["count"] == 2
        assert out["category_wall_s"]["applied"]["total"] == 30.0
        assert out["phase_wall_s"]["find_bugs"]["count"] == 3
        assert out["phase_wall_s"]["discovery"]["count"] == 2

    def test_records_without_wall_s_still_counted(self):
        from agent.timing_analyze import analyze
        recs = [
            {"category": "no_candidate_files", "phases": {}},
            {"category": "no_candidate_files", "phases": {}},
            {"category": "applied", "wall_s": 5.0, "phases": {"find_bugs": 1.0}},
        ]
        out = analyze(recs)
        assert out["category_counts"]["no_candidate_files"] == 2
        assert out["category_wall_s"].get("no_candidate_files", {"count": 0})["count"] == 0

    def test_empty_input_safe(self):
        from agent.timing_analyze import analyze
        out = analyze([])
        assert out["total_records"] == 0
        assert out["category_counts"] == {}
        assert out["category_wall_s"] == {}
        assert out["phase_wall_s"] == {}

    def test_quantile_p95_basic(self):
        from agent.timing_analyze import _quantile
        # 100 values 0..99, p95 should be ~94.05 (linear interp).
        vals = [float(i) for i in range(100)]
        assert 94.0 <= _quantile(vals, 0.95) <= 95.0

    def test_quantile_single_value(self):
        from agent.timing_analyze import _quantile
        assert _quantile([7.0], 0.5) == 7.0
        assert _quantile([7.0], 0.95) == 7.0

    def test_quantile_empty(self):
        from agent.timing_analyze import _quantile
        assert _quantile([], 0.5) == 0.0


class TestFormatReport:
    def test_text_report_mentions_categories_and_phases(self):
        from agent.timing_analyze import analyze, format_report
        recs = [
            {"category": "applied", "wall_s": 1.0, "phases": {"find_bugs": 0.5}},
        ]
        report = analyze(recs)
        text = format_report(report)
        assert "applied" in text
        assert "find_bugs" in text
        assert "1 records" in text


class TestCli:
    def test_cli_text_output_against_real_file(self, tmp_path):
        from agent.timing_analyze import main
        log = tmp_path / "timing.log"
        _write_log(log, [
            {"category": "applied", "wall_s": 1.5, "phases": {"find_bugs": 0.5}},
            {"category": "clean", "wall_s": 0.8, "phases": {"find_bugs": 0.4}},
        ])
        rc = main(["--file", str(log)])
        assert rc == 0

    def test_cli_json_output(self, tmp_path, capsys):
        from agent.timing_analyze import main
        log = tmp_path / "timing.log"
        _write_log(log, [
            {"category": "applied", "wall_s": 1.5, "phases": {"find_bugs": 0.5}},
        ])
        rc = main(["--file", str(log), "--json"])
        assert rc == 0
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert parsed["total_records"] == 1
        assert "applied" in parsed["category_counts"]

    def test_cli_missing_file_returns_nonzero(self, tmp_path):
        from agent.timing_analyze import main
        rc = main(["--file", str(tmp_path / "nope.log")])
        assert rc == 1

    def test_cli_handles_partially_corrupt_log(self, tmp_path):
        """Real-world: a rotation race may leave a half-written final
        line. Parser must skip it and process the rest."""
        from agent.timing_analyze import main, parse_records
        log = tmp_path / "timing.log"
        log.write_text(
            json.dumps({"category": "applied", "wall_s": 1.0, "phases": {}}) + "\n"
            + '{"category": "broken'  # truncated
            + "\n"
            + json.dumps({"category": "clean", "wall_s": 0.5, "phases": {}}) + "\n",
            encoding="utf-8",
        )
        recs = parse_records(log.read_text("utf-8").splitlines())
        assert len(recs) == 2
        rc = main(["--file", str(log)])
        assert rc == 0


class TestWallSDeltaPhasesAnalysis:
    """Loop 113: analyze() collects `wall_s_delta_phases` across all
    records that emit it -- high p95 flags iterations where
    unaccounted-for time dominates."""

    def test_collects_delta_phases_field(self):
        from agent.timing_analyze import analyze
        recs = [
            {"category": "applied", "wall_s": 10.0, "phases": {"find_bugs": 5.0}, "wall_s_delta_phases": 0.5},
            {"category": "applied", "wall_s": 12.0, "phases": {"find_bugs": 6.0}, "wall_s_delta_phases": 1.5},
            {"category": "clean", "wall_s": 8.0, "phases": {}, "wall_s_delta_phases": 8.0},
        ]
        out = analyze(recs)
        d = out["wall_s_delta_phases"]
        assert d["count"] == 3
        assert d["total"] == 10.0
        assert d["p95"] >= 1.5

    def test_records_without_delta_phases_excluded(self):
        from agent.timing_analyze import analyze
        recs = [
            {"category": "no_candidate_files", "phases": {}},
            {"category": "applied", "wall_s": 5.0, "phases": {"find_bugs": 1.0}, "wall_s_delta_phases": 0.2},
        ]
        out = analyze(recs)
        assert out["wall_s_delta_phases"]["count"] == 1

    def test_empty_input_returns_zero_delta_summary(self):
        from agent.timing_analyze import analyze
        out = analyze([])
        d = out["wall_s_delta_phases"]
        assert d == {"count": 0, "total": 0.0, "mean": 0.0, "p50": 0.0, "p95": 0.0}

    def test_format_report_mentions_delta_when_present(self):
        from agent.timing_analyze import analyze, format_report
        recs = [
            {"category": "applied", "wall_s": 10.0, "phases": {"find_bugs": 5.0}, "wall_s_delta_phases": 0.5},
        ]
        text = format_report(analyze(recs))
        assert "wall_s_delta_phases" in text
        assert "p95" in text

    def test_format_report_says_no_records_when_absent(self):
        from agent.timing_analyze import analyze, format_report
        recs = [
            {"category": "no_candidate_files", "phases": {}},
        ]
        text = format_report(analyze(recs))
        assert "wall_s_delta_phases: no records emit this field" in text


class TestReadmeMentionsAnalyzer:
    """Loop 114: the analyzer module must be discoverable via the
    README, not just the loop log. If a future commit removes the
    README block, this audit fires."""

    def test_readme_documents_module_invocation(self):
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text("utf-8")
        assert "python -m agent.timing_analyze" in readme
        assert "Analysing timing.log" in readme

    def test_readme_mentions_json_flag(self):
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text("utf-8")
        assert "--json" in readme

    def test_readme_mentions_file_flag(self):
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text("utf-8")
        assert "--file" in readme

    def test_readme_mentions_no_rotated_flag(self):
        """Loop 117: the `--no-rotated` flag (loop 115) must be
        documented or operators won't know rotated-slot ingestion is
        opt-out-able."""
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text("utf-8")
        assert "--no-rotated" in readme


class TestRotatedLogAggregation:
    """Loop 115: rotation produces `<file>.1`. The CLI should ingest
    both slots by default so analytics span the full retained
    history. `--no-rotated` opts out for use cases that want only
    the current slot."""

    def test_includes_rotated_slot_by_default(self, tmp_path):
        from agent.timing_analyze import main
        log = tmp_path / "timing.log"
        rotated = tmp_path / "timing.log.1"
        _write_log(rotated, [{"category": "applied", "wall_s": 1.0, "phases": {}}])
        _write_log(log, [{"category": "clean", "wall_s": 2.0, "phases": {}}])
        rc = main(["--file", str(log), "--json"])
        assert rc == 0

    def test_no_rotated_flag_skips_rotation_slot(self, tmp_path, capsys):
        from agent.timing_analyze import main
        log = tmp_path / "timing.log"
        rotated = tmp_path / "timing.log.1"
        _write_log(rotated, [{"category": "applied", "wall_s": 1.0, "phases": {}}])
        _write_log(log, [{"category": "clean", "wall_s": 2.0, "phases": {}}])
        rc = main(["--file", str(log), "--json", "--no-rotated"])
        assert rc == 0
        report = json.loads(capsys.readouterr().out)
        assert report["total_records"] == 1
        assert "applied" not in report["category_counts"]

    def test_default_includes_both(self, tmp_path, capsys):
        from agent.timing_analyze import main
        log = tmp_path / "timing.log"
        rotated = tmp_path / "timing.log.1"
        _write_log(rotated, [{"category": "applied", "wall_s": 1.0, "phases": {}}])
        _write_log(log, [{"category": "clean", "wall_s": 2.0, "phases": {}}])
        rc = main(["--file", str(log), "--json"])
        assert rc == 0
        report = json.loads(capsys.readouterr().out)
        assert report["total_records"] == 2
        assert report["category_counts"]["applied"] == 1
        assert report["category_counts"]["clean"] == 1

    def test_resolve_inputs_chronological_order(self, tmp_path):
        from agent.timing_analyze import _resolve_inputs
        import os, time
        log = tmp_path / "timing.log"
        rotated = tmp_path / "timing.log.1"
        log.write_text("a\n")
        rotated.write_text("b\n")
        # Force rotated to have older mtime.
        old = time.time() - 1000
        os.utime(rotated, (old, old))
        out = _resolve_inputs(log, include_rotated=True)
        assert out[0] == rotated and out[-1] == log

    def test_missing_rotated_slot_is_silent(self, tmp_path):
        from agent.timing_analyze import _resolve_inputs
        log = tmp_path / "timing.log"
        log.write_text("a\n")
        out = _resolve_inputs(log, include_rotated=True)
        assert out == [log]

    def test_missing_both_returns_empty(self, tmp_path):
        from agent.timing_analyze import _resolve_inputs
        log = tmp_path / "timing.log"
        out = _resolve_inputs(log, include_rotated=True)
        assert out == []


class TestPerCategoryDeltaPhases:
    """Loop 118: `wall_s_delta_phases` broken down by category --
    high delta on `applied` is more concerning than on `clean`."""

    def test_collects_per_category_delta(self):
        from agent.timing_analyze import analyze
        recs = [
            {"category": "applied", "wall_s": 10.0, "phases": {"find_bugs": 5.0}, "wall_s_delta_phases": 0.5},
            {"category": "applied", "wall_s": 12.0, "phases": {"find_bugs": 6.0}, "wall_s_delta_phases": 1.5},
            {"category": "clean", "wall_s": 8.0, "phases": {}, "wall_s_delta_phases": 8.0},
        ]
        out = analyze(recs)
        per = out["category_wall_s_delta_phases"]
        assert per["applied"]["count"] == 2
        assert per["clean"]["count"] == 1
        assert per["clean"]["p95"] == 8.0

    def test_format_report_includes_per_category_delta(self):
        from agent.timing_analyze import analyze, format_report
        recs = [
            {"category": "applied", "wall_s": 10.0, "phases": {"find_bugs": 5.0}, "wall_s_delta_phases": 0.5},
        ]
        text = format_report(analyze(recs))
        assert "wall_s_delta_phases by category" in text

    def test_no_per_category_block_when_empty(self):
        from agent.timing_analyze import analyze, format_report
        text = format_report(analyze([]))
        assert "wall_s_delta_phases by category" not in text


class TestTopNFlag:
    """Loop 124: `--top-n` limits the per-phase block to the N phases
    with highest p95 wall-clock so high-volume timing logs don't drown
    the user in cosmetic detail when triaging slow phases."""

    def test_top_n_keeps_only_n_phases(self):
        from agent.timing_analyze import analyze, format_report
        recs = [
            {"category": "applied", "wall_s": 1.0, "phases": {"a": 0.1, "b": 5.0, "c": 0.5, "d": 9.0}, "wall_s_delta_phases": 0.0},
        ]
        text = format_report(analyze(recs), top_n=2)
        assert "  d " in text
        assert "  b " in text
        assert "  a " not in text
        assert "  c " not in text
        assert "top 2" in text

    def test_top_n_none_keeps_all_alphabetical(self):
        from agent.timing_analyze import analyze, format_report
        recs = [
            {"category": "applied", "wall_s": 1.0, "phases": {"zeta": 0.1, "alpha": 9.0}, "wall_s_delta_phases": 0.0},
        ]
        text = format_report(analyze(recs))
        assert text.index("alpha") < text.index("zeta")

    def test_top_n_sorts_descending_by_p95(self):
        from agent.timing_analyze import analyze, format_report
        recs = [
            {"category": "applied", "wall_s": 1.0, "phases": {"slow": 9.0, "fast": 0.1}, "wall_s_delta_phases": 0.0},
        ]
        text = format_report(analyze(recs), top_n=5)
        assert text.index("slow") < text.index("fast")

    def test_cli_accepts_top_n(self, tmp_path):
        from agent.timing_analyze import main
        log = tmp_path / "t.log"
        log.write_text(
            json.dumps({"category": "applied", "wall_s": 1.0, "phases": {"a": 1.0, "b": 2.0}, "wall_s_delta_phases": 0.0})
            + "\n"
        )
        rc = main(["--file", str(log), "--top-n", "1", "--no-rotated"])
        assert rc == 0


class TestReadmeMentionsTopNFlag:
    """Loop 124: README documents `--top-n` so the flag is discoverable."""

    def test_readme_mentions_top_n(self):
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text("utf-8")
        assert "--top-n" in readme


class TestSinceFilter:
    """Loop 125: `--since <iso>` filters records by `ts >= since`. Lex
    ISO-8601 compare is correct because `_write_timing` writes UTC
    `YYYY-MM-DDTHH:MM:SSZ`."""

    def test_filter_since_passthrough_when_none(self):
        from agent.timing_analyze import filter_since
        recs = [{"ts": "2026-01-01T00:00:00Z", "category": "applied", "wall_s": 1.0, "phases": {}}]
        assert filter_since(recs, None) == recs
        assert filter_since(recs, "") == recs

    def test_filter_since_keeps_only_at_or_after(self):
        from agent.timing_analyze import filter_since
        recs = [
            {"ts": "2026-01-01T00:00:00Z"},
            {"ts": "2026-04-28T00:00:00Z"},
            {"ts": "2026-12-31T23:59:59Z"},
        ]
        kept = filter_since(recs, "2026-04-28T00:00:00Z")
        assert [r["ts"] for r in kept] == ["2026-04-28T00:00:00Z", "2026-12-31T23:59:59Z"]

    def test_filter_since_excludes_records_without_string_ts(self):
        from agent.timing_analyze import filter_since
        recs = [
            {"ts": "2026-04-28T00:00:00Z"},
            {"ts": None},
            {"ts": 12345},
            {},
        ]
        kept = filter_since(recs, "2026-01-01T00:00:00Z")
        assert len(kept) == 1

    def test_cli_accepts_since(self, tmp_path):
        from agent.timing_analyze import main
        log = tmp_path / "t.log"
        old = json.dumps({"ts": "2026-01-01T00:00:00Z", "category": "applied", "wall_s": 1.0, "phases": {}, "wall_s_delta_phases": 0.0})
        new = json.dumps({"ts": "2026-12-31T00:00:00Z", "category": "clean", "wall_s": 0.5, "phases": {}, "wall_s_delta_phases": 0.0})
        log.write_text(old + "\n" + new + "\n")
        rc = main(["--file", str(log), "--since", "2026-06-01T00:00:00Z", "--no-rotated", "--json"])
        assert rc == 0

    def test_readme_mentions_since(self):
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text("utf-8")
        assert "--since" in readme


class TestUntilFilter:
    """Loop 126: `--until <iso>` symmetric counterpart to --since,
    inclusive upper bound."""

    def test_filter_until_passthrough_when_none(self):
        from agent.timing_analyze import filter_until
        recs = [{"ts": "2026-01-01T00:00:00Z"}]
        assert filter_until(recs, None) == recs
        assert filter_until(recs, "") == recs

    def test_filter_until_keeps_only_at_or_before(self):
        from agent.timing_analyze import filter_until
        recs = [
            {"ts": "2026-01-01T00:00:00Z"},
            {"ts": "2026-04-28T00:00:00Z"},
            {"ts": "2026-12-31T23:59:59Z"},
        ]
        kept = filter_until(recs, "2026-04-28T00:00:00Z")
        assert [r["ts"] for r in kept] == ["2026-01-01T00:00:00Z", "2026-04-28T00:00:00Z"]

    def test_since_until_compose_to_closed_interval(self):
        from agent.timing_analyze import filter_since, filter_until
        recs = [
            {"ts": "2026-01-01T00:00:00Z"},
            {"ts": "2026-04-28T00:00:00Z"},
            {"ts": "2026-04-29T00:00:00Z"},
            {"ts": "2026-12-31T23:59:59Z"},
        ]
        kept = filter_until(filter_since(recs, "2026-04-28T00:00:00Z"), "2026-04-29T00:00:00Z")
        assert [r["ts"] for r in kept] == ["2026-04-28T00:00:00Z", "2026-04-29T00:00:00Z"]

    def test_cli_accepts_until(self, tmp_path):
        from agent.timing_analyze import main
        log = tmp_path / "t.log"
        log.write_text(
            json.dumps({"ts": "2026-01-01T00:00:00Z", "category": "applied", "wall_s": 1.0, "phases": {}, "wall_s_delta_phases": 0.0})
            + "\n"
        )
        rc = main(["--file", str(log), "--until", "2026-12-31T00:00:00Z", "--no-rotated", "--json"])
        assert rc == 0

    def test_readme_mentions_until(self):
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text("utf-8")
        assert "--until" in readme


class TestCategoryAndPhaseFilters:
    """Loop 127: `--category` and `--phase` filters narrow analytics
    to a single outcome category or to records that ran a specific
    named phase."""

    def test_filter_category_passthrough(self):
        from agent.timing_analyze import filter_category
        recs = [{"category": "applied"}, {"category": "clean"}]
        assert filter_category(recs, None) == recs
        assert filter_category(recs, "") == recs

    def test_filter_category_exact_match(self):
        from agent.timing_analyze import filter_category
        recs = [{"category": "applied"}, {"category": "clean"}, {"category": "applied"}]
        kept = filter_category(recs, "applied")
        assert len(kept) == 2
        assert all(r["category"] == "applied" for r in kept)

    def test_filter_phase_passthrough(self):
        from agent.timing_analyze import filter_phase
        recs = [{"phases": {"a": 1.0}}]
        assert filter_phase(recs, None) == recs

    def test_filter_phase_keeps_only_records_with_phase_key(self):
        from agent.timing_analyze import filter_phase
        recs = [
            {"phases": {"discovery": 0.1, "find_bugs": 5.0}},
            {"phases": {}},
            {"phases": {"discovery": 0.2}},
            {},
        ]
        kept = filter_phase(recs, "find_bugs")
        assert len(kept) == 1

    def test_cli_accepts_category_and_phase(self, tmp_path):
        from agent.timing_analyze import main
        log = tmp_path / "t.log"
        log.write_text(
            json.dumps({"ts": "2026-04-28T00:00:00Z", "category": "applied", "wall_s": 1.0, "phases": {"discovery": 0.1, "find_bugs": 5.0}, "wall_s_delta_phases": 0.0})
            + "\n"
            + json.dumps({"ts": "2026-04-28T00:00:00Z", "category": "clean", "wall_s": 0.5, "phases": {}, "wall_s_delta_phases": 0.0})
            + "\n"
        )
        rc = main(["--file", str(log), "--category", "applied", "--phase", "find_bugs", "--no-rotated", "--json"])
        assert rc == 0

    def test_readme_mentions_category_and_phase(self):
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text("utf-8")
        assert "--category" in readme
        assert "--phase" in readme

    def test_readme_documents_loop_232_analytics_surfaces(self):
        # Loop 232: pin the README documentation for the loop-229/230/231
        # analytics-surface trio. If a future loop renames any of these
        # consumer-facing tokens, the docs go stale silently otherwise.
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text("utf-8")
        assert "--since-last-exit" in readme
        assert "exit_records" in readme
        assert "iteration_count" in readme
        assert "shutdown records" in readme


class TestExitRecordsLoop229:
    """Loop 229: timing_analyze surfaces the loop-226 synthetic
    exit:<reason> records (shutdown breadcrumbs) so analytics can
    join them to runtime.log via the iteration_count field."""

    def test_analyze_collects_exit_records(self):
        from agent.timing_analyze import analyze
        recs = [
            {"category": "applied", "outcome": "applied", "wall_s": 1.0, "phases": {}},
            {
                "ts": "2026-04-28T12:00:00Z",
                "category": "exit",
                "outcome": "exit:sigterm",
                "phases": {},
                "iteration_count": 42,
            },
            {
                "ts": "2026-04-28T13:00:00Z",
                "category": "exit",
                "outcome": "exit:keyboard-interrupt",
                "phases": {},
                "iteration_count": 7,
            },
        ]
        rep = analyze(recs)
        assert rep["category_counts"].get("exit") == 2
        exits = rep["exit_records"]
        assert len(exits) == 2
        assert exits[0]["reason"] == "sigterm"
        assert exits[0]["iteration_count"] == 42
        assert exits[1]["reason"] == "keyboard-interrupt"
        assert exits[1]["iteration_count"] == 7

    def test_analyze_handles_exit_record_without_iteration_count(self):
        from agent.timing_analyze import analyze
        recs = [{"category": "exit", "outcome": "exit:system-exit", "phases": {}}]
        rep = analyze(recs)
        exits = rep["exit_records"]
        assert len(exits) == 1
        assert exits[0]["reason"] == "system-exit"
        assert exits[0]["iteration_count"] is None

    def test_analyze_no_exit_records_returns_empty_list(self):
        from agent.timing_analyze import analyze
        recs = [{"category": "applied", "outcome": "applied", "wall_s": 1.0}]
        rep = analyze(recs)
        assert rep["exit_records"] == []

    def test_format_report_includes_exit_breadcrumbs(self):
        from agent.timing_analyze import analyze, format_report
        recs = [
            {
                "ts": "2026-04-28T12:00:00Z",
                "category": "exit",
                "outcome": "exit:sigterm",
                "phases": {},
                "iteration_count": 99,
            }
        ]
        rep = analyze(recs)
        out = format_report(rep)
        assert "shutdown records" in out
        assert "sigterm" in out
        assert "iter=99" in out
        assert "2026-04-28T12:00:00Z" in out

    def test_format_report_omits_section_when_no_exit_records(self):
        from agent.timing_analyze import analyze, format_report
        recs = [{"category": "applied", "outcome": "applied", "wall_s": 1.0, "phases": {}}]
        rep = analyze(recs)
        out = format_report(rep)
        assert "shutdown records" not in out


class TestExitRecordsJsonOutputLoop230:
    """Loop 230: pin the --json schema for the loop-229 exit_records
    field so downstream consumers (dashboards, alerting) can rely on
    the exact key set. If a future loop renames a key, the contract
    test fires before the consumer breaks."""

    def test_json_output_includes_exit_records_field(self, tmp_path, capsys):
        from agent.timing_analyze import main
        log = tmp_path / "t.log"
        log.write_text(
            json.dumps({
                "ts": "2026-04-28T12:00:00Z",
                "category": "exit",
                "outcome": "exit:sigterm",
                "phases": {},
                "iteration_count": 99,
                "pid": 12345,
            }) + "\n"
        )
        rc = main(["--file", str(log), "--no-rotated", "--json"])
        assert rc == 0
        out = capsys.readouterr().out
        report = json.loads(out)
        assert "exit_records" in report
        assert isinstance(report["exit_records"], list)
        assert len(report["exit_records"]) == 1
        rec = report["exit_records"][0]
        # Loop 234: pid added to the contract.
        assert set(rec.keys()) == {"ts", "reason", "iteration_count", "pid"}
        assert rec["ts"] == "2026-04-28T12:00:00Z"
        assert rec["reason"] == "sigterm"
        assert rec["iteration_count"] == 99
        assert rec["pid"] == 12345

    def test_json_output_exit_records_empty_list_when_none_present(self, tmp_path, capsys):
        from agent.timing_analyze import main
        log = tmp_path / "t.log"
        log.write_text(
            json.dumps({"category": "applied", "outcome": "applied", "wall_s": 1.0, "phases": {}}) + "\n"
        )
        rc = main(["--file", str(log), "--no-rotated", "--json"])
        assert rc == 0
        out = capsys.readouterr().out
        report = json.loads(out)
        assert report["exit_records"] == []

    def test_json_output_serializes_iteration_count_none_as_null(self, tmp_path, capsys):
        from agent.timing_analyze import main
        log = tmp_path / "t.log"
        log.write_text(
            json.dumps({
                "ts": "2026-04-28T12:00:00Z",
                "category": "exit",
                "outcome": "exit:keyboard-interrupt",
                "phases": {},
            }) + "\n"
        )
        rc = main(["--file", str(log), "--no-rotated", "--json"])
        assert rc == 0
        out = capsys.readouterr().out
        report = json.loads(out)
        rec = report["exit_records"][0]
        assert rec["iteration_count"] is None
        assert rec["reason"] == "keyboard-interrupt"

    def test_json_output_preserves_all_four_canonical_reasons(self, tmp_path, capsys):
        from agent.timing_analyze import main
        reasons = ["sigterm", "keyboard-interrupt", "system-exit", "unhandled-exception"]
        log = tmp_path / "t.log"
        with log.open("w") as f:
            for i, r in enumerate(reasons):
                f.write(json.dumps({
                    "ts": f"2026-04-28T12:00:{i:02d}Z",
                    "category": "exit",
                    "outcome": f"exit:{r}",
                    "phases": {},
                    "iteration_count": i,
                }) + "\n")
        rc = main(["--file", str(log), "--no-rotated", "--json"])
        assert rc == 0
        out = capsys.readouterr().out
        report = json.loads(out)
        observed = [rec["reason"] for rec in report["exit_records"]]
        assert observed == reasons


class TestSinceLastExitLoop231:
    """Loop 231: filter records to AFTER the most recent exit:* breadcrumb.
    Pairs with the loop-226 exit-record producer + loop-229 analyzer."""

    def test_filter_since_last_exit_keeps_records_after_exit(self):
        from agent.timing_analyze import filter_since_last_exit
        recs = [
            {"category": "applied", "ts": "2026-04-28T01:00:00Z"},
            {"category": "exit", "ts": "2026-04-28T02:00:00Z", "outcome": "exit:sigterm"},
            {"category": "applied", "ts": "2026-04-28T03:00:00Z"},
            {"category": "skip", "ts": "2026-04-28T04:00:00Z"},
        ]
        kept = filter_since_last_exit(recs)
        assert len(kept) == 2
        assert kept[0]["ts"] == "2026-04-28T03:00:00Z"
        assert kept[1]["ts"] == "2026-04-28T04:00:00Z"

    def test_filter_since_last_exit_uses_only_LAST_exit(self):
        # Two exit records in history; only records after the second
        # one survive (the simulated current run).
        from agent.timing_analyze import filter_since_last_exit
        recs = [
            {"category": "applied"},
            {"category": "exit", "outcome": "exit:sigterm"},
            {"category": "applied"},
            {"category": "exit", "outcome": "exit:keyboard-interrupt"},
            {"category": "applied"},
        ]
        kept = filter_since_last_exit(recs)
        assert len(kept) == 1
        assert kept[0] == {"category": "applied"}

    def test_filter_since_last_exit_no_exit_returns_input_unchanged(self):
        from agent.timing_analyze import filter_since_last_exit
        recs = [{"category": "applied"}, {"category": "skip"}]
        kept = filter_since_last_exit(recs)
        assert kept == recs

    def test_filter_since_last_exit_excludes_the_exit_record_itself(self):
        from agent.timing_analyze import filter_since_last_exit
        recs = [
            {"category": "exit", "outcome": "exit:sigterm"},
            {"category": "applied"},
        ]
        kept = filter_since_last_exit(recs)
        assert len(kept) == 1
        assert kept[0]["category"] == "applied"

    def test_filter_since_last_exit_empty_input_returns_empty(self):
        from agent.timing_analyze import filter_since_last_exit
        assert filter_since_last_exit([]) == []

    def test_filter_since_last_exit_only_exit_record_returns_empty(self):
        from agent.timing_analyze import filter_since_last_exit
        recs = [{"category": "exit", "outcome": "exit:sigterm"}]
        kept = filter_since_last_exit(recs)
        assert kept == []

    def test_cli_since_last_exit_scopes_report_to_current_run(self, tmp_path, capsys):
        from agent.timing_analyze import main
        log = tmp_path / "t.log"
        log.write_text(
            json.dumps({"category": "applied", "outcome": "applied", "wall_s": 1.0, "phases": {}}) + "\n"
            + json.dumps({
                "ts": "2026-04-28T02:00:00Z",
                "category": "exit",
                "outcome": "exit:sigterm",
                "phases": {},
                "iteration_count": 5,
            }) + "\n"
            + json.dumps({"category": "applied", "outcome": "applied", "wall_s": 2.0, "phases": {}}) + "\n"
        )
        rc = main(["--file", str(log), "--no-rotated", "--since-last-exit", "--json"])
        assert rc == 0
        out = capsys.readouterr().out
        report = json.loads(out)
        # Only the post-exit applied record should be counted.
        assert report["total_records"] == 1
        assert report["category_counts"] == {"applied": 1}
        assert report["exit_records"] == []

    def test_cli_since_last_exit_noop_when_no_exit(self, tmp_path, capsys):
        from agent.timing_analyze import main
        log = tmp_path / "t.log"
        log.write_text(
            json.dumps({"category": "applied", "outcome": "applied", "wall_s": 1.0, "phases": {}}) + "\n"
        )
        rc = main(["--file", str(log), "--no-rotated", "--since-last-exit", "--json"])
        assert rc == 0
        out = capsys.readouterr().out
        report = json.loads(out)
        assert report["total_records"] == 1


class TestExitRecordsPidLoop234:
    """Loop 234: analyzer-side surfacing of the loop-233 pid field
    in exit records. Pairs with the producer-side change so
    cross-process joins (pid, iteration_count) flow end-to-end."""

    def test_analyze_collects_pid_from_exit_record(self):
        from agent.timing_analyze import analyze
        recs = [{
            "category": "exit",
            "outcome": "exit:sigterm",
            "phases": {},
            "iteration_count": 7,
            "pid": 4242,
        }]
        rep = analyze(recs)
        rec = rep["exit_records"][0]
        assert rec["pid"] == 4242

    def test_analyze_legacy_record_without_pid_yields_none(self):
        # Records emitted before loop 233 didn't have a pid field.
        # Analyzer must tolerate them and surface None so the JSON
        # contract is stable across schema versions.
        from agent.timing_analyze import analyze
        recs = [{
            "category": "exit",
            "outcome": "exit:sigterm",
            "phases": {},
            "iteration_count": 7,
        }]
        rep = analyze(recs)
        rec = rep["exit_records"][0]
        assert rec["pid"] is None

    def test_analyze_non_int_pid_coerced_to_none(self):
        # Defensive: if a corrupt record has a string pid, surface
        # None rather than propagating bad data.
        from agent.timing_analyze import analyze
        recs = [{
            "category": "exit",
            "outcome": "exit:sigterm",
            "phases": {},
            "pid": "bogus",
        }]
        rep = analyze(recs)
        assert rep["exit_records"][0]["pid"] is None

    def test_format_report_includes_pid_in_shutdown_section(self):
        from agent.timing_analyze import analyze, format_report
        recs = [{
            "ts": "2026-04-28T12:00:00Z",
            "category": "exit",
            "outcome": "exit:sigterm",
            "phases": {},
            "iteration_count": 99,
            "pid": 4242,
        }]
        rep = analyze(recs)
        out = format_report(rep)
        assert "iter=99" in out
        assert "pid=4242" in out

    def test_format_report_renders_missing_pid_as_question_mark(self):
        from agent.timing_analyze import analyze, format_report
        recs = [{
            "ts": "2026-04-28T12:00:00Z",
            "category": "exit",
            "outcome": "exit:sigterm",
            "phases": {},
            "iteration_count": 99,
        }]
        rep = analyze(recs)
        out = format_report(rep)
        assert "pid=?" in out

    def test_two_pids_distinguished_in_report(self):
        # The whole point of pid: distinguishing two simultaneous
        # loop processes that would collide on iteration_count.
        from agent.timing_analyze import analyze, format_report
        recs = [
            {"category": "exit", "outcome": "exit:sigterm", "phases": {}, "iteration_count": 5, "pid": 100, "ts": "2026-04-28T01:00:00Z"},
            {"category": "exit", "outcome": "exit:sigterm", "phases": {}, "iteration_count": 5, "pid": 200, "ts": "2026-04-28T02:00:00Z"},
        ]
        rep = analyze(recs)
        out = format_report(rep)
        # Both pid lines must appear so an operator can see TWO
        # different shutdowns even though iter is identical.
        assert "pid=100" in out
        assert "pid=200" in out
