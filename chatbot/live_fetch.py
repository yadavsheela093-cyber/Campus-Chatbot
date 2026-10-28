"""
live_fetch.py
Fetches LIVE content directly from the college's official website at
request time, so the chatbot can mention things that happened/changed
after this project was built (new notices, this year's event details,
etc.) instead of only using the static data baked into college_config.json
and /data.

IMPORTANT LIMITATION (learned from testing against PCTE's real site):
Modern college websites are often built with JavaScript frameworks, so a
simple HTTP fetch (what this file does) only sees the raw HTML - it CANNOT
see content that JavaScript injects into the page after loading (e.g. a
"Latest News" ticker widget on a homepage). This file works well for
pages that are plain server-rendered HTML (most individual course/info
pages tend to be), but will return little/nothing useful for JS-heavy
pages. If a page returns suspiciously little text, that's almost always
why - not a bug in this code.

For "what's the latest update" type questions in general (which often live
only on JS-rendered pages or social media), the chatbot's Gemini Google
Search Grounding (see ai.py) is actually the more reliable mechanism,
since it searches Google's already-rendered index of the site rather than
re-fetching raw HTML itself.
"""

import time
import requests

try:
    from bs4 import BeautifulSoup
    _HAS_BS4 = True
except ImportError:
    _HAS_BS4 = False

# In-memory cache so we don't hit the real website on every single chat
# message - {url: (fetched_at_timestamp, text)}
_cache = {}
CACHE_SECONDS = 30 * 60  # refetch at most every 30 minutes per URL


def fetch_live_page(url: str, max_chars: int = 1500) -> str | None:
    """
    Fetches a page right now and returns its visible text (trimmed), or
    None if it couldn't be fetched/parsed. Cached for CACHE_SECONDS so
    repeated questions don't hammer the real website.
    """
    now = time.time()
    cached = _cache.get(url)
    if cached and (now - cached[0] < CACHE_SECONDS):
        return cached[1]

    if not _HAS_BS4:
        print("[live_fetch.py] beautifulsoup4 not installed - run: pip install beautifulsoup4")
        return None

    try:
        resp = requests.get(
            url,
            timeout=8,
            headers={"User-Agent": "Mozilla/5.0 (CampusChatbot educational project)"},
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"[live_fetch.py] Could not fetch {url}: {type(e).__name__}: {e}")
        return None

    try:
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "noscript"]):
            tag.decompose()
        text = " ".join(soup.get_text(separator=" ").split())
    except Exception as e:
        print(f"[live_fetch.py] Could not parse {url}: {type(e).__name__}: {e}")
        return None

    if len(text) < 200:
        # Very little text usually means the page is JavaScript-rendered
        # and we only got an empty shell - see module docstring.
        print(f"[live_fetch.py] {url} returned very little text ({len(text)} chars) - "
              f"likely a JavaScript-rendered page that can't be scraped this way.")

    text = text[:max_chars]
    _cache[url] = (now, text)
    return text


def fetch_all_live_pages(urls: list[str]) -> str:
    """Fetches several pages and combines them into one labelled context block."""
    sections = []
    for url in urls:
        text = fetch_live_page(url)
        if text:
            sections.append(f"[LIVE from {url}, fetched just now]\n{text}")
    return "\n\n".join(sections)
