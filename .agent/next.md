# Next Loop Candidates

1. (P5) `_revert_changes` final-fallback to a known-good SHA when HEAD itself is broken (out of pre-built-image, mid-rebase, etc.). Currently relies on `_abort_rebase_if_any` + reset HEAD.
2. (P6) Document `_abort_rebase_if_any` as canonical recovery contract in module docstring.
3. (P5) `_iteration` calls `_now()` (timestamp) several times per loop — cache once at iteration start so all records in the same iteration share a timestamp.
4. (P7) `_strip_fence` nested triple-backticks (low risk).
5. (P5) `_RateLimitedSwallowLogger`: add a periodic "still failing" log line at exponential intervals (1, 10, 100, 1000) instead of fixed N=100 to surface persistent faults faster early.
