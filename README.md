# 🎓 Campus Assistant Chatbot

AI-powered chatbot for campus services — admission, fees, courses, hostel,
library, exam schedule, placement, scholarships, faculty and contact details.

Built so that **any college can reuse this project as-is**. All branding
(name, colors, tagline) and campus data (courses, fees, hostel, library,
contacts, FAQs) lives in one file — `college_config.json` — so there is
**zero code editing required** to set it up for a different college.

**Tech stack:** Flask (Python) + SQLite + Gemini API + RAG (FAISS +
Sentence Transformers, with automatic keyword-search fallback) + HTML/CSS/JS
frontend.

---

## 🚀 Quick Start

```bash
# 1. Create a virtual environment (recommended)
python -m venv venv
venv\Scripts\activate        # Windows
source venv/bin/activate     # Mac/Linux

# 2. Install dependencies
pip install -r requirements.txt

# (optional, better search) semantic RAG - needs ~1 GB RAM:
pip install -r requirements-smart.txt

# 3. Add your Gemini API key
copy .env.example .env       # Windows
cp .env.example .env         # Mac/Linux
# then open .env and paste your key (free at https://aistudio.google.com/apikey)

# 4. Run the app
python app.py
```

Open **http://127.0.0.1:5000** in your browser. Done.

> No Gemini key? The app still works — it automatically falls back to
> rule-based answers pulled straight from `college_config.json`, so it never
> crashes and is always demo-ready.

---

## 🏫 Using this for YOUR college (no coding needed)

Everything customizable lives in **`college_config.json`** at the project
root. Open it in any text editor and change:

| Field | What it controls |
|---|---|
| `college_name`, `college_short_name` | Shown in the chat header and browser tab |
| `logo_emoji` | The badge icon in the header (use any emoji) |
| `primary_color`, `secondary_color` | Chat theme colors (hex codes) |
| `tagline`, `footer_text` | Small text under the college name / footer |
| `courses`, `hostel`, `library`, `contacts`, `faqs` | The actual data the bot answers from |
| `suggested_questions` | The quick-tap question chips shown to students |

After editing, **delete `database.db`** (if it already exists) so the app
re-seeds the database from your updated config, then run `python app.py`
again.

You can also drop extra `.txt` or `.pdf` files into the `data/` folder —
they're automatically picked up by the RAG engine as additional knowledge
the chatbot can search through (prospectus, notices, handbooks, etc.).

---

## 📂 Folder Structure

```
CampusChatbot/
├── app.py                    # Flask entry point
├── college_config.json       # ⭐ Edit this to customize for your college
├── requirements.txt
├── .env.example               # copy this to .env and add your Gemini key
├── database.db                 # auto-created on first run
├── chatbot/
│   ├── __init__.py
│   ├── ai.py                  # Gemini integration + rule-based fallback
│   ├── rag.py                 # Retrieval Augmented Generation engine
│   └── database.py            # SQLite setup + structured search (reads college_config.json)
├── templates/
│   └── index.html             # Chat UI
├── static/
│   ├── style.css              # Chat UI styling
│   └── script.js               # Chat UI behaviour
├── data/                      # Knowledge base (txt/pdf) used by RAG
│   ├── admission.txt
│   ├── placement.txt
│   └── scholarship.txt
└── README.md
```

---

## 🩺 Gemini is not working? (read the terminal first)

When you start `python app.py`, the terminal prints one clear verdict within a few seconds:

- `[AI] ✅ Gemini is connected and working (model gemini-2.5-flash-lite, 0.6s)` - all good.
- `[AI] ❌ ... GEMINI_API_KEY is missing` - the file **`.env`** is missing or in the wrong place.
  It must be named exactly `.env`, sit **next to `app.py`**, and contain one line:
  `GEMINI_API_KEY=your_real_key` (no quotes, no spaces).
  **If you replaced the project folder with a new zip, your old `.env` was deleted - create it again.**
- `[AI] ❌ ... key is WRONG or expired` - make a new key at https://aistudio.google.com/apikey.
- `[AI] ❌ ... limit reached` - Google's free limit; it resets daily (try again later).
- `[AI] ❌ ... No internet / firewall` - check your connection or VPN.

The line `[app] .env file: found | GEMINI_API_KEY: set (39 characters)` shows what the app can see.
More detail any time: http://127.0.0.1:5000/api/ai-status

While the AI is offline the chatbot still answers from its notes (exact fees, hostel, contacts, FAQs and
the best lines found by RAG), and the caption under the answer says why the AI did not answer.

---

## ⚡ How an answer is made (fast + accurate)

```
question ──► greeting? ──────────────► instant friendly reply (no AI)
   │         asked before? ──────────► instant answer from cache
   ▼
 1. DATABASE   exact fees / seats / hostel / bus fares / contacts / FAQs
 2. RAG        hybrid search (keywords + meaning) over the brochure pages,
               curated facts and the crawled website  -> best 3-5 passages
 3. GEMINI     gets ONLY those passages (~1,500 tokens), writes the answer
               and STREAMS it word by word
 4. If Gemini is busy/unavailable -> instant answer from the same data
```

Why it is fast now: the old design sent the **whole brochure (~29,000 tokens)** with
every question; now only the passages RAG found are sent (~1,500 tokens). That is
about 15x smaller, so the first words appear in ~1 second and the free quota
(250,000 tokens/minute, shared by all visitors) lasts far longer.

**See that RAG + Gemini are really used**
- Every AI answer shows a small caption: `✨ Gemini + RAG · 4 sources · 1.1s`
  (hover it to see which pages the answer was based on).
- `http://127.0.0.1:5000/api/ai-status` - is Gemini working? (and the exact reason if not)
- `http://127.0.0.1:5000/api/debug?q=BCA fees` - what RAG found and how many tokens go to Gemini.

**Models** (fastest first, each failing model is skipped for a while): `gemini-2.5-flash-lite`
(free: 1,000 requests/day) -> `gemini-2.5-flash` (250/day) -> `gemini-flash-lite-latest` -> `gemini-flash-latest`.
Change the list with `GEMINI_MODELS=...` in `.env`.

**Follow-up questions work** ("tell me about BHMCT" -> "and its seats?") because the last few
messages are sent along. Repeated questions come from a cache.

**Data lives in two places**
- `college_config.json` - courses, fees, hostel, contacts, FAQs. After editing just restart:
  the database re-seeds itself automatically (no need to delete `database.db`).
- `data/` - one text file per brochure page + curated facts. Add or edit `.txt` files here
  to teach the chatbot new things (restart the app).

---

## 🛠 Tools to Install

- **VS Code** — coding
- **Python 3.10+** — programming
- **Git** — version control (optional)
- **Google Chrome** — testing

## 📚 Python Libraries

```
flask
python-dotenv
requests
pypdf
google-genai
sentence-transformers   (optional — smart semantic search)
faiss-cpu               (optional — smart semantic search)
```

If `sentence-transformers` / `faiss-cpu` fail to install on your Python
version, that's fine — just skip them (`pip install flask python-dotenv
requests pypdf google-genai`) and the app automatically uses a lightweight
keyword-search fallback instead. No functionality is lost for demo/viva
purposes.

---

## 🎤 Viva Questions (Quick Reference)

**Q. Why did you use Flask?**
Flask is lightweight, easy to learn, and ideal for REST APIs and chatbot
backends.

**Q. Why Python?**
Python has excellent AI/ML libraries, making it the preferred language for
AI applications.

**Q. Why Gemini?**
It provides natural language understanding so the chatbot can understand
different phrasings of the same question, in English or Hinglish.

**Q. Why SQLite?**
Simple, lightweight, and doesn't require a separate database server — ideal
for a student project.

**Q. What is RAG?**
Retrieval-Augmented Generation — the chatbot searches the college's own
documents first, then uses the AI model to generate an accurate answer
grounded in that retrieved context, instead of relying only on the model's
general knowledge.

**Q. How is this reusable for other colleges?**
All college-specific branding and data is externalized into
`college_config.json`. Swapping that one file (and the files in `data/`)
is enough to redeploy the same codebase for a different institution — no
code changes required.

---

## 🌐 Answers come from the OFFICIAL website

The chatbot treats the college's official site (`official_site` in
`college_config.json`, currently https://pcte.edu.in) as its main source of
truth:

1. When the app starts, `chatbot/site_crawler.py` reads the site **in the
   background** - sitemap, every internal page it can reach, and linked PDFs
   (prospectus / fee structure if published) - and saves the text into
   `data/site_cache.json`. It obeys `robots.txt`, waits between requests and
   never leaves the college's own domain.
2. `rag.py` loads that cache, so every question is answered from those pages
   (with the page URL shown as the source). The knowledge base **reloads
   itself** the moment a newer cache is written - no restart needed.
3. The site is re-read automatically every 24 hours (`crawl_refresh_hours`).
   Check progress any time at `http://127.0.0.1:5000/api/site-status`.
   To force a refresh now: set `ADMIN_TOKEN` in `.env`, then open
   `/api/refresh-site?token=YOUR_TOKEN`.
4. The crawled pages join the brochure in RAG, so Gemini can use them whenever
   they match the question.

**Limitation (honest):** pages built entirely with JavaScript (the PCTE
homepage is one) look empty to any simple crawler, so they are skipped. The
brochure pages in `data/` cover those topics.

---

## 🌍 Deploying online (so anyone can use it, not just you)

By default this runs on your own computer only (`127.0.0.1:5000`). To let
anyone open it from their own phone/laptop via a public link, deploy it to
**Render.com** (free, no credit card needed):

1. **Create a GitHub account** (github.com) if you don't have one, and
   **create a new repository** (e.g. `campus-chatbot`).
2. **Push this project to that repository** (do NOT push your real `.env`
   file - `.gitignore` already excludes it).
3. **Create a Render account** (render.com) - sign up with your GitHub
   account for the easiest setup.
4. On Render, click **New +** → **Web Service**, and connect your GitHub
   repo.
5. Render auto-detects this as a Python app. Set:
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: leave as in the `Procfile` (`gunicorn app:app --worker-class gthread ...`)
6. Under **Environment Variables**, add `GEMINI_API_KEY` with your real key and
   `RAG_MODE` = `simple` (the free server has too little memory for the semantic
   model; keyword search + brochure-page routing is already very good). NEVER put
   your real key in a file you push to GitHub.
7. Click **Create Web Service**. After a few minutes you'll get a public
   URL like `https://campus-chatbot.onrender.com` that anyone can open.

**Good to know:**
- The free tier sleeps after 15 minutes of no visitors, so the first
  visitor after a gap waits ~30-60 seconds for it to wake up - normal for
  a free student project, not a bug.
- The database resets on redeploys (free tier has no permanent storage) -
  it just reseeds itself fresh from `college_config.json` each time, so
  nothing breaks, you just won't keep old chat logs long-term.
- Free Gemini API quota is shared across EVERYONE using your public link,
  not just you. The app limits each visitor to 20 questions/minute
  (`RATE_LIMIT_PER_MIN`) and caches repeated questions to protect it.

---

## ✅ Features

- AI Chatbot (Gemini-powered, with offline fallback)
- Admission, Fee, Hostel, Library, Course, Exam, Placement, Faculty,
  Scholarship & Contact info
- RAG search across uploaded college documents
- One-file customization for any college (`college_config.json`)
- Suggested quick-question chips
- Responsive, modern chat interface
- Chat logging (SQLite)

## Campus photos ("Show me Campus 2 photos")

Photos are served from `campus_photos.json` only. Nothing is guessed or scraped at answer time.
An entry is shown only if it has `campus` ("1" or "2"), `verified: true`, an https `url` and
`source_page` on an allowed official host, a `caption`, and an `evidence` note recording how the
official page establishes which campus it shows (filename or domain alone is not evidence).
If a campus has no verified photos the bot says so and links the official site; it never shows the
other campus's photos instead. "Show more ..." continues from the previous photo reply.

    python tools/verify_photos.py          # needs internet: checks rules + that every image loads
    python -m unittest tests.test_campus_photos
