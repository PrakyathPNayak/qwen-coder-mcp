# Next Loop Candidates

1. (P5) wall_s analytics CLI script that parses timing.log.
2. (P5) Drift-audit for direct module-state mutation outside `global` decls.
3. (P6) `_log_aggregate_swallow_summary` records iteration count to sidecar file.
4. (P5) `_finish` and `_finish_no_file` shape unification.
5. (P5) Wrap `_candidate_files` + `_read_file` in `_PhaseTimer` so timing.log shows discovery wall-clock as a `discovery` phase. Document in README schema section.
6. (P5) Document `apply_failed` internal sub-categories in README.
7. (P5) Add wall_s_delta_phases interpretation hint to README outcome schema.
