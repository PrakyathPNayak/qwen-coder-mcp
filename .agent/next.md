# Next Loop Candidates

1. (P5) `_write_timing` failure counter — repeated swallowed exceptions could mask a permission bug. Add a counter & log first failure.
2. (P5) `_revert_changes` final-fallback to a known-good SHA when HEAD itself is broken.
3. (P6) Document `_abort_rebase_if_any` as canonical recovery contract in module docstring.
4. (P7) `_strip_fence` nested triple-backticks (low risk).
5. (P5) `_log` itself catches & swallows write errors only via `_rotate_log_if_oversized` — a write failure on the actual `fh.write` line propagates. Audit the I/O paths in `_log`.
