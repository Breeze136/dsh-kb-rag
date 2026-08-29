# dsh-kb-rag

[![npm version](https://img.shields.io/npm/v/dsh-kb-rag)](https://www.npmjs.com/package/dsh-kb-rag)
[![npm downloads](https://img.shields.io/npm/dm/dsh-kb-rag)](https://www.npmjs.com/package/dsh-kb-rag)
[![GitHub release](https://img.shields.io/github/v/release/Breeze136/dsh-kb-rag)](https://github.com/Breeze136/dsh-kb-rag/releases)
[![MIT](https://img.shields.io/github/license/Breeze136/dsh-kb-rag)](LICENSE)
[![Awesome DSH Plugin](https://beancookie.github.io/awesome-dsh-plugin/badge.svg)](https://beancookie.github.io/awesome-dsh-plugin)
[![dsh.so security](https://www.dsh.so/badges/kb-rag.svg)](https://www.dsh.so/artifact/kb-rag/)

Static DSH plugin (Host side): local literature knowledge-base RAG. Lightweight, fast, precise — search + cited QA, token-saving.

Import PDF / TXT / MD / DOCX files, whole folders, or a Zotero library into a local knowledge base (workspace `/.kb`),
and run **BM25 + FAISS vector + bge-reranker** hybrid search so the model answers with exact provenance.

## Features (8 model tools)

| Tool | Purpose |
| --- | --- |
| `kb_ingest` | Ingest files/folders (PDF/TXT/MD/DOCX, recursive scan) with incremental skip, dedup, section-aware chunking + vectorization |
| `kb_zotero` | Batch-migrate a local Zotero library (items with PDF attachments) into the KB |
| `kb_search` | Hybrid search Top-N snippets + exact sources (title/authors/year/journal/DOI/section/score) |
| `kb_rag` | Retrieve evidence snippets (Top-3 by default) for the model to answer directly, with citation numbers per claim |
| `kb_scope` | Set/view query scope (kb / both / web) and strict mode |
| `kb_stats` | Doc/chunk/vector counts and recent ingest list |
| `kb_dedup` | Remove duplicate documents (keeps the earliest) |
| `kb_clear` | Wipe all documents and indexes (requires explicit `confirm: true`) |

Citation format: with DOI → `[authors, year, journal](https://doi.org/DOI)` (clickable); without DOI → `[authors, year, filename]`.
`kb_search`/`kb_rag` also return a **related-literature list** (same authors / same journal / nearby year / thematically similar) that the answer's "suggested additions" cites. Every answer ends with that note; in strict mode the answer stays within KB evidence only.

## Install & Enable

### Option 1 — one command (recommended)

```bash
npx dsh-kb-rag-install --profile <name>
```

That's the whole install. The `dsh-kb-rag-install` CLI (shipped in this package) chains: Python ≥3.9 detection → pip deps (`--mirror <url>` for a pip mirror, `--user` fallback, `--with-docx` optional) → engine smoke test → Node/pnpm check (installs pnpm if missing) → `dsh plugin --profile <name> add dsh-kb-rag` → activation. Add `--models` to pre-download the embedding/reranker models (`HF_ENDPOINT` respected) and `--dry-run` to rehearse. Re-running is safe (idempotent).

Equivalent long form: `npx --yes --package dsh-kb-rag -c "dsh-kb-rag-install --profile <name>"`.

### Option 2 — dsh plugin add

The package declares `dsh.bundle`, so `dsh plugin add` installs **and** activates it in one step:

```bash
dsh plugin --profile <name> add dsh-kb-rag
```

Requires pnpm on PATH (the official DSH plugin flow uses pnpm). Python dependencies are then handled two ways:

- **Zero-config**: set `KB_AUTO_PIP=1` in the host environment and restart DSH — the plugin pip-installs missing packages itself (fixed argv; off by default, normally it only logs the command).
- **Bundled installer**: run the installer shipped inside the package:

```powershell
# Windows (from the deployment/profile directory where you installed the package)
powershell -NoProfile -ExecutionPolicy Bypass -File node_modules\dsh-kb-rag\scripts\install.ps1
```
```bash
# macOS / Linux / Git Bash
./node_modules/dsh-kb-rag/scripts/install.sh
```

  (The npx CLI in Option 1 is the same chain in a single cross-platform command.)

Then restart DSH and open a new session — the 8 tools register automatically.

### Option 3 — plugin marketplace (no terminal)

Install [dsh-plugin-registry](https://github.com/beancookie/dsh-plugin-registry) once; its Settings "plugin marketplace" panel lists kb-rag (listed in the curated [awesome-dsh-plugin](https://github.com/beancookie/awesome-dsh-plugin) list) with one-click install.

### Option 4 — manual

```bash
npm install dsh-kb-rag
```

Then activate it: add `"dsh-kb-rag"` to `dsh.profile.bundles` in the profile's package.json, or copy the bundled `cordis.patch.yml` insert into your own patch layer. Restart DSH and open a new session.

### Guide for other Harness users

The DSH plugin loader resolves package names from the deployment's node_modules, same as official static plugins. It does **not** auto-download uninstalled packages at startup — the install step must run once in the deployment/profile directory first. After loading, model sessions get the 8 tools above automatically; tools are injected at session creation, so use a new conversation after the restart.

## Requirements

- Node.js ≥ 18 (host process)
- Python 3.9+ with the packages below (auto-detected at startup; see the paragraph after this list):

```bash
pip install pymupdf faiss-cpu sentence-transformers
```

The plugin **auto-checks these Python dependencies at startup** and reports the complete missing list.
By default it prints the module and the corresponding `pip install` command to the host log (it does
not auto-install and does not block plugin loading). Set `KB_AUTO_PIP=1` to let it pip-install the
missing packages itself (fixed argv, PyPI — or `PIP_INDEX_URL` if configured); if deps are missing
and not auto-installed, tool calls return an actionable error with the exact fix instead of an
opaque engine crash.

The embedding model `BAAI/bge-small-zh-v1.5` and reranker `BAAI/bge-reranker-base` download automatically on first use
(local HF cache; on restricted networks set `HF_ENDPOINT=https://hf-mirror.com`).

- Peer dependencies: `@deepseek-ai/cordis` ^4, `@deepseek-ai/dsh-tools` (host tool registration API).

## Usage Examples

1. Ingest: `kb_ingest(paths=["papers/", "notes.md"])`
2. Zotero: `kb_zotero(dry_run=true)` to preview, then drop dry_run for the real migration
3. Search: `kb_search(query="attention is all you need", top_k=5, filters={year: ">=2018"})`
4. QA: `kb_rag(query="What positional encodings does the Transformer use?", strict=true)`
5. Scope: `kb_scope(scope="both")`; see what's in the library: `kb_stats()`

Data persists in the session workspace `/.kb` by default; every tool accepts `kb_root` to override.

## Notes

- This is a Host-side static plugin (all tools run server-side) and **deliberately ships no browser UI / management panel**: every operation and inspection happens through conversation and tool returns (search results render with clickable DOI links) — a positioning choice, not a gap.
- The engine runs as a resident subprocess via the bundled `kb_engine.py` (JSON-lines protocol) and exits when the session ends.
- On restricted networks (no HF / pip access), prepare the model cache and Python dependencies beforehand.

## Security

See [SECURITY.md](SECURITY.md) for the complete execution model: what the plugin spawns, reads, writes,
and downloads — and why automated scanners flag process-spawning plugins as "shell".

## License

MIT
