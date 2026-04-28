# Next loop candidates

Recently shipped (loops 235-238 -- the user-reported issue cluster):
- 235: web_search anomaly fallback to DDG IA
- 236: _extract_text surfaces finish_reason=length truncation
- 237: chat_stream parity for truncation marker
- 238: default repetition_penalty=1.05 to break Qwen3-Next loops

Queue:

1. **README/docs pass for loops 236-238** -- env knobs (QWEN_MAX_TOKENS
   default change, QWEN_REPETITION_PENALTY, TRUNCATION_MARKER format)
   should be discoverable. Drift test for the new "repetition_penalty"
   surface.

2. **timing_analyze --since-last-pid** -- current-process scope,
   lighter than --since-last-exit (loop 231). Use exit_records[*].pid
   composite joined with the live PID at analyze time.

3. **/checkpoints export N <path> --gzip** -- atomic-write recipe
   already in place; add gzip flag.

4. **Sandbox-isolation bug**: pytest tmpdirs leaking into the live
   .loop/runtime.log. Visible from `tail .loop/runtime.log` showing
   /tmp/pytest-of-root/pytest-NNN/... paths. Find the test that writes
   to a hard-coded ".loop/" path instead of tmp_path/".loop/".

5. **Real-model E2E for streaming-mode think-strip + truncation
   marker** (loop 218 + 237 deferred).

6. **TUI prefix-buffering for unwrapped streaming </think>** -- loop
   218 known TUI deferral.

7. **Operator visibility for the truncation marker** -- when chat or
   stream returns including TRUNCATION_MARKER, the agent loop should
   either auto-retry with a higher max_tokens budget OR log a clear
   "consider raising QWEN_MAX_TOKENS" hint to runtime.log so users
   notice without digging into stderr.

Recommended next: (1) docs pass to make the new knobs discoverable,
since users would otherwise still hit slow-rep-loops not knowing they
can crank QWEN_REPETITION_PENALTY=1.10.
