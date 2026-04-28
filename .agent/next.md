# Next Loop Candidates

1. (P5) wall_s analytics CLI script that parses timing.log.
2. (P5) Drift-audit for direct module-state mutation outside `global` decls.
3. (P6) `_log_aggregate_swallow_summary` records iteration count to sidecar file.
4. (P5) `_finish` and `_finish_no_file` shape unification.
5. (P5) Document `apply_failed` internal sub-categories in README.
6. (P5) Wrap revert/commit in named phases (`revert`, `commit`).
7. (P5) AST audit asserting every phase name in `phases` dict appears in README schema (preventing drift like loop 108 where docs and code can diverge).
