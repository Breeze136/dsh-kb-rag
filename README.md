# kb-rag — Local literature RAG that lands on the exact passage

[![npm version](https://img.shields.io/npm/v/dsh-kb-rag)](https://www.npmjs.com/package/dsh-kb-rag)
[![npm downloads](https://img.shields.io/npm/dm/dsh-kb-rag)](https://www.npmjs.com/package/dsh-kb-rag)
[![GitHub release](https://img.shields.io/github/v/release/Breeze136/dsh-kb-rag)](https://github.com/Breeze136/dsh-kb-rag/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Awesome DSH Plugin](https://beancookie.github.io/awesome-dsh-plugin/badge.svg)](https://beancookie.github.io/awesome-dsh-plugin)
[![dsh.so security](https://www.dsh.so/badges/kb-rag.svg)](https://www.dsh.so/artifact/kb-rag/)

<p align="center">
  <b>把脑子里的模糊记忆，变成一条能点开的文献坐标。</b><br/>
  <i>Ingest once, search forever — only the few relevant sentences ever reach the model, and every claim carries exact provenance.</i>
</p>

> **别人给你一段像样的答案，kb-rag 给你一个能点开的坐标。** 混合检索（BM25 + 向量）+ 精排定位到具体段落，命中结果带 **PDF 页码**（Zotero `?page=N` 一键跳页）与 **DOI 链接**；循着正文引用还能反查出**这个结论引用了哪篇文献、而它是否就在你自己的库里**。索引、嵌入、重排全部在本地完成——零 API 费用、零上传。

<p align="center">
  <a href="#快速开始"><strong>快速开始</strong></a> ·
  <a href="#三种形态"><strong>三种形态</strong></a> ·
  <a href="#工具参考"><strong>工具参考</strong></a> ·
  <a href="#文档"><strong>文档</strong></a> ·
  <a href="#实测数据"><strong>实测数据</strong></a> ·
  <a href="CHANGELOG.md"><strong>更新日志</strong></a>
</p>

---

## 它看起来是什么样

一次 `kb_rag` 调用返回的证据（MCP 渲染，agent 可见）：

```text
**知识库来源 Top-2**
深度检索（deep） · 精排 BAAI/bge-reranker-base · 缓存命中

1. [Field-driven domain evolution in layered oxide thin films](https://doi.org/10.5555/12345678) — Author A; Author B · 2024 · J. Appl. Phys. · §Results · p.4
> the domains reorient in the plane defined by the easy axis and the applied field ... over a length scale of ~65 nm
↳ 引文补充（本证据的参考文献；⭐=已在库内，可检索引用）
  · [Ref 4] Author C, et al. J. Phys.: Condens. Matter 15, 4835 (1982)
    ⭐ 库内命中：[Long-range ordering in layered oxides]（Author C · 1982 · J. Phys.: Condens. Matter）（即本证据的 Ref 4）· [Zotero 打开](zotero://open-pdf/library/items/EXAMPLEKEY1)
  ↳ 另有 3 条库外引文未展开（Ref 6–8），补库时可按编号定位

**关联文献（可作补充建议）**
- [A Practical Guide to Domain Imaging] — Author G et al. · 2020（同作者·主题相似）
```

> 完整推演（含 agent 回答、追问页码）见 [`docs/OUTPUT-FORMAT.md`](docs/OUTPUT-FORMAT.md)。上例为**中性示例数据**（DOI/作者为占位符）。

---

## 定位 Product Positioning

**"能检索"已经是底线，真正的问题是：检索结果离"原始证据"有多远。** kb-rag 交付的不是一段可疑的摘要，而是**文献坐标**——章节、PDF 物理页码、可点击 DOI，以及这条结论在原文里引用了谁。

它有三条明确的取舍：

- **本地优先，零上传** — 嵌入与重排都是本地 bge 模型，文献内容不出机器；索引就是一个 `kb.sqlite` 文件，可直接备份/搬移。
- **垂直文献，不做通用网盘** — 章节感知分块（摘要 ×1.5 / 方法 ×1.2 权重）、原生 Zotero 迁移、DOI 引文规范；面向"论文开箱即用"，不做通用知识库管理器。
- **诚实边界** — 扫描版 PDF（无文字层）不入库、图注只索引文字不索引图像、跨语言检索偏弱。做不到的写在[已知限制](#已知限制)里，不写成"即将支持"。

> [!IMPORTANT]
> ### 这是工具，不是许愿池
> 检索质量的天花板来自**你的库**：库里没有的文献，它答不出来；库里是扫描版 PDF，它读不出来。模型只负责把拿到的证据组织成答案。
>
> 另外三件要有预期的事：① **首次使用**会下载嵌入模型（~95MB）与精排模型（~1.1GB），首次查询要等十几秒加载，之后常驻亚秒级；② **旧库升级**后页码/角标需要 `force` 重入库才有（schema 迁移不会回填解析结果）；③ 单次入库上千篇请用 `async_mode`（MCP 侧超阈值自动转后台），别让宿主超时掐断。

---

## 三种形态

同一套引擎（`kb_engine.py`）、同一份数据格式，三个入口：

| 形态 | 入口 | 工具差异 |
|---|---|---|
| **DSH 插件**（主形态） | `plugin/` — 会话内对话式使用 | 9 工具，含 `kb_scope`（查询范围/严格模式，DSH 会话概念）|
| **MCP server** | `mcp-server/server.py` — stdio，给 Claude Desktop / Cherry Studio / Kimi / DeepSeek / Cursor 等 | 9 工具，`kb_scope` → `kb_status`（后台任务轮询）|
| **npm 静态包** | `dsh-kb-rag` — 声明 `dsh.bundle`，`dsh plugin add` 一步装+激活 | 同 DSH 插件 |

---

## 快速开始

### 1. 前置

只需要 **Python ≥ 3.9**。Node/pnpm 由安装器检查（缺 pnpm 会自动装）。DSH 用户在 DSH profile 下操作，MCP 用户只需 Python。

<details>
<summary><strong>Windows</strong> — 中文用户名/编码相关的坑已修（v1.6.3）</summary>

Windows PowerShell 5.1 默认把管道编码当 ASCII，中文用户名路径会让引擎冒烟测试报 `WinError 123`。v1.6.3 起安装脚本已在顶部强制 UTF-8 管道编码修复，细节见 [`docs/install-winerror123-fix.md`](docs/install-winerror123-fix.md)。
</details>

<details>
<summary><strong>受限网络</strong> — 模型下载走镜像</summary>

安装器与引擎都支持自动回退 `hf-mirror.com`（`_apply_hf_mirror` 会真正 patch `huggingface_hub` 常量，不只是设环境变量）。手动固定：`HF_ENDPOINT=https://hf-mirror.com`。
</details>

### 2. 安装

**方式 A · 一行命令（推荐）**

```bash
npx dsh-kb-rag-install
```

安装器一条链完成：Python 依赖 → 引擎冒烟测试 → Node/pnpm 检查 → `dsh plugin add` 激活 → 模型预下载（默认开启，`--no-models` 可跳过）。profile 未指定时会自动检测 `~/.dsh/profiles/`（唯一即用；多个会询问；都没有则用 `web`）。

> 兼容旧写法（等价，不依赖微包）：`npx --yes --package dsh-kb-rag -c "dsh-kb-rag-install --profile web"`

**方式 B · DSH 用户直接装插件**

```bash
dsh plugin --profile web add dsh-kb-rag
```

**方式 C · 从源码**

```bash
git clone https://github.com/Breeze136/dsh-kb-rag.git && cd dsh-kb-rag
./npm-package/scripts/install.sh        # macOS / Linux / Git Bash
# Windows：install.cmd（双击）或 npm-package\scripts\install.ps1
```

### 3. 建库

在 DSH 对话里说 **"把 downloads 目录入库"**（`kb_ingest`），或 **"同步 Zotero"**（`kb_zotero`）。也可以先按 DOI 抓取：**"下载 10.5555/12345678"**（`kb_fetch`，仅 OA）。

<details>
<summary>大批量入库（几百篇）——别让宿主超时</summary>

MCP 侧 `kb_ingest` 会自动估算待处理文件数，超过 `KB_ASYNC_THRESHOLD`（默认 25）**自动转后台**：立即返回 `job_id`，用 `kb_status` 轮询直到 `done`。Zotero 整库迁移用 `kb_zotero(async_mode=true)`。任务在独立子进程跑，宿主 60s 超时不会中断它。
</details>

### 4. 提问

- "库里关于磁电耦合的文献有哪些？" → `kb_search`
- "这个结论在哪篇文献的第几页？" → 读证据的 `page` 字段，或点击 Zotero 跳页链接
- "严格只按库内回答" → `kb_scope` 切严格模式（DSH）
- "快速看看" / "深入分析" → `kb_search` 默认 `quick`（亚秒）、`kb_rag` 默认 `deep`（精排+引文关联+相关文献）

> 装完务必**重启 DSH 并开新会话**——工具在会话创建时注入，老会话不会自动获得。分步演练与常见坑见 [QUICKSTART.md](QUICKSTART.md)。

---

## 工具参考

### DSH 插件（9 个）

| Tool | 用途 | 示例说法 |
|---|---|---|
| `kb_ingest` | 文件/文件夹入库：增量跳过 + 去重、章节切分、向量化（PDF/TXT/MD/DOCX）| "把 papers 文件夹入库" |
| `kb_zotero` | 批量迁移本地 Zotero 库（带 PDF 附件的条目）| "同步 Zotero" |
| `kb_search` | 混合检索 Top-N 片段 + 精确来源（标题/作者/年份/期刊/DOI/页码/章节）| "搜 graphene CVD on copper" |
| `kb_rag` | 证据问答：默认 Top-3，逐条编号引用 | "这个体系的外场调控机制是什么？" |
| `kb_scope` | 查询范围（封闭库 / 库+全网 / 仅全网）+ 严格模式 + 检索深度 | "切到严格模式" |
| `kb_dedup` | 清理重复文档（保留最早，可反复调用）| "去重" |
| `kb_clear` | 清空全部文献与索引（须显式 `confirm=true`）| "清空知识库" |
| `kb_stats` | 文档/分块/向量统计 + 最近入库清单 | "看看库里有什么？" |
| `kb_fetch` | 按 DOI/arXiv ID 下载 PDF（OA only，出版商正式版优先）| "下载 10.5555/12345678" |

> MCP 版同为 9 个：`kb_scope` 换成 `kb_status`（后台任务轮询）。配置见 [mcp-server/README.md](mcp-server/README.md)。

### 引擎能力

- **章节感知分块** — 摘要 ×1.5、方法 ×1.2 权重；行内标题检测、摘要自动提升、图注块；非论文按段落兜底
- **混合检索** — 关键词 BM25（CJK 双字友好）+ bge-small 向量余弦，RRF 融合 × 章节权重
- **精排** — bge-reranker-base Cross-Encoder，Top-20 → Top-3（缺失时回退 bge-large-en bi-encoder）
- **页码锚点**（schema v3）— 检索结果带 PDF 物理页码，渲染 `§章节 · p.N`，可 Zotero 一键跳页
- **引文关联** — 正文 `[n]` → References 条目；Nature 系上标角标按**字体度量**识别（`graphene1,2` → `graphene[1,2]`）；被引文献**库内匹配**（DOI / 标题 / 作者+年份）标注 ⭐
- **快速/深度双模式** — `quick` 混合召回直出（跳过精排/引文/关联），`deep` 全链路
- **增量与去重** — sha256 增量跳过（重跑 40× 提速）、跨路径重复拦截
- **查询缓存** — 同 query+filters 不重算；任何入库自动失效
- **引擎 daemon** — 模型只加载一次、崩溃自愈、插件停止自动回收

---

## 架构

```text
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

---

## 实测数据

| 项目 | 结果 |
|---|---|
| 入库吞吐 | 242 篇 PDF/DOCX（1.8GB）→ **85.9s**（约 355ms/篇）|
| 增量重跑 | 同目录重入库 **2.17s**（40× 提速）|
| 检索延迟 | 20k chunks 热查询 **0.4–1.3s**（含精排）；`quick` 模式 **~16ms** |
| 库规模 | 209 篇 / 19,832 块 / 19,832 向量，单 SQLite 文件 |
| 引文解析 | 11 篇各出版商 PDF：Wiley 综述 0→399 条、Nature Letter 8→37、Science 0→29（只增不减）|

---

## 文档

| | 文档 | 内容 |
|---|---|---|
| 🚀 | [QUICKSTART.md](QUICKSTART.md) | 5 分钟上手：装依赖 → 建库 → 检索 → 常见坑 |
| 🏗️ | [docs/DESIGN.md](docs/DESIGN.md) | 设计文档：存储模型、分块策略、检索流水线、引擎协议 |
| 📐 | [docs/OUTPUT-FORMAT.md](docs/OUTPUT-FORMAT.md) | 输出格式与引文规范：页码锚点、引文关联、快速/深度模式 |
| 🗄️ | [docs/MIGRATION.md](docs/MIGRATION.md) | schema 迁移：`PRAGMA user_version` 门控、v1→v2→v3 升级 |
| 🔌 | [mcp-server/README.md](mcp-server/README.md) | MCP 配置、工具对照、异步/超时行为 |
| 📦 | [npm-package/README.md](npm-package/README.md) | npm 包文档 + Troubleshooting 表 |
| 🔐 | [SECURITY.md](SECURITY.md) | 执行模型与安全边界（spawn/读写/下载清单）|
| 🧹 | [UNINSTALL.md](UNINSTALL.md) | 卸载：停插件、删索引，不动你的 PDF 与 Zotero 库 |
| 📝 | [CHANGELOG.md](CHANGELOG.md) | 版本历史 |

## 配置（环境变量）

| 变量 | 默认 | 适用 | 说明 |
|---|---|---|---|
| `KB_EMBED_MODEL` | `BAAI/bge-small-zh-v1.5` | 引擎 | 嵌入模型（首次使用自动下载到 HF 缓存）|
| `KB_RERANK_MODEL` | `BAAI/bge-reranker-base` | 引擎 | 精排模型 |
| `HF_ENDPOINT` | 无 | 引擎 | 受限网络设 `https://hf-mirror.com` 走镜像 |
| `KB_AUTO_PIP` | `0` | DSH 插件 | `1` = 启动时自动 pip 安装缺失依赖（默认仅打印命令）|
| `KB_RAG_ROOT` | DSH：会话工作区 `.kb`；MCP：`~/.kb-rag` | MCP | 知识库目录（各工具可用 `kb_root` 覆盖）|
| `KB_RAG_PYTHON` | 当前解释器 | MCP | 引擎 Python 覆盖（避免裸 `python` 命中错误环境）|
| `KB_ASYNC_THRESHOLD` | `25` | MCP | `kb_ingest` 待处理文件数超过即自动转后台 |
| `KB_SQLITE_WAL` | 关 | 引擎 | `1` = 开 WAL（同步 `.kb` 目录时保持默认更安全）|

## 仓库布局

```text
kb-rag/
├─ kb_engine.py              # Python 引擎：章节切分/检索/精排 + 常驻 serve daemon
├─ install.cmd               # Windows 一键入口（双击 → scripts\install.ps1）
├─ scripts/                  # 一键安装脚本（install.ps1 / install.sh）
├─ plugin/                   # DSH 动态插件（kbrag.plugin.json + host.js + client.js）
├─ npm-package/              # npm 静态包 dsh-kb-rag（发布内容；含 cordis.patch.yml）
├─ dsh-kb-rag-install/       # 微包：裸 `npx dsh-kb-rag-install` 入口（零逻辑转发）
├─ mcp-server/               # MCP server（server.py + engine_client.py）
├─ docs/                     # DESIGN / OUTPUT-FORMAT / MIGRATION / install-winerror123-fix
├─ tools/                    # 内部运维脚本（不入发布包）
└─ QUICKSTART.md · CHANGELOG.md · SECURITY.md · UNINSTALL.md · LICENSE
```

**运行时数据**：DSH 插件写会话工作区 `.kb/kb.sqlite`；MCP 默认 `~/.kb-rag/kb.sqlite`。后台任务文件在 `<kb_root>/.kb-jobs/`，完结后自动清理。

## 已知限制

- **扫描版 PDF 不支持** — 无文字层即跳过（不做 OCR，属设计取舍）
- **页码仅 PDF 有效** — txt/md/docx 与 v3 之前入库的旧数据无页码（NULL，自动降级为章节定位），`force` 重入库后恢复
- **引文关联需重灌** — 上标角标与新版 References 切分在入库时处理，旧库需 `force` 重入库
- **元数据可能错抓** — PDF 元数据缺失时从正文启发式抓取；Zotero 元数据可覆盖
- **跨语言检索偏弱** — 中文 query 对英文正文主要靠向量兜底（roadmap：本地中→英翻译）
- **图注只是文字** — 能搜到图注写到的词，搜不到图里的内容
- **检索规模** — 关键词扫描为内存实现；几十万 chunk 以上建议 FAISS HNSW / SQLite FTS5

## 联系与协作

- 🐛 **Bug / 功能建议** — [GitHub Issues](https://github.com/Breeze136/dsh-kb-rag/issues)
- 💬 **讨论** — [GitHub Discussions](https://github.com/Breeze136/dsh-kb-rag/discussions)
- 🔒 **安全问题** — 见 [SECURITY.md](SECURITY.md)

## 相关工具

- [awesome-dsh-plugin](https://github.com/awesome-dsh-plugin/awesome-dsh-plugin) — DSH 插件收录列表
- [dsh-plugin-registry](https://github.com/beancookie/dsh-plugin-registry) — 设置面板里的插件市场

## License

[MIT](LICENSE) — 本地优先，代码可读可改；用到的模型（`BAAI/bge-*`）遵循各自许可。

---

<p align="center">
  <sub>由 <a href="https://github.com/Breeze136">Breeze136</a> 维护 · 如果它帮你找到过一段忘在角落的证据，欢迎点个 ⭐</sub><br/>
  <sub>Distribution: <a href="https://github.com/Breeze136/dsh-kb-rag">GitHub</a>（主） · <a href="https://www.npmjs.com/package/dsh-kb-rag">npm</a> · MIT licensed</sub>
</p>

[⬆ 回到顶部](#kb-rag--local-literature-rag-that-lands-on-the-exact-passage)
