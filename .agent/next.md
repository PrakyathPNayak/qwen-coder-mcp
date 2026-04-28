# Next loop seed

## Candidates ranked
1. **(P7) `_verdict_accepts` audit** — does it accept `Verdict: accept`
   (lowercase), `VERDICT : ACCEPT` (extra spaces)?

2. **(P5) `_changed_paths` quoted-paths handling** — paths with
   spaces/unicode encoded by `core.quotePath`. Does porcelain v1 -z
   actually emit them quoted? Check.

3. **(P8) `.agent/loop_log.md` rotation** — same logic as STATE.md.

4. **(P6) `_revert_changes` idempotency on clean tree**.

5. **(P5) `qwen_client._extract_text` empty-content handling** —
   what if `content` is `None` or empty list? Robustness.
