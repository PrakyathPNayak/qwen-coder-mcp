"""Loop 129: filesystem MCP tools (read_file, list_dir, write_file,
apply_patch) for claude-code / ml-intern style filesystem access.

All paths are confined to a configurable repository root via realpath
resolution -- a relative or symlinked path that escapes the root is
rejected. Reads and writes are byte-capped. apply_patch shells out to
`git apply` for unified-diff application.

Pure helpers; the server layer owns the only mutable state (the
configured root).
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MAX_READ_BYTES = 200_000
DEFAULT_MAX_WRITE_BYTES = 1_000_000
DEFAULT_MAX_LIST_ENTRIES = 500


@dataclass(frozen=True)
class FsConfig:
    root: Path
    max_read_bytes: int = DEFAULT_MAX_READ_BYTES
    max_write_bytes: int = DEFAULT_MAX_WRITE_BYTES
    max_list_entries: int = DEFAULT_MAX_LIST_ENTRIES


class FsError(Exception):
    """Raised on any filesystem-tool error -- caller surfaces as text."""


def _resolve_inside_root(cfg: FsConfig, rel: str) -> Path:
    """Resolve `rel` against `cfg.root` and reject any path that, after
    symlink resolution, escapes the root. The root itself is allowed."""
    if rel is None:
        raise FsError("path must be provided")
    rel = str(rel)
    if not rel:
        raise FsError("path must be non-empty")
    root = cfg.root.resolve(strict=False)
    candidate = (root / rel).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise FsError(f"path escapes repo root: {rel}") from exc
    return candidate


def read_file(
    cfg: FsConfig,
    rel: str,
    *,
    start_line: int | None = None,
    end_line: int | None = None,
    max_lines: int | None = None,
    line_numbers: bool = False,
) -> dict[str, object]:
    """Return ``{path, size, text, truncated, ...}`` for a file inside the root.

    Loop 252 added line-range reads: when ``start_line`` (1-based,
    inclusive) and/or ``end_line`` (inclusive) are provided the
    returned ``text`` covers only that slice. ``max_lines`` caps the
    slice size after the range is applied so a model that asks for
    "lines 1..1e9" still receives a bounded payload. ``line_numbers``
    prefixes each emitted line with ``"<n> | "`` so subsequent
    ``edit_file`` calls can quote exact context unambiguously.

    Range semantics:
      - 1-based, inclusive on both ends, like grep -n / less.
      - ``start_line=None`` defaults to 1; ``end_line=None`` defaults
        to the last line. Negative values count from the end (-1 is
        the last line) for tail-style reads.
      - When the range slices the file, ``range`` is included in the
        result and ``truncated`` reflects the slice (not the on-disk
        size). The full ``size`` and ``total_lines`` fields are always
        returned so a model can plan follow-up reads.

    The byte cap (``max_read_bytes``) still applies *after* slicing so
    a tiny range of a huge file is cheap.
    """
    p = _resolve_inside_root(cfg, rel)
    if not p.exists():
        raise FsError(f"not found: {rel}")
    if p.is_dir():
        raise FsError(f"is a directory: {rel}")
    raw = p.read_bytes()
    full_size = len(raw)
    try:
        full_text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FsError(f"binary file: {rel}") from exc

    range_active = (
        start_line is not None or end_line is not None or max_lines is not None
    )
    if not range_active and not line_numbers:
        truncated = full_size > cfg.max_read_bytes
        if truncated:
            body = raw[: cfg.max_read_bytes]
            try:
                text = body.decode("utf-8")
            except UnicodeDecodeError:
                # Mid-multibyte truncation -- fall back to replacement
                # so the caller still gets useful text. The full file
                # decoded fine above so the file itself is text.
                text = body.decode("utf-8", errors="replace")
        else:
            text = full_text
        return {
            "path": rel,
            "size": full_size,
            "text": text,
            "truncated": truncated,
        }

    lines = full_text.splitlines(keepends=True)
    total_lines = len(lines)

    def _norm(idx: int | None, default: int) -> int:
        if idx is None:
            return default
        if idx < 0:
            return max(1, total_lines + idx + 1)
        return idx

    s = _norm(start_line, 1)
    e = _norm(end_line, total_lines)
    if s < 1:
        s = 1
    if e > total_lines:
        e = total_lines
    if s > e:
        # Empty slice -- still legal, returns "".
        slice_lines: list[str] = []
    else:
        slice_lines = lines[s - 1 : e]
    cap = max_lines if max_lines is not None else len(slice_lines)
    if cap < 0:
        cap = 0
    if len(slice_lines) > cap:
        slice_lines = slice_lines[:cap]
        e = s + cap - 1
    if line_numbers:
        width = len(str(max(1, e)))
        slice_lines = [
            f"{str(s + i).rjust(width)} | {ln}" for i, ln in enumerate(slice_lines)
        ]
    text = "".join(slice_lines)
    encoded_len = len(text.encode("utf-8", errors="replace"))
    truncated = encoded_len > cfg.max_read_bytes
    if truncated:
        text = text.encode("utf-8", errors="replace")[: cfg.max_read_bytes].decode(
            "utf-8", errors="replace"
        )
    out: dict[str, object] = {
        "path": rel,
        "size": full_size,
        "total_lines": total_lines,
        "text": text,
        "truncated": truncated,
    }
    if range_active or line_numbers:
        out["range"] = {"start": s, "end": e}
    return out


def edit_file(
    cfg: FsConfig,
    rel: str,
    old: str,
    new: str,
    *,
    count: int | None = 1,
    create_parents: bool = False,
) -> dict[str, object]:
    """Surgical string-replace in an existing file (loop 252).

    ``old`` must occur exactly ``count`` times in the current file
    contents; if it does not the call is rejected with a precise error
    so the model can re-read with more context and retry.

    ``count=None`` means "replace every occurrence" -- explicit
    sentinel because silent global replace would be dangerous.

    ``count=1`` (the default) is the safest mode: enforces uniqueness
    so a fuzzy match against a generic snippet doesn't accidentally
    rewrite the wrong block.

    Returns ``{path, replacements, size, before_size}``.
    """
    p = _resolve_inside_root(cfg, rel)
    if p.is_dir():
        raise FsError(f"is a directory: {rel}")
    if not p.exists():
        raise FsError(f"not found: {rel}")
    if not isinstance(old, str) or old == "":
        raise FsError("edit_file requires a non-empty 'old' string")
    if not isinstance(new, str):
        raise FsError("edit_file requires a string 'new' value")
    raw = p.read_bytes()
    try:
        original = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FsError(f"binary file: {rel}") from exc
    occurrences = original.count(old)
    if occurrences == 0:
        # Helpful hint: surface a snippet of nearby lines so the model
        # can correct its context without a full re-read.
        head = original.splitlines()[:20]
        head_preview = "\n".join(head)
        raise FsError(
            f"edit_file: 'old' not found in {rel}. "
            f"first 20 lines for re-orientation:\n{head_preview}"
        )
    if count is not None and occurrences != count:
        raise FsError(
            f"edit_file: 'old' occurs {occurrences}x in {rel} but count={count}. "
            "Add more surrounding context to make the match unique, "
            "or pass count=null to replace all occurrences."
        )
    replaced = original.replace(old, new) if count is None else original.replace(
        old, new, count
    )
    encoded = replaced.encode("utf-8")
    if len(encoded) > cfg.max_write_bytes:
        raise FsError(
            f"edited content too large ({len(encoded)} > {cfg.max_write_bytes})"
        )
    if create_parents:
        p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(encoded)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp, p)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise FsError(f"edit failed: {exc}") from exc
    actual = occurrences if count is None else min(count, occurrences)
    return {
        "path": rel,
        "replacements": actual,
        "size": len(encoded),
        "before_size": len(raw),
    }


def insert_lines(
    cfg: FsConfig,
    rel: str,
    *,
    after_line: int | None = None,
    before_line: int | None = None,
    content: str = "",
) -> dict[str, object]:
    """Insert ``content`` at the given line position (loop 252).

    Exactly one of ``after_line`` / ``before_line`` must be set (1-based).
    ``after_line=0`` and ``before_line=1`` both mean "prepend".
    ``after_line=total_lines`` means "append".

    ``content`` is inserted as-is; the caller is responsible for
    trailing newlines so the model has byte-level control.
    """
    p = _resolve_inside_root(cfg, rel)
    if p.is_dir():
        raise FsError(f"is a directory: {rel}")
    if not p.exists():
        raise FsError(f"not found: {rel}")
    if (after_line is None) == (before_line is None):
        raise FsError(
            "insert_lines requires exactly one of after_line / before_line"
        )
    if not isinstance(content, str):
        raise FsError("insert_lines requires a string 'content' arg")
    raw = p.read_bytes()
    try:
        original = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FsError(f"binary file: {rel}") from exc
    lines = original.splitlines(keepends=True)
    total = len(lines)
    if after_line is not None:
        idx = after_line
        if idx < 0:
            idx = max(0, total + idx + 1)
        if idx < 0 or idx > total:
            raise FsError(
                f"after_line out of range: {after_line} (file has {total} lines)"
            )
    else:
        b = before_line  # type: ignore[assignment]
        if b is None:  # pragma: no cover -- guarded above
            raise FsError("internal: before_line None after guard")
        if b < 1:
            b = 1
        if b > total + 1:
            raise FsError(
                f"before_line out of range: {before_line} (file has {total} lines)"
            )
        idx = b - 1
    new_lines = lines[:idx] + [content] + lines[idx:]
    new_text = "".join(new_lines)
    encoded = new_text.encode("utf-8")
    if len(encoded) > cfg.max_write_bytes:
        raise FsError(
            f"inserted content too large ({len(encoded)} > {cfg.max_write_bytes})"
        )
    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(encoded)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp, p)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise FsError(f"insert failed: {exc}") from exc
    return {
        "path": rel,
        "size": len(encoded),
        "before_size": len(raw),
        "inserted_at": idx,
    }


def list_dir(cfg: FsConfig, rel: str = ".") -> dict[str, object]:
    """Return `{path, entries: [{name, kind, size}], truncated}`."""
    p = _resolve_inside_root(cfg, rel) if rel else cfg.root.resolve(strict=False)
    if not p.exists():
        raise FsError(f"not found: {rel}")
    if not p.is_dir():
        raise FsError(f"not a directory: {rel}")
    entries: list[dict[str, object]] = []
    try:
        names = sorted(os.listdir(p))
    except OSError as exc:
        raise FsError(f"listdir failed: {exc}") from exc
    truncated = len(names) > cfg.max_list_entries
    for name in names[: cfg.max_list_entries]:
        child = p / name
        try:
            if child.is_dir():
                entries.append({"name": name, "kind": "dir", "size": 0})
            elif child.is_symlink():
                entries.append({"name": name, "kind": "symlink", "size": 0})
            else:
                entries.append(
                    {"name": name, "kind": "file", "size": child.stat().st_size}
                )
        except OSError:
            entries.append({"name": name, "kind": "unknown", "size": 0})
    return {"path": rel, "entries": entries, "truncated": truncated}


def write_file(cfg: FsConfig, rel: str, content: str, *, create_parents: bool = False) -> dict[str, object]:
    """Write `content` (utf-8) to `rel` inside the root. Returns metadata.

    Refuses to write if `len(content.encode('utf-8')) > max_write_bytes`.
    Creates parent directories only if `create_parents=True`."""
    p = _resolve_inside_root(cfg, rel)
    if p.is_dir():
        raise FsError(f"is a directory: {rel}")
    encoded = content.encode("utf-8")
    if len(encoded) > cfg.max_write_bytes:
        raise FsError(
            f"content too large ({len(encoded)} > {cfg.max_write_bytes})"
        )
    if create_parents:
        p.parent.mkdir(parents=True, exist_ok=True)
    elif not p.parent.exists():
        raise FsError(f"parent does not exist: {p.parent}")
    # Atomic write via .tmp + os.replace so a crash mid-write can never
    # leave the target file half-written. Mirrors save_agent_checkpoint
    # and save_history_jsonl. The .tmp lives next to the target so
    # os.replace stays within one filesystem.
    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(encoded)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp, p)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise FsError(f"write failed: {exc}") from exc
    return {"path": rel, "size": len(encoded), "created": True}


def apply_patch(cfg: FsConfig, diff_text: str, *, check_only: bool = False) -> dict[str, object]:
    """Apply a unified diff via `git apply`. Returns `{ok, message}`.

    `check_only=True` runs `git apply --check` -- useful for the TUI to
    preview applicability without mutating the tree.
    """
    if not diff_text or not diff_text.strip():
        raise FsError("diff is empty")
    root = cfg.root.resolve(strict=False)
    cmd = ["git", "apply"]
    if check_only:
        cmd.append("--check")
    with tempfile.NamedTemporaryFile(
        "w", suffix=".diff", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(diff_text)
        diff_path = fh.name
    try:
        cmd.append(diff_path)
        proc = subprocess.run(
            cmd,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=30,
        )
        ok = proc.returncode == 0
        msg = (proc.stdout + proc.stderr).strip()
        return {"ok": ok, "check_only": check_only, "message": msg}
    finally:
        try:
            os.unlink(diff_path)
        except OSError:
            pass


def format_read(res: dict[str, object]) -> str:
    head = f"# {res['path']} (size={res['size']}"
    if res.get("truncated"):
        head += ", truncated"
    head += ")\n"
    return head + str(res.get("text", ""))


def format_list(res: dict[str, object]) -> str:
    lines = [f"# {res['path']}"]
    for e in res["entries"]:  # type: ignore[index]
        kind = e["kind"]
        size = e.get("size", 0)
        if kind == "dir":
            lines.append(f"  {e['name']}/")
        elif kind == "symlink":
            lines.append(f"  {e['name']} -> (symlink)")
        else:
            lines.append(f"  {e['name']}  {size}")
    if res.get("truncated"):
        lines.append("  ... truncated")
    return "\n".join(lines)
