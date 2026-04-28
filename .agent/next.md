# Next Loop Candidates

1. (P5) wall_s analytics CLI script that parses timing.log (now also emits crashed records).
2. (P5) Drift-audit for direct module-state mutation outside `global` decls.
3. (P6) `_log_aggregate_swallow_summary` records iteration count to sidecar file.
4. (P5) Document outcome string format and timing.log schema in README.
5. (P5) `_finish` and `_finish_no_file` shape unification.
6. (P4) `_iteration_budget_seconds` deadline doesn't cover `_candidate_files` or `_read_file` time.
7. (P5) `iter_monotonic_outer` is captured per-loop-iteration in main() but never threaded into _iteration; if a future code path wants both inner and outer wall-time, they don't agree. Right now no problem.
8. (P5) Test that `crashed` outcomes don't get `_finish`-style sub-tokens (purely a single-segment outcome) -- to enforce the contract that this category is leaf-only.
