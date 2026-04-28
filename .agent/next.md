# Next Loop Candidates

1. (P5) `_write_timing` failure counter — repeated swallowed exceptions could mask permission bug.
2. (P6) Document `_abort_rebase_if_any` as canonical recovery contract in module docstring.
3. (P7) `_strip_fence` nested triple-backticks (low risk).
4. (P5) `_revert_changes` final-fallback to a known-good SHA.
5. (P6) The drift-audit test in TestOuterOutcomeCategories uses a regex that would miss `_finish` calls split across lines. Tighten or convert to AST visitor.
