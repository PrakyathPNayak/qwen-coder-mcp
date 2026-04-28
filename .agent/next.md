# Next Loop Candidates

1. (P5) wall_s analytics CLI script.
2. (P5) Drift-audit: scan `agent/loop.py` for direct module-state mutation outside `global` decls.
3. (P6) `_log_aggregate_swallow_summary` records iteration count to sidecar file.
4. (P4) `_iteration_budget_seconds` cap-check for non-int env values.
5. (P4) Confirm `missing_plus_header`/`missing_minus_header`/`dir_path_conflict` paths through `validation_failed:{rel}` correctly preserve the sub-error somewhere readable for analytics. Currently sub-error appears to be dropped at the `_finish` boundary -- the outer log only shows `validation_failed:{rel}` not `validation_failed:{rel}:{sub_error}`.
6. (P5) `_finish` and `_finish_no_file` shape unification.
7. (P6) AST-audit cache parsed trees if test count grows.
