# Loop 234 candidates

1. timing_analyze --json: surface pid alongside iteration_count
   in exit_records so cross-process joins are explicit (loop
   233 producer side is in; analyzer needs the read side).

2. README documentation pass for the loop-233 pid field --
   pairs with the loop-232 readme pass; same pattern.

3. /checkpoints export N <path> --gzip (carried, low priority).

4. TUI prefix-buffering for unwrapped streaming </think>
   (loop-218 deferred).

5. Real-model E2E for the loop-217 think-strip in streaming
   mode (only non-streaming is gated currently).

Recommended next: (1) - direct continuation of the loop-233
producer-side change. The schema contract for exit_records
should add 'pid' as a documented key (was {ts, reason,
iteration_count}, becomes {ts, reason, iteration_count, pid}).
