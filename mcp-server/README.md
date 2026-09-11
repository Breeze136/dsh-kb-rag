# kb-rag MCP server

把 kb-rag 的本地文献知识库 RAG 通过 **MCP（Model Context Protocol，stdio）** 暴露给任意支持 MCP 的桌面 agent——Claude Desktop、Cherry Studio、Kimi、DeepSeek、Cursor、Zcode、Open WebUI 等。

与 DSH 插件共用同一个 `kb_engine.py` 引擎（常驻 serve daemon + JSON-lines 逐行协议；异步 job、schema v3 页码锚点、quick/deep 深度模式、引文关联等能力也都在引擎内实现）；MCP 侧只提供 `server.py` + `engine_client.py` 这一层 stdio 封装，模型也只下载/加载一份。

**9 个工具**：`kb_ingest`（支持 async 后台）/ `kb_status`（后台任务轮询）/ `kb_zotero`（支持 `async_mode=true`）/ `kb_search`（默认 `depth=quick`）/ `kb_rag`（默认 `depth=deep`）/ `kb_stats` / `kb_dedup` / `kb_clear` / `kb_fetch`。

## 与 DSH 插件的工具对照

| DSH 插件 | MCP server | 说明 |
|---|---|---|
| `kb_scope` | — | 查询范围/严格模式/会话级检索深度是 **DSH 会话概念**，MCP 版没有；范围与严格性由调用方（agent）按检索来源自行把握，深度由每次调用的 `depth` 参数显式传入 |
| `kb_status` | `kb_status` | 两侧同名同义：轮询后台任务（`running` 返回进度，`done` 返回 totals 与最近文件），配合 `kb_ingest` 的自动转后台与 `kb_zotero(async_mode=true)` |
| 其余 8 个 | 同左 | 同一引擎、同一行为（`kb_ingest` / `kb_zotero` / `kb_search` / `kb_rag` / `kb_stats` / `kb_dedup` / `kb_clear` / `kb_fetch`）|

DSH 插件共 10 个工具（上表 8 个 + `kb_scope` + `kb_status`），MCP 版共 9 个：`kb_status` 两侧都有，只有 `kb_scope` 是 DSH 独有。

## 安装依赖

```bash
pip install -r requirements.txt      # mcp SDK（mcp>=1.2.0）
pip install pymupdf faiss-cpu sentence-transformers numpy   # 引擎依赖（与 DSH 插件相同）
```

模型（`BAAI/bge-small-zh-v1.5` + `bge-reranker-base`）首次使用自动下载到本地 HF 缓存；受限网络先设 `HF_ENDPOINT=https://hf-mirror.com`。

## 配置（stdio MCP server）

各客户端都支持"添加 MCP server"，命令统一为（换成实际绝对路径）：

```bash
python "<本仓库路径>/mcp-server/server.py"
```

服务默认用**当前解释器**（`sys.executable`）拉起引擎——用哪个 python 起服务，引擎就用哪个，确保命中装有引擎依赖的解释器（`KB_RAG_PYTHON` 可显式覆盖）。

### Claude Desktop

编辑 `claude_desktop_config.json`：

```json
{
  "mcpServers": {
    "kb-rag": {
      "command": "python",
      "args": ["C:\\path\\to\\kb-rag\\mcp-server\\server.py"]
    }
  }
}
```

### Cherry Studio

设置 → MCP 服务器 → 添加 → 类型选 `stdio`，命令 `python`，参数 `C:\path\to\kb-rag\mcp-server\server.py`。

### 其它（Kimi / DeepSeek / Cursor / Zcode / Open WebUI）

在各自的 MCP 配置里加同样的 stdio server（`command: python`，`args: [server.py 绝对路径]`）。

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `KB_RAG_ROOT` | `~/.kb-rag` | 知识库默认目录（各工具可用 `kb_root` 参数覆盖）|
| `KB_RAG_PYTHON` | 当前解释器 `sys.executable` | 引擎所用 Python 解释器（默认用拉起本服务的解释器；显式设置可指向装有依赖的其它环境）|
| `KB_ASYNC_THRESHOLD` | `25` | `kb_ingest` 待处理文件数超过即自动转后台的阈值 |
| `HF_ENDPOINT` | 无 | 模型镜像（如 `https://hf-mirror.com`）；直连下载失败时引擎自动切 `https://hf-mirror.com` 重试 |
| `UNPAYWALL_EMAIL` | 内置占位值 | `kb_fetch` 走到 Unpaywall 开放获取兜底时使用的联系邮箱 |

## 异步与超时

- **`kb_ingest` 自动转后台**：待处理文件数（目录递归或显式路径）超过 `KB_ASYNC_THRESHOLD`（默认 25）时，本服务自动改用后台执行并**立即返回 `job_id`**（`status=running`）——计数在本服务侧完成（只按扩展名统计，不读内容、不做引擎往返），agent 无需知道 async_mode 的存在，直接传整个文献库文件夹也不会超时；文件少则同步执行、直接返回结果。`async_mode=true` 强制后台，`false` 强制同步。（DSH 插件侧的同名行为由引擎自己计数，见仓库 `README.md`。）
- **`kb_zotero(async_mode=true)`**：整库迁移在后台执行（数百篇也不怕超时）；`dry_run` 与 `async_mode` 不要同时用。
- **`kb_status(job_id=...)`**：`running` 时返回已处理进度；`done` 时返回入库 totals 与最近文件；任务完结后引擎自动清理 `.kb-jobs/` 中间文件（result 保留可重复读）；`kb_clear` 也会一并清空 `.kb-jobs/`。`job_id` 有严格格式校验（12 位十六进制）。
- **宿主超时不影响后台任务**：任务跑在独立子进程里、独立于 MCP 请求；Kimi Work 等宿主的 60s 超时只掐断"等待"这一次调用，任务照常在后台跑完，之后用 `kb_status` 取结果，数据不会丢。
- **分批不再是唯一手段**：`kb_zotero` 同步模式仍可用 `limit=N` 分批（小批量、想直接拿结果的场景；`kb_ingest` 无 `limit`，要控制批量就传更小的目录/文件列表）；大批量首选 async。

## 入库模式（`metadata_only` / `rebuild` / 陈旧数据）

- **`metadata_only=true`**：只重新抽取标题/作者/年份/期刊/DOI 并写回 `docs` 的元数据字段，**不重切块、不重嵌入**（无需模型，实测约 90 ms/篇，312 篇约 30 s）。存在的理由：增量入库按 sha256 跳过未变更文件，所以引擎改进元数据抽取后老库不会自愈——这是那条秒级、可反复执行的刷新通道。文件内容已变的条目不动（标 `changed`），因为元数据必须与已入库的正文一致。
- **`rebuild=true`**：按**库内自己记录的路径**原地重新解析全部已入库文档（`paths` 可省略，引擎直接取库内现有路径）。这是安全的全量重灌方式：传目录会因 `force` 跳过 duplicate 判定而把内容重复的文件重复入库。`paths` 省略时不触发自动转后台判定，会同步跑完整个库。
- **陈旧数据**：每条文档记录入库时所用的解析器版本（`docs.indexed_with`，schema v4）；`kb_stats` 返回 `stale_docs` / `stale_sample` / `parser_rev` / `indexed_with`。`stale_docs > 0` 表示库内仍有旧解析器写入的文档，增量入库不会自愈，用上面的 `metadata_only` 或 `rebuild` 处理（DSH 插件会在该会话首次检索时询问一次处理方式）。
- **批量阈值**：本服务按待处理文件数（只按扩展名统计）决定是否转后台，超过 `KB_ASYNC_THRESHOLD`（默认 25）时连同 `metadata_only` 刷新一起转后台并返回 `job_id`，用 `kb_status` 轮询；`rebuild=true` 通常不传 `paths`（路径取自库内），计数为 0，因此同步跑完。

## 用法

1. 首次建库：`kb_ingest(paths=["D:/graphene-papers"])`，或 `kb_zotero()`（建议先 `dry_run=true` 预览再真迁移）
2. 提问/检索：`kb_rag(query="...")`（默认 `depth=deep`：Top-3 证据 + 精排 + 引文关联 + 关联文献，逐条编号引用）或 `kb_search(query="...")`（默认 `depth=quick`：混合召回直出，亚秒级）
3. 维护：`kb_stats()` / `kb_dedup()` / `kb_clear(confirm=true)`；补库用 `kb_fetch(identifiers=["DOI 或 arXiv ID"])` 下载 PDF（出版商优先：arXiv → 出版商正式版（落地页 `citation_pdf_url`，校园网/机构订阅可直接取得订阅版）→ 落地页 pdf 链接 → Unpaywall 开放获取（`UNPAYWALL_EMAIL`）→ Crossref；只做常规抓取，不绕过付费墙。下载目录可用 `target_dir` 覆盖），再把 PDF 拖进 Zotero 或直接入库

数据默认在 `~/.kb-rag/kb.sqlite`；多个 agent 共用同一个库，想要隔离就设不同的 `KB_RAG_ROOT` 或每次传 `kb_root`。

## 注意

- MCP 无 UI，工具返回即纯文本（检索结果渲染成 markdown：来源 + `§章节 · p.N` 页码锚点 + DOI 链接；`deep` 模式另有 `↳ 引文补充` 的 `[Ref n]` 行与「关联文献」列表）
- `kb_scope`（DSH 里的查询范围/严格模式）是 DSH 会话概念，MCP 版不含；严格性由调用方按来源自行把握
- **深度模式由调用方传参**：引擎侧 `kb_search` 默认 `quick`（不精排、不渲染引文关联与关联文献）、`kb_rag` 默认 `deep`（全链路）；未传的 `top_k` / `snippet` / `rerank` / `related` 按 depth 取模式化缺省，显式传参永远优先
- 并发：引擎是单守护进程，`engine_client` 用 `asyncio.Lock` 把引擎请求串行化（一次仅一个在途），宿主并发触发的工具调用会在锁上排队，不会并发冲击引擎
- MCP 与 DSH 插件并行维护：共用引擎与文档，本目录单独演进
- **查询请用英文术语串**：`kb_search` / `kb_rag` 的查询按原样送往引擎，引擎不翻译、不扩写；库内正文以英文为主，中文查询会让 BM25 关键词路空转（中文二元组匹配不到英文正文）、只靠向量侧跨语言匹配，同一问题命中明显更差。写法用 3–12 个词、「材料/体系 + 方法/工艺 + 性质/表征」的组合（如 `graphene CVD copper single crystal nucleation suppression`），年份/期刊/作者放 `filters`；只有确实需要中文文献时才用原话另发一条中文查询。查询含中日韩字符而库内几乎全为英文时，引擎会在响应里附加 `lang_note` 说明这一点

## 已知限制

- **同步大批量仍会撞宿主超时**：未转后台的同步 ingest/zotero 调用若跑超过宿主单次调用时限（如 Kimi Work 60s）会被掐断——大批量请用上面的 async 流程（`kb_zotero` 也可用 `limit` 分批）
- **大库输出已收敛**：ingest/zotero 的文件清单只回最近 20 条 + `files_total` 真实总数；`kb_stats` 回最近 20 条——避免"MCP 返回体过大（chunk longer than limit）"。如需完整清单，直接查 `kb_root/kb.sqlite`
- 页码字段仅 PDF 且 schema v3 之后入库的数据才有（旧数据 `force` 重入库后恢复；详见仓库 `docs/OUTPUT-FORMAT.md`）
- **`citations` 字段同样是入库时产物**：上标角标识别与 References 切分在解析阶段完成，1.6.3 之前入库的文档需 `force` 重新入库才有引文关联；`quick` 模式下渲染不带引文链与关联文献
