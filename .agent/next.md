# Next Loop Candidates

1. (P5) Audit `_apply_diff` reject-path log calls for missing rate limiting.
2. (P5) wall_s analytics example -- small CLI script that parses timing.log.
3. (P6) `_strip_fence` nested triple-backticks.
4. (P5) Add SIGUSR1 example to README.
5. (P5) Apply drift-audit shape from loop 89 to `agent/loop.py` direct module-state mutation outside of `global` decls.
6. (P6) `_log_aggregate_swallow_summary` should record the iteration count to a sidecar file so a SIGTERM during the cadence boundary still leaves a trail.
7. (P6) `wall_s` could carry a `wall_s_delta_phases` field (wall_s - sum(phases)) for fast scaffolding-overhead detection.
