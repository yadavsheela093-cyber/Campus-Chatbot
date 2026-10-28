"""
ai.py - talks to Google Gemini.

SPEED + RELIABILITY design (why answers are now fast):
- Only a SMALL context is sent (the few passages RAG found, ~2k tokens) instead
  of the whole 160,000-character brochure (~35k tokens). Smaller prompt = faster
  first word AND far fewer 429 "quota exceeded" errors (free tier = 250k tokens/min).
- Answers are STREAMED word by word, so the student starts reading in ~1 second.
- Fastest models first: gemini-2.5-flash-lite (free: 1000 requests/day), then
  gemini-2.5-flash, then the "latest" aliases. Thinking is switched off (faster).
- A model that fails (429 / not found) is put on a short cool-down, so the next
  question does not waste time retrying it. At most 2 models are tried per question.
- If every model fails, the caller falls back to an instant brochure answer.
"""

import os
import re
import time
import threading

from chatbot.database import load_config
from chatbot import brochure

_env_models = [m.strip() for m in os.environ.get("GEMINI_MODELS", "").split(",") if m.strip()]
MODEL_CANDIDATES = _env_models or [
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
    "gemini-flash-lite-latest",
    "gemini-flash-latest",
]
MAX_ATTEMPTS = 2            # models tried per question
REQUEST_TIMEOUT_MS = 25000

_client = {"obj": None, "ready": False}
_client_lock = threading.Lock()
_cooldown = {}              # model -> unix time until which it is skipped
_status = {"last_error": None, "last_model": None, "last_ok": None}


# --------------------------------------------------------------------- prompt
def build_system_prompt():
    config = load_config()
    college = config.get("college_name", "the college")
    language = config.get("reply_language", "English")
    lang_rule = (
        f"ALWAYS reply in {language} only, even if the person writes in another language. "
        "Understand their question, then answer in clear, simple words."
        if language.lower() != "same as user" else
        "Reply in the same language the person used (English / Hindi / Hinglish)."
    )
    return (
        f"You are the Campus Assistant of {college}. You talk to students and parents like a "
        "warm, caring elder sibling: polite, patient and lovingly helpful.\n\n"
        "HOW TO ANSWER:\n"
        "1. For college-specific questions, prioritize verified data and official college information "
        "in the Context. Give the exact answer directly in the first line whenever the information is available.\n"
        "2. Be accurate. Copy fees, seats, duration, eligibility, dates and phone numbers exactly as verified. "
        "Never guess or invent college-specific information, and never mix up two courses. If several relevant "
        "items are listed, provide the complete list.\n"
        "3. If the question is a follow-up (like 'and its fees?'), use the Recent conversation to understand "
        "what it refers to.\n"
        "4. For general educational questions, including the usual B.Tech admission process and commonly "
        "required documents, answer helpfully using general knowledge even if the Context does not contain "
        "the answer. Make clear that requirements can vary by college and course.\n"
        "5. If a specific college detail is missing or unverified, say politely that the exact college "
        "information could not be verified and suggest contacting the admissions office. Give phone or email "
        "details only if verified information is available.\n"
        "6. Never say words like 'brochure', 'context', 'passage', 'document' or 'source', and never show "
        "URLs unless the person asks for the website. Answer naturally.\n"
        "7. If the person only greets you, reply 'Hello! May I help you?'. If they say thanks, reply warmly.\n"
        "8. For things totally unrelated to college or education, politely explain that you are here to help "
        "with campus questions.\n\n"
        "STYLE:\n"
        f"- {lang_rule}\n"
        "- Plain text only. No markdown: no **bold**, no # headings, no backticks. For lists put each "
        "item on a new line starting with '•'.\n"
        "- Keep it short and clear (usually 2-6 lines). Warm and gentle tone ('Sure!', 'Of course!'), "
        "at most one emoji. End with a short friendly line inviting more questions only when natural."
    )


def build_user_prompt(question, context_text, history_text=""):
    parts = [f"Context:\n{context_text}"]
    if history_text:
        parts.append(f"Recent conversation:\n{history_text}")
    parts.append(f"Student question: {question}")
    return "\n\n".join(parts)


# --------------------------------------------------------------------- client
def get_client():
    """Creates the Gemini client once (thread-safe). Returns None if unusable."""
    with _client_lock:
        if _client["ready"]:
            return _client["obj"]
        _client["ready"] = True
        key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not key or key == "your_api_key_here":
            _status["last_error"] = "GEMINI_API_KEY missing in .env"
            print("[ai.py] No GEMINI_API_KEY found - check your .env file (answers will be brochure-only).")
            return None
        try:
            from google import genai
            try:
                from google.genai import types
                client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_MS))
            except Exception:
                client = genai.Client(api_key=key)
            _client["obj"] = client
        except Exception as e:
            _status["last_error"] = f"{type(e).__name__}: {e}"
            print(f"[ai.py] Could not start the Gemini client: {type(e).__name__}: {e}")
        return _client["obj"]


def warmup():
    """Called once at startup in a background thread so the first question is fast."""
    get_client()


def reset_client():
    with _client_lock:
        _client["obj"], _client["ready"] = None, False


# ------------------------------------------------------------------ cool-down
def _classify(err):
    text = str(err)
    low = text.lower()
    if "429" in text or "resource_exhausted" in low or "quota" in low:
        m = re.search(r"retry(?:delay)?\D{0,12}(\d+(?:\.\d+)?)\s*s", low)
        secs = float(m.group(1)) if m else 60
        if "perday" in low.replace(" ", "").replace("_", "") or "per day" in low:
            secs = max(secs, 900)
        return "quota", min(max(secs, 15), 1800)
    if "404" in text or "not_found" in low or "not found" in low:
        return "not_found", 3600
    if "api key" in low or "api_key" in low or "permission" in low or "401" in text or "403" in text:
        return "auth", 120
    return "other", 20


def _cooling(model):
    return _cooldown.get(model, 0) > time.time()


def _record_failure(model, err):
    kind, secs = _classify(err)
    _cooldown[model] = time.time() + secs
    _status["last_error"] = f"{model}: {type(err).__name__}: {str(err)[:240]}"
    print(f"[ai.py] {model} failed ({kind}), skipping it for {int(secs)}s: {str(err)[:160]}")
    return kind


# ------------------------------------------------------------------ streaming
def _chunk_text(chunk):
    try:
        return chunk.text or ""
    except Exception:
        return ""


def _stream_one(client, model, system_prompt, user_prompt):
    """Yields text pieces from ONE model. Raises if it fails before any text."""
    from google.genai import types

    use_thinking_off = "2.5" in model
    for attempt in (0, 1):
        kwargs = dict(system_instruction=system_prompt, temperature=0.2, max_output_tokens=900)
        if use_thinking_off:
            try:
                kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
            except Exception:
                pass
        config = types.GenerateContentConfig(**kwargs)
        got = False
        try:
            if hasattr(client.models, "generate_content_stream"):
                for chunk in client.models.generate_content_stream(model=model, contents=user_prompt, config=config):
                    text = _chunk_text(chunk)
                    if text:
                        got = True
                        yield text
            else:
                resp = client.models.generate_content(model=model, contents=user_prompt, config=config)
                text = _chunk_text(resp)
                if text:
                    got = True
                    yield text
            return
        except Exception as e:
            if got:
                raise
            if attempt == 0 and use_thinking_off and "thinking" in str(e).lower():
                use_thinking_off = False        # this model rejects the thinking setting: retry without it
                continue
            raise


def stream_answer(question, context_text, history_text=""):
    """Generator of events:
         ("delta", text)                      a piece of the answer
         ("done", {"model": name})            finished OK
         ("error", "quota"|"auth"|"other"|"nokey")   nothing could be produced
    """
    client = get_client()
    if client is None:
        yield ("error", "nokey")
        return

    system_prompt = build_system_prompt()
    user_prompt = build_user_prompt(question, context_text, history_text)
    attempts, seen = 0, set()

    for model in MODEL_CANDIDATES:
        if _cooling(model):
            continue
        if attempts >= MAX_ATTEMPTS:
            break
        attempts += 1
        got = False
        try:
            for piece in _stream_one(client, model, system_prompt, user_prompt):
                got = True
                yield ("delta", piece)
            if got:
                _status.update(last_model=model, last_ok=time.time(), last_error=None)
                yield ("done", {"model": model})
                return
            _cooldown[model] = time.time() + 20          # empty reply (blocked / nothing)
            seen.add("other")
            print(f"[ai.py] {model} returned an empty reply.")
        except Exception as e:
            if got:                                       # broke off mid-answer: keep what we have
                _record_failure(model, e)
                yield ("done", {"model": model, "partial": True})
                return
            seen.add(_record_failure(model, e))

    if not seen:                                          # every model was cooling down
        seen.add("quota")
    yield ("error", "quota" if "quota" in seen else ("auth" if "auth" in seen else "other"))


# ---------------------------------------------------------------- wording helpers
def scrub(text):
    """The students must never read the word 'brochure'. The data files mention it (it is
    where the facts came from), so it is removed from everything the student can see
    and from the context sent to Gemini."""
    text = re.sub(r"\(\s*source:[^)]*\)", "", text, flags=re.I)
    text = re.sub(r"(?i)\b(according to|as per|as mentioned in|as stated in|as given in|as written in|in|from)\s+the\s+(official\s+)?(pcte\s+)?brochure\b[,:]?\s*", "", text)
    text = re.sub(r"(?i)\b(the\s+)?(official\s+)?(pcte\s+)?brochure(\s+20\d\d)?\b", "college information", text)
    return text


def reason_label(error_kind):
    """Short text shown under a fallback answer, so it is clear WHY the AI did not answer."""
    return {
        "nokey": "AI not connected - API key missing in .env",
        "auth": "AI key not accepted - check the key in .env",
        "quota": "AI limit reached - try again in a minute",
    }.get(error_kind, "AI temporarily unavailable")


def explain_error(err):
    """Plain-language reason + what to do, for the terminal and /api/ai-status."""
    text = str(err)
    low = text.lower()
    if "api_key_invalid" in low or "api key not valid" in low or "api key expired" in low or "key not valid" in low:
        return "The API key is WRONG or expired. Create a new key at https://aistudio.google.com/apikey and paste it in .env (GEMINI_API_KEY=... with no quotes and no spaces), then restart."
    if "429" in text or "resource_exhausted" in low or "quota" in low:
        return "Free-tier limit reached for this model (Google's limit, not a bug). It resets daily; wait a minute for the per-minute limit, or try again tomorrow."
    if "403" in text or "permission" in low or "denied" in low:
        return "The key has no permission for this model/region. Create a new key in Google AI Studio (aistudio.google.com/apikey) and use it."
    if "404" in text or "not_found" in low or "not found" in low:
        return "This model name is not available for your key. The app will try the next model."
    if any(w in low for w in ("getaddrinfo", "connecterror", "connection", "timed out", "timeout", "name resolution", "ssl")):
        return "No internet / blocked by firewall or VPN. The app cannot reach Google right now."
    return "Unexpected error: " + text[:200]


def startup_check():
    """Runs once at startup (background thread) and PRINTS clearly whether Gemini works,
    and if not, exactly why and how to fix it."""
    line = "=" * 64
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key or key == "your_api_key_here":
        _status["startup"] = "no key"
        print(f"\n{line}\n[AI] \u274c Gemini is NOT connected: GEMINI_API_KEY is missing.\n"
              "     Fix: put your key in a file named .env next to app.py:\n"
              "          GEMINI_API_KEY=your_real_key_here\n"
              "     (copy .env.example to .env). If you replaced the project folder with a new zip,\n"
              "     your old .env was deleted - create it again. Then restart.\n"
              f"     Until then the bot answers from its notes only (no AI).\n{line}\n")
        return
    client = get_client()
    if client is None:
        _status["startup"] = "client failed"
        print(f"\n{line}\n[AI] \u274c Could not start the Google AI library: {_status['last_error']}\n"
              f"     Fix: pip install -U google-genai\n{line}\n")
        return
    problems = {}                                  # explanation -> [models]
    for model in MODEL_CANDIDATES:
        t0 = time.time()
        try:
            r = client.models.generate_content(model=model, contents="Reply with the single word: OK")
            if (getattr(r, "text", "") or "").strip():
                _status.update(startup="ok", last_model=model, last_ok=time.time(), last_error=None)
                print(f"\n{line}\n[AI] \u2705 Gemini is connected and working (model {model}, {time.time() - t0:.1f}s).\n{line}\n")
                return
            problems.setdefault("empty reply", []).append(model)
        except Exception as e:
            kind = _record_failure(model, e)
            problems.setdefault(explain_error(e), []).append(model)
            if kind == "auth":                     # same key for every model - no point trying the rest
                break
    _status["startup"] = "failed"
    print(f"\n{line}\n[AI] \u274c Gemini is NOT working.")
    for reason, models in problems.items():
        print(f"     Reason: {reason}\n     (models tried: {', '.join(models)})")
    print(f"     Details: open http://127.0.0.1:5000/api/ai-status\n{line}\n")


# ---------------------------------------------------------------- text clean-up
def strip_notices(text):
    """Remove 'Gemini quota reached...' notices and any trailing 'Sources: ...' block."""
    text = re.sub(r"(?im)^.*gemini quota reached.*$\n?", "", text)
    text = re.sub(r"(?im)^.*showing available verified information.*$\n?", "", text)
    text = re.sub(r"(?is)\n*\s*sources?\s*:.*$", "", text)
    return text.strip()


def clean_reply(text):
    """Make the reply look like a normal chat message: no markdown symbols,
    no 'Source: ...' lines, no stray blank lines."""
    text = scrub(text).replace("**", "").replace("__", "").replace("`", "")
    text = strip_notices(text)
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if re.match(r"^PCTE Ludhiana\b.*\bbrochure\b", stripped, re.I):
            continue
        if re.match(r"^\(?\s*sources?\s*:", stripped, re.I):
            continue
        stripped = re.sub(r"^(from|according to)\s+the\s+(official\s+)?brochure\s*[:,\-]?\s*", "", stripped, flags=re.I)
        stripped = re.sub(r"^#{1,6}\s*", "", stripped)
        stripped = re.sub(r"^[\*\-]\s+", "• ", stripped)
        stripped = re.sub(r"\s*\(?\s*sources?\s*:\s*https?://\S+\)?", "", stripped, flags=re.I)
        lines.append(stripped)
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return cleaned[:1].upper() + cleaned[1:] if cleaned else cleaned


def office_contact():
    for c in load_config().get("contacts", []):
        bits = [b for b in (c.get("phone"), c.get("email")) if b]
        if c.get("phone"):
            return " / ".join(bits)
    return ""


def _split_sentences(line):
    """Split a long line into sentences without cutting after abbreviations like
    'Dr.', 'B.Sc.', 'K.N.S.' or 'Wg. Cdr.'."""
    parts, start = [], 0
    for m in re.finditer(r"[.!?]\s+(?=[A-Z0-9(\u2022])", line):
        word = re.search(r"(\S+)$", line[start:m.start()])
        last = word.group(1) if word else ""
        if len(last.replace(".", "")) <= 3 and m.group(0)[0] == "." and not last.isdigit():
            continue                              # looks like an abbreviation: keep going
        parts.append(line[start:m.start() + 1].strip())
        start = m.end()
    parts.append(line[start:].strip())
    return [x for x in parts if x]


def extractive_answer(question, passages):
    """No-AI answer built from the RAG passages: finds the passage that best covers the
    question and returns only its relevant lines (not the whole page)."""
    q = set(brochure._tokens(question))
    if not q:
        return None
    best = None                                  # (coverage, -rank, lines)
    for rank, (src, text) in enumerate(passages[:4]):
        units = []
        for line in re.split(r"\n+", text):
            line = line.strip()
            if not line:
                continue
            if len(line) > 260:
                units.extend(_split_sentences(line))
            else:
                units.append(line)
        scored = [(len(q & set(brochure._tokens(u))) / len(q), i, u) for i, u in enumerate(units)]
        page_cover = len(q & set(brochure._tokens(text))) / len(q)
        good = [x for x in scored if x[0] >= 0.3 or (x[0] > 0 and len(q) == 1)]
        if not good or page_cover < 0.67:
            continue
        top = max(x[0] for x in good)
        key = (top, page_cover, -rank)
        if best is None or key > best[0]:
            best = (key, sorted(good, key=lambda x: x[1]))
    if not best:
        return None
    lines, total = [], 0
    for _, _, u in best[1]:
        if total + len(u) > 900 and lines:
            break
        lines.append(u)
        total += len(u)
    body = "\n".join(lines[:7])
    return body if len(lines) == 1 else "\n".join(("• " + l if not l.startswith(("•", "-")) else l) for l in lines[:7])


def fallback_answer(question, structured_answer, error_kind=None, passages=None):
    """Answer used only when Gemini cannot answer: exact database facts first, then the best
    lines of the RAG passages, and otherwise an honest 'I don't know' (never a wrong page)."""
    closer = "\n\nFeel free to ask me anything else \U0001F60A"
    if structured_answer:
        return structured_answer + closer

    if not brochure._tokens(question):           # "tell me about PCTE" -> the introduction
        intro = brochure.clean_section(brochure._read(os.path.join(brochure.DATA_DIR, "brochure_pcte_about.txt")))
        if intro:
            body = intro.split("\n", 1)[1] if "\n" in intro else intro
            return body.strip()[:1400] + "\n\nAsk me about courses, fees, hostel, placements or events \U0001F60A"

    found = extractive_answer(question, passages or [])
    if found:
        return found + closer

    if error_kind == "quota":
        return "I'm a little busy right now \U0001F64F Please ask again in a minute and I'll be happy to help."

    contact = office_contact()
    extra = f" You can reach the admissions office at {contact}." if contact else ""
    return ("I'm sorry, I couldn't find reliable information about that, and I don't want to give you "
            "a wrong answer." + extra + " I'm here if you have any other question \U0001F60A")


# ---------------------------------------------------------------- diagnostics
def diagnose():
    """Used by /api/ai-status: tells you exactly why Gemini is (not) working.
    Never prints the API key."""
    info = {"api_key_found": False, "sdk_version": None, "working": False, "problems": [],
            "models": MODEL_CANDIDATES, "cooling_down": {m: int(t - time.time()) for m, t in _cooldown.items() if t > time.time()},
            "last_ok_model": _status["last_model"], "last_error": _status["last_error"],
            "startup_check": _status.get("startup")}
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    info["api_key_found"] = bool(key)
    if not key or key == "your_api_key_here":
        info["problems"].append("GEMINI_API_KEY is missing in the .env file (file must be named .env, "
                                "sit next to app.py, and restart the app after editing).")
        return info
    try:
        from importlib.metadata import version
        info["sdk_version"] = version("google-genai")
    except Exception:
        pass
    client = get_client()
    if client is None:
        info["problems"].append(_status["last_error"] or "Could not start the Google AI library. Run: pip install -U google-genai")
        return info
    for model in MODEL_CANDIDATES:
        t0 = time.time()
        try:
            r = client.models.generate_content(model=model, contents="Reply with the single word: OK")
            if (getattr(r, "text", "") or "").strip():
                info.update(working=True, working_model=model, test_seconds=round(time.time() - t0, 2))
                return info
            info["problems"].append(f"{model}: empty reply")
        except Exception as e:
            info["problems"].append(f"{model}: {explain_error(e)}")
    return info
