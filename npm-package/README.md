# dsh-kb-rag

[![npm version](https://img.shields.io/npm/v/dsh-kb-rag)](https://www.npmjs.com/package/dsh-kb-rag)
[![npm downloads](https://img.shields.io/npm/dm/dsh-kb-rag)](https://www.npmjs.com/package/dsh-kb-rag)
[![GitHub release](https://img.shields.io/github/v/release/Breeze136/kb-rag)](https://github.com/Breeze136/kb-rag/releases)
[![MIT](https://img.shields.io/github/license/Breeze136/kb-rag)](LICENSE)
[![Awesome DSH Plugin](https://beancookie.github.io/awesome-dsh-plugin/badge.svg)](https://beancookie.github.io/awesome-dsh-plugin)

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
Every answer ends with a "suggested additions" note; in strict mode the answer stays within KB evidence only.

## Install & Enable

```bash
npm install dsh-kb-rag
```

Load the package in your deployment's cordis composition (cordis.yml / preset):

```yaml
plugins:
  dsh-kb-rag: {}
```

Or let cordis-plugin-loader resolve it by package name. After loading, model sessions get the 8 tools above automatically.

### Guide for other Harness users

Any DSH deployment can use this package directly (the loader resolves package names from the deployment's node_modules, same as official static plugins):

1. Install in the **DSH deployment directory** (or add `dsh-kb-rag` to the deployment's package.json dependencies):

   ```bash
   npm install dsh-kb-rag
   ```

2. Add one line to that deployment's **cordis composition** (cordis.yml or agent preset):

   ```yaml
   plugins:
     dsh-kb-rag: {}
   ```

3. Start/reload DSH — the 8 tools register automatically, no other configuration.

Note: the DSH plugin loader does **not** auto-download uninstalled packages at startup — the install (step 1) must run once in the deployment directory first.

## Requirements

- Node.js ≥ 18 (host process)
- Python 3.9+ with the packages below (if missing at first search/ingest, you will be prompted to install them):

```bash
pip install pymupdf faiss-cpu sentence-transformers
```

The plugin **auto-checks these Python dependencies at startup**: if anything is missing it prints the module and the corresponding
`pip install` command to the host log (it does not auto-install from the network and does not block plugin loading).

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

## License

MIT
