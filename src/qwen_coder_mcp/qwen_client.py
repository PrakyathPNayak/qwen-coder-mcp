"""OpenAI Chat Completions client tuned for Qwen3.6-27B backends.

Works against any endpoint that speaks the OpenAI /v1/chat/completions wire
format: vLLM, SGLang, Ollama (with the OpenAI shim), DashScope's compatible
mode, OpenRouter, Together, etc.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import httpx

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
            "max_tokens": max_tokens or self.settings.max_tokens,
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
            "max_tokens": max_tokens or self.settings.max_tokens,
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
