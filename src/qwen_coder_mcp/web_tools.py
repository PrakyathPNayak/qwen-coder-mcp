"""Loop 128: web search and URL fetch helpers for the MCP server.

Provides claude-code / ml-intern style web access without requiring an
API key. Search uses DuckDuckGo's HTML endpoint (`html.duckduckgo.com`)
and parses the result list with a deliberately loose regex so a small
markup change at DDG doesn't crash the tool. URL fetch is a thin
`httpx.get` wrapper with a hard byte cap, content-type filter, and
response-text-only return (binary blobs are refused early).

Both functions are pure (no module-level state) and accept an optional
injected `httpx.Client` so tests can use `httpx.MockTransport` instead
of hitting the network.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Iterable

import httpx


_DDG_URL = "https://html.duckduckgo.com/html/"
_DDG_IA_URL = "https://api.duckduckgo.com/"
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
# DDG returns a 202 challenge page when it bot-detects the caller. The
# page is HTML 200/202 with a form posting to anomaly.js?cc=botnet, so a
# naive parse silently returns []. Detect both markers (loop 235) and
# fall back to the Instant Answer JSON API which doesn't bot-block.
_DDG_ANOMALY_MARKERS = ("anomaly.js", "cc=botnet")
_RESULT_RE = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>'
    r'.*?(?:<a[^>]+class="result__snippet"[^>]*>(.*?)</a>'
    r"|<div[^>]+result__snippet[^>]*>(.*?)</div>)?",
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_DDG_REDIRECT_RE = re.compile(
    r"^(?://duckduckgo\.com)?/l/\?(?:[^&]*&)?uddg=([^&]+)", re.IGNORECASE
)

# Content-types we'll surface as text. Anything else -> error.
_TEXT_CONTENT_TYPES = (
    "text/",
    "application/json",
    "application/xml",
    "application/xhtml+xml",
    "application/rss+xml",
    "application/atom+xml",
    "application/javascript",
    "application/ld+json",
    "application/yaml",
)


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str

    def to_dict(self) -> dict[str, str]:
        return {"title": self.title, "url": self.url, "snippet": self.snippet}


def _strip_html(s: str) -> str:
    s = _TAG_RE.sub("", s or "")
    s = html.unescape(s)
    return _WS_RE.sub(" ", s).strip()


def _resolve_ddg_redirect(url: str) -> str:
    """DDG html endpoint wraps results in `/l/?uddg=<urlencoded>`. Unwrap
    it so callers get the real destination URL. Tolerant of absent
    redirect (just returns the input)."""
    m = _DDG_REDIRECT_RE.match(url)
    if not m:
        return url
    from urllib.parse import unquote
    return unquote(m.group(1))


def parse_search_results(html_text: str, max_results: int) -> list[SearchResult]:
    """Extract a bounded list of search results from DDG HTML."""
    out: list[SearchResult] = []
    for match in _RESULT_RE.finditer(html_text):
        if len(out) >= max_results:
            break
        url = _resolve_ddg_redirect(match.group(1).strip())
        title = _strip_html(match.group(2) or "")
        snippet = _strip_html(match.group(3) or match.group(4) or "")
        if not url or not title:
            continue
        out.append(SearchResult(title=title, url=url, snippet=snippet))
    return out


def _is_ddg_anomaly(html_text: str, status_code: int) -> bool:
    """Detect DDG's bot-challenge page (loop 235).

    DDG returns a 202 (sometimes 200) challenge HTML when the caller is
    fingerprinted as a bot. The page contains a form posting to
    ``anomaly.js`` with ``cc=botnet``. Our result regex finds nothing in
    that page, so the previous behaviour was a silent empty list. This
    helper lets callers explicitly route to the IA fallback instead.
    """
    if not html_text:
        return False
    sample = html_text[:4000].lower()
    return any(marker in sample for marker in _DDG_ANOMALY_MARKERS)


def _ddg_ia_search(
    query: str,
    *,
    max_results: int,
    client: httpx.Client,
) -> list[SearchResult]:
    """Fallback search via DDG's Instant Answer JSON API (loop 235).

    Used when ``html.duckduckgo.com`` returns the anomaly/botnet
    challenge page. The IA API is sparser than full web search (it
    surfaces the abstract + topical sub-pages from DDG's curated index)
    but it doesn't rate-limit by user-agent and reliably returns
    something for common queries instead of an empty list.

    Raises ``httpx.HTTPError`` on network failure.
    """
    params = {
        "q": query,
        "format": "json",
        "no_html": "1",
        "skip_disambig": "0",
        "t": "qwen-coder-mcp",
    }
    resp = client.get(_DDG_IA_URL, params=params)
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError:
        return []
    out: list[SearchResult] = []
    abstract = (data.get("AbstractText") or "").strip()
    abstract_url = (data.get("AbstractURL") or "").strip()
    heading = (data.get("Heading") or query).strip()
    if abstract and abstract_url:
        out.append(SearchResult(title=heading, url=abstract_url, snippet=abstract))

    def _walk(topics: list) -> None:
        for t in topics:
            if len(out) >= max_results:
                return
            if not isinstance(t, dict):
                continue
            if "Topics" in t and isinstance(t["Topics"], list):
                _walk(t["Topics"])
                continue
            url = (t.get("FirstURL") or "").strip()
            text = (t.get("Text") or "").strip()
            if not url or not text:
                continue
            title, _, snippet = text.partition(" - ")
            out.append(
                SearchResult(
                    title=title or text,
                    url=url,
                    snippet=snippet or text,
                )
            )

    _walk(data.get("RelatedTopics") or [])
    return out[:max_results]


def web_search(
    query: str,
    *,
    max_results: int = 5,
    timeout: float = 10.0,
    client: httpx.Client | None = None,
) -> list[SearchResult]:
    """Search DuckDuckGo HTML and return up to `max_results` SearchResults.

    Raises `ValueError` on empty query or non-positive `max_results`.
    Raises `httpx.HTTPError` (or subclass) on network failure -- caller
    decides how to surface it (e.g., wrap in a TextContent error reply).
    """
    q = (query or "").strip()
    if not q:
        raise ValueError("query must be non-empty")
    if max_results <= 0:
        raise ValueError("max_results must be positive")
    headers = {"User-Agent": _USER_AGENT, "Accept": "text/html"}
    owns = client is None
    c = client or httpx.Client(headers=headers, timeout=timeout, follow_redirects=True)
    try:
        resp = c.post(_DDG_URL, data={"q": q})
        resp.raise_for_status()
        # Loop 235: DDG bot-detects scrapers and serves a challenge page
        # (HTTP 202 + anomaly.js form) where our result regex matches
        # nothing. Detect that case and fall back to the Instant Answer
        # JSON API instead of silently returning [].
        if _is_ddg_anomaly(resp.text, resp.status_code):
            return _ddg_ia_search(q, max_results=max_results, client=c)
        results = parse_search_results(resp.text, max_results=max_results)
        if not results:
            # Empty parse on a non-anomaly page can mean DDG changed
            # markup. Try the IA fallback before giving up so the caller
            # at least sees an abstract.
            return _ddg_ia_search(q, max_results=max_results, client=c)
        return results
    finally:
        if owns:
            c.close()


def _is_text_content(content_type: str) -> bool:
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    return any(ct.startswith(prefix) for prefix in _TEXT_CONTENT_TYPES)


def fetch_url(
    url: str,
    *,
    max_bytes: int = 200_000,
    timeout: float = 15.0,
    client: httpx.Client | None = None,
) -> dict[str, object]:
    """Fetch a URL and return `{status, url, content_type, text, truncated}`.

    Refuses to return binary content. Truncates `text` to `max_bytes`
    UTF-8 bytes (decoded loosely). Raises `ValueError` on empty URL or
    non-http(s) scheme.
    """
    u = (url or "").strip()
    if not u:
        raise ValueError("url must be non-empty")
    if not (u.startswith("http://") or u.startswith("https://")):
        raise ValueError("url must use http or https scheme")
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    headers = {"User-Agent": _USER_AGENT}
    owns = client is None
    c = client or httpx.Client(headers=headers, timeout=timeout, follow_redirects=True)
    try:
        resp = c.get(u)
        ct = resp.headers.get("content-type", "")
        if not _is_text_content(ct):
            return {
                "status": resp.status_code,
                "url": str(resp.url),
                "content_type": ct,
                "text": "",
                "truncated": False,
                "error": "non_text_content",
            }
        body = resp.text
        encoded = body.encode("utf-8", errors="replace")
        truncated = len(encoded) > max_bytes
        if truncated:
            body = encoded[:max_bytes].decode("utf-8", errors="replace")
        return {
            "status": resp.status_code,
            "url": str(resp.url),
            "content_type": ct,
            "text": body,
            "truncated": truncated,
        }
    finally:
        if owns:
            c.close()


def format_search_results(results: Iterable[SearchResult]) -> str:
    """Render results as a numbered text block suitable for a TextContent
    reply. Compact and parseable by both humans and Qwen."""
    lines: list[str] = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r.title}")
        lines.append(f"   {r.url}")
        if r.snippet:
            lines.append(f"   {r.snippet}")
    return "\n".join(lines) if lines else "(no results)"
