# Next Loop Candidates

1. (P5) wall_s analytics example -- small CLI script that parses timing.log to tabulate phase wallclock vs total.
2. (P6) `_strip_fence` nested triple-backticks handling (currently only strips outer fence).
3. (P5) Add SIGUSR1 example to README with `pkill -USR1 -f "agent.loop"` instructions.
4. (P5) Apply drift-audit shape from loop 89 to `agent/loop.py` direct module-state mutation outside of `global` decls.
5. (P6) `_log_aggregate_swallow_summary` should record the iteration count to a sidecar file so a SIGTERM during the cadence boundary still leaves a trail.
6. (P6) `wall_s` could carry a `wall_s_delta_phases` field (wall_s - sum(phases)) for fast scaffolding-overhead detection.
7. (P5) Add a test that asserts every swallow logger in `_swallow_loggers()` has a non-empty label and that all labels are unique (prevents accidental duplicate label confusing aggregate summary).
