# kb-rag — 本地文献 RAG，提供段落级溯源

[![npm version](https://img.shields.io/npm/v/dsh-kb-rag)](https://www.npmjs.com/package/dsh-kb-rag)
[![npm downloads](https://img.shields.io/npm/dm/dsh-kb-rag)](https://www.npmjs.com/package/dsh-kb-rag)
[![GitHub release](https://img.shields.io/github/v/release/Breeze136/dsh-kb-rag)](https://github.com/Breeze136/dsh-kb-rag/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Awesome DSH Plugin](https://beancookie.github.io/awesome-dsh-plugin/badge.svg)](https://beancookie.github.io/awesome-dsh-plugin)
[![dsh.so security](https://www.dsh.so/badges/kb-rag.svg)](https://www.dsh.so/artifact/kb-rag/)

[English](./README.md) | **中文**

kb-rag 是面向 DSH（DeepSeek Harness）以及任何支持 MCP 的 agent 的本地文献知识库。它把 PDF 与 Zotero 文库索引到同一个 SQLite 文件中，回答问题时给出的是原文段落而非改写：每条结果都携带所属章节、PDF 物理页码与可点击的 DOI，并且检索到的段落中每一处文内引用都能回溯到被引文献，同时标明该文献是否已在你的文库中。

索引、向量化与重排全部在本地完成，没有 API 费用，也不会上传任何数据。

<p align="center">
  <a href="#快速开始"><strong>快速开始</strong></a> ·
  <a href="#三种部署形态"><strong>部署形态</strong></a> ·
  <a href="#工具参考"><strong>工具参考</strong></a> ·
  <a href="#文档"><strong>文档</strong></a> ·
  <a href="#实测性能"><strong>实测性能</strong></a>
</p>

## 输出示例

一次 `kb_rag` 调用返回的证据如下。下列内容为工具的实际渲染输出（界面文案即中文），`[库内]` 是它打印的「该文献已在库内」标记：

```text
**知识库来源 Top-2**
深度检索（deep） · 精排 BAAI/bge-reranker-base · 缓存命中

1. [Chemical vapour deposition of graphene on copper substrates](https://doi.org/10.5555/12345678) — Author A; Author B · 2024 · Carbon · §Results · p.4
> graphene domains nucleate on the copper surface and coalesce into a continuous film ... at a growth rate of ~2 um/min
↳ 引文补充（本证据的参考文献；[库内]=已在库内，可检索引用）
  · [Ref 4] Author C, et al. Carbon 48, 1234 (2010)
    [库内] [Nucleation and growth of graphene on transition metals](https://doi.org/10.5555/12345684)（Author C · 2010 · Carbon）（即本证据的 Ref 4，可检索引用）· [Zotero 打开](zotero://open-pdf/library/items/EXAMPLEKEY1)
  ↳ 另有 3 条库外引文未展开（Ref 6–8），补库时可按编号定位

**关联文献（可作补充建议）**
- [A Practical Guide to Raman Spectroscopy of Graphene] — Author G et al. · 2020（同作者 · 主题相似）
```

完整流程示例（包括 agent 的回答，以及用于确定页码的追问）见 [`docs/OUTPUT-FORMAT.md`](docs/OUTPUT-FORMAT.md)。上例使用中性占位数据：作者、期刊与 DOI 均为虚构。

## 定位

检索本身已是基本要求，真正的问题在于结果与原始证据之间的距离。kb-rag 返回的是**位置**而非摘要：章节、PDF 物理页码、可点击的 DOI，以及该段落背后的引用链。

三组有意为之的取舍界定了本项目：

- **本地优先，零上传。** 向量化与重排均运行在本地 bge 模型上。整个索引就是一个 `kb.sqlite` 文件，可随时复制或归档。
- **垂直领域，而非通用工具。** 面向章节的切分（摘要与方法部分加权）、原生 Zotero 迁移、DOI 引用规范。它为论文而构建，不用于任意文档管理。
- **明示边界。** 没有文本层的扫描版 PDF 会被跳过，图注按文本而非图像索引，跨语言检索效果较弱。这些内容记录在[已知限制](#已知限制)中，而不是承诺为“即将支持”。

> [!IMPORTANT]
> **适用范围与预期。** 检索质量受限于文库本身：工具无法依据未收录的文档作答，也无法读取没有文本层的扫描页。有三项行为值得提前了解：
>
> - **首次使用较慢。** 向量化模型（约 95 MB）与重排模型（约 1.1 GB）在首次使用时下载，首次查询需等待约十秒完成加载。此后常驻守护进程将模型保留在内存中，后续查询均在亚秒级完成。
> - **锚点属于入库时数据。** 页码锚点与上标引用标记在文档解析阶段生成。在 v1.6 之前索引的文库仍可正常使用，但在对文档使用 `force` 重新入库之前，这些字段保持为空。
> - **批量入库为异步执行。** 待处理文件数超过 `KB_ASYNC_THRESHOLD`（默认 25）时，`kb_ingest` 会自动 fork 为后台任务并立即返回 `job_id`，不再占住本次会话——两种部署形态行为一致，因此宿主端的调用超时不会中断该工作。用 `kb_status` 轮询至状态为 `done`。

## 三种部署形态

一个引擎（`kb_engine.py`）、一种数据格式、三个入口：

| 形态 | 入口 | 工具集 |
|---|---|---|
| **DSH 插件**（主要形态） | `plugin/` — 在 DSH 会话内以对话方式使用 | 10 个工具，新增 `kb_scope`（查询范围与严格模式，属 DSH 会话概念）与 `kb_status`（后台任务轮询） |
| **MCP 服务器** | `mcp-server/server.py` — stdio，适用于 Claude Desktop、Cherry Studio、Kimi、DeepSeek、Cursor 等 | 9 个工具；`kb_status` 两侧都有，因此只有 `kb_scope` 是 DSH 独有 |
| **npm 包** | `dsh-kb-rag` — 声明 `dsh.bundle`，因此 `dsh plugin add` 可一步完成安装与启用 | 与 DSH 插件相同 |

## 快速开始

### 1. 环境要求

Python 3.9 或更高版本。安装脚本会检查 Node 与 pnpm，并在缺少 pnpm 时自动安装。DSH 用户在 DSH profile 中操作；MCP 用户只需 Python。

<details>
<summary><strong>Windows</strong> — 自 v1.6.3 起支持非 ASCII 用户名</summary>

Windows PowerShell 5.1 默认将管道编码设为 ASCII，这会把临时路径中的非 ASCII 用户名替换为 `?`，并导致引擎冒烟测试以 `WinError 123` 失败。自 v1.6.3 起，安装脚本在开头强制使用 UTF-8 管道编码。详见 [`docs/install-winerror123-fix.md`](docs/install-winerror123-fix.md)。
</details>

<details>
<summary><strong>受限网络</strong> — 模型下载回退至镜像</summary>

当直连下载失败时，安装脚本与引擎都会通过 `hf-mirror.com` 重试（`_apply_hf_mirror` 会改写 `huggingface_hub` 的常量，因为在 import 之后设置环境变量不会生效）。如需手动指定：`HF_ENDPOINT=https://hf-mirror.com`。
</details>

### 2. 安装

**方式 A — 一条命令（推荐）**

```bash
npx dsh-kb-rag-install
```

安装脚本依次执行完整流程：Python 依赖、引擎冒烟测试、Node/pnpm 检查、`dsh plugin add` 启用，以及模型预下载（默认开启，传入 `--no-models` 可跳过）。若未指定 profile，脚本会检查 `~/.dsh/profiles/`：只有一个 profile 时直接使用，存在多个时提供选择，一个都没有时回退到 `web`。

> 不使用微型包的等效命令：`npx --yes --package dsh-kb-rag -c "dsh-kb-rag-install --profile web"`

**方式 B — DSH 用户，直接安装插件**

```bash
dsh plugin --profile web add dsh-kb-rag
```

**方式 C — 从源码安装**

```bash
git clone https://github.com/Breeze136/dsh-kb-rag.git && cd dsh-kb-rag
./npm-package/scripts/install.sh        # macOS / Linux / Git Bash
# Windows：install.cmd，或 npm-package\scripts\install.ps1
```

### 3. 构建文库

在 DSH 会话中，让它入库某个文件夹（`kb_ingest`）或同步 Zotero（`kb_zotero`）。单篇论文也可先按标识符获取（`kb_fetch` 优先解析出版商正式版，在校园网或机构订阅网络下可直接取得订阅版；无权限时回退开放获取）。

<details>
<summary>批量入库 — 避免宿主超时干扰任务</summary>

待处理文件数超过 `KB_ASYNC_THRESHOLD`（默认 25）时，`kb_ingest` 在两种部署形态下都会切换为后台任务；统计在调用到达时完成（DSH 插件由引擎自己数，MCP 服务器在宿主侧数）。该调用立即返回 `job_id`；通过 `kb_status` 轮询至状态为 `done`。整库 Zotero 迁移使用 `kb_zotero(async_mode=true)`。任务在独立子进程中运行，因此 60 秒的客户端超时不会将其中断。
</details>

### 4. 提问

- “文库中哪些论文讨论了铜上石墨烯的生长？” — `kb_search`
- “这是哪篇论文的哪一页？” — 读取证据上的 `page` 字段，或跟随 Zotero 页码链接
- “只依据文库作答” — 使用 `kb_scope` 开启严格模式（DSH）
- “快速查看”与“深入分析” — `kb_search` 默认 `quick`（亚秒级），`kb_rag` 默认 `deep`（重排、引用关联、相关文献）

安装后请重启 DSH 并新建会话：工具在会话创建时注入，已有会话不会获得这些工具。分步说明与常见问题见 [QUICKSTART.md](QUICKSTART.md)。

### 5. 升级

DSH profile 是一个 **pnpm workspace**（其中包含 `pnpm-lock.yaml`，且 `dsh plugin` 本身会转发给 pnpm），因此升级使用与安装插件相同的命令：

```bash
dsh plugin --profile web add dsh-kb-rag          # 最新版本
dsh plugin --profile web add dsh-kb-rag@1.6.6    # 或固定到指定版本
```

重新运行安装脚本效果相同，并会额外校正 Python 依赖：

```bash
npx dsh-kb-rag-install --profile web
```

> [!WARNING]
> **不应在 DSH profile 目录中执行 `npm install dsh-kb-rag`。** 它会在 pnpm 的符号链接存储旁写入 npm 风格的 `node_modules`，此后两种目录结构不再一致，后续 `dsh plugin` 操作将变得不可预测。`npm install` 仅适用于完全由你手工管理的部署（见 [npm-package/README.md](npm-package/README.md) 方式 3）。
>
> 升级后请重启 DSH 并新建会话。已有的 `.kb` 文库会自动迁移（见 [docs/MIGRATION.md](docs/MIGRATION.md)），但**页码锚点与上标引用标记需要对更早入库的文档使用 `force` 重新入库**——迁移只新增列，不会重新解析文档。当库内部分文档由更早版本的解析器写入时，`kb_stats` 会报告 `stale_docs`，插件会在该会话首次检索时询问一次处理方式：暂不处理、仅刷新元数据，或重新入库。

## 工具参考

### DSH 插件（10 个工具）

| 工具 | 用途 | 示例请求 |
|---|---|---|
| `kb_ingest` | 入库文件或文件夹：增量跳过、去重、按章节切分、向量化（PDF/TXT/MD/DOCX）。`metadata_only=true` 原地刷新标题/作者/年份/期刊/DOI，`rebuild=true` 重新解析库内全部文档；大批量自动转后台任务 | “入库论文文件夹” |
| `kb_status` | 按 `job_id` 轮询后台入库任务：`running` 返回进度（已处理 / 错误 / 分块数），`done` 返回该任务的 totals 与最近文件，另有 `error` 与 `not_found` | “入库进行到哪一步了？” |
| `kb_zotero` | 迁移本地 Zotero 文库，含 PDF 附件 | “同步 Zotero” |
| `kb_search` | 混合检索，返回带精确溯源（标题、作者、年份、期刊、DOI、页码、章节）的原文段落 | “检索铜上化学气相沉积石墨烯” |
| `kb_rag` | 证据式问答，默认返回前 3 条，带编号引用 | “该体系中的磁畴演化由什么主导？” |
| `kb_scope` | 查询范围（仅文库／文库加网络／仅网络）、严格模式、检索深度 | “切换到严格模式” |
| `kb_dedup` | 删除重复文档，保留最早的一份 | “去重” |
| `kb_clear` | 清空全部文档与索引；需要 `confirm=true` | “清空知识库” |
| `kb_stats` | 文档、分块与向量数量、最近的入库记录，以及 `stale_docs`（由更早版本解析器写入的文档数） | “文库中有什么？” |
| `kb_fetch` | 按 DOI 或 arXiv ID 下载 PDF（优先出版商正式版，校园网/机构订阅可直接下订阅版；无权限回退开放获取） | “下载 10.5555/12345678” |

MCP 服务器通过 9 个工具暴露同一个引擎，其中也提供 `kb_status`（后台任务轮询），因此只有 `kb_scope` 是 DSH 独有。配置与客户端配置片段见 [mcp-server/README.md](mcp-server/README.md)。

### 引擎能力

- **章节感知切分。** 摘要权重 ×1.5、方法 ×1.2，支持行内标题识别、摘要提升、图注块；对非论文类文档执行段落合并。
- **混合检索。** BM25 关键词匹配（支持中日韩二元组），bge-small 向量余弦相似度，RRF 融合，章节加权。
- **重排。** bge-reranker-base 交叉编码器，从 top 20 收敛到 top 3；交叉编码器不可用时自动回退到 bge-large-en 双编码器。
- **页码锚点**（schema v3）。结果携带 PDF 物理页码，渲染为 `section · p.N`，可对应 Zotero 的 `?page=N` 深度链接。
- **引用关联。** 文内 `[n]` 标记解析到参考文献条目；Nature 风格的上标依据字体度量识别（`graphene1,2` 转为 `graphene[1,2]`）；被引文献按 DOI、规范化标题，或第一作者加年份与文库匹配，命中项在渲染结果中标记为 `[库内]`。
- **快速与深度模式。** `quick` 直接返回混合检索结果（不进行重排、引用关联与相关文献检索），`deep` 运行完整链路。
- **增量索引与去重。** SHA-256 内容哈希跳过未变更的文件（重复运行时约快 40 倍），并拦截跨路径的重复文档。
- **元数据刷新与陈旧数据检测**（schema v4）。每条文档记录写入它时的解析器版本（`docs.indexed_with`），因此 `kb_stats` 能报告 `stale_docs`——由更早版本解析器写入、增量入库原本永远不会再碰的行。`kb_ingest(metadata_only=true)` 只重新抽取标题、作者、年份、期刊与 DOI，不重切块、不重嵌入（实测约 90 ms/篇）；`kb_ingest(rebuild=true)` 按库内路径原地重新解析全部文档。
- **查询缓存。** 相同的查询与过滤条件不会重复计算；任何入库操作都会使其失效。
- **常驻守护进程。** 模型只加载一次，进程崩溃后可自动恢复，插件停止时被回收。

## 检索建议

查询会按原样送入引擎：它不翻译、不扩写、不改写查询，因此检索效果取决于查询语言与库内正文语言是否匹配。典型文库以英文为主（实测正文约 98% 为英文），由此有三条实用规则：

- **查询写成英文术语串。** BM25 按词元匹配，中文查询会让混合检索的关键词路基本空转——中文二元组无法匹配英文正文——命中只能靠向量侧的跨语言匹配。同一个问题，英文术语串的命中明显优于中文问句。
- **用 3–12 个词的组合** `材料/体系 + 方法/工艺 + 性质/表征`，不要写整句提问：用 `graphene CVD copper single crystal nucleation suppression`，而不是“铜上化学气相沉积石墨烯时如何抑制成核”。
- **限定条件放 `filters`，不要写进查询。** 年份、期刊、作者、章节与文件类型都属于元数据过滤；留在查询文本里只会把关键词浪费在正文并不包含的词上。
- **只有确实需要中文文献时，才用原话另发一条中文查询**（例如库内还存放了中文综述）。

当查询含中日韩字符而库内几乎全为英文时，引擎会在响应中附加 `lang_note` 说明这一点，插件会把它与结果一并渲染。

## 架构

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

## 实测性能

| 指标 | 结果 |
|---|---|
| 入库吞吐 | 242 个 PDF/DOCX 文件（1.8 GB）用时 **85.9 s**，约每篇文档 355 ms |
| 增量重跑 | 同一目录重新入库用时 **2.17 s**，约 40 倍加速 |
| 查询延迟 | 2 万 chunk 规模下暖态含重排为 0.4–1.3 s；`quick` 模式为 **~16 ms** |
| 文库规模 | 单个 SQLite 文件中的 209 篇文档、19,832 个 chunk、19,832 个向量 |
| 引用解析 | 11 份出版商 PDF：一份 Wiley 综述从 0 到 399 条，一封 Nature 快报从 8 到 37 条，一篇 Science 论文从 0 到 29 条——严格递增 |

以上数据在 Windows 上使用 CPU 推理测得。测量方法与设计依据见 [`docs/DESIGN.md`](docs/DESIGN.md)。

## 文档

| 文档 | 内容 |
|---|---|
| [QUICKSTART.md](QUICKSTART.md) | 五分钟上手：依赖、索引、检索、常见问题 |
| [docs/DESIGN.md](docs/DESIGN.md) | 设计说明：存储模型、切分策略、检索流水线、引擎协议 |
| [docs/OUTPUT-FORMAT.md](docs/OUTPUT-FORMAT.md) | 输出与引用规范：页码锚点、引用关联、快速与深度模式 |
| [docs/MIGRATION.md](docs/MIGRATION.md) | Schema 迁移：`PRAGMA user_version` 门控，v1 到 v4 |
| [docs/BACKLOG.md](docs/BACKLOG.md) | 已知缺口：未修复问题、待验证项、回归验证方法与记录约定 |
| [mcp-server/README.md](mcp-server/README.md) | MCP 配置、工具映射、异步行为与超时 |
| [npm-package/README.md](npm-package/README.md) | npm 包文档与故障排查表 |
| [SECURITY.md](SECURITY.md) | 执行模型与安全边界：会启动、读取、写入、下载什么 |
| [UNINSTALL.md](UNINSTALL.md) | 卸载：停止插件并删除索引，不影响 PDF 与 Zotero |
| [CHANGELOG.md](CHANGELOG.md) | 版本历史 |

## 配置

| 变量 | 默认值 | 适用对象 | 说明 |
|---|---|---|---|
| `KB_EMBED_MODEL` | `BAAI/bge-small-zh-v1.5` | 引擎 | 向量化模型；首次使用时下载到 Hugging Face 缓存 |
| `KB_RERANK_MODEL` | `BAAI/bge-reranker-base` | 引擎 | 重排模型 |
| `HF_ENDPOINT` | 无 | 引擎 | 在受限网络中设为 `https://hf-mirror.com` |
| `KB_AUTO_PIP` | `0` | npm 包 | 设为 `1` 时在启动阶段安装缺失的 Python 依赖（固定 argv；默认仅打印命令）。动态插件宿主只报告，不安装 |
| `KB_RAG_ROOT` | DSH：会话工作区 `.kb`；MCP：`~/.kb-rag` | MCP | 知识库目录；可用 `kb_root` 按次调用覆盖 |
| `KB_RAG_PYTHON` | 当前解释器 | MCP | 引擎所用的解释器，以避免裸 `python` 解析到其他环境 |
| `KB_ASYNC_THRESHOLD` | `25` | 引擎 | 待处理文件数超过该值时，`kb_ingest` fork 为后台任务并返回 `job_id`（用 `kb_status` 轮询） |
| `KB_SQLITE_WAL` | 关闭 | 引擎 | 设为 `1` 启用 SQLite WAL；当 `.kb` 目录会被同步时，默认值更安全 |
| `UNPAYWALL_EMAIL` | 内置占位值 | 引擎 | `kb_fetch` 查询 Unpaywall 时使用的联系邮箱；建议设置为你自己的地址 |

## 仓库结构

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

运行时数据：DSH 插件写入会话工作区中的 `.kb/kb.sqlite`；MCP 服务器默认使用 `~/.kb-rag/kb.sqlite`。后台任务文件位于 `<kb_root>/.kb-jobs/`，任务结束后即被删除。

## 已知限制

- **不支持扫描版 PDF。** 没有文本层的文档会被跳过；OCR 有意不在范围内。
- **页码锚点仅适用于 PDF。** TXT、MD 与 DOCX 文件，以及在 schema v3 之前入库的文档都没有页码，在使用 `force` 重新入库前只能定位到章节级别。
- **引用关联需要重新入库。** 上标识别与当前的参考文献切分在解析阶段执行；较早建立的文库需要 `force` 才能获得这些能力。
- **元数据可能误读。** 当 PDF 元数据缺失时，标题与年份由页面文本推断；Zotero 元数据会覆盖该结果。
- **跨语言检索较弱。** 以中文查询检索英文全文主要依赖向量路径；本地查询翻译在规划中。替代做法见[检索建议](#检索建议)。
- **图注仅以文本形式存在。** 图注可作为文本检索，但仅出现在图像内部的内容无法检索。
- **规模。** 关键词匹配为内存实现。超过数十万 chunk 后，FAISS HNSW 或 SQLite FTS5 是更合适的下一步。

## 联系

- 缺陷报告与功能请求：[GitHub Issues](https://github.com/Breeze136/dsh-kb-rag/issues)
- 疑问与讨论：[GitHub Discussions](https://github.com/Breeze136/dsh-kb-rag/discussions)
- 安全问题报告：见 [SECURITY.md](SECURITY.md)

## 相关项目

- [awesome-dsh-plugin](https://github.com/awesome-dsh-plugin/awesome-dsh-plugin) — DSH 插件精选列表
- [dsh-plugin-registry](https://github.com/beancookie/dsh-plugin-registry) — DSH 设置中的插件市场面板

## 许可

[MIT](LICENSE)。随包模型（`BAAI/bge-*`）在运行时下载，遵循其各自的许可协议。
