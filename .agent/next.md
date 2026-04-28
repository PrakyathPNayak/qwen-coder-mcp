# Loop 231 candidates

1. /sysinfo --probe --json: machine-readable form of the
   loop-219 /health probe. Consistent with the loop-229/230
   exit_records analytics surface.

2. _format_exit_line could include session id (process pid or
   a uuid) so two simultaneous loops in different repos do not
   collide on iteration_count alone in joined analytics.

3. timing_analyze --since-last-exit: filter to records AFTER
   the most recent exit:* breadcrumb so analyses naturally
   focus on the current run.

4. /checkpoints export N <path> --gzip (carried, low priority).

5. TUI prefix-buffering for unwrapped streaming </think>
   (loop-218 deferred).

6. Real-model E2E for the loop-217 think-strip in streaming
   mode (only non-streaming is gated currently).

Recommended next: (3) - small, useful, and exercises the
loop-229 exit_records list as a join key in a different
direction (records before/after, not just enumeration).
