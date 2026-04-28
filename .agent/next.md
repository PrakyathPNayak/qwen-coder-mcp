# Next Loop Candidates

1. (P5) wall_s analytics CLI script that parses timing.log.
2. (P6) `_strip_fence` nested triple-backticks handling.
3. (P5) Add SIGUSR1 example to README.
4. (P5) Drift-audit shape from loop 89 to `agent/loop.py` direct module-state mutation outside `global` decls.
5. (P6) `_log_aggregate_swallow_summary` records iteration count to sidecar file for SIGTERM resilience.
6. (P6) Test that asserts `wall_s_delta_phases` documented in module docstring.
7. (P5) Now that swallow loggers auto-reset, also auto-reset `_LAST_SWALLOW_SUMMARY_COUNTS` (separate global dict that tracks delta).
8. (P5) Audit `_revert_changes` for the case where `git reset --hard` succeeds but leaves untracked files (e.g., new files added by the bad diff that weren't committed).
