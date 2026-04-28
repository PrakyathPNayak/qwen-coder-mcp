"""Persistent task / todo memory injected into Qwen system prompts.

Loop 244: user reported the model "stops abruptly and forgets context
often". The pre-loop-244 client kept *no* state between turns: every
chat call shipped only the immediate message history. When that
history compressed (loop 240) or was dropped between sessions, the
model lost all knowledge of:

  * what task it was currently working on
  * what its open todos were
  * what facts/decisions it had already established

This module provides a small ``TaskMemory`` class that:

  * Persists to JSON on disk (``QWEN_TASK_MEMORY_PATH`` overrides the
    default ``./.agent/context/state.json``).
  * Tracks a single ``current_task`` description, a list of todos
    (each ``{id, status, description, created_at}``), and a bounded
    list of recent ``decisions`` (free-form strings, FIFO-capped).
  * Renders into a single ``[Task memory: ...]`` block suitable for
    prepending to a Qwen chat as a synthetic ``system`` message via
    :meth:`to_system_prompt`.

The QwenClient picks up an optional ``task_memory`` attribute and, when
present, prepends the rendered block to every outgoing chat request.

Design constraints:

  * **Size-bounded.** Old todos and decisions are evicted FIFO so the
    memory block can't itself overflow the context cap.
  * **Atomic writes.** Save via tempfile + ``os.replace`` so an
    interrupted process never leaves a half-written JSON.
  * **Best-effort.** Load failures (corrupt JSON, missing parent dir)
    return an empty memory rather than crashing the client.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable


_DEFAULT_PATH = ".agent/context/state.json"
_DEFAULT_MAX_TODOS = 32
_DEFAULT_MAX_DECISIONS = 16
_DEFAULT_MAX_FACTS = 32


@dataclass
class Todo:
    id: str
    description: str
    status: str = "open"  # "open", "in_progress", "done", "blocked"
    created_at: float = field(default_factory=lambda: time.time())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Todo:
        return cls(
            id=str(d.get("id", "")),
            description=str(d.get("description", "")),
            status=str(d.get("status", "open")),
            created_at=float(d.get("created_at", time.time())),
        )


def _coerce_int(env_name: str, default: int) -> int:
    raw = os.environ.get(env_name)
    if raw is None:
        return default
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


class TaskMemory:
    """Thread-safe, JSON-persisted task / todo / facts store.

    Not strictly necessary to be thread-safe (the client is sync), but
    cheap insurance against future async / TUI background workers.
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        max_todos: int | None = None,
        max_decisions: int | None = None,
        max_facts: int | None = None,
    ) -> None:
        self.path = Path(
            path
            or os.environ.get("QWEN_TASK_MEMORY_PATH")
            or _DEFAULT_PATH
        )
        self._lock = threading.Lock()
        self.max_todos = max_todos or _coerce_int(
            "QWEN_TASK_MEMORY_MAX_TODOS", _DEFAULT_MAX_TODOS
        )
        self.max_decisions = max_decisions or _coerce_int(
            "QWEN_TASK_MEMORY_MAX_DECISIONS", _DEFAULT_MAX_DECISIONS
        )
        self.max_facts = max_facts or _coerce_int(
            "QWEN_TASK_MEMORY_MAX_FACTS", _DEFAULT_MAX_FACTS
        )
        self.current_task: str = ""
        self.todos: list[Todo] = []
        self.facts: dict[str, str] = {}
        self.decisions: list[str] = []
        self.load()

    # -------------------------------------------------- Persistence
    def load(self) -> None:
        """Load state from disk; missing/corrupt files yield empty mem."""
        with self._lock:
            try:
                raw = self.path.read_text(encoding="utf-8")
                data = json.loads(raw)
            except (FileNotFoundError, OSError, json.JSONDecodeError):
                self.current_task = ""
                self.todos = []
                self.facts = {}
                self.decisions = []
                return
            if not isinstance(data, dict):
                self.current_task = ""
                self.todos = []
                self.facts = {}
                self.decisions = []
                return
            self.current_task = str(data.get("current_task", "") or "")
            self.todos = [
                Todo.from_dict(t)
                for t in (data.get("todos") or [])
                if isinstance(t, dict)
            ]
            facts = data.get("facts") or {}
            self.facts = {
                str(k): str(v) for k, v in (facts.items() if isinstance(facts, dict) else [])
            }
            self.decisions = [
                str(d) for d in (data.get("decisions") or []) if d is not None
            ]

    def save(self) -> None:
        """Atomically persist to disk."""
        with self._lock:
            self._save_locked()

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "current_task": self.current_task,
            "todos": [t.to_dict() for t in self.todos],
            "facts": dict(self.facts),
            "decisions": list(self.decisions),
        }
        # Atomic write: temp file + rename so an interrupted save can't
        # leave a half-written JSON the next load() trips over.
        fd, tmp = tempfile.mkstemp(
            prefix=".state-", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
        except Exception:
            # Best-effort cleanup of the temp file on failure.
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -------------------------------------------------- Mutations
    def set_current_task(self, description: str) -> None:
        with self._lock:
            self.current_task = (description or "").strip()
            self._save_locked()

    def add_todo(
        self, todo_id: str, description: str, *, status: str = "open"
    ) -> Todo:
        with self._lock:
            todo_id = (todo_id or "").strip()
            if not todo_id:
                raise ValueError("todo_id must be non-empty")
            # Update in place if id already exists.
            for t in self.todos:
                if t.id == todo_id:
                    t.description = description
                    t.status = status
                    self._save_locked()
                    return t
            t = Todo(id=todo_id, description=description, status=status)
            self.todos.append(t)
            # FIFO eviction of oldest *done* todos first, then any.
            self._evict_overflow_locked()
            self._save_locked()
            return t

    def update_todo_status(self, todo_id: str, status: str) -> bool:
        with self._lock:
            for t in self.todos:
                if t.id == todo_id:
                    t.status = status
                    self._save_locked()
                    return True
            return False

    def remove_todo(self, todo_id: str) -> bool:
        with self._lock:
            before = len(self.todos)
            self.todos = [t for t in self.todos if t.id != todo_id]
            if len(self.todos) != before:
                self._save_locked()
                return True
            return False

    def record_fact(self, key: str, value: str) -> None:
        with self._lock:
            key = (key or "").strip()
            if not key:
                raise ValueError("fact key must be non-empty")
            self.facts[key] = str(value)
            self._evict_overflow_locked()
            self._save_locked()

    def record_decision(self, text: str) -> None:
        with self._lock:
            text = (text or "").strip()
            if not text:
                return
            self.decisions.append(text)
            self._evict_overflow_locked()
            self._save_locked()

    def clear(self) -> None:
        with self._lock:
            self.current_task = ""
            self.todos = []
            self.facts = {}
            self.decisions = []
            self._save_locked()

    def _evict_overflow_locked(self) -> None:
        # Drop oldest done todos first; only then any oldest.
        if len(self.todos) > self.max_todos:
            done = [i for i, t in enumerate(self.todos) if t.status == "done"]
            for idx in done:
                if len(self.todos) <= self.max_todos:
                    break
                self.todos.pop(idx)
        while len(self.todos) > self.max_todos:
            self.todos.pop(0)
        while len(self.decisions) > self.max_decisions:
            self.decisions.pop(0)
        if len(self.facts) > self.max_facts:
            # dict insertion order preserves FIFO; drop oldest keys.
            keys = list(self.facts.keys())
            for k in keys[: len(keys) - self.max_facts]:
                self.facts.pop(k, None)

    # -------------------------------------------------- Rendering
    def is_empty(self) -> bool:
        with self._lock:
            return not (
                self.current_task or self.todos or self.facts or self.decisions
            )

    def to_system_prompt(self) -> str:
        """Render a compact ``[Task memory: ...]`` block. Returns empty
        string when the memory is empty so callers can do a truthy check
        before injecting."""
        with self._lock:
            if not (
                self.current_task or self.todos or self.facts or self.decisions
            ):
                return ""
            lines: list[str] = ["[Task memory:"]
            if self.current_task:
                lines.append(f"  current task: {self.current_task}")
            open_todos = [t for t in self.todos if t.status != "done"]
            done_todos = [t for t in self.todos if t.status == "done"]
            if open_todos:
                lines.append("  open todos:")
                for t in open_todos:
                    lines.append(
                        f"    - [{t.status}] {t.id}: {t.description}"
                    )
            if done_todos:
                # Compact done summary: count + last 3 ids.
                tail = ", ".join(t.id for t in done_todos[-3:])
                lines.append(
                    f"  done todos ({len(done_todos)} total, recent: {tail})"
                )
            if self.facts:
                lines.append("  facts:")
                for k, v in self.facts.items():
                    snippet = v if len(v) <= 200 else v[:200] + "…"
                    lines.append(f"    - {k}: {snippet}")
            if self.decisions:
                lines.append("  recent decisions:")
                for d in self.decisions[-5:]:
                    snippet = d if len(d) <= 200 else d[:200] + "…"
                    lines.append(f"    - {snippet}")
            lines.append(
                "Use this to keep continuity across turns; update todo "
                "status as you make progress.]"
            )
            return "\n".join(lines)

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-safe view of the current memory."""
        with self._lock:
            return {
                "current_task": self.current_task,
                "todos": [t.to_dict() for t in self.todos],
                "facts": dict(self.facts),
                "decisions": list(self.decisions),
            }


def _task_memory_enabled() -> bool:
    """Master kill-switch. Default *off* -- task memory is opt-in so we
    don't surprise existing deployments. Enable with
    ``QWEN_TASK_MEMORY=1``."""
    raw = os.environ.get("QWEN_TASK_MEMORY", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def load_default_task_memory() -> TaskMemory | None:
    """Construct the singleton TaskMemory from env when enabled.

    Returns ``None`` when the feature is disabled so callers can do a
    truthy check before attaching it to a QwenClient.
    """
    if not _task_memory_enabled():
        return None
    return TaskMemory()
