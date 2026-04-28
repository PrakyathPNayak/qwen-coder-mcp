# Next Loop Candidates

1. (P5) wall_s analytics CLI script that parses timing.log.
2. (P5) Drift-audit shape for `agent/loop.py` direct module-state mutation outside `global` decls (extending the loop 89 test idea).
3. (P6) `_log_aggregate_swallow_summary` records iteration count to sidecar file for SIGTERM resilience.
4. (P5) `_finish` and `_finish_no_file` shape unification.
5. (P5) Document outcome string format and timing.log schema in README so analytics consumers know what to parse.
6. (P4) Crashed iteration branch in main() does NOT increment a "crashed_iterations" counter -- if I want to track crash rate over time, the only signal is the traceback in runtime.log. Consider adding a sidecar counter OR emitting a synthetic timing.log record with outcome="crashed".
7. (P4) `_iteration_budget_seconds` deadline doesn't apply to time spent BEFORE the deadline is set (line 1690 deadline = monotonic + budget; this happens after `_candidate_files` and `_read_file`). If `_candidate_files` is slow on a huge repo, that time isn't budgeted.
