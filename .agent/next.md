# Next Loop Candidates
1. (P3) qwen_client.chat / chat_stream should surface base_url + body excerpt in the error string when vllm returns a 4xx so we never get an opaque retry loop again.
2. (P3) Live RichLog token streaming in chat_turn_stream so users see tokens arrive instead of one wall at the end.
3. (P3) Agent loop loop.py: smoke test that a full OBSERVE/ORIENT/DECIDE/DEVIL/ACT cycle does not crash on an empty repo.
4. (P5) Status bar widget showing model name plus running token meter.
5. (P5) Session checkpoint file 018 covering loops 132 onward.
6. (P5) /find_bugs auto chain to /apply when the model returns a single diff.

