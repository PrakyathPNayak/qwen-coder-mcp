# Next Loop Candidates

1. (P5) `_revert_changes` final-fallback to a known-good SHA when HEAD itself is broken.
2. (P6) Document `_abort_rebase_if_any` as canonical recovery contract in module docstring.
3. (P7) `_strip_fence` nested triple-backticks (low risk).
4. (P5) `_RateLimitedSwallowLogger`: add a public `summary()` method so a future admin endpoint can query "still suppressed N=count - last_logged".
5. (P6) Audit other callers of `_log` for similar spam potential — e.g. `_run_git` failure logs in `_commit_and_push` could go through a per-error-class rate limiter.
6. (P5) Cache `iter_ts` is currently a string formed via `_now()` — for sub-second precision in future analytics, also capture `iter_ts_monotonic = time.monotonic()` and emit alongside in timing.log.
