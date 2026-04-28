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
