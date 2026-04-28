# Loop 229 candidates

1. /sysinfo --probe --json: machine-readable form of the
   loop-219 /health probe so analytics can ingest it.

2. timing.log analytics reader: a small CLI/helper that groups
   exit/applied/skipped/crashed records by category over the
   last N records. Uses the loop-226 'exit' record + the
   iteration_count field.

3. _format_exit_line includes session id from runtime.log so
   exit records can be joined to the runtime.log entry that
   produced them (currently just iteration_count joins, but
   two simultaneous loops in different repos would collide).

4. /checkpoints export N <path> --gzip (carried, low priority).

5. TUI prefix-buffering for unwrapped streaming </think>
   (loop-218 deferred).

Recommended next: (2) - the loop-226 exit records have no
analytics consumer yet, and a thin reader proves the schema
end-to-end.
