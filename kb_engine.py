#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kb_engine.py — lightweight local RAG engine (v2, keyword BM25 + vector hybrid).

Design goals (from kb_system_design.md):
  - structured chunking: papers split by section (Abstract x1.5 / Methods x1.2),
    generic docs split by markdown heading or paragraph fallback;
  - incremental ingest: sha256 + size, unchanged files are skipped;
  - hybrid retrieval: in-memory BM25 (CJK-friendly) + local bge-small embeddings
    (FAISS IndexFlatIP) fused with RRF, metadata SQL pre-filter;
  - token economy: snippet-only results with exact provenance, query cache.

Protocol (DSH plugin <-> engine):
  python kb_engine.py <command>
  request JSON on stdin (UTF-8), response JSON on stdout (ensure_ascii).

Commands:
  ingest  {paths:[...], kb_root?:str, force?:bool}
  search  {query:str, top_k?:int, snippet?:int, mode?:'keyword'|'vector'|'hybrid',
           filters?:{authors?,title?,journal?,kind?,year?,section?}, kb_root?:str, cache?:bool}
  rag     {query:str, top_k?:int, filters?:{...}, kb_root?:str}
  stats   {kb_root?:str}
"""

import array
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

VERSION = "3.0.0"
SUPPORTED_EXTS = {".pdf", ".txt", ".md", ".markdown", ".docx"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
  id INTEGER PRIMARY KEY,
  path TEXT NOT NULL UNIQUE,
  title TEXT, authors TEXT, year INTEGER, journal TEXT, doi TEXT,
  kind TEXT, sha256 TEXT, size INTEGER, mtime REAL,
  chunk_count INTEGER, indexed_at REAL
);
CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY,
  doc_id INTEGER NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
  section TEXT NOT NULL,
  weight REAL NOT NULL DEFAULT 1.0,
  seq INTEGER NOT NULL,
  text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vecs (
  chunk_id INTEGER PRIMARY KEY,
  vec BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS cache (
  key TEXT PRIMARY KEY,
  payload TEXT NOT NULL,
  created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
"""

# ---------------------------------------------------------------- sectioning

# (pattern, canonical section, weight) — first match wins.
SECTION_MAP = [
    (re.compile(r"abstract", re.I), "Abstract", 1.5),
    (re.compile(r"摘要"), "Abstract", 1.5),
    (re.compile(r"introduction|intro\b|background|related\s+work", re.I), "Introduction", 1.0),
    (re.compile(r"引言|背景|概述"), "Introduction", 1.0),
    (re.compile(r"methods?|materials?\s*(?:and|&)?\s*methods?|methodology|experiments?", re.I), "Methods", 1.2),
    (re.compile(r"方法|实验"), "Methods", 1.2),
    (re.compile(r"results?", re.I), "Results", 1.0),
    (re.compile(r"结果"), "Results", 1.0),
    (re.compile(r"discussions?", re.I), "Discussion", 1.0),
    (re.compile(r"讨论"), "Discussion", 1.0),
    (re.compile(r"conclusions?|summary", re.I), "Conclusion", 1.0),
    (re.compile(r"结论|总结|小结"), "Conclusion", 1.0),
    (re.compile(r"references|bibliography", re.I), "References", 0.0),
    (re.compile(r"参考文献"), "References", 0.0),
    (re.compile(r"acknowledg", re.I), "Acknowledgements", 0.0),
    (re.compile(r"致谢|附录|appendix", re.I), "Appendix", 0.0),
]

NUM_PREFIX = r"(?:\d+(?:\.\d+)*[\.\)、]?|[一二三四五六七八九十百]+[、．.])"

CAPTION_RE = re.compile(
    r"^\s*(fig(?:ure)?\.?\s*\d+|table\.?\s*\d+|scheme\.?\s*\d+|图\s*\d+|表\s*\d+)", re.I)

# ---------------------------------------------------------------- helpers


def clean(text):
    text = re.sub(r"\s+", " ", text)
    return text.replace("\x00", "").strip()


def match_section_prefix(line, allow_long=False):
    """Return (section, weight, rest_after_heading) if `line` starts with a heading.
    allow_long=True consumes the heading phrase even when body text follows inline
    (Science-style papers embed headings mid-paragraph)."""
    s = line.strip()
    if not s:
        return None
    if s.startswith("#"):
        s = s.lstrip("#").strip()
        for pat, name, weight in SECTION_MAP:
            m = pat.match(s)
            if m:
                return name, weight, s[m.end():].strip()
        return s[:40], 1.0, ""
    s2 = re.sub(r"^\s*" + NUM_PREFIX + r"\s*", "", s)
    for pat, name, weight in SECTION_MAP:
        m = pat.match(s2)
        if not m:
            continue
        if allow_long or len(s2) <= 80:
            return name, weight, s2[m.end():].strip()
    return None


INLINE_HEAD_RE = re.compile(
    r"(?<=[.!?])\s+(?=(?:Abstract|Introduction|Results|"
    r"Discussion(?:\s+and\s+outlook)?|Conclusions?|"
    r"Materials?\s+and\s+methods?|Methods|Outlook|Summary)\b)")


def split_inline_headings(p):
    """Split a paragraph at capitalized heading phrases embedded mid-paragraph."""
    return [s.strip() for s in INLINE_HEAD_RE.split(p)]


def split_long(text, limit=1000):
    """Split an overlong chunk at sentence boundaries."""
    parts, buf = [], ""
    for m in re.split(r"(?<=[。；.!?])\s+", text):
        if len(buf) + len(m) + 1 > limit and buf:
            parts.append(buf)
            buf = m
        else:
            buf = (buf + " " + m).strip()
    if buf:
        parts.append(buf)
    return parts


def _promote_abstract(sectioned):
    """Science-style papers often lack a literal 'Abstract' heading: promote the
    first long prose paragraph of a BOUNDED front-matter block to Abstract x1.5."""
    for i, (sec, w, paras) in enumerate(sectioned):
        if sec != "Front matter" or len(paras) < 2:
            continue
        if not any(s2 != "Front matter" for s2, _, _ in sectioned[i + 1:]):
            continue  # unbounded: the whole doc fell into front matter
        for j, p in enumerate(paras):
            if j == 0:
                continue
            if 400 <= len(p) <= 3000:
                out = list(sectioned[:i])
                if j > 0:
                    out.append((sec, w, paras[:j]))
                out.append(("Abstract", 1.5, paras[j:]))
                out.extend(sectioned[i + 1:])
                return out
        break  # only the first front-matter block is the paper header
    return sectioned


def chunk_document(full_text):
    """Section-aware chunking; falls back to paragraph merging."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", full_text) if p.strip()]
    sectioned = []  # (section, weight, [paragraphs])
    section, weight = "Front matter", 1.0
    buf = []
    structured = False

    def flush():
        nonlocal section, weight, buf
        if buf:
            sectioned.append((section, weight, buf))
        buf = []

    for p in paragraphs:
        for j, seg in enumerate(split_inline_headings(p)):
            if not seg:
                continue
            hit = match_section_prefix(seg, allow_long=(j > 0))
            if hit is not None:
                structured = True
                flush()
                section, weight, rest = hit
                if rest:
                    buf.append(rest)
                continue
            if CAPTION_RE.match(seg):
                structured = True
                flush()
                section, weight = "Figure/Table", 1.0
                buf.append(seg)
                flush()
                section, weight = "Front matter", 1.0
                continue
            buf.append(seg)
    flush()

    if not structured:
        return fallback_chunks(full_text)

    sectioned = _promote_abstract(sectioned)

    chunks = []
    for sec, w, paras in sectioned:
        text = clean(" ".join(paras))
        if len(text) < 40 or w <= 0:
            continue
        if len(text) > 1200:
            chunks.extend((sec, w, piece) for piece in split_long(text))
        else:
            chunks.append((sec, w, text))
    return chunks


def fallback_chunks(full_text, low=300, high=800):
    """Paragraph merging with sentence-level splitting for oversized blocks."""
    pieces = []
    for p in (p.strip() for p in re.split(r"\n\s*\n", full_text) if p.strip()):
        if len(p) > high:
            pieces.extend(split_long(p, limit=high))
        else:
            pieces.append(p)
    chunks, buf = [], ""
    for p in pieces:
        if len(buf) + len(p) + 1 > high and len(buf) >= low:
            chunks.append(("Body", 1.0, clean(buf)))
            buf = p
        else:
            buf = (buf + " " + p).strip()
    if buf:
        chunks.append(("Body", 1.0, clean(buf)))
    return chunks


# ---------------------------------------------------------------- extraction


def read_document(path):
    """Return (text, pdf_meta_or_None). Raises on unsupported/corrupt input."""
    ext = Path(path).suffix.lower()
    if ext == ".pdf":
        import fitz  # PyMuPDF
        doc = fitz.open(str(path))
        try:
            text = "\n".join(page.get_text() for page in doc)
            meta = doc.metadata
        finally:
            doc.close()
        return text or "", meta
    if ext in {".txt", ".md", ".markdown"}:
        raw = Path(path).read_bytes()
        for enc in ("utf-8-sig", "utf-8", "gb18030"):
            try:
                return raw.decode(enc), None
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace"), None
    if ext == ".docx":
        try:
            import docx
            d = docx.Document(str(path))
            return "\n".join(p.text for p in d.paragraphs), None
        except ImportError:
            return _docx_fallback(path), None
    raise ValueError(f"unsupported file type: {ext}")


def _docx_fallback(path):
    import zipfile
    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", errors="replace")
        xml = re.sub(r"<w:p[ >]", "\n", xml)
        return re.sub(r"<[^>]+>", "", xml)
    except Exception:
        return ""


_TITLE_BAD = {"untitled", "无标题", "标题", "作者", "author", "unknown", "论文",
              "document", "无题", "title", "untitled document"}
_AUTHOR_BAD = {"作者", "author", "unknown", "authors", "none", "佚名"}
_WORD_PREFIX_RE = re.compile(r"^microsoft\s+word\s*[-–—:：]?\s*", re.I)
_HEAD_SKIP_RE = re.compile(
    r"^(?:abstract\b|introduction\b|doi\b|https?://|www\.|"
    r"fig(?:ure)?\.?\s*\d|table\.?\s*\d|scheme\.?\s*\d|"
    r"corresponding\s+author|received\b|accepted\b|published\b|"
    r"issn\b|isbn\b|copyright\b|©|journal\s+of\b|vol(?:ume)?\.?\s*\d)",
    re.I)
_DIGITONLY_LINE = re.compile(r"^[\d\s\-–—.,;:()\[\]{}]+$")


def _usable_title(s):
    s = (s or "").strip()
    if not s or s.lower() in _TITLE_BAD:
        return None
    s = _WORD_PREFIX_RE.sub("", s).strip()
    if not s or s.lower() in _TITLE_BAD:
        return None
    words = [w for w in s.split() if re.search(r"[A-Za-z]", w)]
    if len(words) < 2 and not re.search(r"[A-Za-z]{4,}", s):
        return None
    if _DIGITONLY_LINE.match(s):
        return None
    return s


def _first_page_title(text):
    """Conservative first-page title heuristic: the first plausible prose line."""
    if not text:
        return None
    for line in text[:3000].split("\n")[:14]:
        l = " ".join(line.split())
        if not (10 <= len(l) <= 320):
            continue
        if _HEAD_SKIP_RE.match(l) or _DIGITONLY_LINE.match(l):
            continue
        words = [w for w in l.split() if re.search(r"[A-Za-z]", w)]
        if len(words) >= 2 or re.search(r"[A-Za-z]{4,}", l):
            return l
    return None


def _clean_authors(s):
    if not s:
        return None
    s = re.sub(r"\s+", " ", s).strip().strip(".,;:")
    if not s or s.lower() in _AUTHOR_BAD:
        return None
    return s or None


def _clean_year(value):
    try:
        y = int(value)
    except (TypeError, ValueError):
        return None
    if 1900 <= y <= datetime.now().year + 1:
        return y
    return None


def extract_meta(path, text, pdf_meta=None):
    title = authors = journal = doi = None
    year = None
    if pdf_meta:
        title = _usable_title(pdf_meta.get("title"))
        authors = _clean_authors(pdf_meta.get("author"))
        m = re.search(r"(?:D:)?(19|20)\d{2}", pdf_meta.get("creationDate") or "")
        year = _clean_year(m.group(0)[-4:] if m else None)
    if not title:
        title = _first_page_title(text)
    if not title:
        title = _usable_title(Path(path).stem) or Path(path).stem
    head = text[:3000]
    m = re.search(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", head)
    if m:
        doi = m.group(0).rstrip(".,;")
    if year is None:
        m = re.search(r"\b(19|20)\d{2}\b", head)
        year = _clean_year(m.group(0)) if m else None
    return title, authors, year, journal, doi


# ---------------------------------------------------------------- storage


def connect(kb_root):
    root = Path(kb_root)
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(root / "kb.sqlite"))
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    db.execute("DELETE FROM vecs WHERE chunk_id NOT IN (SELECT id FROM chunks)")
    return db


# ---------------------------------------------------------------- embeddings

_EMBEDDER = None
_EMBED_ERR = None
_EMBED_NAME = None

BGE_QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："


def get_embedder():
    """Lazy singleton; prefers the local HF cache, never waits on the network."""
    global _EMBEDDER, _EMBED_ERR, _EMBED_NAME
    if _EMBEDDER is not None or _EMBED_ERR is not None:
        return _EMBEDDER
    name = os.environ.get("KB_EMBED_MODEL", "BAAI/bge-small-zh-v1.5")
    try:
        from sentence_transformers import SentenceTransformer
        try:
            model = SentenceTransformer(name, local_files_only=True)
        except Exception:
            model = SentenceTransformer(name)
        _EMBEDDER = model
        _EMBED_NAME = name
    except Exception as e:
        _EMBED_ERR = f"{type(e).__name__}: {e}"[:300]
    return _EMBEDDER


def encode(texts, is_query=False, cjk=False):
    model = get_embedder()
    if model is None:
        raise RuntimeError("embedding model unavailable: " + (_EMBED_ERR or "unknown"))
    prefix = BGE_QUERY_PREFIX if is_query and cjk else ""
    if prefix:
        texts = [prefix + t for t in texts]
    return model.encode(texts, normalize_embeddings=True, batch_size=32,
                        show_progress_bar=False).astype("float32")


def pack_vec(v):
    return array.array("f", v.tolist()).tobytes()


def unpack_vec(b):
    return __import__("numpy").frombuffer(b, dtype="float32")


# ---------------------------------------------------------------- reranker

_RERANKER = None
_RERANK_ERR = None
_RERANK_NAME = None


def get_reranker():
    """Stage-2 scorer: cached bge-reranker-base Cross-Encoder first, then an
    attempted mirror download (bounded), then the local bge-large-en bi-encoder."""
    global _RERANKER, _RERANK_ERR, _RERANK_NAME
    if _RERANKER is not None or _RERANK_ERR is not None:
        return _RERANKER
    name = os.environ.get("KB_RERANK_MODEL", "BAAI/bge-reranker-base")
    try:  # already cached locally?
        from sentence_transformers import CrossEncoder
        _RERANKER = CrossEncoder(name, local_files_only=True)
        _RERANK_NAME = name
        return _RERANKER
    except Exception:
        pass
    try:  # bounded download attempt (HF mirror for CN networks)
        os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "120")
        from sentence_transformers import CrossEncoder
        _RERANKER = CrossEncoder(name)
        _RERANK_NAME = name
        return _RERANKER
    except Exception as e1:
        _RERANK_ERR = f"cross-encoder: {str(e1)[:120]}"
    try:  # offline fallback: large bi-encoder re-scoring
        from sentence_transformers import SentenceTransformer
        _RERANKER = SentenceTransformer("BAAI/bge-large-en-v1.5", local_files_only=True)
        _RERANK_NAME = "BAAI/bge-large-en-v1.5 (bi-encoder)"
        return _RERANKER
    except Exception as e2:
        _RERANK_ERR = f"{_RERANK_ERR}; bi-encoder: {str(e2)[:120]}"
    return None


def rerank(query, texts):
    """Scores (query, text) pairs; returns (scores list, model name)."""
    model = get_reranker()
    if model is None:
        raise RuntimeError("reranker unavailable: " + (_RERANK_ERR or "unknown"))
    if _RERANK_NAME and "bi-encoder" not in _RERANK_NAME:
        import numpy as np
        pairs = [[query, t[:1800]] for t in texts]
        scores = model.predict(pairs, batch_size=16, show_progress_bar=False)
        return np.asarray(scores, dtype="float32").flatten().tolist(), _RERANK_NAME
    import numpy as np
    q = model.encode([query], normalize_embeddings=True)
    d = model.encode([t[:1800] for t in texts], normalize_embeddings=True)
    return (d @ q.T).flatten().tolist(), _RERANK_NAME


# ---------------------------------------------------------------- ingest


def cmd_ingest(req):
    _REL_CENTROID.clear()
    t0 = time.time()
    kb_root = req.get("kb_root") or ".kb"
    force = bool(req.get("force"))
    paths = req.get("paths") or []
    if not paths:
        return {"ok": False, "error": "paths is required (file or directory list)"}
    db = connect(kb_root)
    files = []
    totals = {"added": 0, "updated": 0, "skipped": 0, "errors": 0, "duplicates": 0,
              "chunks": 0, "vectors": 0}
    try:
        for p in paths:
            p = Path(p)
            if not p.exists():
                files.append({"path": str(p), "status": "error", "error": "not found"})
                totals["errors"] += 1
                continue
            candidates = sorted(p.rglob("*")) if p.is_dir() else [p]
            for f in candidates:
                if not f.is_file() or f.suffix.lower() not in SUPPORTED_EXTS:
                    continue
                _ingest_file(db, f, force, files, totals)
        if totals["added"] or totals["updated"]:
            db.execute("DELETE FROM cache")  # any index change invalidates cache
        db.commit()
    finally:
        db.close()
    resp = {
        "ok": True,
        "kb_root": str(Path(kb_root).resolve()),
        "files": files,
        "totals": totals,
        "embedding": _EMBED_NAME if get_embedder() is not None else None,
        "ms": round((time.time() - t0) * 1000),
    }
    return resp


def _embed_new_chunks(db, doc_id):
    """Embed chunks that have no vector yet; returns count (0 when model absent)."""
    if get_embedder() is None:
        return 0
    rows = db.execute(
        "SELECT id, text FROM chunks WHERE doc_id = ? AND id NOT IN "
        "(SELECT chunk_id FROM vecs)", (doc_id,)).fetchall()
    if not rows:
        return 0
    vecs = encode([r["text"] for r in rows])
    db.executemany("INSERT OR REPLACE INTO vecs(chunk_id, vec) VALUES(?, ?)",
                   [(r["id"], pack_vec(v)) for r, v in zip(rows, vecs)])
    return len(rows)


def _ingest_file(db, f, force, files, totals, meta=None):
    t0 = time.time()
    entry = {"path": str(f)}
    try:
        data = f.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        key = str(f.resolve())
        row = db.execute("SELECT id, sha256 FROM docs WHERE path = ?", (key,)).fetchone()
        if row is not None and row["sha256"] == sha and not force:
            n = db.execute(
                "SELECT COUNT(*) AS n FROM chunks WHERE doc_id = ?", (row["id"],)).fetchone()["n"]
            entry.update({"status": "skipped", "chunks": n,
                          "ms": round((time.time() - t0) * 1000)})
            totals["skipped"] += 1
            files.append(entry)
            return
        dup = db.execute("SELECT id, path FROM docs WHERE sha256 = ? AND path != ?",
                         (sha, key)).fetchone()
        if dup is not None and not force:  # same content already indexed elsewhere
            entry.update({"status": "duplicate", "of": dup["path"],
                          "ms": round((time.time() - t0) * 1000)})
            totals["duplicates"] = totals.get("duplicates", 0) + 1
            files.append(entry)
            return
        text, pdf_meta = read_document(f)
        if not text.strip():
            raise ValueError("no text extracted")
        title, authors, year, journal, doi = extract_meta(f, text, pdf_meta)
        if meta:  # authoritative metadata override (e.g. Zotero)
            title = meta.get("title") or title
            authors = meta.get("authors") or authors
            year = meta.get("year") or year
            journal = meta.get("journal") or journal
            doi = meta.get("doi") or doi
        chunks = chunk_document(text)
        seen, uniq = set(), []
        for sec, w, t in chunks:
            h = hashlib.sha1(t.encode("utf-8")).hexdigest()
            if h in seen:
                continue
            seen.add(h)
            uniq.append((sec, w, t))
        chunks = uniq
        st = f.stat()
        if row is not None:
            db.execute(
                "UPDATE docs SET title=?,authors=?,year=?,journal=?,doi=?,kind=?,"
                "sha256=?,size=?,mtime=?,chunk_count=?,indexed_at=? WHERE id=?",
                (title, authors, year, journal, doi, f.suffix.lower().lstrip("."),
                 sha, st.st_size, st.st_mtime, len(chunks), time.time(), row["id"]))
            doc_id = row["id"]
            db.execute("DELETE FROM vecs WHERE chunk_id IN "
                       "(SELECT id FROM chunks WHERE doc_id = ?)", (doc_id,))
            db.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
            status = "updated"
        else:
            cur = db.execute(
                "INSERT INTO docs(path,title,authors,year,journal,doi,kind,sha256,"
                "size,mtime,chunk_count,indexed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (key, title, authors, year, journal, doi, f.suffix.lower().lstrip("."),
                 sha, st.st_size, st.st_mtime, len(chunks), time.time()))
            doc_id = cur.lastrowid
            status = "added"
        db.executemany(
            "INSERT INTO chunks(doc_id,section,weight,seq,text) VALUES(?,?,?,?,?)",
            [(doc_id, sec, w, i, t) for i, (sec, w, t) in enumerate(chunks)])
        n_vec = _embed_new_chunks(db, doc_id)
        totals[status] += 1
        totals["chunks"] += len(chunks)
        totals["vectors"] += n_vec
        entry.update({"status": status, "chunks": len(chunks), "vectors": n_vec,
                      "title": title, "year": year,
                      "ms": round((time.time() - t0) * 1000)})
    except Exception as e:  # per-file failure must not kill the batch
        totals["errors"] += 1
        entry.update({"status": "error", "error": f"{type(e).__name__}: {e}"[:300],
                      "ms": round((time.time() - t0) * 1000)})
    files.append(entry)


# ---------------------------------------------------------------- search

STOP = {"the", "a", "an", "of", "and", "or", "in", "on", "for", "with", "is",
        "are", "was", "were", "be", "to", "by", "from", "as", "at", "that",
        "this", "these", "those", "we", "they", "it", "et", "al", "their", "its"}

FILTER_COLS = {
    "authors": "d.authors", "title": "d.title", "journal": "d.journal",
    "kind": "d.kind", "year": "d.year", "section": "c.section",
}

RRF_K = 60


def extract_terms(query):
    """CJK phrases + bigrams, plus ASCII words (numbers, formulas, terms)."""
    terms, seen = [], set()

    def add(term, kind, w):
        if (term, kind) in seen:
            return
        seen.add((term, kind))
        terms.append((term, kind, w))

    for run in re.findall(r"[\u4e00-\u9fff]+", query):
        r = run.lower()
        add(r, "phrase", 2.0 if len(r) <= 4 else 1.5)
        if len(r) >= 2:
            for i in range(len(r) - 1):
                add(r[i:i + 2], "word", 1.0)
    for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-+.#%]*", query):
        w = w.lower().strip(".-")
        if len(w) < 2 or w in STOP:
            continue
        add(w, "word", 1.0)
    return terms


def build_where(filters):
    where, args = [], []
    for key, col in FILTER_COLS.items():
        v = filters.get(key)
        if v is None or v == "":
            continue
        if key == "year":
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                where.append(f"{col} = ?")
                args.append(int(v))
            elif isinstance(v, str):
                m = re.match(r"^\s*(>=|<=|>|<|=)?\s*(\d{4})\s*$", v)
                if m:
                    where.append(f"{col} {m.group(1) or '='} ?")
                    args.append(int(m.group(2)))
        else:
            where.append(f"{col} LIKE ?")
            args.append(f"%{v}%")
    return (" WHERE " + " AND ".join(where)) if where else "", args


def make_snippet(text, term, width):
    if term is None or term not in text.lower():
        return text[:width] + ("…" if len(text) > width else "")
    i = text.lower().find(term)
    start = max(0, i - width // 3)
    end = min(len(text), start + width)
    pre = "…" if start > 0 else ""
    post = "…" if end < len(text) else ""
    return pre + text[start:end].strip() + post


def keyword_ranking(rows, query):
    """BM25 x section weight over candidate chunks; returns ranked (index, score)."""
    terms = extract_terms(query)
    if not terms:
        return [], "无法从 query 解析出可检索的关键词"
    texts = [r["text"] for r in rows]
    lowered = [t.lower() for t in texts]
    n = len(rows)
    avgdl = sum(len(t) for t in texts) / max(1, n)
    k1, b = 1.2, 0.75
    scores = [0.0] * n
    best_term = [None] * n
    best_idf = [0.0] * n
    for term, _kind, tw in terms:
        df = sum(1 for lt in lowered if term in lt)
        if df == 0:
            continue
        idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
        for i, lt in enumerate(lowered):
            tf = lt.count(term)
            if tf == 0:
                continue
            denom = tf + k1 * (1 - b + b * len(texts[i]) / avgdl)
            scores[i] += idf * tf * (k1 + 1) / denom * tw
            if idf > best_idf[i]:
                best_idf[i] = idf
                best_term[i] = term
    ranked = sorted(((i, scores[i] * rows[i]["weight"]) for i in range(n)
                     if scores[i] > 0), key=lambda x: x[1], reverse=True)
    return ranked, best_term, None


def vector_ranking(rows, qvec, k=30):
    """FAISS IndexFlatIP cosine ranking; returns ranked (index, score)."""
    import faiss
    import numpy as np
    idx_of = []
    mats = []
    for i, r in enumerate(rows):
        v = r["vec"]
        if v is None:
            continue
        arr = unpack_vec(v)
        if arr.shape[0] == 0:
            continue
        idx_of.append(i)
        mats.append(arr)
    if not mats:
        return [], "no vectors indexed (embedding model unavailable at ingest time)"
    index = faiss.IndexFlatIP(mats[0].shape[0])
    index.add(np.vstack(mats))
    k = min(k, len(mats))
    scores, ids = index.search(np.asarray([qvec], dtype="float32"), k)
    ranked = [(idx_of[int(ids[0][j])], float(scores[0][j]))
              for j in range(k) if ids[0][j] >= 0]
    return ranked, None


def rrf_fuse(kw_ranked, v_ranked):
    fused = {}
    for rank, (i, _score) in enumerate(kw_ranked):
        fused[i] = fused.get(i, 0.0) + 1.0 / (RRF_K + rank + 1)
    for rank, (i, _score) in enumerate(v_ranked):
        fused[i] = fused.get(i, 0.0) + 1.0 / (RRF_K + rank + 1)
    return sorted(fused.items(), key=lambda x: x[1], reverse=True)


# ------------------------------------------------------- caption association

_FIGREF_RE = re.compile(r"(?:fig(?:ure|s)?\.?\s*|图\s*)(\d+)([a-zA-Z])?(?!\d)", re.I)
_CAPTION_NUM_RE = re.compile(r"\d+")


def _doc_captions(db, doc_id):
    """{figure_number: caption_text} for one doc, from 'Figure/Table' chunks."""
    caps = {}
    for r in db.execute(
            "SELECT text FROM chunks WHERE doc_id = ? AND section = 'Figure/Table'",
            (doc_id,)).fetchall():
        m = CAPTION_RE.match(r["text"])
        if not m:
            continue
        n = _CAPTION_NUM_RE.search(m.group(1))
        if n:
            caps[n.group(0)] = r["text"]
    return caps


# ------------------------------------------------------- related literature
# (kb_root, doc_id) -> centroid vector (float32 array); cleared on any ingest mutation.
_REL_CENTROID = {}


def _author_tokens(authors):
    return {t.lower() for t in re.split(r"[;,/&]+", authors or "") if t.strip() and
            len(t.strip()) > 1 and not t.strip().lower() in ("et al", "al", "and")}


def _doc_centroid(db, kb_key, doc_id):
    key = (kb_key, doc_id)
    if key in _REL_CENTROID:
        return _REL_CENTROID[key]
    rows = db.execute(
        "SELECT v.vec FROM chunks c JOIN vecs v ON v.chunk_id = c.id "
        "WHERE c.doc_id = ?", (doc_id,)).fetchall()
    mats = []
    for r in rows:
        arr = unpack_vec(r["vec"])
        if arr.shape[0] > 0:
            mats.append(arr)
    if not mats:
        return None
    import numpy as np
    c = np.vstack(mats).mean(axis=0).astype("float32")
    _REL_CENTROID[key] = c
    return c


def related_docs(db, kb_key, seed_doc_ids, related_k=5):
    """Metadata + centroid-similarity association over the docs not already in results."""
    if not seed_doc_ids:
        return []
    import numpy as np
    seeds = []
    for did in seed_doc_ids:
        c = _doc_centroid(db, kb_key, did)
        if c is not None:
            seeds.append(c)
    seed_c = np.vstack(seeds).mean(axis=0) if seeds else None
    seed_meta = {}
    for r in db.execute(
            "SELECT id, authors, journal, year FROM docs WHERE id IN (%s)"
            % ",".join("?" * len(seed_doc_ids)), list(seed_doc_ids)).fetchall():
        seed_meta[r["id"]] = r
    seed_authors = set()
    seed_journals = set()
    seed_years = []
    for sm in seed_meta.values():
        seed_authors |= _author_tokens(sm["authors"])
        if sm["journal"]:
            seed_journals.add(sm["journal"].strip().lower())
        try:
            seed_years.append(int(sm["year"]))
        except (TypeError, ValueError):
            pass
    cands = db.execute(
        "SELECT id, title, authors, year, journal, doi, path FROM docs "
        "WHERE id NOT IN (%s)" % ",".join("?" * len(seed_doc_ids)),
        list(seed_doc_ids)).fetchall()
    scored = []
    for c in cands:
        meta = 0.0
        reasons = []
        shared = _author_tokens(c["authors"]) & seed_authors
        if shared:
            meta += 2.0
            reasons.append("同作者")
        if c["journal"] and c["journal"].strip().lower() in seed_journals:
            meta += 1.5
            reasons.append("同期刊")
        try:
            cy = int(c["year"])
            if seed_years:
                dist = min(abs(cy - sy) for sy in seed_years)
                meta += max(0.0, 1.0 - dist / 10.0)
                if dist <= 3:
                    reasons.append("年份相近")
        except (TypeError, ValueError):
            pass
        vec = 0.0
        if seed_c is not None:
            cc = _doc_centroid(db, kb_key, c["id"])
            if cc is not None:
                vec = float(np.dot(seed_c, cc) / (np.linalg.norm(seed_c) * np.linalg.norm(cc) + 1e-9)) * 2.0
                if vec > 0.7:
                    reasons.append("主题相似")
        score = round(meta + vec, 4)
        if score <= 0:
            continue
        scored.append({
            "file": Path(c["path"]).name,
            "title": c["title"],
            "authors": c["authors"],
            "year": c["year"],
            "journal": c["journal"],
            "doi": c["doi"],
            "score": score,
            "reason": "、".join(reasons[:2]) if reasons else "内容相关",
        })
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:related_k]


def _search_core(db, query, top_k, snippet_w, filters, mode, use_cache, rerank_flag=True,
                  related_flag=True, related_k=5):
    t0 = time.time()
    where, args = build_where(filters)
    rows = db.execute(
        "SELECT c.id AS cid, c.doc_id, c.text, c.section, c.weight, d.title, d.authors, "
        "d.year, d.journal, d.doi, d.path, d.kind, v.vec "
        "FROM chunks c JOIN docs d ON d.id = c.doc_id "
        "LEFT JOIN vecs v ON v.chunk_id = c.id" + where, args).fetchall()

    if not rows:
        return {"query": query, "scored": 0, "results": [],
                "note": "知识库为空或过滤条件过严。先用 kb_ingest 入库，或放宽 filters。",
                "ms": round((time.time() - t0) * 1000), "cached": False}

    # background zh->en translation is intentionally NOT wired in this build;
    # queries are searched as-is (CJK bigrams + ASCII terms both participate).

    cache_key = None
    if use_cache:
        cache_key = hashlib.sha1(json.dumps(
            [query, filters, top_k, snippet_w, mode, rerank_flag, _RERANK_NAME,
             related_flag, related_k],
            sort_keys=True, ensure_ascii=True, default=str).encode("utf-8")).hexdigest()
        hit = db.execute("SELECT payload FROM cache WHERE key = ?", (cache_key,)).fetchone()
        if hit is not None:
            data = json.loads(hit["payload"])
            data["cached"] = True
            data["ms"] = 0
            return data

    mode_used = mode or "hybrid"
    note = ""
    ranked = []
    best_term = None

    if mode_used in ("keyword", "hybrid"):
        kw_ranked, best_term, _err = keyword_ranking(rows, query)
        if mode_used == "keyword":
            ranked = kw_ranked
        else:
            if not kw_ranked:
                note += "关键词无命中；"
            vecs_present = any(r["vec"] is not None for r in rows)
            if not vecs_present:
                mode_used = "keyword"
                ranked = kw_ranked
                note += "向量索引缺失，降级为纯关键词；"
            else:
                try:
                    qvec = encode([query], is_query=True,
                                  cjk=bool(re.search(r"[\u4e00-\u9fff]", query)))[0]
                    v_ranked, _err2 = vector_ranking(rows, qvec, k=max(20, top_k * 5))
                    if _err2:
                        mode_used = "keyword"
                        ranked = kw_ranked
                        note += _err2 + "，降级为纯关键词；"
                    else:
                        fused = rrf_fuse(kw_ranked, v_ranked)
                        ranked = [(i, s * rows[i]["weight"]) for i, s in fused]
                except Exception as e:
                    mode_used = "keyword"
                    ranked = kw_ranked
                    note += f"向量检索失败({str(e)[:120]})，降级为纯关键词；"

    elif mode_used == "vector":
        try:
            qvec = encode([query], is_query=True,
                          cjk=bool(re.search(r"[\u4e00-\u9fff]", query)))[0]
            ranked, _err2 = vector_ranking(rows, qvec, k=max(20, top_k * 5))
            if _err2:
                mode_used = "keyword"
                ranked, best_term, _ = keyword_ranking(rows, query)
                note += _err2 + "，降级为纯关键词；"
        except Exception as e:
            mode_used = "keyword"
            ranked, best_term, _ = keyword_ranking(rows, query)
            note += f"向量检索失败({str(e)[:120]})，降级为纯关键词；"
    else:
        return {"ok": False, "error": f"unknown mode: {mode_used}"}

    # stage 2: rerank the fused pool with a stronger local scorer
    reranker_used = None
    if rerank_flag and len(ranked) > top_k:
        try:
            pool = ranked[:max(20, top_k * 5)]
            cand_idx = [i for i, _s in pool]
            texts = [rows[i]["text"] for i in cand_idx]
            rscores, reranker_used = rerank(query, texts)
            reranked = sorted(zip(cand_idx, rscores), key=lambda x: x[1], reverse=True)
            ranked = [(i, s * rows[i]["weight"]) for i, s in reranked]
        except Exception as e:
            note += f"精排不可用({str(e)[:100]})；"

    results = []
    seed_doc_ids = []
    caption_cache = {}
    for i, score in ranked[:top_k]:
        r = rows[i]
        entry = {
            "file": Path(r["path"]).name,
            "path": r["path"],
            "title": r["title"],
            "authors": r["authors"],
            "year": r["year"],
            "journal": r["journal"],
            "doi": r["doi"],
            "section": r["section"],
            "score": round(score, 4),
            "snippet": make_snippet(r["text"], best_term[i] if best_term else None, snippet_w),
        }
        if r["section"] != "Figure/Table":
            fm = _FIGREF_RE.search(r["text"])
            if fm:
                if r["doc_id"] not in caption_cache:
                    caption_cache[r["doc_id"]] = _doc_captions(db, r["doc_id"])
                caps = caption_cache[r["doc_id"]]
                if fm.group(1) in caps:
                    entry["figure"] = "Fig. %s%s — %s" % (
                        fm.group(1), fm.group(2) or "", caps[fm.group(1)][:140])
        results.append(entry)
        if r["doc_id"] not in seed_doc_ids:
            seed_doc_ids.append(r["doc_id"])

    resp = {"query": query, "scored": len(ranked), "top_k": top_k,
            "mode_used": mode_used, "reranker": reranker_used, "results": results,
            "note": (note + f"命中 {len(ranked)} 块，返回 Top-{len(results)}") if len(ranked) else (note or "无命中"),
            "cached": False,
            "ms": round((time.time() - t0) * 1000)}
    if related_flag and results:
        try:
            kb_key = str(db.execute("PRAGMA database_list").fetchall()[0][2] or "kb")
            if seed_doc_ids:
                resp["related"] = related_docs(db, kb_key, seed_doc_ids, related_k)
        except Exception as e:
            resp["related_error"] = str(e)[:200]
    if use_cache and cache_key is not None and results:
        db.execute("INSERT OR REPLACE INTO cache(key, payload, created) VALUES(?,?,?)",
                   (cache_key, json.dumps(resp, ensure_ascii=True), time.time()))
    return resp


def cmd_search(req):
    t0 = time.time()
    query = (req.get("query") or "").strip()
    if not query:
        return {"ok": False, "error": "query is required"}
    top_k = min(max(int(req.get("top_k") or 5), 1), 10)
    snippet_w = min(max(int(req.get("snippet") or 400), 100), 2000)
    filters = req.get("filters") or {}
    mode = req.get("mode") or "hybrid"
    use_cache = req.get("cache") is not False
    rerank_flag = req.get("rerank") is not False
    related_flag = req.get("related") is not False
    related_k = min(max(int(req.get("related_k") or 5), 1), 10)
    db = connect(req.get("kb_root") or ".kb")
    try:
        resp = _search_core(db, query, top_k, snippet_w, filters, mode, use_cache,
                            rerank_flag, related_flag, related_k)
        db.commit()
    finally:
        db.close()
    resp["ok"] = True
    resp["ms_total"] = round((time.time() - t0) * 1000)
    return resp


def cmd_rag(req):
    """Evidence assembly for the DSH kb_rag tool; generation is the model's job."""
    query = (req.get("query") or "").strip()
    if not query:
        return {"ok": False, "error": "query is required"}
    top_k = min(max(int(req.get("top_k") or 3), 1), 10)
    filters = req.get("filters") or {}
    rerank_flag = req.get("rerank") is not False
    related_flag = req.get("related") is not False
    related_k = min(max(int(req.get("related_k") or 5), 1), 10)
    db = connect(req.get("kb_root") or ".kb")
    try:
        resp = _search_core(db, query, top_k, 600, filters, "hybrid", True,
                            rerank_flag, related_flag, related_k)
        db.commit()
    finally:
        db.close()
    resp["ok"] = True
    resp["evidence"] = resp.pop("results")
    resp["guidance"] = ("基于 evidence 作答，每个事实标注来源编号 [n]（对应 evidence 下标）；"
                        "资料不足时明确回答\"根据现有资料无法回答\"；多源冲突时分别列出；"
                        "答案末尾的补充建议可参考 related 关联文献列表（若相关）。")
    return resp


# ---------------------------------------------------------------- stats


def cmd_stats(req):
    t0 = time.time()
    db = connect(req.get("kb_root") or ".kb")
    try:
        docs_n = db.execute("SELECT COUNT(*) AS n FROM docs").fetchone()["n"]
        chunks_n = db.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
        chars_n = db.execute(
            "SELECT COALESCE(SUM(LENGTH(text)),0) AS n FROM chunks").fetchone()["n"]
        vecs_n = db.execute("SELECT COUNT(*) AS n FROM vecs").fetchone()["n"]
        rows = db.execute(
            "SELECT path,title,authors,year,kind,chunk_count,indexed_at "
            "FROM docs ORDER BY indexed_at DESC LIMIT 200").fetchall()
    finally:
        db.close()
    return {
        "ok": True,
        "db": str((Path(req.get("kb_root") or ".kb") / "kb.sqlite").resolve()),
        "docs": docs_n,
        "chunks": chunks_n,
        "vectors": vecs_n,
        "chars": chars_n,
        "recent": [{
            "file": Path(r["path"]).name, "path": r["path"], "title": r["title"],
            "authors": r["authors"], "year": r["year"], "kind": r["kind"],
            "chunks": r["chunk_count"], "indexed_at": r["indexed_at"],
        } for r in rows],
        "ms": round((time.time() - t0) * 1000),
    }


# ---------------------------------------------------------------- zotero

def _find_zotero_db(explicit):
    import glob
    if explicit:
        p = Path(explicit)
        return str(p) if p.is_file() else None
    for c in [os.path.expanduser("~/Zotero/zotero.sqlite"),
              os.path.expanduser("~/Documents/Zotero/zotero.sqlite")]:
        if os.path.isfile(c):
            return c
    for p in glob.glob(os.path.expandvars(r"%APPDATA%\Zotero\Zotero\Profiles\*\zotero\zotero.sqlite")):
        return p
    return None


def _zotero_entries(zdb):
    """[(absolute_path, meta_dict, parent_type), ...] for every stored PDF attachment."""
    conn = sqlite3.connect(f"file:{zdb}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    data_dir = str(Path(zdb).parent)
    rows = conn.execute("""
        SELECT ia.itemID, ia.parentItemID, ia.path, ia.linkMode, i.key AS itemKey,
               it.typeName AS parentType
        FROM itemAttachments ia
        JOIN items i ON i.itemID = ia.itemID
        LEFT JOIN deletedItems d ON d.itemID = ia.itemID
        LEFT JOIN items pi ON pi.itemID = ia.parentItemID
        LEFT JOIN itemTypes it ON it.itemTypeID = pi.itemTypeID
        WHERE d.itemID IS NULL AND ia.contentType = 'application/pdf'
    """).fetchall()
    entries = []
    for r in rows:
        path = r["path"] or ""
        if path.startswith("storage:"):  # stored file: storage/<itemKey>/<name>
            fname = path[len("storage:"):]
            full = os.path.join(data_dir, "storage", r["itemKey"], fname)
        elif path.startswith("attachments:") or os.path.isabs(path):  # linked file
            full = path[len("attachments:"):] if path.startswith("attachments:") else path
            if not os.path.isabs(full):
                full = os.path.join(data_dir, full)
        else:
            continue  # imported URL / web snapshot: no local file
        entries.append((full, _zotero_meta(conn, r["parentItemID"]), r["parentType"]))
    conn.close()
    return entries


def _zotero_meta(conn, parent_id):
    if parent_id is None:
        return {}
    fields = {}
    for row in conn.execute("""
            SELECT f.fieldName AS name, idv.value AS value
            FROM itemData id JOIN fields f ON f.fieldID = id.fieldID
            JOIN itemDataValues idv ON idv.valueID = id.valueID
            WHERE id.itemID = ?""", (parent_id,)):
        fields[row["name"]] = row["value"]
    creators = []
    for row in conn.execute("""
            SELECT c.lastName AS ln, c.firstName AS fn, ct.creatorType AS role
            FROM itemCreators ic JOIN creators c ON c.creatorID = ic.creatorID
            JOIN creatorTypes ct ON ct.creatorTypeID = ic.creatorTypeID
            WHERE ic.itemID = ? ORDER BY ic.orderIndex""", (parent_id,)):
        if (row["role"] or "").lower() == "author":
            creators.append(f"{row['fn'] or ''} {row['ln'] or ''}".strip())
    date = fields.get("date") or ""
    m = re.search(r"(19|20)\d{2}", date)
    return {
        "title": fields.get("title"),
        "authors": "; ".join(c for c in creators if c) or None,
        "year": int(m.group(0)) if m else None,
        "journal": fields.get("publicationTitle") or fields.get("journalAbbreviation"),
        "doi": fields.get("DOI"),
    }


def cmd_zotero(req):
    _REL_CENTROID.clear()
    """Migrate Zotero library entries with PDF attachments into the KB."""
    t0 = time.time()
    kb_root = req.get("kb_root") or ".kb"
    zdb = _find_zotero_db(req.get("zotero_db"))
    if not zdb:
        return {"ok": False,
                "error": "未找到 zotero.sqlite（默认位置 ~/Zotero、~/Documents/Zotero、%APPDATA% 配置文件均未命中）。请用 zotero_db 参数显式指定路径。"}
    limit = req.get("limit")
    force = bool(req.get("force"))
    dry_run = bool(req.get("dry_run"))
    try:
        entries = _zotero_entries(zdb)
    except Exception as e:
        return {"ok": False, "error": f"读取 Zotero 数据库失败: {type(e).__name__}: {e}"[:300]}
    if limit:
        entries = entries[: int(limit)]
    db = connect(kb_root)
    files = []
    totals = {"added": 0, "updated": 0, "skipped": 0, "errors": 0, "missing": 0,
              "duplicates": 0, "chunks": 0, "vectors": 0}
    try:
        for full, meta, ptype in entries:
            if not os.path.isfile(full):
                totals["missing"] += 1
                files.append({"path": full, "status": "missing", "type": ptype})
                continue
            if dry_run:
                files.append({"path": full, "status": "candidate", "type": ptype,
                              "title": meta.get("title"), "year": meta.get("year")})
                continue
            _ingest_file(db, Path(full), force, files, totals, meta)
        if totals["added"] or totals["updated"]:
            db.execute("DELETE FROM cache")
        db.commit()
    finally:
        db.close()
    return {"ok": True, "zotero_db": zdb, "candidates": len(entries),
            "dry_run": dry_run, "files": files, "totals": totals,
            "ms": round((time.time() - t0) * 1000)}


# ---------------------------------------------------------------- dedup


def cmd_dedup(req):
    _REL_CENTROID.clear()
    """Remove docs whose sha256 duplicates an earlier doc (keeps lowest id)."""
    t0 = time.time()
    db = connect(req.get("kb_root") or ".kb")
    removed = []
    try:
        dups = db.execute("""
            SELECT id, path, sha256 FROM docs
            WHERE sha256 IS NOT NULL AND sha256 IN (
                SELECT sha256 FROM docs WHERE sha256 IS NOT NULL
                GROUP BY sha256 HAVING COUNT(*) > 1)
            ORDER BY sha256, id""").fetchall()
        keep = {}
        for r in dups:
            if r["sha256"] not in keep:
                keep[r["sha256"]] = r["id"]
            else:
                removed.append({"id": r["id"], "path": r["path"]})
        for r in removed:
            db.execute("DELETE FROM vecs WHERE chunk_id IN "
                       "(SELECT id FROM chunks WHERE doc_id = ?)", (r["id"],))
            db.execute("DELETE FROM chunks WHERE doc_id = ?", (r["id"],))
            db.execute("DELETE FROM docs WHERE id = ?", (r["id"],))
        if removed:
            db.execute("DELETE FROM cache")
        db.commit()
        n_docs = db.execute("SELECT COUNT(*) AS n FROM docs").fetchone()["n"]
        n_chunks = db.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
    finally:
        db.close()
    return {"ok": True, "removed": len(removed), "docs": n_docs,
            "chunks": n_chunks, "files": removed,
            "ms": round((time.time() - t0) * 1000)}


# ---------------------------------------------------------------- clear


def cmd_clear(req):
    _REL_CENTROID.clear()
    """Wipe every doc/chunk/vector/cache row; destructive, requires confirm: true."""
    t0 = time.time()
    if req.get("confirm") is not True:
        return {"ok": False,
                "error": "清空全部文献是破坏性操作且不可恢复：请显式传 confirm: true 确认"}
    db = connect(req.get("kb_root") or ".kb")
    try:
        docs_n = db.execute("SELECT COUNT(*) AS n FROM docs").fetchone()["n"]
        chunks_n = db.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
        db.execute("DELETE FROM vecs")
        db.execute("DELETE FROM chunks")
        db.execute("DELETE FROM docs")
        db.execute("DELETE FROM cache")
        db.commit()
        db.execute("VACUUM")
        db.commit()
        db_path = str((Path(req.get("kb_root") or ".kb") / "kb.sqlite").resolve())
    finally:
        db.close()
    return {"ok": True, "cleared_docs": docs_n, "cleared_chunks": chunks_n,
            "db": db_path,
            "note": "已清空全部文献与索引，可用 kb_ingest 或 kb_zotero 重建。",
            "ms": round((time.time() - t0) * 1000)}


# ---------------------------------------------------------------- serve


def cmd_serve():
    """Line-delimited JSON server: {id, command, payload} -> {id, ok, response|error}.
    Keeps the embedding model loaded across requests (daemon mode)."""
    stdout = sys.stdout.buffer
    for raw in sys.stdin.buffer:
        line = raw.strip()
        if not line:
            continue
        try:
            req = json.loads(line.decode("utf-8"))
        except Exception as e:
            stdout.write(json.dumps(
                {"id": None, "ok": False, "error": f"bad request JSON: {e}"},
                ensure_ascii=True).encode() + b"\n")
            stdout.flush()
            continue
        rid = req.get("id")
        handler = {"ingest": cmd_ingest, "search": cmd_search, "rag": cmd_rag,
                   "stats": cmd_stats, "zotero": cmd_zotero,
                   "dedup": cmd_dedup, "clear": cmd_clear}.get(req.get("command"))
        try:
            if handler is None:
                raise ValueError(f"unknown command: {req.get('command')}")
            resp = handler(req.get("payload") or {})
            resp.setdefault("ok", True)
            resp["engine"] = VERSION
            out = {"id": rid, "ok": True, "response": resp}
        except Exception as e:
            out = {"id": rid, "ok": False, "error": f"{type(e).__name__}: {e}"}
        stdout.write(json.dumps(out, ensure_ascii=True).encode() + b"\n")
        stdout.flush()


# ---------------------------------------------------------------- main


def main():
    if len(sys.argv) < 2:
        sys.stdout.write(json.dumps({"ok": False, "error": "usage: kb_engine.py <ingest|search|rag|stats|serve>"}))
        return 1
    command = sys.argv[1]
    if command == "serve":
        cmd_serve()
        return 0
    handler = {"ingest": cmd_ingest, "search": cmd_search, "rag": cmd_rag,
               "stats": cmd_stats, "zotero": cmd_zotero,
               "dedup": cmd_dedup, "clear": cmd_clear}.get(command)
    if handler is None:
        sys.stdout.write(json.dumps({"ok": False, "error": f"unknown command: {command}"}))
        return 1
    try:
        raw = sys.stdin.buffer.read()
        req = json.loads(raw.decode("utf-8")) if raw.strip() else {}
    except Exception as e:
        sys.stdout.write(json.dumps({"ok": False, "error": f"bad request JSON: {e}"}))
        return 1
    try:
        resp = handler(req)
        resp.setdefault("ok", True)
        resp["engine"] = VERSION
        sys.stdout.write(json.dumps(resp, ensure_ascii=True))
        return 0
    except Exception as e:
        sys.stdout.write(json.dumps(
            {"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=True))
        return 1


if __name__ == "__main__":
    sys.exit(main())
