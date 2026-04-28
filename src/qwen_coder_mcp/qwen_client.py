"""OpenAI Chat Completions client tuned for Qwen3.6-27B backends.

Works against any endpoint that speaks the OpenAI /v1/chat/completions wire
format: vLLM, SGLang, Ollama (with the OpenAI shim), DashScope's compatible
mode, OpenRouter, Together, etc.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import httpx

_logger = logging.getLogger(__name__)

# Loop 236: marker appended when the upstream completion was truncated
# at max_tokens. Downstream parsers (tool-call regex, verdict matcher,
# TUI) can detect this string to either retry with a higher budget or
# surface "model ran out of tokens" feedback to the user instead of
# silently presenting a partial answer that looks like a premature stop.
TRUNCATION_MARKER = "[truncated: model hit max_tokens]"


def _default_repetition_penalty() -> float:
    """Loop 238: default repetition_penalty for every Qwen request.

    User reported the model "repeats itself and does nothing but that"
    after a while. Root cause: the codebase pinned ``temperature=0.2``
    everywhere but never set any repetition control. Qwen3-Next's
    own ``generation_config.json`` recommends ``temperature=1.0``,
    ``top_k=20``, ``top_p=0.95`` precisely because the model degenerates
    into n-gram loops at low temperature without a rep penalty. We keep
    the low temperature for code-generation determinism and add a mild
    ``repetition_penalty=1.05`` (vLLM extension; passed through OpenAI
    chat completions) to break loops without distorting code output.

    Override via ``QWEN_REPETITION_PENALTY`` env var. Set to ``1.0`` to
    disable. Sane range: ``1.0`` (off) -- ``1.2`` (aggressive).
    """
    raw = os.environ.get("QWEN_REPETITION_PENALTY", "1.05")
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 1.05
    if v <= 0:
        return 1.05
    return v


def _chars_per_token() -> float:
    """Loop 240: characters-per-token estimator ratio.

    vLLM rejected requests with 49153 actual prompt tokens when our old
    ``len // 4`` (==4 chars/token) heuristic estimated only ~37k. Code
    and markdown tokenize at roughly 3 chars/token on Qwen3-Next, so
    the old estimator under-counted by ~25% and the client-side clamp
    in ``_resolve_max_tokens`` failed to catch the overflow before
    sending. Default to a tighter 3.0 ratio and let operators override
    via ``QWEN_CHARS_PER_TOKEN`` if their workload tokenizes differently
    (e.g. natural-language English averages closer to 4).
    """
    raw = os.environ.get("QWEN_CHARS_PER_TOKEN", "3.0")
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 3.0
    if v <= 0:
        return 3.0
    return v


def _estimate_tokens(text: str) -> int:
    """Loop 240: char-based token estimate, conservative (rounds up)."""
    if not text:
        return 0
    cpt = _chars_per_token()
    return max(1, int(len(text) / cpt) + (1 if len(text) % cpt else 0))


def _context_reserve_tokens() -> int:
    """Loop 240: headroom kept free of prompt+completion for chat-template
    overhead and per-message tokenizer markers (system/user/assistant
    role tags, eot tokens). Default 256 is generous; ``QWEN_CONTEXT_RESERVE``
    overrides."""
    raw = os.environ.get("QWEN_CONTEXT_RESERVE", "256")
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return 256
    if v < 0:
        return 256
    return v


def _auto_compress_enabled() -> bool:
    """Loop 240: master kill-switch for history compression. Defaults on."""
    raw = os.environ.get("QWEN_AUTO_COMPRESS", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


_THINK_BLOCK_RE = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.DOTALL | re.IGNORECASE)
_DANGLING_OPEN_THINK_RE = re.compile(r".*?</think\s*>", re.DOTALL | re.IGNORECASE)


def _strip_think_blocks(text: str) -> str:
    """Strip ``<think>...</think>`` reasoning blocks from assistant content.

    Live testing of Qwen3.6-27B against vLLM 0.11 confirmed the model
    emits its chain-of-thought inline in ``message.content`` (no
    separate ``reasoning_content`` channel like DeepSeek-R1). When the
    agent loop's tool-call regex scans that raw content it can match
    speculative tool calls the model was reasoning *about*, not
    actually committing to. Strip the blocks before any downstream
    parser sees the text.

    Behaviour:
      * Removes complete ``<think>...</think>`` blocks (case-insensitive).
      * If a ``</think>`` appears with no opening tag (Qwen3.6 sometimes
        starts its reasoning unwrapped and only emits the closing tag),
        drop everything up to and including the first ``</think>``.
      * Returns the trimmed remainder.

    Disable by setting ``QWEN_DISABLE_THINK_STRIP=1`` for callers that
    want to inspect the raw chain-of-thought (e.g., debugging).
    """
    if os.environ.get("QWEN_DISABLE_THINK_STRIP", "").lower() in {"1", "true", "yes"}:
        return text
    if not text or "</think" not in text.lower():
        return text
    cleaned = _THINK_BLOCK_RE.sub("", text)
    if "</think" in cleaned.lower():
        # No matching open tag — strip everything up to the close.
        cleaned = _DANGLING_OPEN_THINK_RE.sub("", cleaned, count=1)
    return cleaned.strip()


class _StreamingThinkStripFilter:
    """Stateful chunk-level filter that drops ``<think>...</think>``
    blocks from a streamed assistant response.

    The non-streaming path strips think blocks in one pass via
    :func:`_strip_think_blocks`. Streaming consumers (TUI, SSE
    relays) can't apply that pass because tags may be split across
    delta chunks (chunk1 = ``"<thi"``, chunk2 = ``"nk>...content"``).

    Strategy: track an ``inside`` flag plus a small ``tail`` buffer
    that holds back any text that *might* be the start of a tag. On
    each :meth:`feed` call, return whatever is provably outside any
    think block; hold the rest until enough context arrives. Call
    :meth:`flush` at end-of-stream to release any tail not part of a
    tag.

    Limitations:
      * Only the wrapped case is handled. The unwrapped case (Qwen3.6
        sometimes emits ``</think>`` without a prior ``<think>``) is
        impossible to suppress in true streaming because earlier
        chunks are already user-visible. The non-streaming
        :func:`_strip_think_blocks` still handles it for the
        non-streaming path.
      * Disable wholesale via ``QWEN_DISABLE_THINK_STRIP=1``.
    """

    # Longest tag we might see partially: ``</think >`` = 9 chars.
    _MAX_TAG_TAIL = 9
    _OPEN_RE = re.compile(r"<think\b[^>]*>", re.IGNORECASE)
    _CLOSE_RE = re.compile(r"</think\s*>", re.IGNORECASE)

    def __init__(self) -> None:
        self.inside = False
        self.tail = ""
        self.disabled = os.environ.get(
            "QWEN_DISABLE_THINK_STRIP", ""
        ).lower() in {"1", "true", "yes"}

    def feed(self, chunk: str) -> str:
        if self.disabled or not chunk:
            return chunk
        buf = self.tail + chunk
        self.tail = ""
        out: list[str] = []
        while buf:
            if self.inside:
                m = self._CLOSE_RE.search(buf)
                if m is None:
                    # Hold up to the longest possible partial-close
                    # suffix in case the close tag straddles chunks.
                    if len(buf) > self._MAX_TAG_TAIL:
                        buf = buf[-self._MAX_TAG_TAIL :]
                    self.tail = buf
                    buf = ""
                    break
                # Drop everything up to and including the close tag.
                self.inside = False
                buf = buf[m.end() :]
                continue
            # Outside a think block: search for the next open tag.
            m = self._OPEN_RE.search(buf)
            if m is None:
                # No complete open tag. Hold back any trailing run
                # that *might* be the start of one (`<th`, `<thin`, ...).
                lt = buf.rfind("<")
                if lt != -1 and len(buf) - lt < 8:
                    out.append(buf[:lt])
                    self.tail = buf[lt:]
                else:
                    out.append(buf)
                buf = ""
                break
            # Emit the prefix before the open tag, then enter the block.
            out.append(buf[: m.start()])
            self.inside = True
            buf = buf[m.end() :]
        return "".join(out)

    def flush(self) -> str:
        """Release any held-back tail at end of stream.

        If we're still inside a think block when the stream ends
        (model truncated mid-thought), drop the tail entirely. If the
        tail contains no open tag, it's safe to emit verbatim.
        """
        if self.disabled:
            tail, self.tail = self.tail, ""
            return tail
        if self.inside:
            self.tail = ""
            self.inside = False
            return ""
        # Outside a block, the tail can only ever be a partial-tag
        # prefix that turned out not to be a tag. Safe to release.
        tail, self.tail = self.tail, ""
        return tail

from .config import Settings, load_settings


@dataclass
class ChatMessage:
    role: str  # "system" | "user" | "assistant"
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


class QwenError(RuntimeError):
    """Raised when the Qwen backend returns an unrecoverable error."""


class QwenFatalError(QwenError):
    """Non-retriable error (4xx other than 408/429, malformed payload).

    Distinguished from ``QwenError`` so the retry loop can fail fast
    instead of wasting attempts on requests that will never succeed.
    """


_RETRIABLE_4XX = frozenset({408, 425, 429})


def _chat_total_budget_seconds() -> float:
    """Wall-clock ceiling for one `chat()` call across all retries.
    Per-request httpx timeout protects individual attempts; this cap
    bounds the *aggregate* time including backoff sleeps so a flaky
    backend can't exhaust the per-iteration budget on a single call.

    Clamped to (0, 1h]. Bad / non-positive input falls back to default;
    absurdly large values are capped so a typo cannot disable the cap.
    """
    import os as _os
    raw = _os.environ.get("QWEN_CHAT_BUDGET_S", "300")
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 300.0
    if v <= 0:
        return 300.0
    if v > 3600.0:
        return 3600.0
    return v


class QwenClient:
    """Thin OpenAI-compatible client with retries and sane defaults."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or load_settings()
        self._client = httpx.Client(
            base_url=self.settings.base_url,
            timeout=self.settings.timeout,
            headers={
                "Authorization": f"Bearer {self.settings.api_key}",
                "Content-Type": "application/json",
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "QwenClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @staticmethod
    def _msg_role(m: Any) -> str:
        if isinstance(m, ChatMessage):
            return m.role
        if isinstance(m, dict):
            return m.get("role", "")
        return ""

    @staticmethod
    def _msg_content(m: Any) -> str:
        if isinstance(m, ChatMessage):
            return m.content or ""
        if isinstance(m, dict):
            c = m.get("content", "")
            return c if isinstance(c, str) else ""
        return ""

    def _prompt_tokens(self, messages: Sequence[Any]) -> int:
        return sum(_estimate_tokens(self._msg_content(m)) for m in messages)

    def _compress_messages_to_fit(
        self,
        messages: Sequence[ChatMessage | dict[str, str]],
        requested_max_tokens: int | None,
    ) -> tuple[list[Any], int]:
        """Loop 240: drop oldest non-protected messages so prompt + completion
        + reserve fits under the server's context cap.

        Symptom that drove this: vLLM 400'd with ``This model's maximum
        context length is 65536 tokens. However, you requested 16384
        output tokens and your prompt contains at least 49153 input
        tokens``. The pre-loop-240 ``_resolve_max_tokens`` only clamped
        the ``max_tokens`` budget; it never dropped messages, so a long
        agent history could push the prompt itself past the server cap
        with no recovery path.

        Compression rules:
          * Always preserve every ``system`` message (role-prompt /
            persona).
          * Always preserve the LAST ``user`` message (the actual query).
          * Drop oldest non-protected messages first (FIFO), in pairs
            where possible (assistant reply orphans look weird in the
            chat template).
          * After dropping, clamp ``max_tokens`` to whatever room is
            still left so the request strictly fits.

        Returns ``(messages, max_tokens)``. The messages list is a fresh
        copy (caller's list is never mutated). When the cap is unknown
        (``server_max_len <= 0``) compression is a no-op and only the
        ``max_tokens`` value is returned alongside the original list.
        Disable entirely with ``QWEN_AUTO_COMPRESS=0``.
        """
        target = requested_max_tokens or self.settings.max_tokens
        cap = getattr(self.settings, "server_max_len", 0) or 0
        msgs: list[Any] = list(messages)

        if cap <= 0 or not _auto_compress_enabled():
            # Best-effort: still clamp to target if cap unknown, mirror
            # legacy _resolve_max_tokens behaviour for callers that
            # opt out of compression.
            return msgs, max(1, target if cap <= 0 else self._resolve_max_tokens(msgs, target))

        reserve = _context_reserve_tokens()

        def _protected_indices(ms: list[Any]) -> set[int]:
            keep = {i for i, m in enumerate(ms) if self._msg_role(m) == "system"}
            for i in range(len(ms) - 1, -1, -1):
                if self._msg_role(ms[i]) == "user":
                    keep.add(i)
                    break
            return keep

        dropped = 0
        # Loop until the request fits OR we have no more droppable msgs.
        while True:
            prompt_t = self._prompt_tokens(msgs)
            # We want: prompt + target + reserve <= cap.
            if prompt_t + target + reserve <= cap:
                break
            keep = _protected_indices(msgs)
            droppable = [i for i in range(len(msgs)) if i not in keep]
            if not droppable:
                break
            msgs.pop(droppable[0])
            dropped += 1

        if dropped > 0:
            _logger.warning(
                "context compression: dropped %d oldest message(s) to "
                "fit %d-token cap (target_completion=%d, reserve=%d)",
                dropped, cap, target, reserve,
            )

        # Final clamp: if we still don't fit (only system + last user
        # remain and they're already huge), clamp max_tokens down.
        prompt_t = self._prompt_tokens(msgs)
        room = cap - prompt_t - reserve
        if room <= 0:
            _logger.warning(
                "context compression: prompt (%d est. tokens) + reserve "
                "(%d) already exceeds cap (%d); sending max_tokens=1 "
                "and letting server reject", prompt_t, reserve, cap,
            )
            return msgs, 1
        return msgs, max(1, min(target, room))

    def _resolve_max_tokens(
        self,
        messages: Sequence[ChatMessage | dict[str, str]],
        requested: int | None,
    ) -> int:
        """Clamp the requested completion budget against the server cap.

        vLLM raises ``VLLMValidationError`` when ``max_tokens`` exceeds
        the server's ``--max-model-len`` (it counts prompt + completion
        against the same budget). We mirror that constraint client-side
        so the user gets a small completion instead of a 400 from the
        upstream server when their config and the serve script drift
        out of sync (e.g. ``QWEN_MAX_TOKENS=4096`` on the client but
        ``QWEN_SERVE_MAX_LEN=2048`` on the server).

        Returns at least 1, even on tiny budgets, so the request still
        goes through and the server can produce a one-token error.

        Loop 240: now uses ``_estimate_tokens`` (3 chars/token by default,
        vs. the old looser 4 chars/token) and a ``QWEN_CONTEXT_RESERVE``
        knob (default 256, was hardcoded 64) so chat-template overhead
        doesn't push us off the wall. For full history compression call
        :meth:`_compress_messages_to_fit` instead.
        """
        budget = requested or self.settings.max_tokens
        cap = getattr(self.settings, "server_max_len", 0) or 0
        if cap <= 0:
            return max(1, budget)
        prompt_tokens = self._prompt_tokens(messages)
        room = cap - prompt_tokens - _context_reserve_tokens()
        if room <= 0:
            return 1
        return max(1, min(budget, room))

    def health_check(self, timeout: float = 2.0) -> dict[str, Any]:
        """Probe the backend with a short GET /models call.

        Returns a dict of shape:
          {"ok": True,  "status": int, "models": [str, ...]}
          {"ok": False, "error": str, "hint": str | None}

        Used by the TUI on startup to give an actionable banner instead
        of letting the first chat fail with a raw httpx ConnectError.
        Never raises -- callers may safely use this in a UI thread.
        """
        try:
            resp = self._client.get("/models", timeout=timeout)
        except httpx.ConnectError as exc:
            return {
                "ok": False,
                "error": f"connection refused at {self.settings.base_url}: {exc}",
                "hint": (
                    "is the qwen server running on this host/port? "
                    "start it with scripts/serve_qwen.sh, then verify with "
                    f"curl -fsS {self.settings.base_url}/models"
                ),
            }
        except httpx.TimeoutException as exc:
            return {
                "ok": False,
                "error": f"connection timed out at {self.settings.base_url}: {exc}",
                "hint": "backend is reachable but slow to respond; still warming up?",
            }
        except httpx.HTTPError as exc:
            return {
                "ok": False,
                "error": f"http error talking to {self.settings.base_url}: {type(exc).__name__}: {exc}",
                "hint": None,
            }
        if resp.status_code >= 400:
            return {
                "ok": False,
                "error": f"backend returned {resp.status_code}: {resp.text[:200]}",
                "hint": (
                    "check the api key matches QWEN_SERVE_API_KEY"
                    if resp.status_code in (401, 403)
                    else None
                ),
            }
        models: list[str] = []
        try:
            data = resp.json()
            for entry in data.get("data") or []:
                mid = entry.get("id")
                if isinstance(mid, str):
                    models.append(mid)
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass
        return {"ok": True, "status": resp.status_code, "models": models}

    def vllm_health_probe(self, timeout: float = 2.0) -> dict[str, Any]:
        """Probe vLLM's ``/health`` endpoint (sibling of ``/v1``).

        vLLM exposes a dedicated readiness endpoint at the *server root*,
        not under ``/v1``: e.g. ``http://host:8000/health``. It returns
        200 (often with empty body) once the engine has finished loading
        weights and is ready to serve requests. A 503 means the engine
        is still warming up.

        ``health_check`` only proves ``/v1/models`` answers — but vLLM
        will happily 200 ``/v1/models`` while the engine is mid-restart
        and chat requests are queueing forever. ``vllm_health_probe``
        is the *active* readiness signal that catches the gap loops 205
        and 211 left in production: arg-level OK + engine-level not-yet.

        Returns the same shape as ``health_check``:
          {"ok": True,  "status": int}
          {"ok": False, "error": str, "hint": str | None}

        Never raises. Designed so the TUI can surface "engine still
        warming up" as actionable text instead of letting the next chat
        hang for 60 seconds.
        """
        # Reconstruct the root URL: strip a trailing /v1 (or /v1/) from
        # base_url. We do not assume the same httpx.Client because
        # base_url is a constructor concern; build a one-shot GET.
        base = str(self.settings.base_url).rstrip("/")
        if base.endswith("/v1"):
            root = base[: -len("/v1")]
        else:
            root = base
        health_url = f"{root}/health"
        try:
            resp = httpx.get(
                health_url,
                timeout=timeout,
                headers={"Authorization": f"Bearer {self.settings.api_key}"},
            )
        except httpx.ConnectError as exc:
            return {
                "ok": False,
                "error": f"connection refused at {health_url}: {exc}",
                "hint": (
                    "vLLM is not listening on the expected host/port; "
                    "start it with scripts/serve_qwen.sh and watch "
                    ".loop/serve.log for 'application startup complete'"
                ),
            }
        except httpx.TimeoutException as exc:
            return {
                "ok": False,
                "error": f"health probe timed out at {health_url}: {exc}",
                "hint": (
                    "engine is alive but not ready; this is normal during "
                    "model load (can take 30-90s for 27B at fp8)"
                ),
            }
        except httpx.HTTPError as exc:
            return {
                "ok": False,
                "error": f"http error probing {health_url}: {type(exc).__name__}: {exc}",
                "hint": None,
            }
        if resp.status_code == 200:
            return {"ok": True, "status": 200}
        if resp.status_code == 503:
            return {
                "ok": False,
                "error": f"engine not ready (503) at {health_url}",
                "hint": (
                    "vLLM is up but the engine is still initialising. "
                    "Tail .loop/serve.log; if it stalls beyond 2 minutes "
                    "the model probably failed to load (OOM, missing "
                    "weights, or a flag-pairing bug like loop 211)"
                ),
            }
        return {
            "ok": False,
            "error": f"health probe returned {resp.status_code}: {resp.text[:200]}",
            "hint": None,
        }

    def chat(
        self,
        messages: Sequence[ChatMessage | dict[str, str]],
        *,
        temperature: float = 0.2,
        top_p: float = 0.95,
        max_tokens: int | None = None,
        stop: Iterable[str] | None = None,
        extra: dict[str, Any] | None = None,
        max_retries: int = 3,
        repetition_penalty: float | None = None,
    ) -> str:
        """Send a chat completion request and return the assistant text."""
        # Loop 240: drop oldest non-protected messages so prompt + completion
        # fits inside the server's context cap. No-op if QWEN_AUTO_COMPRESS=0.
        compressed_msgs, resolved_max = self._compress_messages_to_fit(
            messages, max_tokens
        )
        payload: dict[str, Any] = {
            "model": self.settings.model,
            "messages": [
                m.to_dict() if isinstance(m, ChatMessage) else dict(m)
                for m in compressed_msgs
            ],
            "temperature": temperature,
            "top_p": top_p,
            "max_tokens": resolved_max,
            "stream": False,
            # Loop 238: prevent Qwen3-Next n-gram loops at low temperature.
            "repetition_penalty": (
                repetition_penalty
                if repetition_penalty is not None
                else _default_repetition_penalty()
            ),
        }
        if stop:
            payload["stop"] = list(stop)
        if extra:
            # Reserved keys are managed by this method directly. Letting
            # callers overwrite them via `extra` would silently change
            # the request model, prompt, or response shape and break
            # downstream parsing (`_extract_text` only handles
            # `stream=False` payloads).
            reserved = {"model", "messages", "stream"}
            conflicts = reserved.intersection(extra.keys())
            if conflicts:
                raise QwenFatalError(
                    "extra cannot override reserved keys: "
                    f"{sorted(conflicts)}"
                )
            payload.update(extra)

        last_err: Exception | None = None
        chat_deadline = time.monotonic() + _chat_total_budget_seconds()
        for attempt in range(max_retries):
            if time.monotonic() > chat_deadline:
                raise QwenError(
                    f"chat budget exceeded after {attempt} attempts: {last_err}"
                )
            try:
                resp = self._client.post("/chat/completions", json=payload)
                if resp.status_code >= 500:
                    raise QwenError(
                        f"backend {resp.status_code}: {resp.text[:300]}"
                    )
                if resp.status_code >= 400:
                    if resp.status_code in _RETRIABLE_4XX:
                        raise QwenError(
                            f"transient {resp.status_code}: {resp.text[:300]}"
                        )
                    raise QwenFatalError(
                        f"client error {resp.status_code}: {resp.text[:300]}"
                    )
                data = resp.json()
                return self._extract_text(data)
            except QwenFatalError:
                # Not retriable — fail fast.
                raise
            except (httpx.HTTPError, QwenError, json.JSONDecodeError) as exc:
                last_err = exc
                sleep = min(2**attempt, 10)
                # Don't sleep past the deadline.
                remaining = chat_deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(sleep, remaining))
        raise QwenError(f"chat failed after {max_retries} attempts: {last_err}")

    def chat_stream(
        self,
        messages: Sequence[ChatMessage | dict[str, str]],
        *,
        temperature: float = 0.2,
        top_p: float = 0.95,
        max_tokens: int | None = None,
        stop: Iterable[str] | None = None,
        extra: dict[str, Any] | None = None,
        repetition_penalty: float | None = None,
    ) -> Iterable[str]:
        """Stream a chat completion. Yields incremental token strings.

        Uses the OpenAI-compatible `stream=true` SSE protocol. Lines
        of the form `data: {...}` are parsed; `data: [DONE]` ends the
        stream. Empty lines and other server-sent fields are ignored.
        Malformed chunks are skipped (best-effort streaming).

        No retries: streaming is interactive and a partial result is
        more useful than blocking for a retry. Callers wanting
        retries should fall back to `chat()`.
        """
        # Loop 240: same compression as chat() — drop oldest non-protected
        # messages so prompt + completion fits the server's context cap.
        compressed_msgs, resolved_max = self._compress_messages_to_fit(
            messages, max_tokens
        )
        payload: dict[str, Any] = {
            "model": self.settings.model,
            "messages": [
                m.to_dict() if isinstance(m, ChatMessage) else dict(m)
                for m in compressed_msgs
            ],
            "temperature": temperature,
            "top_p": top_p,
            "max_tokens": resolved_max,
            "stream": True,
            # Loop 238: prevent Qwen3-Next n-gram loops at low temperature.
            "repetition_penalty": (
                repetition_penalty
                if repetition_penalty is not None
                else _default_repetition_penalty()
            ),
        }
        if stop:
            payload["stop"] = list(stop)
        if extra:
            reserved = {"model", "messages", "stream"}
            conflicts = reserved.intersection(extra.keys())
            if conflicts:
                raise QwenFatalError(
                    "extra cannot override reserved keys: "
                    f"{sorted(conflicts)}"
                )
            payload.update(extra)

        with self._client.stream(
            "POST", "/chat/completions", json=payload
        ) as resp:
            if resp.status_code >= 400:
                body = resp.read().decode("utf-8", errors="replace")[:300]
                if resp.status_code >= 500 or resp.status_code in _RETRIABLE_4XX:
                    raise QwenError(f"stream {resp.status_code}: {body}")
                raise QwenFatalError(f"stream client error {resp.status_code}: {body}")
            think_filter = _StreamingThinkStripFilter()
            stream_finish_reason: str | None = None
            for line in resp.iter_lines():
                if not line:
                    continue
                if isinstance(line, bytes):
                    line = line.decode("utf-8", errors="replace")
                if not line.startswith("data:"):
                    continue
                data_str = line[len("data:"):].strip()
                if not data_str or data_str == "[DONE]":
                    if data_str == "[DONE]":
                        # Flush any held-back tail before ending the
                        # stream so non-tag suffix tokens still reach
                        # the consumer.
                        tail = think_filter.flush()
                        if tail:
                            yield tail
                        # Loop 237: streaming-path parity with loop 236.
                        # Surface max_tokens truncation by emitting the
                        # marker after the tail flush so consumers see
                        # the same signal the non-stream path produces.
                        if stream_finish_reason == "length":
                            yield f"\n\n{TRUNCATION_MARKER}"
                        return
                    continue
                try:
                    obj = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                try:
                    choice0 = obj["choices"][0]
                except (KeyError, IndexError):
                    continue
                # Latch the finish_reason from any chunk that carries
                # one (vLLM emits it on the final delta). null/missing
                # values don't overwrite a previously-seen reason.
                fr = choice0.get("finish_reason")
                if isinstance(fr, str) and fr:
                    stream_finish_reason = fr
                delta = choice0.get("delta") or {}
                content = delta.get("content")
                if isinstance(content, str) and content:
                    cleaned = think_filter.feed(content)
                    if cleaned:
                        yield cleaned
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and "text" in block:
                            t = str(block["text"])
                            if t:
                                cleaned = think_filter.feed(t)
                                if cleaned:
                                    yield cleaned
            # Stream ended without an explicit [DONE] sentinel; still
            # flush so a held tail isn't silently dropped.
            tail = think_filter.flush()
            if tail:
                yield tail
            if stream_finish_reason == "length":
                yield f"\n\n{TRUNCATION_MARKER}"

    @staticmethod
    def _extract_text(data: dict[str, Any]) -> str:
        try:
            choice = data["choices"][0]
        except (KeyError, IndexError) as exc:
            raise QwenError(f"malformed response: {data!r}") from exc
        msg = choice.get("message") or {}
        content = msg.get("content")
        finish_reason = choice.get("finish_reason")
        if isinstance(content, list):  # some backends return content blocks
            parts = []
            for block in content:
                if isinstance(block, dict) and "text" in block:
                    parts.append(str(block["text"]))
                elif isinstance(block, str):
                    parts.append(block)
            text = "".join(parts).strip()
        elif isinstance(content, str):
            text = content.strip()
        elif content is None:
            text = ""
        else:
            text = str(content).strip()
        truncated = finish_reason == "length"
        # Loop 236: When the model hits max_tokens mid-think (Qwen3-Next
        # emits long <think>...</think> reasoning blocks), the closing
        # </think> may never arrive. _strip_think_blocks would then
        # return the raw text-with-open-tag and the user sees what looks
        # like a premature stop. Detect that case and salvage whatever
        # text the model managed to emit AFTER the strip, plus a marker.
        raw_text = text
        # Strip Qwen3.6 chain-of-thought blocks before any downstream
        # parser (tool-call regex, JSON extractor, verdict matcher)
        # sees the text. See _strip_think_blocks for rationale.
        text = _strip_think_blocks(text)
        if truncated and not text and raw_text:
            # All output got eaten because the think block never closed.
            # Surface the truncation rather than raising QwenError (which
            # would trigger a retry that hits the same budget).
            _logger.warning(
                "qwen completion truncated at max_tokens with unclosed "
                "<think>; returning marker. raw_len=%d", len(raw_text)
            )
            return TRUNCATION_MARKER
        if not text:
            # Empty content is treated as a transient failure: in this
            # agent's domain every prompt expects substantive output
            # (diff / issue / verdict). Silent "" was misclassified
            # downstream as "no findings" / "no_verdict", dropping
            # iterations. Raising QwenError lets the retry loop kick in.
            raise QwenError(f"empty assistant content: {data!r}")
        if truncated:
            _logger.warning(
                "qwen completion truncated at max_tokens (finish_reason=length); "
                "out_len=%d. Consider raising QWEN_MAX_TOKENS.", len(text)
            )
            # Idempotent: don't double-append on repeated extraction.
            if TRUNCATION_MARKER not in text:
                text = f"{text}\n\n{TRUNCATION_MARKER}"
        return text

    def system_user(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        top_p: float = 0.95,
        stop: Iterable[str] | None = None,
        extra: dict[str, Any] | None = None,
        max_retries: int = 3,
        repetition_penalty: float | None = None,
    ) -> str:
        """One-shot system+user prompt, returns text.

        All sampling kwargs forward to :meth:`chat` so callers retain
        full control over generation; defaults match :meth:`chat`.
        """
        return self.chat(
            [ChatMessage("system", system), ChatMessage("user", user)],
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            stop=stop,
            extra=extra,
            max_retries=max_retries,
            repetition_penalty=repetition_penalty,
        )
