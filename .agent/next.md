# Next Loop Candidates

1. (P6) Document `_abort_rebase_if_any` and `_revert_changes` cascade as the canonical recovery contract in module docstring.
2. (P7) `_strip_fence` nested triple-backticks (low risk).
3. (P5) `_RateLimitedSwallowLogger`: add `summary()` method ({label, count, last_logged_count, suppressed}) so a future admin endpoint can query suppression state.
4. (P6) Audit other callers of `_log` for similar spam potential — `_run_git` failures, `_apply_diff` reject paths.
5. (P5) `_revert_changes`: when origin/main resolution fails, log the *full* command stderr (not just first 200 chars) once at iteration boundary so a "stuck repo" diagnosis is possible from runtime.log alone.
6. (P5) Cache `iter_ts_monotonic = time.monotonic()` in `_iteration` and emit alongside `iter_ts` in timing.log for sub-second analytics.
