"""
photos.py - campus-specific photo answers ("Show me Campus 2 photos").

Design rules (important):
  * Photos come ONLY from campus_photos.json. No URL is ever generated or guessed.
  * An entry is shown only if it is explicitly tagged with ONE campus (1 or 2),
    has an https URL on an allowed official host (or is an own photo stored under
    static/campus_photos/), a caption, a clickable source
    page, and a non-empty "evidence" note saying HOW the campus was established
    (e.g. the official page's own caption). Filename / domain alone is never evidence.
  * Campus 1 and Campus 2 are never mixed up. If a campus has no verified photos,
    the bot says so honestly and links the official page instead of showing
    photos from the other campus.
  * Follow-ups ("show more Campus 2 photos") continue where the last photo reply
    stopped, read from a marker line in the previous bot message.
"""

import json
import os
import re
from urllib.parse import urlparse

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGISTRY_PATH = os.environ.get("CAMPUS_PHOTOS_FILE", os.path.join(BASE_DIR, "campus_photos.json"))
PAGE_SIZE = 4
LOCAL_PREFIX = "/static/campus_photos/"      # photos stored in the project's own static folder

_PHOTO_WORDS = {"photo", "photos", "picture", "pictures", "pic", "pics", "image", "images",
                "gallery", "galleries", "photograph", "photographs", "foto", "fotos", "tasveer", "tasveerein"}
# "photo" in an admission-paperwork question is not a request to see campus photos
_NOT_A_GALLERY_REQUEST = {"passport", "upload", "signature", "certificate", "certificates", "documents",
                          "document", "required", "id", "scan", "size", "attested", "form"}
_MORE_WORDS = {"more", "next", "another", "else", "other", "aur", "zyada", "agla"}

_CAMPUS_PATTERNS = {
    "2": re.compile(r"\b(campus\s*[-_]?\s*(2|two|ii)|campus2|second campus|2nd campus|c2 campus)\b"),
    "1": re.compile(r"\b(campus\s*[-_]?\s*(1|one|i)|campus1|first campus|1st campus|main campus|c1 campus)\b"),
}
_MARKER = re.compile(r"Campus\s+(1|2|PCTE)\s+photos\s+(\d+)\s*[-–]\s*(\d+)\s+of\s+(\d+)", re.I)


# ----------------------------------------------------------------- registry
def load_registry(path=None):
    path = path or REGISTRY_PATH
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _host_ok(url, allowed):
    try:
        p = urlparse(url)
    except ValueError:
        return False
    host = (p.hostname or "").lower()
    return p.scheme == "https" and any(host == a or host.endswith("." + a) for a in allowed)


def _local_ok(url):
    """Own photo inside static/campus_photos/ - must exist on disk, no path tricks."""
    if not isinstance(url, str) or not url.startswith(LOCAL_PREFIX) or ".." in url or "\\" in url:
        return False
    return os.path.isfile(os.path.join(BASE_DIR, url.lstrip("/").replace("/", os.sep)))


def valid_entries(registry):
    """Entries that pass every verification rule, grouped by campus: {"1": [...], "2": [...]}.
    Two kinds are accepted:
      * official web photo  - https URL + https source page, both on an allowed host
      * own photo           - file under static/campus_photos/ that really exists
    Both need a campus tag (1 or 2), a caption and an evidence note."""
    allowed = [h.lower() for h in registry.get("allowed_hosts", [])]
    out = {"1": [], "2": []}
    seen = set()
    for e in registry.get("photos", []):
        if not isinstance(e, dict):
            continue
        campus = str(e.get("campus", "")).strip()
        url, source = e.get("url", ""), e.get("source_page", "")
        if campus not in out or not e.get("verified") or url in seen:
            continue
        if not (str(e.get("caption", "")).strip() and str(e.get("evidence", "")).strip()):
            continue
        if _local_ok(url):
            source = ""                              # own photo: no external source page
        elif not (_host_ok(url, allowed) and _host_ok(source, allowed)):
            continue
        seen.add(url)
        out[campus].append({"url": url, "caption": e["caption"].strip(), "source_page": source, "campus": campus})
    return out


# ------------------------------------------------------------ classification
def _tokens(text):
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def is_photo_request(message):
    toks = set(_tokens(message))
    return bool(toks & _PHOTO_WORDS) and not (toks & _NOT_A_GALLERY_REQUEST)


def campus_in(message):
    low = re.sub(r"\s+", " ", (message or "").lower())
    c2, c1 = _CAMPUS_PATTERNS["2"].search(low), _CAMPUS_PATTERNS["1"].search(low)
    if c2 and not c1:
        return "2"
    if c1 and not c2:
        return "1"
    return None


def last_photo_state(history):
    """(campus, shown_upto) from the most recent bot photo reply in history, else (None, 0)."""
    for h in reversed(history or []):
        if isinstance(h, dict) and h.get("role") != "user":
            m = _MARKER.search(str(h.get("text", "")))
            if m:
                return ("all" if m.group(1).upper() == "PCTE" else m.group(1)), int(m.group(3))
            break  # the latest bot message was not a photo reply
    return None, 0


def classify(message, history=None):
    """-> None (not a photo question) or {"campus": "1"|"2"|"all", "offset": int, "more": bool}"""
    toks = set(_tokens(message))
    campus = campus_in(message)
    prev_campus, prev_end = last_photo_state(history)
    more = bool(toks & _MORE_WORDS)
    if is_photo_request(message):
        resolved = campus or (prev_campus if more and prev_campus else "all")
        offset = prev_end if more and prev_campus == resolved else 0
        return {"campus": resolved, "offset": offset, "more": more}
    # bare follow-up: "more", "show more", "next ones" right after a photo reply
    if prev_campus and more and len(toks) <= 4:
        return {"campus": campus or prev_campus, "offset": prev_end if (campus or prev_campus) == prev_campus else 0,
                "more": True}
    return None


# --------------------------------------------------------------------- answer
OFFICIAL_PAGE = {"title": "PCTE official website", "url": "https://pcte.edu.in/"}


def photo_reply(message, history=None, registry=None):
    """Returns None if this is not a photo question, else
    {"text", "photos": [{url, caption, source_page, campus}], "links": [{title, url}]}"""
    req = classify(message, history)
    if not req:
        return None
    registry = registry if registry is not None else load_registry()
    pool = valid_entries(registry)
    campus, offset = req["campus"], req["offset"]

    if campus == "all":
        items = pool["1"] + pool["2"]          # each item keeps its own campus tag
        label = "PCTE"
    else:
        items = pool[campus]
        label = f"Campus {campus}"

    if not items:
        return _none_verified(campus, pool)

    batch = items[offset:offset + PAGE_SIZE]
    if not batch:
        return {"text": f"That's all the verified {label} photos I have ({len(items)} in total). "
                        f"You can see more on the official PCTE website.\n"
                        f"[{label} photos {len(items)}-{len(items)} of {len(items)}]",
                "photos": [], "links": [OFFICIAL_PAGE]}
    end = offset + len(batch)
    photos = [dict(p) for p in batch]          # caption exactly as written in campus_photos.json
    more_hint = "  Say \"show more\" for the next ones." if end < len(items) else ""
    text = (f"Here are {label} photos.{more_hint}\n"
            f"[{label} photos {offset + 1}-{end} of {len(items)}]")
    return {"text": text, "photos": photos, "links": []}


def _none_verified(campus, pool):
    if campus in ("1", "2"):
        other = "2" if campus == "1" else "1"
        text = (f"I couldn't find any official PCTE photos that are verified as Campus {campus}, "
                f"so I won't show pictures I can't be sure about"
                + (f" (I have not substituted Campus {other} photos either)." if pool[other] else ".")
                + " You can browse the official PCTE website directly.")
    else:
        text = ("I don't have any verified official PCTE photos loaded right now. "
                "You can browse the official PCTE website directly.")
    return {"text": text, "photos": [], "links": [OFFICIAL_PAGE]}
