"""Perplexity API client + MCP tool helpers.

This is a faithful Python port of the surface area documented by:

* The reference Perplexity MCP server -- ``perplexityai/modelcontextprotocol``
  (MIT license).  Provides the four web-grounded chat tools and the
  search tool.
* The ``perplexity`` Python SDK -- ``perplexityai/perplexity-py``
  (Apache-2.0 license).  Documents the full request parameter surface
  for ``/chat/completions``, ``/search``, ``/v1/embeddings`` and
  ``/async/chat/completions``.

All upstream code was treated as read-only reference material -- this
module is original Python, but the request and response *shapes* are
deliberately byte-compatible with what those upstreams send and
expect, so an operator can swap one for the other and not notice.

Endpoints exposed:

* ``perplexity_search``      -- POST ``/search``
* ``perplexity_ask``         -- POST ``/chat/completions``  (sonar-pro)
* ``perplexity_research``    -- POST ``/chat/completions``  (sonar-deep-research, SSE)
* ``perplexity_reason``      -- POST ``/chat/completions``  (sonar-reasoning-pro)
* ``perplexity_embed``       -- POST ``/v1/embeddings``
* ``perplexity_async_create``-- POST ``/async/chat/completions``
* ``perplexity_async_get``   -- GET  ``/async/chat/completions/{id}``
* ``perplexity_async_list``  -- GET  ``/async/chat/completions``

Configuration mirrors the reference MCP server so existing operator
habits carry over:

| env var                  | default                       |
| ------------------------ | ----------------------------- |
| ``PERPLEXITY_API_KEY``   | required                      |
| ``PERPLEXITY_BASE_URL``  | ``https://api.perplexity.ai`` |
| ``PERPLEXITY_TIMEOUT_MS``| 300000  (5 min)               |
| ``PERPLEXITY_PROXY``     | unset; falls back to ``HTTPS_PROXY`` / ``HTTP_PROXY`` |

The functions are pure (no module-level state) and accept an optional
injected ``httpx.Client`` so unit tests use ``httpx.MockTransport``
instead of hitting the real API.
"""
from __future__ import annotations

import os
import re
import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import httpx

# ----------------------------------------------------------- constants

DEFAULT_BASE_URL = "https://api.perplexity.ai"
DEFAULT_TIMEOUT_SECONDS = 300.0
USER_AGENT = "qwen-coder-mcp-perplexity/0.2"

# Models exposed by the four chat-completions tools.
ASK_MODEL = "sonar-pro"
RESEARCH_MODEL = "sonar-deep-research"
REASON_MODEL = "sonar-reasoning-pro"

# Embedding models documented by the SDK -- use whichever the operator
# prefers; we don't pick a default to avoid lock-in.
EMBED_MODELS = ("pplx-embed-v1-0.6b", "pplx-embed-v1-4b")

# Allowed enum values, mirroring the upstream type definitions.
VALID_RECENCY = ("hour", "day", "week", "month", "year")
VALID_CONTEXT_SIZE = ("low", "medium", "high")
VALID_REASONING_EFFORT = ("minimal", "low", "medium", "high")
VALID_SEARCH_MODE = ("web", "academic", "sec")
VALID_SEARCH_TYPE = ("fast", "pro", "auto")
VALID_ROLES = ("system", "user", "assistant")
VALID_EMBED_ENCODING = ("base64_int8", "base64_binary")
VALID_ASYNC_STATUS = ("CREATED", "IN_PROGRESS", "COMPLETED", "FAILED")

# Strip Qwen-style ``<think>...</think>`` reasoning blocks. Kept local
# so this module has no coupling to qwen_client.
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


class PerplexityError(Exception):
    """Raised on any Perplexity-tool error -- caller surfaces as text."""


# ----------------------------------------------------------- helpers

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
    kwargs: dict[str, Any] = {
        "headers": headers,
        "timeout": t,
        "follow_redirects": True,
    }
    if proxy:
        kwargs["proxy"] = proxy
    return httpx.Client(**kwargs)


def _enum(name: str, value: Any, allowed: tuple[str, ...]) -> str:
    """Validate ``value`` against ``allowed`` and return it. ``None`` is
    rejected -- callers should not invoke this for optional fields when
    the value is absent."""
    if value not in allowed:
        raise PerplexityError(f"{name} must be one of {allowed}")
    return value


def _str_list(name: str, value: Any) -> list[str]:
    """Validate ``value`` is a list of strings and return a defensive
    copy. Used for ``search_domain_filter`` / ``search_language_filter``
    / etc."""
    if not isinstance(value, list) or not all(isinstance(s, str) for s in value):
        raise PerplexityError(f"{name} must be a list of strings")
    return list(value)


def validate_messages(messages: Any, *, tool_name: str = "perplexity") -> list[dict[str, str]]:
    """Validate a chat ``messages`` argument and normalise to plain dicts.

    Raises :class:`PerplexityError` if the structure is malformed --
    the server layer catches that and surfaces a readable error to the
    MCP client. Mirrors the reference server's ``validateMessages``
    contract: ``role`` and ``content`` both required, ``content`` must
    be a string, ``role`` constrained to ``{system,user,assistant}``
    matching the documented input enum."""
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


def _request(
    method: str,
    endpoint: str,
    *,
    body: Mapping[str, Any] | None = None,
    stream: bool = False,
    client: httpx.Client | None = None,
) -> httpx.Response:
    """Issue a single HTTP request to the Perplexity API.

    Centralises auth, error mapping, and client lifetime. Always returns
    a fully-buffered ``httpx.Response`` -- streaming endpoints (only
    ``sonar-deep-research`` today) are buffered server-side and parsed
    line-by-line by :func:`_consume_sse_stream`. Buffered streaming is
    correct because Perplexity's SSE responses are bounded; switching
    to truly-incremental streaming would require exposing a generator,
    which neither the MCP layer nor the TUI consume.

    The ``stream`` flag only affects the ``Accept`` header. Client
    lifetime is handled in a single ``try/finally`` so we never leak
    a connection on either the timeout, the network-error, or the
    HTTP-status-error paths.
    """
    api_key = _resolve_api_key()
    base = _resolve_base_url()
    url = f"{base}/{endpoint.lstrip('/')}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "text/event-stream" if stream else "application/json",
        "User-Agent": USER_AGENT,
        "X-Source": "qwen-coder-mcp",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"

    owns = client is None
    c = client or _build_client()
    timeout_seconds: float | None
    try:
        timeout_seconds = c.timeout.connect if c.timeout else None
    except Exception:  # noqa: BLE001
        timeout_seconds = None

    try:
        try:
            if method == "GET":
                resp = c.get(url, headers=headers)
            else:
                resp = c.request(method, url, json=body, headers=headers)
        except httpx.TimeoutException as exc:
            raise PerplexityError(
                f"Perplexity API timeout after {timeout_seconds or '?'}s "
                f"(set PERPLEXITY_TIMEOUT_MS to extend)"
            ) from exc
        except httpx.HTTPError as exc:
            raise PerplexityError(f"Perplexity network error: {exc}") from exc

        if resp.status_code >= 400:
            body_text = resp.text or ""
            # Cap so we don't dump a 5 MB HTML error page into a
            # TextContent reply.
            snippet = body_text[:1000]
            raise PerplexityError(
                f"Perplexity API error: {resp.status_code} "
                f"{resp.reason_phrase}\n{snippet}"
            )
        return resp
    finally:
        if owns:
            c.close()


# ----------------------------------------------------------- /search

@dataclass(frozen=True)
class PerplexitySearchResult:
    """One result row returned by ``/search``.

    ``snippet``, ``date``, and ``score`` are optional in the upstream
    Zod schema so they default to empty / ``None`` here."""

    title: str
    url: str
    snippet: str = ""
    date: str = ""
    score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "date": self.date,
        }
        if self.score is not None:
            d["score"] = self.score
        return d


def _parse_search_response(payload: Any) -> list[PerplexitySearchResult]:
    """Parse a ``/search`` JSON body into a list of typed rows.

    Rows that don't conform to the upstream Zod schema (missing or
    non-string ``title`` / ``url``) are skipped silently because the
    MCP client expects a clean list -- but unlike the previous
    revision we *do* surface rows whose ``title`` or ``url`` happen
    to be empty strings, since the schema only requires the field be
    a string."""
    if not isinstance(payload, dict):
        return []
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        return []
    out: list[PerplexitySearchResult] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        url = item.get("url")
        if not isinstance(title, str) or not isinstance(url, str):
            continue
        snippet = item.get("snippet") or ""
        date = item.get("date") or ""
        score = item.get("score")
        out.append(
            PerplexitySearchResult(
                title=title,
                url=url,
                snippet=str(snippet),
                date=str(date),
                score=float(score) if isinstance(score, (int, float)) else None,
            )
        )
    return out


def perplexity_search(
    query: str,
    *,
    max_results: int = 10,
    max_tokens_per_page: int = 1024,
    max_tokens: int | None = None,
    country: str | None = None,
    search_mode: str | None = None,
    search_recency_filter: str | None = None,
    search_domain_filter: list[str] | None = None,
    search_language_filter: list[str] | None = None,
    last_updated_after_filter: str | None = None,
    last_updated_before_filter: str | None = None,
    search_after_date_filter: str | None = None,
    search_before_date_filter: str | None = None,
    client: httpx.Client | None = None,
) -> list[PerplexitySearchResult]:
    """Run a Perplexity ``/search`` request and return parsed results.

    Faithful port of ``SearchCreateParams`` from the perplexity-py SDK.
    ``max_results`` is clamped to ``1..20`` and ``max_tokens_per_page``
    to ``256..2048`` to match the bounds documented by the reference
    MCP server. All other parameters are passed through verbatim so
    new operator features (academic search, language filters, dated
    queries, ...) work out of the box."""
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
    if max_tokens is not None:
        body["max_tokens"] = int(max_tokens)
    if country:
        body["country"] = str(country).strip()
    if search_mode:
        body["search_mode"] = _enum("search_mode", search_mode, VALID_SEARCH_MODE)
    if search_recency_filter:
        body["search_recency_filter"] = _enum(
            "search_recency_filter", search_recency_filter, VALID_RECENCY
        )
    if search_domain_filter:
        body["search_domain_filter"] = _str_list(
            "search_domain_filter", search_domain_filter
        )
    if search_language_filter:
        body["search_language_filter"] = _str_list(
            "search_language_filter", search_language_filter
        )
    if last_updated_after_filter:
        body["last_updated_after_filter"] = str(last_updated_after_filter)
    if last_updated_before_filter:
        body["last_updated_before_filter"] = str(last_updated_before_filter)
    if search_after_date_filter:
        body["search_after_date_filter"] = str(search_after_date_filter)
    if search_before_date_filter:
        body["search_before_date_filter"] = str(search_before_date_filter)
    resp = _request("POST", "search", body=body, client=client)
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
        title = r.title or "(untitled)"
        url = r.url or "(no url)"
        lines.append(f"{i}. {title}")
        lines.append(f"   {url}")
        if r.snippet:
            lines.append(f"   {r.snippet}")
        if r.date:
            lines.append(f"   ({r.date})")
        if r.score is not None:
            lines.append(f"   score={r.score:.3f}")
    return "\n".join(lines)


# ----------------------------------------------------- /chat/completions

def _consume_sse_stream(text: str) -> dict[str, Any]:
    """Reassemble a full chat-completions response from a buffered SSE
    body. Mirrors ``consumeSSEStream`` in the reference server -- walks
    each ``data:`` line, JSON-decodes it, accumulates ``delta.content``
    pieces and any cumulative metadata fields, and returns a synthesised
    dict shaped like the non-streaming response so the caller's parsing
    path is uniform."""
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
            first = choices[0] if isinstance(choices[0], dict) else None
            if first is not None:
                delta = first.get("delta")
                if isinstance(delta, dict):
                    piece = delta.get("content")
                    if isinstance(piece, str):
                        content_parts.append(piece)
                else:
                    # Some chunks may already carry a full ``message``.
                    message = first.get("message")
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
    """Pull the assistant content + citations out of a chat-completions
    response. Validation here mirrors the upstream Zod schema:
    ``choices`` must be present, non-empty, and the first element must
    expose a ``message.content`` string."""
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


def _build_web_search_options(
    *,
    search_context_size: str | None,
    search_type: str | None,
    user_location: dict[str, Any] | None,
    image_results_enhanced_relevance: bool | None,
) -> dict[str, Any] | None:
    """Build the ``web_search_options`` request payload, omitting unset
    keys so the body matches what the SDK would have sent."""
    opts: dict[str, Any] = {}
    if search_context_size:
        opts["search_context_size"] = _enum(
            "search_context_size", search_context_size, VALID_CONTEXT_SIZE
        )
    if search_type:
        opts["search_type"] = _enum("search_type", search_type, VALID_SEARCH_TYPE)
    if user_location is not None:
        if not isinstance(user_location, dict):
            raise PerplexityError("user_location must be an object")
        opts["user_location"] = dict(user_location)
    if image_results_enhanced_relevance is not None:
        opts["image_results_enhanced_relevance"] = bool(
            image_results_enhanced_relevance
        )
    return opts or None


def _build_chat_body(
    messages: list[dict[str, str]],
    *,
    model: str,
    stream: bool = False,
    # search / web
    search_recency_filter: str | None = None,
    search_domain_filter: list[str] | None = None,
    search_language_filter: list[str] | None = None,
    search_mode: str | None = None,
    search_after_date_filter: str | None = None,
    search_before_date_filter: str | None = None,
    last_updated_after_filter: str | None = None,
    last_updated_before_filter: str | None = None,
    disable_search: bool | None = None,
    return_related_questions: bool | None = None,
    return_images: bool | None = None,
    # web_search_options sub-object
    search_context_size: str | None = None,
    search_type: str | None = None,
    user_location: dict[str, Any] | None = None,
    image_results_enhanced_relevance: bool | None = None,
    # generation
    reasoning_effort: str | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    top_k: int | None = None,
    max_tokens: int | None = None,
    frequency_penalty: float | None = None,
    presence_penalty: float | None = None,
    stop: str | list[str] | None = None,
    country: str | None = None,
    response_format: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the ``/chat/completions`` request body from the union of
    options documented by the perplexity-py SDK. Only sets keys whose
    values were supplied by the caller -- everything else is omitted so
    the API can apply its own defaults."""
    body: dict[str, Any] = {"model": model, "messages": messages}
    if stream:
        body["stream"] = True
    if search_recency_filter:
        body["search_recency_filter"] = _enum(
            "search_recency_filter", search_recency_filter, VALID_RECENCY
        )
    if search_domain_filter:
        body["search_domain_filter"] = _str_list(
            "search_domain_filter", search_domain_filter
        )
    if search_language_filter:
        body["search_language_filter"] = _str_list(
            "search_language_filter", search_language_filter
        )
    if search_mode:
        body["search_mode"] = _enum("search_mode", search_mode, VALID_SEARCH_MODE)
    if search_after_date_filter:
        body["search_after_date_filter"] = str(search_after_date_filter)
    if search_before_date_filter:
        body["search_before_date_filter"] = str(search_before_date_filter)
    if last_updated_after_filter:
        body["last_updated_after_filter"] = str(last_updated_after_filter)
    if last_updated_before_filter:
        body["last_updated_before_filter"] = str(last_updated_before_filter)
    if disable_search is not None:
        body["disable_search"] = bool(disable_search)
    if return_related_questions is not None:
        body["return_related_questions"] = bool(return_related_questions)
    if return_images is not None:
        body["return_images"] = bool(return_images)
    if reasoning_effort:
        body["reasoning_effort"] = _enum(
            "reasoning_effort", reasoning_effort, VALID_REASONING_EFFORT
        )
    if temperature is not None:
        body["temperature"] = float(temperature)
    if top_p is not None:
        body["top_p"] = float(top_p)
    if top_k is not None:
        body["top_k"] = int(top_k)
    if max_tokens is not None:
        body["max_tokens"] = int(max_tokens)
    if frequency_penalty is not None:
        body["frequency_penalty"] = float(frequency_penalty)
    if presence_penalty is not None:
        body["presence_penalty"] = float(presence_penalty)
    if stop is not None:
        body["stop"] = stop
    if country:
        body["country"] = str(country).strip()
    if response_format is not None:
        if not isinstance(response_format, dict):
            raise PerplexityError("response_format must be an object")
        body["response_format"] = response_format
    wso = _build_web_search_options(
        search_context_size=search_context_size,
        search_type=search_type,
        user_location=user_location,
        image_results_enhanced_relevance=image_results_enhanced_relevance,
    )
    if wso:
        body["web_search_options"] = wso
    return body


def perplexity_chat(
    messages: list[dict[str, str]],
    *,
    model: str,
    strip_thinking: bool = False,
    return_raw: bool = False,
    client: httpx.Client | None = None,
    **options: Any,
) -> str | dict[str, Any]:
    """Low-level chat-completions caller.

    Accepts every option documented by ``_build_chat_body`` via ``**options``
    so callers can pass any of the SDK's documented fields (e.g.
    ``temperature``, ``search_mode``, ``user_location``).

    Returns the formatted assistant content with a numbered citations
    footer when ``return_raw`` is False (the default). When
    ``return_raw=True`` returns the full JSON-decoded response so
    callers that need related questions / usage / images can inspect
    them directly.
    """
    msgs = validate_messages(messages, tool_name=f"perplexity({model})")
    use_streaming = model == RESEARCH_MODEL
    body = _build_chat_body(
        msgs, model=model, stream=use_streaming, **options
    )
    resp = _request(
        "POST", "chat/completions", body=body, stream=use_streaming, client=client
    )
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
    # Surface related questions if the API returned them and the caller
    # asked for them via the request flag (best effort -- the field is
    # not in every response).
    related = payload.get("related_questions")
    if isinstance(related, list) and related:
        content += "\n\nRelated questions:"
        for q in related:
            content += f"\n- {q}"
    if return_raw:
        return {"content": content, "raw": payload}
    return content


def perplexity_ask(
    messages: list[dict[str, str]],
    *,
    client: httpx.Client | None = None,
    **options: Any,
) -> str:
    """Quick web-grounded Q&A via ``sonar-pro``.

    Accepts every option documented by ``perplexity_chat`` via ``**options``."""
    return perplexity_chat(  # type: ignore[return-value]
        messages, model=ASK_MODEL, strip_thinking=False, client=client, **options
    )


def perplexity_research(
    messages: list[dict[str, str]],
    *,
    strip_thinking: bool = False,
    client: httpx.Client | None = None,
    **options: Any,
) -> str:
    """Deep research via ``sonar-deep-research`` (slow; SSE-streamed)."""
    return perplexity_chat(  # type: ignore[return-value]
        messages,
        model=RESEARCH_MODEL,
        strip_thinking=strip_thinking,
        client=client,
        **options,
    )


def perplexity_reason(
    messages: list[dict[str, str]],
    *,
    strip_thinking: bool = False,
    client: httpx.Client | None = None,
    **options: Any,
) -> str:
    """Step-by-step reasoning via ``sonar-reasoning-pro``."""
    return perplexity_chat(  # type: ignore[return-value]
        messages,
        model=REASON_MODEL,
        strip_thinking=strip_thinking,
        client=client,
        **options,
    )


# --------------------------------------------------------- /v1/embeddings

@dataclass(frozen=True)
class PerplexityEmbeddingsResult:
    """Response from ``/v1/embeddings``.

    ``data`` is the list of per-input embedding objects (each with
    ``index``, ``embedding``, ``object``) as returned by the API.
    ``encoding_format`` is set when the caller requested a non-default
    encoding -- it is preserved here so the receiver knows how to
    decode the embedding values."""

    data: list[dict[str, Any]]
    model: str = ""
    usage: dict[str, Any] | None = None
    encoding_format: str | None = None


def perplexity_embed(
    input: str | list[str],
    *,
    model: str,
    dimensions: int | None = None,
    encoding_format: str | None = None,
    client: httpx.Client | None = None,
) -> PerplexityEmbeddingsResult:
    """Generate embeddings via ``POST /v1/embeddings``.

    Faithful port of ``EmbeddingCreateParams`` from the SDK:

    * ``input`` -- one string or a list of up to 512 strings (caller's
      responsibility to keep totals under the 120k-token budget).
    * ``model`` -- one of :data:`EMBED_MODELS`.
    * ``dimensions`` -- Matryoshka truncation length (128..1024 for the
      0.6b model, 128..2560 for the 4b model). Server enforces the
      bounds; we just pass it through.
    * ``encoding_format`` -- ``base64_int8`` or ``base64_binary``;
      omit for the default float-array encoding.
    """
    if isinstance(input, str):
        if not input.strip():
            raise PerplexityError("perplexity_embed: input must be non-empty")
        payload_input: str | list[str] = input
    elif isinstance(input, list):
        if not input or not all(isinstance(x, str) and x for x in input):
            raise PerplexityError(
                "perplexity_embed: input must be a non-empty list of non-empty strings"
            )
        if len(input) > 512:
            raise PerplexityError(
                "perplexity_embed: input list exceeds 512 entries"
            )
        payload_input = list(input)
    else:
        raise PerplexityError(
            "perplexity_embed: input must be a string or list of strings"
        )
    if not model:
        raise PerplexityError("perplexity_embed: model is required")
    if model not in EMBED_MODELS:
        # Don't hard-reject -- the API may add new models. Just warn-by-text.
        # But the SDK's TypedDict literal would reject; we mirror that by
        # raising on completely garbage values is overzealous, so we accept
        # any non-empty string here.
        pass
    body: dict[str, Any] = {"input": payload_input, "model": model}
    if dimensions is not None:
        if not isinstance(dimensions, int) or dimensions < 1:
            raise PerplexityError("dimensions must be a positive integer")
        body["dimensions"] = dimensions
    if encoding_format is not None:
        body["encoding_format"] = _enum(
            "encoding_format", encoding_format, VALID_EMBED_ENCODING
        )
    resp = _request("POST", "v1/embeddings", body=body, client=client)
    try:
        payload = resp.json()
    except ValueError as exc:
        raise PerplexityError(
            f"perplexity_embed: invalid JSON response: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise PerplexityError("perplexity_embed: response is not an object")
    data = payload.get("data")
    if not isinstance(data, list):
        data = []
    return PerplexityEmbeddingsResult(
        data=[d for d in data if isinstance(d, dict)],
        model=str(payload.get("model") or ""),
        usage=payload.get("usage") if isinstance(payload.get("usage"), dict) else None,
        encoding_format=encoding_format,
    )


def format_embeddings_result(res: PerplexityEmbeddingsResult) -> str:
    """Compact one-line-per-vector summary for TUI display.

    Embeddings can be ~2k floats per vector -- we never want to dump
    those into a chat panel. Instead surface index + dimensions +
    first-few-values, plus the usage block if present."""
    if not res.data:
        return "(no embeddings returned)"
    lines: list[str] = [f"Generated {len(res.data)} embedding(s) (model={res.model or '?'})"]
    if res.encoding_format:
        lines.append(f"  encoding_format={res.encoding_format}")
    for i, obj in enumerate(res.data):
        emb = obj.get("embedding")
        idx = obj.get("index", i)
        if isinstance(emb, list):
            head = ", ".join(
                f"{v:.4f}" if isinstance(v, (int, float)) else str(v)
                for v in emb[:4]
            )
            tail_marker = ", ..." if len(emb) > 4 else ""
            lines.append(f"  [{idx}] dim={len(emb)} [{head}{tail_marker}]")
        elif isinstance(emb, str):
            # base64_int8 / base64_binary
            preview = emb[:32] + ("..." if len(emb) > 32 else "")
            lines.append(f"  [{idx}] {res.encoding_format or 'base64'} {preview!r}")
        else:
            lines.append(f"  [{idx}] (no embedding payload)")
    if res.usage:
        lines.append(f"  usage={res.usage}")
    return "\n".join(lines)


# ---------------------------------------------- /async/chat/completions

def perplexity_async_create(
    messages: list[dict[str, str]],
    *,
    model: str,
    idempotency_key: str | None = None,
    client: httpx.Client | None = None,
    **options: Any,
) -> dict[str, Any]:
    """Submit an asynchronous chat-completions job.

    Faithful port of ``CompletionCreateParams`` from the
    ``async_/chat/completions`` resource. The request body has the
    shape ``{"request": {model, messages, ...}, "idempotency_key": ...}``
    -- note the wrapper key, which is what distinguishes this from the
    sync endpoint.

    Returns the API's raw JSON response (typically ``{id, status,
    created_at, ...}``). Use :func:`perplexity_async_get` to poll for
    completion."""
    msgs = validate_messages(messages, tool_name=f"async-create({model})")
    inner = _build_chat_body(msgs, model=model, **options)
    body: dict[str, Any] = {"request": inner}
    if idempotency_key:
        body["idempotency_key"] = str(idempotency_key)
    resp = _request("POST", "async/chat/completions", body=body, client=client)
    try:
        return resp.json()
    except ValueError as exc:
        raise PerplexityError(
            f"perplexity_async_create: invalid JSON response: {exc}"
        ) from exc


def perplexity_async_get(
    api_request_id: str,
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Poll one async job by id. Returns the full record:
    ``{id, model, status, created_at, started_at?, completed_at?,
    failed_at?, error_message?, response?}``."""
    rid = (api_request_id or "").strip()
    if not rid:
        raise PerplexityError("perplexity_async_get: api_request_id is required")
    resp = _request("GET", f"async/chat/completions/{rid}", client=client)
    try:
        return resp.json()
    except ValueError as exc:
        raise PerplexityError(
            f"perplexity_async_get: invalid JSON response: {exc}"
        ) from exc


def perplexity_async_list(
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """List all async jobs. Returns the API's raw JSON envelope --
    typically ``{"data": [...]}``."""
    resp = _request("GET", "async/chat/completions", client=client)
    try:
        return resp.json()
    except ValueError as exc:
        raise PerplexityError(
            f"perplexity_async_list: invalid JSON response: {exc}"
        ) from exc


def format_async_record(record: Mapping[str, Any]) -> str:
    """Render one async record into the compact form the TUI expects.

    The status field is always present in well-formed records; we
    promote it to the first line so glance-scanning a queue of jobs is
    cheap."""
    if not isinstance(record, Mapping):
        return str(record)
    status = record.get("status") or "?"
    rid = record.get("id") or "?"
    model = record.get("model") or "?"
    lines = [f"[{status}] id={rid} model={model}"]
    for key in ("created_at", "started_at", "completed_at", "failed_at"):
        if record.get(key):
            lines.append(f"  {key}={record[key]}")
    if record.get("error_message"):
        lines.append(f"  error: {record['error_message']}")
    response = record.get("response")
    if isinstance(response, dict):
        # Try to pull the assistant content out, the same shape as the
        # sync endpoint's response body.
        try:
            content, citations = _extract_chat_content(response)
        except PerplexityError:
            content, citations = "", []
        if content:
            lines.append("  response:")
            for cl in content.splitlines():
                lines.append(f"    {cl}")
            if citations:
                lines.append("  citations:")
                for i, cite in enumerate(citations, 1):
                    lines.append(f"    [{i}] {cite}")
    return "\n".join(lines)


def format_async_list(payload: Any) -> str:
    """Render an async-list response as a stack of formatted records."""
    if isinstance(payload, dict):
        items = payload.get("data") or payload.get("items") or []
    elif isinstance(payload, list):
        items = payload
    else:
        items = []
    if not isinstance(items, list) or not items:
        return "(no async jobs)"
    return "\n\n".join(format_async_record(r) for r in items if isinstance(r, dict))
