"""Tavily web search behind an explicit opt-in + privacy rules.

Enabled only when SEARCH_ENABLED=true AND TAVILY_API_KEY is set. Tavily's
privacy policy lets them reuse and onward-share queries, so:

- send only a short query the model wrote (<= 240 chars after checks);
- never send memories, owner facts, instructions, the task text verbatim,
  private documents or secrets (enforced by scans + tests);
- tasks marked private, or queries flagged sensitive -> the owner sees
  the query first (search_query approval) or we fail closed;
- results are candidate URLs only - pages are fetched by our own guard.

We do not claim the sensitivity heuristics catch everything.
"""

import json
import re
import urllib.parse

from .config import TAVILY_BASE_URL, TAVILY_HOST
from .netclient import APIClient, HTTPError, TransportError
from .provider import ProviderRateLimit, ProviderUnavailable, reset_at_iso

MAX_QUERY_LEN = 240

# Things that must never reach the search API. Patterns aim at credential
# shapes and obvious personal data; they are a seatbelt, not a promise.
SENSITIVE_PATTERNS = [
    re.compile(r"\bgsk_[A-Za-z0-9]{10,}"),        # Groq key
    re.compile(r"\btvly-[A-Za-z0-9]{8,}"),        # Tavily key
    re.compile(r"\bsk-[A-Za-z0-9_-]{10,}"),       # OpenAI-style key
    re.compile(r"\bAIza[A-Za-z0-9_-]{10,}"),      # Google key
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),  # email
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),         # SSN-shaped
    re.compile(r"(?i)\b(passw?or?d|api[_-]?key|secret|token)\s*[:=]"),
    re.compile(r"\b(?:\d[ -]?){13,19}\b"),        # card-number-shaped
]


class SearchDisabled(Exception):
    kind = "search_disabled"


class SearchError(Exception):
    kind = "search_error"


def search_configured(env) -> bool:
    return bool(env.get("TAVILY_API_KEY"))


def is_sensitive(query: str) -> bool:
    return any(p.search(query) for p in SENSITIVE_PATTERNS)


def check_query_policy(query: str, task_private: bool) -> str:
    """Returns 'ok', 'preview' (must be shown to the owner first) or
    raises SearchDisabled/SearchError."""
    q = (query or "").strip()
    if not q:
        raise SearchError("empty search query")
    if len(q) > MAX_QUERY_LEN:
        q = q[:MAX_QUERY_LEN]
    if task_private or is_sensitive(q):
        return "preview"
    return "ok"


def contains_forbidden_text(query: str, forbidden_fragments) -> bool:
    """True if the query reproduces memory/fact/secret text. Tests use
    this to prove queries never carry private content."""
    q = query.lower()
    for frag in forbidden_fragments:
        frag = (frag or "").strip().lower()
        if len(frag) >= 12 and frag in q:
            return True
    return False


class TavilySearch:
    def __init__(self, api_key: str, base_url: str = TAVILY_BASE_URL):
        self.client = APIClient(base_url, timeout=20)
        self.api_key = api_key

    def search(self, query: str, max_results: int = 5):
        """One basic-search call (1 credit). Returns [{url,title,content}]."""
        body = {
            "query": query[:MAX_QUERY_LEN],
            "max_results": max(1, min(int(max_results), 10)),
            "search_depth": "basic",
            "include_answer": False,
            "include_raw_content": False,
        }
        try:
            _h, data = self.client.post_json(
                "/search", body,
                headers={"Authorization": f"Bearer {self.api_key}"})
        except HTTPError as e:
            if e.status == 429:
                raise ProviderRateLimit(
                    "tavily rate limit", reset_at=reset_at_iso(e.headers))
            if e.status in (401, 403):
                raise SearchError(f"tavily auth failed (HTTP {e.status})")
            raise SearchError(f"tavily HTTP {e.status}")
        except TransportError as e:
            raise ProviderUnavailable(str(e))
        out = []
        for r in (data.get("results") or [])[:10]:
            out.append({
                "url": r.get("url") or "",
                "title": r.get("title") or "",
                "content": (r.get("content") or "")[:1000],
            })
        return out


def search_service(settings, env) -> TavilySearch:
    """Build the service only when both opt-ins are present."""
    if not settings.search_enabled:
        raise SearchDisabled(
            "web search is disabled (SEARCH_ENABLED is not set)")
    key = env.get("TAVILY_API_KEY")
    if not key:
        raise SearchDisabled("web search is disabled (no TAVILY_API_KEY)")
    return TavilySearch(key)


# --- keyless DuckDuckGo HTML fallback (chat path) ---------------------------

_DDG_LINK = re.compile(
    r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_DDG_SNIP = re.compile(
    r'class="result__snippet"[^>]*>(.*?)</a>', re.S)
_TAG = re.compile(r"<[^>]+>")


def _ddg_target(href: str) -> str:
    """DDG wraps results as //duckduckgo.com/l/?uddg=<url-encoded>."""
    if "uddg=" in href:
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query)
        if qs.get("uddg"):
            return qs["uddg"][0]
    if href.startswith("//"):
        return "https:" + href
    return href


class DuckDuckGoSearch:
    """No-key web search via the DDG HTML endpoint.

    Returns the same shape as TavilySearch.search: [{url,title,content}].
    The page is fetched through the normal egress guard, so private hosts
    and non-text responses are still refused.
    """

    def __init__(self, base_url: str = "https://html.duckduckgo.com",
                 fetch_impl=None):
        self.base_url = base_url.rstrip("/")
        self._fetch = fetch_impl

    def search(self, query: str, max_results: int = 5):
        q = urllib.parse.urlencode({"q": (query or "")[:MAX_QUERY_LEN]})
        url = f"{self.base_url}/html/?{q}"
        if self._fetch:
            res = self._fetch(url)
        else:
            from .egress import guarded_get
            res = guarded_get(url)
        page = res["text"]
        links = _DDG_LINK.findall(page)
        snips = [_TAG.sub("", s).strip() for s in _DDG_SNIP.findall(page)]
        out = []
        for i, (href, title_html) in enumerate(links):
            target = _ddg_target(urllib.parse.unquote(href))
            if not target.startswith(("http://", "https://")):
                continue
            out.append({
                "url": target,
                "title": _TAG.sub("", title_html).strip()[:300],
                "content": (snips[i] if i < len(snips) else "")[:1000],
            })
            if len(out) >= max(1, min(int(max_results), 10)):
                break
        if not out:
            raise SearchError(
                "duckduckgo returned no results (possibly rate-limited)")
        return out


def chat_search_service(settings, env):
    """Search for chat turns: Tavily when configured, else keyless DDG."""
    key = env.get("TAVILY_API_KEY")
    if settings.search_enabled and key:
        return TavilySearch(key)
    return DuckDuckGoSearch(settings.ddg_base)
