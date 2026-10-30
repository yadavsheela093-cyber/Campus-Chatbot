"""
brochure.py
Gives the chatbot the COMPLETE official brochure (all pages), so no detail is
missed. Also finds the single best-matching brochure section, which is used
for a clean answer when the AI model is unavailable.
"""

import os
import re
import math
from collections import Counter

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
FULL_PATH = os.path.join(BASE_DIR, "content", "brochure_full.txt")

_cache = {"full": None, "sections": None, "rank": None}

_STOP = {
    "the", "and", "for", "what", "tell", "about", "please", "are", "you", "with", "this",
    "that", "have", "does", "how", "much", "kitni", "kitna", "kitne", "batao", "bataiye",
    "btao", "hai", "hain", "kya", "kyu", "mujhe", "aap", "pcte", "college", "ludhiana",
    # common Hinglish filler words
    "kab", "kaun", "kaise", "kahan", "mein", "hoga", "hogi", "hota", "hoti", "wala", "wali", "koi",
    "aur", "bhi", "yeh", "woh", "kuch", "bataye", "batayiye", "kare", "karo", "karna", "chahiye",
    "milta", "milti", "milte", "hamare", "unka", "uski", "uska", "iski", "iska", "give", "show",
    # generic English words that carry no topic
    "which", "who", "when", "where", "there", "their", "been", "will", "can", "not", "its", "from",
    "into", "than", "then", "also", "only", "very", "many", "more", "some", "any", "get", "got",
    "has", "had", "was",
    "baare", "bare", "jankari", "information", "info", "details", "detail",
    "kitne", "kitni", "kitna", "kyun", "kitna",
}


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception:
        return ""


_SYNONYMS = {
    "fess": "fee", "fes": "fee", "fee": "fee", "fees": "fee", "charges": "fee", "charge": "fee",
    "cost": "fee", "costs": "fee", "kharcha": "fee", "price": "fee", "tuition": "fee",
    "ranking": "rank", "rankings": "rank", "ranked": "rank", "ranks": "rank",
    "packages": "package", "salary": "package", "ctc": "package", "lpa": "package",
    "hostels": "hostel", "pg": "hostel", "stay": "hostel", "accommodation": "hostel",
    "buses": "bus", "transport": "bus", "transportation": "bus",
    "address": "contact", "phone": "contact", "number": "contact", "email": "contact",
    "eligible": "eligibility", "qualification": "eligibility", "criteria": "eligibility",
    # history / founding
    # (a value with two words becomes two tokens: "kab bana" = established + year)
    "bana": "established year", "banaya": "established year", "bani": "established year",
    "founded": "established founders", "founder": "established founders", "founders": "established founders",
    "establish": "established", "estd": "established", "sthapna": "established year", "itihaas": "history",
    # duration
    "saal": "duration", "sal": "duration", "years": "duration", "year": "duration",
    # food / mess
    "khana": "food", "meal": "food", "meals": "food", "mess": "food", "canteen": "food",
    # internet
    "wifi": "internet", "wi": "internet",
}


def _stem(w):
    if w.endswith("uses") and len(w) > 5:        # campuses -> campus, buses -> bus
        return w[:-2]
    for suf in ("ings", "ing", "ed", "s"):
        if suf == "s" and w.endswith(("us", "ss", "is")):
            continue                                 # campus, bus, class, analysis stay whole
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[: -len(suf)]
    return w


def _tokens(text):
    out = []
    for raw in re.findall(r"[a-z0-9]+", text.lower()):
        for w in _SYNONYMS.get(raw, raw).split():
            for part in _SYNONYMS.get(w, w).split():          # one more level: "bana" -> established + year -> duration
                if (len(part) >= 3 or part in ("ai", "ml", "ib")) and part not in _STOP:
                    out.append(_stem(part))
    return out


def clean_section(text):
    """Remove the internal header line ('PCTE Ludhiana official brochure 2026 - ...')."""
    lines = [l for l in text.splitlines()
             if not re.match(r"^\s*PCTE Ludhiana\b.*\bbrochure\b", l, re.I)]
    return "\n".join(lines).strip()


def full_text():
    """Whole brochure: the clean summary files first, then every page of the raw text."""
    if _cache["full"] is None:
        summaries = []
        if os.path.isdir(DATA_DIR):
            for name in sorted(os.listdir(DATA_DIR)):
                if name.startswith("brochure_pcte_") and name.endswith(".txt"):
                    summaries.append(clean_section(_read(os.path.join(DATA_DIR, name))))
        raw = _read(FULL_PATH)
        _cache["full"] = "\n\n".join(summaries) + "\n\n=== FULL BROCHURE, PAGE BY PAGE ===\n" + raw
    return _cache["full"]


def _sections():
    """List of (file name tokens, text) for every brochure section."""
    if _cache["sections"] is None:
        secs = []
        if os.path.isdir(DATA_DIR):
            for name in sorted(os.listdir(DATA_DIR)):
                if name.startswith("brochure_") and name.endswith(".txt"):
                    text = clean_section(_read(os.path.join(DATA_DIR, name)))
                    if text:
                        stem = re.sub(r"\.txt$", "", name)
                        stem = re.sub(r"^brochure_", "", stem)
                        stem = re.sub(r"^(p\d+_|pcte_)", "", stem)
                        secs.append((name, set(_tokens(stem.replace("_", " "))), text))
        _cache["sections"] = secs
    return _cache["sections"]


_RANK_WORDS = {"rank", "award", "survey", "recognition", "best", "top", "rating"}


def _ranked(query, with_names=False, min_cover=0.5):
    """All brochure sections for the question, best first (only plausible ones).
    with_names=True returns (file name, text) pairs."""
    secs = _sections()
    q = set(_tokens(query))
    if not secs or not q:
        return []
    if _cache.get("rank") is None:              # built once, not on every question
        toks = [Counter(_tokens(t)) for _, _, t in secs]
        dfc = Counter()
        for t in toks:
            dfc.update(set(t))
        _cache["rank"] = (toks, dfc, [set(t) for t in toks])
    sec_tokens, df, sec_sets = _cache["rank"]
    n = len(secs)
    scored = []
    for idx, ((name, name_tokens, text), tf) in enumerate(zip(secs, sec_tokens)):
        score = 0.0
        for w in q:
            idf = math.log((n + 1) / (df.get(w, 0) + 1)) + 1
            if w in tf:
                score += idf * (1 + math.log(tf[w]))
            if w in name_tokens:
                score += 4 * idf                 # topic named in the section's title
        if name_tokens and q & name_tokens:
            score += 6 * len(q & name_tokens) / len(name_tokens)   # exact-topic page beats a broader one
        if "awards" in name and not (q & _RANK_WORDS):
            score *= 0.25                       # rankings page only when asked about rankings
        if score > 0:
            scored.append((score, text, name, idx))
    scored.sort(key=lambda x: -x[0])
    out = []
    for score, text, name, idx in scored[:6]:
        if sum(1 for w in q if w in sec_sets[idx]) >= min_cover * len(q):
            out.append((name, text) if with_names else text)
    return out


def best_section(query):
    """Most relevant brochure section for the question, or None if nothing fits."""
    r = _ranked(query)
    return r[0] if r else None


def _stem_text(text):
    return set(_tokens(text))


_TOPICS = {
    "eligibility": ("eligib",),
    "seat": ("seats",),
    "duration": ("duration",),
    "placement": ("placement", "placed"),
}


def best_answer(query, min_cover=0.5):
    """Best brochure text for the question. If the student asks about one thing of
    a course (eligibility / seats / duration / placement), only those lines are returned."""
    ranked = _ranked(query, min_cover=min_cover)
    if not ranked:
        return None
    q = set(_tokens(query))
    keys = [k for t, k in _TOPICS.items() if t in q]
    if keys:
        for section in ranked:
            lines = [l for l in section.splitlines() if l.strip()]
            hits = []
            for i, l in enumerate(lines):
                low = l.lower()
                if any(any(k in low for k in kk) for kk in keys):
                    hits.append(l)
                    if "placement" in low:                # a placement paragraph spans lines
                        hits.extend(lines[i + 1:i + 3])
                    else:                                 # sentence wrapped over several lines
                        j = i
                        while (j + 1 < len(lines) and j - i < 4
                               and not lines[j].rstrip().endswith((".", "!", "?"))
                               and not re.match(r"^[A-Z][\w &/\-\(\)]{1,30}:\s", lines[j + 1])):
                            j += 1
                            hits.append(lines[j])
            if hits:
                for i, l in enumerate(lines):             # program name sits above "Duration:"
                    if l.lower().startswith("duration") and i > 0:
                        hits.insert(0, lines[i - 1])
                        break
                return "\n".join(dict.fromkeys(hits))
    return ranked[0]
