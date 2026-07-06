"""JU website search — the tool behind the chatbot's web-search agent.

Discovers pages on the official University of Jordan domain (``ju.edu.jo`` and
its subdomains) via DuckDuckGo's keyless HTML endpoint, fetches the top results,
and extracts readable text for the model. Everything is hard-scoped to the
university domain so the assistant only ever cites official pages.
"""

from __future__ import annotations

import html
import logging
import re
import threading
import time
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from ..config import settings

log = logging.getLogger(__name__)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 JU-Assistant/1.0")
_DDG_ENDPOINTS = ["https://html.duckduckgo.com/html/", "https://lite.duckduckgo.com/lite/"]

_cache: dict[str, tuple[float, str]] = {}
_lock = threading.Lock()
_CACHE_TTL = 1800  # seconds


# --------------------------------------------------------------------------- #
#  Domain scoping                                                             #
# --------------------------------------------------------------------------- #
def _domains() -> list[str]:
    """Allowed domains (comma-separated in WEB_SEARCH_DOMAIN). Each entry also
    matches its subdomains (e.g. ju.edu.jo -> engineering.ju.edu.jo)."""
    return [d.strip().lower() for d in settings.WEB_SEARCH_DOMAIN.split(",") if d.strip()]


def _in_domain(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == dom or host.endswith("." + dom) for dom in _domains())


def _resolve_ddg(href: str) -> str | None:
    """DuckDuckGo wraps results as //duckduckgo.com/l/?uddg=<encoded>."""
    href = html.unescape(href)
    if "uddg=" in href:
        qs = parse_qs(urlparse(href).query)
        if qs.get("uddg"):
            return unquote(qs["uddg"][0])
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("http"):
        return href
    return None


# --------------------------------------------------------------------------- #
#  Search + fetch                                                             #
# --------------------------------------------------------------------------- #
def search_urls(query: str, k: int) -> list[dict]:
    q = f"site:{settings.WEB_SEARCH_DOMAIN} {query}".strip()
    body = None
    for endpoint in _DDG_ENDPOINTS:
        try:
            r = httpx.post(endpoint, data={"q": q}, headers={"User-Agent": _UA},
                           timeout=settings.WEB_SEARCH_TIMEOUT_S, follow_redirects=True)
            if r.status_code == 200 and r.text:
                body = r.text
                break
        except Exception as exc:  # noqa: BLE001
            log.warning("DDG search failed on %s: %s", endpoint, exc)
    if not body:
        return []

    results: list[dict] = []
    seen: set[str] = set()
    # Primary: titled result anchors.
    for m in re.finditer(r'<a\b[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                         body, re.S | re.I):
        url = _resolve_ddg(m.group(1))
        title = html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        if url and _in_domain(url) and url not in seen:
            seen.add(url)
            results.append({"url": url, "title": title or url})
        if len(results) >= k:
            return results
    # Fallback: any in-domain links in the page.
    for m in re.finditer(r'https?://[^\s"\'<>]+', body):
        url = _resolve_ddg(m.group(0)) or m.group(0)
        if url and _in_domain(url) and url not in seen and "duckduckgo" not in url:
            seen.add(url)
            results.append({"url": url, "title": url})
        if len(results) >= k:
            break
    return results


_DROP = re.compile(r"(?is)<(script|style|head|nav|footer|svg|noscript|form)\b[^>]*>.*?</\1>")
_TAGS = re.compile(r"(?s)<[^>]+>")
_COMMENTS = re.compile(r"(?is)<!--.*?-->")


def _html_to_text(raw: str) -> str:
    raw = _COMMENTS.sub(" ", raw)
    raw = _DROP.sub(" ", raw)
    raw = _TAGS.sub(" ", raw)
    raw = html.unescape(raw)
    raw = re.sub(r"[ \t\r\f ​]+", " ", raw)
    raw = re.sub(r"\n\s*\n+", "\n", raw)
    return raw.strip()


def fetch_text(url: str, max_chars: int) -> str:
    now = time.time()
    with _lock:
        cached = _cache.get(url)
        if cached and now - cached[0] < _CACHE_TTL:
            return cached[1]
    text = ""
    try:
        r = httpx.get(url, headers={"User-Agent": _UA},
                      timeout=settings.WEB_SEARCH_TIMEOUT_S, follow_redirects=True)
        if r.status_code == 200 and "html" in r.headers.get("content-type", "").lower():
            text = _html_to_text(r.text)[:max_chars]
    except Exception as exc:  # noqa: BLE001
        log.warning("Fetch failed %s: %s", url, exc)
    with _lock:
        _cache[url] = (now, text)
    return text


def _annotation_citation(ann):
    """Pull the url_citation out of an SDK or dict annotation."""
    cit = getattr(ann, "url_citation", None)
    if cit is None and isinstance(ann, dict):
        cit = ann.get("url_citation")
    if cit is None:
        return None
    get = (lambda k: getattr(cit, k, None)) if not isinstance(cit, dict) else cit.get
    return {"url": get("url") or "", "title": (get("title") or "").strip(),
            "content": get("content") or ""}


def _plugin_search(query: str) -> tuple[list[str], list[dict]]:
    """Primary backend: OpenRouter's web plugin (Exa), scoped to the JU domain."""
    from ..core import tokens
    from ..core.llm import get_client

    dom = settings.WEB_SEARCH_DOMAIN
    try:
        resp = get_client().chat.completions.create(
            model=settings.CHAT_MODEL,
            messages=[{"role": "user", "content":
                       f"Find official University of Jordan pages on {dom} about: {query}"}],
            max_tokens=16,
            extra_body={"plugins": [{"id": "web", "max_results": settings.WEB_SEARCH_RESULTS}],
                        "usage": {"include": True}},
            timeout=float(settings.WEB_SEARCH_TIMEOUT_S) + 20,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Web-plugin search failed: %s", exc)
        return [], []

    usage = getattr(resp, "usage", None)
    tokens.record(settings.CHAT_MODEL, "web_search", usage, cost_usd=tokens.usage_cost(usage))

    anns = getattr(resp.choices[0].message, "annotations", None) or []
    blocks, sources = [], []
    for ann in anns:
        cit = _annotation_citation(ann)
        if not cit or not cit["url"] or not _in_domain(cit["url"]):
            continue
        content = cit["content"][:settings.WEB_SEARCH_PAGE_CHARS]
        if len(content) < 80:
            continue
        title = cit["title"] or cit["url"]
        sources.append({"url": cit["url"], "title": title})
        blocks.append(f"[{len(blocks) + 1}] {title}\nURL: {cit['url']}\n{content}")
    return blocks, sources


def _tavily_search(query: str) -> tuple[list[str], list[dict]]:
    """Tavily search API: domain-restricted retrieval that also SYNTHESIZES a
    direct answer from the found JU pages (the biggest quality win), returns the
    most relevant chunks per page, and cleanly extracts JS-rendered pages that a
    raw fetch can't read. Requires ``TAVILY_API_KEY``.
    """
    key = settings.TAVILY_API_KEY
    if not key:
        return [], []
    payload = {
        "query": query,
        "search_depth": settings.TAVILY_SEARCH_DEPTH,
        "include_domains": _domains(),
        "max_results": settings.WEB_SEARCH_RESULTS,
        "include_raw_content": True,
        "chunks_per_source": max(1, settings.TAVILY_CHUNKS_PER_SOURCE),
    }
    if settings.WEB_SEARCH_INCLUDE_ANSWER:
        payload["include_answer"] = "advanced"
    try:
        r = httpx.post(
            "https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload, timeout=settings.WEB_SEARCH_TIMEOUT_S + 15,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("Tavily search failed: %s", exc)
        return [], []

    blocks, sources = [], []
    answer = (data.get("answer") or "").strip()
    if answer:
        blocks.append("SUMMARY (synthesized by the search tool from official JU "
                      f"pages — verify against the sources below):\n{answer}")
    for item in data.get("results", []):
        url = item.get("url", "")
        if not url or not _in_domain(url):
            continue
        content = (item.get("raw_content") or item.get("content") or "")[:settings.WEB_SEARCH_PAGE_CHARS]
        title = (item.get("title") or url).strip()
        sources.append({"url": url, "title": title})
        if len(content) >= 60:
            blocks.append(f"[{len(sources)}] {title}\nURL: {url}\n{content}")
    return blocks, sources


def _tavily_extract(urls: list[str]) -> dict[str, str]:
    """Pull clean content from specific URLs via Tavily /extract.

    JU pages serve a near-empty JS shell to a raw GET, so when another provider
    (Serper) finds the right pages we still need Tavily to read them.
    """
    key = settings.TAVILY_API_KEY
    if not key or not urls:
        return {}
    try:
        r = httpx.post(
            "https://api.tavily.com/extract",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"urls": urls[:8], "extract_depth": "advanced"},
            timeout=settings.WEB_SEARCH_TIMEOUT_S + 20,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("Tavily extract failed: %s", exc)
        return {}
    out: dict[str, str] = {}
    for it in data.get("results", []):
        u, c = it.get("url"), (it.get("raw_content") or "")
        if u and c:
            out[u] = c
    return out


def _serper_search(query: str) -> tuple[list[str], list[dict]]:
    """Google Search via Serper.dev, scoped to the JU site — the best ranking and
    coverage for a known site. Finds the right pages; content is read with Tavily
    /extract (JU pages are JS shells to a raw GET), falling back to Google's
    snippets. Requires ``SERPER_API_KEY``.
    """
    key = settings.SERPER_API_KEY
    if not key:
        return [], []
    primary = (_domains() or ["ju.edu.jo"])[0]
    try:
        r = httpx.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": key, "Content-Type": "application/json"},
            json={"q": f"{query} site:{primary}", "gl": settings.SERPER_GL,
                  "hl": settings.SERPER_HL, "num": max(10, settings.WEB_SEARCH_RESULTS)},
            timeout=settings.WEB_SEARCH_TIMEOUT_S + 10,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("Serper search failed: %s", exc)
        return [], []

    picks, seen = [], set()
    for it in (data.get("organic") or []):
        url = it.get("link", "")
        if url and _in_domain(url) and url not in seen:
            seen.add(url)
            picks.append({"url": url, "title": (it.get("title") or url).strip(),
                          "snippet": it.get("snippet") or ""})
        if len(picks) >= settings.WEB_SEARCH_RESULTS:
            break
    if not picks:
        return [], []

    extracted = _tavily_extract([p["url"] for p in picks])  # {} if no Tavily key
    blocks, sources = [], []
    ab = data.get("answerBox") or {}
    summary = (ab.get("answer") or ab.get("snippet") or "").strip()
    if summary:
        blocks.append(f"SUMMARY (Google answer box for an official JU page):\n{summary}")
    for p in picks:
        content = (extracted.get(p["url"]) or p["snippet"] or "")[:settings.WEB_SEARCH_PAGE_CHARS]
        sources.append({"url": p["url"], "title": p["title"]})
        if len(content) >= 40:
            blocks.append(f"[{len(sources)}] {p['title']}\nURL: {p['url']}\n{content}")
    return blocks, sources


def _ddg_search(query: str) -> tuple[list[str], list[dict]]:
    """Fallback backend: keyless DuckDuckGo discovery + direct fetch."""
    blocks, sources = [], []
    for item in search_urls(query, settings.WEB_SEARCH_RESULTS):
        text = fetch_text(item["url"], settings.WEB_SEARCH_PAGE_CHARS)
        if len(text) < 80:
            continue
        sources.append(item)
        blocks.append(f"[{len(blocks) + 1}] {item['title']}\nURL: {item['url']}\n{text}")
    return blocks, sources


_PROVIDERS = {"serper": _serper_search, "tavily": _tavily_search,
              "openrouter": _plugin_search, "duckduckgo": _ddg_search}


def _has_key(name: str) -> bool:
    if name == "serper":
        return bool(settings.SERPER_API_KEY)
    if name == "tavily":
        return bool(settings.TAVILY_API_KEY)
    return True


def _provider_order() -> list[str]:
    """Provider try-order: chosen provider first, then the others as fallbacks.

    Auto: Serper (Google, best ranking) -> Tavily (answer synthesis + extract)
    -> OpenRouter web plugin -> DuckDuckGo. Key-less providers are skipped.
    """
    p = settings.WEB_SEARCH_PROVIDER.lower().strip()
    if p in _PROVIDERS:
        order = [p]
    else:  # auto
        order = []
        if settings.SERPER_API_KEY:
            order.append("serper")
        if settings.TAVILY_API_KEY:
            order.append("tavily")
        order.append("openrouter")
    for fb in ("serper", "tavily", "openrouter", "duckduckgo"):
        if fb not in order and _has_key(fb):
            order.append(fb)
    return order


def run_search(query: str) -> tuple[str, list[dict]]:
    """Search the JU site via the configured provider (with fallbacks) and
    return ``(text_for_model, sources)`` where sources is ``[{"url", "title"}]``."""
    query = (query or "").strip()
    if not query:
        return "Empty query.", []

    for name in _provider_order():
        blocks, sources = _PROVIDERS[name](query)
        if blocks:
            joined = "\n\n---\n\n".join(blocks)[:settings.WEB_SEARCH_RESULT_CHARS]
            return joined, sources
    return (f"No usable results were found on the {settings.WEB_SEARCH_DOMAIN} "
            "website for this query."), []
