# Next Loop Candidates

1. (P5) wall_s analytics example -- small CLI script that parses timing.log to tabulate phase-wallclock vs total vs scaffolding (wall_s_delta_phases now makes this trivial).
2. (P6) `_strip_fence` nested triple-backticks handling.
3. (P5) Add SIGUSR1 example to README.
4. (P5) Apply drift-audit shape from loop 89 to `agent/loop.py` direct module-state mutation outside of `global` decls.
5. (P6) `_log_aggregate_swallow_summary` records iteration count to sidecar file for SIGTERM resilience.
6. (P5) Pytest fixture to auto-reset every swallow logger between tests (replace scattered try/finally pattern).
7. (P6) Test that asserts `wall_s_delta_phases` documented in module docstring.
