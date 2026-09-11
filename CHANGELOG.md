# Changelog

## [1.6.6] - 元数据刷新通道 + 陈旧数据检测 + 入库进度可见 + 检索语言归 AI 层

### 新增：元数据刷新通道（`kb_ingest`）
- **`metadata_only=true`**：只重跑解析与元数据抽取并 `UPDATE docs`（标题/作者/年份/期刊/DOI），**不重切块、不重嵌入**。实测 312 篇 **28–30 秒（约 90 ms/篇）**。存在的理由：增量入库按 sha256 跳过未变文件，所以引擎的解析改进**不会自动作用于老库**——实测一处抽取改动漏掉了 49 篇的 DOI，直到一次全量重灌（322 秒）才暴露
- **`rebuild=true`**：按库内现有路径**原地重灌全部文档**（引擎自己从库里取路径）。避免由调用方传目录：`force` 会绕过去重检测（`if dup is not None and not force`），传目录会把内容重复的文件当新文献重复入库（实测会多插 31 个重复文件 + 一个 Office 临时锁文件）
- 元数据刷新走**只读首页**的快通道（新增 `read_first_page()`）：标识符/年份/标题的判据全部落在 `page1` 与 PDF 元数据上，因此结果与全量解析一致、成本降到约 1/10（首页无文本层或非 PDF 时自动回退全量解析）

### 新增：DOI 与元数据的抽取改进（`PARSER_REV` 2 → 3）
- **DOI 增加 XMP 来源**：部分出版商 PDF 正文里根本不印 DOI，只写在 XMP 元数据里（实测 Science Advances / RSC / Nature 系）；**16 篇**因此恢复，全库 DOI 覆盖率 **46% → 66%**（191 → 207 篇）
- **不再盲信 PDF 生产元数据**：`/Author` 形如单个「姓, 名」（排版/制作人员）且首页或文件名给出多作者信号时弃用、改走文件名回退；标题里的生产残片（如 `*.indd`）一律拒绝。实测某篇的 `title='nmat1805 Ramesh Review.indd'`、`authors='Simpson, Derna'` 被修正为真实标题与作者，并因此**恢复了「引文 → 库内匹配」**（`[库内]` 标记此前静默失效）

### 新增：陈旧数据检测（schema v3 → v4）与升级提示
- 每条 `docs` 记录写入 `indexed_with`（形如 `<引擎版本>/rev<N>`）；`PARSER_REV` **只在改动会写进库的解析逻辑时 +1**——判定只看 rev、不看引擎版本号，否则每次发版都会把整库标成陈旧，提示变成噪音
- `kb_stats` 新增 `stale_docs` / `stale_sample` / `parser_rev` / `indexed_with`
- DSH 插件在**会话内首次检索时**询问一次（仅当 `stale_docs > 0`）：暂不处理 / **只刷新元数据（秒级）** / 全量重灌（自动转后台）

### 新增：入库进度可见（第 10 个工具 `kb_status`）
- DSH 侧此前是同步调用，30 分钟时限内**没有任何进度输出**；现在**由引擎自己**判断批量大小并转后台（`async_if_large`，阈值 `KB_ASYNC_THRESHOLD` 默认 25），立即返回 `job_id`，用新增的 `kb_status` 轮询：`running` 给出已处理/错误/分块数，`done` 给出 totals 与最近文件，`error`/`not_found` 如实说明
- 计数在引擎侧完成（不在 JS 宿主里引入文件系统访问）；后台任务的 `progress_path` 保证不会二次 fork
- 工具数 **9 → 10**；DSH 与 MCP 两侧现在都有 `kb_status`（此前只有 MCP 有，文档里"用 kb_status 替代 kb_scope"的表述一并订正）

### 改进：检索语言归 AI 层
- 工具描述与 README 明确：**query 用英文术语串**（3–12 词，结构「材料/体系 + 方法/工艺 + 性质/表征」），年份/期刊/作者放 `filters`，需要中文文献时用原话再发一条；**引擎按原样检索、不翻译**
- 引擎新增零成本检测：query 含 CJK 且库内中文占比 < 10% 时，响应附 `lang_note` 说明（BM25 关键词路空转、命中主要由跨语言向量匹配决定），**不改写查询**；渲染层显示该提示

### 实测
- 全库 312 篇：`force` 全量重灌 **322 s**（`updated=312 / errors=0 / duplicates=0`）；`metadata_only` 刷新 **28–30 s**；DOI **46% → 66%**；页码锚点恢复 **99.7%**；`stale_docs` 归零
- 新增功能的回归验证见 `docs/BACKLOG.md` §3

### 真实文档测试与审计修正（发布前抓出并修掉）
- **同一论文的多份 PDF 会各占一个结果位**：两份 PDF 内容不同 → sha256 去重不合并，deep `top_k=3` 里同一篇综述出现两次（用户实际只拿到 2 篇）。检索层现按**归一化 DOI**（大小写不敏感）折叠，保留最高分那条并**从后续候选补齐 Top-K**，返回 `dup_collapsed`，界面提示"已折叠 N 份同论文副本"
- **查询缓存 key 现包含引擎/解析器版本**：否则升级后旧缓存继续吐旧行为（实测：改了结果折叠规则后，缓存仍在返回没有新字段的旧响应）
- **元数据刷新不再抹掉 Zotero 写入的字段**：`metadata_only` 曾用首页抽取结果无条件覆盖 `docs` 行，而 Zotero 迁移写入的 `doi`/`journal` 不在 PDF 首页里（实测 DOI 211→209）。现改为**绝不用空值覆盖非空值**，同时保留"好值替换脏值"（如 `.indd` 标题）
- **作者 junk 过滤扩充**（`PARSER_REV` → 4）：真实库命中 `user`、`Administrator`、`aipuser` 等出版商/系统账号，现按名单 + `*user` 账号模式 + 软件名拒绝，回退文件名解析
- **`kb_ingest(rebuild=true)` 可省略 `paths`**：此前 schema 把 `paths` 标为必填，而 rebuild 的路径由引擎从库内取，调用方必然撞 `missing required property paths`；`kb_scope` 同理改为可只查看（此前 `scope` 必填，"查看"调不通）
- **渲染修正**：失败条目显示原因（`✗ 1.pdf · ValueError: no text extracted`，此前只有文件名）；`metadata_only` 结果单独渲染（此前显示成"入库完成 新增 0…"）；`kb_zotero(dry_run)` 渲染为"Zotero 预演 · 候选 N 篇"（此前同样显示成"入库完成 0/0/0"）；检索头部按实际 `mode_used` 显示（此前 `mode=keyword/vector` 都写"混合检索"）；后台转交提示不再重复两遍；异步 `pending_files` 报真实篇数（此前是"阈值+1"的提前退出值，如 100 篇显示 26）


## [1.6.5] - 并发写锁容错 + zotero 逐文件提交 + 引文链后章节还原

### 并发与后台任务（补上 1.6.2 未覆盖的两处）
- **检索缓存写不再让整次检索失败**：`_search_core` 结尾的 `INSERT OR REPLACE INTO cache` 此前没有锁容错——异步入库子进程持有写锁时，已经算完的检索会以 `database is locked` 整次报错（DSH/MCP 侧表现为工具调用失败）。现按"缓存只是加速"处理：只吞 locked/busy，其他 `OperationalError` 照旧上抛
- **`kb_zotero` 改为逐文件 commit + 进度回写**：原实现整批单事务、且从不写 progress——异步整库迁移既看不到进度，任务被杀还会把已入库文件**全部回滚**（与 `cmd_ingest` 语义不一致），写锁窗口也覆盖整批。现与 `cmd_ingest` 对齐：每篇 `_ingest_file` 后 `db.commit()` + `_prog()`
- **异步启动校验真正生效**：原实现 `Popen` 后**立刻** `poll()`，而 python 即使只是打开不存在的脚本也要数十毫秒才退出，最可能的启动失败（脚本路径/解释器错误）抓不到、留下长期 running 的任务。现改为 `wait(timeout=0.5)`，并在 Popen 前校验引擎脚本存在，失败即清掉 job 文件
- 订正 1.6.2 的两处过宽表述（见该节内注）

### 章节标注
- **引文链结束后还原被打断的章节**：原实现一律重置为 `Front matter`（权重 1.0）。Nature 式论文的正文 refs 与 Methods refs 是**两段离散链**，链后正文因此被标错章节——`filters.section` 精确过滤（如 Methods）会漏召回、hybrid 排序权重偏低、结果里的 §标签也是错的。现进入 References 时暂存 `(section, weight)`，链结束还原（实测：链后段落由 `Front matter/1.0` 变为 `Methods/1.2`）
- **图注块之后的正文同样还原章节**（同类缺陷）：`Fig./Table` 图注原实现处理完后把章节重置为 `Front matter`，Results 中插图之后的所有段落都丢章节与权重；现图注仍独立成 `Figure/Table` 块，但其后的正文回到图注前的章节

### 卫生
- `kb_clear` 清理 `.kb-jobs/` 时连原子写的 `*.json.tmp` 一起清（原先 glob `*.json` 不匹配，进程在 rename 前被杀就会留下清不掉的残留）
- `_apply_ref_spans` 在无页信息时返回 `None` 而非 `[]`，保持"无页 = None"语义（当前调用方都有 `len()` 守卫，属预防性修正）

### 实测
- 新增 16 项回归验证并全部通过：缓存撞锁时检索仍返回结果、无竞争时缓存照常命中、zotero 中途中断后**首篇已落盘且进度已回写 processed=1**、引文链后章节还原为 Methods/1.2、图注独立成块且其后正文还原、子进程立即退出被识别且不留 job、引擎脚本缺失时直接失败、`kb_clear` 连 `.tmp` 一起清

### 文档
- 新增 [`docs/BACKLOG.md`](docs/BACKLOG.md)：本版修复清单、**尚未修复**的问题（后台任务无自动失败终态、页码近似、引擎退出行为待验证、`run_job` 白名单待确认、未审计区域）、回归验证方法与记录约定；README 文档索引已挂链

## [1.6.4] - kb_fetch 描述订正、文档与元数据同步

- **文档与元数据同步到 1.6.3**：`plugin/kbrag.plugin.json` 描述补全新能力（混合检索 + 交叉编码器精排 + 章节/页码级出处 + 快速/深度双模式）；`plugin/host.js`、`plugin/client.js` 头部注释与工具注册日志的版本号 `v1.0.0` → `v1.6.3`；`npm-package/package.json` 的 description 与 keywords 同步（补 `dsh-plugin`、`mcp`）
- **异步作业归属订正**：异步入库（`ingest_async` / `status` / `.kb-jobs/`）**只走 MCP 侧**（`KB_ASYNC_THRESHOLD` 自动转后台 + `kb_status` 轮询）；DSH 插件是同步长任务（`plugin/host.js` 时限 30 分钟，无 job/status 处理）。此前 README 架构图、npm 页与 QUICKSTART 把异步说成通用行为，已按实现订正
- **npm 页（`npm-package/README.md`）补齐 1.6.x 能力**：新增「Engine capabilities」小节（章节感知分块、混合检索、精排、出处、引文关联、快速/深度、增量去重、查询缓存、常驻守护进程 + 异步入库）；工具表补异步作业、出处（含 PDF 页码）与检索深度说明；示例改为石墨烯主题；文档统一英文（中文见 `README_CN.md`）
- **过时文档订正**：`SECURITY.md` 与 `npm-package/SECURITY.md` 工具数 8 → 9；`docs/MIGRATION.md` 当前 schema 由 1.5.0 更新为 1.6.3 / `user_version = 3`（补 `chunks.para_start/para_end`、`page_start/page_end` 两列与 v1/v2/v3 三个迁移块），变更记录拆出 1.6.1 的 v3 行；`QUICKSTART.md` 补检索深度、引文补充与同步/异步入库边界说明；`UNINSTALL.md` 补 `.kb-jobs/` 说明并统一路径占位符；`docs/OUTPUT-FORMAT.md` 第 6 节的"待落地"表述改为已实施状态；`mcp-server/README.md`、`docs/DESIGN.md` 同步到 1.6.3（schema v3 / 检索链路 / 9 工具 / 异步作业 / depth 双模式 / `UNPAYWALL_EMAIL`）

- **`kb_fetch` 行为描述订正**：实现一直是"出版商正式版优先"（先解析落地页 `citation_pdf_url`，**校园网/机构订阅网络下可直接取得订阅版 PDF**，无权限再回退 Unpaywall/Crossref 的开放获取），但工具描述却写成"只下载 OA 文献，不碰付费墙"，与实际行为不符。现统一为完整顺序说明，并保留合规边界：只做常规抓取，不绕过付费墙、不访问 Sci-Hub、不伪造凭据。同步 `plugin/host.js`、`npm-package/lib/index.js`、`mcp-server/server.py` 三处工具描述与 README（中英）、npm/mcp 文档、QUICKSTART
- **`doi_pdf.mjs` 尊重 `UNPAYWALL_EMAIL`**：Node 下载器的 Unpaywall 请求此前硬编码示例邮箱（`researcher@university.edu`），配置项只对 Python 回退路径生效；现与引擎一致读取该环境变量
- **`doi_pdf.mjs` 头部注释订正**：原注释写"优先 OA，其次校园网订阅"，与实际实现顺序相反，已按实际顺序重写（arXiv → 出版商正式版 → 落地页 pdf 链接 → Unpaywall OA → Crossref）

## [1.6.3] - 引文关联深挖 + Nature 角标识别 + 快速/深度双模式 + 真·一键安装

### 引文关联（citation linking）
- **Nature 系上标角标识别**：PDF 文本层会把上标引用压平成 `graphene1,2`，引擎在 `read_document` 按**字体度量**检测（字号 ≤ 行内正文 80% + 基线抬高 ≥ 12%），转写为 `graphene[1,2]` 方括号形式入库；宁缺勿错：跳过作者行（≥3 个上标簇）、指数（锚点以数字结尾）、单位标记（`1*` 等非纯数字簇）
- **References 三级检测**（原两级）：③ 新增**递增条目链**——Nature 无标题 References（标题是图形，正文 refs 1–30 与 Methods refs 31–37 分两段离散出现）与 Science `1. Author` 行首风格；编号从 1 递增或续接上条链、过半条目带年份/et al 信号、链尾在 Acknowledgements/©/图注停止行截止，边界段落自动切分。阶段 ② 修复"文末连续段"被末页公式/坐标轴数字劫持（新增文献列表相似度门槛：过半条目带年份/et al）
- **引文条目锚点修复**：References 长块改按**行边界**切分（`split_refs`）——原 `split_long` 句边界拼接把 2、3 号条目挤到行中间压平 `N.` 行首锚点，长引文列表只能解析第 1 条；`_parse_references` 多风格同时命中时取**编号链最完整**的模式（避免年被拆行的噪声风格劫持）
- **实测**（11 篇各出版商 PDF）：Wiley 综述 0→399 条、Nature Letter 8→37、中文期刊 0→90、Science 0→29，全部只增不减
- **被引文献库内匹配（引文关联深挖）**：`_match_cite_lib` 三规则——① 引文文本中 DOI 精确命中；② 库内标题（归一化 ≥30 字符）整串出现；③ 首作者姓（≥6 字符）+ 括号年份双命中。命中条目带 `lib` 字段（title/authors/year/journal/doi/zotero_key），三层渲染输出「[库内]」标记行（DOI 链接 + 元数据 + 即本证据的 Ref n + Zotero 打开）
- **推荐输出格式（回答层三列制）**：`kb_rag` guidance 硬性要求答案末尾按来源分三列——①「库内可查（循引文找到）」必须带关系链《被引文献》被 [证据编号] 的引文 Ref n 引用；②「建议补库（循引文发现）」注明被 Ref n 引用尚不在库内；③「相关文献」（元数据相似）。不得混列，引文关联的必须带关系链
- **渲染优化**：引文编号 `[n]` → `[Ref n]`（与证据编号消歧）；库内命中合并为单行（标题链接 + 元数据 + 关系 + Zotero 内联）；命中条目（≤5）优先、库外仅展开 3 条 + 一行折叠汇总（Ref 区间压缩如 `5–8`）；`score` 仅精排后显示（RRF 融合分无绝对含义）；snippet 起止按词边界对齐
- 旧库需 `force` 重灌才有角标与全新 References 切分（入库时处理）

### 快速/深度双模式（depth）
- `kb_search` 默认 `quick`（快速检索：混合召回直出、跳过精排/引文扩展/相关文献，亚秒级响应）、`kb_rag` 默认 `deep`（深度检索：重排序 + 引文关联 + 相关文献全链路）——按入口定位自动分流，显式传参永远优先；实测 34ms vs 2.8s
- 会话级深度：`kb_scope` 新增 depth 参数；会话启动询问新增「检索深度」问题（快速检索 / 深度检索，默认推荐深度检索）；选择解析双向显式（快速检索→quick）
- 快速检索 guidance 反长思考：立即作答、一两句内直给、禁止背景铺垫/延伸分析/二次检索；渲染尾注提示 `depth=deep` 升级路径
- 深度检索 guidance：跨文献综合论述 + 三列推荐格式；快速检索渲染压缩（无引文链/相关文献、短 snippet），体积约 57%
- 术语统一（五文件）：快查→快速检索、详细→深度检索（会话询问/工具 description/guidance/渲染标签/尾注）
- MCP `server.py`：`kb_search`/`kb_rag` 签名改 Optional，未传参不再以默认值覆盖引擎模式化缺省；请求剔除 null 值；引擎 `_depth_flag` null 语义兜底

### 真·一键安装
- **新微包 `dsh-kb-rag-install`**（已发布 1.0.0）：裸 `npx dsh-kb-rag-install` 直接可用，根治"包名与 bin 名不一致导致裸命令 E404"的老坑；微包零逻辑（定位依赖转发），安装逻辑仍在主包维护；npm 平铺/嵌套双布局验证通过
- **profile 自动检测**：未指定 `-Profile` 时扫 `~/.dsh/profiles/`（含 cordis.yml/package.json 才算，排除 node_modules 误报）——唯一 profile 直接用；多个时列出（交互可选、非交互走默认目录）
- **模型预下载默认开**：`install.mjs` 默认注入 `--models`（配合直连失败自动切 hf-mirror.com 镜像重试，装完即全就绪），`--no-models` 可跳过（两平台脚本均支持）
- **非交互默认**：`install.mjs` 默认注入 `--yes`（npx 场景不再被 pnpm 确认卡住）；pnpm 全局安装失败自动回退 `corepack enable pnpm`
- **中文用户名安装修复**：Windows PowerShell 5.1 的 `$OutputEncoding` 默认 ASCII，管道送 python 的中文路径变 `?` 报 WinError 123——脚本顶部强制 UTF-8（无 BOM）管道编码
- **安装前内存提示**：装前读物理内存并按阈值提示（≥8GB 正常 / 4–8 偏紧 / <4 可能不足）
- 模型下载失败自动镜像重试（两平台）；Python 缺失提示补 winget/brew/apt
- 文档：QUICKSTART/README 换裸命令为推荐写法；npm README 排错表补 WinError 123 行

### 其他
- `docs/OUTPUT-FORMAT.md`：新增 §2 双模式章节、三列推荐模板、实施记录与局限全面修订（角标/References 三级检测/库内匹配），章节重编号
- 引擎 `kb_engine.py` 运行期 HF 镜像回退真正生效：`HF_ENDPOINT` 在 huggingface_hub import 时固化，同进程后置 `os.environ.setdefault` 是空操作——新增 `_apply_hf_mirror()` 直接 patch `constants.ENDPOINT` + 派生的 URL 模板，embedder/reranker 直连失败自动切镜像重试
- 兼容：`install.mjs` 旧写法 `npx --yes --package dsh-kb-rag -c "dsh-kb-rag-install"` 等价不变

### 文档重写与隐私清理
- **README 双语化重写**：`README.md`（英文）与 `README_CN.md`（中文）两份逐节对应，顶部互链。结构：定位段 → 输出示例 → Positioning（三条取舍 + 「适用范围与预期」callout）→ 三种形态 → 快速开始（`<details>` 折叠 Windows/受限网络/大批量）→ 升级 → 工具参考 → 架构 → 实测数据 → 文档表格 → 配置 → 仓库布局 → 已知限制 → 联系/相关项目；术语与边界表述保持技术文档语气
- **去除装饰性符号**：全仓库清理装饰性图标字符（README 与 docs 的表格图标列、状态标记等），渲染输出中的星形库内命中标记改为纯文本 `[库内]`——同步 `plugin/host.js`、`npm-package/lib/index.js`、`mcp-server/engine_client.py` 三处渲染器与 `kb_engine.py`/`server.py` 的 guidance 文本；`→ ↳ ✓ ✗` 等技术符号保留
- **升级路径修正**：DSH profile 是 pnpm 工作区（含 `pnpm-lock.yaml`，`dsh plugin` 内部即转发 pnpm），原文档让用户在 profile 里跑 `npm install dsh-kb-rag` 会与 pnpm 布局冲突。现统一为 `dsh plugin --profile <name> add dsh-kb-rag[@版本]`（或重跑安装器），`npm install` 仅标注为手动部署场景（npm README 的 Option 3 加醒目警告 + 新增 Upgrading 章节）
- **隐私清理**：`docs/OUTPUT-FORMAT.md` 的示例改为中性占位数据（原示例使用具体真实文献与 Zotero item key）；全部文档/脚本描述中的具体 DOI 与 arXiv ID 统一换成占位符（`10.5555/…`、`arXiv:2401.00001`），作者/期刊改为 `Author A` / `J. Appl. Phys.` 形式；`tools/README.md` 的领域相关示例参数改为中性措辞
- 安装脚本侧修复（审查发现）：HF 缓存目录探测在 sh 下用单连字符 `tr '/' '-'` 导致缓存恒判未命中（应为双连字符 `--`）；未指定 profile 时 `dsh plugin add` 缺 `--profile` 必然失败却仍 `exit 0`（现改为：唯一 profile 自动用 / 多个非交互报错退出 / 无 profile 默认 web / 安装失败 `exit 1`；dry-run 下多 profile 不中断演练）；内存探测的 CIM 非终止错误导致误报"0 GB 内存"（补 `-ErrorAction Stop` + 守卫）；微包嵌套布局兜底路径修正
- 配置表订正：`KB_AUTO_PIP` 仅在 npm 静态包 `lib/index.js` 实现（动态插件 host 只提示不自动装）；补充 `UNPAYWALL_EMAIL`

## [1.6.2] - MCP 超时加固 + 健壮性修复

- **MCP 大批量入库自动转后台（Kimi Work 60s 超时解药落地）**：`kb_ingest` 先轻量估算待处理文件数（目录递归/文件列表），超过 `KB_ASYNC_THRESHOLD`（默认 25）自动改用 async_mode，立即返回 `job_id` + `kb_status` 轮询指引——agent 无需知道 async_mode 存在，传整个文献库文件夹也不会超时；显式 `async_mode=true/false` 可强制
- **`kb_zotero` 支持 async_mode=true**：async 任务分发泛化（job 带 command 字段，`run_async_job` 按命令分发 ingest/zotero），整库迁移可后台执行 + kb_status 轮询
- **异步入库期间并发读不再锁死（高）**：`cmd_ingest` 由整批单事务改为**逐文件 commit**（写锁窗口从"整批"缩到"单文件+嵌入"）；`_migrate` 孤儿向量清理改 500ms 短超时探测、撞锁即跳过（读命令的 connect 不再干等 5s 或抛 database is locked）
  - *1.6.5 订正*：该断言当时过宽——只覆盖 `cmd_ingest` 的连接路径；检索末尾**写缓存**仍无锁容错、`cmd_zotero` 仍是整批单事务（两处均已在本版修复）
- **迁移健壮性**：`_migrate` 的 ALTER 只吞 "duplicate column"，锁冲突等其他 OperationalError 上抛（避免"版本号置新但列缺失"的静默不一致）
- **后台任务卫生**：`cmd_status` 校验 job_id 为 12 位十六进制（阻断目录穿越）；done 后自动清理 job/progress 残留（result 保留可重复读）；超 1h 无进展提示可能卡死；`kb_clear` 一并清空 `.kb-jobs`
- **启动即失败可感知**：`cmd_ingest_async` spawn 后短窗口 poll，子进程启动即退出时立即报错并清理，不再留"永久 running"的幽灵任务
  - *1.6.5 订正*：Popen 后立刻 `poll()` 基本抓不到失败（python 打开不存在的脚本也要数十毫秒才退出），实际只剩 1h stale 提示兜底；已改为 `wait(timeout=0.5)` + 启动前脚本存在性校验
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
