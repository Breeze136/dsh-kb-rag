# dsh-kb-rag

[![npm version](https://img.shields.io/npm/v/dsh-kb-rag)](https://www.npmjs.com/package/dsh-kb-rag)
[![npm downloads](https://img.shields.io/npm/dm/dsh-kb-rag)](https://www.npmjs.com/package/dsh-kb-rag)
[![GitHub release](https://img.shields.io/github/v/release/Breeze136/dsh-kb-rag)](https://github.com/Breeze136/dsh-kb-rag/releases)
[![MIT](https://img.shields.io/github/license/Breeze136/dsh-kb-rag)](LICENSE)
[![Awesome DSH Plugin](https://beancookie.github.io/awesome-dsh-plugin/badge.svg)](https://beancookie.github.io/awesome-dsh-plugin)
[![dsh.so security](https://www.dsh.so/badges/kb-rag.svg)](https://www.dsh.so/artifact/kb-rag/)

Static DSH plugin (Host side): local literature knowledge-base RAG. Lightweight, fast, precise — search + cited QA, token-saving.

> **Latest version v1.6.4** — install with `dsh plugin --profile web add dsh-kb-rag@latest`. A DSH profile is a pnpm workspace, so do not run `npm install` inside it; for a by-hand deployment see [Option 3](#option-3--manual-npm-install-bring-your-own-activation).

Import PDF / TXT / MD / DOCX files, whole folders, or a Zotero library into a local knowledge base (workspace `/.kb`),
and run **BM25 + FAISS vector + bge-reranker** hybrid search so the model answers with exact provenance.

## Features (9 model tools)

| Tool | Purpose |
| --- | --- |
| `kb_ingest` | Ingest files/folders (PDF/TXT/MD/DOCX, recursive scan) with incremental skip, dedup, section-aware chunking and vectorisation |
| `kb_zotero` | Batch-migrate a local Zotero library (items with PDF attachments) into the KB |
| `kb_search` | Hybrid search Top-N passages with exact provenance (title/authors/year/journal/DOI/section/PDF page/score) |
| `kb_rag` | Retrieve evidence passages (Top-3 by default) for the model to answer directly, with numbered citations per claim |
| `kb_scope` | Set/view query scope (kb / both / web), strict mode and retrieval depth |
| `kb_stats` | Doc/chunk/vector counts and recent ingest list |
| `kb_dedup` | Remove duplicate documents (keeps the earliest) |
| `kb_clear` | Wipe all documents and indexes (requires explicit `confirm: true`) |
| `kb_fetch` | Download PDF by DOI / arXiv ID (publisher version first, so a campus or institutional subscription applies; open-access fallback) |

Citation format: with DOI → `[authors, year, journal](https://doi.org/DOI)` (clickable); without DOI → `[authors, year, filename]`.
`kb_search`/`kb_rag` also return a **related-literature list** (same authors / same journal / nearby year / thematically similar) that the answer's "suggested additions" cites. Every answer ends with that note; in strict mode the answer stays within KB evidence only.

### Engine capabilities

- **Section-aware chunking.** Abstract weighted ×1.5, methods ×1.2, inline heading detection, abstract promotion, caption blocks; paragraph merging for non-article documents.
- **Hybrid retrieval.** BM25 keyword matching with CJK bigram support, bge-small vector cosine, RRF fusion, section weights.
- **Reranking.** bge-reranker-base cross-encoder, top 20 to top 3, with automatic fallback to a bge-large-en bi-encoder when the cross-encoder is unavailable.
- **Provenance.** Every passage carries section, paragraph range and physical PDF page (schema v3), which maps onto Zotero's `?page=N` deep link, plus authors, year, journal and DOI.
- **Citation linking.** In-text `[n]` markers resolve to the document's reference entries; Nature-style superscripts are detected from font metrics. References are stored (weight 0) and excluded from retrieval.
- **Fast and deep modes.** `quick` returns hybrid hits directly (no reranking, citation linking or related work), `deep` runs the full chain.
- **Incremental indexing and deduplication.** SHA-256 content hashes skip unchanged files and intercept duplicates across folders.
- **Query cache.** Identical query and filters are not recomputed; any ingest invalidates it.
- **Resident daemon.** Models load once, the daemon recovers from crashes, and it is reclaimed when the plugin stops. Indexing commits per file, and a long batch is allowed up to 30 minutes before the tool call gives up.
- **Background jobs (MCP path).** The bundled engine also exposes `ingest_async` and `status`: the MCP server forks large batches under `.kb-jobs/` and returns a `job_id` that is polled with `kb_status`, so a client-side call timeout cannot interrupt the work.

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
# Recommended (1.6.3+): the bare command works — served by the micro-package dsh-kb-rag-install,
# which forwards to this package's installer and contains no logic of its own.
npx dsh-kb-rag-install

# Equivalent older form (no micro-package needed; still supported)
npx --yes --package dsh-kb-rag -c "dsh-kb-rag-install --profile web"
```

> **Historical pitfall (≤ 1.6.2):** the registry had no package named `dsh-kb-rag-install`, so a bare `npx dsh-kb-rag-install` returned E404 (npx resolves that name as a package). The same-named micro-package added in 1.6.3 fixes it; the `--package dsh-kb-rag` form stays equivalent and supported.
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

> [!WARNING]
> **Only for deployments you manage by hand.** A DSH profile created by `dsh plugin` is a **pnpm workspace** (it has `pnpm-lock.yaml`); running `npm install` there lays out a npm-style `node_modules` next to pnpm's symlink store and the two disagree from then on. If your plugin was installed via `dsh plugin add`, upgrade it the same way — see [Upgrading](#upgrading) below.

Run **inside the DSH profile/deployment directory** (this is where the plugin loader resolves packages from):

```bash
cd <your-dsh-profile-dir>          # e.g. ~/.dsh/profiles/web
npm install dsh-kb-rag@latest      # or npm install dsh-kb-rag@1.6.4 to pin
```

Then activate it: add `"dsh-kb-rag"` to `dsh.profile.bundles` in the profile's `package.json`, or copy the bundled `cordis.patch.yml` insert into your own patch layer. Restart DSH and open a new session.

### Option 4 — plugin marketplace (no terminal)

Install [dsh-plugin-registry](https://github.com/beancookie/dsh-plugin-registry) once; its Settings "plugin marketplace" panel lists kb-rag (listed in the curated [awesome-dsh-plugin](https://github.com/beancookie/awesome-dsh-plugin) list) with one-click install.

---

### Upgrading

**Installed via `dsh plugin` (the normal case)** — that profile is a **pnpm workspace**, so upgrade through dsh and let it keep the lockfile consistent:

```bash
dsh plugin --profile web add dsh-kb-rag            # latest
dsh plugin --profile web add dsh-kb-rag@1.6.4      # or pin
```

**Installed manually via npm** (Option 3) — stay with npm in that profile dir:

```bash
cd <your-dsh-profile-dir>
npm install dsh-kb-rag@latest
```

Either way: **restart DSH and open a new session**. Existing `.kb` libraries migrate automatically (schema versioning), but **page anchors and superscript citation markers are parse-time data** — run `kb_ingest` with `force: true` over old documents to get them.

---

### Troubleshooting (things that bite)

| Symptom | Cause / Fix |
|---|---|
| `npx dsh-kb-rag-install` → "npm error code E404 / package not found" | Pitfall of releases up to 1.6.2: the registry had no package of that name. Fixed by the `dsh-kb-rag-install` micro-package in 1.6.3+; if npm cached the old metadata, run `npm cache clean --force` first. Temporary workaround: `npx --yes --package dsh-kb-rag -c "dsh-kb-rag-install --profile web"`. |
| `dsh plugin ... add` → pnpm errors | pnpm missing from PATH: `npm install -g pnpm`, then retry. |
| install.ps1 → garbled Chinese / syntax error on Windows PowerShell 5.1 | The script ships with UTF-8 BOM (fixed in 1.3.1+). If you copied it manually, re-save as UTF-8 **with BOM**. |
| install.ps1 → `OSError: [WinError 123] ... C:\Users\??\...` (fails at engine smoke test) | Non-ASCII Windows username: PowerShell 5.1's default `$OutputEncoding` is ASCII, mangling Chinese chars in the piped JSON to `?` (≤ 1.6.2). Fixed by forcing UTF-8 pipe encoding (1.6.3+); workaround: `$env:TEMP='C:\kbragtmp'; $env:TMP='C:\kbragtmp'`, then re-run. |
| Installed but tools don't appear | Tools are injected at **session creation** — restart DSH and open a **new** conversation. |
| Model download on the first search is slow / fails | If the direct download fails it now retries automatically through the `hf-mirror.com` mirror (installer `--models` and first search). To pin it manually: `HF_ENDPOINT=https://hf-mirror.com`; the models are `bge-small-zh-v1.5` (~95 MB) + `bge-reranker-base` (~1.1 GB). |
| Upgrading from an older version | **Depends on how you installed it** — see [Upgrading](#upgrading) above: `dsh plugin --profile <name> add dsh-kb-rag` for pnpm-managed profiles, `npm install dsh-kb-rag@latest` only if you installed manually with npm. Restart DSH and open a new session; `.kb` libraries migrate automatically (see `docs/MIGRATION.md`), page anchors / citation markers need `force` re-ingest on old data. |
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
3. Search: `kb_search(query="graphene domain coalescence during CVD growth on copper", top_k=5, filters={year: ">=2018"})`
4. QA: `kb_rag(query="How is layer thickness determined from the Raman 2D peak?", strict=true)`
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
