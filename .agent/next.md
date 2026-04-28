# Loop 233 candidates

1. /sysinfo --probe --json: machine-readable form of the
   loop-219 /health probe. Last analytics-surface gap.

2. _format_exit_line could include session id (process pid or
   uuid) so two simultaneous loops in different repos do not
   collide on iteration_count alone in joined analytics.

3. /checkpoints export N <path> --gzip (carried, low priority).

4. TUI prefix-buffering for unwrapped streaming </think>
   (loop-218 deferred).

5. Real-model E2E for the loop-217 think-strip in streaming
   mode (only non-streaming is gated currently).

6. timing_analyze --since-last-exit + --json regression: pin
   that --json honors the filter (likely already does, no test
   asserts it).

Recommended next: (1) - the /sysinfo --probe --json gap is the
last machine-readable surface in the analytics arc. After that
the natural pivot is to (2) for cross-process disambiguation.
