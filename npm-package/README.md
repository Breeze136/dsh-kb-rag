# dsh-kb-rag

DSH 静态插件（Host 端）：本地文献知识库 RAG。轻量、快速、精确，检索 + 关联查询，省 token。

把 PDF / TXT / MD / DOCX 或整个文件夹、以及 Zotero 文献库导入本地知识库（工作区 `/.kb`），
用 **BM25 + FAISS 向量 + bge-reranker 精排** 做混合检索，供模型带着精确来源作答。

## 功能（8 个模型工具）

| 工具 | 用途 |
| --- | --- |
| `kb_ingest` | 入库文件/文件夹（PDF/TXT/MD/DOCX，递归扫描），增量、去重、按章节切分 + 向量化 |
| `kb_zotero` | 把本地 Zotero 文献库（带 PDF 附件）批量迁移入库 |
| `kb_search` | 混合检索 Top-N 片段 + 精确来源（标题/作者/年份/期刊/DOI/章节/得分） |
| `kb_rag` | 检索证据片段（默认 Top-3）供模型直接作答，每句标注引用编号 |
| `kb_scope` | 设置/查看查询范围（kb / both / web）与严格模式 |
| `kb_stats` | 文档数、分块数、向量数、最近入库列表 |
| `kb_dedup` | 清理重复文献（保留最早一份） |
| `kb_clear` | 清空全部文献与索引（必须显式 `confirm: true`） |

来源引用格式：有 DOI → `[作者, 年份, 期刊](https://doi.org/DOI)`（可点击）；无 DOI → `[作者, 年份, 文件名]`。
每次问答末尾会附"建议补充入库"提示；严格模式（strict）下答案仅基于库内证据。

## 安装与启用

```bash
npm install dsh-kb-rag
```

在部署的 cordis 组合（cordis.yml / 预设）中加载本包：

```yaml
plugins:
  dsh-kb-rag: {}
```

或通过 cordis-plugin-loader 按包名解析加载。加载后模型会话自动获得上述 8 个工具。

## 依赖要求

- Node.js ≥ 18（宿主进程）
- Python 3.9+ 及以下包（首次检索/入库时若缺会提示安装）：

```bash
pip install pymupdf faiss-cpu sentence-transformers
```

嵌入模型 `BAAI/bge-small-zh-v1.5`、精排模型 `BAAI/bge-reranker-base` 首次使用时自动下载
（本地 HF 缓存；国内网络可用 `HF_ENDPOINT=https://hf-mirror.com`）。

- 对等依赖：`@deepseek-ai/cordis` ^4、`@deepseek-ai/dsh-tools`（宿主工具注册接口）。

## 用法示例

1. 入库：`kb_ingest(paths=["papers/", "notes.md"])`
2. Zotero：`kb_zotero(dry_run=true)` 预览后去掉 dry_run 正式迁移
3. 检索：`kb_search(query="attention is all you need", top_k=5, filters={year: ">=2018"})`
4. 问答：`kb_rag(query="Transformer 的位置编码有哪几种？", strict=true)`
5. 范围：`kb_scope(scope="both")`；查看库内有什么：`kb_stats()`

数据默认持久化在会话工作区 `/.kb`，各工具可用 `kb_root` 覆盖。

## 注意

- 本包为 Host 端静态插件（工具全部在服务端执行），不含浏览器 UI。
- 引擎通过随包分发的 `kb_engine.py` 以常驻子进程运行（JSON-line 协议），会话结束自动退出。
- 网络受限环境（无法访问 HF / pip）需提前准备模型缓存与 Python 依赖。

## License

MIT
