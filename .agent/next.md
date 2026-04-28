# Loop 206 candidates

1. **End-to-end check on `tui.py` startup against real backend** — `test_serve_qwen_help_validation.py` proved the validator-vs-real-tool pattern catches drift. Apply it to the TUI: actually start the app, point it at a fake-vLLM HTTP fixture, walk through 3-4 slash commands, assert no exceptions. Currently TUI tests are all unit-level — same dry-run-vs-reality gap that bit /serve_qwen.sh.
2. **Audit other shell scripts for vLLM CLI drift** — there are scripts in `scripts/` and `serve/` that may also reference removed flags.
3. **Terminal-width awareness for `_format_checkpoint_listing`** (carried).
4. **`/checkpoints export N <path> --gzip`** (carried).
5. **Live vLLM smoke test** — environment-dependent; this loop's --help-based validator is a partial replacement.

**Recommended:** (2) — same class of bug may exist elsewhere. Quick audit, possibly more fixes.
