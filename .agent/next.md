# Next Loop Candidates

1. (P5) wall_s analytics CLI script.
2. (P5) Drift-audit shape from loop 89 to `agent/loop.py` direct module-state mutation outside `global` decls.
3. (P6) `_log_aggregate_swallow_summary` records iteration count to sidecar file.
4. (P5) Conftest auto-clears `_LAST_SWALLOW_SUMMARY_COUNTS`; the in-test `.clear()` calls are now redundant -- 38 redundant lines to clean up.
5. (P5) `_outer_outcome_category` -- does it have a category for `no_candidate_files`? If not, the records emitted by loop 99 will fall into "unknown" and analytics dashboards will break the category drift audit.
6. (P5) Categories drift audit (APPLY_ERROR_CATEGORIES, OUTER_OUTCOME_CATEGORIES) -- now that loop 99 emits a new outcome string verbatim, may need a new category.
7. (P4) `_iteration_budget_seconds` with a 24h cap is fine but the iteration uses `time.monotonic()` deadline; if `_abort_rebase_if_any` itself blocks for hours, the deadline is already past on first phase. Consider pre-deadline checkpoint.
