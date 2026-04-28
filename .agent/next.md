# Next Loop Candidates

1. (P6) Document `_abort_rebase_if_any` + `_revert_changes` cascade as canonical recovery contract in module docstring.
2. (P7) `_strip_fence` nested triple-backticks (low risk).
3. (P6) Audit other callers of `_log` for similar spam potential — `_run_git` failures, `_apply_diff` reject paths.
4. (P5) Cache `iter_ts_monotonic` in `_iteration` and emit alongside `iter_ts` in timing.log for sub-second analytics.
5. (P5) `main()` loop-summary line at 100-iteration boundaries — print aggregate of `_LAST_SWALLOW_SUMMARY_COUNTS` so even a long run with no per-iteration growth shows historical totals.
6. (P5) `_LAST_SWALLOW_SUMMARY_COUNTS` should also expose a `_swallow_summary_state()` method for testability and future admin endpoint.
