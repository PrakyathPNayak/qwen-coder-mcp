# Next Loop Candidates

1. (P5) Apply same first/Nth pattern to `_log`, `_append_state`, `_write_history`, `_rotate_*` swallow sites — they all spam on persistent failure. Generalise via a `_RateLimitedLogger` helper.
2. (P5) `_revert_changes` final-fallback to a known-good SHA when HEAD itself is broken.
3. (P6) Document `_abort_rebase_if_any` as canonical recovery contract in module docstring.
4. (P7) `_strip_fence` nested triple-backticks (low risk).
5. (P5) `_iteration` calls `_now()` (timestamp) several times per loop — cache once at iteration start so all records in the same iteration share a timestamp.
