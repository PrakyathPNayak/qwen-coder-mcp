"""Self-improving agentic loop for qwen-coder-mcp.

For each iteration:
  1. Pick a candidate file (rotating cursor across the repo).
  2. Ask Qwen to find issues.
  3. Pick the top issue and ask Qwen to produce a unified diff fix.
  4. Run a "devil's advocate" pass to challenge the diff.
  5. If ACCEPT and `git apply --check` passes (and Python syntax check passes
     for *.py), apply, commit, and push.
  6. Append to STATE.md and .loop/history/<timestamp>.md.
  7. Sleep and repeat. Errors never break the loop.

Recovery contract
-----------------
Two helpers cooperate to guarantee every iteration starts from a clean,
known-good working tree no matter how badly a previous iteration ended:

* ``_abort_rebase_if_any()`` runs at the very start of ``_iteration`` and
  unconditionally clears any in-flight rebase state by calling
  ``git rebase --abort`` (best-effort; ignored if no rebase is in flight),
  then ``git reset --hard HEAD`` to drop any half-applied tree changes.
  This handles the case where a prior push-rebase-conflict left files
  partially merged.

* ``_revert_changes()`` is the iteration's mid-stream rollback. It runs
  whenever ``_apply_diff`` produced changes that we do not want to keep
  (test failure, devil-rejection-after-apply, etc.). It cascades through
  four progressively more aggressive fallbacks:

    1. ``git checkout -- .`` (drop unstaged edits).
    2. ``git clean -fd`` (drop untracked files/dirs).
    3. ``git reset --hard HEAD`` if either of the above failed.
    4. ``git reset --hard origin/main`` if HEAD itself is broken.

  ``origin/main`` is the final ground truth because the loop is the sole
  writer to that ref; resetting to it can only ever discard a failed
  commit/push attempt this loop just produced. All four failure paths
  funnel through ``_REVERT_SWALLOW_LOG`` so a persistently corrupt repo
  doesn't spam four log lines per iteration; the two success info logs
  ("recovered via reset --hard" / "...origin/main") are intentionally
  bare ``_log`` calls so successful recoveries stay visible.

Together these two helpers form the loop's "never wedged" contract:
even if iteration N crashed mid-apply, iteration N+1 starts on a clean
HEAD that matches origin/main.

Observability swallow loggers
-----------------------------
``_RateLimitedSwallowLogger`` instances wrap every per-iteration sink that
catches and swallows exceptions to keep the loop alive. A persistent
fault (disk full, network down, perm-denied) at any of these sinks would
otherwise emit one log line per iteration. The instances cover:
``_write_timing``, ``_append_state``, ``_write_history``,
``_prune_dir_oldest``, ``_save_cursor``, ``_commit_and_push`` (split into
``git_local`` and ``git_remote``), ``_revert_changes``, and
``_run_git_timeout``, and the ``git_empty_commit`` sink for the
anomalous "apply produced no committable changes" path. Each one is
registered in ``_swallow_loggers()`` and gets a per-iteration summary
emitted from ``_finish`` whenever its count has grown since the last
summary. ``main()`` additionally emits an aggregate cumulative snapshot
every ``QWEN_AGGREGATE_SUMMARY_EVERY`` iterations (default 100) for
long-run diagnostics.

Runtime introspection
---------------------
Operators of a long-running daemon can request a one-shot snapshot of
every swallow logger's full state (counts, suppression deltas,
``last_log_message``, and the cached ``_LAST_SWALLOW_SUMMARY_COUNTS``)
without restarting the process by sending SIGUSR1 to the loop PID
(POSIX only; the handler is a no-op on Windows). The same snapshot is
exposed programmatically as ``_dump_logger_state(reason, iteration=...)``.
"""
from __future__ import annotations

import codecs
import datetime as _dt
import json
import os
import re
import stat as _stat
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Iterable

# Allow `python -m agent.loop` from repo root.
_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

from qwen_coder_mcp import prompts  # noqa: E402
from qwen_coder_mcp.qwen_client import QwenClient, QwenError  # noqa: E402

LOOP_DIR = _REPO / ".loop"
HISTORY_DIR = LOOP_DIR / "history"
STATE_ARCHIVE_DIR = LOOP_DIR / "state_archive"
CURSOR_FILE = LOOP_DIR / "cursor.json"
LOG_FILE = LOOP_DIR / "runtime.log"
TIMING_FILE = LOOP_DIR / "timing.log"
STATE_FILE = _REPO / "STATE.md"
STATE_MAX_BYTES = 256 * 1024  # legacy default; use _state_max_bytes()
_STATE_MAX_BYTES_DEFAULT = 256 * 1024
_STATE_MAX_BYTES_CAP = 100 * 1024 * 1024
_STATE_ARCHIVE_MAX_FILES_DEFAULT = 50
_STATE_ARCHIVE_MAX_FILES_CAP = 10_000


def _state_max_bytes() -> int:
    """Cap for STATE.md size before rotation. Env-tunable; falls back to
    the module-level STATE_MAX_BYTES constant (preserved for monkeypatch
    compatibility in tests)."""
    raw = os.environ.get("QWEN_STATE_MAX_BYTES")
    if raw is None:
        return STATE_MAX_BYTES
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return STATE_MAX_BYTES
    if v <= 0:
        return STATE_MAX_BYTES
    return min(v, _STATE_MAX_BYTES_CAP)

# Paths excluded from candidate file selection.
EXCLUDE_DIRS = {".git", ".loop", ".venv", "venv", "__pycache__", "dist", "build"}
EXCLUDE_FILES = {"STATE.md", "LICENSE"}
TEXT_SUFFIXES = {
    ".py", ".md", ".toml", ".yaml", ".yml", ".json", ".cfg", ".ini",
    ".txt", ".sh", ".js", ".ts",
}


# ---------------------------------------------------------------- utilities
def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_RUNTIME_LOG_MAX_BYTES_DEFAULT = 5_000_000
_RUNTIME_LOG_MAX_BYTES_CAP = 100_000_000


def _runtime_log_max_bytes() -> int:
    """Cap for `.loop/runtime.log` size before rotation. Env-tunable."""
    return _env_int_capped(
        "QWEN_RUNTIME_LOG_MAX_BYTES",
        _RUNTIME_LOG_MAX_BYTES_DEFAULT,
        _RUNTIME_LOG_MAX_BYTES_CAP,
    )


def _rotate_log_if_oversized(path: Path, max_bytes: int) -> None:
    """Generic single-slot rotation: rename `path` to `path.1` when oversized."""
    try:
        if not path.exists():
            return
        if path.stat().st_size <= max_bytes:
            return
        rotated = path.with_suffix(path.suffix + ".1")
        if rotated.exists():
            rotated.unlink()
        path.rename(rotated)
    except Exception:  # never break the loop on logging
        pass


def _log(msg: str) -> None:
    line = f"[{_now()}] {msg}"
    try:
        print(line, flush=True)
    except Exception:  # logging must never break the loop
        pass
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        _rotate_log_if_oversized(LOG_FILE, _runtime_log_max_bytes())
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # logging must never break the loop
        pass


class _RateLimitedSwallowLogger:
    """Rate-limit log lines emitted by exception-swallowing observability sinks.

    A persistent fault (disk full, permission denied) at a sink that runs
    once per iteration would otherwise emit one swallow-log line per
    iteration. We log a configurable subset:

    * ``schedule="linear"`` — log when ``count == 1`` or
      ``count % every == 0``. Predictable cadence.
    * ``schedule="exponential"`` — log when ``count`` is a power of two
      up to ``every`` (1, 2, 4, 8, …, ``every``), then linearly every
      ``every`` after that. Surfaces persistent faults fast while still
      reporting late, rare faults.

    All module-level instances default to exponential because operators
    benefit from early warning.

    Use ``summary()`` to introspect: ``count`` (total failures since
    construction or last ``reset()``), ``last_logged_count`` (count at
    most-recent log emission, or 0 if nothing has been logged), and
    ``suppressed`` (failures since the last log). Useful for a future
    admin endpoint.
    """

    def __init__(
        self,
        label: str,
        every: int = 100,
        schedule: str = "linear",
    ) -> None:
        self.label = label
        self.every = every
        self.schedule = schedule
        self.count = 0
        self.last_logged_count = 0
        self.last_log_message: str | None = None

    def _should_log(self) -> bool:
        n = self.count
        if n == 1:
            return True
        if self.schedule == "exponential":
            # Power-of-two phase: 2, 4, 8, ..., <= every.
            if n <= self.every and (n & (n - 1)) == 0:
                return True
            # Linear phase past `every`.
            if self.every > 0 and n > self.every and n % self.every == 0:
                return True
            return False
        # Linear default.
        return self.every > 0 and n % self.every == 0

    def report(self, exc: BaseException, context: str = "") -> bool:
        """Increment the failure counter and emit a log line on a
        rate-limited cadence. Returns ``True`` iff a log line was
        emitted this call (False = suppressed). Callers that want to
        bind extra one-time work to the same cadence (e.g., dumping
        diagnostic context, sending a metric) can branch on the
        return value.
        """
        self.count += 1
        if self._should_log():
            ctx = f" [{context}]" if context else ""
            msg = f"{self.label} failed (count={self.count}){ctx}: {exc}"
            _log(msg)
            self.last_logged_count = self.count
            self.last_log_message = msg
            return True
        return False

    def reset(self) -> None:
        self.count = 0
        self.last_logged_count = 0
        self.last_log_message = None

    def summary(self) -> dict[str, int | str | None]:
        """Return a snapshot of suppression state for diagnostics."""
        return {
            "label": self.label,
            "count": self.count,
            "last_logged_count": self.last_logged_count,
            "suppressed": self.count - self.last_logged_count,
            "schedule": self.schedule,
            "every": self.every,
            "last_log_message": self.last_log_message,
        }


_GIT_CMD_TIMEOUT_SECONDS = 60  # legacy alias; use _git_cmd_timeout_seconds()


def _env_int_capped(env_key: str, default: int, max_value: int) -> int:
    """Generic env-configurable positive-integer reader with clamping.

    Bad/non-positive values fall back to ``default``; values above
    ``max_value`` are clamped to ``max_value``. Used for both subprocess
    timeouts and log-rotation byte caps.
    """
    raw = os.environ.get(env_key, str(default))
    try:
        v = int(float(raw))
    except (TypeError, ValueError):
        return default
    if v <= 0:
        return default
    if v > max_value:
        return max_value
    return v


def _env_timeout_seconds(env_key: str, default: int, max_value: int) -> int:
    """Backward-compatible alias for `_env_int_capped` (timeout helpers)."""
    return _env_int_capped(env_key, default, max_value)


_LOOP_ITER_BUDGET_DEFAULT = 600.0
_LOOP_ITER_BUDGET_MAX = 24 * 60 * 60.0  # 24 hours


def _iteration_budget_seconds() -> float:
    """Wall-clock ceiling for one `_iteration` call. Three Qwen calls can
    each retry several times with backoff (~120s timeout × ~3 attempts +
    sleeps), so a single iteration could otherwise burn ~20 minutes if
    the backend is flapping. The budget is checked *between* phases so
    one in-flight network call may still complete after the deadline.

    Values are clamped to (0, 24h]. Bad input or non-positive values
    fall back to the default. Absurdly large values are capped so a
    typo (`6000000` instead of `600`) cannot effectively disable the
    budget.
    """
    raw = os.environ.get("QWEN_LOOP_ITER_BUDGET_S", str(_LOOP_ITER_BUDGET_DEFAULT))
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return _LOOP_ITER_BUDGET_DEFAULT
    if v <= 0:
        return _LOOP_ITER_BUDGET_DEFAULT
    if v > _LOOP_ITER_BUDGET_MAX:
        return _LOOP_ITER_BUDGET_MAX
    return v


_GIT_CMD_TIMEOUT_DEFAULT = 60
_GIT_CMD_TIMEOUT_MAX = 600  # 10 minutes


def _git_cmd_timeout_seconds() -> int:
    """Hard timeout for one `git` subprocess invocation. Configurable
    via `QWEN_GIT_CMD_TIMEOUT_S`. Clamped to (0, 600s]; bad/non-positive
    falls back to the default. The cap prevents a typo from disabling
    the timeout entirely on a slow `git push` against a flaky remote.
    """
    return _env_timeout_seconds(
        "QWEN_GIT_CMD_TIMEOUT_S",
        _GIT_CMD_TIMEOUT_DEFAULT,
        _GIT_CMD_TIMEOUT_MAX,
    )


def _run_git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run `git <args>` with a hard timeout.

    Raises `subprocess.TimeoutExpired` only when callers passed
    `check=True`; with `check=False` (the cleanup paths) we synthesise
    a non-zero `CompletedProcess` so the caller can keep going.
    """
    timeout = _git_cmd_timeout_seconds()
    try:
        return subprocess.run(
            ["git", *args],
            cwd=_REPO,
            check=check,
            text=True,
            capture_output=True,
            timeout=timeout,
            errors="surrogateescape",
        )
    except subprocess.TimeoutExpired:
        if check:
            raise
        _GIT_TIMEOUT_SWALLOW_LOG.report(
            RuntimeError(f"exceeded {timeout}s"),
            context=f"git {' '.join(args)}",
        )
        return subprocess.CompletedProcess(
            args=["git", *args],
            returncode=124,
            stdout="",
            stderr=f"timed_out_after_{timeout}s",
        )


def _candidate_files() -> list[Path]:
    out: list[Path] = []
    for root, dirs, files in os.walk(_REPO):
        rel_root = Path(root).relative_to(_REPO)
        # prune excluded dirs in-place
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS and not d.startswith(".")]
        for name in files:
            if name in EXCLUDE_FILES or name.startswith("."):
                continue
            p = Path(root) / name
            if p.suffix.lower() not in TEXT_SUFFIXES:
                continue
            try:
                # `lstat` so symlinks are detected as themselves rather
                # than their target. Symlinks are skipped: in-repo links
                # are redundant (the target is also a candidate) and
                # out-of-repo links are blocked by `_read_file` anyway,
                # so picking one wastes an iteration slot on a
                # guaranteed "unreadable_or_too_large".
                st = p.lstat()
                if _stat.S_ISLNK(st.st_mode):
                    continue
                if st.st_size == 0:
                    continue
            except OSError:
                continue
            out.append(p.relative_to(_REPO))
        # don't recurse into excluded
    out.sort()
    return out


def _load_cursor() -> int:
    try:
        return int(json.loads(CURSOR_FILE.read_text("utf-8")).get("idx", 0))
    except Exception:
        return 0


def _save_cursor(idx: int) -> None:
    """Persist the cursor atomically.

    A naive `Path.write_text` is non-atomic: if the process is killed
    while the file is being truncated/written, the next `_load_cursor`
    sees an empty/corrupt file and (per its own try/except) falls back
    to ``0`` — silently re-scanning files that were already covered.
    Write to a sibling tempfile then `os.replace`, which is atomic on
    POSIX (and on Windows for files on the same volume).

    On rename failure (disk full, permissions, etc.), the previous
    cursor value is preserved and the failure is logged. We do NOT
    re-raise: the outer loop catches all exceptions, but if we raise
    here we abort the current iteration *after* the model round-trip
    has already cost time/tokens, and the next iteration loads the
    OLD cursor again — so the same file is re-scanned indefinitely
    until disk recovers. Logging-and-continuing means we lose progress
    for one iteration, not unbounded compute.
    """
    CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CURSOR_FILE.with_suffix(CURSOR_FILE.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps({"idx": idx}), "utf-8")
        os.replace(tmp, CURSOR_FILE)
    except OSError as exc:
        # Drop the half-written tmp; previous CURSOR_FILE (if any) is
        # untouched because we never opened it for writing.
        try:
            tmp.unlink()
        except OSError:
            pass
        try:
            _CURSOR_SWALLOW_LOG.report(exc, context=f"idx={idx}")
        except Exception:
            pass


def _read_file(path: Path, max_bytes: int) -> str | None:
    """Read a candidate file from the repo. Refuses to read content
    whose resolved target is outside `_REPO`, so an in-repo symlink
    pointing at e.g. `/etc/passwd` cannot leak host content into a
    model prompt."""
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    repo_resolved = _REPO.resolve()
    try:
        if not resolved.is_relative_to(repo_resolved):
            return None
    except AttributeError:
        # Python < 3.9 fallback (we target 3.11+, but be defensive).
        try:
            resolved.relative_to(repo_resolved)
        except ValueError:
            return None
    try:
        data = resolved.read_bytes()
    except OSError:
        return None
    if len(data) > max_bytes:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


# ------------------------------------------------------------- model output
_INNER_FENCE_RE = re.compile(
    r"```[a-zA-Z0-9_+\-]*\s*\n(.*?)\n```", re.DOTALL
)


_OPEN_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_+\-]*\s*\n", re.MULTILINE)
_CLOSE_FENCE_RE = re.compile(r"\n```\s*$")


def _strip_fence(text: str) -> str:
    """Extract the payload from a model response.

    The model is prompted to emit a fenced block, but in practice it
    sometimes wraps the fence in prose ("Here is the diff:\n```diff…```")
    or omits the fence entirely and returns a raw unified diff. Handle
    all three:

    1. Pure raw diff (starts with ``diff --git`` or ``--- ``) → return as-is.
    2. Otherwise return the inner text of the *first* properly-closed
       fenced block.
    3. Unclosed fence (model dropped the trailing ```): strip the
       leading ```lang\\n and any trailing ```; the remainder is best-
       effort payload (downstream `_apply_diff` validates).
    4. No fence at all → return the stripped original.

    When the model emits multiple fences we return the first; the prompt
    is contractually one diff in one fence, so multiple fences indicate
    a misformatted response which the downstream `_apply_diff` will
    reject if the first fence isn't actually a diff.
    """
    text = text.strip()
    if not text:
        return text
    if text.startswith(("diff --git", "--- ")):
        return text
    m = _INNER_FENCE_RE.search(text)
    if m:
        return m.group(1).strip()
    # Unclosed-fence salvage: model produced ```lang\n<body> with no closer.
    om = _OPEN_FENCE_RE.match(text)
    if om:
        body = text[om.end():]
        body = _CLOSE_FENCE_RE.sub("", body)
        return body.strip()
    return text


# Phrases that signal "no real findings" when they form the entire reply.
# Matched only when the response has no bullet or numbered-list markers,
# so a longer reply that happens to *contain* "no issues" inside one of
# its bullets is still parsed for that bullet.
_NO_ISSUE_RE = re.compile(
    r"""^\s*(?:
        no\s+(?:issues?|bugs?|problems?|errors?|findings?|defects?|concerns?)
            (?:\s+found)?
            (?:\s+(?:in|with)\s+(?:this|the)\s+(?:file|code))?\s*[.!]?
        |
        (?:(?:everything|this(?:\s+code)?|the\s+code)\s+)?
            looks?\s+(?:good|fine|clean|ok|okay|correct)
            (?:\s+to\s+me)?\s*[.!]?
        |
        lgtm\s*[.!]?
        |
        nothing\s+(?:to\s+(?:fix|change|do|report)|wrong|broken)\s*[.!]?
        |
        clean\s*[.!]?
        |
        all\s+good\s*[.!]?
    )\s*$""",
    re.IGNORECASE | re.VERBOSE,
)


def _parse_first_issue(text: str) -> str | None:
    text = text.strip()
    if not text or text.upper().startswith("NO_ISSUES"):
        return None
    # If the model returned a single benign "no findings" sentence with
    # no list markers, treat it as clean.
    if "\n" not in text and _NO_ISSUE_RE.match(text):
        return None
    # numbered list: "1. ...". Capture first item.
    m = re.search(r"(?ms)^\s*1[.)]\s+(.+?)(?=^\s*2[.)]\s+|\Z)", text)
    if m:
        return m.group(1).strip()
    # bullet list fallback.
    m = re.search(r"(?ms)^\s*[-*]\s+(.+?)(?=^\s*[-*]\s+|\Z)", text)
    if m:
        return m.group(1).strip()
    # Last-chance fallback: first line, but reject benign one-liners.
    first = text.splitlines()[0].strip()
    if not first or _NO_ISSUE_RE.match(first):
        return None
    return first


_VERDICT_ACCEPT_RE = re.compile(r"VERDICT\s*:\s*ACCEPT\b", re.IGNORECASE)
_VERDICT_REJECT_RE = re.compile(
    r"VERDICT\s*:\s*REJECT\b\s*(.*)", re.IGNORECASE | re.DOTALL
)


def _verdict_accepts(text: str) -> tuple[bool, str]:
    if _VERDICT_ACCEPT_RE.search(text):
        return True, "accept"
    m = _VERDICT_REJECT_RE.search(text)
    if m:
        # Trim only the immediate reject reason (first line), so a long
        # post-verdict ramble doesn't bloat the log.
        tail = m.group(1).strip()
        if tail:
            tail = tail.splitlines()[0].strip()
        return False, tail or "reject"
    # No clear verdict -> reject conservatively.
    return False, "no_verdict"


# ------------------------------------------------------------- diff handling
_DIFF_PATH_HEADER_RE = re.compile(
    r"^(?:"
    # `diff --git` form: a-path then b-path, each either unquoted or
    # quoted (git wraps paths in C-string quotes when they contain
    # spaces, special chars, or non-ASCII bytes with quotePath=true).
    r"diff --git\s+(?P<a1>(?:\"a/(?:\\.|[^\"\\])*\"|a/\S+))"
    r"\s+(?P<b1>(?:\"b/(?:\\.|[^\"\\])*\"|b/\S+))"
    r"|---\s+(?P<a2>(?:\"a/(?:\\.|[^\"\\])*\"|a/\S+))"
    r"|\+\+\+\s+(?P<b2>(?:\"b/(?:\\.|[^\"\\])*\"|b/\S+))"
    r")",
    re.MULTILINE,
)

# `rename from <path>` / `rename to <path>` / `copy from <path>` /
# `copy to <path>` — git-apply consumes these and writes to the named
# destination path. They are NOT prefixed with `a/` or `b/`. Without
# this, a rename-to traversal path could slip through `_has_unsafe_path`.
# Path may be quoted (contains spaces / non-ASCII with quotePath=true).
_DIFF_RENAME_COPY_RE = re.compile(
    r"^(?:rename from|rename to|copy from|copy to)\s+"
    r"(?P<p>(?:\"(?:\\.|[^\"\\])*\"|\S.*?))\s*$",
    re.MULTILINE,
)


def _unquote_diff_path(raw: str) -> str:
    """Strip the `a/` or `b/` prefix and decode C-string quoting that
    git uses for paths containing spaces, control bytes, or non-ASCII
    octets when `core.quotePath=true`. Returns the repo-relative path.
    """
    s = raw
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        body = s[1:-1]
        try:
            decoded = codecs.escape_decode(body.encode("utf-8"))[0]
            assert isinstance(decoded, bytes)
            s = decoded.decode("utf-8", errors="surrogateescape")
        except (ValueError, UnicodeDecodeError):
            # Decoding failed — return the literal body without quotes
            # so downstream checks still see *some* path string and can
            # reject it via traversal/absolute checks.
            s = body
    if s.startswith("a/") or s.startswith("b/"):
        s = s[2:]
    return s


def _diff_paths(diff: str) -> list[str]:
    """Return every repo-relative path mentioned in diff headers.

    Covers `--- a/`, `+++ b/`, `diff --git a/X b/Y`, plus
    `rename from`/`rename to`/`copy from`/`copy to`. Paths quoted in
    git's C-string format (used when paths contain spaces or non-ASCII
    bytes with `core.quotePath=true`) are decoded before being
    returned, so safety checks see the real destination path.
    """
    paths: list[str] = []
    for m in _DIFF_PATH_HEADER_RE.finditer(diff):
        for g in ("a1", "b1", "a2", "b2"):
            v = m.group(g)
            if not v:
                continue
            decoded = _unquote_diff_path(v)
            if decoded and decoded != "/dev/null":
                paths.append(decoded)
    for m in _DIFF_RENAME_COPY_RE.finditer(diff):
        v = m.group("p")
        if not v:
            continue
        decoded = _unquote_diff_path(v)
        if decoded and decoded != "/dev/null":
            paths.append(decoded)
    return paths


def _has_unsafe_path(diff: str) -> str | None:
    """Return an error message if any header path is unsafe, else None.

    Refuses absolute paths, `..` traversal segments, and Windows-drive paths.
    Defence in depth: `git apply` already refuses to write outside the tree,
    but we want the loop to log this distinctly (not as a generic apply
    failure) and to abort before the working tree is touched.
    """
    for raw in _diff_paths(diff):
        # Strip a single trailing tab+timestamp some diffs emit.
        path = raw.split("\t", 1)[0]
        if not path:
            return "empty_path"
        # NUL byte in a path is always hostile — UNIX path APIs split
        # at NUL silently; a quoted decoded `\0` could rewrite the
        # destination. Newlines/CR likewise have no place in a real
        # filename and break log parsing.
        if "\x00" in path:
            return f"nul_in_path:{path!r}"
        if "\n" in path or "\r" in path:
            return f"newline_in_path:{path!r}"
        if path.startswith("/"):
            return f"absolute_path:{path}"
        # Windows drive prefix: one ASCII letter followed by ':'. POSIX
        # filenames legitimately contain `:` (e.g. `a:b.py`), so we must
        # not flag every 2nd-char-colon path as absolute.
        if (
            len(path) >= 2
            and path[1] == ":"
            and "A" <= path[0].upper() <= "Z"
        ):
            return f"absolute_path:{path}"
        # Normalise forward slashes only; backslashes shouldn't appear.
        if "\\" in path:
            return f"backslash_in_path:{path}"
        parts = path.split("/")
        if any(p == ".." for p in parts):
            return f"path_traversal:{path}"
    return None


def _has_binary_patch(diff: str) -> str | None:
    """Reject diffs containing binary patch markers.

    Two formats matter:
      1. `Binary files a/X and b/Y differ` — git-diff's textual marker
         that a binary diff was suppressed; applying it is a no-op but
         indicates the model misunderstood the task.
      2. `GIT binary patch` — actual binary delta in base85 blocks. A
         coding loop never legitimately emits these; the corpus this
         model edits is text-only.

    Only header lines (everything before the first `@@` hunk header)
    are scanned. Inside a hunk, content lines are data — a markdown
    file documenting "Binary files differ" is not a binary patch.
    """
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("@@"):
            in_hunk = True
            continue
        if in_hunk:
            # New file headers reset the in_hunk state.
            if line.startswith("diff --git ") or line.startswith("--- "):
                in_hunk = False
            else:
                continue
        s = line.strip()
        if s.startswith("GIT binary patch"):
            return "git_binary_patch"
        if s.startswith("Binary files ") and s.endswith(" differ"):
            return "binary_files_marker"
    return None


def _has_unsafe_mode(diff: str) -> str | None:
    """Reject diffs that create or change to dangerous file modes.

    `120000` is git's symlink mode. A model-emitted diff that drops a
    symlink into the worktree could point at `/etc/passwd` or
    `~/.ssh/authorized_keys`, and `git apply` would happily honour it
    inside the repo (the symlink target is just bytes to git). We
    refuse symlinks blanket-style: a code-fix loop never legitimately
    needs to introduce one. `160000` (gitlinks / submodules) is
    similarly out of scope.
    """
    for line in diff.splitlines():
        s = line.strip()
        # `new file mode 120000` / `new mode 120000` / `old mode 120000`
        # any mode-line with the symlink or gitlink mode is suspicious.
        if s.startswith(("new file mode ", "new mode ", "old mode ", "deleted file mode ")):
            mode = s.rsplit(" ", 1)[-1]
            if mode == "120000":
                return f"symlink_mode:{s}"
            if mode == "160000":
                return f"gitlink_mode:{s}"
        # `index <sha>..<sha> <mode>` — git encodes the file mode on the
        # index line for new files / mode-only changes when no explicit
        # `new file mode` header is emitted. A concise diff for a new
        # symlink can be just `diff --git ...\nindex 0..abc 120000\n...`.
        if s.startswith("index "):
            parts = s.split()
            if len(parts) == 3 and parts[2] in ("120000", "160000"):
                kind = "symlink_mode" if parts[2] == "120000" else "gitlink_mode"
                return f"{kind}:{s}"
    return None


def _has_dir_path_conflict(diff: str) -> str | None:
    """Reject diffs whose `+++ b/<path>` target is an existing directory
    in the working tree. `git apply --check` does NOT catch this — it
    succeeds, then `git apply` fails late with a generic
    "Directory not empty" error. Catching it up front gives a clear
    diagnostic and avoids partial applies.

    Only the destination side (`+++` / `b/`-side rename/copy targets)
    is checked. The source side is allowed to overlap with directories
    only via deletes, which we detect via `--- a/<path>` paired with
    `+++ /dev/null`; for now we conservatively check every destination
    path emitted by `_diff_paths`.
    """
    for raw in _diff_paths(diff):
        path = raw.split("\t", 1)[0]
        if not path:
            continue
        # `_diff_paths` returns repo-relative; absolute/traversal paths
        # were already rejected by `_has_unsafe_path` in the apply
        # pipeline before this helper runs.
        target = (_REPO / path)
        try:
            if target.is_dir() and not target.is_symlink():
                return f"dir_path_conflict:{path}"
        except OSError:
            # Permission or filesystem issue — let `git apply` surface it.
            continue
    return None


def _has_structural_defect(diff: str) -> str | None:
    """Return an error if the diff is malformed, else None.

    A unified diff body must contain at least one `+++ ` header and at
    least one `@@ ` hunk marker. Some malformed model outputs include
    only `--- a/PATH` (a half-diff) or include `--- ` and `+++ ` but no
    hunks at all. These would be rejected by `git apply` but with an
    opaque message; we want a distinct outcome so the log is readable.
    """
    has_minus = False
    has_plus = False
    has_hunk = False
    for line in diff.splitlines():
        if line.startswith("--- "):
            has_minus = True
        elif line.startswith("+++ "):
            has_plus = True
        elif line.startswith("@@ ") or line.startswith("@@\t"):
            has_hunk = True
    if has_minus and not has_plus:
        return "missing_plus_header"
    if has_plus and not has_minus:
        return "missing_minus_header"
    if not has_hunk:
        # `diff --git` alone with rename-only metadata is rare from a
        # coding model and we never want to apply a rename-without-hunks.
        return "no_hunks"
    return None


_MAX_DIFF_BYTES = 256 * 1024
_MAX_DIFF_LINES = 5000


def _has_oversized_diff(diff: str) -> str | None:
    """Reject pathologically large diffs.

    A single coding fix should be a focused patch — usually <100 lines,
    rarely above ~1000. A 5000-line / 256 KB diff signals one of:
      - the model dumped the entire file rewritten as a diff,
      - context window leaked partial corpus into the response,
      - model is hallucinating mass-edits across many files.

    None of these are recoverable patches; reject before we waste
    `git apply` and a commit on them. Limits are deliberately well
    above any legitimate fix to avoid false positives.
    """
    n = len(diff)
    if n > _MAX_DIFF_BYTES:
        return f"size_bytes:{n}>{_MAX_DIFF_BYTES}"
    lines = diff.count("\n") + (0 if diff.endswith("\n") else 1)
    if lines > _MAX_DIFF_LINES:
        return f"size_lines:{lines}>{_MAX_DIFF_LINES}"
    return None


_GIT_APPLY_TIMEOUT_DEFAULT = 30
_GIT_APPLY_TIMEOUT_MAX = 600
_GIT_APPLY_TIMEOUT_SECONDS = 30  # legacy alias


def _git_apply_timeout_seconds() -> int:
    """Hard timeout for one `git apply` subprocess invocation.
    Configurable via `QWEN_GIT_APPLY_TIMEOUT_S`. Clamped to (0, 600s]."""
    return _env_timeout_seconds(
        "QWEN_GIT_APPLY_TIMEOUT_S",
        _GIT_APPLY_TIMEOUT_DEFAULT,
        _GIT_APPLY_TIMEOUT_MAX,
    )


def _run_git_apply(args: list[str], diff: str) -> tuple[int, str]:
    """Run `git <args>` feeding `diff` on stdin with a hard timeout.

    Returns (returncode, stderr-text). On TimeoutExpired the process is
    terminated and (124, "timed_out_after_<N>s") is returned.
    """
    timeout = _git_apply_timeout_seconds()
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=_REPO,
            input=diff,
            text=True,
            capture_output=True,
            timeout=timeout,
            errors="surrogateescape",
        )
        return proc.returncode, proc.stderr.strip()[:300]
    except subprocess.TimeoutExpired:
        return 124, f"timed_out_after_{timeout}s"


# Stable, machine-parseable error categories returned by `_apply_diff`.
# The detail (after `: `) is free-form; the category before `:` is the
# contract surface log-aggregators rely on.
APPLY_ERROR_CATEGORIES: frozenset[str] = frozenset({
    "not_a_unified_diff",
    "oversized_diff",
    "unsafe_path",
    "binary_patch",
    "unsafe_mode",
    "malformed_diff",
    "dir_conflict",
    "apply_check_failed",
    "apply_failed",
})

APPLY_OK_CATEGORY: str = "applied"


# ---------------------------------------------------------- outer iteration
# Stable category set for the outer `_iteration` outcome string. Every
# outcome string starts with one of these tokens (followed by ":" and
# context). External monitoring/parsers can grep for these to classify
# loop runs without parsing the full free-form tail.
OUTER_OUTCOME_CATEGORIES: frozenset[str] = frozenset({
    "applied",
    "clean",
    "skip",
    "rejected",
    "out_of_scope",
    "validation_failed",
    "commit_failed",
    "commit_skipped_empty",
    "revert_failed",
    "apply_failed",
    "qwen_error_find_bugs",
    "qwen_error_propose_fix",
    "qwen_error_devils_advocate",
    "budget_exceeded",
    "no_candidate_files",
    "crashed",
})


def _outer_outcome_category(outcome: str) -> str:
    """Extract the leading category from an `_iteration` outcome string.

    Outcome strings are formatted as either ``<category>`` (no colon) or
    ``<category>:<rest>``; this returns the leading token. If the leading
    token is not in `OUTER_OUTCOME_CATEGORIES` the raw token is still
    returned so callers can detect drift, but they SHOULD treat anything
    outside the frozenset as a contract bug.
    """
    return outcome.split(":", 1)[0].strip()


def _apply_error_category(msg: str) -> str:
    """Extract the leading category from an `_apply_diff` error message."""
    return msg.split(":", 1)[0].strip()


def _apply_diff(diff_text: str) -> tuple[bool, str]:
    """Try `git apply --check` then `git apply`. Returns (ok, message)."""
    diff = _strip_fence(diff_text)
    if not diff.lstrip().startswith(("diff --git", "--- ")):
        return False, "not_a_unified_diff"
    # Normalise line endings. Some models emit CRLF, which `git apply`
    # rejects with "patch with CRLF line endings" by default. We never
    # want CRs in unified-diff metadata lines, and content-line CRs in
    # the working tree are independently encoded by the patch hunks.
    diff = diff.replace("\r\n", "\n").replace("\r", "\n")
    if not diff.endswith("\n"):
        diff += "\n"
    oversized = _has_oversized_diff(diff)
    if oversized is not None:
        return False, f"oversized_diff: {oversized}"
    unsafe = _has_unsafe_path(diff)
    if unsafe is not None:
        return False, f"unsafe_path: {unsafe}"
    binary = _has_binary_patch(diff)
    if binary is not None:
        return False, f"binary_patch: {binary}"
    unsafe_mode = _has_unsafe_mode(diff)
    if unsafe_mode is not None:
        return False, f"unsafe_mode: {unsafe_mode}"
    structural = _has_structural_defect(diff)
    if structural is not None:
        return False, f"malformed_diff: {structural}"
    dir_conflict = _has_dir_path_conflict(diff)
    if dir_conflict is not None:
        return False, f"dir_conflict: {dir_conflict}"
    rc, err = _run_git_apply(["apply", "--check", "-"], diff)
    if rc != 0:
        return False, f"apply_check_failed: {err}"
    rc, err = _run_git_apply(["apply", "-"], diff)
    if rc != 0:
        return False, f"apply_failed: {err}"
    return True, "applied"


_VALIDATE_TIMEOUT_DEFAULT = 30
_VALIDATE_TIMEOUT_MAX = 600
_VALIDATE_TIMEOUT_SECONDS = 30  # legacy alias


def _validate_timeout_seconds() -> int:
    """Hard timeout for the per-file validate (`compileall`) subprocess.
    Configurable via `QWEN_VALIDATE_TIMEOUT_S`. Clamped to (0, 600s]."""
    return _env_timeout_seconds(
        "QWEN_VALIDATE_TIMEOUT_S",
        _VALIDATE_TIMEOUT_DEFAULT,
        _VALIDATE_TIMEOUT_MAX,
    )


def _validate_changed_files(paths: Iterable[Path]) -> tuple[bool, str]:
    """Validate touched files by extension. Rejects diffs that produce
    syntactically invalid Python, JSON, TOML, or YAML.

    Returns (ok, message). Unknown extensions are skipped silently.
    """
    paths = [Path(p) for p in paths]
    py = [str(_REPO / p) for p in paths if p.suffix == ".py"]
    if py:
        timeout = _validate_timeout_seconds()
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "compileall", "-q", *py],
                cwd=_REPO,
                text=True,
                capture_output=True,
                timeout=timeout,
                errors="surrogateescape",
            )
        except subprocess.TimeoutExpired:
            return False, f"py_invalid: timed_out_after_{timeout}s"
        if proc.returncode != 0:
            return False, f"py_invalid: {(proc.stdout + proc.stderr).strip()[:300]}"
        # compileall returncode=0 even when SyntaxWarning fires (e.g.
        # invalid escape sequences, "is" with a literal). These are
        # almost always real bugs we don't want to commit. Surface
        # them as a validation failure.
        if "SyntaxWarning" in proc.stderr:
            return False, f"py_syntax_warning: {proc.stderr.strip()[:300]}"

    for p in paths:
        full = _REPO / p
        if not full.is_file():
            continue
        suffix = p.suffix.lower()
        try:
            if suffix == ".json":
                import json
                # `json.loads` silently keeps only the last duplicate key,
                # so a fix that accidentally copies a key into a JSON
                # config file would round-trip green and corrupt the
                # config silently. Use object_pairs_hook to detect.
                def _no_dup(pairs: list[tuple[str, object]]) -> dict:
                    seen: set[str] = set()
                    for k, _ in pairs:
                        if k in seen:
                            raise ValueError(f"duplicate key: {k!r}")
                        seen.add(k)
                    return dict(pairs)
                json.loads(
                    full.read_text(encoding="utf-8"),
                    object_pairs_hook=_no_dup,
                )
            elif suffix == ".toml":
                try:
                    import tomllib
                except ModuleNotFoundError:
                    import tomli as tomllib  # type: ignore[no-redef]
                with full.open("rb") as fh:
                    tomllib.load(fh)
            elif suffix in {".yml", ".yaml"}:
                try:
                    import yaml  # type: ignore[import-not-found]
                except ModuleNotFoundError:
                    continue
                yaml.safe_load(full.read_text(encoding="utf-8"))
            elif suffix in {".cfg", ".ini"}:
                # `setup.cfg`, `tox.ini`, `pytest.ini` etc. configparser
                # raises on duplicate sections, malformed headers, or
                # interpolation errors. We skip interpolation (raw
                # parser) so legitimate `%` characters in values don't
                # trip false-positives.
                import configparser
                parser = configparser.RawConfigParser()
                parser.read_string(full.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 — surface any parse error
            return False, f"{suffix.lstrip('.')}_invalid:{p}: {str(exc)[:200]}"

    return True, "ok"


def _python_syntax_ok(paths: Iterable[Path]) -> tuple[bool, str]:
    """Backwards-compatible alias kept for any external callers / tests."""
    return _validate_changed_files(paths)


def _diff_in_scope(changed: Iterable[Path], target: Path) -> tuple[bool, str]:
    """The loop asks the model to fix one specific file. Reject diffs that
    touch any file other than `target` so a misbehaving model cannot
    silently rewrite the rest of the repo.

    An empty `changed` list is intentionally NOT rejected here: the
    empty-diff case is filtered later in `_commit_and_push` via its
    `git status --porcelain` check, so callers get a single
    canonical "nothing to commit" outcome there."""
    target_norm = Path(target).as_posix()
    out_of_scope = [
        Path(p).as_posix() for p in changed
        if Path(p).as_posix() != target_norm
    ]
    if out_of_scope:
        return False, f"out_of_scope:{','.join(out_of_scope[:3])}"
    return True, "ok"


# Paths the loop owns and writes to during an iteration. They must
# never count as "changes" for scope/validation purposes, otherwise a
# missing `.gitignore` or a misconfigured worktree would make every
# iteration look out-of-scope and the loop would silently never commit
# anything useful.
_INTERNAL_PATHS = {
    Path(".loop"),
    Path("STATE.md"),
}


def _is_internal_path(p: Path) -> bool:
    parts = p.parts
    if not parts:
        return False
    if Path(parts[0]) in _INTERNAL_PATHS:
        return True
    return p in _INTERNAL_PATHS


def _changed_paths() -> list[Path]:
    """Return every path in the working tree that differs from HEAD.

    Uses ``git status --porcelain=v1 -z -uall`` so the result includes
    modified *and* untracked files (`git diff` alone misses untracked
    additions, which would let an out-of-scope diff that creates a new
    file slip past `_diff_in_scope`). NUL-separated output is parsed so
    paths containing whitespace are handled correctly. Loop-internal
    artefacts (`.loop/...`, `STATE.md`) are filtered out so that a
    missing or stale `.gitignore` cannot cause every iteration to
    misclassify itself as out-of-scope.
    """
    proc = _run_git(
        "status", "--porcelain=v1", "-z", "-uall", check=False
    )
    out: list[Path] = []
    raw = proc.stdout
    i = 0
    n = len(raw)
    while i < n:
        # Each record: 2 status chars + ' ' + path + '\0'
        # Renames (R/C) look like 'R  newpath\0oldpath\0'.
        if i + 3 > n:
            break
        code = raw[i : i + 2]
        i += 3  # skip 'XY '
        end = raw.find("\0", i)
        if end < 0:
            break
        path = raw[i:end]
        i = end + 1
        if code[0] in ("R", "C"):
            # Rename/copy: a second NUL-terminated path follows (the source).
            end2 = raw.find("\0", i)
            if end2 < 0:
                break
            src = raw[i:end2]
            i = end2 + 1
            if src and not _is_internal_path(Path(src)):
                out.append(Path(src))
        if path and not _is_internal_path(Path(path)):
            out.append(Path(path))
    return out


def _revert_changes() -> bool:
    """Discard every working-tree change *and* untracked files.

    `git checkout -- .` only restores tracked files; untracked additions
    (e.g. a brand-new file produced by a model diff) survive it. We
    follow up with `git clean -fd` so the tree is identical to HEAD.

    On any failure, attempts a `git reset --hard HEAD` fallback. If
    HEAD itself is broken (corrupted ref, missing object), tries one
    more fallback to `git reset --hard origin/main` — the loop is
    the only writer to that ref, so resetting to it can only ever
    discard work this loop just produced and failed to commit, which
    is exactly what `_revert_changes` is meant to do anyway.

    Returns ``True`` on success, ``False`` if every fallback failed
    (failure is logged but never raised, so the outer loop keeps
    running).
    """
    ok = True
    co = _run_git("checkout", "--", ".", check=False)
    if co.returncode != 0:
        ok = False
        _REVERT_SWALLOW_LOG.report(
            RuntimeError(co.stderr.strip()[:200]),
            context=f"checkout rc={co.returncode}",
        )
    cl = _run_git("clean", "-fd", check=False)
    if cl.returncode != 0:
        ok = False
        _REVERT_SWALLOW_LOG.report(
            RuntimeError(cl.stderr.strip()[:200]),
            context=f"clean rc={cl.returncode}",
        )
    if not ok:
        rs = _run_git("reset", "--hard", "HEAD", check=False)
        if rs.returncode == 0:
            # `reset --hard` only restores tracked content; an untracked
            # file produced by the bad diff survives it. Re-run clean
            # so the tree is identical to HEAD on every successful
            # fallback. Best-effort: a second clean failure leaves the
            # file behind but `ok=True` is still correct because the
            # tracked tree is restored, and the next iteration's
            # in-scope check will reject any diff that touches it.
            _run_git("clean", "-fd", check=False)
            _log("_revert_changes: recovered via reset --hard")
            ok = True
        else:
            _REVERT_SWALLOW_LOG.report(
                RuntimeError(rs.stderr.strip()[:200]),
                context=f"reset --hard HEAD rc={rs.returncode}",
            )
            # Final fallback: HEAD itself may be broken. Try the
            # remote-tracking ref `origin/main` as the ground truth
            # (this loop is the sole writer, so it represents the last
            # known-good state).
            rs2 = _run_git(
                "reset", "--hard", "origin/main", check=False
            )
            if rs2.returncode == 0:
                # Same untracked-file caveat as the HEAD fallback.
                _run_git("clean", "-fd", check=False)
                _log(
                    "_revert_changes: recovered via reset --hard origin/main"
                )
                ok = True
            else:
                _REVERT_SWALLOW_LOG.report(
                    RuntimeError(rs2.stderr.strip()[:200]),
                    context=f"reset --hard origin/main rc={rs2.returncode}",
                )
    return ok


# ------------------------------------------------------------------- state
def _rotate_state_if_needed() -> Path | None:
    """If STATE.md is over the threshold, archive it to
    `.loop/state_archive/STATE.<UTC>.md` and leave a fresh header in
    place. Returns the archive path, or None if no rotation occurred.

    Rotation is best-effort: any error returns None and leaves
    STATE.md untouched. Archive directory is under `.loop/`, which is
    `.gitignore`d AND in `_INTERNAL_PATHS`, so the archive never
    enters a commit nor trips scope/internal-path filtering.
    """
    try:
        size = STATE_FILE.stat().st_size
    except FileNotFoundError:
        return None
    except OSError:
        return None
    if size <= _state_max_bytes():
        return None
    try:
        STATE_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archive = STATE_ARCHIVE_DIR / f"STATE.{ts}.md"
        # If a same-second archive already exists (clock skew, fast loop),
        # de-duplicate with a counter suffix rather than overwriting.
        if archive.exists():
            i = 1
            while (STATE_ARCHIVE_DIR / f"STATE.{ts}.{i}.md").exists():
                i += 1
            archive = STATE_ARCHIVE_DIR / f"STATE.{ts}.{i}.md"
        # Move the body atomically; replace it with a small header that
        # references the archive so future readers know history exists.
        os.replace(STATE_FILE, archive)
        header = (
            "# qwen-coder-mcp — Rolling State\n\n"
            f"_Previous entries archived to `{archive.relative_to(_REPO).as_posix()}`._\n\n"
        )
        STATE_FILE.write_text(header, "utf-8")
        _prune_state_archive(_state_archive_max_files())
        return archive
    except OSError:
        return None


def _prune_dir_oldest(directory: Path, max_files: int) -> int:
    """Delete oldest regular files in ``directory`` until at most
    ``max_files`` remain. Order is by mtime ascending. Subdirectories
    are skipped. Returns count deleted. Never raises."""
    try:
        if not directory.exists():
            return 0
        entries = [p for p in directory.iterdir() if p.is_file()]
        if len(entries) <= max_files:
            return 0
        entries.sort(key=lambda p: p.stat().st_mtime)
        excess = len(entries) - max_files
        deleted = 0
        for old in entries[:excess]:
            try:
                old.unlink()
                deleted += 1
            except OSError:
                pass
        return deleted
    except Exception as exc:  # never break the loop on cleanup
        _PRUNE_SWALLOW_LOG.report(exc, context=str(directory))
        return 0


def _state_archive_max_files() -> int:
    """Cap on retained `.loop/state_archive/*.md` files."""
    return _env_int_capped(
        "QWEN_STATE_ARCHIVE_MAX_FILES",
        _STATE_ARCHIVE_MAX_FILES_DEFAULT,
        _STATE_ARCHIVE_MAX_FILES_CAP,
    )


def _prune_state_archive(max_files: int) -> int:
    """Delete oldest STATE archive files until at most ``max_files`` remain."""
    return _prune_dir_oldest(STATE_ARCHIVE_DIR, max_files)


def _append_state(entry: str) -> None:
    """Append one entry to STATE.md. Observability — never raises.

    State persistence failures (disk full, permission denied, etc.) are
    rate-limit-logged but do not break the iteration; they are best-effort.
    """
    try:
        _rotate_state_if_needed()
        header = "# qwen-coder-mcp — Rolling State\n\n"
        if not STATE_FILE.exists():
            STATE_FILE.write_text(header, "utf-8")
        with STATE_FILE.open("a", encoding="utf-8") as fh:
            fh.write(entry)
    except Exception as exc:  # observability must never break the loop
        _STATE_SWALLOW_LOG.report(exc)


_HISTORY_MAX_FILES_DEFAULT = 500
_HISTORY_MAX_FILES_CAP = 100_000


def _history_max_files() -> int:
    """Cap on retained `.loop/history/*.md` files. Env-tunable."""
    return _env_int_capped(
        "QWEN_HISTORY_MAX_FILES",
        _HISTORY_MAX_FILES_DEFAULT,
        _HISTORY_MAX_FILES_CAP,
    )


def _prune_history(max_files: int) -> int:
    """Delete oldest history files until at most ``max_files`` remain."""
    return _prune_dir_oldest(HISTORY_DIR, max_files)


def _write_history(name: str, body: str) -> Path | None:
    """Write a history file under `.loop/history/`. Observability — never raises.

    Returns the resulting path on success, or None on failure
    (rate-limit-logged). Callers don't currently use the return value,
    but the contract is explicit so future callers can branch on the
    failure case.
    """
    try:
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        path = HISTORY_DIR / name
        path.write_text(body, "utf-8")
        _prune_history(_history_max_files())
        return path
    except Exception as exc:  # observability must never break the loop
        _HISTORY_SWALLOW_LOG.report(exc)
        return None


_STATE_SWALLOW_LOG = _RateLimitedSwallowLogger("_append_state", schedule="exponential")
_HISTORY_SWALLOW_LOG = _RateLimitedSwallowLogger("_write_history", schedule="exponential")
_PRUNE_SWALLOW_LOG = _RateLimitedSwallowLogger("_prune_dir_oldest", schedule="exponential")
_CURSOR_SWALLOW_LOG = _RateLimitedSwallowLogger("_save_cursor", schedule="exponential")
_GIT_REMOTE_SWALLOW_LOG = _RateLimitedSwallowLogger("git_remote", schedule="exponential")
_GIT_LOCAL_SWALLOW_LOG = _RateLimitedSwallowLogger("git_local", schedule="exponential")
_REVERT_SWALLOW_LOG = _RateLimitedSwallowLogger("_revert_changes", schedule="exponential")
_GIT_TIMEOUT_SWALLOW_LOG = _RateLimitedSwallowLogger("_run_git_timeout", schedule="exponential")
_EMPTY_COMMIT_SWALLOW_LOG = _RateLimitedSwallowLogger("git_empty_commit", schedule="exponential")


# -------------------------------------------------------------------- core
def _abort_rebase_if_any() -> None:
    """Best-effort: if a rebase is in progress, abort it. Then if the tree
    is still dirty, hard-reset to the current HEAD to guarantee a clean
    working tree before the next iteration.

    Loop 98 hardened the recovery path: if `reset --hard HEAD` fails
    (e.g., HEAD is broken), fall back to `reset --hard origin/main` --
    same ground truth used by `_revert_changes`. Failures are logged
    through `_REVERT_SWALLOW_LOG` (rate-limited) so a persistent
    corruption surfaces in the per-iteration delta channel.
    """
    rebase_dir_a = _REPO / ".git" / "rebase-merge"
    rebase_dir_b = _REPO / ".git" / "rebase-apply"
    if rebase_dir_a.exists() or rebase_dir_b.exists():
        _run_git("rebase", "--abort", check=False)
    status = _run_git("status", "--porcelain", check=False).stdout
    if not status.strip():
        return
    rs = _run_git("reset", "--hard", "HEAD", check=False)
    if rs.returncode != 0:
        _REVERT_SWALLOW_LOG.report(
            RuntimeError(rs.stderr.strip()[:200]),
            context=f"abort_rebase reset --hard HEAD rc={rs.returncode}",
        )
        rs2 = _run_git("reset", "--hard", "origin/main", check=False)
        if rs2.returncode != 0:
            _REVERT_SWALLOW_LOG.report(
                RuntimeError(rs2.stderr.strip()[:200]),
                context=(
                    f"abort_rebase reset --hard origin/main "
                    f"rc={rs2.returncode}"
                ),
            )
            # Tree stays dirty; downstream `_diff_in_scope` will reject
            # any diff that touches the orphaned files, so the loop
            # remains correct -- just slower until the operator
            # intervenes.
    _run_git("clean", "-fd", check=False)


def _commit_and_push(message: str, push: bool) -> str:
    """Stage, commit and (optionally) push.

    Returns a tri-state status string:
      - "ok"     — commit (and push, if requested) succeeded
      - "empty"  — staged tree was empty; nothing to commit (anomalous
                   when called from `_iteration`, which only invokes us
                   after a non-empty `_changed_paths()`)
      - "failed" — git command failed (add, commit, pull-rebase, push)
    """
    add = _run_git("add", "-A", check=False)
    if add.returncode != 0:
        _GIT_LOCAL_SWALLOW_LOG.report(RuntimeError(add.stderr.strip()), context="git add")
        return "failed"
    status = _run_git("status", "--porcelain", check=False).stdout
    if not status.strip():
        # Anomalous: caller only invokes us after `_apply_diff` succeeded
        # and `_changed_paths()` was non-empty, so an empty staged tree
        # here means the changes were lost between apply and commit
        # (e.g., an external reset, or all changes were .gitignore'd).
        # Rate-limited because a persistent fault (e.g., a .gitignore
        # rule that captures every diff target) would otherwise emit one
        # line per iteration.
        _EMPTY_COMMIT_SWALLOW_LOG.report(
            RuntimeError("apply produced no committable changes")
        )
        return "empty"
    commit = _run_git("commit", "-m", message, check=False)
    if commit.returncode != 0:
        _GIT_LOCAL_SWALLOW_LOG.report(RuntimeError(commit.stderr.strip()), context="git commit")
        return "failed"
    if not push:
        return "ok"
    # Best-effort sync with remote. On rebase conflict, abort cleanly so
    # the next iteration starts from a known-good tree instead of wedging.
    pull = _run_git("pull", "--rebase", "--autostash", "origin", "main", check=False)
    if pull.returncode != 0:
        _GIT_REMOTE_SWALLOW_LOG.report(
            RuntimeError(pull.stderr.strip()[:300]),
            context="git pull --rebase",
        )
        _abort_rebase_if_any()
        return "failed"
    push_proc = _run_git("push", "origin", "HEAD:main", check=False)
    if push_proc.returncode != 0:
        _GIT_REMOTE_SWALLOW_LOG.report(
            RuntimeError(push_proc.stderr.strip()),
            context="git push",
        )
        return "failed"
    return "ok"


_TIMING_MAX_BYTES_DEFAULT = 1_000_000
_TIMING_MAX_BYTES_CAP = 100_000_000


def _timing_max_bytes() -> int:
    """Cap for `.loop/timing.log` size before rotation. Env-tunable."""
    return _env_int_capped(
        "QWEN_TIMING_MAX_BYTES",
        _TIMING_MAX_BYTES_DEFAULT,
        _TIMING_MAX_BYTES_CAP,
    )


def _rotate_timing_if_oversized() -> None:
    """If the timing log exceeds the cap, rename it to `.1` and start fresh."""
    _rotate_log_if_oversized(TIMING_FILE, _timing_max_bytes())


_TIMING_SWALLOW_LOG = _RateLimitedSwallowLogger("_write_timing", schedule="exponential")


def _swallow_loggers() -> tuple["_RateLimitedSwallowLogger", ...]:
    """Module-level swallow loggers we report periodic summaries for.

    Kept as a function (rather than a constant) so tests that swap
    individual loggers on the module don't see a stale tuple.
    """
    return (
        _TIMING_SWALLOW_LOG,
        _STATE_SWALLOW_LOG,
        _HISTORY_SWALLOW_LOG,
        _PRUNE_SWALLOW_LOG,
        _CURSOR_SWALLOW_LOG,
        _GIT_REMOTE_SWALLOW_LOG,
        _GIT_LOCAL_SWALLOW_LOG,
        _REVERT_SWALLOW_LOG,
        _GIT_TIMEOUT_SWALLOW_LOG,
        _EMPTY_COMMIT_SWALLOW_LOG,
    )


_LAST_SWALLOW_SUMMARY_COUNTS: dict[str, int] = {}


def _log_swallow_summaries() -> None:
    """Emit one summary line per module logger that has *new* suppressed
    failures since the last summary call.

    Called at every iteration boundary so a sink that's been quietly
    failing is visible even while the rate limiter is suppressing
    per-failure logs. We track each logger's count from the last
    summary so a fault that has *stopped* doesn't keep re-logging the
    same stale snapshot every iteration.

    Best-effort; never raises.
    """
    try:
        for logger in _swallow_loggers():
            s = logger.summary()
            count = int(s.get("count", 0))
            label = str(s.get("label", "?"))
            last = _LAST_SWALLOW_SUMMARY_COUNTS.get(label, 0)
            if count <= last:
                continue  # nothing new this iteration
            _LAST_SWALLOW_SUMMARY_COUNTS[label] = count
            if int(s.get("suppressed", 0)) > 0:
                _log(
                    f"swallow-summary {label}: count={count} "
                    f"last_logged={s['last_logged_count']} "
                    f"suppressed={s['suppressed']}"
                )
    except Exception:  # observability: must never break the loop
        pass


def _write_timing(
    rel: Path,
    outcome: str,
    phases: dict[str, float],
    *,
    iter_monotonic: float | None = None,
) -> None:
    """Append one JSON line per iteration capturing per-phase wallclock.

    Phases are recorded only when actually entered, so an early-exit
    iteration produces a partial record. Failures are swallowed: timing
    is observability, not correctness, and must never break the loop.

    When ``iter_monotonic`` is provided (the ``time.monotonic()`` value
    captured at the start of ``_iteration``), the record includes a
    ``wall_s`` field with the total iteration wallclock. Unlike
    ``sum(phases.values())`` this also captures unnamed scaffolding time
    (between phases, error paths) so analytics can distinguish "slow
    Qwen response" from "slow setup/teardown". A companion
    ``wall_s_delta_phases`` field records ``max(0, wall_s - sum(phases))``
    so the scaffolding overhead is directly queryable without recomputing
    in every analytics consumer; the floor at 0 protects against
    sub-millisecond float dust where the rounded ``wall_s`` could be
    fractionally below the unrounded phase sum.

    Failure logging is rate-limited via `_RateLimitedSwallowLogger` so a
    persistent fault (disk full, permission denied) doesn't fill
    runtime.log with one swallow line per iteration. The first failure
    and every Nth subsequent failure are logged together with the
    cumulative count.
    """
    try:
        import json
        TIMING_FILE.parent.mkdir(parents=True, exist_ok=True)
        _rotate_timing_if_oversized()
        record: dict[str, object] = {
            "ts": _now(),
            "file": rel.as_posix(),
            "outcome": outcome,
            "category": _outer_outcome_category(outcome),
            "phases": {k: round(v, 4) for k, v in phases.items()},
        }
        if iter_monotonic is not None:
            wall_s = round(time.monotonic() - iter_monotonic, 4)
            record["wall_s"] = wall_s
            phase_sum = sum(phases.values())
            record["wall_s_delta_phases"] = round(
                max(0.0, wall_s - phase_sum), 4
            )
        with TIMING_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception as exc:  # never break the loop on logging
        _TIMING_SWALLOW_LOG.report(exc)


class _PhaseTimer:
    """Context manager that records elapsed seconds for one phase."""

    def __init__(self, phases: dict[str, float], name: str) -> None:
        self._phases = phases
        self._name = name
        self._start = 0.0

    def __enter__(self) -> "_PhaseTimer":
        self._start = time.monotonic()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._phases[self._name] = time.monotonic() - self._start


def _iteration(client: QwenClient, max_bytes: int, push: bool) -> str:
    # Belt-and-suspenders: if the previous iteration left the tree dirty
    # (revert failed, crash, etc), reset before reading any file so the
    # current iteration cannot read a stale modification as if it were
    # the canonical source.
    _abort_rebase_if_any()

    # Cache one timestamp per iteration so all state.md / history-md
    # records emitted by this iteration share the exact same `ts`.
    # Without this, a slow iteration could span a clock-second boundary
    # and produce records that *look* like they came from different
    # iterations on later log review.
    iter_ts = _now()
    iter_monotonic = time.monotonic()

    # Loop 99: emit timing + swallow summaries for early-exit iterations
    # too (no-candidate-files, unreadable-file). Without this, analytics
    # counting "iterations per hour" undercount and a persistent fault
    # (e.g., every file too large) would cause swallow summaries to
    # silently stop firing, masking the underlying problem.
    def _finish_no_file(outcome: str, rel_for_timing: Path) -> str:
        _write_timing(
            rel_for_timing, outcome, {}, iter_monotonic=iter_monotonic
        )
        _log_swallow_summaries()
        return outcome

    deadline = time.monotonic() + _iteration_budget_seconds()
    phases: dict[str, float] = {}

    def _over_budget() -> bool:
        return time.monotonic() > deadline

    # Loop 108: wrap discovery + read in a `discovery` phase so timing.log
    # reflects how long file selection takes vs the Qwen calls. Without
    # this, a slow filesystem (cold cache, networked storage) shows up
    # only as elevated `wall_s_delta_phases` -- meaningful but harder
    # to attribute. With the explicit phase, dashboards can rank repos
    # by discovery cost and detect filesystem regressions. Note that
    # the early-exit paths emit `phases={}` via `_finish_no_file` so
    # they don't get a `discovery` row -- correct, since their
    # iteration was a no-op from the caller's perspective.
    discovery_phases: dict[str, float] = {}
    with _PhaseTimer(discovery_phases, "discovery"):
        files = _candidate_files()
        if files:
            idx = _load_cursor() % len(files)
            rel = files[idx]
            _save_cursor((idx + 1) % len(files))
            code = _read_file(_REPO / rel, max_bytes)
        else:
            rel = None
            code = None
    if not files:
        return _finish_no_file("no_candidate_files", Path("."))
    if code is None:
        return _finish_no_file(
            f"skip:{rel} (unreadable_or_too_large)", rel
        )
    phases["discovery"] = discovery_phases["discovery"]

    def _finish(outcome: str) -> str:
        _write_timing(rel, outcome, phases, iter_monotonic=iter_monotonic)
        _log_swallow_summaries()
        return outcome

    _log(f"scanning {rel}")
    # Loop 107: budget-check after file discovery + read so a slow
    # `_candidate_files()` or `_read_file()` (e.g., huge repo with cold
    # filesystem cache) can't burn the entire budget before the first
    # Qwen call even starts. Without this, the first `_over_budget()`
    # check is only after find_bugs completes, by which point
    # discovery+read+find_bugs may have all run past the deadline.
    if _over_budget():
        return _finish(f"budget_exceeded:{rel}:after_discovery")
    try:
        with _PhaseTimer(phases, "find_bugs"):
            issues = client.system_user(
                prompts.REVIEWER_SYSTEM,
                prompts.find_bugs_user(str(rel), code),
                temperature=0.1,
            )
    except QwenError as exc:
        return _finish(f"qwen_error_find_bugs:{exc}")

    if _over_budget():
        return _finish(f"budget_exceeded:{rel}:after_find_bugs")

    issue = _parse_first_issue(issues)
    if not issue:
        return _finish(f"clean:{rel}")

    try:
        with _PhaseTimer(phases, "propose_fix"):
            diff = client.system_user(
                prompts.CODER_SYSTEM,
                prompts.propose_fix_user(str(rel), code, issue),
                temperature=0.1,
            )
    except QwenError as exc:
        return _finish(f"qwen_error_propose_fix:{exc}")

    if _over_budget():
        return _finish(f"budget_exceeded:{rel}:after_propose_fix")

    diff_clean = _strip_fence(diff)

    try:
        with _PhaseTimer(phases, "devils_advocate"):
            critique = client.system_user(
                prompts.DEVILS_ADVOCATE_SYSTEM,
                prompts.devils_advocate_user(str(rel), code, diff_clean, issue),
                temperature=0.0,
            )
    except QwenError as exc:
        return _finish(f"qwen_error_devils_advocate:{exc}")

    if _over_budget():
        return _finish(f"budget_exceeded:{rel}:after_devils_advocate")

    accept, reason = _verdict_accepts(critique)
    history_body = (
        f"# {iter_ts} — {rel}\n\n"
        f"## Issue\n{issue}\n\n## Proposed diff\n```diff\n{diff_clean}\n```\n\n"
        f"## Devil's advocate\n{critique}\n\n## Outcome\n"
    )

    if not accept:
        _write_history(
            f"{int(time.time())}-rejected.md",
            history_body + f"REJECTED ({reason})\n",
        )
        _append_state(
            f"- {iter_ts} `{rel}` — rejected fix ({reason[:80]})\n"
        )
        return _finish(f"rejected:{rel}:{reason[:80]}")

    with _PhaseTimer(phases, "apply_diff"):
        ok, msg = _apply_diff(diff_clean)
    if not ok:
        category = _apply_error_category(msg)
        _write_history(
            f"{int(time.time())}-apply-failed.md",
            history_body + f"APPLY FAILED ({msg})\n",
        )
        _append_state(f"- {iter_ts} `{rel}` — apply failed [{category}] ({msg[:80]})\n")
        return _finish(f"apply_failed:{category}:{rel}:{msg[:60]}")

    changed = _changed_paths()
    scope_ok, scope_msg = _diff_in_scope(changed, rel)
    if not scope_ok:
        with _PhaseTimer(phases, "revert"):
            rev_ok = _revert_changes()
        _write_history(
            f"{int(time.time())}-out-of-scope.md",
            history_body + f"OUT OF SCOPE ({scope_msg})\n",
        )
        _append_state(f"- {iter_ts} `{rel}` — reverted ({scope_msg[:60]})\n")
        if not rev_ok:
            return _finish(f"revert_failed:{rel}:after_out_of_scope")
        return _finish(f"out_of_scope:{rel}:{scope_msg[:80]}")

    with _PhaseTimer(phases, "validate"):
        syn_ok, syn_msg = _validate_changed_files(changed)
    if not syn_ok:
        with _PhaseTimer(phases, "revert"):
            rev_ok = _revert_changes()
        _write_history(
            f"{int(time.time())}-syntax-failed.md",
            history_body + f"VALIDATION FAILED:\n```\n{syn_msg}\n```\n",
        )
        _append_state(f"- {iter_ts} `{rel}` — reverted ({syn_msg[:60]})\n")
        if not rev_ok:
            return _finish(f"revert_failed:{rel}:after_validation:{syn_msg.split(':', 1)[0]}")
        return _finish(f"validation_failed:{rel}:{syn_msg.split(':', 1)[0]}")

    summary_line = issue.splitlines()[0][:72]
    commit_msg = f"fix({rel.as_posix()}): {summary_line}"
    with _PhaseTimer(phases, "commit_push"):
        commit_status = _commit_and_push(commit_msg, push)
    if commit_status == "ok":
        _write_history(
            f"{int(time.time())}-applied.md",
            history_body + "APPLIED + COMMITTED\n",
        )
        _append_state(f"- {iter_ts} `{rel}` — applied: {summary_line}\n")
        return _finish(f"applied:{rel}")

    with _PhaseTimer(phases, "revert"):
        rev_ok = _revert_changes()
    if commit_status == "empty":
        _append_state(f"- {iter_ts} `{rel}` — commit skipped: empty staged tree, reverted\n")
    else:
        _append_state(f"- {iter_ts} `{rel}` — commit/push failed, reverted\n")
    if not rev_ok:
        return _finish(f"revert_failed:{rel}:after_commit_push")
    if commit_status == "empty":
        return _finish(f"commit_skipped_empty:{rel}")
    return _finish(f"commit_failed:{rel}")


_AGGREGATE_SUMMARY_EVERY_DEFAULT = 100
_AGGREGATE_SUMMARY_EVERY_MAX = 100_000


def _aggregate_summary_every() -> int:
    """How often (in iterations) ``main()`` emits the aggregate
    swallow-logger snapshot. Env-tunable via
    ``QWEN_AGGREGATE_SUMMARY_EVERY``; clamped to (0, 100_000].
    """
    return _env_int_capped(
        "QWEN_AGGREGATE_SUMMARY_EVERY",
        _AGGREGATE_SUMMARY_EVERY_DEFAULT,
        _AGGREGATE_SUMMARY_EVERY_MAX,
    )


def _log_aggregate_swallow_summary(iteration_count: int) -> None:
    """Emit a single aggregate line summarising every swallow logger's
    cumulative count at the current point in the run. Only emits if
    *some* logger has a non-zero count (avoids noise on healthy runs).

    Unlike ``_log_swallow_summaries`` (per-iteration delta detection),
    this is an unconditional cumulative snapshot useful for long-run
    diagnostics where transient suppressions may have stopped reporting
    via the per-iteration channel.
    """
    try:
        parts: list[str] = []
        any_nonzero = False
        for lg in _swallow_loggers():
            try:
                s = lg.summary()
                count = int(s.get("count", 0))
                if count > 0:
                    any_nonzero = True
                parts.append(f"{s.get('label', '?')}={count}")
            except Exception:
                continue
        if not any_nonzero:
            return
        _log(f"aggregate-swallow-summary iter={iteration_count} " + " ".join(parts))
    except Exception:
        # Aggregate logging is observability; never break the outer loop.
        pass


def _dump_logger_state(reason: str = "manual", iteration: int | None = None) -> None:
    """Emit a multi-line snapshot of every swallow logger's full
    summary (including ``last_log_message``). Intended to be wired to a
    SIGUSR1 handler so operators can pull a real-time report from a
    long-running daemon without restarting it or grepping loop.log.

    Unlike ``_log_aggregate_swallow_summary``, this dump always emits
    (even when all counts are zero) so a SIGUSR1 ping always produces a
    visible response. Each logger's full summary dict is written on its
    own line for grep-friendliness.

    When ``iteration`` is provided it is included in the begin marker so
    operators can correlate the snapshot with the current loop position.
    The cached delta-summary state in ``_LAST_SWALLOW_SUMMARY_COUNTS``
    is also dumped so suppressed-but-not-yet-summarised counts are
    visible.
    """
    try:
        iter_part = f" iter={iteration}" if iteration is not None else ""
        _log(f"logger-state-dump reason={reason}{iter_part} begin")
        for lg in _swallow_loggers():
            try:
                s = lg.summary()
            except Exception as exc:
                _log(f"logger-state-dump summary failed: {exc}")
                continue
            try:
                _log(f"logger-state-dump {s}")
            except Exception:
                continue
        try:
            _log(
                "logger-state-dump last-summary-counts "
                f"{dict(_LAST_SWALLOW_SUMMARY_COUNTS)}"
            )
        except Exception:
            pass
        _log(f"logger-state-dump reason={reason}{iter_part} end")
    except Exception:
        # Observability path; never raise.
        pass


_CURRENT_ITERATION: int = 0


def _install_sigusr1_handler() -> bool:
    """Install a SIGUSR1 handler that calls ``_dump_logger_state``.
    Returns True if the handler was installed, False if the platform
    doesn't support SIGUSR1 (Windows) or installation failed.
    """
    try:
        import signal
        if not hasattr(signal, "SIGUSR1"):
            return False
        signal.signal(
            signal.SIGUSR1,
            lambda _signum, _frame: _dump_logger_state(
                reason="sigusr1", iteration=_CURRENT_ITERATION
            ),
        )
        return True
    except Exception:
        return False


def main() -> None:
    from qwen_coder_mcp.config import load_settings  # local import
    settings = load_settings()
    LOOP_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    _log(
        f"loop starting | model={settings.model} base={settings.base_url} "
        f"interval={settings.loop_interval_seconds}s push={settings.loop_push}"
    )
    client = QwenClient(settings)
    iteration_count = 0
    aggregate_every = _aggregate_summary_every()
    sigusr1_installed = _install_sigusr1_handler()
    _log(
        f"loop diagnostics | aggregate_summary_every={aggregate_every} "
        f"sigusr1_handler={'installed' if sigusr1_installed else 'unavailable'}"
    )
    try:
        while True:
            iter_monotonic_outer = time.monotonic()
            try:
                outcome = _iteration(
                    client, settings.loop_max_file_bytes, settings.loop_push
                )
                _log(f"iteration [{_outer_outcome_category(outcome)}] -> {outcome}")
            except Exception:  # never break the loop
                _log("iteration crashed:\n" + traceback.format_exc())
                # Loop 104: a crash inside `_iteration` skips both
                # `_finish` and `_finish_no_file`, so the per-iteration
                # swallow summary cycle never fires and any sink
                # failures that already incremented before the crash
                # stay hidden. If every iteration crashes (e.g., a
                # regression in `_candidate_files`), the delta channel
                # would silently stop forever. Run the cycle here as a
                # best-effort fallback. `_log_swallow_summaries` is
                # itself try/except-wrapped so it cannot re-raise.
                _log_swallow_summaries()
                # Loop 105: emit a synthetic timing.log record so the
                # crash is visible to analytics counting outcomes per
                # category. Without this, runtime.log has the
                # traceback but timing.log undercounts iterations and
                # crash-rate dashboards have no signal. `_write_timing`
                # is itself rate-limited via `_TIMING_SWALLOW_LOG` so
                # this best-effort write cannot raise.
                try:
                    _write_timing(
                        Path("."),
                        "crashed",
                        {},
                        iter_monotonic=iter_monotonic_outer,
                    )
                except Exception:  # observability: never break the loop
                    pass
            iteration_count += 1
            global _CURRENT_ITERATION
            _CURRENT_ITERATION = iteration_count
            if aggregate_every > 0 and iteration_count % aggregate_every == 0:
                _log_aggregate_swallow_summary(iteration_count)
            time.sleep(max(1, settings.loop_interval_seconds))
    finally:
        client.close()


if __name__ == "__main__":
    main()
