# Next Loop Candidates

1. (P5) wall_s analytics CLI script.
2. (P5) Drift-audit shape from loop 89 to `agent/loop.py` direct module-state mutation outside `global` decls.
3. (P6) `_log_aggregate_swallow_summary` records iteration count to sidecar file.
4. (P5) Conftest auto-clears `_LAST_SWALLOW_SUMMARY_COUNTS`; the 4 in-test `.clear()` calls are now redundant -- clean them up.
5. (P4) `_revert_changes`: cache corruption when even origin/main fails so the next iteration's diff is short-circuited.
6. (P5) `_abort_rebase_if_any`: same caching consideration as #5 -- if both resets fail, signal upstream.
7. (P6) Drift-audit: every helper that uses `reset --hard` should now route failures through `_REVERT_SWALLOW_LOG`. Currently 2 helpers do (`_revert_changes`, `_abort_rebase_if_any`). If a 3rd is added it should follow the pattern.
