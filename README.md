# kb-rag: local literature RAG with passage-level provenance

[![npm version](https://img.shields.io/npm/v/dsh-kb-rag)](https://www.npmjs.com/package/dsh-kb-rag)
[![npm downloads](https://img.shields.io/npm/dm/dsh-kb-rag)](https://www.npmjs.com/package/dsh-kb-rag)
[![GitHub release](https://img.shields.io/github/v/release/Breeze136/dsh-kb-rag)](https://github.com/Breeze136/dsh-kb-rag/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Awesome DSH Plugin](https://beancookie.github.io/awesome-dsh-plugin/badge.svg)](https://beancookie.github.io/awesome-dsh-plugin)
[![dsh.so security](https://www.dsh.so/badges/kb-rag.svg)](https://www.dsh.so/artifact/kb-rag/)

**English** | [Chinese](./README_CN.md)

kb-rag is a local literature knowledge base for DSH (DeepSeek Harness) and other MCP clients. It indexes PDFs and Zotero libraries into one SQLite file, then returns the passages that answer a question. Each result carries its section, physical page and DOI, and the citations attached to that passage, including whether the cited work is already in your library.

Indexing, embedding and reranking run on your machine. There is no API cost and nothing is uploaded.

The npm package [`dsh-kb-rag`](https://www.npmjs.com/package/dsh-kb-rag) is published from [`npm-package/`](./npm-package). Other repositories with the same name are not affiliated with it.

## What a result looks like

A `kb_rag` call returns evidence in this shape. The interface is in Chinese today, so the block below is a translation. `[in-library]` marks a cited work that is already indexed.

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

Authors, journals and DOIs are placeholders; the format is real. The full walkthrough is in [`docs/OUTPUT-FORMAT.md`](docs/OUTPUT-FORMAT.md).

## What 1.6.8 changes

1.6.8 fixes two things that broke the DSH 0.2 install: the peer range and the client injection list. With both wrong, the plugin was rejected during install and the tool cards stayed empty.

It also adds HTML answer pages rendered by the engine and a title-level probe. `kb_stats` now reports model cache state, and deep searches return `no_hit` when the best reranked score stays below the floor. The search core is otherwise unchanged: on the same library, results line up with 1.6.7. The full list is in [CHANGELOG.md](CHANGELOG.md).

## Install

Install from the DSH desktop app, or use one of the command-line paths below. After installing, restart DSH and open a new session. Tools are injected when a session is created, so existing sessions do not see them.

### A. DSH desktop app (no terminal)

1. Open **Settings → Plugins** (设置 → 插件).
2. Search `kb-rag`. The package is `dsh-kb-rag`, and the entry should say **1.6.8**.
3. Install, restart DSH, and start a new conversation.

The desktop app needs to be 0.2 or newer. 1.6.7 and earlier fail that version's compatibility check; 1.6.8 is the first release that passes it. The first search downloads the two models (~1.2 GB), so expect a short wait. `kb_stats` shows whether they are present.

### B. DSH CLI

```bash
dsh plugin --profile web add dsh-kb-rag@1.6.8
```

Pin the version. A DSH profile is a pnpm workspace, and pnpm's `minimumReleaseAge` policy refuses packages published minutes ago. An unpinned install right after a release can quietly resolve to the previous version. Re-running the installer also works and reconciles Python dependencies.

### C. One-shot installer

```bash
npx dsh-kb-rag-install
```

The installer runs the chain: Python dependencies, an engine smoke test, a Node/pnpm check, `dsh plugin add`, and model pre-download (on by default; `--no-models` skips it). With no profile argument it looks at `~/.dsh/profiles/`: one profile is used directly, several are offered as a choice, none falls back to `web`.

### D. From source

```bash
git clone https://github.com/Breeze136/dsh-kb-rag.git && cd dsh-kb-rag
./npm-package/scripts/install.sh        # macOS / Linux / Git Bash
# Windows: install.cmd, or npm-package\scripts\install.ps1
```

On Windows, if DSH is not installed yet, the companion [dsh-oneclick](https://github.com/Breeze136/dsh-oneclick/releases/latest) zip installs Node.js, the DSH CLI, a desktop shortcut, and optionally this plugin with its Python dependencies and models. Unzip it completely and double-click `install.cmd`. It is a third-party helper, not a DeepSeek release, and it installs the official packages through their official channels.

If your desktop build cannot search npm, install [dsh-plugin-registry](https://github.com/beancookie/dsh-plugin-registry) once and use its marketplace panel. kb-rag is listed there.

> [!WARNING]
> Do not run `npm install dsh-kb-rag` inside a DSH profile. It writes an npm-style `node_modules` next to pnpm's symlink store, and later `dsh plugin` operations become unpredictable. `npm install` is only for a deployment you manage by hand; see [npm-package/README.md](npm-package/README.md).

## Build a library

In a DSH conversation, ask the agent to ingest a folder (`kb_ingest`) or to sync Zotero (`kb_zotero`). Papers can be fetched by DOI or arXiv ID first (`kb_fetch`). It tries the publisher version, which works on campus or through an institutional subscription, then falls back to open access.

Above `KB_ASYNC_THRESHOLD` (default 25) pending files, `kb_ingest` forks the batch into a background job and returns a `job_id` immediately. Poll it with `kb_status` until it reports `done`; a host-side timeout does not interrupt the job. The same applies to whole-library Zotero migrations with `kb_zotero(async_mode=true)`.

## Ask

- "Which papers discuss graphene growth on copper?" → `kb_search`
- "Which page states this?" → the `page` field on the evidence, or the Zotero page link
- "Answer only from the library" → strict mode through `kb_scope` (DSH)
- "Quick look or deep analysis?" → both tools default to `quick`; pass `depth: "deep"` for reranking, citation linking and related work, or set a session default with `/kb deep`

For a formatted answer, `kb_rag` accepts an `answer` object and writes a self-contained HTML page next to a gallery of earlier answers. Ten templates cover an instant one-screen card, a review, a method breakdown, a comparison, numeric findings, a timeline, a glossary, disputes, a derivation, and Q&A. See [HTML answers](#html-answers).

## Tools

The DSH plugin registers 10 tools.

| Tool | Purpose |
|---|---|
| `kb_ingest` | Ingest files or folders (PDF/TXT/MD/DOCX). Incremental skip, deduplication, section chunking, vectorisation. `metadata_only=true` refreshes title/authors/year/journal/DOI in place; `rebuild=true` re-parses every indexed document; large batches move to a background job |
| `kb_status` | Poll a background job: progress while `running`, totals and recent files when `done`, or the error |
| `kb_zotero` | Migrate a local Zotero library, including PDF attachments |
| `kb_search` | Hybrid retrieval returning passages with title, authors, year, journal, DOI, page and section |
| `kb_rag` | Evidence-based answering with numbered citations; optional HTML answer page |
| `kb_scope` | Query scope (library / library plus web / web), strict mode, retrieval depth |
| `kb_dedup` | Remove duplicate documents, keeping the earliest copy |
| `kb_clear` | Wipe all documents and indexes; requires `confirm=true` |
| `kb_stats` | Document, chunk and vector counts, recent ingests, model cache state, stale documents |
| `kb_fetch` | Download a PDF by DOI or arXiv ID (publisher version first, open-access fallback) |

The MCP server exposes 10 tools as well, including `kb_status` and a diagnostic `kb_mcp_status`. `kb_scope` is DSH-specific and has no MCP equivalent. MCP keeps `kb_rag`'s default at `deep`; the DSH plugin defaults to `quick`. Configuration and client snippets: [mcp-server/README.md](mcp-server/README.md).

### Engine capabilities

- Chunking is section-aware: abstracts get weight 1.5, methods 1.2. It also handles inline headings, abstract promotion, caption blocks and paragraph merging for non-article documents.
- Retrieval combines BM25 (with CJK bigrams), bge-small vector cosine, RRF fusion and section weights.
- Reranking uses bge-reranker-base to cut the top 20 down to 3, with a bge-large-en bi-encoder as fallback.
- Every passage carries its PDF page and section. The page renders as `section · p.N` and maps onto Zotero's `?page=N` link.
- In-text `[n]` markers resolve to reference entries. Nature-style superscripts are detected from font metrics, so `graphene1,2` becomes `graphene[1,2]`. Cited works are matched by DOI, normalised title, or first author plus year, and in-library matches are marked.
- SHA-256 hashes skip unchanged files (about 40× faster on re-runs) and catch duplicate copies across folders.
- Each document records the parser revision that wrote it (`docs.indexed_with`), so `kb_stats` can report rows an older revision left behind.
- Identical queries hit a cache. Any ingest or engine rebuild clears it.
- The engine runs as a resident daemon. Models load once, the daemon recovers from crashes, and it is released when the plugin stops.
- If the library's vectors were built with a different embedding model, retrieval stops with an explicit error instead of falling back to keyword search.

## Query guidance

The engine passes queries through unchanged. It does not translate, expand or rewrite them, so what you type has to match the language of the indexed text. Most libraries are about 98% English, so a few rules matter in practice.

In a DSH session the agent writes the query. Its tool description already turns a Chinese question into an English term string, so asking in Chinese works. Write English term strings yourself only when you drive the tools directly (MCP clients, scripts, the engine CLI).

BM25 matches tokens. A CJK query cannot match English body text, so the keyword half of hybrid ranking contributes nothing and the hit rests on vector similarity. For the same question, an English term string retrieves better.

Keep queries to 3-12 words in the pattern `material/system + method/process + property/characterisation`, for example `graphene CVD copper single crystal nucleation suppression`. Do not write a full sentence.

Put limits in `filters`, not in the query. Year, journal, author, section and file kind are metadata filters. `journal` is filled only by the Zotero migration path, so it is `NULL` for documents indexed with `kb_ingest`. Filtering on it usually returns nothing. Use author, year, title or section instead.

Send a second query in the original language only when you want documents in that language, for example Chinese-language reviews in the same library.

When a CJK query hits an almost entirely English library, the response carries a `lang_note` and the plugin renders it with the results.

## HTML answers

`kb_rag` accepts an `answer` object and returns a self-contained HTML file plus a compact text card. The engine lays out the page itself; there is no model call and the file makes no external requests.

```jsonc
{ "query": "graphene CVD nucleation density", "depth": "quick",
  "answer": { "stage": "instant",                       // one screen, 300-character budget
              "template": "compare",                    // review | method | compare | numeric | timeline | concept | dispute | derivation | qa
              "fields": { "bottom_line": { "text": "…", "refs": [1] },
                          "matrix": { "columns": [{ "label": "Pre-treatment" }, { "label": "Density", "num": true }],
                                      "rows": [["Mechanical polish", "10⁶–10⁷"]] } } } }
```

`refs` are 1-based indexes into the call's `evidence`; out-of-range ones are rejected before rendering. Each field is capped by the template's limit and the envelope by `budget.max_total_chars`. Figures are computed from the numbers you supply: the renderer picks axis ranges, tick spacing, bar lengths and heat levels, so do not pre-compute them.

The page has four colour presets (blue, green, ink, warm), switchable from a control at the bottom or fixed for the workspace in `.kb-rag/answer-theme.json`. The footer ends with the retrieval and render time for that answer:

```text
检索 1.6 s · 渲染 12 ms
```

Files land in `~/.kb-rag/answers/` (override with `KB_ANSWER_DIR` or `answer_dir`) next to an `index.html` gallery. Text-only clients still get the card; the HTML file is an addition, not a dependency.

## MCP

```bash
pip install -r mcp-server/requirements.txt
python mcp-server/server.py
```

Register it as a stdio server in Claude Desktop, Cherry Studio, Kimi, Cursor, DeepSeek or another MCP host. The server shares the same engine and the same `kb.sqlite`. By default it uses `~/.kb-rag` rather than the DSH workspace `.kb`, and every tool accepts `kb_root` to point elsewhere. See [mcp-server/README.md](mcp-server/README.md) for config snippets, capability quarantine and timeout behaviour.

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
   |              under .kb-jobs/ and a job_id is returned for kb_status to poll)
   |-- search:  SQL prefilter -> BM25 + vector -> RRF fusion -> bge-reranker rerank
   |             -> top-N verbatim passages with DOI, page, section and score
   `-- storage: <kb_root>/kb.sqlite (docs, chunks, vecs, cache; schema v4,
                migrations gated by PRAGMA user_version)
```

## Measured performance

Windows, CPU inference unless noted. Query numbers come from a 248-document / 20,760-chunk library; ingest numbers from a 242-file / 1.8 GB run.

| Metric | Result |
|---|---|
| Query latency (warm) | `quick` 0.33-0.38 s; `deep` 1.4-1.9 s, median 1.6 s |
| Cold first call | up to 8.9 s while models load |
| Ingest throughput | 242 PDF/DOCX files (1.8 GB) in 85.9 s, about 355 ms per document |
| Incremental re-run | same directory in 2.17 s, a 40× speed-up |
| Full re-index (GPU) | 316 PDFs / 23.5k chunks / 20.1k vectors in 219 s on an 8 GB consumer GPU, zero errors |
| Embedding throughput | 164 chunks/s on GPU vs 37 chunks/s on CPU (~4.4×); flat from batch 32 to 256, so tokenisation is the limiting factor |

Methodology and design rationale: [`docs/DESIGN.md`](docs/DESIGN.md).

### Device handling (GPU / CPU)

Ingestion and search share one embedding model. A CUDA build of torch is used automatically (`KB_DEVICE=auto`, the default).

- The device is probed before the first model load with a tiny matmul. A driver can report a GPU and still fail at the first kernel, so the probe catches that early and falls back to CPU with a readable reason. `KB_GPU_PROBE=0` skips the probe.
- Device failures during a call degrade instead of aborting: the batch is halved, then cut to 4, then the model moves to CPU and the call finishes. The batch size that worked is remembered. Non-device errors are raised unchanged.
- `reload` resets the device verdict after a driver fix. `drop_models=true` reloads the models onto the GPU without restarting the host.
- Batch sizes follow the device and free VRAM. `KB_EMBED_BATCH` / `KB_RERANK_BATCH` override them, but raising them does not speed up small models.
- CPU-only is about 4× slower on the embedding step. PDF extraction, chunking and BM25 are CPU-bound either way.

`kb_stats` reports the outcome in `device`, including `gpu_disabled_reason` and `note` when a fallback happened.

## Configuration

| Variable | Default | Applies to | Description |
|---|---|---|---|
| `KB_EMBED_MODEL` | `BAAI/bge-small-zh-v1.5` | Engine | Embedding model, downloaded to the Hugging Face cache on first use |
| `KB_RERANK_MODEL` | `BAAI/bge-reranker-base` | Engine | Reranking model |
| `KB_DEVICE` | `auto` | Engine | `auto` uses a working CUDA device; `cpu` / `cuda` / `cuda:1` / `mps` force a choice. Device failures fall back to CPU |
| `KB_GPU_PROBE` | `1` | Engine | `0` skips the one-off "can this device compute?" probe |
| `KB_EMBED_BATCH` | GPU 128 / CPU 32 (tiered below 8 GB) | Engine | Embedding batch size for ingest and search |
| `KB_RERANK_BATCH` | GPU 64 / CPU 16 (tiered below 8 GB) | Engine | Reranking batch size |
| `KB_MODEL_RETRY_SECS` | `120` | Engine | How long a failed model load is remembered before retrying (`0` = every call, negative = never) |
| `HF_ENDPOINT` | none | Engine | `https://hf-mirror.com` on restricted networks |
| `KB_HF_MIRROR_FIRST` | off | Engine | `1` tries the mirror before the direct download |
| `KB_AUTO_PIP` | `0` | npm package | `1` installs missing Python dependencies at startup (fixed argv); by default only prints the command |
| `KB_RAG_ROOT` | DSH: workspace `.kb`; MCP: `~/.kb-rag` | MCP | Knowledge base directory; per-call override with `kb_root` |
| `KB_RAG_PYTHON` | current interpreter | MCP | Interpreter used for the engine |
| `KB_ASYNC_THRESHOLD` | `25` | Engine | Pending files above which `kb_ingest` forks a background job |
| `KB_SQLITE_WAL` | off | Engine | `1` enables SQLite WAL; the default is safer when `.kb` is synchronised |
| `UNPAYWALL_EMAIL` | built-in placeholder | Engine | Contact address for Unpaywall in `kb_fetch`; set your own |

A workspace file `.kb-rag/engine.env` (KEY=VALUE, one per line) is merged into the engine's environment at startup; explicit environment variables win. It is the practical way to pin a model or mirror per library without editing system settings.

## Repository layout

```text
kb-rag/
├─ kb_engine.py              Python engine: chunking, retrieval, reranking, serve daemon
├─ install.cmd               Windows entry point (runs scripts\install.ps1)
├─ scripts/                  Installer scripts (install.ps1, install.sh)
├─ plugin/                   DSH dynamic plugin (kbrag.plugin.json, host.js, client.js)
├─ npm-package/              npm package dsh-kb-rag (published contents, cordis.patch.yml)
├─ dsh-kb-rag-install/       Micro-package for the bare `npx dsh-kb-rag-install` command
├─ mcp-server/               MCP server (server.py, engine_client.py)
├─ docs/                     DESIGN.md, OUTPUT-FORMAT.md, MIGRATION.md, BACKLOG.md
├─ tests/                    Engine and client test suites
├─ tools/                    Internal maintenance scripts (not published)
└─ QUICKSTART.md, CHANGELOG.md, SECURITY.md, UNINSTALL.md, LICENSE
```

Runtime data: the DSH plugin writes to `.kb/kb.sqlite` in the session workspace; the MCP server defaults to `~/.kb-rag/kb.sqlite`. Background job files live in `<kb_root>/.kb-jobs/` and are removed when a job finishes.

## Known limitations

- Scanned PDFs are not supported. Documents without a text layer are skipped; OCR is out of scope.
- Page anchors are PDF-only. TXT, MD and DOCX files, and documents indexed before schema v3, fall back to section-level location until re-ingested with `force`.
- Citation linking requires a re-ingest. Superscript detection and reference splitting run at parse time.
- Metadata can be misread. When PDF metadata is missing, title and year are inferred from page text; Zotero metadata overrides this.
- Cross-language retrieval is weak at the engine level. The engine never translates; a CJK query against English text leans on the vector path and the response carries a `lang_note`. The agent layer compensates by rewriting the query.
- Captions are searchable as text; content that exists only inside a figure is not.
- Keyword matching is in memory. Beyond a few hundred thousand chunks, FAISS HNSW or SQLite FTS5 would be the next step.
- Some publishers redirect automated PDF requests to a login page. `kb_fetch` reports the failure and leaves the manual path (download, then `kb_ingest`) as the fallback.

## Documentation

| Document | Contents |
|---|---|
| [QUICKSTART.md](QUICKSTART.md) | Five-minute setup |
| [docs/DESIGN.md](docs/DESIGN.md) | Storage model, chunking, retrieval pipeline, engine protocol |
| [docs/OUTPUT-FORMAT.md](docs/OUTPUT-FORMAT.md) | Output and citation conventions |
| [docs/MIGRATION.md](docs/MIGRATION.md) | Schema migration, v1 through v4 |
| [docs/BACKLOG.md](docs/BACKLOG.md) | Known gaps and how to verify a change |
| [mcp-server/README.md](mcp-server/README.md) | MCP configuration, tool mapping, timeouts |
| [npm-package/README.md](npm-package/README.md) | npm installation and troubleshooting |
| [SECURITY.md](SECURITY.md) | Execution model and security boundaries |
| [UNINSTALL.md](UNINSTALL.md) | Removal, leaving PDFs and Zotero untouched |
| [CHANGELOG.md](CHANGELOG.md) | Release history |

## Contact

- Bugs and feature requests: [GitHub Issues](https://github.com/Breeze136/dsh-kb-rag/issues)
- Questions: [GitHub Discussions](https://github.com/Breeze136/dsh-kb-rag/discussions)
- Security: see [SECURITY.md](SECURITY.md)

## License

[MIT](LICENSE). The `BAAI/bge-*` models are downloaded at runtime and remain under their own licences.
