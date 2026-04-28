# Next Loop Candidates

1. (P5) Audit `_apply_diff` reject-path log calls for missing rate limiting on malformed-diff paths.
2. (P5) `wall_s` analytics example -- small CLI script that parses timing.log to tabulate phase wallclock vs total.
3. (P6) Sanity test: wall_s >= sum(phases) for healthy iterations.
4. (P6) `_strip_fence` nested triple-backticks.
5. (P5) `_dump_logger_state` should also include `_LAST_SWALLOW_SUMMARY_COUNTS` and current iteration count.
6. (P5) main() should log aggregate-summary cadence at startup so operators know what to expect.
7. (P6) Audit other tests for direct `L.x = ...` assignments not via monkeypatch.
