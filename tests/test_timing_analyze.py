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
