# kb-rag 设计文档

## 1. 目标与非目标

**目标**：让本地文献"可问、可用、可溯源"，比直接上传给 LLM 更快、更省、更准。

**非目标**（明确不做）：多用户/权限、云端同步、自动爬取、引用网络图谱、图片理解（图注只存文字）、AI 自动写综述。

## 2. 存储模型（SQLite，工作区/.kb/kb.sqlite）

```sql
docs(id, path UNIQUE, title, authors, year, journal, doi, kind,
     sha256, size, mtime, chunk_count, indexed_at)
chunks(id, doc_id→docs, section, weight, seq, text)
vecs(chunk_id PK, vec BLOB)          -- float32, bge-small 512 维
cache(key PK, payload, created)      -- 查询缓存，入库变更时整体失效
```

- 增量判定：`path` 命中且 `sha256` 相同 → skipped（0 解析）
- 跨路径防重：`sha256` 在任意其他路径已存在 → duplicate
- 孤儿向量：连接时清理（`vecs WHERE chunk_id NOT IN chunks`）

## 3. 分块策略（chunk_document）

1. 按空行切段，逐段检测标题：
   - 英文/中文章节正则（Abstract/Introduction/Methods/Results/Discussion/Conclusion/References/致谢…）
   - Markdown `#` 标题 → 通用章节（标题文本作 section）
   - 数字/中文数字前缀剥离
2. **内联标题**：Science 类无空行正文中的行间大写标题（"…coupling. Discussion and outlook Generally…"）按正则切段
3. **摘要自动提升**：无显式 Abstract 标题时，有界 front-matter 中首个 400–3000 字符段落提升为 Abstract（×1.5）
4. **图注块**：`Fig./Figure/Table/图/表 + 数字` 开头的段独立成块
5. 无任何标题 → 段落合并回退（句子级切分，800 字符上限）
6. 权重：Abstract 1.5 / Methods 1.2 / 其他 1.0 / References·致谢·附录 0（丢弃）

## 4. 检索流水线（search/rag 命令）

```
query → filters SQL 预过滤（authors/title/journal/kind/year/section）
      → 双路召回:
        关键词: extract_terms（CJK 短语+二元组；ASCII 词干）→ 内存 BM25(k1=1.2,b=0.75)
        向量:  bge-small 归一化查询向量 → FAISS IndexFlatIP 余弦 Top-20×
      → RRF 融合: Σ 1/(60+rank)
      → ×章节权重
      → 精排: bge-reranker-base Cross-Encoder 重打 Top-20 池 → Top-K
      → 片段定位（最高 idf 命中词窗口）→ 返回 {title,authors,year,journal,doi,section,score,snippet}
```

- mode：`keyword | vector | hybrid`（默认 hybrid）；rerank 默认开、模型缺失自动降级
- 查询缓存：key = sha1(query,filters,top_k,snippet,mode,rerank_flag,reranker名)，命中零重算
- 降级链：向量缺失→纯关键词；精排失败→融合序直接输出

## 5. 嵌入与精排模型

- 嵌入：`BAAI/bge-small-zh-v1.5`（SentenceTransformer，normalize），中文查询加检索指令前缀
- 精排：`BAAI/bge-reranker-base`（CrossEncoder）；不可用时回退 `bge-large-en-v1.5` 双塔余弦
- 加载：本地缓存优先（local_files_only），缺失才尝试下载（HF_ENDPOINT 镜像支持，下载限时）
- 增量编码：只编码新分块（vecs 表差集）

## 6. Zotero 迁移

- 定位：`~/Zotero/zotero.sqlite`、`~/Documents/Zotero/...`、`%APPDATA%/Zotero/Profiles/*/zotero/zotero.sqlite`，或 `zotero_db` 显式指定
- 附件路径：`storage:` 前缀 → `<dataDir>/storage/<itemKey>/<文件名>`（不依赖 linkMode）
- 元数据：itemData（title/date/publicationTitle/DOI）+ itemCreators（author 角色）→ 覆盖 PDF 抽取值
- 缺失文件标记 missing 跳过；dry_run 只列候选

## 7. 插件架构（DSH 双端）

### Host 半（plugin/host.js）

- **守护进程**：`kb_engine.py serve`，JSON 行协议（`{id,command,payload}` → `{id,ok,response|error}`），stdin pipe + stdout collect(offset 读取)，串行请求队列，崩溃自愈，工作区切换自动重启，插件停止 terminate
- **工具**：8 个（见 README），长任务（ingest/zotero）超时 30min，支持 exec.signal 取消
- **范围/严格模式**：内存偏好（kb/both/web + strict），首次检索经 userQuestions.ask 弹出选择（120s 竞速，默认 kb）
- **RPC**：`kb-open-file` — Client 打开原文回退通道（系统默认程序打开）
- **输出渲染**：来源列表 Markdown（DOI 链接内联，无 DOI 显示文件名），卡片兼容解析

### Client 半（plugin/client.js）

- 注册 `tool.call.toolview`（key=kb_rag/kb_search）来源卡片；不渲染的界面自动降级为 Host 输出的 markdown 文本

## 8. 引擎进程协议（kb_engine.py）

- 单发：`echo '<json>' | python kb_engine.py <ingest|search|rag|stats|zotero|dedup|clear>`
- 常驻：`python kb_engine.py serve`（逐行 JSON，stdout ensure_ascii 单行 flush）
- 响应统一 `{ok:bool, ...fields, engine:ver, ms:int}`；单文件错误不中断批量

## 9. 实测性能（2026-xx，Windows/CPU）

| 场景 | 指标 |
|---|---|
| 首次全量入库 | 242 文件/1.8GB → 85.9s（~355ms/篇，含向量） |
| 增量重跑 | 2.17s（40×） |
| 热检索（20k 块） | 0.4–1.3s（混合+精排） |
| 清空 | 177 文档/25k 块 → 278ms（含 VACUUM） |
