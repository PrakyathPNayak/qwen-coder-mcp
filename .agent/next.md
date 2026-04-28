# Next Loop Candidates

1. (P5) Audit `_apply_diff` reject-path log calls for missing rate limiting.
2. (P5) wall_s analytics example -- small CLI script that parses timing.log.
3. (P6) Sanity test: wall_s >= sum(phases) for healthy iterations.
4. (P6) `_strip_fence` nested triple-backticks handling.
5. (P5) Add SIGUSR1 example to README with appropriate instructions.
6. (P6) The drift-audit test takes ~30ms walking AST; cache parsed trees if test count grows.
7. (P5) Apply the same drift-audit shape for `agent/loop.py` direct module-state mutation outside of approved helpers (`global` decl in main()).
