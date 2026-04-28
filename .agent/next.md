# Next-loop candidates (loop 241+)

1. **Real-tokenizer integration** — lazy-load `transformers.AutoTokenizer`
   (cached) for exact prompt counts, fallback to heuristic when transformers
   absent or model name unset. Env: `QWEN_TOKENIZER_NAME` override.
2. **Per-message chat-template overhead** — `_estimate_tokens` should
   add ~6 token wrapper per message (`<|im_start|>role\n...<|im_end|>\n`)
   on top of content estimate.
3. **TUI /sysinfo "compressed N msgs last call"** — observability for
   loop-240 compression; currently silent except for warning logs.
4. **Operator visibility for TRUNCATION_MARKER** — auto-retry with
   bumped budget? Or just a TUI banner when seen.
5. **Live vLLM smoke test** — empirical verification of `<tool_call>`
   syntax + repetition_penalty against the real model. Carried since 164.
6. **timing_analyze --since-last-pid**, **/checkpoints --gzip**.
7. **Sandbox isolation bug** — pytest tmpdirs occasionally leaking into
   live `.loop/runtime.log`.
