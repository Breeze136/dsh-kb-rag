# dsh-kb-rag

[![npm version](https://img.shields.io/npm/v/dsh-kb-rag)](https://www.npmjs.com/package/dsh-kb-rag)
[![npm downloads](https://img.shields.io/npm/dm/dsh-kb-rag)](https://www.npmjs.com/package/dsh-kb-rag)
[![GitHub release](https://img.shields.io/github/v/release/Breeze136/dsh-kb-rag)](https://github.com/Breeze136/dsh-kb-rag/releases)
[![MIT](https://img.shields.io/github/license/Breeze136/dsh-kb-rag)](LICENSE)
[![Awesome DSH Plugin](https://beancookie.github.io/awesome-dsh-plugin/badge.svg)](https://beancookie.github.io/awesome-dsh-plugin)
[![dsh.so security](https://www.dsh.so/badges/kb-rag.svg)](https://www.dsh.so/artifact/kb-rag/)

Static DSH plugin for a local literature knowledge base. It indexes PDFs, folders or a Zotero library into one SQLite file and gives the model ten tools for ingest, hybrid search, cited answers and HTML reports. Embedding and reranking run locally.

> **Latest version 1.6.8.** Install with `dsh plugin --profile web add dsh-kb-rag@1.6.8`. A DSH profile is a pnpm workspace, so do not run `npm install` inside it; for a hand-managed deployment see [Option 3](#option-3--manual-npm-install).

This release fixes the DSH 0.2 install path (`peerDependencies` and the client injection list), the tool cards, and adds engine-rendered HTML answers with retrieval/render time in the footer.

## Tools (10)

| Tool | Purpose |
| --- | --- |
| `kb_ingest` | Ingest files or folders (PDF/TXT/MD/DOCX, recursive). Incremental skip, dedup, section-aware chunking, vectorisation; `metadata_only=true` refreshes title/authors/year/journal/DOI in place, `rebuild=true` re-parses every indexed document, and a large batch moves to a background job |
| `kb_status` | Poll a background job by `job_id`: progress while `running`, totals and recent files when `done` |
| `kb_zotero` | Migrate a local Zotero library, including PDF attachments |
| `kb_search` | Hybrid search returning passages with title, authors, year, journal, DOI, section and PDF page |
| `kb_rag` | Retrieve evidence for the model to answer from, with numbered citations; optional self-contained HTML answer |
| `kb_scope` | Query scope (kb / both / web), strict mode and retrieval depth |
| `kb_stats` | Document, chunk and vector counts, recent ingests, model cache state, stale documents |
| `kb_dedup` | Remove duplicate documents, keeping the earliest |
| `kb_clear` | Wipe all documents and indexes (requires `confirm: true`) |
| `kb_fetch` | Download a PDF by DOI or arXiv ID (publisher version first, open-access fallback) |

Citations render as `[authors, year, journal](https://doi.org/DOI)` when a DOI exists, and as `[authors, year, filename]` when it does not. Search and RAG results also carry a related-literature list used by the answer's "suggested additions" note.

### HTML answers

Ask for one in the conversation ("write this up as a shareable HTML report"); the agent passes an `answer` object to `kb_rag`. A plain `kb_rag` call returns evidence for the model to answer from and writes no file. The engine lays out the page itself, with no model call and no second round trip, and the `answer_file` object carries `path`, `url`, `bytes`, `template`, `layout` and `warnings`.

```jsonc
{
  "query": "graphene CVD nucleation density on copper",
  "depth": "quick",
  "answer": {
    "template": "compare",                    // review is the default
    "fields": {
      "bottom_line": {
        "text": "Pre-treatment changes nucleation density by up to two orders of magnitude; oxygen plasma gives the lowest density and the highest monolayer coverage.",
        "refs": [1, 2]                        // 1-based indexes into this call's evidence
      },
      "matrix": {
        "columns": [
          { "label": "Pre-treatment" },
          { "label": "Density (cm⁻²)", "num": true },
          { "label": "Monolayer coverage", "num": true }
        ],
        "rows": [
          ["Mechanical polish", "10⁶–10⁷", "80–90%"],
          ["Electropolishing", "10⁶–10⁷", "90–95%"],
          ["Oxygen plasma", "10⁵–10⁶", "95–97%"]
        ]
      },
      "reading": ["Ranges come from different experiments; compare within a column, not across rows."]
    }
  }
}
```

**Templates.** The template fixes the sections, their order and each block's renderer; fields it does not define are ignored, and empty ones are skipped.

| `template` | Use it for | Main fields |
| --- | --- | --- |
| `instant` | one-screen answer to a single question | `answer`, `limit`, `followup` |
| `review` | synthesis across several papers | `bottom_line`, `state`, `methods`, `limits`, `takeaway` |
| `method` | one method end to end | `what`, `when`, `steps`, `params`, `figure`, `caveats` |
| `compare` | side-by-side comparison | `bottom_line`, `matrix`, `numbers`, `reading` |
| `numeric` | when the numbers are the point | `bottom_line`, `kpis`, `numbers`, `trend`, `reading` |
| `timeline` | how a line of work developed | `bottom_line`, `line` |
| `concept` | terms and definitions | `cards` |
| `dispute` | papers that disagree | `bottom_line`, `issues` |
| `derivation` | a derivation with a checkable result | `conventions`, `deriv`, `figure`, `result`, `code`, `verify`, `pitfalls` |
| `qa` | a concept explained in Q&A form | `intro`, `questions`, `summary` |

`stage: "instant"` is a shortcut: it selects `instant`, sets a 300-character budget, and forces `quick`. `layout: "doc"` switches from the card layout to long-form. `summary` supplies a one-line lead when the template has no `bottom_line`, or that field is empty.

**Fields.** A section takes a string, `{ text, refs }`, an array, a table (`{ columns: [{ label, num, heat, bar }], rows: [[...]] }` or a markdown pipe table), `{ items: [{ label, value, unit }] }` for `rank` and `kpis`, `{ x, series }` for `chart`, and `{ nodes, edges }` for `flow`. Write numbers and units as the paper gives them; the renderer derives axis ranges, ticks, bar lengths, heat levels and sort order, so do not pre-compute them. Fields are clipped at the template limit, and the whole envelope at `budget.max_total_chars` when set; every clip is listed in `warnings`.

**Files.** Pages land in `~/.kb-rag/answers/` as `<date>_<question-slug>-<hash8>.html`, next to `index.html` (a gallery of the latest 200 answers, newest first) and `index.json`. Set `answer_dir` on the call (an absolute path, or one starting with `~`), or `KB_ANSWER_DIR` in the environment, to move the folder. The filename carries a content hash, so re-rendering the same envelope on the same day overwrites one file instead of adding another.

**Open, print, share.** Open the file in any browser; it needs no server and no network. Formula pages are the one exception: they load MathJax from jsDelivr and fall back to raw TeX if that request fails. Two buttons at the top print or export to PDF and copy the body as Markdown; Ctrl/Cmd+P works too, the layout holds down to phone width, and the footer reports retrieval and render time. Text-only clients still get the compact card; the file is an addition, not a dependency.

**Colours.** Four presets, blue, green, ink and warm, switch from the footer control and are remembered in that browser. To pin a default for a workspace, create `.kb-rag/answer-theme.json` next to `.kb`; a `theme` object on the call overrides it.

**Limits in 1.6.8.** The page chrome is Chinese; English chrome is planned for the next release. A `refs` number outside this call's evidence is reported in `warnings` and renders as a marker with no source, so fix it before sharing. Unknown `template` names fall back to `review` in the engine; the DSH tool schema rejects them first.

| Symptom | Fix |
| --- | --- |
| Text answer only, no file | `answer` was not passed; ask for a report explicitly |
| `answer_file.ok` is false | the error text names the cause; fix it and re-run |
| A section is missing | its field was empty; empty sections are omitted |
| A field ends in `…` | the field hit its limit; `warnings` says which one |
| A citation marker has no source | its `refs` number is outside this call's evidence; `warnings` names it |
| Chinese labels in an English report | 1.6.8 chrome is Chinese; the body text is unchanged |

### Engine capabilities

- Section-aware chunking: abstract ×1.5, methods ×1.2, inline heading detection, abstract promotion, caption blocks, paragraph merging for non-article documents.
- Hybrid retrieval: BM25 with CJK bigrams, bge-small vector cosine, RRF fusion, section weights.
- Reranking: bge-reranker-base cross-encoder (top 20 into top 3), with a bge-large-en fallback.
- Provenance: section, paragraph range, physical PDF page (schema v3), authors, year, journal, DOI.
- Citation linking: `[n]` markers resolve to references; Nature-style superscripts are detected from font metrics; references are stored at weight 0 and excluded from retrieval.
- Fast and deep modes: `quick` returns hybrid hits directly; `deep` adds reranking, citation linking and related work.
- Incremental indexing and dedup by SHA-256 content hash.
- Metadata refresh and stale detection (schema v4): `docs.indexed_with` records the parser revision; `metadata_only` re-reads page 1 in about 90 ms per document, `rebuild` re-parses in place.
- Query cache: identical query and filters are not recomputed; ingest or an engine rebuild invalidates it.
- Resident daemon: models load once, the daemon recovers from crashes, and it is reclaimed when the plugin stops.
- Background jobs: above `KB_ASYNC_THRESHOLD` (default 25) pending files the batch is forked under `.kb-jobs/`, and `job_id` is polled with `kb_status`.

## Query guidance

Queries go to the engine verbatim. It never translates or rewrites them, so retrieval depends on the query matching the indexed text. A typical library is about 98% English body text:

- Write queries as English term strings. BM25 matches tokens; a CJK query leaves the keyword half idle and the hit rests on cross-language vector similarity alone.
- Use 3–12 words in the pattern `material/system + method/process + property/characterisation`, e.g. `graphene CVD copper single crystal nucleation suppression`.
- Put limits in `filters` (year, journal, author, section, kind), not in the query. `journal` is filled only by the Zotero path, so it stays `NULL` for `kb_ingest` documents; use author, year, title or section instead.
- Send a second query in the original language only when you want documents in that language.

A CJK query against an English library gets a `lang_note` in the response, and the plugin renders it with the results.

## Install

Pick one path. After installing, restart DSH and open a new session; tools are injected when a session is created.

### Option 0 — DSH desktop app (recommended, no terminal)

Open **Settings → Plugins** (设置 → 插件), search `kb-rag`, and install **1.6.8**. Restart DSH and start a new conversation. The desktop app must be 0.2 or newer; 1.6.7 and earlier are rejected by 0.2's compatibility check, which is what this release fixes.

Upgrading from 1.6.7: the panel evaluates the installed 1.6.7 manifest and can refuse 1.6.8 with an error that quotes 1.6.7. Uninstall 1.6.7 first, then install 1.6.8.

### Option 1 — `dsh plugin` (legacy web profile)

New DSH users should use the desktop app above. The web profile is no longer a supported install target; this route stays for existing web or headless deployments.

The package declares `dsh.bundle`, so this installs and activates it in one step:

```bash
dsh plugin --profile web add dsh-kb-rag@1.6.8
```

Pin the version. pnpm's `minimumReleaseAge` refuses packages published minutes ago, so an unpinned install right after a release can resolve to the previous version while looking successful. pnpm must be on PATH (`npm install -g pnpm` once).

### Option 1b — one-shot installer via npx (legacy web/headless)

Runs the bundled `scripts/install.ps1` / `install.sh`: Python dependencies, engine smoke test, Node/pnpm check, `dsh plugin add`, and model pre-download. It targets a DSH profile, so it is not the desktop path.

```bash
npx dsh-kb-rag-install
# equivalent older form:
npx --yes --package dsh-kb-rag -c "dsh-kb-rag-install --profile web"
```

Bash-style flags (`--profile`, `--models`, `--no-models`, `--dry-run`, `--mirror`) work on every OS; the entry translates them for PowerShell.

### Option 2 — run the bundled script directly

From the profile directory where the package is installed:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File node_modules\dsh-kb-rag\scripts\install.ps1
```
```bash
./node_modules/dsh-kb-rag/scripts/install.sh
```

### Option 3 — manual npm install

> [!WARNING]
> Only for deployments you manage by hand. A `dsh plugin` profile is a pnpm workspace (`pnpm-lock.yaml`); an npm-style `node_modules` next to pnpm's symlink store makes later `dsh plugin` operations unpredictable.

```bash
cd <your-dsh-profile-dir>          # e.g. ~/.dsh/profiles/web
npm install dsh-kb-rag@1.6.8
```

Then add `"dsh-kb-rag"` to `dsh.profile.bundles` in the profile's `package.json`, or copy the bundled `cordis.patch.yml` insert into your patch layer. Restart DSH and open a new session.

### Option 4 — plugin marketplace

Install [dsh-plugin-registry](https://github.com/beancookie/dsh-plugin-registry) once; its Settings marketplace panel lists kb-rag and installs it with one click.

### Upgrading

Stay with the tool that installed it:

```bash
dsh plugin --profile web add dsh-kb-rag@1.6.8      # pnpm-managed profile
npm install dsh-kb-rag@1.6.8                        # hand-managed npm deployment
```

Restart DSH and open a new session. `.kb` libraries migrate automatically, but page anchors and superscript citation markers are parse-time data: run `kb_ingest` with `force: true` over old documents to get them.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `npx dsh-kb-rag-install` → E404 | Releases up to 1.6.2 had no package with that name. 1.6.3+ ships the micro-package; if npm cached old metadata, run `npm cache clean --force`. Workaround: `npx --yes --package dsh-kb-rag -c "dsh-kb-rag-install --profile web"`. |
| `dsh plugin ... add` → pnpm errors | pnpm missing from PATH: `npm install -g pnpm`, retry. |
| `install.ps1` → garbled Chinese / syntax error | The script ships as UTF-8 with BOM (1.3.1+). If you copied it manually, re-save with BOM. |
| `install.ps1` → `WinError 123` with `C:\Users\??\...` | Non-ASCII Windows username under PowerShell 5.1. Fixed in 1.6.3 by forcing UTF-8 pipe encoding. Workaround: `$env:TEMP='C:\kbragtmp'; $env:TMP='C:\kbragtmp'`, then re-run. |
| Installed but tools do not appear | Tools are injected at session creation: restart DSH and open a new conversation. |
| First search is slow or seems stuck | The models are downloading (~95 MB + ~1.1 GB). Check `kb_stats.models`; set `HF_ENDPOINT=https://hf-mirror.com` or `KB_HF_MIRROR_FIRST=1`, or pre-download with `huggingface-cli`. `KB_MODEL_RETRY_SECS` controls retry after a failure. |
| Tool call says Python deps are missing | By default only the `pip install` command is logged. Set `KB_AUTO_PIP=1` to install, or run the installer. |
| A publisher blocks `kb_fetch` | Some sites redirect automated requests to a login page. Download manually and run `kb_ingest`; the error text says so. |

## Requirements

- Node.js ≥ 18
- Python 3.9+ with:

```bash
pip install pymupdf faiss-cpu sentence-transformers
```

The plugin checks these at startup and reports the complete missing list. Missing packages do not block plugin loading; tool calls return the exact `pip install` command.

GPU or CPU: a CUDA build of torch is used automatically (`KB_DEVICE=auto`). A probe before the first model load catches drivers that report a GPU but cannot compute; failures fall back to CPU with the reason in `kb_stats.device`. CPU-only is about 4× slower on embedding; PDF parsing is CPU-bound either way. `KB_EMBED_BATCH` / `KB_RERANK_BATCH` override batch sizes.

Models `BAAI/bge-small-zh-v1.5` and `BAAI/bge-reranker-base` download on first use into the local Hugging Face cache.

Peer dependencies: `@deepseek-ai/cordis` ^4, `@deepseek-ai/dsh-tools` (0.2.x).

## Usage

1. `kb_ingest(paths=["papers/", "notes.md"])`; if it returns a `job_id`, follow with `kb_status(job_id="...")` until `done`.
2. `kb_zotero(dry_run=true)` to preview, then run it for real.
3. `kb_search(query="graphene domain coalescence during CVD growth on copper", top_k=5, filters={year: ">=2018"})`.
4. `kb_rag(query="How is layer thickness determined from the Raman 2D peak?", strict=true)`.
5. `kb_scope(scope="both")`; `kb_stats()` shows what is in the library.
6. Add `answer={...}` to a `kb_rag` call for an HTML report; open `answer_file.path`, or browse the gallery at `~/.kb-rag/answers/index.html`.

Data persists in the session workspace `/.kb` by default; every tool accepts `kb_root` to override.

## Notes

- Host-side static plugin: all tools run server-side, and there is no management panel by design. Operations and inspection happen through the conversation and tool returns.
- The engine runs as a resident subprocess from the bundled `kb_engine.py` (JSON-lines protocol) and exits when the session ends.
- On restricted networks, prepare the model cache and Python dependencies beforehand.

## Security

See [SECURITY.md](SECURITY.md) for the execution model: what the plugin spawns, reads, writes and downloads, and why registry scanners flag process-spawning plugins as "shell".

## License

MIT
