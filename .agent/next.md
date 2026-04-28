# Next Loop Candidates
1. (P5) `_finish` and `_finish_no_file` shape unification (both emit timing+swallow; refactor to share helper).
2. (P5) Add a third README example showing an early-exit outcome (no_candidate_files or budget_exceeded:after_discovery).
3. (P6) `_log_aggregate_swallow_summary` records iteration count to sidecar file.
4. (P5) README update describing per-category delta breakdown.
5. (P5) Audit asserting every value in `OUTER_OUTCOME_CATEGORIES` appears at least once as a `_finish*` first-arg literal in the source (orphan category detection).
