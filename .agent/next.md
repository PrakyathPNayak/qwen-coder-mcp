# Loop 226 candidates

1. agent/loop.py: SIGINT handler symmetry. Currently
   KeyboardInterrupt is handled, but SIGINT-from-non-foreground (e.g.
   detached run_loop.sh + signal-from-supervisor) may bypass the
   keyboard-interrupt path. Worth pinning.
2. Real-model E2E with <tool_call> round trip (loop-217 followup).
3. /checkpoints export N <path> --gzip variant (long-deferred).
4. TUI prefix-buffering for unwrapped streaming </think> (loop-218
   deferred).
5. /sysinfo --probe --json failure-shape audit.
6. timing.log exit record on shutdown - currently the exit reason
   only goes to runtime.log; timing.log analytics undercount the
   final iteration. Symmetric to the loop-105 crashed-record fix.
7. /agent --resume validation: pin resume-from-checkpoint path.

Recommended: (6) - extends the loop-225 observability work to the
analytics channel; small, isolated, and fixes a real undercounting
asymmetry that the loop-105 crashed-record pin already established
the pattern for.
