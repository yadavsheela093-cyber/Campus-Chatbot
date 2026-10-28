"""
site_crawler.py
Crawls the college's OFFICIAL website and saves every readable page (and
any PDFs it links to, like prospectus / fee structure) into a local cache
file: data/site_cache.json. The RAG engine (rag.py) then loads that cache,
so the chatbot answers questions FROM THE OFFICIAL SITE.

How pages are discovered (in order):
1. Sitemaps (robots.txt "Sitemap:" lines, /sitemap.xml, /wp-sitemap.xml ...)
2. The seed URLs in college_config.json ("official_site" + "live_pages")
3. Links found on every page fetched (same website only), up to a limit

Good-citizen rules built in: obeys robots.txt, identifies itself, waits
between requests, caps the number of pages/PDFs, never leaves the college's
own domain.

HONEST LIMITATION: pages that are built entirely by JavaScript (the PCTE
homepage is one) return an empty HTML shell to any simple crawler - those
pages are skipped automatically (less than MIN_TEXT_CHARS of text). For
those, the chatbot relies on Gemini's Google Search grounding instead,
because Google's index already contains the rendered content.
"""

import hashlib
import io
import json
import os
import threading
import time
import xml.etree.ElementTree as ET
from collections import deque
from urllib.parse import urljoin, urlparse, urldefrag
from urllib.robotparser import RobotFileParser

import requests

try:
    from bs4 import BeautifulSoup
    BS4_OK = True
except ImportError:
    BS4_OK = False

try:
    from pypdf import PdfReader
    PYPDF_OK = True
except ImportError:
    PYPDF_OK = False

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
CACHE_PATH = os.path.join(BASE_DIR, "data", "site_cache.json")
LOCK_PATH = os.path.join(BASE_DIR, "data", ".crawl.lock")

USER_AGENT = "Mozilla/5.0 (CampusChatbot educational project; polite crawler)"
MAX_PAGES = 150
MAX_PDFS = 15
MAX_PDF_BYTES = 8 * 1024 * 1024
MIN_TEXT_CHARS = 250
MAX_TEXT_PER_PAGE = 20000
REQUEST_DELAY = 0.4
TIMEOUT = 12

SKIP_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico", ".css", ".js",
    ".zip", ".rar", ".mp4", ".mp3", ".avi", ".doc", ".docx", ".xls", ".xlsx",
    ".ppt", ".pptx", ".woff", ".woff2", ".ttf",
)
SKIP_PATH_PARTS = ("wp-json", "wp-admin", "wp-login", "xmlrpc", "/feed", "/cart", "/checkout")

_status = {"running": False, "last_error": None}


# ----------------------------------------------------------------- helpers
def _clean_url(url):
    url, _ = urldefrag(url.strip())
    # drop query strings (tracking params etc.) so the same page isn't crawled twice
    parsed = urlparse(url)._replace(query="")
    path = parsed.path if parsed.path in ("", "/") else parsed.path.rstrip("/")
    return parsed._replace(path=path).geturl()


def _allowed_host(url, root_domain):
    host = (urlparse(url).hostname or "").lower()
    return host == root_domain or host.endswith("." + root_domain)


def _root_domain(site_url):
    host = (urlparse(site_url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _get(url, session, stream=False):
    return session.get(url, timeout=TIMEOUT, stream=stream,
                       headers={"User-Agent": USER_AGENT})


def _load_robots(site_url, session):
    rp = RobotFileParser()
    sitemaps = []
    try:
        resp = _get(urljoin(site_url, "/robots.txt"), session)
        if resp.status_code == 200:
            lines = resp.text.splitlines()
            rp.parse(lines)
            for line in lines:
                if line.lower().startswith("sitemap:"):
                    sitemaps.append(line.split(":", 1)[1].strip())
        else:
            rp.parse([])
    except Exception:
        rp.parse([])
    return rp, sitemaps


def _sitemap_urls(sitemap_url, session, depth=0):
    """Returns page URLs listed in a sitemap (follows nested sitemap indexes)."""
    if depth > 2:
        return []
    try:
        resp = _get(sitemap_url, session)
        if resp.status_code != 200:
            return []
        root = ET.fromstring(resp.content)
    except Exception:
        return []

    urls = []
    for el in root.iter():
        if el.tag.endswith("}loc") or el.tag == "loc":
            loc = (el.text or "").strip()
            if not loc:
                continue
            if loc.lower().endswith(".xml"):
                urls.extend(_sitemap_urls(loc, session, depth + 1))
            else:
                urls.append(loc)
    return urls


def _extract_page(html, url):
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
    links = [a["href"] for a in soup.find_all("a", href=True)]

    for tag in soup(["script", "style", "noscript", "nav", "footer", "header",
                     "form", "svg", "iframe"]):
        tag.decompose()

    body = soup.find("main") or soup.find("article") or soup.body or soup
    text = " ".join(body.get_text(separator=" ").split())
    return title, text[:MAX_TEXT_PER_PAGE], links


def _pdf_to_text(content):
    if not PYPDF_OK:
        return ""
    try:
        reader = PdfReader(io.BytesIO(content))
        parts = []
        for page in reader.pages[:40]:
            parts.append(page.extract_text() or "")
        return " ".join(" ".join(parts).split())[:MAX_TEXT_PER_PAGE]
    except Exception:
        return ""


# ------------------------------------------------------------------ crawl
def crawl_site(site_url, extra_seeds=None, max_pages=MAX_PAGES, max_pdfs=MAX_PDFS):
    """Crawls the site and returns {'crawled_at', 'site', 'pages': [...]}."""
    if not BS4_OK:
        raise RuntimeError("beautifulsoup4 is not installed - run: pip install beautifulsoup4")

    site_url = site_url.rstrip("/")
    root_domain = _root_domain(site_url)
    session = requests.Session()
    robots, robots_sitemaps = _load_robots(site_url, session)

    queue = deque()
    seen = set()

    def enqueue(u):
        u = _clean_url(urljoin(site_url + "/", u))
        if u in seen or not u.startswith("http"):
            return
        if not _allowed_host(u, root_domain):
            return
        low = u.lower()
        if any(p in low for p in SKIP_PATH_PARTS):
            return
        if low.endswith(SKIP_EXTENSIONS):
            return
        seen.add(u)
        queue.append(u)

    # 1) sitemaps
    sitemap_candidates = list(robots_sitemaps) + [
        urljoin(site_url, p) for p in
        ("/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml", "/sitemap-index.xml")
    ]
    tried = set()
    for sm in sitemap_candidates:
        if sm in tried:
            continue
        tried.add(sm)
        for u in _sitemap_urls(sm, session):
            enqueue(u)

    # 2) seeds
    enqueue(site_url)
    for s in (extra_seeds or []):
        enqueue(s)

    pages, seen_hashes = [], set()
    pdf_count = 0

    while queue and len(pages) < max_pages:
        url = queue.popleft()
        if not robots.can_fetch(USER_AGENT, url):
            continue

        try:
            time.sleep(REQUEST_DELAY)
            is_pdf = url.lower().endswith(".pdf")

            if is_pdf:
                if pdf_count >= max_pdfs:
                    continue
                resp = _get(url, session, stream=True)
                if resp.status_code != 200:
                    continue
                content = resp.raw.read(MAX_PDF_BYTES + 1, decode_content=True)
                if len(content) > MAX_PDF_BYTES:
                    continue
                text, title, links = _pdf_to_text(content), url.rsplit("/", 1)[-1], []
                pdf_count += 1
            else:
                resp = _get(url, session)
                ctype = resp.headers.get("Content-Type", "")
                if resp.status_code != 200 or "html" not in ctype.lower():
                    continue
                title, text, links = _extract_page(resp.text, url)
        except Exception as e:
            print(f"[site_crawler] skip {url}: {type(e).__name__}")
            continue

        for href in links:
            enqueue(urljoin(url, href))

        if len(text) < MIN_TEXT_CHARS:
            continue  # empty shell (JavaScript-rendered) or no real content

        digest = hashlib.md5(text.encode("utf-8", "ignore")).hexdigest()
        if digest in seen_hashes:
            continue
        seen_hashes.add(digest)

        pages.append({"url": url, "title": title, "text": text})
        print(f"[site_crawler] saved {len(pages):>3}: {url}")

    return {"crawled_at": time.time(), "site": site_url, "pages": pages}


# ------------------------------------------------------- cache management
def load_cache():
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"crawled_at": 0, "site": "", "pages": []}


def cache_mtime():
    try:
        return os.path.getmtime(CACHE_PATH)
    except OSError:
        return 0


def get_status():
    cache = load_cache()
    return {
        "running": _status["running"],
        "pages_cached": len(cache.get("pages", [])),
        "crawled_at": cache.get("crawled_at", 0),
        "site": cache.get("site", ""),
        "last_error": _status["last_error"],
    }


def _acquire_lock():
    """File lock so only ONE process crawls at a time (Flask reloader /
    several gunicorn workers would otherwise all crawl at once)."""
    try:
        if os.path.exists(LOCK_PATH) and time.time() - os.path.getmtime(LOCK_PATH) < 20 * 60:
            return False
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_WRONLY | os.O_TRUNC)
        os.close(fd)
        return True
    except OSError:
        return False


def _release_lock():
    try:
        os.remove(LOCK_PATH)
    except OSError:
        pass


def run_crawl_and_save(site_url, extra_seeds=None):
    if not _acquire_lock():
        return False
    _status["running"] = True
    _status["last_error"] = None
    try:
        result = crawl_site(site_url, extra_seeds)
        if result["pages"]:
            tmp = CACHE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False)
            os.replace(tmp, CACHE_PATH)  # atomic swap
            print(f"[site_crawler] done - {len(result['pages'])} pages cached.")
        else:
            _status["last_error"] = (
                "No readable pages found (site may be JavaScript-rendered, or "
                "blocked/offline). Old cache kept if any."
            )
            print("[site_crawler]", _status["last_error"])
        return True
    except Exception as e:
        _status["last_error"] = f"{type(e).__name__}: {e}"
        print("[site_crawler] crawl failed:", _status["last_error"])
        return False
    finally:
        _status["running"] = False
        _release_lock()


def refresh_in_background(site_url, extra_seeds=None, max_age_hours=24, force=False):
    """Starts a background crawl if the cache is missing/older than max_age_hours."""
    cache = load_cache()
    age_hours = (time.time() - cache.get("crawled_at", 0)) / 3600
    if not force and cache.get("pages") and age_hours < max_age_hours:
        return False
    t = threading.Thread(target=run_crawl_and_save, args=(site_url, extra_seeds), daemon=True)
    t.start()
    return True
