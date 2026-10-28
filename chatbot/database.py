"""
database.py
Handles SQLite database creation, seeding, and structured (exact) lookups
for things like fees, courses, hostel charges, library timings and contacts.

Seed data comes from college_config.json at the project root - this means
ANY college can customize the chatbot just by editing that one JSON file,
with zero code changes.

Structured lookup is tried FIRST because it gives 100% accurate answers for
known facts (fees, timings) instead of relying purely on the AI model.
"""

import os
import re
import json
import hashlib
import sqlite3

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, "database.db")
CONFIG_PATH = os.path.join(BASE_DIR, "college_config.json")

_config_cache = None

FEE_WORDS = {"fee", "fees", "fess", "fes", "charges", "charge", "cost", "kharcha", "tuition", "price", "payment"}
NOT_COURSE_FEE_WORDS = {"bus", "buses", "transport", "transportation", "route", "exam", "examination",
                        "counseling", "counselling", "urf", "registration", "form", "pass"}


def load_config():
    """Load college_config.json (cached). Falls back to safe defaults if missing."""
    global _config_cache
    if _config_cache is not None:
        return _config_cache

    defaults = {
        "college_name": "Your College",
        "college_short_name": "College",
        "logo_emoji": "🎓",
        "tagline": "Ask me about admission, fees, hostel, library & more",
        "primary_color": "#6C5CE7",
        "secondary_color": "#00B894",
        "footer_text": "Campus Assistant",
        "courses": [], "hostel": [], "library": [], "contacts": [], "faqs": [],
        "suggested_questions": [],
    }

    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            defaults.update(data)
        except Exception:
            pass

    _config_cache = defaults
    return _config_cache


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _config_hash():
    try:
        with open(CONFIG_PATH, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return ""


DATA_TABLES = ("courses", "hostel", "library", "contacts", "faqs")


def init_db():
    """Create tables and (re)seed them from college_config.json.

    The seed is repeated automatically whenever college_config.json changes
    (its hash is stored in the DB), so an old database.db can never keep
    serving stale fees/contacts - no need to delete the file by hand."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_message TEXT,
            bot_reply TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    current = _config_hash()
    row = cur.execute("SELECT value FROM meta WHERE key='config_hash'").fetchone()
    if row and row[0] == current:
        conn.commit(); conn.close()
        return

    for t in DATA_TABLES:
        cur.execute(f"DROP TABLE IF EXISTS {t}")
    cur.executescript(
        """
        CREATE TABLE courses (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, duration TEXT, fee INTEGER);
        CREATE TABLE hostel (id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL, fee INTEGER, details TEXT);
        CREATE TABLE library (id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, timing TEXT);
        CREATE TABLE contacts (id INTEGER PRIMARY KEY AUTOINCREMENT, department TEXT NOT NULL, person TEXT, phone TEXT, email TEXT);
        CREATE TABLE faqs (id INTEGER PRIMARY KEY AUTOINCREMENT, question TEXT NOT NULL, answer TEXT NOT NULL);
        """
    )
    config = load_config()
    cur.executemany("INSERT INTO courses (name, duration, fee) VALUES (?, ?, ?)",
        [(c["name"], c.get("duration", ""), c.get("fee", 0)) for c in config.get("courses", [])])
    cur.executemany("INSERT INTO hostel (type, fee, details) VALUES (?, ?, ?)",
        [(h["type"], h.get("fee", 0), h.get("details", "")) for h in config.get("hostel", [])])
    cur.executemany("INSERT INTO library (day, timing) VALUES (?, ?)",
        [(l["day"], l.get("timing", "")) for l in config.get("library", [])])
    cur.executemany("INSERT INTO contacts (department, person, phone, email) VALUES (?, ?, ?, ?)",
        [(c["department"], c.get("person", ""), c.get("phone", ""), c.get("email", "")) for c in config.get("contacts", [])])
    cur.executemany("INSERT INTO faqs (question, answer) VALUES (?, ?)",
        [(f["question"], f["answer"]) for f in config.get("faqs", [])])
    cur.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('config_hash', ?)", (current,))
    conn.commit(); conn.close()


def _fetch_all(query, params=()):
    conn = get_db_connection()
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(row) for row in rows]



def _courses_named_in(msg, courses):
    """If the student mentions a specific course ("BCA", "btech civil", "MBA-IB"),
    return only that course so the fee answer is exact, not the whole list."""
    plain = re.sub(r"[^a-z0-9\s&-]", "", msg.lower().replace(".", ""))
    words = set(re.findall(r"[a-z0-9&-]+", plain))
    # 1-3 consecutive words glued together ("d pharmacy" -> "dpharmacy", "mba ib" -> "mbaib").
    # Whole-word windows only - so "kab bana" can never accidentally contain "bba".
    seq = re.findall(r"[a-z0-9&-]+", plain)
    windows = set()
    for i in range(len(seq)):
        acc = ""
        for w in seq[i:i + 3]:
            acc += w
            windows.add(acc)
    matched, strong = [], []
    for c in courses:
        name = c["name"].lower().replace(".", "")
        first = re.split(r"[\s(]", name)[0]
        inside = re.findall(r"\(([^)]*)\)", name)
        aliases = {first}
        aliases.update(a.strip().replace(" ", "") for a in inside if len(a.strip()) <= 12)
        specific = {a.strip().replace(" ", "") for a in inside if len(a.strip()) <= 12}
        if any(a in windows for a in specific if len(a) >= 3):
            strong.append(c)                    # e.g. "MBA-IB", "BAJMC" typed exactly
        if aliases & words or any(a in windows for a in aliases if len(a) >= 3):
            matched.append(c)
    if strong:
        return strong
    if len(matched) > 1:
        # several share the same prefix (e.g. all B.Tech): narrow by extra words
        def hits(c):
            extra = set(re.findall(r"[a-z]{2,}", c["name"].lower().replace(".", ""))) - {"btech", "and", "the"}
            return len(extra & words)
        best = max(hits(c) for c in matched)
        if best > 0:
            matched = [c for c in matched if hits(c) == best]
    return matched


def _bus_pass_answer(msg):
    path = os.path.join(BASE_DIR, "data", "brochure_pcte_bus_pass.txt")
    try:
        text = open(path, encoding="utf-8").read()
    except Exception:
        return None
    routes = []
    for m in re.finditer(r"([A-Za-z][A-Za-z0-9\.,/ \(\)&'\-]*?)\s+(\d{4,5}) / (\d{4,5})", text.split("\n", 1)[-1]):
        name = re.sub(r"^.*?\bOne Side\)\s*", "", m.group(1)).strip(" .,")
        routes.append((name, int(m.group(2)), int(m.group(3))))
    if not routes:
        return None
    asked = [w for w in re.findall(r"[a-z]{3,}", msg)
             if w not in {"bus", "pass", "fee", "fees", "fess", "fare", "for", "the", "kitni", "kitna",
                          "hai", "batao", "from", "route", "transport", "price", "cost", "what", "how",
                          "much", "charges", "pcte", "college", "area", "stop", "tell", "about", "buses"}]
    hit = [r for r in routes if any(w in r[0].lower() for w in asked)] if asked else []
    chosen = hit or routes
    lines = [f"• {n}: Both side Rs. {b:,} | One side Rs. {o:,}" for n, b, o in chosen]
    note = "" if hit else "\n\nNew rates may be added as required; diesel rate changes may affect bus pass charges."
    return "Bus pass fare 2026:\n" + "\n".join(lines) + note


def search_structured_data(user_message: str):
    """
    Very simple keyword-based router to structured tables.
    Returns a formatted string answer if a match is found, else None.
    This runs BEFORE the AI model so factual data (fees/timings) is always
    100% accurate and never 'hallucinated' - but ONLY for rows the admin has
    actually filled in with real data. Placeholder "SAMPLE" rows (left over
    from the template) are ignored here so the bot doesn't present fake demo
    data as if it were real - the AI/search step handles those cases instead.
    """
    msg = user_message.lower()

    def _real(rows):
        """Filter out un-edited placeholder/SAMPLE rows from college_config.json."""
        return [r for r in rows if "sample" not in " ".join(str(v) for v in r.values()).lower()]

    # Bus pass / transport (before "fee" so "bus fees" never shows course fees)
    if ("hostel" not in msg) and (set(re.findall(r"[a-z]+", msg)) & {"bus", "buses", "transport", "transportation"}
                                  or "bus pass" in msg):
        ans = _bus_pass_answer(msg)
        if ans:
            return ans

    # Hostel (checked before generic "fee" so "hostel fee" doesn't match courses)
    if "hostel" in msg:
        hostel = _real(_fetch_all("SELECT * FROM hostel"))
        if hostel:
            lines = []
            for h in hostel:
                fee_part = f"₹{h['fee']:,} - " if h["fee"] else ""
                lines.append(f"• {h['type']}: {fee_part}{h['details']}")
            return "Hostel options:\n" + "\n".join(lines)

    words = set(re.findall(r"[a-z0-9]+", msg))
    asks_fee = bool(words & FEE_WORDS) or any(w.startswith("fee") for w in words)
    other_money = bool(words & NOT_COURSE_FEE_WORDS) or "bus pass" in msg

    # Which specific course (if any) is the student talking about?
    all_courses = _real(_fetch_all("SELECT * FROM courses"))
    named = _courses_named_in(msg, [c for c in all_courses if c["fee"]]) if all_courses else []

    # Courses / Fees  (bus pass / exam fee / transport questions are NOT course fees)
    if asks_fee and not other_money:
        courses = [c for c in _real(_fetch_all("SELECT * FROM courses")) if c["fee"]]
        if courses:
            if named:
                courses = named
            lines = [f"{c['name']}: ₹{c['fee']:,} ({c['duration']})" for c in courses]
            head = "Fee details:" if named else "Here are our course fees:"
            only_dpharm = bool(named) and all("d.pharmacy" in c["name"].lower() for c in named)
            extra = "" if only_dpharm else (
                "\n\nAdditional charges (all courses except D.Pharmacy): Rs. 2,000 one-time "
                "P.T.U. counseling fee, Rs. 3,000 examination fee per semester and Rs. 1,600 URF "
                "per semester. Subject to revision as per P.T.U. notification.")
            return head + "\n" + "\n".join(lines) + extra

    if not named and any(word in msg for word in ["course", "courses", "programs", "programmes"]):
        courses = _real(_fetch_all("SELECT * FROM courses"))
        if courses:
            lines = [f"{c['name']} - {c['duration']}" for c in courses]
            return "We offer the following courses:\n" + "\n".join(lines)

    # Library
    if "library" in msg:
        library = _real(_fetch_all("SELECT * FROM library"))
        if library:
            lines = [f"{l['day']}: {l['timing']}" for l in library]
            return "Library timings:\n" + "\n".join(lines)

    # Contacts
    if any(word in msg for word in ["contact", "phone", "email", "number", "instagram", "website"]):
        contacts = _real(_fetch_all("SELECT * FROM contacts"))
        if contacts:
            lines = []
            for c in contacts:
                bits = [b for b in [c["phone"], c["email"]] if b]
                who = f" ({c['person']})" if c["person"] else ""
                lines.append(f"{c['department']}{who}: {' / '.join(bits)}")
            return "Contact details:\n" + "\n".join(lines)

    # A question about one specific course (eligibility, seats, duration...) is answered
    # from that course's own brochure page, not from a general FAQ.
    if named:
        return None

    # FAQs - best match wins. Words are compared after synonym/stemming clean-up, so
    # "kab bana" matches "established", "fess" matches "fee", etc.
    from chatbot.brochure import _tokens
    faqs = _real(_fetch_all("SELECT * FROM faqs"))
    msg_tokens = set(_tokens(user_message))
    faq_tokens = [set(_tokens(f["question"])) for f in faqs]
    best_faq, best_score, best_toks = None, 0, set()
    for faq, toks in zip(faqs, faq_tokens):
        score = len(msg_tokens & toks)
        if score > best_score:
            best_faq, best_score, best_toks = faq, score, toks
    # A solid match shares 2+ meaningful words. A FAQ with only 1-2 meaningful words
    # (like "How many campuses does PCTE have") is matched when ALL of them appear.
    WEAK = {"year", "page", "event", "student", "course", "program", "faculty", "available", "duration"}
    if best_faq:
        shared = msg_tokens & best_toks
        own_strong = best_toks - WEAK
        if best_score >= 2 or (own_strong and len(own_strong) <= 2 and own_strong <= shared):
            return best_faq["answer"]

    return None


def log_chat(user_message: str, bot_reply: str):
    conn = get_db_connection()
    conn.execute(
        "INSERT INTO chat_history (user_message, bot_reply) VALUES (?, ?)",
        (user_message, bot_reply),
    )
    conn.commit()
    conn.close()
