# QUICKSTART — 五分钟上手

## 1. 环境要求

- Python 3.10+（本机为 3.12）
- DSH 会话（插件运行环境）

```bash
pip install PyMuPDF faiss-cpu sentence-transformers numpy
# 可选：DOCX 支持
pip install python-docx
```

模型（bge-small-zh-v1.5、bge-reranker-base）首次使用时自动从 HuggingFace 下载到本地缓存；
网络受限时先执行 `set HF_ENDPOINT=https://hf-mirror.com`（Windows）再运行。

## 2. 放置引擎

把 `kb_engine.py` 复制到你的 DSH 会话工作区根目录（插件会按会话工作区自动定位）。

## 3. 加载 DSH 插件

在 DSH 会话中调用 `cordis_define`：

- `code.host` ← `plugin/host.js` 的文件内容
- `code.client` ← `plugin/client.js` 的文件内容
- 然后 `cordis_run` 激活（客户端半首次需要审批，勾选"授权未来版本"）

或者：直接在对话里说"加载 kb-rag 插件，代码在工作区 kb-rag/plugin/ 下"，让模型代劳。

## 4. 第一次使用

1. 首次检索会自动弹出**查询范围**选择（封闭库 / 库+全网 / 仅全网）
2. 入库：
   - 文件夹："把 `PDF file test` 入库" → kb_ingest
   - Zotero："同步 Zotero" → kb_zotero（自动定位 zotero.sqlite）
3. 提问："BiFeO3 畴壁导电机制是什么？" → kb_rag（自动带引用）

## 5. 常用操作

| 需求 | 说法 |
|---|---|
| 切换范围 | "切到知识库+全网"（kb_scope） |
| 严格模式 | "严格只按库内回答"（kb_rag strict=true） |
| 增量同步 | 重复 kb_ingest / kb_zotero（自动跳过） |
| 清理重复 | "去重"（kb_dedup） |
| 清空重建 | "清空知识库"（kb_clear，需确认） |
| 看清单 | "看看库里有什么"（kb_stats） |

## 6. 常见问题

- **首次检索慢（~15s）**：模型加载（守护进程只加载一次，后续亚秒级）
- **Zotero 报 missing**：附件文件本体缺失（未下载），正常跳过
- **工具输出不是卡片**：部分 DSH 界面不渲染自定义卡片，不影响使用——点击靠答复中的 DOI 链接与文件名
