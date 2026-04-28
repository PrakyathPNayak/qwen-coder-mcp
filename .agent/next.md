# Loop 232 candidates

1. /sysinfo --probe --json: machine-readable form of the
   loop-219 /health probe. Last analytics-surface gap.

2. _format_exit_line could include session id (process pid or
   uuid) so two simultaneous loops in different repos do not
   collide on iteration_count alone in joined analytics.

3. README documentation pass for the loop-229/230/231 trio:
   exit_records JSON shape + --since-last-exit usage example.

4. /checkpoints export N <path> --gzip (carried, low priority).

5. TUI prefix-buffering for unwrapped streaming </think>
   (loop-218 deferred).

6. Real-model E2E for the loop-217 think-strip in streaming
   mode (only non-streaming is gated currently).

Recommended next: (3) - the loop-229/230/231 features need a
single coherent README pass before another fire pulls focus.
