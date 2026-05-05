"""Perplexity API client + MCP tool helpers.

Adds four web-grounded tools backed by the Perplexity API Platform:

* ``perplexity_search``    -- POST /search       (ranked web results)
* ``perplexity_ask``       -- POST /chat/completions, model ``sonar-pro``
* ``perplexity_research``  -- POST /chat/completions, model ``sonar-deep-research``
                              (SSE streaming, slow / 30s+)
* ``perplexity_reason``    -- POST /chat/completions, model ``sonar-reasoning-pro``

Public REST surface and tool semantics follow the official open-source
``@perplexity-ai/mcp-server`` (MIT) and the ``perplexity`` Python SDK
(Apache-2.0).  This is an independent Python implementation: no source
code is copied, only the documented HTTP contract is reused.

Configuration mirrors the reference server so existing operator habits
carry over:

| env var                 | default                     |
| ----------------------- | --------------------------- |
| ``PERPLEXITY_API_KEY``  | required                    |
| ``PERPLEXITY_BASE_URL`` | ``https://api.perplexity.ai``|
| ``PERPLEXITY_TIMEOUT_MS`` | 300000  (5 min)           |
| ``PERPLEXITY_PROXY``    | unset; falls back to ``HTTPS_PROXY`` / ``HTTP_PROXY`` |

The functions are pure (no module-level state) and accept an optional
injected ``httpx.Client`` so unit tests use ``httpx.MockTransport``
instead of hitting the real API -- mirroring ``web_tools.py``.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Iterable

import httpx

DEFAULT_BASE_URL = "https://api.perplexity.ai"
DEFAULT_TIMEOUT_SECONDS = 300.0
USER_AGENT = "qwen-coder-mcp-perplexity/0.1"

ASK_MODEL = "sonar-pro"
RESEARCH_MODEL = "sonar-deep-research"
REASON_MODEL = "sonar-reasoning-pro"

VALID_RECENCY = ("hour", "day", "week", "month", "year")
VALID_CONTEXT_SIZE = ("low", "medium", "high")
VALID_REASONING_EFFORT = ("minimal", "low", "medium", "high")
VALID_ROLES = ("system", "user", "assistant")

# Same regex shape used elsewhere in this codebase to strip Qwen-style
# <think>...</think> reasoning blocks. Kept local so this module has no
# coupling to qwen_client.
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


class PerplexityError(Exception):
    """Raised on any Perplexity-tool error -- caller surfaces as text."""


def _strip_think(text: str) -> str:
    return _THINK_RE.sub("", text or "").strip()


def _resolve_proxy() -> str | None:
    """Look up the proxy URL using the same precedence as the reference
    Perplexity MCP server: ``PERPLEXITY_PROXY`` first, then
    ``HTTPS_PROXY`` / ``https_proxy``, then ``HTTP_PROXY`` / ``http_proxy``.
    Returns ``None`` if nothing is set."""
    for var in (
        "PERPLEXITY_PROXY",
        "HTTPS_PROXY",
        "https_proxy",
        "HTTP_PROXY",
        "http_proxy",
    ):
        val = os.environ.get(var)
        if val:
            return val
    return None


def _resolve_timeout() -> float:
    """Read ``PERPLEXITY_TIMEOUT_MS`` fresh on every call so an operator
    can tweak it between requests without rebuilding the client."""
    raw = os.environ.get("PERPLEXITY_TIMEOUT_MS")
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        ms = int(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    if ms <= 0:
        return DEFAULT_TIMEOUT_SECONDS
    return ms / 1000.0


def _resolve_base_url() -> str:
    base = (os.environ.get("PERPLEXITY_BASE_URL") or DEFAULT_BASE_URL).strip()
    return base.rstrip("/") or DEFAULT_BASE_URL


def _resolve_api_key() -> str:
    key = (os.environ.get("PERPLEXITY_API_KEY") or "").strip()
    if not key:
        raise PerplexityError("PERPLEXITY_API_KEY environment variable is required")
    return key


def _build_client(timeout: float | None = None) -> httpx.Client:
    """Construct an ``httpx.Client`` with the operator's proxy + timeout
    settings applied. Caller owns the client lifetime."""
    t = timeout if timeout is not None else _resolve_timeout()
    proxy = _resolve_proxy()
    headers = {"User-Agent": USER_AGENT}
    kwargs: dict[str, Any] = {"headers": headers, "timeout": t, "follow_redirects": True}
    if proxy:
        # httpx accepts the same URL format the reference server documents:
        # ``https://[user:pass@]host:port``.
        kwargs["proxy"] = proxy
    return httpx.Client(**kwargs)


def validate_messages(messages: Any, *, tool_name: str = "perplexity") -> list[dict[str, str]]:
    """Validate a chat ``messages`` argument and normalise to plain dicts.

    Raises :class:`PerplexityError` if the structure is malformed -- the
    server layer catches that and surfaces a readable error to the MCP
    client. Mirrors the reference server's ``validateMessages`` contract
    (role + content both required, content must be a string)."""
    if not isinstance(messages, list) or not messages:
        raise PerplexityError(
            f"{tool_name}: 'messages' must be a non-empty array"
        )
    out: list[dict[str, str]] = []
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            raise PerplexityError(
                f"{tool_name}: message[{i}] must be an object"
            )
        role = msg.get("role")
        content = msg.get("content")
        if not isinstance(role, str) or role not in VALID_ROLES:
            raise PerplexityError(
                f"{tool_name}: message[{i}].role must be one of {VALID_ROLES}"
            )
        if not isinstance(content, str):
            raise PerplexityError(
                f"{tool_name}: message[{i}].content must be a string"
            )
        out.append({"role": role, "content": content})
    return out


def _post_json(
    endpoint: str,
    body: dict[str, Any],
    *,
    client: httpx.Client | None = None,
    stream: bool = False,
) -> httpx.Response:
    """POST ``body`` as JSON to ``<base>/<endpoint>`` with the
    Authorization header set. Caller is responsible for closing the
    response if ``stream=True``.

    ``stream=False`` (the default) reads the full body, raises on a
    non-2xx status with the response text included for debuggability,
    and returns the response object."""
    api_key = _resolve_api_key()
    base = _resolve_base_url()
    url = f"{base}/{endpoint.lstrip('/')}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
        "X-Source": "qwen-coder-mcp",
    }
    owns = client is None
    c = client or _build_client()
    try:
        if stream:
            # Caller will iterate the SSE stream; returning the request
            # object lets ``client.stream`` be used as a context manager
            # by the caller. We can't stream cleanly without that, so
            # this branch instead reads the full text (Perplexity's SSE
            # streams are bounded and small enough to buffer).
            resp = c.post(url, json=body, headers=headers)
        else:
            resp = c.post(url, json=body, headers=headers)
    except httpx.TimeoutException as exc:
        raise PerplexityError(
            f"Perplexity API timeout after {c.timeout.connect or '?'}s "
            f"(set PERPLEXITY_TIMEOUT_MS to extend)"
        ) from exc
    except httpx.HTTPError as exc:
        raise PerplexityError(f"Perplexity network error: {exc}") from exc
    finally:
        if owns and not stream:
            # Close the borrowed client now that we have the parsed
            # response in memory.
            c.close()
    if resp.status_code >= 400:
        body_text = resp.text or ""
        # Cap at a reasonable size so we don't dump a 5 MB HTML error
        # page into a TextContent reply.
        snippet = body_text[:1000]
        raise PerplexityError(
            f"Perplexity API error: {resp.status_code} {resp.reason_phrase}\n{snippet}"
        )
    if owns and stream:
        # If we own the client and streamed, close it now that we've
        # finished reading. Buffered streaming -- see comment above.
        c.close()
    return resp


# ----------------------------------------------------------------- /search

@dataclass(frozen=True)
class PerplexitySearchResult:
    title: str
    url: str
    snippet: str = ""
    date: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "date": self.date,
        }


def _parse_search_response(payload: Any) -> list[PerplexitySearchResult]:
    if not isinstance(payload, dict):
        return []
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        return []
    out: list[PerplexitySearchResult] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        title = str(item.get("title") or "").strip()
        if not url or not title:
            continue
        snippet = str(item.get("snippet") or "").strip()
        date = str(item.get("date") or "").strip()
        out.append(PerplexitySearchResult(title=title, url=url, snippet=snippet, date=date))
    return out


def perplexity_search(
    query: str,
    *,
    max_results: int = 10,
    max_tokens_per_page: int = 1024,
    country: str | None = None,
    client: httpx.Client | None = None,
) -> list[PerplexitySearchResult]:
    """Run a Perplexity ``/search`` request and return parsed results.

    ``max_results`` is clamped to ``1..20`` and ``max_tokens_per_page``
    to ``256..2048`` to match the documented bounds of the reference
    server. ``country`` is an ISO-3166-1 alpha-2 code (e.g. ``"US"``).
    """
    q = (query or "").strip()
    if not q:
        raise PerplexityError("perplexity_search: query must be non-empty")
    n = max(1, min(20, int(max_results)))
    tpp = max(256, min(2048, int(max_tokens_per_page)))
    body: dict[str, Any] = {
        "query": q,
        "max_results": n,
        "max_tokens_per_page": tpp,
    }
    if country:
        c = str(country).strip()
        if c:
            body["country"] = c
    resp = _post_json("search", body, client=client)
    try:
        data = resp.json()
    except ValueError as exc:
        raise PerplexityError(
            f"perplexity_search: invalid JSON response: {exc}"
        ) from exc
    return _parse_search_response(data)


def format_search_results(results: Iterable[PerplexitySearchResult]) -> str:
    """Render search results as a numbered text block. Compact, matches
    the style used by ``web_tools.format_search_results`` so the two are
    interchangeable in TUI rendering."""
    items = list(results)
    if not items:
        return "(no results)"
    lines: list[str] = [f"Found {len(items)} search results:", ""]
    for i, r in enumerate(items, 1):
        lines.append(f"{i}. {r.title}")
        lines.append(f"   {r.url}")
        if r.snippet:
            lines.append(f"   {r.snippet}")
        if r.date:
            lines.append(f"   ({r.date})")
    return "\n".join(lines)


# ----------------------------------------------------- /chat/completions

def _consume_sse_stream(text: str) -> dict[str, Any]:
    """Reassemble a full chat-completions response from a buffered SSE
    body. The reference server uses a streaming reader; here we POST and
    read the whole body (typically <1 MB) and then walk the ``data:``
    lines. Returns a synthesised dict shaped like the non-streaming
    response so the caller's parsing path is uniform.
    """
    import json

    content_parts: list[str] = []
    citations: list[Any] | None = None
    usage: dict[str, Any] | None = None
    resp_id: str | None = None
    model: str | None = None
    created: int | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if not data or data == "[DONE]":
            continue
        try:
            parsed = json.loads(data)
        except (ValueError, TypeError):
            continue  # tolerate keep-alives / malformed pings
        if not isinstance(parsed, dict):
            continue
        if isinstance(parsed.get("id"), str):
            resp_id = parsed["id"]
        if isinstance(parsed.get("model"), str):
            model = parsed["model"]
        if isinstance(parsed.get("created"), int):
            created = parsed["created"]
        if isinstance(parsed.get("citations"), list):
            citations = parsed["citations"]
        if isinstance(parsed.get("usage"), dict):
            usage = parsed["usage"]
        choices = parsed.get("choices")
        if isinstance(choices, list) and choices:
            delta = choices[0].get("delta") if isinstance(choices[0], dict) else None
            if isinstance(delta, dict):
                piece = delta.get("content")
                if isinstance(piece, str):
                    content_parts.append(piece)
            else:
                # Some chunks may already carry a full ``message`` field.
                message = choices[0].get("message") if isinstance(choices[0], dict) else None
                if isinstance(message, dict):
                    piece = message.get("content")
                    if isinstance(piece, str):
                        content_parts.append(piece)
    assembled: dict[str, Any] = {
        "choices": [
            {
                "message": {"content": "".join(content_parts)},
                "finish_reason": "stop",
                "index": 0,
            }
        ],
    }
    if citations is not None:
        assembled["citations"] = citations
    if usage is not None:
        assembled["usage"] = usage
    if resp_id is not None:
        assembled["id"] = resp_id
    if model is not None:
        assembled["model"] = model
    if created is not None:
        assembled["created"] = created
    return assembled


def _extract_chat_content(payload: dict[str, Any]) -> tuple[str, list[Any]]:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise PerplexityError("Invalid API response: missing or empty choices array")
    first = choices[0]
    if not isinstance(first, dict):
        raise PerplexityError("Invalid API response: choices[0] not an object")
    message = first.get("message")
    if not isinstance(message, dict):
        raise PerplexityError("Invalid API response: missing message object")
    content = message.get("content")
    if not isinstance(content, str):
        raise PerplexityError("Invalid API response: missing message content")
    citations_raw = payload.get("citations")
    citations = citations_raw if isinstance(citations_raw, list) else []
    return content, citations


def perplexity_chat(
    messages: list[dict[str, str]],
    *,
    model: str,
    strip_thinking: bool = False,
    search_recency_filter: str | None = None,
    search_domain_filter: list[str] | None = None,
    search_context_size: str | None = None,
    reasoning_effort: str | None = None,
    client: httpx.Client | None = None,
) -> str:
    """Low-level chat-completions caller used by ``perplexity_ask`` /
    ``perplexity_research`` / ``perplexity_reason``. Returns the assistant
    content with a citations footer appended (numbered ``[1]..[n]``)."""
    msgs = validate_messages(messages, tool_name=f"perplexity({model})")
    body: dict[str, Any] = {"model": model, "messages": msgs}
    use_streaming = model == RESEARCH_MODEL
    if use_streaming:
        body["stream"] = True
    if search_recency_filter:
        if search_recency_filter not in VALID_RECENCY:
            raise PerplexityError(
                f"search_recency_filter must be one of {VALID_RECENCY}"
            )
        body["search_recency_filter"] = search_recency_filter
    if search_domain_filter:
        if not isinstance(search_domain_filter, list) or not all(
            isinstance(d, str) for d in search_domain_filter
        ):
            raise PerplexityError("search_domain_filter must be a list of strings")
        body["search_domain_filter"] = search_domain_filter
    if search_context_size:
        if search_context_size not in VALID_CONTEXT_SIZE:
            raise PerplexityError(
                f"search_context_size must be one of {VALID_CONTEXT_SIZE}"
            )
        body["web_search_options"] = {"search_context_size": search_context_size}
    if reasoning_effort:
        if reasoning_effort not in VALID_REASONING_EFFORT:
            raise PerplexityError(
                f"reasoning_effort must be one of {VALID_REASONING_EFFORT}"
            )
        body["reasoning_effort"] = reasoning_effort

    resp = _post_json("chat/completions", body, client=client, stream=use_streaming)
    if use_streaming:
        payload = _consume_sse_stream(resp.text)
    else:
        try:
            payload = resp.json()
        except ValueError as exc:
            raise PerplexityError(
                f"Failed to parse JSON response from Perplexity API: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise PerplexityError("Perplexity API returned a non-object response")

    content, citations = _extract_chat_content(payload)
    if strip_thinking:
        content = _strip_think(content)
    if citations:
        content = content.rstrip() + "\n\nCitations:"
        for i, cite in enumerate(citations, 1):
            content += f"\n[{i}] {cite}"
    return content


def perplexity_ask(
    messages: list[dict[str, str]],
    *,
    search_recency_filter: str | None = None,
    search_domain_filter: list[str] | None = None,
    search_context_size: str | None = None,
    client: httpx.Client | None = None,
) -> str:
    """Quick web-grounded Q&A via ``sonar-pro``."""
    return perplexity_chat(
        messages,
        model=ASK_MODEL,
        strip_thinking=False,
        search_recency_filter=search_recency_filter,
        search_domain_filter=search_domain_filter,
        search_context_size=search_context_size,
        client=client,
    )


def perplexity_research(
    messages: list[dict[str, str]],
    *,
    strip_thinking: bool = False,
    reasoning_effort: str | None = None,
    client: httpx.Client | None = None,
) -> str:
    """Deep research via ``sonar-deep-research`` (slow; SSE-streamed)."""
    return perplexity_chat(
        messages,
        model=RESEARCH_MODEL,
        strip_thinking=strip_thinking,
        reasoning_effort=reasoning_effort,
        client=client,
    )


def perplexity_reason(
    messages: list[dict[str, str]],
    *,
    strip_thinking: bool = False,
    search_recency_filter: str | None = None,
    search_domain_filter: list[str] | None = None,
    search_context_size: str | None = None,
    client: httpx.Client | None = None,
) -> str:
    """Step-by-step reasoning via ``sonar-reasoning-pro``."""
    return perplexity_chat(
        messages,
        model=REASON_MODEL,
        strip_thinking=strip_thinking,
        search_recency_filter=search_recency_filter,
        search_domain_filter=search_domain_filter,
        search_context_size=search_context_size,
        client=client,
    )
