# kb-rag — Local Literature Knowledge-Base RAG (DSH Plugin)

<p align="center">
  <b>把脑子里的模糊记忆,变成一条能点开的文献坐标。</b><br/>
  <i>Turn a fuzzy memory into an exact passage / figure — one-click DOI to source.</i>
</p>

[![npm version](https://img.shields.io/npm/v/dsh-kb-rag)](https://www.npmjs.com/package/dsh-kb-rag)
[![npm downloads](https://img.shields.io/npm/dm/dsh-kb-rag)](https://www.npmjs.com/package/dsh-kb-rag)
[![GitHub release](https://img.shields.io/github/v/release/Breeze136/dsh-kb-rag)](https://github.com/Breeze136/dsh-kb-rag/releases)
[![MIT](https://img.shields.io/github/license/Breeze136/dsh-kb-rag)](LICENSE)
[![Awesome DSH Plugin](https://beancookie.github.io/awesome-dsh-plugin/badge.svg)](https://beancookie.github.io/awesome-dsh-plugin)
[![dsh.so security](https://www.dsh.so/badges/kb-rag.svg)](https://www.dsh.so/artifact/kb-rag/)

> Ingest once, search forever. Only the most relevant few sentences ever reach the LLM — and every claim carries exact provenance.

## Who it's for

Graduate students and PhD researchers. An idea strikes, and you *know* it's somewhere in your
library — but which paper said it, and where? kb-rag makes the whole pile queryable: hybrid
retrieval + reranking associate the right passages, every answer lands on a clickable DOI (or the
exact file), and the reply tells you what your library is still missing. **Think it → find it → cite it.**

kb-rag is a lightweight local database-RAG plugin for **DSH (DeepSeek Harness)**: it turns PDF/Zotero literature into a SQLite knowledge base with section structure and vector indexes, providing the full hybrid search + rerank + cited-QA workflow. All indexing, embedding, and reranking run locally — zero API cost, zero upload.

## Features

- **8 model tools**: `kb_ingest` (file/folder ingest), `kb_zotero` (Zotero migration), `kb_search` (hybrid search), `kb_rag` (cited QA), `kb_scope` (scope/strict mode), `kb_dedup` (dedup), `kb_clear` (wipe), `kb_stats` (stats)
- **Structured chunking**: paper section recognition (abstract ×1.5, methods ×1.2 weights), inline-heading detection, abstract auto-promotion, caption blocks; paragraph fallback for non-papers
- **Hybrid retrieval**: keyword BM25 (CJK-bigram friendly) + bge-small vector cosine, RRF fusion, × section weights
- **Reranking**: bge-reranker-base Cross-Encoder, Top-20 → Top-3 (auto-fallback to bge-large-en bi-encoder if missing)
- **Incremental & dedup**: sha256 incremental skip (40× faster reruns), cross-path duplicate interception, `kb_dedup` for existing stores
- **Query cache**: same query+filters never recompute; any ingest invalidates it
- **Citation standard**: with DOI → markdown link; without DOI → `[authors, year, filename]`
- **Scope & strict mode**: closed-KB / KB+web / web-only; strict mode forbids outside-knowledge extrapolation
- **Related literature**: every search also returns associated papers (same authors / same journal / nearby year / thematically similar), so one query surfaces the surrounding literature — and the answer's "suggested additions" cites them
- **Engine daemon**: models load once, sub-second hot queries; crash self-heal; auto-reclaim on plugin stop

## Design Principles

- **Deliberately zero UI**: every operation and inspection happens through conversation and tool returns (search results render with clickable DOI links); no management panel, no frontend state, no client dependencies — a positioning choice, not a gap. DSH's interface is conversation, and a plugin's interface is tool calls; "panels" belong to scenarios that need direct human administration.
- **Vertical on academic literature**: section-aware chunking (abstract/methods weighting), native Zotero migration, DOI citation standards — not a general-purpose KB manager, but "papers, out of the box".
- **Stay in the sweet spot**: at 20k chunks, brute-force BM25 + IndexFlatIP is optimal; simple implementation plus measured numbers beats feature-stacking.

## Architecture

```
DSH model ──tool call──▶ plugin Host (thin JS) ──JSON-lines──▶ kb_engine.py (resident serve)
                                                             ├─ ingest: hash skip → PyMuPDF extract → section chunking → bge-small encode
                                                             ├─ search: SQL prefilter → BM25+vector dual path → RRF fuse → reranker → snippet+source
                                                             └─ storage: workspace/.kb/kb.sqlite (docs/chunks/vecs/cache)
```

Data flow: raw PDF → verbatim extraction + section chunking → chunks into the DB (with metadata and vectors) → hybrid search + rerank on query → Top-N verbatim snippets (with DOI/file/section/score) → the current conversation model answers with citations.

## Quick Start

See [QUICKSTART.md](QUICKSTART.md). Core three steps:

1. Install Python dependencies (see requirements.txt)
2. Place `kb_engine.py` at the DSH session workspace root
3. Load `plugin/host.js` and `plugin/client.js` via `cordis_define`, run, then just chat (the first search asks for the query scope)

## npm Static Package (for other Harness users)

Published to npm: **`dsh-kb-rag`** ([npmjs.com/package/dsh-kb-rag](https://www.npmjs.com/package/dsh-kb-rag)), and indexed on the [dsh.so registry](https://www.dsh.so/artifact/kb-rag/) (security scan: **passed**).

### Option 1 — one command (recommended, DSH profiles)

The package declares `dsh.bundle`, so `dsh plugin add` installs **and** activates it in one step:

```bash
dsh plugin --profile <name> add dsh-kb-rag
```

Requires pnpm on PATH (the official DSH plugin flow uses pnpm). Then restart DSH and open a new session — the 8 tools register automatically.

### Option 2 — plugin marketplace (no terminal)

Install [dsh-plugin-registry](https://github.com/beancookie/dsh-plugin-registry) once; its Settings "plugin marketplace" panel lists kb-rag (we are in the curated [awesome-dsh-plugin](https://github.com/beancookie/awesome-dsh-plugin) list) with one-click install.

### Option 3 — manual

1. `npm install dsh-kb-rag` in the deployment/profile directory
2. Activate it: add `"dsh-kb-rag"` to `dsh.profile.bundles` in the profile's package.json (or copy the bundled `cordis.patch.yml` insert into your own patch layer)
3. Restart DSH and open a new session

Notes: the DSH plugin loader resolves package names from the deployment's node_modules and does **not** auto-download missing packages. The package ships its own `kb_engine.py` (no manual placement needed); on startup it auto-checks Python dependencies and prints the `pip install` command if anything is missing. See [npm-package/README.md](npm-package/README.md) for full details.

## Tool Reference

| Tool | Purpose | Example phrasing |
|---|---|---|
| kb_ingest | File/folder ingest (incremental + dedup) | "Ingest the papers directory" |
| kb_zotero | Zotero migration (metadata + PDF) | "Sync Zotero" |
| kb_search | Hybrid search + rerank, snippets + sources | "Search domain-wall conduction in BiFeO3" |
| kb_rag | Evidence QA with enforced citations | "What is the domain-wall conduction mechanism?" |
| kb_scope | Scope (closed-KB / KB+web / web-only) + strict mode | "Switch to strict mode" |
| kb_dedup | Clean up existing duplicates | "Deduplicate" |
| kb_clear | Wipe all documents (confirm-guarded) | "Clear the knowledge base" |
| kb_stats | Stats and inventory | "What's in the library?" |

## Benchmarks (measured)

| Item | Result |
|---|---|
| Ingest throughput | 242 PDF/DOCX (1.8GB) → **85.9s** (~355ms/doc) |
| Incremental rerun | Same directory re-ingest **2.17s** (40× speedup) |
| Search latency | Hot queries at 20k chunks **0.4–1.3s** (incl. rerank) |
| Library size | 209 docs / 19,832 chunks / 19,832 vectors, single SQLite file |

## Citation Style (answer format)

| Case | Format |
|---|---|
| With DOI | `[authors, year, journal](https://doi.org/DOI)` |
| Without DOI | `[authors, year, filename]` |
| Strict mode | Answer only from the retrieved evidence; if evidence is insufficient, say "cannot answer from available sources" |
| Normal mode | General-knowledge supplements allowed, marked as "not from the KB" |
| End of answer | Append a "suggested additions" note (key literature missing from the KB) |

## Configuration

| Variable | Default | Description |
|---|---|---|
| `KB_EMBED_MODEL` | `BAAI/bge-small-zh-v1.5` | Embedding model (auto-downloaded to HF cache on first use) |
| `KB_RERANK_MODEL` | `BAAI/bge-reranker-base` | Reranker model |
| `HF_ENDPOINT` | none | Set `https://hf-mirror.com` on restricted networks |

## Repository Layout

```
kb-rag/
├─ kb_engine.py          # Python search engine (CLI + serve protocol)
├─ plugin/
│  ├─ host.js            # DSH plugin Host half (8 tools + daemon + RPC)
│  └─ client.js          # DSH plugin Client half (tool source cards, optional)
├─ npm-package/          # npm static package dsh-kb-rag (lib/index.js + kb_engine.py)
├─ docs/DESIGN.md        # Design doc (chunking/search/protocol details)
├─ QUICKSTART.md         # Five-minute start
├─ CHANGELOG.md
├─ requirements.txt
└─ LICENSE
```

## Known Limitations & Roadmap

- Metadata year: scraped from text when PDF metadata is missing, may mis-pick (Zotero metadata can override)
- Search performance: keyword scan is an in-memory implementation; beyond a few hundred thousand chunks consider FAISS HNSW / SQLite FTS5
- Roadmap: zh→en query translation (local opus-mt model), caption OCR, citation-network graph

## License

MIT — see [LICENSE](LICENSE)
