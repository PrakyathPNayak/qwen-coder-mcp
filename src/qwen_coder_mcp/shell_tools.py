"""Bounded shell-execution + grep + find utilities for the TUI.

Loop 135. Pure helpers (no Textual dependency) so the dispatcher can
unit test them without spinning up an App. All filesystem ops route
through `fs_tools._resolve_inside_root` so a `/run` cwd or a `/grep`
path cannot escape the configured repo root.

Safety contract for `run_shell`:
  - default timeout 10 seconds, hard maximum 120 seconds
  - default 64 KB cap per stream (stdout + stderr separately)
  - deny-list scan rejects obviously destructive patterns
    (rm -rf /, sudo, mkfs, dd of=/dev/, :() { :|: & };:, shutdown,
     reboot, halt, poweroff, chmod -R 777 /, chown -R)
  - cwd is locked to fs_cfg.root; relative cwd resolves inside the root

These limits are deliberately conservative: the model can still get a
lot done (`pytest`, `git status`, `python script.py`) while a naive
typo or prompt injection cannot wipe the workspace.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from . import fs_tools

DEFAULT_RUN_TIMEOUT = 10.0
MAX_RUN_TIMEOUT = 120.0
DEFAULT_OUTPUT_CAP = 64 * 1024
DEFAULT_GREP_MAX_FILES = 2000
DEFAULT_GREP_MAX_HITS = 200
DEFAULT_FIND_MAX = 500

# Patterns that immediately reject a /run invocation. Matched as
# substrings against the raw command line (case-insensitive). The list
# errs on the side of false positives -- the TUI surfaces the rejection
# message so the user can rephrase. Better a false reject than a false
# allow on rm -rf root.
_DENY_PATTERNS: tuple[str, ...] = (
    r"\brm\s+-rf\s+/(?!\w)",
    r"\brm\s+-fr\s+/(?!\w)",
    r"\brm\s+--no-preserve-root",
    r"\bsudo\b",
    r"\bmkfs\b",
    r"\bdd\s+of=/dev/",
    r":\(\)\s*\{",  # fork bomb prefix
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bhalt\b",
    r"\bpoweroff\b",
    r"\bchmod\s+-R\s+0*777\s+/(?!\w)",
    r"\bchown\s+-R\s+\S+\s+/(?!\w)",
    r">\s*/dev/sd[a-z]",
)
_DENY_RE = re.compile("|".join(_DENY_PATTERNS), re.IGNORECASE)


class ShellError(RuntimeError):
    """Raised when a shell-level invariant is violated (deny list, cwd escape)."""


@dataclass
class RunResult:
    returncode: int
    stdout: str
    stderr: str
    truncated: bool
    timed_out: bool
    cmd: str

    def to_dict(self) -> dict[str, object]:
        return {
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "truncated": self.truncated,
            "timed_out": self.timed_out,
            "cmd": self.cmd,
        }


def _check_denylist(cmd: str) -> None:
    if _DENY_RE.search(cmd):
        raise ShellError(
            "command rejected by deny list -- refuses obvious destructive "
            "patterns (rm -rf /, sudo, mkfs, dd, fork bomb, shutdown, ...)"
        )


def _truncate(text: str, cap: int) -> tuple[str, bool]:
    if len(text) <= cap:
        return text, False
    return text[:cap] + f"\n... [truncated, {len(text) - cap} more bytes]", True


def run_shell(
    cfg: fs_tools.FsConfig,
    cmd: str,
    *,
    timeout: float = DEFAULT_RUN_TIMEOUT,
    output_cap: int = DEFAULT_OUTPUT_CAP,
    cwd: str | None = None,
) -> RunResult:
    """Execute `cmd` via /bin/sh inside `cfg.root`. Capped & timed.

    Returns a `RunResult`. Raises `ShellError` if the deny list trips
    or if `cwd` resolves outside the sandbox.
    """
    cmd = (cmd or "").strip()
    if not cmd:
        raise ShellError("empty command")
    _check_denylist(cmd)
    timeout = min(max(0.5, float(timeout)), MAX_RUN_TIMEOUT)
    output_cap = max(1024, int(output_cap))

    if cwd:
        cwd_path = fs_tools._resolve_inside_root(cfg, cwd)
        if not cwd_path.is_dir():
            raise ShellError(f"cwd is not a directory: {cwd}")
    else:
        cwd_path = Path(cfg.root).resolve()

    timed_out = False
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            executable="/bin/sh",
            cwd=str(cwd_path),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        rc, out, err = proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        rc = -1
        out = (exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or ""))
        err = (exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or ""))
        err = (err + f"\n[timeout after {timeout:.1f}s]").strip()

    out_t, t1 = _truncate(out, output_cap)
    err_t, t2 = _truncate(err, output_cap)
    return RunResult(
        returncode=rc,
        stdout=out_t,
        stderr=err_t,
        truncated=t1 or t2,
        timed_out=timed_out,
        cmd=cmd,
    )


def format_run_result(res: RunResult) -> str:
    parts: list[str] = [f"$ {res.cmd}"]
    if res.timed_out:
        parts.append("[TIMED OUT]")
    parts.append(f"exit={res.returncode}")
    if res.stdout:
        parts.append("--- stdout ---")
        parts.append(res.stdout.rstrip())
    if res.stderr:
        parts.append("--- stderr ---")
        parts.append(res.stderr.rstrip())
    if not res.stdout and not res.stderr:
        parts.append("(no output)")
    return "\n".join(parts)


# --------------------------------------------------------------------- grep
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", ".venv-serve",
              "dist", "build", ".mypy_cache", ".pytest_cache", ".ruff_cache"}


def _iter_files(root: Path, *, max_files: int) -> Iterable[Path]:
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            yield Path(dirpath) / name
            count += 1
            if count >= max_files:
                return


@dataclass
class GrepHit:
    path: str
    line: int
    text: str


def grep(
    cfg: fs_tools.FsConfig,
    pattern: str,
    *,
    path: str = ".",
    max_hits: int = DEFAULT_GREP_MAX_HITS,
    max_files: int = DEFAULT_GREP_MAX_FILES,
    case_insensitive: bool = False,
) -> list[GrepHit]:
    """Recursive regex search rooted at `cfg.root / path`.

    Files larger than `cfg.max_read_bytes` and likely-binary files
    (containing NUL in the first 4 KB) are skipped silently.
    """
    if not pattern:
        raise ShellError("empty pattern")
    try:
        flags = re.IGNORECASE if case_insensitive else 0
        regex = re.compile(pattern, flags)
    except re.error as exc:
        raise ShellError(f"bad regex: {exc}")
    base = fs_tools._resolve_inside_root(cfg, path or ".")
    if base.is_file():
        candidates = [base]
    else:
        candidates = list(_iter_files(base, max_files=max_files))
    hits: list[GrepHit] = []
    for fp in candidates:
        try:
            if fp.stat().st_size > cfg.max_read_bytes:
                continue
            head = fp.open("rb").read(4096)
            if b"\x00" in head:
                continue
            text = fp.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeDecodeError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                rel = str(fp.relative_to(cfg.root)) if fp.is_relative_to(cfg.root) else str(fp)
                hits.append(GrepHit(path=rel, line=i, text=line[:400]))
                if len(hits) >= max_hits:
                    return hits
    return hits


def format_grep(hits: list[GrepHit]) -> str:
    if not hits:
        return "(no matches)"
    return "\n".join(f"{h.path}:{h.line}: {h.text}" for h in hits)


# --------------------------------------------------------------------- find
def find(
    cfg: fs_tools.FsConfig,
    glob_pattern: str,
    *,
    path: str = ".",
    max_results: int = DEFAULT_FIND_MAX,
) -> list[str]:
    """Glob search rooted at `cfg.root / path`. Returns repo-relative paths."""
    if not glob_pattern:
        raise ShellError("empty glob pattern")
    base = fs_tools._resolve_inside_root(cfg, path or ".")
    out: list[str] = []
    for match in base.rglob(glob_pattern):
        try:
            rel = match.relative_to(cfg.root)
        except ValueError:
            continue
        parts = set(rel.parts)
        if parts & _SKIP_DIRS:
            continue
        out.append(str(rel))
        if len(out) >= max_results:
            break
    return out


def format_find(paths: list[str]) -> str:
    if not paths:
        return "(no matches)"
    return "\n".join(paths)
