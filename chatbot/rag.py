"""
rag.py - Retrieval-Augmented Generation engine.

Reads every .txt/.md/.pdf in /data (brochure pages, curated facts) plus the
pages crawled from the official website (data/site_cache.json), splits them
into labelled chunks and finds the passages that really answer a question.

Search is HYBRID, so both exact words and meaning count:
  1. keyword search (TF-IDF with synonyms/stemming: "fess", "salary", "pg"...)
  2. semantic search (sentence-transformers + FAISS) when those packages are
     installed - otherwise it is skipped automatically
The two rankings are merged with Reciprocal Rank Fusion, so a passage that is
good in BOTH rises to the top.

Env var RAG_MODE = auto (default) | simple (keyword only; use this on small
free servers because the semantic model needs ~1 GB RAM) | smart.
"""

import os
import re
import json
import math
from collections import Counter

try:
    from pypdf import PdfReader
    PYPDF_AVAILABLE = True
except ImportError:
    PYPDF_AVAILABLE = False

from chatbot import brochure

RAG_MODE = os.environ.get("RAG_MODE", "auto").strip().lower()

SMART_MODE_AVAILABLE = False
if RAG_MODE != "simple":
    try:
        from sentence_transformers import SentenceTransformer
        import faiss
        import numpy as np
        SMART_MODE_AVAILABLE = True
    except Exception:
        SMART_MODE_AVAILABLE = False

BROCHURE_PREFIX = "[Official brochure"
BROCHURE_BOOST = 1.3          # brochure passages rank a little higher than website pages
RRF_K = 60

_MODEL_CACHE = {"model": None}   # the embedding model is loaded once and reused on rebuilds


def _read_text_file(path):
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


def _read_pdf_file(path):
    if not PYPDF_AVAILABLE:
        return ""
    try:
        reader = PdfReader(path)
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:
        return ""


def _chunk_text(text, chunk_size=300, overlap=50):
    """Split text into overlapping word chunks for better retrieval."""
    words = text.split()
    chunks, start = [], 0
    while start < len(words):
        chunk = " ".join(words[start:start + chunk_size]).strip()
        if chunk:
            chunks.append(chunk)
        start += chunk_size - overlap
    return chunks


def topic_from_filename(filename):
    """brochure_p53_faculty_of_computer_applications_bca.txt -> 'Faculty of computer applications bca'"""
    stem = re.sub(r"\.(txt|md|pdf)$", "", filename)
    stem = re.sub(r"^brochure_", "", stem)
    stem = re.sub(r"^(p\d+[a-z]?_|pcte_)", "", stem)
    return stem.replace("_", " ").strip().capitalize()


def source_of(chunk):
    """Human-readable source of a chunk (for the 'sources' line in the UI)."""
    m = re.match(r"\[Official brochure - ([^\]]+)\]", chunk)
    if m:
        return m.group(1)
    m = re.match(r"\[Official website - ([^\]]+)\]", chunk)
    if m:
        return m.group(1)
    m = re.match(r"\[([^\]]{1,60})\]", chunk)
    return m.group(1) if m else "College data"


class RAGEngine:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.site_cache_path = os.path.join(data_dir, "site_cache.json")
        self.cache_mtime = 0          # mtime of site_cache.json this index was built from
        self.chunks = []              # list[str], each starts with a [label]
        self.mode = "simple"
        self._doc_freq = Counter()
        self._chunk_tokens = []
        self._model = None
        self._index = None

    # ------------------------------------------------------------ loading
    def _load_documents(self):
        docs = []
        if not os.path.isdir(self.data_dir):
            return docs
        for filename in sorted(os.listdir(self.data_dir)):
            path = os.path.join(self.data_dir, filename)
            low = filename.lower()
            if low.endswith(".pdf"):
                content = _read_pdf_file(path)
            elif low.endswith((".txt", ".md")):
                content = _read_text_file(path)
            else:
                continue
            if content.strip().startswith("[SAMPLE DATA"):
                continue                       # un-edited template placeholder
            docs.append((filename, content))
        return docs

    def _load_site_chunks(self):
        chunks = []
        try:
            self.cache_mtime = os.path.getmtime(self.site_cache_path)
            with open(self.site_cache_path, "r", encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            return chunks
        for page in cache.get("pages", []):
            label = f"[Official website - {page.get('url', '')}] {page.get('title', '')}: "
            for piece in _chunk_text(page.get("text", ""), chunk_size=220, overlap=40):
                chunks.append(label + piece)
        return chunks

    def build_index(self):
        chunks = []
        for filename, text in self._load_documents():
            text = brochure.clean_section(text)
            topic = topic_from_filename(filename)
            if filename.lower().startswith("brochure_"):
                label = f"{BROCHURE_PREFIX} - {topic}] "
            else:
                label = f"[{topic}] "
            chunks.extend(label + c for c in _chunk_text(text))
        chunks.extend(self._load_site_chunks())
        self.chunks = chunks
        if not chunks:
            return

        self._build_simple_index()
        if SMART_MODE_AVAILABLE and RAG_MODE in ("auto", "smart"):
            self._build_smart_index()

    # ------------------------------------------------------- simple mode
    def _build_simple_index(self):
        self._chunk_tokens = [brochure._tokens(c) for c in self.chunks]
        self._doc_freq = Counter()
        for tokens in self._chunk_tokens:
            for word in set(tokens):
                self._doc_freq[word] += 1
        self.mode = "simple"

    def _simple_ranked(self, query, limit):
        q = brochure._tokens(query)
        if not q:
            return []
        n_docs = len(self._chunk_tokens) or 1
        scored = []
        for idx, tokens in enumerate(self._chunk_tokens):
            tf = Counter(tokens)
            score = 0.0
            for qt in q:
                if qt in tf:
                    idf = math.log((n_docs + 1) / (self._doc_freq.get(qt, 0) + 1)) + 1
                    score += (1 + math.log(tf[qt])) * idf
            if score > 0:
                if self.chunks[idx].startswith(BROCHURE_PREFIX):
                    score *= BROCHURE_BOOST
                scored.append((score, idx))
        scored.sort(reverse=True)
        return [idx for _, idx in scored[:limit]]

    # -------------------------------------------------------- smart mode
    def _build_smart_index(self):
        try:
            if _MODEL_CACHE["model"] is None:
                _MODEL_CACHE["model"] = SentenceTransformer("all-MiniLM-L6-v2")
            self._model = _MODEL_CACHE["model"]
            emb = self._model.encode(self.chunks, convert_to_numpy=True,
                                     normalize_embeddings=True, show_progress_bar=False)
            self._index = faiss.IndexFlatIP(emb.shape[1])     # cosine similarity
            self._index.add(emb.astype("float32"))
            self.mode = "hybrid"
        except Exception as e:
            print(f"[rag] semantic search unavailable ({type(e).__name__}); using keyword search only.")
            self._model = self._index = None

    def _smart_ranked(self, query, limit):
        if self._model is None or self._index is None:
            return []
        qv = self._model.encode([query], convert_to_numpy=True, normalize_embeddings=True).astype("float32")
        _, ids = self._index.search(qv, limit)
        return [int(i) for i in ids[0] if 0 <= i < len(self.chunks)]

    # --------------------------------------------------------- public API
    def retrieve(self, query, top_k=5):
        """Best chunks for the question (hybrid keyword + semantic, merged by RRF)."""
        if not self.chunks:
            return []
        limit = max(top_k * 3, 12)
        fused = Counter()
        for ranking in (self._simple_ranked(query, limit), self._smart_ranked(query, limit)):
            for rank, idx in enumerate(ranking):
                fused[idx] += 1.0 / (RRF_K + rank)
        best = [idx for idx, _ in fused.most_common(top_k)]
        return [self.chunks[i] for i in best]
