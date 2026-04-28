# Next Loop Candidates
1. (P5) `_finish` and `_finish_no_file` shape unification (extract a `_finalize_iteration(outcome, rel_for_timing, phases)` helper).
2. (P5) README update describing per-category delta breakdown in analytics.
3. (P6) `_log_aggregate_swallow_summary` records iteration count to sidecar file.
4. (P5) Audit asserting README JSON examples cover at least 3 distinct categories (currently we cover applied, validation_failed, no_candidate_files but no audit pins the count).
5. (P5) `agent.timing_analyze` `--top-n` flag to limit per-phase output to the N slowest phases.
6. (P5) Verify all swallow-counter mutations occur via dedicated helpers (no direct `_SWALLOW_X.append()` from non-helper sites).
