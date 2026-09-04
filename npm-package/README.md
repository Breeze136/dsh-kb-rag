# dsh-kb-rag

[![npm version](https://img.shields.io/npm/v/dsh-kb-rag)](https://www.npmjs.com/package/dsh-kb-rag)
[![npm downloads](https://img.shields.io/npm/dm/dsh-kb-rag)](https://www.npmjs.com/package/dsh-kb-rag)
[![GitHub release](https://img.shields.io/github/v/release/Breeze136/dsh-kb-rag)](https://github.com/Breeze136/dsh-kb-rag/releases)
[![MIT](https://img.shields.io/github/license/Breeze136/dsh-kb-rag)](LICENSE)
[![Awesome DSH Plugin](https://beancookie.github.io/awesome-dsh-plugin/badge.svg)](https://beancookie.github.io/awesome-dsh-plugin)
[![dsh.so security](https://www.dsh.so/badges/kb-rag.svg)](https://www.dsh.so/artifact/kb-rag/)

Static DSH plugin (Host side): local literature knowledge-base RAG. Lightweight, fast, precise — search + cited QA, token-saving.

> **最新版本 v1.6.1** — 下载安装：`dsh plugin --profile web add dsh-kb-rag@latest`（或 `npm install dsh-kb-rag@latest`）

Import PDF / TXT / MD / DOCX files, whole folders, or a Zotero library into a local knowledge base (workspace `/.kb`),
and run **BM25 + FAISS vector + bge-reranker** hybrid search so the model answers with exact provenance.

## Features (9 model tools)

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
| `kb_fetch` | Download PDF by DOI / arXiv ID (publisher-first, OA fallback; network + proxy hints) |

Citation format: with DOI → `[authors, year, journal](https://doi.org/DOI)` (clickable); without DOI → `[authors, year, filename]`.
`kb_search`/`kb_rag` also return a **related-literature list** (same authors / same journal / nearby year / thematically similar) that the answer's "suggested additions" cites. Every answer ends with that note; in strict mode the answer stays within KB evidence only.

## Install & Enable

Three ways — pick **one**. After installing: **restart DSH and open a new session** (tools are injected at session creation).

### Option 1 — `dsh plugin` one-liner (recommended, DSH profiles)

The package declares `dsh.bundle`, so this installs **and** activates it in one step:

```bash
dsh plugin --profile web add dsh-kb-rag
```

> Requires `pnpm` on PATH (the official DSH plugin flow uses pnpm). Install it once with `npm install -g pnpm` if missing.

### Option 1b — one-shot environment installer via npx (no prior install needed)

Runs the bundled installer (`scripts/install.ps1` / `scripts/install.sh`) straight from the npm registry:
Python deps → engine smoke test → Node/pnpm check → `dsh plugin add` activation → optional model pre-download.

```bash
# ✅ correct — `--package dsh-kb-rag` tells npx which package provides the command
npx --yes --package dsh-kb-rag -c "dsh-kb-rag-install --profile web"
```

> ⚠️ **Common mistake**: a bare `npx dsh-kb-rag-install` **fails** — npx looks for a *package named* `dsh-kb-rag-install` on the registry (which doesn't exist). You must use `--package dsh-kb-rag` (and `-c` to run the bin inside that package's context).
>
> Bash-style flags (`--profile`, `--models`, `--dry-run`, `--mirror`) work on every OS — the entry translates them for Windows PowerShell. Add `--dry-run` to rehearse without changing anything.

### Option 2 — run the bundled script directly (package already installed)

From the deployment/profile directory where you installed the package:

```powershell
# Windows
powershell -NoProfile -ExecutionPolicy Bypass -File node_modules\dsh-kb-rag\scripts\install.ps1
```
```bash
# macOS / Linux / Git Bash
./node_modules/dsh-kb-rag/scripts/install.sh
```

Flags (Windows / bash): `-Mirror`/`--mirror` (pip mirror), `-Profile`/`--profile`, `-Models`/`--models`, `-DryRun`/`--dry-run`.

### Option 3 — manual `npm install` (bring your own activation)

Run **inside the DSH profile/deployment directory** (this is where the plugin loader resolves packages from):

```bash
cd <your-dsh-profile-dir>          # e.g. ~/.dsh/profiles/web
npm install dsh-kb-rag@latest      # or npm install dsh-kb-rag@1.6.1 to pin
```

Then activate it: add `"dsh-kb-rag"` to `dsh.profile.bundles` in the profile's `package.json`, or copy the bundled `cordis.patch.yml` insert into your own patch layer. Restart DSH and open a new session.

### Option 4 — plugin marketplace (no terminal)

Install [dsh-plugin-registry](https://github.com/beancookie/dsh-plugin-registry) once; its Settings "plugin marketplace" panel lists kb-rag (listed in the curated [awesome-dsh-plugin](https://github.com/beancookie/awesome-dsh-plugin) list) with one-click install.

---

### Troubleshooting (things that bite)

| Symptom | Cause / Fix |
|---|---|
| `npx dsh-kb-rag-install` → "npm error code E404 / package not found" | Bare npx looks for a package **named** `dsh-kb-rag-install`. Use `npx --yes --package dsh-kb-rag -c "dsh-kb-rag-install --profile web"` (Option 1b). |
| `dsh plugin ... add` → pnpm errors | pnpm missing from PATH: `npm install -g pnpm`, then retry. |
| install.ps1 → garbled Chinese / syntax error on Windows PowerShell 5.1 | The script ships with UTF-8 BOM (fixed in 1.3.1+). If you copied it manually, re-save as UTF-8 **with BOM**. |
| Installed but tools don't appear | Tools are injected at **session creation** — restart DSH and open a **new** conversation. |
| "模型首次检索自动下载" is slow / fails | Set `HF_ENDPOINT=https://hf-mirror.com` (or run the installer with `--mirror`) for the HuggingFace mirror; models are `bge-small-zh-v1.5` (~95MB) + `bge-reranker-base` (~1.1GB). |
| Upgrading from an older version | In the profile dir: `npm install dsh-kb-rag@latest`, restart, new session. Existing `.kb` libraries migrate automatically (schema versioning, see repo `docs/MIGRATION.md`). |
| Tool call says Python deps missing | Default: only logs the `pip install` command. Set `KB_AUTO_PIP=1` in the host env to auto-install (fixed argv), or run the installer (Option 1b/2). |

### Guide for other Harness users

The DSH plugin loader resolves package names from the deployment's node_modules, same as official static plugins. It does **not** auto-download uninstalled packages at startup — the install step must run once in the deployment/profile directory first. After loading, model sessions get the 9 tools above automatically; tools are injected at session creation, so use a new conversation after the restart.

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
