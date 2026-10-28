"""
Campus Services Chatbot - Flask Backend
Main entry point. Run with: python app.py

This app is built to be reusable by ANY college. All branding (name, colors,
tagline) and campus data lives in college_config.json - no code changes
needed to reuse this project.

The college's OFFICIAL WEBSITE (config key "official_site") is the chatbot's
main source of truth: on startup a background crawler reads the site into
data/site_cache.json, and every answer is built from those pages.
"""

import os
import json
import time
import threading
from collections import deque, defaultdict
from flask import Flask, render_template, request, jsonify, Response, stream_with_context
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")
load_dotenv(ENV_PATH)          # always read the .env that sits next to app.py, whatever folder you run from

from chatbot.database import init_db, load_config
from chatbot.rag import RAGEngine
from chatbot.live_fetch import fetch_all_live_pages
from chatbot import site_crawler, ai, pipeline

MAX_MESSAGE_CHARS = 500
RATE_LIMIT = int(os.environ.get("RATE_LIMIT_PER_MIN", "20"))   # questions per minute per visitor

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

app = Flask(__name__)

# Database (seeded from college_config.json on first run)
init_db()

# ---------------------------------------------------------------- RAG engine
# The engine is rebuilt automatically whenever the crawler writes a newer
# data/site_cache.json (even if a different process did the crawling).
_rag = {"engine": None}
_rag_lock = threading.Lock()
_rebuilding = {"flag": False}


def _build_engine():
    engine = RAGEngine(data_dir=DATA_DIR)
    engine.build_index()
    return engine


_rag["engine"] = _build_engine()


def _rebuild_in_background():
    try:
        new_engine = _build_engine()
        with _rag_lock:
            _rag["engine"] = new_engine
        print(f"[app] Knowledge base reloaded ({len(new_engine.chunks)} chunks).")
    finally:
        _rebuilding["flag"] = False


def get_rag():
    """Returns the current engine; kicks off a background rebuild if the
    crawled-site cache has changed since the engine was built."""
    engine = _rag["engine"]
    if site_crawler.cache_mtime() > engine.cache_mtime and not _rebuilding["flag"]:
        _rebuilding["flag"] = True
        threading.Thread(target=_rebuild_in_background, daemon=True).start()
    return engine


# ------------------------------------------- start crawling the official site
def _start_site_crawl():
    config = load_config()
    site = config.get("official_site")
    if not site:
        return
    seeds = config.get("live_pages", [])
    hours = config.get("crawl_refresh_hours", 24)
    started = site_crawler.refresh_in_background(site, seeds, max_age_hours=hours)
    if started:
        print(f"[app] Reading the official website in the background: {site}")


_start_site_crawl()


# Create the Gemini client in the background so the very first question is fast.
threading.Thread(target=ai.warmup, daemon=True).start()

_key = os.environ.get("GEMINI_API_KEY", "").strip()
print(f"[app] .env file: {'found' if os.path.exists(ENV_PATH) else 'NOT FOUND (expected at ' + ENV_PATH + ')'} | "
      f"GEMINI_API_KEY: {'set (' + str(len(_key)) + ' characters)' if _key and _key != 'your_api_key_here' else 'MISSING'}")
if not os.environ.get("SKIP_AI_STARTUP_CHECK"):
    # one tiny test call at startup: prints a clear "Gemini works / does not work, and why"
    threading.Thread(target=ai.startup_check, daemon=True).start()


# ------------------------------------------------------- simple rate limiter
_hits = defaultdict(deque)


def _too_many_requests():
    """Protects the shared free Gemini quota when the site is public."""
    ip = (request.headers.get("X-Forwarded-For", request.remote_addr) or "?").split(",")[0].strip()
    now, q = time.time(), _hits[ip]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        return True
    q.append(now)
    return False


# ------------------------------------------------------------------- routes
@app.route("/")
def home():
    config = load_config()
    return render_template("index.html", config=config)



@app.route("/api/live-updates")
def api_live_updates():
    """Called automatically by the page on load - fetches the live pages
    right now (cached 30 min) so the 'Live from the official website' strip
    can show without the student asking anything."""
    config = load_config()
    live_urls = config.get("live_pages", [])
    if not live_urls:
        return jsonify({"available": False})

    live_text = fetch_all_live_pages(live_urls)
    if not live_text:
        return jsonify({"available": False})

    return jsonify({"available": True, "snippet": live_text[:600]})


def _read_request():
    payload = request.get_json(silent=True) or {}
    message = (payload.get("message") or "").strip()[:MAX_MESSAGE_CHARS]
    history = payload.get("history") or []
    return message, history if isinstance(history, list) else []


@app.route("/api/chat", methods=["POST"])
def chat():
    """Normal (non-streaming) answer - returns the whole reply as JSON."""
    message, history = _read_request()
    if not message:
        return jsonify({"reply": "Please type a question. 🙂"}), 400
    if _too_many_requests():
        return jsonify({"reply": "You're asking very fast 😊 Please wait a few seconds and try again."}), 429
    final = pipeline.answer(message, history, get_rag())
    return jsonify({"reply": final["text"], "mode": final["mode"], "model": final.get("model"),
                    "ms": final["ms"], "sources": final.get("sources", []),
                    "photos": final.get("photos", []), "links": final.get("links", [])})


@app.route("/api/chat-stream", methods=["POST"])
def chat_stream():
    """Streaming answer: one JSON object per line (meta -> delta... -> final).
    The page shows the words while Gemini is still writing them."""
    message, history = _read_request()
    if not message:
        return jsonify({"reply": "Please type a question. 🙂"}), 400
    if _too_many_requests():
        def limited():
            yield json.dumps({"type": "final", "mode": "limited", "ms": 0, "sources": [],
                              "text": "You're asking very fast 😊 Please wait a few seconds and try again."}) + "\n"
        return Response(limited(), mimetype="application/x-ndjson")

    engine = get_rag()

    def generate():
        try:
            for event in pipeline.answer_events(message, history, engine):
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except Exception as e:                      # never leave the page hanging
            print(f"[app] chat-stream error: {type(e).__name__}: {e}")
            yield json.dumps({"type": "final", "mode": "error", "ms": 0, "sources": [],
                              "text": "Sorry, something went wrong on my side 🙏 Please try again."}) + "\n"

    return Response(stream_with_context(generate()), mimetype="application/x-ndjson",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/debug")
def debug():
    """See exactly what RAG finds for a question: /api/debug?q=bca fees"""
    q = (request.args.get("q") or "").strip()[:MAX_MESSAGE_CHARS]
    if not q:
        return jsonify({"usage": "/api/debug?q=your question"})
    return jsonify(pipeline.debug_info(q, None, get_rag()))


@app.route("/api/ai-status")
def ai_status():
    """Open http://127.0.0.1:5000/api/ai-status to see if Gemini is working (and why not)."""
    return jsonify(ai.diagnose())


@app.route("/api/site-status")
def site_status():
    """How much of the official website the chatbot has read so far."""
    status = site_crawler.get_status()
    status["chunks_in_memory"] = len(get_rag().chunks)
    return jsonify(status)


@app.route("/api/refresh-site")
def refresh_site():
    """Force a fresh re-read of the official website.
    Protected: set ADMIN_TOKEN in .env, then open /api/refresh-site?token=YOUR_TOKEN"""
    token = os.environ.get("ADMIN_TOKEN", "")
    if not token or request.args.get("token") != token:
        return jsonify({"error": "Not allowed. Set ADMIN_TOKEN in .env and pass ?token=..."}), 403
    config = load_config()
    site = config.get("official_site")
    if not site:
        return jsonify({"error": "No official_site set in college_config.json"}), 400
    started = site_crawler.refresh_in_background(site, config.get("live_pages", []), force=True)
    return jsonify({"started": started})


@app.route("/api/config")
def api_config():
    """Expose non-sensitive branding config to the frontend if needed dynamically."""
    config = load_config()
    return jsonify({
        "college_name": config.get("college_name"),
        "college_short_name": config.get("college_short_name"),
        "logo_emoji": config.get("logo_emoji"),
        "tagline": config.get("tagline"),
        "primary_color": config.get("primary_color"),
        "secondary_color": config.get("secondary_color"),
        "suggested_questions": config.get("suggested_questions", []),
    })


@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, host="0.0.0.0", port=port)
