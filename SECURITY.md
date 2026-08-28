# Security

kb-rag executes code on your machine like any DSH plugin — review it before installing.
This document lists exactly what it runs and touches, so you can verify it against the source.

## Execution model

- The npm package (`dsh-kb-rag`) registers 8 model tools in the DSH host process (Node.js, ESM plugin).
- Tools talk to a resident Python engine (`kb_engine.py`, bundled in the package) over a
  stdin/stdout JSON-lines protocol. No network server is opened.
- The plugin spawns Python at exactly three sites, all with **fixed argv arrays**
  (no shell, no user input interpolation):

  1. `python <package>/kb_engine.py serve` — the engine daemon, started lazily on the first tool call.
  2. `python -c "import importlib.util, json; print(json.dumps(...))"` — one-shot startup dependency probe
     (reports the complete missing-package list; nothing is executed from its output beyond `JSON.parse`).
  3. `python -m pip install --disable-pip-version-check <missing>` — **opt-in only**: runs at startup when the
     environment variable `KB_AUTO_PIP=1` is set and the probe found missing packages. Off by default;
     without it the plugin only logs the suggested pip command and never installs anything.

- argv is built as an array (`SubprocessSpawnSpec.argv`); nothing is ever concatenated into a
  `cmd`/`sh` command string. The missing-package list inserted into the pip argv comes from the
  plugin's own fixed module→package mapping, not from free-form user input.

## What it reads and writes

- **Reads**: only documents you explicitly ingest — the PDF/TXT/MD/DOCX paths passed to
  `kb_ingest`, and the Zotero database + PDF attachments passed to `kb_zotero`.
- **Writes**: only the knowledge-base directory (`<workspace>/.kb` by default, `kb_root` to
  override): one SQLite database and the query cache. Tool arguments are the only sources of paths.
- **Network**: on first use, the embedding/reranker models download from Hugging Face
  (`HF_ENDPOINT` is respected, e.g. `https://hf-mirror.com`). Nothing is uploaded. By default the
  JS host makes no network calls; with `KB_AUTO_PIP=1` set it additionally runs `pip install`,
  which downloads from PyPI (or `PIP_INDEX_URL` if configured). The standalone installers
  (`npm-package/scripts/install.ps1` / `npm-package/scripts/install.sh`) also run `pip`/`npm`/`dsh` — read them before
  running, same as any install script. `package.json` still declares no install scripts.

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
