# Next Loop Candidates

1. (P5) wall_s analytics CLI script that parses timing.log.
2. (P5) Drift-audit for direct module-state mutation outside `global` decls.
3. (P6) `_log_aggregate_swallow_summary` records iteration count to sidecar file.
4. (P5) `_finish` and `_finish_no_file` shape unification.
5. (P4) `_iteration_budget_seconds` deadline doesn't cover `_candidate_files` or `_read_file` time.
6. (P5) Document `apply_failed` internal sub-categories (`category` from `_apply_diff` return) in README.
7. (P5) The README outcome table should also include the wall_s_delta_phases interpretation hint (zero = good, large = silent overhead). Currently just defined.
