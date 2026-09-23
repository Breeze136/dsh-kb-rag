# kb-rag — Local literature RAG with passage-level provenance

[![npm version](https://img.shields.io/npm/v/dsh-kb-rag)](https://www.npmjs.com/package/dsh-kb-rag)
[![npm downloads](https://img.shields.io/npm/dm/dsh-kb-rag)](https://www.npmjs.com/package/dsh-kb-rag)
[![GitHub release](https://img.shields.io/github/v/release/Breeze136/dsh-kb-rag)](https://github.com/Breeze136/dsh-kb-rag/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Awesome DSH Plugin](https://beancookie.github.io/awesome-dsh-plugin/badge.svg)](https://beancookie.github.io/awesome-dsh-plugin)
[![dsh.so security](https://www.dsh.so/badges/kb-rag.svg)](https://www.dsh.so/artifact/kb-rag/)

**English** | [Chinese](./README_CN.md)

kb-rag is a local literature knowledge base for DSH (DeepSeek Harness) and any MCP-capable agent. It indexes PDFs and Zotero libraries into a single SQLite file, then answers questions with passages rather than paraphrases: every result carries its section, physical PDF page, and a clickable DOI — and every in-text citation in the retrieved passage can be traced back to the referenced work, including whether that work is already in your library.

> The npm package [`dsh-kb-rag`](https://www.npmjs.com/package/dsh-kb-rag) is published from [`npm-package/`](./npm-package) in this repository. It is not affiliated with other repositories that share the name `dsh-kb-rag`.

Indexing, embedding, and reranking all run locally. There is no API cost and no upload.

<p align="center">
  <a href="#quick-start"><strong>Quick Start</strong></a> ·
  <a href="#three-deployment-shapes"><strong>Deployment shapes</strong></a> ·
  <a href="#tool-reference"><strong>Tool reference</strong></a> ·
  <a href="#documentation"><strong>Documentation</strong></a> ·
  <a href="#measured-performance"><strong>Measured performance</strong></a>
</p>

## What the output looks like

A single `kb_rag` call returns evidence in this form. The tool renders its interface in Chinese today, so the block below is that output translated; the in-library marker it prints appears here as `[in-library]`:

```text
**Knowledge base sources Top-2**
deep · reranked with BAAI/bge-reranker-base · cache hit

1. [Chemical vapour deposition of graphene on copper substrates](https://doi.org/10.5555/12345678) — Author A; Author B · 2024 · Carbon · Results · p.4
> graphene domains nucleate on the copper surface and coalesce into a continuous film ... at a growth rate of ~2 um/min
citations from this evidence ([in-library] = already held, searchable)
  · [Ref 4] Author C, et al. Carbon 48, 1234 (2010)
    [in-library] [Nucleation and growth of graphene on transition metals](https://doi.org/10.5555/12345684) (Author C · 2010 · Carbon) (this evidence's Ref 4) · [open in Zotero](zotero://open-pdf/library/items/EXAMPLEKEY1)
  · 3 further citations collapsed (Ref 6-8); use the numbers to fetch them

**Related work**
- [A Practical Guide to Raman Spectroscopy of Graphene] — Author G et al. · 2020 (same author, related topic)
```

The full walkthrough, including the agent's answer and the follow-up that resolves a page number, is in [`docs/OUTPUT-FORMAT.md`](docs/OUTPUT-FORMAT.md). The example uses neutral placeholder data: authors, journals, and DOIs are fictional.

## Positioning

Retrieval is table stakes; the question is how far a result sits from the original evidence. kb-rag returns a **location** rather than a summary: section, physical PDF page, clickable DOI, and the citation chain behind the passage.

Three deliberate trade-offs define the project:

- **Local first, zero upload.** Embedding and reranking run on local bge models. The entire index is one `kb.sqlite` file that can be copied or archived.
- **Vertical, not general purpose.** Section-aware chunking (abstract and methods weighted), native Zotero migration, and DOI citation conventions. It is built for papers, not for arbitrary document management.
- **Stated limits.** Scanned PDFs without a text layer are skipped, figure captions are indexed as text rather than images, and cross-language retrieval is weak. These are documented under [Known limitations](#known-limitations) instead of being promised as forthcoming.

> [!IMPORTANT]
> **Scope and expectations.** Retrieval quality is bounded by the library itself: the tool cannot answer from documents it does not hold, and it cannot read a scanned page that has no text layer. Three behaviours are worth knowing in advance:
>
> - **First use is slow.** The embedding model (~95 MB) and the reranker (~1.1 GB) are downloaded on first use, and the first query waits roughly ten seconds for them to load. The resident daemon then keeps them in memory and subsequent queries are sub-second.
> - **Anchors are ingest-time data.** Page anchors and superscript citation markers are produced when a document is parsed. Libraries indexed before v1.6 keep working, but those fields stay empty until the documents are re-ingested with `force`.
> - **Bulk ingestion is asynchronous.** Above `KB_ASYNC_THRESHOLD` (default 25) pending files, `kb_ingest` forks the batch as a background job and returns a `job_id` immediately instead of blocking the session — in both deployment shapes, so a host-side call timeout cannot interrupt the work. Poll the job with `kb_status` until it reports `done`.

## Three deployment shapes

One engine (`kb_engine.py`), one data format, three entry points:

| Shape | Entry point | Tool set |
|---|---|---|
| **DSH plugin** (primary) | `plugin/` — conversational use inside a DSH session | 10 tools, adding `kb_scope` (query scope and strict mode, a DSH session concept) and `kb_status` (background job polling) |
| **MCP server** | `mcp-server/server.py` — stdio, for Claude Desktop, Cherry Studio, Kimi, DeepSeek, Cursor, and similar | 9 tools; `kb_status` exists in both shapes, so only `kb_scope` remains DSH-specific |
| **npm package** | `dsh-kb-rag` — declares `dsh.bundle`, so `dsh plugin add` installs and activates in one step | Same as the DSH plugin |

## Quick Start

### 1. Requirements

Python 3.9 or newer. Node and pnpm are checked by the installer, which installs pnpm if it is missing. DSH users operate inside a DSH profile; MCP users need only Python.

<details>
<summary><strong>Windows</strong> — non-ASCII usernames are handled from v1.6.3</summary>

Windows PowerShell 5.1 defaults its pipe encoding to ASCII, which turned non-ASCII usernames in the temp path into `?` and made the engine smoke test fail with `WinError 123`. From v1.6.3 the installer forces UTF-8 pipe encoding at the top of the script. Details: [`docs/install-winerror123-fix.md`](docs/install-winerror123-fix.md).
</details>

<details>
<summary><strong>Restricted networks</strong> — model downloads fall back to a mirror</summary>

Both the installer and the engine retry through `hf-mirror.com` when a direct download fails (`_apply_hf_mirror` patches the `huggingface_hub` constants, since setting the environment variable after import has no effect). To pin it manually: `HF_ENDPOINT=https://hf-mirror.com`.
</details>

### 2. Install

> **On Windows and would rather not touch a command line?** Use the companion
> one-click installer: download the zip from the
> [dsh-oneclick release page](https://github.com/Breeze136/dsh-oneclick/releases/latest),
> unzip it **completely**, then double-click `install.cmd`. It fills in a missing
> Node.js (no administrator rights), the official DSH CLI and a desktop shortcut, and
> asks once whether you want this knowledge base — press Enter and Python, the engine
> dependencies and the ~1.2 GB of retrieval models are installed too. Running it again
> updates. It is a third-party helper, not published by DeepSeek: it installs the
> official packages through their official channels.

**Option A — one command (recommended if you have a terminal)**

```bash
npx dsh-kb-rag-install
```

The installer runs the whole chain: Python dependencies, engine smoke test, Node/pnpm check, `dsh plugin add` activation, and model pre-download (on by default; pass `--no-models` to skip). If no profile is given it inspects `~/.dsh/profiles/`: a single profile is used directly, several are offered as a choice, and none falls back to `web`.

> Equivalent without the micro-package: `npx --yes --package dsh-kb-rag -c "dsh-kb-rag-install --profile web"`

**Option B — DSH users, install the plugin directly**

```bash
dsh plugin --profile web add dsh-kb-rag
```

**Option C — from source**

```bash
git clone https://github.com/Breeze136/dsh-kb-rag.git && cd dsh-kb-rag
./npm-package/scripts/install.sh        # macOS / Linux / Git Bash
# Windows: install.cmd, or npm-package\scripts\install.ps1
```

### 3. Build a library

In a DSH conversation, ask it to ingest a folder (`kb_ingest`) or to sync Zotero (`kb_zotero`). Individual papers can be fetched first by identifier (`kb_fetch` — it resolves the publisher version first, which works on campus or institutional networks, and falls back to open access).

<details>
<summary>Bulk ingestion — keeping host timeouts out of the way</summary>

Above `KB_ASYNC_THRESHOLD` (default 25) pending files, `kb_ingest` switches to a background job in both deployment shapes; the count is taken when the call arrives (the DSH plugin has the engine count the files, the MCP server counts them host-side). The call returns a `job_id` immediately; poll it with `kb_status` until the status is `done`. Whole-library Zotero migrations use `kb_zotero(async_mode=true)`. The job runs in its own subprocess, so a 60-second client timeout does not interrupt it.
</details>

### 4. Ask

- "Which papers in the library discuss graphene growth on copper?" — `kb_search`
- "Which page of which paper states this?" — read the `page` field on the evidence, or follow the Zotero page link
- "Answer only from the library" — switch on strict mode with `kb_scope` (DSH)
- "Quick look" versus "analyze in depth" — `kb_search` defaults to `quick` (sub-second), `kb_rag` defaults to `deep` (reranking, citation linking, related work)

Restart DSH and open a new session after installing: tools are injected when a session is created, so existing sessions do not pick them up. Step-by-step instructions and common pitfalls are in [QUICKSTART.md](QUICKSTART.md).

### 5. Upgrade

A DSH profile is a **pnpm workspace** (it contains `pnpm-lock.yaml`, and `dsh plugin` itself forwards to pnpm), so upgrades go through the same command that installed the plugin:

```bash
dsh plugin --profile web add dsh-kb-rag          # latest
dsh plugin --profile web add dsh-kb-rag@1.6.7    # or pin a version
```

Re-running the installer is equivalent and additionally reconciles Python dependencies:

```bash
npx dsh-kb-rag-install --profile web
```

> [!WARNING]
> **Do not run `npm install dsh-kb-rag` inside a DSH profile.** It writes an npm-style `node_modules` next to pnpm's symlink store, and the two layouts disagree from then on; subsequent `dsh plugin` operations become unpredictable. `npm install` is only appropriate for a deployment you manage entirely by hand (see [npm-package/README.md](npm-package/README.md), Option 3).
>
> After upgrading, restart DSH and open a new session. Existing `.kb` libraries migrate automatically (see [docs/MIGRATION.md](docs/MIGRATION.md)), but **page anchors and superscript citation markers require a re-ingest with `force` on documents indexed earlier** — the migration adds columns, it does not re-parse documents. `kb_stats` reports `stale_docs` when part of the library was written by an older parser revision, and the plugin then asks once, at the first retrieval of a session, how to handle it: ignore, refresh metadata only, or re-ingest.

## Tool reference

### DSH plugin (10 tools)

| Tool | Purpose | Example request |
|---|---|---|
| `kb_ingest` | Ingest files or folders: incremental skip, deduplication, section chunking, vectorisation (PDF/TXT/MD/DOCX). `metadata_only=true` refreshes title/authors/year/journal/DOI in place, `rebuild=true` re-parses every indexed document; large batches move to a background job automatically | "Ingest the papers folder" |
| `kb_status` | Poll a background ingest job by `job_id`: `running` with progress (processed / errors / chunks), `done` with the job's totals and recent files, `error`, or `not_found` | "How is the ingest going?" |
| `kb_zotero` | Migrate a local Zotero library, including PDF attachments | "Sync Zotero" |
| `kb_search` | Hybrid retrieval returning passages with exact provenance (title, authors, year, journal, DOI, page, section) | "Search chemical vapour deposition of graphene on copper" |
| `kb_rag` | Evidence question answering, top 3 by default, numbered citations | "How does graphene grow on copper during CVD?" |
| `kb_scope` | Query scope (library only / library plus web / web only), strict mode, retrieval depth | "Switch to strict mode" |
| `kb_dedup` | Remove duplicate documents, keeping the earliest copy | "Deduplicate" |
| `kb_clear` | Wipe all documents and indexes; requires `confirm=true` | "Clear the knowledge base" |
| `kb_stats` | Document, chunk and vector counts, recent ingests, and `stale_docs` (documents written by an older parser revision) | "What is in the library?" |
| `kb_fetch` | Download a PDF by DOI or arXiv ID (publisher version first, so a campus or institutional subscription applies; open-access fallback) | "Download 10.5555/12345678" |

The MCP server exposes the same engine through 9 tools, and it also provides `kb_status` (background job polling); only `kb_scope` stays DSH-specific. Configuration and client snippets: [mcp-server/README.md](mcp-server/README.md).

### Engine capabilities

- **Section-aware chunking.** Abstract weighted ×1.5, methods ×1.2, inline heading detection, abstract promotion, caption blocks; paragraph merging for non-article documents.
- **Hybrid retrieval.** BM25 keyword matching with CJK bigram support, bge-small vector cosine, RRF fusion, section weights.
- **Reranking.** bge-reranker-base cross-encoder, top 20 to top 3, with automatic fallback to a bge-large-en bi-encoder when the cross-encoder is unavailable.
- **Page anchors** (schema v3). Results carry the physical PDF page and render as `section · p.N`, which maps onto Zotero's `?page=N` deep link.
- **Citation linking.** In-text `[n]` markers resolve to reference entries; Nature-style superscripts are detected from font metrics (`graphene1,2` becomes `graphene[1,2]`); cited works are matched against the library by DOI, normalised title, or first author plus year, and matches are marked as in-library in the rendered result.
- **Fast and deep modes.** `quick` returns hybrid hits directly (no reranking, citation linking, or related work), `deep` runs the full chain.
- **Incremental indexing and deduplication.** SHA-256 content hashes skip unchanged files (about 40× faster on re-runs) and intercept duplicates across paths.
- **Metadata refresh and stale-data detection** (schema v4). Every document records the parser revision that wrote it (`docs.indexed_with`), so `kb_stats` can report `stale_docs` — rows an older revision wrote, which incremental ingest would otherwise never revisit. `kb_ingest(metadata_only=true)` re-extracts title, authors, year, journal and DOI without re-chunking or re-embedding (measured at about 90 ms per document), and `kb_ingest(rebuild=true)` re-parses every indexed document in place.
- **Query cache.** Identical query and filters are not recomputed; any ingest invalidates it.
- **Resident daemon.** Models load once, the daemon recovers from crashes, and it is reclaimed when the plugin stops.

## Query guidance

Queries reach the engine verbatim: it never translates, expands or rewrites them, so retrieval depends on the query matching the language of the indexed text. A typical library is overwhelmingly English (measured: about 98% of the body text), which has practical consequences:

- **Who writes the query comes first.** Inside a DSH session you never write queries yourself — the agent does, and its tool description already instructs it to rewrite a CJK question into an English term string before calling `kb_search` / `kb_rag`. Asking in Chinese is therefore expected and fine. Write English term strings yourself only when you drive the tools directly (MCP clients, scripts, the engine CLI).
- **Why English term strings retrieve better.** BM25 matches on tokens, so a CJK query leaves the keyword half of the hybrid ranking idle — CJK bigrams cannot match English body text — and the hit depends on cross-language vector similarity alone. For the same question, an English term string retrieves noticeably better than its translation.
- **Use the 3–12 word pattern** `material/system + method/process + property/characterisation`, not a full question: `graphene CVD copper single crystal nucleation suppression` rather than "how is nucleation suppressed on copper during chemical vapour deposition of graphene".
- **Put limits in `filters`, not in the query.** Year, journal, author, section and file kind are metadata filters; keeping them in the query text spends keywords on terms the ranked body text does not contain. One caveat: **`journal` is populated only by the Zotero migration path**, so it stays `NULL` for everything indexed with `kb_ingest` and filtering on it usually returns nothing — use author, year, title or section instead.
- **Send a second query in the original language only when documents in that language are actually wanted** — for example when the library also holds Chinese-language reviews.

When a query contains CJK characters and the library is almost entirely English, the engine adds a `lang_note` to the response saying so, and the plugin renders it next to the results.

## Architecture

```text
DSH model / MCP client (Claude, Cherry, Kimi, Cursor, ...)
   |  tool call: kb_ingest / kb_search / kb_rag / kb_stats ...
   v
plugin host (JS) or MCP server (server.py + engine_client.py)
   |  JSON lines over stdio, one request/response per line
   v
kb_engine.py -- resident `serve` daemon (models load once)
   |-- ingest:  sha256 skip -> PyMuPDF extraction -> section chunking -> bge-small encode
   |             (committed per file; above KB_ASYNC_THRESHOLD the batch is forked as a job
   |              under .kb-jobs/ and a job_id is returned for kb_status to poll, while
   |              metadata_only reads page 1 only and rebuild re-parses the library's
   |              own recorded paths)
   |-- search:  SQL prefilter -> BM25 + vector -> RRF fusion -> bge-reranker rerank
   |             -> top-N verbatim passages with DOI, page, section and score
   `-- storage: <kb_root>/kb.sqlite (docs, chunks, vecs, cache; schema v4,
                migrations gated by PRAGMA user_version)
```

## Measured performance

| Metric | Result |
|---|---|
| Ingest throughput | 242 PDF/DOCX files (1.8 GB) in **85.9 s**, about 355 ms per document |
| Incremental re-run | Same directory re-ingested in **2.17 s**, a 40× speed-up |
| Query latency | 0.4–1.3 s warm at 20k chunks including reranking; **~16 ms** in `quick` mode |
| Library size | 209 documents, 19,832 chunks, 19,832 vectors in a single SQLite file |
| Citation parsing | Across 11 publisher PDFs: a Wiley review 0 to 399 entries, a Nature letter 8 to 37, a Science paper 0 to 29 — strictly additive |
| Full re-index (GPU) | 316 PDFs / 23.5k chunks / 20.1k vectors rebuilt in **219 s** on an 8 GB consumer GPU, zero errors |
| Embedding throughput | **164 chunks/s on GPU** vs **37 chunks/s on CPU** (~4.4×); flat from batch 32 to 256, so CPU-side tokenisation — not the GPU — is the limiting factor |

Measured on Windows; the first four rows were taken with CPU inference. Methodology and design rationale: [`docs/DESIGN.md`](docs/DESIGN.md).

### Device handling (GPU / CPU)

Ingestion and search share one embedding model, and a CUDA build of torch is picked up automatically — no configuration needed (`KB_DEVICE=auto`, the default):

- **The device is probed before the first model load** with a tiny matmul. A driver that merely *reports* a GPU is not enough — version mismatches, containers and exclusive-compute mode all fail at the first kernel — so the probe catches them up front and falls back to CPU with a readable reason instead of failing later during model loading. `KB_GPU_PROBE=0` skips the probe.
- **Device failures during a call degrade instead of aborting it**: the batch is halved, then cut to 4, then the model moves to CPU and the call still finishes. The batch size that worked is remembered for the rest of the process. Non-device errors (a missing file, a bad model directory) are raised unchanged — the fallback never hides a real problem.
- **`reload` re-arms the GPU**: after fixing a driver or installing a CUDA build of torch, `reload` resets the device verdict, and `drop_models=true` reloads the models onto the GPU — no need to restart the host.
- **Batch sizes follow the device and the free VRAM** (tiered below 8 GB); raising them does not speed up small models. `KB_EMBED_BATCH` / `KB_RERANK_BATCH` override the defaults.
- **CPU-only is about 4× slower on the embedding step.** PDF text extraction (PyMuPDF), chunking, reference judging and BM25 are CPU-bound either way, so a faster CPU also shortens ingestion.

`kb_stats` reports the outcome — `device: {requested: auto, probe: ok, cuda_available: true, gpu: …, vram_gb: 8.0, embed_device: cuda:0, rerank_device: cuda:0, batch: {…}}` — plus `gpu_disabled_reason` and `note` whenever a fallback happened.

## Documentation

| Document | Contents |
|---|---|
| [QUICKSTART.md](QUICKSTART.md) | Five-minute setup: dependencies, indexing, retrieval, common pitfalls |
| [docs/DESIGN.md](docs/DESIGN.md) | Design notes: storage model, chunking strategy, retrieval pipeline, engine protocol |
| [docs/OUTPUT-FORMAT.md](docs/OUTPUT-FORMAT.md) | Output and citation conventions: page anchors, citation linking, fast and deep modes |
| [docs/MIGRATION.md](docs/MIGRATION.md) | Schema migration: `PRAGMA user_version` gating, v1 through v4 |
| [docs/BACKLOG.md](docs/BACKLOG.md) | Known gaps: unfixed issues, items still to verify, and how to verify a change |
| [mcp-server/README.md](mcp-server/README.md) | MCP configuration, tool mapping, asynchronous behaviour and timeouts |
| [npm-package/README.md](npm-package/README.md) | npm package documentation and troubleshooting table |
| [SECURITY.md](SECURITY.md) | Execution model and security boundaries: what is spawned, read, written, downloaded |
| [UNINSTALL.md](UNINSTALL.md) | Removal: stop the plugin and delete the index, leaving PDFs and Zotero untouched |
| [CHANGELOG.md](CHANGELOG.md) | Release history |

## Configuration

| Variable | Default | Applies to | Description |
|---|---|---|---|
| `KB_EMBED_MODEL` | `BAAI/bge-small-zh-v1.5` | Engine | Embedding model; downloaded to the Hugging Face cache on first use |
| `KB_RERANK_MODEL` | `BAAI/bge-reranker-base` | Engine | Reranking model |
| `KB_DEVICE` | `auto` | Engine | `auto` uses the GPU whenever a CUDA build of torch finds a working device; `cpu` / `cuda` / `cuda:1` / `mps` force a choice. Any device failure falls back to CPU automatically |
| `KB_GPU_PROBE` | `1` | Engine | `0` skips the one-off "can this device actually compute?" probe (a tiny matmul) and goes straight to the model load — an escape hatch for unusual environments |
| `KB_EMBED_BATCH` | GPU 128 / CPU 32 (tiered below 8 GB VRAM) | Engine | Embedding batch size for ingest and search. Raising it does not help on small models — measured throughput is flat from 32 to 256 |
| `KB_RERANK_BATCH` | GPU 64 / CPU 16 (tiered below 8 GB VRAM) | Engine | Reranking batch size |
| `KB_MODEL_RETRY_SECS` | `120` | Engine | How long a failed model load is remembered before retrying (`0` = every call, negative = never retry) |
| `HF_ENDPOINT` | none | Engine | Set to `https://hf-mirror.com` on restricted networks |
| `KB_AUTO_PIP` | `0` | npm package | `1` installs missing Python dependencies at startup (fixed argv; by default only the command is printed). The dynamic plugin host reports but does not install |
| `KB_RAG_ROOT` | DSH: session workspace `.kb`; MCP: `~/.kb-rag` | MCP | Knowledge base directory; per-call override with `kb_root` |
| `KB_RAG_PYTHON` | current interpreter | MCP | Interpreter used for the engine, to avoid a bare `python` resolving elsewhere |
| `KB_ASYNC_THRESHOLD` | `25` | Engine | Pending file count above which `kb_ingest` forks a background job and returns a `job_id` (poll with `kb_status`) |
| `KB_SQLITE_WAL` | off | Engine | `1` enables SQLite WAL; the default is safer when the `.kb` directory is synchronised |
| `UNPAYWALL_EMAIL` | built-in placeholder | Engine | Contact address used by `kb_fetch` for Unpaywall queries; set your own |

## Repository layout

```text
kb-rag/
├─ kb_engine.py              Python engine: chunking, retrieval, reranking, serve daemon
├─ install.cmd               Windows entry point (double-click, runs scripts\install.ps1)
├─ scripts/                  Installer scripts (install.ps1, install.sh)
├─ plugin/                   DSH dynamic plugin (kbrag.plugin.json, host.js, client.js)
├─ npm-package/              npm package dsh-kb-rag (published contents, cordis.patch.yml)
├─ dsh-kb-rag-install/       Micro-package providing the bare `npx dsh-kb-rag-install` command
├─ mcp-server/               MCP server (server.py, engine_client.py)
├─ docs/                     DESIGN.md, OUTPUT-FORMAT.md, MIGRATION.md, install-winerror123-fix.md
├─ tools/                    Internal maintenance scripts (not published)
└─ QUICKSTART.md, CHANGELOG.md, SECURITY.md, UNINSTALL.md, LICENSE
```

Runtime data: the DSH plugin writes to `.kb/kb.sqlite` in the session workspace; the MCP server defaults to `~/.kb-rag/kb.sqlite`. Background job files live in `<kb_root>/.kb-jobs/` and are removed once a job finishes.

## Known limitations

- **Scanned PDFs are not supported.** Documents without a text layer are skipped; OCR is deliberately out of scope.
- **Page anchors are PDF-only.** TXT, MD and DOCX files, along with documents indexed before schema v3, have no page numbers and fall back to section-level location until re-ingested with `force`.
- **Citation linking requires a re-ingest.** Superscript detection and the current reference splitting run at parse time; older libraries need `force` to gain them.
- **Metadata can be misread.** When PDF metadata is missing, the title and year are inferred from page text; Zotero metadata overrides this.
- **Cross-language retrieval is weak at the engine level.** The engine never translates: a CJK query against English full text leans on the vector path, and the response carries a `lang_note` saying so. The **agent layer** compensates by rewriting CJK questions into English term strings before the call, so a Chinese question asked inside a DSH session is handled; in-engine query translation is still on the roadmap. See [Query guidance](#query-guidance).
- **Captions are text only.** A caption is searchable as text, but content that appears only inside a figure is not.
- **Scale.** Keyword matching is an in-memory implementation. Beyond a few hundred thousand chunks, FAISS HNSW or SQLite FTS5 would be the appropriate next step.

## Contact

- Bug reports and feature requests: [GitHub Issues](https://github.com/Breeze136/dsh-kb-rag/issues)
- Questions and discussion: [GitHub Discussions](https://github.com/Breeze136/dsh-kb-rag/discussions)
- Security reports: see [SECURITY.md](SECURITY.md)

## Related projects

- [awesome-dsh-plugin](https://github.com/awesome-dsh-plugin/awesome-dsh-plugin) — curated list of DSH plugins
- [dsh-plugin-registry](https://github.com/beancookie/dsh-plugin-registry) — plugin marketplace panel for DSH settings

## License

[MIT](LICENSE). The bundled models (`BAAI/bge-*`) are downloaded at runtime and remain under their own licences.
