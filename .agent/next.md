# Next Loop Candidates

1. (P5) `_revert_changes` final-fallback to a known-good SHA when HEAD itself is broken (out of pre-built-image, mid-rebase, etc.). Currently relies on `_abort_rebase_if_any` + reset HEAD.
2. (P6) Document `_abort_rebase_if_any` as canonical recovery contract in module docstring.
3. (P5) `_iteration` calls `_now()` (timestamp) several times per loop — cache once at iteration start so all records in the same iteration share a timestamp.
4. (P7) `_strip_fence` nested triple-backticks (low risk).
5. (P5) `_RateLimitedSwallowLogger`: add a `last_logged_count` field and a public `summary()` method so operators can query "still suppressed N=count - last_logged" via SIGUSR1 handler or admin endpoint.
6. (P6) Audit other callers of `_log` for similar spam potential — e.g. `_run_git` failure logs in `_commit_and_push` could go through a per-error-class rate limiter.
