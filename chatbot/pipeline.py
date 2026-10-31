"""
pipeline.py - the whole "question -> answer" flow in one place.

   greeting?  -> instant friendly reply (no AI needed)
   asked before? -> instant answer from the cache
   otherwise:
     1. structured database  (exact fees / hostel / bus / contacts / FAQs)
     2. RAG retrieval        (best passages from the brochure + website)
     3. brochure page routing (the page named after the topic, e.g. "BCA")
     -> a SMALL context (~1-2k tokens) is built from 1+2+3
     4. Gemini writes the answer, streamed word by word
     5. if Gemini is unavailable -> instant fallback from the same data

Everything is yielded as small "events" so the website can show the answer
while it is still being written (see /api/chat-stream in app.py).
"""

import re
import time
import threading
from collections import OrderedDict, Counter

from chatbot import ai, brochure, photos, admission_faq
from chatbot.database import search_structured_data, log_chat
from chatbot.rag import source_of, topic_from_filename
from chatbot.smalltalk import smalltalk_reply

MAX_CONTEXT_CHARS = 8000
TOP_PASSAGE_CHARS = 3500
OTHER_PASSAGE_CHARS = 2200
CACHE_TTL = 6 * 3600
CACHE_MAX = 300
HISTORY_TURNS = 4

_cache = OrderedDict()          # normalized question -> (time, text, sources)
_cache_lock = threading.Lock()

_FOLLOWUP_WORDS = {"it", "its", "this", "that", "they", "their", "there", "same", "uska", "uski", "uske",
                   "iska", "iski", "iske", "isme", "usme", "wahan", "yahan", "also", "aur", "and", "or"}


# ------------------------------------------------------------------- helpers
NO_ANSWER_REPLY = ("I'm sorry.\n"
                   "Here is the contact of PCTE:\n"
                   "Phone: +91-161-2888500\n"
                   "Email: admissions@pcte.edu.in")
_NO_ANSWER_RE = re.compile(r"couldn.?t find reliable information|don.?t want to give you a wrong answer", re.I)


def is_no_answer(text):
    return bool(_NO_ANSWER_RE.search(text or ""))


def polish(text):
    """The long 'I'm sorry, I couldn't find reliable information...' message is replaced by a short one with the contact."""
    return NO_ANSWER_REPLY if is_no_answer(text) else text


def normalize_q(text):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", (text or "").lower())).strip()


def _flat(text):
    return re.sub(r"\s+", " ", text).strip().lower()


def _cap(text, limit):
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("\n"))
    return (cut[:end + 1] if end > limit * 0.6 else cut).strip()


def clean_history(history):
    """history = [{'role': 'user'|'bot', 'text': '...'}] from the browser -> last few safe turns."""
    out = []
    for h in (history or [])[-HISTORY_TURNS * 2:]:
        if not isinstance(h, dict):
            continue
        role = "Student" if h.get("role") == "user" else "Assistant"
        text = str(h.get("text", "")).strip()
        if text:
            out.append((role, text[:400]))
    return out


def _is_followup(message):
    norm = normalize_q(message)
    words = norm.split()
    return len(words) <= 4 or any(w in _FOLLOWUP_WORDS for w in words) or norm.startswith("what about")


def retrieval_query(message, turns):
    """For a short follow-up ('and hostel?', 'uski fees?') add the previous student question
    so retrieval knows the topic."""
    if turns and _is_followup(message):
        prev = [t for r, t in turns if r == "Student"]
        if prev:
            return prev[-1] + " " + message
    return message


# ------------------------------------------------------------------- cache
def cache_get(key):
    with _cache_lock:
        item = _cache.get(key)
        if not item:
            return None
        if time.time() - item[0] > CACHE_TTL:
            _cache.pop(key, None)
            return None
        _cache.move_to_end(key)
        return item


def cache_put(key, text, sources):
    with _cache_lock:
        _cache[key] = (time.time(), text, sources)
        _cache.move_to_end(key)
        while len(_cache) > CACHE_MAX:
            _cache.popitem(last=False)


# ----------------------------------------------------------------- retrieval
def gather_passages(query, engine, top_k=6):
    """Merge RAG hits and brochure-page routing (Reciprocal Rank Fusion), drop duplicates
    and weak matches. Returns [(source_name, text)] best first."""
    pool, fused, votes = {}, Counter(), Counter()
    rag_hits = engine.retrieve(query, top_k=top_k) if engine else []
    for rank, chunk in enumerate(rag_hits):
        src = source_of(chunk)
        body = re.sub(r"^\[[^\]]*\]\s*", "", chunk)
        key = _flat(body)[:160]
        pool.setdefault(key, (src, body))
        fused[key] += 1.0 / (60 + rank)
        votes[key] += 1
    for rank, (name, text) in enumerate(brochure._ranked(query, with_names=True)[:4]):
        key = _flat(text)[:160]
        pool.setdefault(key, (topic_from_filename(name), text))
        fused[key] += 1.2 / (60 + rank)          # the page named after the topic is a strong signal
        votes[key] += 1

    ordered, flats = [], []
    for position, (key, _) in enumerate(fused.most_common()):
        if position >= 3 and votes[key] < 2:
            continue                 # weak, unconfirmed hit: keeps the context small and on-topic
        src, body = pool[key]
        fb = _flat(body)
        probe = fb[:200]
        dup = False
        for i, existing in enumerate(flats):
            if probe and probe in existing:              # already covered by a bigger passage
                dup = True
                break
            if existing[:200] and existing[:200] in fb:  # this one is the bigger version
                ordered[i], flats[i] = (src, body), fb
                dup = True
                break
        if not dup:
            ordered.append((src, body))
            flats.append(fb)
    return ordered[:5]


def build_context(structured, passages):
    parts, used = [], 0
    if structured:
        block = "VERIFIED DATA (exact figures - trust these):\n" + _cap(structured, 3000)
        parts.append(block)
        used += len(block)
    if passages:
        parts.append("RELEVANT OFFICIAL PASSAGES:")
    for i, (src, text) in enumerate(passages):
        room = MAX_CONTEXT_CHARS - used
        if room < 400:
            break
        piece = f"[{i + 1}] ({src}) " + _cap(text, min(TOP_PASSAGE_CHARS if i == 0 else OTHER_PASSAGE_CHARS, room))
        parts.append(piece)
        used += len(piece)
    return ai.scrub("\n\n".join(parts))


def _sources(passages, structured):
    names = []
    if structured:
        names.append("Verified college data")
    for src, _ in passages:
        if src not in names:
            names.append(src)
    return names[:5]


# -------------------------------------------------------------------- events
def answer_events(message, history=None, engine=None):
    """Generator of dict events: meta -> delta* -> final."""
    t0 = time.time()
    message = (message or "").strip()
    turns = clean_history(history)

    def ms():
        return int((time.time() - t0) * 1000)

    # 0) greetings / thanks / bye - instant, no AI
    quick = smalltalk_reply(message)
    if quick:
        yield {"type": "meta", "mode": "instant", "sources": []}
        yield {"type": "final", "text": quick, "mode": "instant", "ms": ms(), "sources": []}
        return

    # 0b) campus photo requests ("Show me Campus 2 photos", "show more") - registry-backed, never AI-generated
    photo = photos.photo_reply(message, history)
    if photo:
        yield {"type": "meta", "mode": "photos", "sources": []}
        yield {"type": "final", "text": photo["text"], "mode": "photos", "ms": ms(), "sources": [],
               "photos": photo["photos"], "links": photo["links"]}
        _log(message, photo["text"])
        return

    # 0c) admission requirements - instant answer (spelling mistakes allowed), no AI
    adm = admission_faq.admission_reply(message)
    if adm:
        yield {"type": "meta", "mode": "instant", "sources": []}
        yield {"type": "final", "text": adm, "mode": "instant", "ms": ms(), "sources": []}
        _log(message, adm)
        return

    # 1) cache (only for stand-alone questions, not follow-ups)
    standalone = not (turns and _is_followup(message))
    key = normalize_q(message)
    if standalone:
        hit = cache_get(key)
        if hit:
            yield {"type": "meta", "mode": "cache", "sources": hit[2]}
            yield {"type": "final", "text": hit[1], "mode": "cache", "ms": ms(), "sources": hit[2]}
            return

    # 2) exact database facts + 3) RAG/brochure passages -> small context
    query = retrieval_query(message, turns)
    structured = search_structured_data(query)
    passages = gather_passages(query, engine)
    context = build_context(structured, passages)
    sources = []          # sources are never shown to students
    history_text = "\n".join(f"{r}: {t}" for r, t in turns)
    yield {"type": "meta", "mode": "ai", "sources": sources}

    # 4) Gemini, streamed
    raw, model, error = "", None, None
    t_ctx, first_ms = ms(), None
    held, state = [], "hold"      # first words are held back briefly so the long "I'm sorry..." text is never shown
    for kind, payload in ai.stream_answer(message, context, history_text):
        if kind == "delta":
            if first_ms is None:
                first_ms = ms()
            raw += payload
            if state == "hold":
                held.append(payload)
                if is_no_answer(raw):
                    state, held = "suppress", []
                elif len(raw) >= 70:
                    state = "flow"
                    for h in held:
                        yield {"type": "delta", "text": h}
                    held = []
            elif state == "flow":
                yield {"type": "delta", "text": payload}
        elif kind == "done":
            model = payload.get("model")
        elif kind == "error":
            error = payload
    if state == "hold" and held and not is_no_answer(raw):      # very short answer
        for h in held:
            yield {"type": "delta", "text": h}

    if raw.strip():
        text = polish(ai.clean_reply(raw))
        if standalone:
            cache_put(key, text, sources)
        _log(message, text)
        print(f"[speed] search {t_ctx}ms | first word {first_ms}ms | total {ms()}ms | {model}")
        yield {"type": "final", "text": text, "mode": "ai", "model": model, "ms": ms(), "sources": sources}
        return

    # 5) Gemini could not answer -> instant answer from the same data
    text = polish(ai.clean_reply(ai.fallback_answer(query, structured, error, passages)))
    _log(message, text)
    yield {"type": "final", "text": text, "mode": "fallback", "error": error,
           "reason": ai.reason_label(error), "ms": ms(), "sources": sources}


def _log(question, reply):
    try:
        log_chat(question, reply)
    except Exception:
        pass


def answer(message, history=None, engine=None):
    """Non-streaming helper: returns the final event as a dict."""
    final = {}
    for ev in answer_events(message, history, engine):
        if ev["type"] == "final":
            final = ev
    return final


def debug_info(message, history=None, engine=None):
    turns = clean_history(history)
    query = retrieval_query(message, turns)
    structured = search_structured_data(query)
    passages = gather_passages(query, engine)
    context = build_context(structured, passages)
    return {
        "retrieval_query": query,
        "structured_answer": structured,
        "rag_mode": getattr(engine, "mode", None),
        "chunks_indexed": len(getattr(engine, "chunks", []) or []),
        "passages": [{"source": s, "chars": len(t), "preview": t[:160]} for s, t in passages],
        "context_chars": len(context),
        "approx_tokens_sent_to_gemini": len(context) // 4,
        "old_design_tokens_for_comparison": len(brochure.full_text()) // 4,
    }
