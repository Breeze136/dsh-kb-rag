# kb-rag — Local Literature Knowledge-Base RAG

<p align="center">
  <b>把脑子里的模糊记忆，变成一条能点开的文献坐标。</b><br/>
  <i>Turn a fuzzy memory into an exact passage / figure — one-click DOI to source.</i>
</p>

[![npm version](https://img.shields.io/npm/v/dsh-kb-rag)](https://www.npmjs.com/package/dsh-kb-rag)
[![npm downloads](https://img.shields.io/npm/dm/dsh-kb-rag)](https://www.npmjs.com/package/dsh-kb-rag)
[![GitHub release](https://img.shields.io/github/v/release/Breeze136/dsh-kb-rag)](https://github.com/Breeze136/dsh-kb-rag/releases)
[![MIT](https://img.shields.io/github/license/Breeze136/dsh-kb-rag)](LICENSE)
[![Awesome DSH Plugin](https://beancookie.github.io/awesome-dsh-plugin/badge.svg)](https://beancookie.github.io/awesome-dsh-plugin)
[![dsh.so security](https://www.dsh.so/badges/kb-rag.svg)](https://www.dsh.so/artifact/kb-rag/)

> **最新版本 v1.6.2**（npm 包名：`dsh-kb-rag`）— Ingest once, search forever. Only the most relevant few sentences ever reach the LLM — and every claim carries exact provenance.

## Who it's for

Graduate students and PhD researchers. An idea strikes, and you *know* it's somewhere in your library — but which paper said it, and where? kb-rag makes the whole pile queryable: hybrid retrieval + reranking surface the right passages, every answer lands on a clickable DOI (or the exact file), and the reply tells you what your library is still missing. **Think it → find it → cite it.**

kb-rag 是给 **DSH（DeepSeek Harness）** 的轻量本地文献知识库 RAG 插件：把 PDF/Zotero 文献变成带章节结构与向量索引的 SQLite 知识库，提供混合检索 + 精排 + 带引文问答的全流程。索引、嵌入、重排全部在本地完成——零 API 费用、零上传，dsh.so 安全扫描 passed。

**面向两类读者：**

- **DSH 用户** — 本文的安装指引围绕 DSH 插件展开（9 个工具，含 `kb_scope` 查询范围控制），往下看「快速安装」。
- **MCP 桌面 agent 用户**（Claude Desktop / Cherry Studio / Kimi / DeepSeek / Cursor 等）— 共用同一引擎，但没有 DSH 会话概念（`kb_scope` 换成 `kb_status`）。直接看 [mcp-server/README.md](mcp-server/README.md)。

## 三种消费形态 Three ways to consume

同一套引擎（`kb_engine.py`）、同一份数据格式，三个入口：

1. **DSH 插件（主形态）** — `plugin/`。在 DSH 会话里以 9 个工具对话式使用：入库、检索、带引文问答、范围控制。引擎 `kb_engine.py` 由插件 Host 拉起（常驻 serve daemon），数据默认在会话工作区 `.kb/`。
2. **MCP server** — `mcp-server/server.py`，stdio 协议，把同一知识库暴露给支持 MCP 的桌面 agent。9 个工具与插件同源，但用 `kb_status`（后台任务轮询）替换 `kb_scope`——scope/strict 是 DSH 会话概念，MCP 版由调用方（agent 的提示词）自行把握。配置见下方「MCP 配置」与 [mcp-server/README.md](mcp-server/README.md)。
3. **npm 静态包 `dsh-kb-rag`** — 面向其他 DSH/Harness 部署用户。包声明了 `dsh.bundle` 并随包分发 `cordis.patch.yml`，`dsh plugin --profile <name> add dsh-kb-rag` 即可一步安装 + 激活；包内还带 `install.mjs`（npx 一键安装器入口）与 `scripts/doi_pdf.mjs`（`kb_fetch` 的 Node 下载器）。安装命令见「快速安装」方式 B。

## What's new（v1.6.x）

- **异步入库 async（MCP 宿主 60s 超时的解药）**：MCP 端 `kb_ingest` 待处理文件数超过 `KB_ASYNC_THRESHOLD`（默认 25）自动转后台（agent 无需知道 async_mode 的存在），`async_mode=true` 可强制、`false` 强制同步；MCP 端 `kb_zotero(async_mode=true)` 可整库后台。两者立即返回 `job_id`，用 `kb_status(job_id=...)` 轮询直到 done——宿主超时（如 Kimi Work 60s）不影响后台任务，任务照常跑完。
- **PDF 页码锚点（schema v3）**：chunks 表新增 `page_start/page_end`（PDF 物理页码），检索结果带页码，渲染为「§章节 · p.N」，可配合 Zotero `zotero://open-pdf?page=N` 一键跳页。旧库自动迁移（`PRAGMA user_version` 门控）；旧数据页码为 NULL，`force` 重入库后恢复。
- **引擎健壮性**：逐文件 commit（async 期间并发读不锁库）；`job_id` 严格校验（12 位十六进制，杜绝路径穿越）；任务结束自动清理 `.kb-jobs/` 残留（`kb_clear` 一并清空）；spawn 后短窗口 poll，启动即失败的任务立即报错，不留"幽灵任务"。
- **渲染与体积**：证据引文关联（正文 `[n]` 引用 → References 条目，渲染「↳ 引文补充」，供补库/深读）；ingest/zotero 的文件清单只回最近 20 条 + `files_total` 真实总数（针对 Kimi Work 等宿主体积限制）。

## Architecture

```
DSH model / MCP client (Claude · Cherry · Kimi · Cursor …)
   │  tool call: kb_ingest / kb_search / kb_rag / kb_stats …
   ▼
plugin Host (JS) 或 MCP server (server.py + engine_client.py)
   │  JSON-lines（stdio，请求-响应逐行）
   ▼
kb_engine.py —— resident `serve` daemon（常驻，模型只加载一次）
   ├─ ingest: sha256 skip → PyMuPDF 提取 → 章节切分 → bge-small 编码（逐文件 commit）
   │     └─ 大批量 → fork async job 子进程（.kb-jobs/，立即返回 job_id，kb_status 轮询）
   ├─ search/rag: SQL 预过滤 → BM25 + 向量双路 → RRF 融合 → bge-reranker 精排
   │                → Top-N 逐字片段 + 来源（DOI / 页码 §p.N / 章节 / 得分）
   └─ storage: <kb_root>/kb.sqlite（docs / chunks / vecs / cache 表，schema v3，
                PRAGMA user_version 门控迁移）
```

数据流：raw PDF → 逐字提取 + 章节切分 → 分块入 SQLite（带元数据与向量）→ 查询时混合检索 + 精排 → Top-N 片段（含 DOI/文件/页码/章节/得分）→ 当前会话的模型据此作答并逐条引用。模型全本地：`BAAI/bge-small-zh-v1.5`（嵌入）+ `bge-reranker-base`（精排），首次使用自动下载到 HF 缓存，受限网络可用 `HF_ENDPOINT` 走镜像。

## Features

### 9 个工具（DSH 插件）

| Tool | 用途 Purpose | 示例说法 |
|---|---|---|
| `kb_ingest` | 文件/文件夹入库：增量跳过 + 去重、章节切分、向量化（PDF/TXT/MD/DOCX）| "把 papers 文件夹入库" |
| `kb_zotero` | 批量迁移本地 Zotero 库（带 PDF 附件的条目）| "同步 Zotero" |
| `kb_search` | 混合检索 Top-N 片段 + 精确来源（标题/作者/年份/期刊/DOI/页码/章节）| "Search chemical vapor deposition of graphene" |
| `kb_rag` | 证据问答：默认 Top-3，逐条编号引用 | "How does graphene CVD growth proceed on copper?" |
| `kb_scope` | 查询范围（封闭库 / 库+全网 / 仅全网）+ 严格模式 | "切到严格模式" |
| `kb_dedup` | 清理重复文档（保留最早，可反复调用）| "去重" |
| `kb_clear` | 清空全部文献与索引（须显式 `confirm=true`）| "清空知识库" |
| `kb_stats` | 文档/分块/向量统计 + 最近入库清单 | "看看库里有什么？" |
| `kb_fetch` | 按 DOI/arXiv ID 下载 PDF（OA only，出版商正式版优先；Node 下载器 `scripts/doi_pdf.mjs` + Python 回退）| "Download 10.1038/s41467-025-56065-9" |

> MCP 版同为 9 个工具：`kb_ingest` / `kb_status` / `kb_zotero` / `kb_search` / `kb_rag` / `kb_stats` / `kb_dedup` / `kb_clear` / `kb_fetch` —— 用 `kb_status`（后台任务轮询）替换 DSH 会话概念 `kb_scope`。

### 引擎能力

- **Structured chunking**：章节识别（摘要 ×1.5、方法 ×1.2 权重）、行内标题检测、摘要自动提升、图注块；非论文按段落兜底
- **Hybrid retrieval**：关键词 BM25（CJK 双字友好）+ bge-small 向量余弦，RRF 融合 × 章节权重
- **Reranking**：bge-reranker-base Cross-Encoder，Top-20 → Top-3（缺失时自动回退 bge-large-en bi-encoder）
- **Incremental & dedup**：sha256 增量跳过（重跑 40× 提速）、跨路径重复拦截、`kb_dedup` 兜底
- **Query cache**：同 query+filters 永不重算；任何入库自动失效
- **Citation standard**：有 DOI → `[作者, 年份, 期刊](https://doi.org/DOI)`；无 DOI → `[作者, 年份, 文件名]`；规范细节见 `docs/OUTPUT-FORMAT.md`
- **Related literature**：每次检索附带关联文献（同作者/同期刊/年份相近/主题相似），答案末尾的"建议补充"优先引用它们
- **Scope & strict**：closed-KB / KB+web / web-only 三档 + strict（禁止库外知识外延，证据不足明说无法回答）
- **Engine daemon**：模型只加载一次、热查询亚秒级、崩溃自愈、插件停止自动回收
- **刻意零 UI**：一切操作经由对话与工具返回完成（检索结果渲染可点击 DOI 链接），无管理面板、无前端状态——定位选择，不是缺失

## 快速安装 Quick Start（DSH 用户）

三种方式选一即可（安装器一条链完成：Python 依赖 → 引擎冒烟 → Node/pnpm 检查（缺 pnpm 自动装）→ `dsh plugin add` 激活 → 可选 `--models` 预下载模型，尊重 `HF_ENDPOINT`）。

**方式 A · npx 一键（推荐，无需先安装包）**

```bash
# ✅ 正确：--package dsh-kb-rag 指明命令来自哪个包（可 pin 版本：--package dsh-kb-rag@1.6.2）
npx --yes --package dsh-kb-rag -c "dsh-kb-rag-install --profile web"
```

> ⚠️ **常见坑**：裸写 `npx dsh-kb-rag-install` 会失败（npx 会去找一个**名为 `dsh-kb-rag-install` 的包**，注册表里不存在 → E404）。必须带 `--package dsh-kb-rag`。Windows 下 bash 风格参数（`--profile`/`--models`/`--dry-run`）会被自动翻译，全平台通用；`--dry-run` 可先演练。

**方式 B · 已装 dsh CLI 的 DSH 用户**

```bash
dsh plugin --profile web add dsh-kb-rag       # 依赖 dsh.bundle 声明，一步安装 + 激活
```

需要 pnpm on PATH（DSH 官方插件流程用 pnpm）。Python 依赖两种补法：设 `KB_AUTO_PIP=1` 重启 DSH 由插件自动 pip 安装（默认关闭、仅打印命令），或直接跑一次方式 A 的安装器。

**方式 C · git clone 后从源码安装**

```bash
git clone https://github.com/Breeze136/dsh-kb-rag.git && cd dsh-kb-rag
./npm-package/scripts/install.sh        # macOS / Linux / Git Bash
# Windows：install.cmd（双击）或 npm-package\scripts\install.ps1
```

装完务必**重启 DSH 并开新会话**（工具在会话创建时注入，老会话不会自动获得）。升级旧版：在 profile 目录 `npm install dsh-kb-rag@latest`（钉版本 `npm install dsh-kb-rag@1.6.2`），重启开新会话；旧 `.kb` 库 schema 自动迁移（见 `docs/MIGRATION.md`）。分步演练与常见坑见 [QUICKSTART.md](QUICKSTART.md)；老式动态插件手动路线（`cordis_define` 加载 `plugin/host.js` + `plugin/client.js`）也在 QUICKSTART 末尾。

## MCP 配置（桌面 agent 用户）

给 Claude Desktop / Cherry Studio / Kimi / DeepSeek / Cursor 等任意支持 MCP 的桌面 agent 添加 stdio server：

```bash
pip install "mcp>=1.2.0" pymupdf faiss-cpu sentence-transformers numpy   # mcp SDK + 引擎依赖
python mcp-server/server.py                                               # stdio，引擎自动拉起
```

- 各客户端的配置片段（`claude_desktop_config.json`、Cherry Studio 等）与异步/超时行为 → **[mcp-server/README.md](mcp-server/README.md)**。
- 默认库目录 `~/.kb-rag`（`KB_RAG_ROOT` 可改；每个工具也支持 `kb_root` 参数覆盖）。
- MCP 与 DSH 插件共用同一 `kb_engine.py`，可指向同一个库；隔离多套库就设不同 `KB_RAG_ROOT`。

## Benchmarks (measured)

| Item | Result |
|---|---|
| Ingest throughput | 242 PDF/DOCX（1.8GB）→ **85.9s**（约 355ms/篇）|
| Incremental rerun | 同目录重入库 **2.17s**（40× 提速）|
| Search latency | 20k chunks 热查询 **0.4–1.3s**（含精排）|
| Library size | 209 docs / 19,832 chunks / 19,832 vectors，单 SQLite 文件 |

（Windows/CPU 实测，详见 `docs/DESIGN.md` §10。）

## 文档地图 Documentation map

| 文档 | 内容 |
|---|---|
| [docs/DESIGN.md](docs/DESIGN.md) | 设计文档：存储模型、分块策略、检索流水线、引擎协议、性能 |
| [docs/MIGRATION.md](docs/MIGRATION.md) | schema 迁移：`PRAGMA user_version` 门控、v1→v2→v3 变更与升级清单 |
| [docs/OUTPUT-FORMAT.md](docs/OUTPUT-FORMAT.md) | 输出格式与引文规范：页码锚点、引文关联、边界与降级 |
| [QUICKSTART.md](QUICKSTART.md) | 5 分钟快速上手（装依赖 → 建库 → 检索 → 常见坑）|
| [npm-package/README.md](npm-package/README.md) | npm 包内文档 + Troubleshooting 表（不随本 README 同步维护）|
| [mcp-server/README.md](mcp-server/README.md) | MCP server 配置、工具对照、异步/超时说明 |
| [CHANGELOG.md](CHANGELOG.md) | 版本历史 |
| [UNINSTALL.md](UNINSTALL.md) | 卸载：停插件、删索引/`kb.sqlite`，不动 PDF 与 Zotero 库 |
| [SECURITY.md](SECURITY.md) | 执行模型与安全边界（spawn/读写/下载清单）|

## Configuration（环境变量）

| Variable | Default | 适用 | Description |
|---|---|---|---|
| `KB_EMBED_MODEL` | `BAAI/bge-small-zh-v1.5` | 引擎 | 嵌入模型（首次使用自动下载到 HF 缓存）|
| `KB_RERANK_MODEL` | `BAAI/bge-reranker-base` | 引擎 | 精排模型 |
| `HF_ENDPOINT` | 无 | 引擎 | 受限网络设 `https://hf-mirror.com` 走镜像 |
| `KB_AUTO_PIP` | `0` | DSH 插件 | `1` = 启动时自动 pip 安装缺失 Python 依赖（固定 argv；默认仅打印命令）|
| `KB_RAG_ROOT` | DSH：会话工作区 `.kb`；MCP：`~/.kb-rag` | MCP | 知识库目录（各工具可用 `kb_root` 参数覆盖）|
| `KB_RAG_PYTHON` | 当前解释器 `sys.executable` | MCP | 引擎 Python 覆盖（默认用拉起 server.py 的解释器，避免裸 `python` 命中错误环境）|
| `KB_ASYNC_THRESHOLD` | `25` | MCP | `kb_ingest` 待处理文件数超过即自动转后台的阈值 |
| `KB_SQLITE_WAL` | 关 | 引擎 | `1` = 开启 SQLite WAL（需同步 `.kb` 目录时保持默认 DELETE 模式更安全）|

## 仓库布局 Repository Layout

```
kb-rag/
├─ kb_engine.py              # Python 引擎：章节切分/检索/精排 + 常驻 serve daemon
│                            #   schema v3（page_start/page_end），PRAGMA user_version 门控迁移
├─ install.cmd               # Windows 一键入口（双击 → scripts\install.ps1）
├─ scripts/                  # 一键安装脚本（install.ps1 / install.sh，与 npm 包内同源）
├─ plugin/                   # DSH 插件（动态）
│  ├─ kbrag.plugin.json      # 插件清单（9 工具 / engine.commands / models / dataDir=.kb）
│  ├─ host.js                # Host 半：9 工具注册 + 引擎 daemon 拉起 + RPC
│  └─ client.js              # Client 半（来源卡片渲染，可选）
├─ npm-package/              # npm 静态包 dsh-kb-rag（发布内容）
│  ├─ package.json           # 声明 dsh.bundle；bin: dsh-kb-rag-install
│  ├─ cordis.patch.yml       # 激活补丁（dsh plugin add 自动应用）
│  ├─ install.mjs            # npm bin 入口（npx 一键安装器，37 行薄分发）
│  ├─ lib/index.js           # Host 插件（9 工具 + 依赖探测 + KB_AUTO_PIP）
│  ├─ scripts/               # install.ps1 / install.sh / doi_pdf.mjs（kb_fetch 的 Node 下载器）
│  └─ kb_engine.py           # 引擎副本（随包分发，无需手动放置）
├─ mcp-server/               # MCP server（stdio）
│  ├─ server.py              # 9 工具：kb_ingest / kb_status / kb_zotero / kb_search / kb_rag /
│  │                         #   kb_stats / kb_dedup / kb_clear / kb_fetch（kb_scope → kb_status）
│  ├─ engine_client.py       # 引擎 daemon 客户端 + 结果渲染（stdlib only）
│  ├─ requirements.txt       # mcp SDK
│  └─ README.md              # MCP 配置说明
├─ docs/                     # DESIGN.md / MIGRATION.md / OUTPUT-FORMAT.md
├─ QUICKSTART.md · CHANGELOG.md · requirements.txt · SECURITY.md · UNINSTALL.md · LICENSE
└─ tools/                    # 内部运维脚本（不入发布包）
```

**运行时数据**：DSH 插件默认写入会话工作区 `.kb/kb.sqlite`（`docs`/`chunks`/`vecs`/`cache` 表）；MCP 默认 `~/.kb-rag/kb.sqlite`。异步后台任务文件在 `<kb_root>/.kb-jobs/`（与 kb.sqlite 同级），任务完结后由 `kb_status` 自动清理。

## Known Limitations & Roadmap

- 页码锚点仅 PDF 有效：txt/md/docx 与 schema v3 之前入库的旧数据无页码（NULL，自动降级为章节/段落定位），`force` 重入库后恢复
- 元数据年份：PDF 元数据缺失时从正文启发式抓取，可能错抓（Zotero 元数据可覆盖）
- 检索性能：关键词扫描为内存实现；几十万 chunk 以上建议 FAISS HNSW / SQLite FTS5
- Roadmap：中→英 query 翻译（本地 opus-mt）、图注 OCR、引文网络图谱

## License

MIT — see [LICENSE](LICENSE)。
