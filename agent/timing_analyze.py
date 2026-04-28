"""Loop 112: minimal CLI to summarise `.loop/timing.log` records.

Reads JSON-line records from a timing log (default: `.loop/timing.log`
under the current working directory) and prints a concise per-category
and per-phase summary -- count, total wall-clock, mean, p50, p95.

Designed to be importable for tests (the `analyze` and `format_report`
functions are pure) and runnable as `python -m agent.timing_analyze`.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Iterable


def parse_records(lines: Iterable[str]) -> list[dict]:
    """Parse a stream of JSON-line strings, skipping blanks and lines
    that don't deserialise to a dict. Malformed lines are silently
    dropped -- the timing log format is forward-compatible and a
    half-written final line on rotation is normal."""
    out: list[dict] = []
    for raw in lines:
        s = raw.strip()
        if not s:
            continue
        try:
            rec = json.loads(s)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    s = sorted(values)
    # Linear interpolation between closest ranks.
    pos = (len(s) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    frac = pos - lo
    return s[lo] * (1 - frac) + s[hi] * frac


def _summarize(values: list[float]) -> dict[str, float]:
    if not values:
        return {"count": 0, "total": 0.0, "mean": 0.0, "p50": 0.0, "p95": 0.0}
    return {
        "count": len(values),
        "total": round(sum(values), 4),
        "mean": round(statistics.fmean(values), 4),
        "p50": round(_quantile(values, 0.5), 4),
        "p95": round(_quantile(values, 0.95), 4),
    }


def analyze(records: list[dict]) -> dict:
    """Group `wall_s` by `category` and per-phase wall-clock by phase
    name. Records without `wall_s` (early-exit, `crashed`, etc) still
    count toward category counts but contribute 0.0 to wall_s totals.

    Also collects `wall_s_delta_phases` across all records that emit
    it -- a high p95 here flags iterations where unaccounted-for time
    (work outside the named phases) is dominating, which signals
    either filesystem-level slowness or a missing `_PhaseTimer`."""
    by_cat: dict[str, list[float]] = {}
    by_phase: dict[str, list[float]] = {}
    by_cat_delta: dict[str, list[float]] = {}
    cat_counts: dict[str, int] = {}
    deltas: list[float] = []
    for rec in records:
        cat = rec.get("category")
        if isinstance(cat, str):
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
            wall = rec.get("wall_s")
            if isinstance(wall, (int, float)):
                by_cat.setdefault(cat, []).append(float(wall))
        phases = rec.get("phases")
        if isinstance(phases, dict):
            for name, val in phases.items():
                if isinstance(val, (int, float)):
                    by_phase.setdefault(name, []).append(float(val))
        delta = rec.get("wall_s_delta_phases")
        if isinstance(delta, (int, float)):
            deltas.append(float(delta))
            if isinstance(cat, str):
                by_cat_delta.setdefault(cat, []).append(float(delta))
    return {
        "total_records": len(records),
        "category_counts": cat_counts,
        "category_wall_s": {k: _summarize(v) for k, v in by_cat.items()},
        "phase_wall_s": {k: _summarize(v) for k, v in by_phase.items()},
        "wall_s_delta_phases": _summarize(deltas),
        "category_wall_s_delta_phases": {
            k: _summarize(v) for k, v in by_cat_delta.items()
        },
    }


def format_report(report: dict, top_n: int | None = None) -> str:
    lines: list[str] = []
    lines.append(f"timing.log analysis -- {report['total_records']} records")
    lines.append("")
    lines.append("by category (count, with wall_s stats where available):")
    for cat in sorted(report["category_counts"]):
        n = report["category_counts"][cat]
        stats = report["category_wall_s"].get(cat)
        if stats and stats["count"]:
            lines.append(
                f"  {cat:30s} count={n:4d}  wall_s "
                f"mean={stats['mean']:.3f} p50={stats['p50']:.3f} p95={stats['p95']:.3f}"
            )
        else:
            lines.append(f"  {cat:30s} count={n:4d}  (no wall_s)")
    lines.append("")
    phase_items = list(report["phase_wall_s"].items())
    if top_n is not None and top_n > 0:
        phase_items.sort(key=lambda kv: kv[1].get("p95", 0.0), reverse=True)
        phase_items = phase_items[:top_n]
        lines.append(f"by phase (top {top_n} by p95 wall-clock):")
    else:
        phase_items.sort(key=lambda kv: kv[0])
        lines.append("by phase (per-phase wall-clock):")
    for ph, s in phase_items:
        lines.append(
            f"  {ph:20s} count={s['count']:4d}  total={s['total']:.2f}s  "
            f"mean={s['mean']:.3f} p50={s['p50']:.3f} p95={s['p95']:.3f}"
        )
    lines.append("")
    d = report.get("wall_s_delta_phases", {"count": 0})
    if d.get("count"):
        lines.append(
            "wall_s_delta_phases (unaccounted time outside named phases):"
        )
        lines.append(
            f"  count={d['count']:4d}  total={d['total']:.2f}s  "
            f"mean={d['mean']:.3f} p50={d['p50']:.3f} p95={d['p95']:.3f}"
        )
    else:
        lines.append("wall_s_delta_phases: no records emit this field")
    by_cat_delta = report.get("category_wall_s_delta_phases", {})
    if by_cat_delta:
        lines.append("")
        lines.append("wall_s_delta_phases by category (p95 of unaccounted time):")
        for cat in sorted(by_cat_delta):
            s = by_cat_delta[cat]
            lines.append(
                f"  {cat:30s} count={s['count']:4d}  "
                f"mean={s['mean']:.3f} p50={s['p50']:.3f} p95={s['p95']:.3f}"
            )
    return "\n".join(lines) + "\n"


def _resolve_inputs(file: Path, include_rotated: bool) -> list[Path]:
    """Return the list of files to ingest. When `include_rotated`, also
    include `<file>.1` (the rotation slot from `_rotate_log_if_oversized`)
    if it exists. Older logs are appended last so chronological order
    is preserved when rotation slots have higher mtimes than the live
    file: the helper sorts by mtime ascending."""
    candidates: list[Path] = [file]
    if include_rotated:
        rotated = file.with_suffix(file.suffix + ".1")
        if rotated.exists():
            candidates.append(rotated)
    existing = [p for p in candidates if p.exists()]
    existing.sort(key=lambda p: p.stat().st_mtime)
    return existing


def filter_since(records: list[dict], since: str | None) -> list[dict]:
    """Return only records with `ts >= since` (lexicographic ISO-8601
    compare; both sides assumed UTC `Z`-suffix as written by
    `_write_timing`). When `since` is None or empty, the input is
    returned unchanged. Records missing or with non-string `ts` are
    excluded when `since` is active so partial logs cannot leak past
    the filter."""
    if not since:
        return records
    out: list[dict] = []
    for rec in records:
        ts = rec.get("ts")
        if isinstance(ts, str) and ts >= since:
            out.append(rec)
    return out


def filter_category(records: list[dict], category: str | None) -> list[dict]:
    """Return only records whose `category` field equals `category`.
    None or empty value is passthrough."""
    if not category:
        return records
    return [r for r in records if r.get("category") == category]


def filter_phase(records: list[dict], phase: str | None) -> list[dict]:
    """Return only records whose `phases` dict contains `phase` as a key.
    None or empty value is passthrough."""
    if not phase:
        return records
    out: list[dict] = []
    for r in records:
        phases = r.get("phases")
        if isinstance(phases, dict) and phase in phases:
            out.append(r)
    return out


def filter_until(records: list[dict], until: str | None) -> list[dict]:
    """Symmetric counterpart to `filter_since`: keep only `ts <= until`
    (inclusive). Same UTC Z-suffix lex compare assumption. Records
    with non-string `ts` are excluded when active."""
    if not until:
        return records
    out: list[dict] = []
    for rec in records:
        ts = rec.get("ts")
        if isinstance(ts, str) and ts <= until:
            out.append(rec)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Summarise .loop/timing.log records.")
    p.add_argument(
        "--file",
        type=Path,
        default=Path(".loop/timing.log"),
        help="Path to timing.log (default: .loop/timing.log)",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of a text report.",
    )
    p.add_argument(
        "--no-rotated",
        action="store_true",
        help="Skip `<file>.1` rotation slot even if it exists.",
    )
    p.add_argument(
        "--top-n",
        type=int,
        default=None,
        help="Limit the per-phase block to the N phases with highest p95 wall-clock.",
    )
    p.add_argument(
        "--since",
        type=str,
        default=None,
        help="Only include records with ts >= this ISO-8601 timestamp (UTC Z suffix).",
    )
    p.add_argument(
        "--until",
        type=str,
        default=None,
        help="Only include records with ts <= this ISO-8601 timestamp (UTC Z suffix).",
    )
    p.add_argument(
        "--category",
        type=str,
        default=None,
        help="Only include records whose category exactly matches this value.",
    )
    p.add_argument(
        "--phase",
        type=str,
        default=None,
        help="Only include records whose phases dict contains this phase name as a key.",
    )
    args = p.parse_args(argv)
    inputs = _resolve_inputs(args.file, include_rotated=not args.no_rotated)
    if not inputs:
        print(f"timing log not found: {args.file}", file=sys.stderr)
        return 1
    records: list[dict] = []
    for path in inputs:
        records.extend(parse_records(path.read_text("utf-8").splitlines()))
    records = filter_since(records, args.since)
    records = filter_until(records, args.until)
    records = filter_category(records, args.category)
    records = filter_phase(records, args.phase)
    report = analyze(records)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        sys.stdout.write(format_report(report, top_n=args.top_n))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
