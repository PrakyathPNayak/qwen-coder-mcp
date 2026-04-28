# Loop 225 candidates

scripts/ coverage arc complete (loops 205, 222, 223, 224).
Next leverage points:

1. Real-model E2E with <tool_call> round trip (loop-217 followup) -
   prompt -> model emits <tool_call> JSON -> agent_loop parses ->
   fs_tool fires -> result fed back. The live loop-217 run already
   confirmed the model emits parseable JSON; promote that into a
   gated regression.
2. /checkpoints export N <path> --gzip variant (long-deferred).
3. TUI prefix-buffering for unwrapped streaming </think> (loop-218
   deferred).
4. /sysinfo --probe --json failure-shape audit.
5. agent/loop.py: structured exit reason logging when the
   autonomous loop terminates (currently silent on shutdown).
6. /agent --resume validation: pin the resume-from-checkpoint
   path against a fake checkpoint payload.

Recommended: (5) - small, isolated observability win for the
autonomous loop itself; gives us a postmortem channel.
