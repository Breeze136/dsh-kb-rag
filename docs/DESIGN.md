# kb-rag 设计文档

## 1. 目标与非目标

**目标**：让本地文献"可问、可用、可溯源"，比直接上传给 LLM 更快、更省、更准。

**非目标**（明确不做）：多用户/权限、云端同步、自动爬取、引用网络图谱、图片理解（图注只存文字）、AI 自动写综述。

## 2. 存储模型（SQLite，工作区/.kb/kb.sqlite）

```sql
docs(id, path UNIQUE, title, authors, year, journal, doi, kind,
     sha256, size, mtime, chunk_count, indexed_at, zotero_key, indexed_with)
chunks(id, doc_id→docs, section, weight, seq, text,
       para_start, para_end, page_start, page_end)
vecs(chunk_id PK, vec BLOB)          -- float32, bge-small 512 维
cache(key PK, payload, created)      -- 查询缓存，入库变更时整体失效
```

- **schema v4（`PRAGMA user_version` 门控）**：`SCHEMA_VERSION = 4`，`_migrate()` 每次连接执行、幂等、纯加法优先——v1 = `docs.zotero_key`（连接时按 storage 路径回填），v2 = `chunks.para_start/para_end`（段落定位，旧行 NULL），v3 = `chunks.page_start/page_end`（PDF 物理页码锚点，旧行 NULL），v4 = `docs.indexed_with`（写入该行的解析器版本 `VERSION/revN`，旧行 NULL）；迁移日志走 stderr，`kb_stats` 返回 `schema_version` / `parser_rev` / `indexed_with` / `stale_docs` / `stale_sample` / `migration` / `health`（详见 `docs/MIGRATION.md`）
- 增量判定：`path` 命中且 `sha256` 相同 → skipped（0 解析）
- 跨路径防重：`sha256` 在任意其他路径已存在 → duplicate
- 陈旧判定：`docs.indexed_with` 取 `/rev` 之后的部分与当前 `PARSER_REV` **精确**比较（避免 `rev2` 前缀误匹配 `rev20`），不一致或为 NULL 即计入 `kb_stats` 的 `stale_docs`；判定只看 `rev`、不看引擎 `VERSION`，否则每次发版都会把整个库标成陈旧，提示变成噪音
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

## 4. 入库通道（ingest / metadata_only / rebuild / async_if_large）

- **三种模式**：普通入库（增量判定 + 去重）、`metadata_only=true`（只刷元数据）、`rebuild=true`（按库内路径原地重灌）；引擎同一入口 `cmd_ingest` 处理，响应里的 `mode` 字段指明本次实际走的是哪种
- **`metadata_only=true`**：逐篇走 `_refresh_meta_file()`——`read_first_page()` 只取首页文本 + PDF 元数据 + XMP（不做全文解析、不做上标角标转写），再跑与全量入库同一套 `extract_meta()`，`UPDATE docs` 的 title/authors/year/journal/doi 与 `indexed_with`，**不重切块、不重嵌入**（无需模型，实测约 90 ms/篇，312 篇约 30 s）。sha256 与库内不一致的条目记 `changed` 跳过（元数据必须与已入库的正文一致，内容变更属于真正的入库）；库内不存在的记 `not_indexed`
- **`rebuild=true`**：`paths` 缺省时从 `SELECT path FROM docs ORDER BY id` 取库内路径并强制 `force=True` 原地重灌——传目录会因 `force` 绕过去重检测而把内容重复的文件重复入库，所以只认库内记录
- **`async_if_large`**：引擎自己用 `_count_candidates()` 统计待处理文件数（数到阈值+1 即停），超过 `KB_ASYNC_THRESHOLD`（默认 25）即转 `ingest_async` fork 后台任务并返回 `job_id`（响应带 `background=true` 与 `pending_files`）；带 `progress_path` 的子进程绝不再 fork（防递归），`rebuild=true` 不参与该判定。进度按 `{processed, errors, chunks}` 原子写回 `.kb-jobs/<job_id>.progress.json`。DSH 插件走这条路（`async_if_large: true`，计数在引擎侧，宿主不碰文件系统）；MCP 服务器仍在宿主侧按扩展名统计（`_should_async`）并直接调 `ingest_async`
- **逐文件 commit**：把 SQLite 写锁窗口从"整批"缩到"单文件+嵌入"，避免异步入库持锁期间同库的只读命令在 `connect` 处撞锁
- **元数据抽取**（`extract_meta`，全量入库与元数据刷新共用同一套判据）：
  - **判据全部落在首页 + PDF 元数据 + XMP 上**（`scope = page1`，并在 References/Bibliography 处截断，避免命中参考文献里别人的 DOI）。因此 `read_first_page()` 的结果与全量解析一致，而成本从约 0.3–1 s/篇降到只读首页的量级（端到端刷新实测约 90 ms/篇）
  - **DOI 级联**：首页文本 DOI → **XMP 兜底**（`dc:identifier` / `prism:doi` 等；部分出版商 PDF 正文根本不印 DOI，实测 312 篇里靠 XMP 补回 16 篇）→ arXiv 编号。一次元数据刷新后库内 DOI 覆盖率约从 46% 升到 66%
  - **作者**：PDF `/Author` 不再无条件信任——形如单个「姓, 名」且首页出现 `et al` 或 `&`、或文件名带多作者信号时，判为排版/制作信息并丢弃，再走文件名回退。作者字段错误会安静地破坏"引文 → 库内匹配"（匹配规则含第一作者 + 年份）
  - **标题**：明显是制作产物的（如 `*.indd`）直接拒绝；级联为 PDF 元数据标题 → 首页最大字号标题 → 首页首个标题 → 文件名
  - **`journal` 不由本通道填充**：`extract_meta()` 里 `journal` 恒为 `None`，只有 Zotero 迁移（`cmd_zotero` 读 `publicationTitle` / `journalAbbreviation`）会写入。因此 `kb_ingest` 建起来的库该列为 `NULL`，`filters.journal` 必然零命中——工具描述与 README 都已写明这一点；补全方案与 DOI 反查（§2.6）合并评估，见 `docs/BACKLOG.md` §2.8
- **为什么要有元数据刷新**：增量入库按 sha256 跳过未变文件，解析器的改进不会自动作用于老库（实测一次抽取改动会漏掉数十篇的 DOI，直到一次全量重灌才暴露）。因此每行记录写入它的解析器版本 `docs.indexed_with`，`kb_stats` 据此报 `stale_docs` / `stale_sample`，宿主提示用户选择刷新元数据或全量重灌

## 5. 检索流水线（search/rag 命令）

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
- 语言提示：查询含 CJK 且库内中文占比 < 10% 时，响应附加 `lang_note`（说明 BM25 关键词路基本空转、命中主要由向量侧跨语言匹配决定）。**引擎不改写、不翻译查询**：归一化由调用方模型负责，工具描述要求查询写成 3–12 词的英文术语串「材料/体系 + 方法/工艺 + 性质/表征」，限定条件（年份/期刊/作者）放进 `filters`，需要中文文献时再用原话另发一条
- 预过滤字段：authors/title/journal/kind/section/year 直接映射到 `docs` 对应列。注意 **`journal` 只由 Zotero 迁移填充**（见 §4），`kb_ingest` 入库的库里该列为 `NULL`，`filters.journal` 会零命中；限定来源请用 authors/year/title/section

## 6. 嵌入与精排模型

- 嵌入：`BAAI/bge-small-zh-v1.5`（SentenceTransformer，normalize），中文查询加检索指令前缀
- 精排：`BAAI/bge-reranker-base`（CrossEncoder）；不可用时回退 `bge-large-en-v1.5` 双塔余弦
- 加载：本地缓存优先（local_files_only），缺失才尝试下载（HF_ENDPOINT 镜像支持，下载限时）；直连失败自动改写 endpoint 并经 `https://hf-mirror.com` 重试一次（`_apply_hf_mirror()`）
- 增量编码：只编码新分块（vecs 表差集）

## 7. Zotero 迁移

- 定位：`~/Zotero/zotero.sqlite`、`~/Documents/Zotero/...`、`%APPDATA%/Zotero/Profiles/*/zotero/zotero.sqlite`，或 `zotero_db` 显式指定
- 附件路径：`storage:` 前缀 → `<dataDir>/storage/<itemKey>/<文件名>`（不依赖 linkMode）
- 元数据：itemData（title/date/publicationTitle/DOI）+ itemCreators（author 角色）→ 覆盖 PDF 抽取值；同时落 `zotero_key`，检索结果渲染 `zotero://open-pdf/library/items/{key}` 可直开 Zotero 阅读器
- 缺失文件标记 missing 跳过；dry_run 只列候选

## 8. 安装与分发（v1.6.6 一键安装）

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

## 9. 插件架构（DSH 双端）

两个半边（npm 静态包 `lib/index.js`、动态插件 `plugin/host.js`）**功能对齐**：同一套会话级状态、同一套提示规则（描述层/结果层由 `lib/guidance.js` 生成，动态半边用镜像块内嵌并由 `tools/sync-host-guidance.mjs --check` 防漂移）、10 个工具定义逐字段一致，`tests/suites/s_host_half.mjs` 对此做回归。

### Host 半（plugin/host.js、npm-package/lib/index.js）

- **守护进程**：`kb_engine.py serve`，JSON 行协议（`{id,command,payload}` → `{id,ok,response|error}`），stdin pipe + stdout collect(offset 读取)，串行请求队列，崩溃自愈，工作区切换自动重启，插件停止 terminate
- **工具**：10 个（`kb_ingest` / `kb_status` / `kb_zotero` / `kb_search` / `kb_rag` / `kb_scope` / `kb_dedup` / `kb_clear` / `kb_stats` / `kb_fetch`，见 README），长任务（ingest/zotero）超时 30min，支持 exec.signal 取消
- **后台任务与陈旧数据**：`kb_status` 轮询后台入库任务（running 返回进度，done 返回 totals 与最近文件）；`kb_stats` 返回 `stale_docs > 0` 时，插件在该会话首次检索时询问一次处理方式（暂不处理 / 仅刷新元数据 / 全量重灌）
- **会话级状态**：`scope / depth / strict / enabled / diligence` 按会话隔离（会话键取 `exec.agent.id`），首次检索经 `userQuestions.ask` 询问范围/深度（120s 竞速，默认 kb / deep）；工作区默认值存 `<工作区>/.kb-rag/state.json`（引擎 `state` 命令代读写），只有 `/kb save` 或 `kb_scope(save=true)` 才落盘
- **`/kb` 命令**（direct UI handler，不进模型）：`status` / `kb|both|web` / `quick|deep` / `strict on|off` / `thorough|normal` / `off [soft|hard|search]` / `on` / `save` / `policy`；三档关闭 = 软关闭（工具在、调用即返回 `kb_rag_disabled`）/ 硬关闭（运行时撤销注册）/ 半关闭（只撤 `kb_search`+`kb_rag`）
- **输出渲染**：来源列表 Markdown（DOI 链接内联、`§章节 · p.N` 页码锚点、`[Ref n]` 引文行与「关联文献」区块；无 DOI 显示文件名）；结果层提示（无命中 / 弱相关 / 向量降级 / 已关闭 / 深挖补库指引）由 `withNotes` 追加，`undefined` 字段在动态半边返回前剔除（沙箱要求 lossless JSON）
- **结构化元数据**：`output.presentationMeta` 投影 `sources / verdict / closest` 供客户端卡片使用；`presentCall` 给调用卡片一个标题

### Client 半（plugin/client.js、npm-package/lib/client.js）

- 注册 `tool.call.toolview`（key=kb_rag/kb_search）来源卡片，优先读宿主的 `presentationMeta`（结构化来源、无命中理由与"最接近的几篇"、弱相关提示），没有就退回解析结果文本里的 markdown 链接；另注册会话栏指示条（`conversation.session.header.actions`）
- 两个半边的实现一致，差别只在模块形态：npm 侧是 lazy-CJS bundle（`window.__ModuleLoader__.load`），动态侧是函数体（`React` 由沙箱作为闭包符号注入，`inject: ['slots']`）
- 不渲染卡片视图的界面自动降级为 Host 输出的 markdown 文本（核心可点击来源始终由宿主渲染）

## 10. 引擎进程协议（kb_engine.py）

- 单发：`echo '<json>' | python kb_engine.py <ingest|ingest_async|status|search|rag|stats|zotero|dedup|clear|fetch>`；`ingest` 的 payload 可带 `metadata_only` / `rebuild` / `async_if_large`（见第 4 节），`status` 的 payload 为 `job_id`
- 常驻：`python kb_engine.py serve`（逐行 JSON，stdout ensure_ascii 单行 flush）
- 异步任务：`ingest_async`（payload 的 `command` 字段分发 ingest/zotero）fork 独立子进程后立即返回 12 位十六进制 `job_id`（`status=running`），进度与结果写在 `<kb_root>/.kb-jobs/`；`status` 轮询返回 `running`/`done`/`error`/`not_found`，progress/result 以临时文件 + `os.replace` 原子落盘（不会读到半截 JSON），done 后清理 job/progress 中间文件；`run_job <job.json>` 是子进程内部入口，不由宿主直接调用
- `fetch`：按 arXiv → 出版商正式版（落地页 `citation_pdf_url`，校园网/机构订阅可直接取得订阅版）→ 落地页 pdf 链接 → Unpaywall（`UNPAYWALL_EMAIL`）→ Crossref 的顺序取 PDF，只做常规抓取、不绕过付费墙
- 响应统一 `{ok:bool, ...fields, engine:ver, ms:int}`；单文件错误不中断批量

## 11. 实测性能（2026-xx，Windows/CPU）

| 场景 | 指标 |
|---|---|
| 首次全量入库 | 242 文件/1.8GB → 85.9s（~355ms/篇，含向量） |
| 增量重跑 | 2.17s（40×） |
| 热检索（20k 块，deep） | 0.4–1.3s（混合+精排）；`quick` 无精排直出，亚秒级 |
| 清空 | 177 文档/25k 块 → 278ms（含 VACUUM） |
