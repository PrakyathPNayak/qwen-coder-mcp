# Next Loop Candidates
1. (P3) If the serve still oom-s on the user's 4090 add MAX_LEN=1024 to the dropdown and consider --max-num-batched-tokens override.
2. (P5) Status bar widget showing model name plus running token meter.
3. (P5) Live RichLog update during chat_turn_stream so the user sees tokens arrive.
4. (P5) Session checkpoint file 018 covering loops 132 onward.
5. (P5) /find_bugs auto chain to /apply when the model returns a single diff.
6. (P5) /grep --count count only mode to mirror ripgrep.

