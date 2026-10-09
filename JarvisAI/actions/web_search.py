# web_search.py

import json
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from core import injection_guard


"""
Security layer to prevent prompt injection attacks.

Untrusted content fetched from the web is sanitized and wrapped before
being returned to the model's context.
"""


# ── Gemini grounding quota circuit breaker ────────────────────────────────────

# The Google Search grounding tool has its own quota, separate from normal
# generation. Once exhausted, calls return 429. After a quota error, skip
# Gemini grounding temporarily and use the DDG fallback.

_QUOTA_COOLDOWN_SEC = 900  # 15 minutes
_quota_blocked_until = 0.0
_quota_lock = threading.Lock()


def _gemini_available() -> bool:
    with _quota_lock:
        return time.monotonic() >= _quota_blocked_until


def _note_gemini_error(exc: Exception) -> None:
    """Trip the breaker when the error is a quota/rate-limit rejection."""
    global _quota_blocked_until

    msg = str(exc)

    if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
        with _quota_lock:
            already = time.monotonic() < _quota_blocked_until
            _quota_blocked_until = (
                time.monotonic() + _QUOTA_COOLDOWN_SEC
            )

        if not already:
            print(
                "[WebSearch] Gemini grounding quota exhausted — "
                f"skipping it for {_QUOTA_COOLDOWN_SEC // 60} min "
                "and serving results from DDG."
            )


class _QuotaCooldown(RuntimeError):
    """Raised instead of calling Gemini while the quota breaker is open."""


def _log_gemini_failure(context: str, exc: Exception) -> None:
    """Log a Gemini failure, except for the expected quota cooldown."""
    if isinstance(exc, _QuotaCooldown):
        return

    print(
        f"[WebSearch] ⚠️ {context} failed ({exc}) — "
        "using DDG instead"
    )


def _run_bounded(fn, timeout: float, label: str = "task"):
    """
    Run fn() in a daemon thread.

    Returns its result, or None if it exceeds the timeout.
    """
    box = [None]

    def _run():
        try:
            box[0] = fn()
        except Exception as e:
            _log_gemini_failure(label, e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout)

    if t.is_alive():
        print(
            f"[WebSearch] {label} exceeded {timeout:.0f}s — "
            "moving on"
        )

    return box[0]


# ── Paths / configuration ────────────────────────────────────────────────────


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent

    return Path(__file__).resolve().parent.parent


BASE_DIR = _get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"


def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


# ── Gemini search ────────────────────────────────────────────────────────────


def _gemini_search(query: str) -> str:
    if not _gemini_available():
        raise _QuotaCooldown(
            "Gemini grounding is in quota cooldown"
        )

    from core import gemini

    try:
        response = gemini.call(
            query,
            tier=gemini.SEARCH,
            config={
                "tools": [
                    {
                        "google_search": {}
                    }
                ]
            },
            timeout_ms=30_000,
        )

        if response is None:
            raise RuntimeError(
                "every Gemini model on the ladder failed"
            )

    except Exception as e:
        _note_gemini_error(e)
        raise

    text = ""

    for part in response.candidates[0].content.parts:
        if hasattr(part, "text") and part.text:
            text += part.text

    text = text.strip()

    if not text:
        raise ValueError(
            "Gemini returned an empty response."
        )

    return text


# ── DuckDuckGo ───────────────────────────────────────────────────────────────


def _get_ddgs():
    """
    Return the DDGS class.

    The package was renamed from duckduckgo-search to ddgs.
    """
    try:
        from ddgs import DDGS

        return DDGS

    except ImportError:
        from duckduckgo_search import DDGS

        print(
            "[WebSearch] ⚠️ Using the deprecated "
            "'duckduckgo-search' package — "
            "fix with: pip install -U ddgs"
        )

        return DDGS


def _ddg_search(
    query: str,
    max_results: int = 6,
) -> list[dict]:
    DDGS = _get_ddgs()
    results = []

    try:
        with DDGS() as ddgs:
            for r in ddgs.text(
                query,
                max_results=max_results,
            ):
                results.append(
                    {
                        "title": r.get("title", ""),
                        "snippet": r.get("body", ""),
                        "url": r.get("href", ""),
                    }
                )

    except Exception as e:
        print(
            f"[WebSearch] ⚠️ DDG text() failed: {e}"
        )

    return results


def _ddg_images(query: str, max_results: int = 12) -> list[dict]:
    """Return a small, display-only image gallery payload from DDGS."""
    DDGS = _get_ddgs()
    with DDGS() as ddgs:
        results = list(ddgs.images(query, max_results=max_results))
    out = []
    for row in results:
        image_url = str(row.get("image") or row.get("thumbnail") or "").strip()
        if not image_url.startswith("https://"):
            continue
        out.append({
            "url": image_url,
            "title": str(row.get("title") or row.get("source") or "Image result")[:120],
        })
        if len(out) >= max_results:
            break
    return out


def _ddg_news(
    query: str,
    max_results: int = 8,
) -> list[dict]:
    """DDG news search — returns actual articles."""
    DDGS = _get_ddgs()
    results = []

    try:
        with DDGS() as ddgs:
            for r in ddgs.news(
                query,
                max_results=max_results,
            ):
                results.append(
                    {
                        "title": r.get("title", ""),
                        "snippet": r.get("body", ""),
                        "url": r.get("url", ""),
                        "source": r.get("source", ""),
                    }
                )

    except Exception as e:
        print(
            f"[WebSearch] ⚠️ DDG news() failed ({e}) "
            "— falling back to text search"
        )

    # Covers the legacy package case where news() returns no results.
    if not results:
        results = _ddg_search(
            query,
            max_results=max_results,
        )

    return results


# ── Sri Lankan publisher news ────────────────────────────────────────────────

_SRI_LANKA_NEWS_SOURCES = (
    {
        "label": "Newsfirst — English",
        "query": "site:newsfirst.lk/latest Sri Lanka latest news",
        "fallback_query": "Newsfirst Sri Lanka latest news English",
        "hosts": {"newsfirst.lk", "www.newsfirst.lk", "english.newsfirst.lk"},
    },
    {
        "label": "Newsfirst — Sinhala",
        # Newsfirst now serves Sinhala headlines on /sinhala/latest on the
        # main site, while the language subdomain remains a valid edition.
        "query": "site:newsfirst.lk/sinhala/latest Sri Lanka Sinhala latest news",
        "fallback_query": "Newsfirst Sinhala ශ්‍රී ලංකා පුවත් latest",
        "hosts": {"newsfirst.lk", "www.newsfirst.lk", "sinhala.newsfirst.lk"},
    },
    {
        "label": "Ada Derana — English",
        "query": "site:adaderana.lk/categories/latest Sri Lanka latest news",
        "fallback_query": "Ada Derana Sri Lanka latest news English",
        "hosts": {"adaderana.lk", "www.adaderana.lk"},
    },
    {
        "label": "Ada Derana — Sinhala",
        "query": "site:sinhala.adaderana.lk Sri Lanka Sinhala latest news",
        "fallback_query": "Ada Derana Sinhala latest Sri Lanka news",
        "hosts": {"sinhala.adaderana.lk"},
    },
)


def _sri_lanka_news(_query: str = "") -> str:
    """Return recent headlines from both language editions of two local outlets."""
    sections = []
    seen_urls = set()

    # Search the four editions concurrently. A shared deadline keeps the
    # morning briefing from waiting four times for the search provider.
    source_results: list[list[tuple[str, str, str]] | None] = [None] * len(_SRI_LANKA_NEWS_SOURCES)

    def _publisher_items(source: dict, results: list[dict]) -> list[tuple[str, str, str]]:
        accepted = []
        for result in results:
            url = str(result.get("url") or "").strip()
            try:
                parsed = urlparse(url)
                host = (parsed.hostname or "").lower().rstrip(".")
                port = parsed.port
            except ValueError:
                # Ignore malformed provider links without discarding other
                # valid results returned for this publisher.
                continue
            path_prefix = source.get("path_prefix")
            if (parsed.scheme != "https" or host not in source["hosts"]
                    or parsed.username or parsed.password or port not in (None, 443)
                    or (path_prefix and not parsed.path.startswith(path_prefix))):
                continue
            title = " ".join(str(result.get("title") or "").split())
            if not title:
                continue
            snippet = " ".join(str(result.get("snippet") or "").split())
            accepted.append((title, snippet, url))
            if len(accepted) >= 3:
                break
        return accepted

    def _fetch_source(index: int, source: dict) -> None:
        try:
            # News search tends to return fresher article links than general
            # text search. If its results are empty or point off-publisher,
            # retry with text search and still enforce the exact publisher host.
            results = _ddg_news(source["query"], max_results=8)
            accepted = _publisher_items(source, results)
            if not accepted and source.get("fallback_query"):
                results = _ddg_search(source["fallback_query"], max_results=8)
                accepted = _publisher_items(source, results)
            source_results[index] = accepted
        except Exception as e:
            print(f"[WebSearch] Sri Lankan source search failed: {e}")
            source_results[index] = []

    workers = [
        threading.Thread(
            target=_fetch_source,
            args=(index, source),
            daemon=True,
        )
        for index, source in enumerate(_SRI_LANKA_NEWS_SOURCES)
    ]
    for worker in workers:
        worker.start()

    # Search providers can be slow to establish a connection on a cold start.
    # Keep the four editions parallel, but give each one enough time to return
    # instead of discarding all results after a short shared deadline.
    # Leave a little room for each worker's single fallback query before the
    # outer news-fetch timeout closes the attempt.
    deadline = time.monotonic() + 17.0
    for worker in workers:
        worker.join(max(0.0, deadline - time.monotonic()))

    for index, source in enumerate(_SRI_LANKA_NEWS_SOURCES):
        accepted = []
        for title, snippet, url in (source_results[index] or []):
            if url in seen_urls:
                continue
            seen_urls.add(url)
            accepted.append((title, snippet, url))

        if accepted:
            lines = [source["label"]]
            for title, snippet, url in accepted:
                lines.append(f"• {title}")
                if snippet:
                    lines.append(f"  {snippet[:220]}")
                lines.append(f"  Source: {url}")
            sections.append("\n".join(lines))
        else:
            sections.append(f"{source['label']}: no headlines found right now.")

    if not seen_urls:
        return "No Sri Lankan headlines were found from Newsfirst or Ada Derana right now."

    return (
        "Sri Lankan local news — Newsfirst and Ada Derana, English and Sinhala\n\n"
        + "\n\n".join(sections)
    )


def _is_sri_lanka_news_request(query: str) -> bool:
    q = (query or "").casefold()
    return any(term in q for term in (
        "sri lanka", "sri lankan", "srilanka", "local news",
        "newsfirst", "news first", "ada derana", "sinhalese news",
        "sinhala news", "ශ්‍රී ලංකා", "පුවත්",
    ))


# ── Formatting ───────────────────────────────────────────────────────────────


def _format_ddg(
    query: str,
    results: list[dict],
) -> str:
    if not results:
        return f"No results found for: {query}"

    lines = [
        f"Search results for: {query}\n"
    ]

    for i, r in enumerate(results, 1):
        if r.get("title"):
            lines.append(
                f"{i}. {r['title']}"
            )

        if r.get("snippet"):
            lines.append(
                f"   {r['snippet']}"
            )

        if r.get("url"):
            lines.append(
                f"   Source: {r['url']}"
            )

        lines.append("")

    return "\n".join(lines).strip()


def _format_news(
    query: str,
    results: list[dict],
) -> str:
    if not results:
        return f"No news found for: {query}"

    lines = [
        f"Latest news: {query}\n"
    ]

    for i, r in enumerate(results, 1):
        title = r.get("title", "")

        if not title:
            continue

        src = (
            f"  [{r['source']}]"
            if r.get("source")
            else ""
        )

        lines.append(
            f"{i}. {title}{src}"
        )

        if r.get("snippet"):
            lines.append(
                f"   {r['snippet'][:140]}"
            )

        if r.get("url"):
            lines.append(
                f"   {r['url']}"
            )

        lines.append("")

    return "\n".join(lines).strip()


# ── Briefing helper ──────────────────────────────────────────────────────────


def _gemini_headlines(
    n: int = 5,
) -> tuple[list[str], str]:
    """
    Fetch current headlines via Gemini grounded search.

    Returns:
        (headline_list, raw_text_for_display)
    """
    import re

    from core import gemini

    response = gemini.call(
        f"Current world news: {n} headlines. "
        "Numbered list, titles only.",
        tier=gemini.SEARCH,
        config={
            "tools": [
                {
                    "google_search": {}
                }
            ]
        },
        timeout_ms=30_000,
    )

    if response is None:
        return [], ""

    raw = ""

    for part in response.candidates[0].content.parts:
        if hasattr(part, "text") and part.text:
            raw += part.text

    headlines = []

    for line in raw.strip().split("\n"):
        line = line.strip()

        if not line:
            continue

        # Accept lines beginning with a number:
        # 1. headline
        # 1) headline
        # 1- headline
        if not re.match(r"^\d+[.\)\-]\s*", line):
            continue

        clean = re.sub(
            r"^\d+[.\)\-]\s*",
            "",
            line,
        )

        clean = re.sub(
            r"^[*#]+\s*",
            "",
            clean,
        ).strip()

        if clean and len(clean) > 10:
            headlines.append(clean)

    return headlines[:n], raw.strip()


# ── Search modes ─────────────────────────────────────────────────────────────


def _search(query: str) -> str:
    """Default search — Gemini grounded, DDG fallback."""
    try:
        return _gemini_search(query)

    except Exception as e:
        _log_gemini_failure(
            "Gemini search",
            e,
        )

        results = _ddg_search(query)

        return _format_ddg(
            query,
            results,
        )


def _news(query: str) -> str:
    """
    DDG first, Gemini as backup.

    DDG is preferred for fast headline retrieval.
    Gemini is only used when DDG returns insufficient results.
    """
    if _is_sri_lanka_news_request(query):
        local_news = _run_bounded(
            lambda: _sri_lanka_news(query),
            timeout=18.0,
            label="Sri Lankan news",
        )
        if local_news and not local_news.startswith("No Sri Lankan headlines"):
            return local_news
        return local_news or "Sri Lankan news could not be fetched right now."

    gemini_query = (
        f"latest news today: {query}"
        if query
        else "top world news today"
    )

    ddg_query = (
        query
        if query
        else "world news today"
    )

    def _ddg_attempt() -> str:
        return _format_news(
            ddg_query,
            _ddg_news(
                ddg_query,
                max_results=8,
            ),
        )

    text = _run_bounded(
        _ddg_attempt,
        timeout=5.0,
        label="DDG news",
    )

    if (
        text
        and len(text) > 60
        and not text.startswith("No news found")
    ):
        return text

    text = _run_bounded(
        lambda: _gemini_search(gemini_query),
        timeout=6.0,
        label="Gemini news",
    )

    if text and len(text) > 60:
        return text

    return f"No news found for: {query}"


def _research(query: str) -> str:
    """
    Deep dive — asks Gemini for a comprehensive answer.

    Falls back to a wider DDG fetch.
    """
    research_query = (
        f"Comprehensive, detailed explanation of: {query}. "
        "Include background context, key facts, current state, "
        "and important nuances."
    )

    try:
        return _gemini_search(
            research_query
        )

    except Exception as e:
        _log_gemini_failure(
            "Gemini research",
            e,
        )

        results = _ddg_search(
            query,
            max_results=10,
        )

        return _format_ddg(
            query,
            results,
        )


def _price(query: str) -> str:
    """Product price lookup."""
    price_query = (
        f"current price of {query} — "
        "how much does it cost today"
    )

    try:
        return _gemini_search(
            price_query
        )

    except Exception as e:
        _log_gemini_failure(
            "Gemini price",
            e,
        )

        results = _ddg_search(
            f"{query} price buy",
            max_results=6,
        )

        return _format_ddg(
            query,
            results,
        )


def _compare(
    items: list[str],
    aspect: str,
) -> str:
    query = (
        f"Compare {', '.join(items)} "
        f"in terms of {aspect}. "
        "Give specific facts and data."
    )

    try:
        return _gemini_search(query)

    except Exception as e:
        _log_gemini_failure(
            "Gemini compare",
            e,
        )

    all_results: dict[str, list] = {}

    for item in items:
        try:
            all_results[item] = _ddg_search(
                f"{item} {aspect}",
                max_results=3,
            )

        except Exception:
            all_results[item] = []

    lines = [
        f"Comparison — {aspect.upper()}",
        "─" * 40,
    ]

    for item in items:
        lines.append(f"\n▸ {item}")

        for r in all_results.get(item, [])[:2]:
            if r.get("snippet"):
                lines.append(
                    f"  • {r['snippet']}"
                )

            if r.get("url"):
                lines.append(
                    f"    {r['url']}"
                )

    return "\n".join(lines)


# ── Public entry point ───────────────────────────────────────────────────────


def web_search(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params = parameters or {}

    query = str(
        params.get("query", "")
    ).strip()

    mode = str(
        params.get("mode", "search")
    ).lower().strip()

    items = params.get("items", [])

    if not isinstance(items, list):
        items = []

    aspect = str(
        params.get("aspect", "general")
    ).strip() or "general"

    if not query and not items:
        return "Please provide a search query."

    if items and mode != "compare":
        mode = "compare"

    if player:
        try:
            player.write_log(
                f"[Search:{mode}] "
                f"{query or ', '.join(map(str, items))}"
            )
        except Exception:
            pass

    print(
        f"[WebSearch] 🔍 mode={mode!r} "
        f"query={query!r}"
    )

    try:
        if mode == "compare" and items:
            raw = _compare(
                items,
                aspect,
            )

        elif mode == "news":
            raw = _news(query)

        elif mode == "research":
            raw = _research(query)

        elif mode == "price":
            raw = _price(query)

        elif mode in ("image", "images"):
            images = _ddg_images(query, max_results=12)
            if player and images and hasattr(player, "show_images"):
                player.show_images(images, query)
            raw = (
                f"Found {len(images)} image results for {query}. "
                "The thumbnails are displayed in the JARVIS HUD."
                if images else f"No image results were found for {query}."
            )

        else:
            raw = _search(query)

        if mode == "news" and player and hasattr(player, "show_news_cards"):
            player.show_news_cards(raw, f"HEADLINES / {query}")
        elif mode == "search" and player and hasattr(player, "show_news_cards") and "http" in raw.lower():
            player.show_news_cards(raw, f"SEARCH RESULTS / {query}")
        elif mode == "price" and player and hasattr(player, "show_market_chart"):
            player.show_market_chart(query)

        return injection_guard.wrap_untrusted(
            raw,
            source="web_search",
        )

    except Exception as e:
        print(
            f"[WebSearch] ❌ All backends failed: {e}"
        )

        return f"Search failed: {e}"


# ── Tool declaration ─────────────────────────────────────────────────────────

TOOL = {
    "name": "web_search",
    "description": (
        "Searches the web. Use for ANY question about current facts, "
        "events, prices, or topics — always prefer this over guessing. "
        "Modes: 'search' (default), 'news' (latest headlines on a topic), "
        "For Sri Lankan local news, use news mode and return headlines from "
        "Newsfirst and Ada Derana in both English and Sinhala when available. "
        "'research' (deep comprehensive answer), 'price' (product cost "
        "lookup), 'images' (show a thumbnail gallery), 'compare' (side-by-side "
        "comparison of items). Search and news results appear as HUD cards."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "query": {
                "type": "STRING",
                "description": "Search query or topic",
            },
            "mode": {
                "type": "STRING",
                "description": (
                    "search | news | research | price | images | compare"
                ),
            },
            "items": {
                "type": "ARRAY",
                "items": {
                    "type": "STRING",
                },
                "description": (
                    "Items to compare (compare mode)"
                ),
            },
            "aspect": {
                "type": "STRING",
                "description": (
                    "Comparison aspect: "
                    "price | specs | reviews | features"
                ),
            },
        },
        "required": [
            "query",
        ],
    },
    "handler": web_search,
}
