# Loop 227 candidates

URGENT: live-model OOM during chunked-delta-rule forward path.
Engine boots, /v1/models 200, /health 200, then dies on first
chat completion with torch.OutOfMemoryError in
chunk_gated_delta_rule_fwd_h: tried to allocate 96 MiB, only 73
MiB free, all 23.15 GiB in use by this process. The mamba/GDN
linear-attention path has a runtime memory bulge that the static
KV cache budget did not account for.

Suspected fix surface (in order of leverage):
1. Lower QWEN_SERVE_GPU_UTIL from 0.95 to 0.88 default - leave
   ~3 GiB headroom for the GDN forward bulge instead of squeezing
   to 1.2 GiB.
2. Lower QWEN_SERVE_MAX_LEN from 65536 to 32768 default - the
   per-token KV footprint scales linearly; halving max-len gives
   back ~3 GiB and the bulge is per-chunk-tokens-squared.
3. Lower QWEN_SERVE_MAX_BATCHED from 4096 to 2048 default -
   directly bounds the chunked-prefill chunk size which feeds the
   GDN forward.
4. Confirm PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True is
   actually being set in the engine subprocess (the OOM error
   recommends it; serve_qwen.sh already exports it -- but maybe
   only into the parent shell, not the engine process).

The defensible fix is (1) + (3) together: drop GPU_UTIL to 0.88
and MAX_BATCHED to 2048. Test pattern: extend the loop-205 dry-run
to assert the new defaults, plus a real-model E2E gate verifying
a 1k-token completion doesnt OOM.

Recommended: (5 is OOM fix above as a single composite loop). The
real-model E2E that proved the regression is the loop-217 test;
it asserts only readiness so far. Promoting it to drive a real
completion is the regression-prevention pin.
