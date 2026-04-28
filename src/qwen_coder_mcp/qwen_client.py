"""OpenAI Chat Completions client tuned for Qwen3.6-27B backends.

Works against any endpoint that speaks the OpenAI /v1/chat/completions wire
format: vLLM, SGLang, Ollama (with the OpenAI shim), DashScope's compatible
mode, OpenRouter, Together, etc.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import httpx


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
        """
        budget = requested or self.settings.max_tokens
        cap = getattr(self.settings, "server_max_len", 0) or 0
        if cap <= 0:
            return max(1, budget)
        prompt_tokens = 0
        for m in messages:
            content = m.content if isinstance(m, ChatMessage) else m.get("content", "")
            if isinstance(content, str):
                # Same crude estimator as tui.estimate_tokens (~4 chars/token)
                prompt_tokens += max(1, len(content) // 4)
        # Reserve 64 tokens of headroom for chat template overhead so we
        # don't slam right against the server's wall.
        room = cap - prompt_tokens - 64
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
    ) -> str:
        """Send a chat completion request and return the assistant text."""
        payload: dict[str, Any] = {
            "model": self.settings.model,
            "messages": [
                m.to_dict() if isinstance(m, ChatMessage) else dict(m)
                for m in messages
            ],
            "temperature": temperature,
            "top_p": top_p,
            "max_tokens": self._resolve_max_tokens(messages, max_tokens),
            "stream": False,
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
        payload: dict[str, Any] = {
            "model": self.settings.model,
            "messages": [
                m.to_dict() if isinstance(m, ChatMessage) else dict(m)
                for m in messages
            ],
            "temperature": temperature,
            "top_p": top_p,
            "max_tokens": self._resolve_max_tokens(messages, max_tokens),
            "stream": True,
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
                        return
                    continue
                try:
                    obj = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                try:
                    delta = obj["choices"][0].get("delta") or {}
                except (KeyError, IndexError):
                    continue
                content = delta.get("content")
                if isinstance(content, str) and content:
                    yield content
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and "text" in block:
                            t = str(block["text"])
                            if t:
                                yield t

    @staticmethod
    def _extract_text(data: dict[str, Any]) -> str:
        try:
            choice = data["choices"][0]
        except (KeyError, IndexError) as exc:
            raise QwenError(f"malformed response: {data!r}") from exc
        msg = choice.get("message") or {}
        content = msg.get("content")
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
        # Strip Qwen3.6 chain-of-thought blocks before any downstream
        # parser (tool-call regex, JSON extractor, verdict matcher)
        # sees the text. See _strip_think_blocks for rationale.
        text = _strip_think_blocks(text)
        if not text:
            # Empty content is treated as a transient failure: in this
            # agent's domain every prompt expects substantive output
            # (diff / issue / verdict). Silent "" was misclassified
            # downstream as "no findings" / "no_verdict", dropping
            # iterations. Raising QwenError lets the retry loop kick in.
            raise QwenError(f"empty assistant content: {data!r}")
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
        )
