# kb-rag 设计文档

## 1. 目标与非目标

**目标**：让本地文献"可问、可用、可溯源"，比直接上传给 LLM 更快、更省、更准。

**非目标**（明确不做）：多用户/权限、云端同步、自动爬取、引用网络图谱、图片理解（图注只存文字）、AI 自动写综述。

## 2. 存储模型（SQLite，工作区/.kb/kb.sqlite）

```sql
docs(id, path UNIQUE, title, authors, year, journal, doi, kind,
     sha256, size, mtime, chunk_count, indexed_at, zotero_key)
chunks(id, doc_id→docs, section, weight, seq, text,
       para_start, para_end, page_start, page_end)
vecs(chunk_id PK, vec BLOB)          -- float32, bge-small 512 维
cache(key PK, payload, created)      -- 查询缓存，入库变更时整体失效
```

- **schema v3（`PRAGMA user_version` 门控）**：`SCHEMA_VERSION = 3`，`_migrate()` 每次连接执行、幂等、纯加法优先——v1 = `docs.zotero_key`（连接时按 storage 路径回填），v2 = `chunks.para_start/para_end`（段落定位，旧行 NULL），v3 = `chunks.page_start/page_end`（PDF 物理页码锚点，旧行 NULL）；迁移日志走 stderr，`kb_stats` 返回 `schema_version` / `migration` / `health`（详见 `docs/MIGRATION.md`）
- 增量判定：`path` 命中且 `sha256` 相同 → skipped（0 解析）
- 跨路径防重：`sha256` 在任意其他路径已存在 → duplicate
- 孤儿向量：连接时清理（`vecs WHERE chunk_id NOT IN chunks`）

## 3. 分块策略（chunk_document）

1. 按空行切段，逐段检测标题：
   - 英文/中文章节正则（Abstract/Introduction/Methods/Results/Discussion/Conclusion/References/致谢…）
   - Markdown `#` 标题 → 通用章节（标题文本作 section）
   - 数字/中文数字前缀剥离
2. **内联标题**：Science 类无空行正文中的行间大写标题（"…domain coalescence. Discussion and outlook Generally…"）按正则切段
3. **摘要自动提升**：无显式 Abstract 标题时，有界 front-matter 中首个 400–3000 字符段落提升为 Abstract（×1.5）
4. **图注块**：`Fig./Figure/Table/图/表 + 数字` 开头的段独立成块
5. 无任何标题 → 段落合并回退（句子级切分，800 字符上限）
6. 权重：Abstract 1.5 / Methods 1.2 / 其他 1.0 / References 0（保留入库，见第 8 条）/ 致谢·附录 0（丢弃）
7. **锚点**：`chunk_document` 返回 `(section, weight, text, para_start, para_end, page_start, page_end)`——段落号为全局计数（跨章节不重置，References 段也参与计数），页码为 PDF 物理页码（1 基、段落归属起始页；txt/md/docx 无页，为 NULL）；超长块切分后各片沿用同一起止范围
8. **References 保留**：weight 0 但**保留入库**，作为引文关联的数据源（正文 `[n]` → 条目）；检索侧按 `c.weight > 0` 排除；长引文列表按行边界切分（`split_refs`，非句边界），保住 `N.` 行首锚点，条目才不会被压平
9. **上标角标识别**：`read_document` 按字体度量（字号 ≤ 行内正文 80% 且基线抬高 ≥ 12%）把 Nature 系上标引用 `graphene1,2` 转写为 `graphene[1,2]` 方括号形式入库；宁缺勿错，跳过作者行（≥3 个上标簇）、指数、单位标记

## 4. 检索流水线（search/rag 命令）

```
query → filters SQL 预过滤（authors/title/journal/kind/section/year）+ 恒加 c.weight > 0（排除 References）
      → 双路召回:
        关键词: extract_terms（CJK 短语+二元组；ASCII 词干）→ 内存 BM25(k1=1.2,b=0.75)
        向量:  bge-small 归一化查询向量 → FAISS IndexFlatIP 余弦 Top-20×
      → RRF 融合: Σ 1/(60+rank)
      → ×章节权重
      → 精排: bge-reranker-base Cross-Encoder 重打 Top-20 池 → Top-K
      → 片段定位（最高 idf 命中词窗口）→ 结果层按文档去重
      → 引文关联（[n] → References 条目；命中库内的条目带 lib 字段）
      → 返回 {title,authors,year,journal,doi,section,para,page,score,snippet,citations}
```

- depth 双模式：`quick`（`kb_search` 默认）=混合召回直出，不精排、不渲染引文关联与关联文献；`deep`（`kb_rag` 默认）=完整链路（精排 + 引文关联 + 关联文献）；未传的 `top_k`/`snippet`/`rerank`/`related` 按 depth 取模式化缺省，显式传参永远优先
- mode：`keyword | vector | hybrid`（默认 hybrid）；rerank 缺省随 depth（quick 关 / deep 开），模型缺失自动降级
- 页码锚点：`page` 为 PDF 物理页码（1 基），渲染优先 `§章节 · p.N`（跨页为 `p.N–M`）；无页码时降级为段落号 `para`，再降级为章节名
- 查询缓存：key = sha1(query,filters,top_k,snippet,mode,rerank_flag,reranker名,related_flag,related_k)，命中零重算；任何入库变更整体失效（`DELETE FROM cache`）
- 降级链：向量缺失→纯关键词；精排失败→融合序直接输出；Cross-Encoder 不可用→`bge-large-en-v1.5` 双塔余弦重排

## 5. 嵌入与精排模型

- 嵌入：`BAAI/bge-small-zh-v1.5`（SentenceTransformer，normalize），中文查询加检索指令前缀
- 精排：`BAAI/bge-reranker-base`（CrossEncoder）；不可用时回退 `bge-large-en-v1.5` 双塔余弦
- 加载：本地缓存优先（local_files_only），缺失才尝试下载（HF_ENDPOINT 镜像支持，下载限时）；直连失败自动改写 endpoint 并经 `https://hf-mirror.com` 重试一次（`_apply_hf_mirror()`）
- 增量编码：只编码新分块（vecs 表差集）

## 6. Zotero 迁移

- 定位：`~/Zotero/zotero.sqlite`、`~/Documents/Zotero/...`、`%APPDATA%/Zotero/Profiles/*/zotero/zotero.sqlite`，或 `zotero_db` 显式指定
- 附件路径：`storage:` 前缀 → `<dataDir>/storage/<itemKey>/<文件名>`（不依赖 linkMode）
- 元数据：itemData（title/date/publicationTitle/DOI）+ itemCreators（author 角色）→ 覆盖 PDF 抽取值；同时落 `zotero_key`，检索结果渲染 `zotero://open-pdf/library/items/{key}` 可直开 Zotero 阅读器
- 缺失文件标记 missing 跳过；dry_run 只列候选

## 7. 安装与分发（v1.6.5 一键安装）

四个安装入口，同一条安装链（Python 依赖 → 引擎冒烟测试 → Node/pnpm → dsh 插件安装激活 → 模型预下载）：

| 入口 | 命令 | 适用 |
|---|---|---|
| 微包 `dsh-kb-rag-install`（bin 名与包名一致，零逻辑转发到主包的安装器） | `npx dsh-kb-rag-install --profile <name>` | 最终用户，无需克隆，裸命令不再 E404；Node ≥18，跨平台 |
| npm bin（`install.mjs` → 按平台分发 `scripts/install.ps1\|sh`） | `npx --yes --package dsh-kb-rag -c "dsh-kb-rag-install --profile <name>"`（旧写法，等价） | 最终用户；与上一入口同一条链 |
| 平台脚本（`scripts/install.ps1` / `install.sh`，npm 包内同款镜像） | `install.cmd` 双击 / `./scripts/install.sh` | 克隆仓库的用户；npm 手动安装后从 `node_modules/dsh-kb-rag/scripts/` 运行 |
| 插件自检 | `dsh plugin add` 后设 `KB_AUTO_PIP=1` 重启 DSH | 已装插件但缺 Python 依赖的环境 |

公共参数：`--profile <name>`（DSH profile；缺省时扫 `~/.dsh/profiles/` 自动检测）、`--mirror <url>`（pip 镜像，或 `PIP_INDEX_URL`）、`--models` / `--no-models`（预下载 bge 模型，尊重 `HF_ENDPOINT`/`KB_EMBED_MODEL`/`KB_RERANK_MODEL`；`install.mjs` 默认注入 `--models` 与 `--yes`）、`--with-docx`（可选 python-docx）、`--dry-run`、`-y`。模型下载失败自动切镜像重试；全部幂等可重跑。

**依赖探测与 KB_AUTO_PIP（插件内建，lib/index.js）**：

- 启动时 spawn `python -c <importlib.util.find_spec 探测>`，一次性输出**完整缺失清单**（裸 import 链在首个缺失处中断，只能看到一个——v1.2.0 的缺陷）
- 默认只打印 `pip install` 命令到宿主日志（不联网、不阻塞加载）；`KB_AUTO_PIP=1` 时自动执行 `python -m pip install`（固定 argv、装后二次探测确认）
- 缺失且未自动安装时：首次工具调用先过一次性 depsGate（等探测/安装结束，之后零开销），再返回带三种修复路径的中文错误——不让引擎子进程反复崩出裸 ImportError
- 安全边界：引擎/探测/pip spawn 全部固定 argv 数组；`install.mjs` 分发器以 `spawnSync` 直启 bash/powershell（不经 shell 拼接），Windows 参数翻译表固定；`package.json` 仍声明零 lifecycle install scripts，bin 仅显式调用时执行（详见 SECURITY.md）

## 8. 插件架构（DSH 双端）

### Host 半（plugin/host.js）

- **守护进程**：`kb_engine.py serve`，JSON 行协议（`{id,command,payload}` → `{id,ok,response|error}`），stdin pipe + stdout collect(offset 读取)，串行请求队列，崩溃自愈，工作区切换自动重启，插件停止 terminate
- **工具**：9 个（`kb_ingest` / `kb_zotero` / `kb_search` / `kb_rag` / `kb_scope` / `kb_dedup` / `kb_clear` / `kb_stats` / `kb_fetch`，见 README），长任务（ingest/zotero）超时 30min，支持 exec.signal 取消
- **范围/深度/严格模式**：内存偏好（scope：kb/both/web；depth：quick/deep；strict），首次检索经 userQuestions.ask 弹出范围与检索深度两个问题（120s 竞速，默认 kb / deep）
- **RPC**：`kb-open-file` — Client 打开原文回退通道（系统默认程序打开）
- **输出渲染**：来源列表 Markdown（DOI 链接内联、`§章节 · p.N` 页码锚点、`[Ref n]` 引文行与「关联文献」区块；无 DOI 显示文件名），卡片兼容解析

### Client 半（plugin/client.js）

- 注册 `tool.call.toolview`（key=kb_rag/kb_search）来源卡片；不渲染的界面自动降级为 Host 输出的 markdown 文本

## 9. 引擎进程协议（kb_engine.py）

- 单发：`echo '<json>' | python kb_engine.py <ingest|ingest_async|status|search|rag|stats|zotero|dedup|clear|fetch>`
- 常驻：`python kb_engine.py serve`（逐行 JSON，stdout ensure_ascii 单行 flush）
- 异步任务：`ingest_async`（payload 的 `command` 字段分发 ingest/zotero）fork 独立子进程后立即返回 12 位十六进制 `job_id`（`status=running`），进度与结果写在 `<kb_root>/.kb-jobs/`；`status` 轮询返回 `running`/`done`/`error`/`not_found`，progress/result 以临时文件 + `os.replace` 原子落盘（不会读到半截 JSON），done 后清理 job/progress 中间文件；`run_job <job.json>` 是子进程内部入口，不由宿主直接调用
- `fetch`：按 arXiv → 出版商正式版（落地页 `citation_pdf_url`，校园网/机构订阅可直接取得订阅版）→ 落地页 pdf 链接 → Unpaywall（`UNPAYWALL_EMAIL`）→ Crossref 的顺序取 PDF，只做常规抓取、不绕过付费墙
- 响应统一 `{ok:bool, ...fields, engine:ver, ms:int}`；单文件错误不中断批量

## 10. 实测性能（2026-xx，Windows/CPU）

| 场景 | 指标 |
|---|---|
| 首次全量入库 | 242 文件/1.8GB → 85.9s（~355ms/篇，含向量） |
| 增量重跑 | 2.17s（40×） |
| 热检索（20k 块，deep） | 0.4–1.3s（混合+精排）；`quick` 无精排直出，亚秒级 |
| 清空 | 177 文档/25k 块 → 278ms（含 VACUUM） |
