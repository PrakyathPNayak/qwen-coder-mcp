# Next loop candidates

After loops 235-236 (the user-reported web_search + premature-stop fixes),
remaining queue:

1. **Streaming-filter parity for finish_reason=length** — _extract_text
   handles the non-stream path. Streaming path (qwen_client.stream_chat
   + _StreamingThinkStripFilter) does NOT emit a TRUNCATION_MARKER yet.
   When a stream ends with `data: [DONE]` after a finish_reason=length
   chunk, surface the marker on the final flush. Most user-visible.

2. **timing_analyze --since-last-pid** — current-process scope, lighter
   than --since-last-exit (loop 231). Use exit_records[*].pid composite
   joined with the live PID at analyze time.

3. **/checkpoints export N <path> --gzip** — atomic-write recipe is
   already in place; add gzip flag.

4. **Sandbox-isolation bug**: pytest tmpdirs leaking into the live
   .loop/runtime.log. Visible from `tail .loop/runtime.log` showing
   /tmp/pytest-of-root/pytest-NNN/... paths. Find the test that writes
   to a hard-coded ".loop/" path instead of tmp_path/".loop/".

5. **Real-model E2E for streaming-mode think-strip** (loop 218 deferred).

6. **TUI prefix-buffering for unwrapped streaming </think>** — loop 218
   known TUI deferral.

Recommended next: (1) — the streaming path is the user's primary
interface and shares the premature-stop symptom.
