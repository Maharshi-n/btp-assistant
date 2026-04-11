"""Phase 5: web search and web fetch tools.

web_search — DuckDuckGo via the `ddgs` library (no API key needed).
web_fetch  — httpx GET + basic text extraction (strips HTML tags).
"""
from __future__ import annotations

import re
from typing import Annotated

import httpx
from langchain_core.tools import tool


# ---------------------------------------------------------------------------
# Web search
# ---------------------------------------------------------------------------

@tool
def web_search(
    query: Annotated[str, "Search query to look up on DuckDuckGo"],
    max_results: Annotated[int, "Maximum number of results to return (default 5)"] = 5,
) -> str:
    """Search the web using DuckDuckGo and return a list of results with titles, URLs, and snippets."""
    try:
        from ddgs import DDGS
    except ImportError:
        return "Error: 'ddgs' package is not installed. Run: pip install ddgs"

    try:
        with DDGS() as ddg:
            results = list(ddg.text(query, max_results=max_results))
    except Exception as e:
        return f"Error performing web search: {e}"

    if not results:
        return f"No results found for '{query}'."

    lines = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "No title")
        url = r.get("href", "")
        snippet = r.get("body", "")
        lines.append(f"{i}. **{title}**\n   URL: {url}\n   {snippet}")

    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Web fetch
# ---------------------------------------------------------------------------

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\n{3,}")


def _extract_text(html: str) -> str:
    """Very lightweight HTML → plain text: strip tags, collapse whitespace."""
    # Remove script and style blocks entirely
    text = re.sub(r"<(script|style)[^>]*>.*?</(script|style)>", "", html, flags=re.DOTALL | re.IGNORECASE)
    # Replace block-level tags with newlines
    text = re.sub(r"<(br|p|div|h[1-6]|li|tr)[^>]*/?>", "\n", text, flags=re.IGNORECASE)
    # Strip remaining tags
    text = _HTML_TAG_RE.sub("", text)
    # Decode common HTML entities
    text = (
        text.replace("&amp;", "&")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&quot;", '"')
            .replace("&#39;", "'")
            .replace("&nbsp;", " ")
    )
    # Collapse excessive blank lines
    text = _WHITESPACE_RE.sub("\n\n", text)
    return text.strip()


@tool
def web_fetch(
    url: Annotated[str, "URL to fetch and extract text from"],
    max_chars: Annotated[int, "Maximum characters to return (default 8000)"] = 8000,
) -> str:
    """Fetch a webpage and return its text content (HTML tags stripped)."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; BTPAssistant/1.0; +https://github.com/maharshi)"
        )
    }
    try:
        with httpx.Client(follow_redirects=True, timeout=20) as client:
            response = client.get(url, headers=headers)
            response.raise_for_status()
    except httpx.HTTPStatusError as e:
        return f"Error: HTTP {e.response.status_code} when fetching '{url}'."
    except httpx.RequestError as e:
        return f"Error fetching '{url}': {e}"

    content_type = response.headers.get("content-type", "")
    if "html" in content_type:
        text = _extract_text(response.text)
    else:
        # For plain text, JSON, etc. — return as-is
        text = response.text

    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n[... truncated at {max_chars} characters]"

    return text
