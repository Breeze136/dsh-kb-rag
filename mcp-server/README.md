# kb-rag MCP server

把 kb-rag 的本地文献知识库 RAG 通过 **MCP（Model Context Protocol）** 暴露给任意支持 MCP 的桌面 agent——Claude Desktop、Cherry Studio、Kimi、DeepSeek、Zcode、Open WebUI、Cursor 等。

与 DSH 插件共用同一个 `kb_engine.py` 引擎（常驻守护进程、JSON-lines 协议），**引擎零改动**。7 个工具：`kb_ingest` / `kb_zotero` / `kb_search` / `kb_rag` / `kb_stats` / `kb_dedup` / `kb_clear`。

## 安装依赖

```bash
pip install -r requirements.txt
# 引擎依赖（与 DSH 版相同）：
pip install pymupdf faiss-cpu sentence-transformers
```

模型首次使用自动下载（受限网络设 `HF_ENDPOINT=https://hf-mirror.com`）。

## 配置（stdio MCP server）

各客户端都支持"添加 MCP server"，命令统一为：

```bash
python "<本仓库路径>/mcp-server/server.py"
```

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

### 其它（Kimi / DeepSeek / Zcode / Open WebUI / Cursor）

在各自的 MCP 配置里加同样的 stdio server（`command: python`，`args: [server.py 绝对路径]`）。

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `KB_RAG_ROOT` | `~/.kb-rag` | 知识库默认目录（各工具可用 `kb_root` 参数覆盖） |
| `KB_RAG_PYTHON` | `python` | 引擎所用的 Python 解释器（带引擎依赖的那个） |
| `HF_ENDPOINT` | 无 | 模型镜像（如 `https://hf-mirror.com`） |

## 用法

1. 首次：`kb_ingest(paths=["D:/papers"])` 或 `kb_zotero()` 建库
2. 提问/检索：`kb_rag(query="...")` 或 `kb_search(query="...")`
3. 维护：`kb_stats()` / `kb_dedup()` / `kb_clear(confirm=true)`

数据默认在 `~/.kb-rag/kb.sqlite`，多个 agent 共用同一个库；想要隔离就设不同的 `KB_RAG_ROOT` 或每次传 `kb_root`。

## 注意

- MCP 无 UI，工具返回即纯文本（检索结果渲染成带 DOI 链接的 markdown）
- `kb_scope`（DSH 里的查询范围/严格模式）是 DSH 会话概念，MCP 版不含；严格性由调用方（agent）按 `kb_rag`/`kb_search` 返回的来源自行把握
- 这是**补充内容**：DSH 插件版仍是主发布形态，本目录单独演进

## 已知限制

- **长任务会超时但后台照常完成**：`kb_zotero`（全量）/ `kb_ingest`（大批量）可能跑几分钟，部分 MCP 客户端有自己的工具超时（如 60s），会先报超时——但引擎守护进程是独立子进程，任务会继续跑完。做法：① 用 `kb_zotero(limit=N)` 分批；② 超时后等一会儿再 `kb_stats` 确认结果，数据不会丢。
- **大库输出已收敛**：`kb_stats`/`kb_ingest`/`kb_zotero` 返回的是紧凑摘要 + 最近 N 条（引擎侧 `kb_stats` 只回最近 20 条），避免"MCP 返回体过大（chunk longer than limit）"。如需完整清单，直接查 `kb_root/kb.sqlite`。
