# Next Loop Candidates

1. (P5) wall_s analytics CLI script.
2. (P5) Drift-audit shape from loop 89 to `agent/loop.py` direct module-state mutation outside `global` decls.
3. (P6) `_log_aggregate_swallow_summary` records iteration count to sidecar file.
4. (P5) Conftest auto-clears state; in-test `.clear()` calls are now redundant -- ~38 redundant lines.
5. (P4) `_iteration` `iter_ts` is captured but `_finish_no_file` doesn't use it (no STATE.md or history-md emission for early-exit paths). Should it?
6. (P5) Now that `_finish_no_file` exists, does `main()` still emit the iteration result line for these outcomes? Yes (covered by existing test) but verify the outcome formatting handles `skip:foo.py (unreadable_or_too_large)` with the parens and space.
7. (P6) AST-audit cache parsed trees if test count grows (~30ms overhead per test class).
