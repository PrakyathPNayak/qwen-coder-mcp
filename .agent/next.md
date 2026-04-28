# Next Loop Candidates

1. (P5) Audit `_apply_diff` reject-path log calls for missing rate limiting.
2. (P5) wall_s analytics example -- small CLI script that parses timing.log.
3. (P6) Sanity test: wall_s >= sum(phases) for healthy iterations.
4. (P6) `_strip_fence` nested triple-backticks.
5. (P6) Audit other tests for direct `L.x = ...` assignments not via monkeypatch.
6. (P5) main() startup line currently emits two lines (`loop starting` + `loop diagnostics`); consider a single structured KV line. Lower priority.
7. (P5) Add SIGUSR1 example to README with `pkill -USR1 -f "agent.loop"` instructions.
