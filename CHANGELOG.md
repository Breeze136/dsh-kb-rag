# Changelog

## [1.6.2] - MCP 超时加固 + 健壮性修复

- **MCP 大批量入库自动转后台（Kimi Work 60s 超时解药落地）**：`kb_ingest` 先轻量估算待处理文件数（目录递归/文件列表），超过 `KB_ASYNC_THRESHOLD`（默认 25）自动改用 async_mode，立即返回 `job_id` + `kb_status` 轮询指引——agent 无需知道 async_mode 存在，传整个文献库文件夹也不会超时；显式 `async_mode=true/false` 可强制
- **`kb_zotero` 支持 async_mode=true**：async 任务分发泛化（job 带 command 字段，`run_async_job` 按命令分发 ingest/zotero），整库迁移可后台执行 + kb_status 轮询
- **异步入库期间并发读不再锁死（高）**：`cmd_ingest` 由整批单事务改为**逐文件 commit**（写锁窗口从"整批"缩到"单文件+嵌入"）；`_migrate` 孤儿向量清理改 500ms 短超时探测、撞锁即跳过（读命令的 connect 不再干等 5s 或抛 database is locked）
- **迁移健壮性**：`_migrate` 的 ALTER 只吞 "duplicate column"，锁冲突等其他 OperationalError 上抛（避免"版本号置新但列缺失"的静默不一致）
- **后台任务卫生**：`cmd_status` 校验 job_id 为 12 位十六进制（阻断目录穿越）；done 后自动清理 job/progress 残留（result 保留可重复读）；超 1h 无进展提示可能卡死；`kb_clear` 一并清空 `.kb-jobs`
- **启动即失败可感知**：`cmd_ingest_async` spawn 后短窗口 poll，子进程启动即退出时立即报错并清理，不再留"永久 running"的幽灵任务
- **原子写**：progress/result 改为临时文件 + rename，轮询不会读到半截 JSON
- **渲染修正**：DSH 宿主（`plugin/host.js` 与 `npm lib`）改用 `files_total` 显示真实文件数（引擎只回最近 20 条后不再误报"共 20 个文件"）；`kb_zotero` dry_run 返回完整候选清单（预览语义，不截断）；MCP `render_status` 展示 error 详情、`result.ok=false` 如实呈现失败而非假"完成"
- 引擎同步进 npm-package 副本

## [1.6.1] - MCP 异步入库 + PDF 页码锚点（schema v3）

- **异步入库（MCP 60s 超时解药）**：`kb_ingest` 支持 `async_mode=true`，fork 独立子进程跑 ingest 并立即返回 `job_id`；新增 `kb_status(job_id)` 轮询进度（`.kb-jobs/` 目录，与 kb.sqlite 同级），宿主超时不影响后台任务
- **PDF 页码锚点（schema v2→v3）**：`chunks` 表新增 `page_start/page_end`（PDF 物理页码，1 基）；`read_document` 建立段落→页码映射（`meta['_paras']`）；检索结果带 `page` 字段，渲染优先 `§章节 · p.N`，可配合 Zotero `?page=N` 一键跳页；段落号降级为辅助（两栏 PDF 段落合并时段号不可靠）；txt/md/docx 与旧数据无页码（NULL）自动降级
- **响应体积压缩**：ingest/zotero 的 `files` 只回最近 20 条 + 新增 `files_total` 真实总数（针对 Kimi Work 等宿主的体积限制）
- **渲染增强**：页码优先定位；证据引文关联前 5 条（"↳ 引文补充"，供补库/深读）
- **解释器修复**：MCP 服务默认用 `sys.executable`（拉起服务的 Python）替代裸 `python`，避免命中错误解释器；`KB_RAG_PYTHON` 仍可覆盖
- **库迁移**：`_migrate()` 自动 v2→v3 ALTER 加页码列；旧数据页码为 NULL，`force` 重入库后恢复（详见 `docs/MIGRATION.md`）
- 文档：`docs/OUTPUT-FORMAT.md` 页码版示例与 `docs/MIGRATION.md` v3 迁移行同步更新

## [1.6.0] - 版本化迁移 + 元数据质量修复 + 段落定位与引文关联

- **库结构版本化迁移**：`PRAGMA user_version` 门控替代临时 ALTER（详见 `docs/MIGRATION.md`）；首次建库一次建全表+索引并写版本号；旧库 v0→v1 自动补齐表/列并回填 `zotero_key`；**v1→v2 新增 `chunks.para_start/para_end`（段落定位，旧行 NULL）**；迁移显式 `commit()`
- **段落定位（隐式元数据）**：`chunk_document`/`fallback_chunks` 记录全局段落号；检索结果带 `para` 字段，默认不渲染，供"这句在文献第几段"追问与点开文献定位（详见 `docs/OUTPUT-FORMAT.md`）
- **References 保留 + 引文关联**：References（weight 0）入库供引文关联（检索按 `weight>0` 排除）；References 检测两级（行首标题 / 文末连续序号段，支持 `n.` `[n]` `nAuthor` 风格）；正文 `[n]` 引用 → 该文献引文条目，检索结果带 `citations` 字段（实测：结构化论文可解析；无标题栏排 PDF 部分解析，见 OUTPUT-FORMAT §6）
- **迁移与健康提示**：连接时迁移日志走 stderr；`kb_stats` 返回 `schema_version` / `migration` / `health`
- **元数据修复**：纯中文标题支持（≥4 汉字）；年份级联（文件名 → ©/Copyright/Vol → 括号 → 裸年份 → creationDate 兜底 + <1990 修正）；文件名命名习惯解析（作者-年份-标题 / Z-Library / (作者1,作者2) / 中文 作者-标题）；短标题偏好 + 封面重复去重
- **发布包隐私**：移除 npm-package 与 tools/ 中的本地绝对路径与硬编码库路径；Unpaywall 邮箱改 `UNPAYWALL_EMAIL` 可配置
- **README 安装命令补全**：明确 `npx` 必须带 `--package dsh-kb-rag`（裸命令 E404）、`npm install` 在 profile 目录执行、新增 Troubleshooting 表
- 可选 `KB_SQLITE_WAL=1` 开启 WAL（默认关闭）

## [1.5.0] - Zotero 集成 + 文件路径显示

- **Zotero 直接打开 PDF**：`kb_zotero` 迁移时存储 Zotero itemKey，搜索结果渲染 `zotero://open-pdf/library/items/{key}` 链接，点击直接跳 Zotero 阅读器
- **文件路径显示**：所有搜索结果底行展示完整文件路径，方便复制后在文件管理器或引用管理器中打开
- 引擎：docs 表新增 `zotero_key` 列（含存量 DB 自动迁移），搜索/关联文献结果带回 `zotero_key` 字段

## [1.4.0] - kb_fetch 下载增强

- **kb_fetch 首选 Node 下载器**（随包分发 `scripts/doi_pdf.mjs`）：Node fetch 的 TLS 指纹更接近浏览器，手动重定向 + 全程 cookie jar 绕过 Nature `cookies_not_supported`；候选源比 Python 版多（Unpaywall / Crossref PDF link / `citation_pdf_url` meta / 页面 pdf 链接模式），Node 不可用或漏项时回退 Python urllib 路径
- **下载顺序改为「出版商正式版优先，OA 兜底」**：先落地页 `citation_pdf_url`（校园网/机构 IP 直接下订阅版 PDF，实测 Nature Materials 付费墙期刊成功），再 Unpaywall/Crossref OA
- **arXiv 直连补全**：裸 ID / `arXiv:ID` / `10.48550/arXiv.ID` / abs URL 四种形式均直达 arxiv.org（原 doi_pdf.mjs 无 arXiv 分支）
- **反爬识别**：Cloudflare "Just a moment" 与 Akamai `bm-verify` 挑战页明确报"需真实浏览器手动下载后入库"（Wiley / science.org / cambridge.org / MDPI 实测 403）；MDPI 令牌跟随尝试保留（部分站点可过）
- **快速失败**：`_download_bytes` 对 `text/html`（付费墙页）立即失败回退，不再整页下载后再判魔数
- **动态插件清单同步**：`plugin/kbrag.plugin.json` 版本号 1.0.0 → 1.4.0，`engine.commands` 补 `fetch`，`tools` 补 `kb_fetch`（此前清单长期未随发版更新）
- 实测（2026-09，校园网）：Nature Comms / Sci Reports / arXiv / Nature Materials(订阅) 均经 citation_pdf_url 或直连成功；Wiley/Science/Cambridge/MDPI 为 JS 反爬，需浏览器手动下载

## [1.3.1] - 安装器体验 + 隐私修正

- **检测环境避免重复下载**：安装器先探测 embed/rerank 模型是否已在 HF 缓存，已缓存则打印"已缓存，跳过下载"；未缓存且未加 `--models` 时提示"首次检索自动下载"并给出镜像指引
- **人类可读提醒**：模型下载前明示体积（embed ~95MB / rerank ~1.1GB）、Ctrl+C 可跳过、HF 镜像地址；未设 `HF_ENDPOINT` 时主动提醒国内镜像
- **隐私修正**：安装脚本与文档中的示例从「研究者领域专属示例」改为中性的「石墨烯化学气相沉积合成」，移除作者研究领域信息
- **编码修复**：install.ps1 恢复 UTF-8 BOM（Windows PowerShell 5.1 无 BOM 会把中文当 GBK 读导致脚本语法报错）
- 文档：安装命令用 `web` 实值 profile（可直接复制，`dsh web` 启动即 `web`）

## [1.3.0] - 一键安装补全

- **npm bin 入口 `dsh-kb-rag-install`**：新增 `install.mjs`（37 行薄分发器，`"bin": {"dsh-kb-rag-install": "./install.mjs"}`），一行装环境：`npx --yes --package dsh-kb-rag -c "dsh-kb-rag-install --profile <name>"`；按平台转发到 `scripts/install.ps1|sh`，Windows 自动把 bash 风格参数翻译成 PowerShell 风格（`--profile`→`-Profile`），用户全程只用一种参数写法；`engines: node>=18`
- **一键安装脚本**：新增 `scripts/install.ps1`（Windows）与 `scripts/install.sh`（macOS/Linux/Git Bash）+ `install.cmd` 双击入口，一条链完成：Python ≥3.9 定位 → pip 依赖安装（`--mirror` 镜像、`--user` 回退、`--with-docx` 可选）→ 引擎 stats 冒烟测试 → Node/pnpm 检查（缺 pnpm 自动 `npm i -g`）→ `dsh plugin --profile <name> add dsh-kb-rag` 安装并激活 → 可选 `--models` 预下载模型（尊重 `HF_ENDPOINT`/`KB_EMBED_MODEL`/`KB_RERANK_MODEL`）；`--dry-run` 全流程演练，幂等可重跑
- **KB_AUTO_PIP=1 可选自动装依赖**：插件启动探测到缺失时默认仍只打印命令（安全默认不变）；设 `KB_AUTO_PIP=1` 后自动执行 `python -m pip install`（固定 argv，不进 shell，尊重 `PIP_INDEX_URL`），装完二次探测确认
- **修复依赖探测缺陷**：裸 `import` 链在首个缺失模块即中断（最多报 1 个）；改用 `importlib.util.find_spec` 一次性给出**完整缺失清单**
- **可操作的工具错误**：依赖缺失且未自动安装时，工具调用直接返回中文修复指引（手动 pip / KB_AUTO_PIP / 安装脚本三条路径），不再让引擎子进程崩出裸 ImportError；首次工具调用先等探测/自动安装结束（一次性门控，后续零开销）
- npm 包随包分发安装入口与脚本（`files` 清单含 `install.mjs` 与 `scripts/install.ps1|sh`），手动 `npm install` 用户可从 `node_modules/dsh-kb-rag/` 一键补环境
- SECURITY.md 更新：spawn 点清单 2→3（新增可选 pip 安装点）、网络节补 KB_AUTO_PIP/PIP_INDEX_URL/安装脚本行为、bin 为显式调用不随安装执行；`package.json` 仍声明零 lifecycle install scripts
- 文档：QUICKSTART 以一键安装为第 0 节首选路径；README（Quick Start / Option 1 / 配置表 KB_AUTO_PIP / 目录结构）与 npm-package README 同步

## [1.2.0] - 标识符阶梯 + 元数据增强 + 图注坐标

- **标识符阶梯**：首页限定 DOI（References 之前截断，避免抓参考文献的 DOI）+ arXiv ID 归一化为可解析 DOI（`10.48550/arXiv.xxxx`）+ 最大字号行提取真实标题
- 元数据本地增强（纯离线）：清理 Word/PowerPoint 前缀、arXiv 头、投稿模板串、文件名式占位标题；回退首页标题启发式；作者占位符清理；年份合理性校验
- 图注坐标关联（保守精确匹配）：命中正文段引用 `Fig. N`（仅同文档、编号完全一致）时附带"↳ 图注坐标: Fig. N — 图注原文"，匹配不到不猜
- 无 DOI 命中附"搜索串"（标题+第一作者+年份，可复制到 Scholar 精确定位）
- 不做：Crossref 联网回填（伤"零上传"承诺 + 错配 DOI 风险）、OCR

## [1.1.0] - 关联文献

- kb_search / kb_rag 新增 related 关联文献列表（同作者/同期刊/年份相近/主题相似，基于元数据 + 文档向量质心余弦，默认开启，可用 related=false 关闭）
- 检索渲染新增"关联文献（可作补充建议）"区块；kb_rag 的补充建议优先引用 related 列表
- 文档质心缓存随入库/去重/清空/Zotero 变更自动失效

## [1.0.7] - 仓库更名 dsh-kb-rag

- GitHub 仓库 Breeze136/kb-rag → Breeze136/dsh-kb-rag（搜索"dsh-kb-rag"时精确匹配同名仓库、提升发现性；旧链接 301 重定向）
- 更新全部内部引用（README/SECURITY/package.json repository 字段）
- 同步更新两个 awesome 列表条目链接

## [1.0.6] - 安装指引现代化 + dsh.so 徽章

- README 安装说明改为以 `dsh plugin --profile <name> add dsh-kb-rag` 一键流程为首选（pnpm 要求注明），补充插件市场（dsh-plugin-registry）与手动三种路径
- 增加 dsh.so 安全徽章（扫描状态 passed）；仓库已被 dsh.so 注册表收录（artifact: kb-rag）

## [1.0.5] - 安全文档入包 + 移除遗留 shell 调用

- 删除 plugin/host.js 中遗留的 `cmd /c start` 打开文件 RPC（唯一变量路径进 shell 的点）
- 新增 SECURITY.md（执行模型/spawn 清单/读写边界/模型下载说明）并随 npm 包分发
- npm-package README 增加 Security 一节

## [1.0.4] - 声明 dsh.bundle，一键安装即激活

- package.json 增加 `dsh.bundle.patch` 声明并随包分发 `cordis.patch.yml`（插入 `kb-rag` 行）
- 用户现在只需 `dsh plugin --profile <name> add dsh-kb-rag` 即可安装并自动激活为 profile layer（无需手改 cordis.patch.yml）
- 增加 `exports` 入口（`./cordis.patch.yml`、`./package.json`）

## [1.0.3] - README 全英文化

- 仓库 README.md 与 npm-package/README.md 全部译为英文（代码与功能不变）

## [1.0.2] - npm 文档补丁

- README 增加 npm 版本/下载量、GitHub release、MIT 徽章
- 新增"设计原则"一节：刻意零 UI（无管理面板/前端状态/客户端依赖，一切经由对话与工具返回完成，检索结果内置 DOI 链接渲染）、垂直学术文献、留在甜区
- 同步更新 awesome 列表两处 PR 的定位描述

## [1.0.1] - npm 静态包补丁

- 静态包启动时自动检测 Python 依赖（pymupdf/faiss-cpu/sentence-transformers/torch），缺失时在宿主日志打印对应 `pip install` 命令（不阻塞加载）
- npm-package/README 增加"其他 Harness 用户安装指引"（部署目录 npm install + cordis 组合加载两步）
- 仓库 README 增加 npm 静态包一节与目录结构更新

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
