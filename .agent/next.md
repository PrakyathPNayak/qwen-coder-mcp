# Loop 230 candidates

1. /sysinfo --probe --json: machine-readable form of the
   loop-219 /health probe so analytics can ingest it. Pairs
   with the loop-229 exit-record analyzer.

2. timing_analyze --json --include-exits: ensure the loop-229
   exit_records list survives JSON serialization (probably
   already does -- but no test pins the shape).

3. _format_exit_line could include session id from runtime.log
   so two simultaneous loops in different repos do not collide
   on iteration_count alone.

4. /checkpoints export N <path> --gzip (carried, low priority).

5. TUI prefix-buffering for unwrapped streaming </think>
   (loop-218 deferred).

Recommended next: (2) - small, defensive, pins the loop-229
schema for downstream consumers.
