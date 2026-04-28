# Next Loop Candidates

1. (P5) wall_s analytics CLI script.
2. (P6) `_strip_fence` nested triple-backticks handling.
3. (P5) Add SIGUSR1 example to README.
4. (P5) Drift-audit shape from loop 89 to `agent/loop.py` direct module-state mutation outside `global` decls.
5. (P6) `_log_aggregate_swallow_summary` records iteration count to sidecar file.
6. (P6) Test that asserts `wall_s_delta_phases` documented in module docstring.
7. (P5) Now that conftest auto-clears `_LAST_SWALLOW_SUMMARY_COUNTS`, the 4 in-test `.clear()` calls are redundant -- remove them.
8. (P4) `_revert_changes`: if even reset --hard origin/main fails, the next iteration starts from a dirty tree. Should we cache the corruption and skip the next iteration's diff entirely?
9. (P6) Audit other places that use `git reset --hard` -- `_commit_and_push` rebase abort path.
