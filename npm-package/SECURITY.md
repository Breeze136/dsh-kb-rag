# Security

kb-rag executes code on your machine like any DSH plugin — review it before installing.
This document lists exactly what it runs and touches, so you can verify it against the source.

## Execution model

- The npm package (`dsh-kb-rag`) registers 8 model tools in the DSH host process (Node.js, ESM plugin).
- Tools talk to a resident Python engine (`kb_engine.py`, bundled in the package) over a
  stdin/stdout JSON-lines protocol. No network server is opened.
- The plugin spawns Python at exactly two sites, both with **fixed argv arrays**
  (no shell, no user input interpolation):

  1. `python <package>/kb_engine.py serve` — the engine daemon, started lazily on the first tool call.
  2. `python -c "import fitz, faiss, sentence_transformers, torch"` — one-shot startup dependency probe.

- argv is built as an array (`SubprocessSpawnSpec.argv`); nothing is ever concatenated into a
  `cmd`/`sh` command string.

## What it reads and writes

- **Reads**: only documents you explicitly ingest — the PDF/TXT/MD/DOCX paths passed to
  `kb_ingest`, and the Zotero database + PDF attachments passed to `kb_zotero`.
- **Writes**: only the knowledge-base directory (`<workspace>/.kb` by default, `kb_root` to
  override): one SQLite database and the query cache. Tool arguments are the only sources of paths.
- **Network**: on first use, the embedding/reranker models download from Hugging Face
  (`HF_ENDPOINT` is respected, e.g. `https://hf-mirror.com`). Nothing is uploaded. The JS host
  makes no network calls, and `package.json` declares no install scripts.

## Why automated scanners flag it

Registry scanners (such as dsh.so security reports) bucket every process spawn as "shell" and
every plugin/tool registration as "code-exec". A DSH plugin *is* executable code by design, and
any plugin that spawns a helper process lands in those buckets — the same buckets as official
shell tools. The sites listed above are the complete inventory of what the scanner sees; each one
uses fixed argv and reads/writes only the paths named above.

## Residual risk

The Python engine runs with the same OS privileges as the DSH process. Ingest only documents
you trust (the same rule as opening a PDF in any viewer). Installing any DSH plugin means running
third-party code — automated scans are not a guarantee; read the source.

## Reporting

Issues: <https://github.com/Breeze136/dsh-kb-rag/issues>
