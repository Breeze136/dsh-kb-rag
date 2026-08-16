# kb-rag — 本地文献知识库 RAG（DSH 插件版）

> 一次入库，永久检索；只把最相关的三句话送进 LLM，每个结论带精确溯源。

kb-rag 是一个面向 **DSH（DeepSeek Harness）** 的轻量本地数据库 RAG 插件：把 PDF/Zotero 文献建成带章节结构与向量索引的 SQLite 知识库，提供混合检索 + 精排 + 带溯源问答的完整工作流。全部索引、嵌入与精排模型本地运行，零 API 费用、零上传。

## 特性

- **8 个模型工具**：`kb_ingest`（文件/文件夹入库）、`kb_zotero`（Zotero 迁移）、`kb_search`（混合检索）、`kb_rag`（带溯源问答）、`kb_scope`（范围/严格模式）、`kb_dedup`（去重）、`kb_clear`（清空）、`kb_stats`（统计）
- **结构化切分**：论文章节识别（摘要×1.5、方法×1.2 权重）、内联标题识别、摘要自动提升、图注块；非论文回退段落切分
- **混合检索**：关键词 BM25（CJK 二元组友好）+ bge-small 向量余弦，RRF 融合，×章节权重
- **精排**：bge-reranker-base Cross-Encoder，Top-20 → Top-3（缺失时自动回退 bge-large-en 双塔）
- **增量与防重**：sha256 增量跳过（重跑 40 倍提速）、跨路径重复拦截、`kb_dedup` 存量清理
- **查询缓存**：同 query+filters 零重算；入库变更自动失效
- **溯源规范**：有 DOI → markdown 链接；无 DOI → `[作者, 年份, 文件名]`
- **范围与严格模式**：封闭库 / 库+全网 / 仅全网；strict 模式禁止库外知识外延
- **引擎守护进程**：模型只加载一次，热查询亚秒级；崩溃自愈；插件停止自动回收

## 架构

```
DSH 模型 ──工具调用──▶ 插件 Host(JS 薄层) ──JSON行协议──▶ kb_engine.py(常驻 serve)
                                                            ├─ ingest: 哈希跳过→PyMuPDF提取→章节切分→bge-small编码
                                                            ├─ search: SQL预过滤→BM25+向量双路→RRF融合→reranker精排→片段+来源
                                                            └─ 存储: 工作区/.kb/kb.sqlite (docs/chunks/vecs/cache)
```

数据流：原始 PDF → 逐字提取 + 章节切分 → 分块入库（附元数据与向量）→ 提问时混合检索+精排 → 返回 Top-N 原文片段（带 DOI/文件/章节/得分）→ 当前对话模型直接引用作答。

## 快速开始

见 [QUICKSTART.md](QUICKSTART.md)。核心三步：

1. 安装 Python 依赖（见 requirements.txt）
2. 把 `kb_engine.py` 放到 DSH 会话工作区根目录
3. 用 `cordis_define` 加载 `plugin/host.js` 与 `plugin/client.js`，运行后直接对话即可（首次检索会弹出"查询范围"选择）

## npm 静态包（给其他 Harness 用户）

已发布到 npm：**`dsh-kb-rag`**（[npmjs.com/package/dsh-kb-rag](https://www.npmjs.com/package/dsh-kb-rag)）。任何 DSH 部署可直接安装使用：

1. 在 DSH 部署目录安装（或写进部署 package.json dependencies）：

   ```bash
   npm install dsh-kb-rag
   ```

2. 在该部署的 cordis 组合（cordis.yml / 预设）中加载：

   ```yaml
   plugins:
     dsh-kb-rag: {}
   ```

3. 启动/重载 DSH，8 个工具自动注册。注意：DSH 插件加载器按包名从部署 node_modules 解析，**不会自动下载未安装的包**——第 1 步必须先执行一次。

静态包自带 `kb_engine.py`（随包分发，无需手动放置）；启动时自动检测 Python 依赖，缺失时在宿主日志打印 `pip install` 命令。完整说明见 [npm-package/README.md](npm-package/README.md)。

## 工具速查

| 工具 | 功能 | 典型说法 |
|---|---|---|
| kb_ingest | 文件/文件夹入库（增量+防重） | "把 papers 目录入库" |
| kb_zotero | Zotero 文献迁移（元数据+PDF） | "同步 Zotero" |
| kb_search | 混合检索+精排，返回片段+来源 | "搜 BiFeO3 畴壁导电" |
| kb_rag | 证据问答，强制引用溯源 | "畴壁导电机制是什么" |
| kb_scope | 范围（封闭库/库+网/仅网）+ 严格模式 | "切到严格模式" |
| kb_dedup | 清理存量重复 | "去重" |
| kb_clear | 清空全部文献（confirm 保护） | "清空知识库" |
| kb_stats | 统计与清单 | "看看库里有什么" |

## 性能基准（实测）

| 项目 | 结果 |
|---|---|
| 入库吞吐 | 242 篇 PDF/DOCX（1.8GB）→ **85.9s**（平均 355ms/篇） |
| 增量重跑 | 同目录二次入库 **2.17s**（40 倍提速） |
| 检索延迟 | 20k 块规模热查询 **0.4–1.3s**（含精排） |
| 库规模 | 209 文档 / 19,832 块 / 19,832 向量，SQLite 单文件 |

## 引用规范（答复样式）

| 场景 | 写法 |
|---|---|
| 有 DOI | `[作者, 年份, 期刊](https://doi.org/DOI)` |
| 无 DOI | `[作者, 年份, 文件名]` |
| 严格模式 | 仅基于本次证据作答，证据不足明确说"根据现有资料无法回答" |
| 常规模式 | 允许一般知识补充，补充处会注明"非库内证据" |
| 答复结尾 | 附"建议补充入库"提示（指出库内缺失的关键文献） |

## 配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `KB_EMBED_MODEL` | `BAAI/bge-small-zh-v1.5` | 嵌入模型（首次自动下载至 HF 缓存） |
| `KB_RERANK_MODEL` | `BAAI/bge-reranker-base` | 精排模型 |
| `HF_ENDPOINT` | 无 | 网络受限时设 `https://hf-mirror.com` |

## 目录结构

```
kb-rag/
├─ kb_engine.py          # Python 检索引擎（CLI + serve 常驻协议）
├─ plugin/
│  ├─ host.js            # DSH 插件 Host 半（8 个工具 + 守护进程 + RPC）
│  └─ client.js          # DSH 插件 Client 半（工具来源卡片，可选）
├─ npm-package/          # npm 静态包 dsh-kb-rag（lib/index.js + kb_engine.py）
├─ docs/DESIGN.md        # 设计文档（分块/检索/协议细节）
├─ QUICKSTART.md         # 五分钟上手
├─ CHANGELOG.md
├─ requirements.txt
└─ LICENSE
```

## 已知限制与路线图

- 元数据年份：无 PDF 元数据时从正文抓取，可能误抓（可用 Zotero 元数据覆盖）
- 检索性能：关键词扫描为内存实现，数十万块以上建议切换 FAISS HNSW / SQLite FTS5
- 路线图：中译英查询翻译（opus-mt 本地小模型）、图注 OCR、引用网络图谱

## License

MIT — 见 [LICENSE](LICENSE)
