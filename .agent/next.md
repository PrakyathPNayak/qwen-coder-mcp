# Next Loop Candidates

1. (P5) `_write_timing` failure counter — repeated swallowed exceptions could mask permission bug.
2. (P6) Document `_abort_rebase_if_any` as canonical recovery contract in module docstring.
3. (P7) `_strip_fence` nested triple-backticks (low risk).
4. (P5) `_revert_changes` final-fallback to a known-good SHA when HEAD itself is broken.
5. (P6) `APPLY_ERROR_CATEGORIES` audit (mirror of loop 65) — verify every emitted error message in `_apply_diff` starts with a category in the frozenset.
