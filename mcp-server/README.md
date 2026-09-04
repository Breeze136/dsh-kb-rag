# kb-rag MCP server

把 kb-rag 的本地文献知识库 RAG 通过 **MCP（Model Context Protocol，stdio）** 暴露给任意支持 MCP 的桌面 agent——Claude Desktop、Cherry Studio、Kimi、DeepSeek、Cursor、Zcode、Open WebUI 等。

与 DSH 插件共用同一个 `kb_engine.py` 引擎（常驻 serve daemon + JSON-lines 逐行协议；异步 job、schema v3 页码等能力也都在引擎内实现）；MCP 侧只提供 `server.py` + `engine_client.py` 这一层 stdio 封装，模型也只下载/加载一份。

**9 个工具**：`kb_ingest`（支持 async 后台）/ `kb_status`（后台任务轮询）/ `kb_zotero`（支持 `async_mode=true`）/ `kb_search` / `kb_rag` / `kb_stats` / `kb_dedup` / `kb_clear` / `kb_fetch`。

## 与 DSH 插件的工具对照

| DSH 插件 | MCP server | 说明 |
|---|---|---|
| `kb_scope` | — | 查询范围/严格模式是 **DSH 会话概念**，MCP 版没有；严格性由调用方（agent）按检索来源自行把握 |
| — | `kb_status` | 轮询后台任务（配合 `kb_ingest` / `kb_zotero` 的 async 模式）|
| 其余 8 个 | 同左 | 同一引擎、同一行为 |

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
| `HF_ENDPOINT` | 无 | 模型镜像（如 `https://hf-mirror.com`）|

## 异步与超时

- **`kb_ingest` 自动转后台**：目录递归扫描或显式路径的待处理文件数超过 `KB_ASYNC_THRESHOLD`（默认 25）时，自动 fork 独立子进程执行并**立即返回 `job_id`**（`status=running`）——agent 无需知道 async_mode 的存在，直接传整个文献库文件夹也不会超时；文件少则同步执行、直接返回结果。`async_mode=true` 强制后台，`false` 强制同步。
- **`kb_zotero(async_mode=true)`**：整库迁移在后台执行（数百篇也不怕超时）；`dry_run` 与 `async_mode` 不要同时用。
- **`kb_status(job_id=...)`**：`running` 时返回已处理进度；`done` 时返回入库 totals 与最近文件；任务完结后引擎自动清理 `.kb-jobs/` 中间文件（result 保留可重复读）；`kb_clear` 也会一并清空 `.kb-jobs/`。`job_id` 有严格格式校验（12 位十六进制）。
- **宿主超时不影响后台任务**：任务跑在独立子进程里、独立于 MCP 请求；Kimi Work 等宿主的 60s 超时只掐断"等待"这一次调用，任务照常在后台跑完，之后用 `kb_status` 取结果，数据不会丢。
- **分批不再是唯一手段**：同步模式仍可用 `limit=N` 分批（小批量、想直接拿结果的场景）；大批量首选 async。

## 用法

1. 首次建库：`kb_ingest(paths=["D:/papers"])`，或 `kb_zotero()`（建议先 `dry_run=true` 预览再真迁移）
2. 提问/检索：`kb_rag(query="...")`（默认 Top-3 证据 + 逐条编号引用）或 `kb_search(query="...")`
3. 维护：`kb_stats()` / `kb_dedup()` / `kb_clear(confirm=true)`；补库用 `kb_fetch(identifiers=["DOI 或 arXiv ID"])` 下载 OA PDF（出版商正式版优先，下载目录可用 `target_dir` 覆盖），再把 PDF 拖进 Zotero 或直接入库

数据默认在 `~/.kb-rag/kb.sqlite`；多个 agent 共用同一个库，想要隔离就设不同的 `KB_RAG_ROOT` 或每次传 `kb_root`。

## 注意

- MCP 无 UI，工具返回即纯文本（检索结果渲染成带 DOI 链接的 markdown）
- `kb_scope`（DSH 里的查询范围/严格模式）是 DSH 会话概念，MCP 版不含；严格性由调用方按来源自行把握
- 并发：引擎是单守护进程，`engine_client` 用 `asyncio.Lock` 把引擎请求串行化（一次仅一个在途），宿主并发触发的工具调用会在锁上排队，不会并发冲击引擎
- MCP 与 DSH 插件并行维护：共用引擎与文档，本目录单独演进

## 已知限制

- **同步大批量仍会撞宿主超时**：未转后台的同步 ingest/zotero 调用若跑超过宿主单次调用时限（如 Kimi Work 60s）会被掐断——大批量请用上面的 async 流程，或 `limit` 分批
- **大库输出已收敛**：ingest/zotero 的文件清单只回最近 20 条 + `files_total` 真实总数；`kb_stats` 回最近 20 条——避免"MCP 返回体过大（chunk longer than limit）"。如需完整清单，直接查 `kb_root/kb.sqlite`
- 页码字段仅 PDF 且 schema v3 之后入库的数据才有（旧数据 `force` 重入库后恢复；详见仓库 `docs/OUTPUT-FORMAT.md`）
