# Next Loop Candidates

1. (P5) wall_s analytics CLI script.
2. (P5) Drift-audit: scan `agent/loop.py` for direct module-state mutation outside `global` decls.
3. (P6) `_log_aggregate_swallow_summary` records iteration count to sidecar file.
4. (P5) Now that the fixture handles state reset, can other test-local resets (file system, monkeypatch state) also be hoisted? Probably not -- those are tmp_path-scoped already.
5. (P4) `_iteration_budget_seconds` cap-check audit: if env var is non-int it falls back to default? Verify.
6. (P5) `_finish` and `_finish_no_file` are structurally identical apart from outer `rel`/`phases` capture. Unify? Cost: clarity may suffer if you have to read the helper signature.
7. (P6) `OUTER_OUTCOME_CATEGORIES` test_no_extras_beyond_emitted does substring search; could give false positive if a category name appears in a comment. Tighten to AST literal scan.
