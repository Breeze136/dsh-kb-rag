# Changelog

## [1.0.0] - 2026-xx-xx（发布版）

初始发布：本地文献知识库 RAG（DSH 插件 + Python 引擎）。

- 8 个工具：kb_ingest / kb_zotero / kb_search / kb_rag / kb_scope / kb_dedup / kb_clear / kb_stats
- 章节结构化切分（内联标题、摘要自动提升、图注块）
- 混合检索（BM25 + bge-small 向量 RRF 融合）+ bge-reranker-base 精排
- 增量入库（sha256）+ 跨路径防重 + 查询缓存
- 引擎守护进程（模型单次加载、崩溃自愈）
- Zotero 迁移（元数据覆盖、missing 跳过、dry-run）
- 范围控制（封闭库/库+全网/仅全网）+ 严格模式（strict）
- 溯源规范：DOI markdown 链接 / 无 DOI 文件名引用
- 客户端来源卡片（可选，随界面能力渲染）
- 实测性能：242 篇 85.9s 入库、40× 增量提速、20k 块热检索亚秒级
