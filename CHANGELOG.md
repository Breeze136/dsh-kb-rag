# Changelog

## [Unreleased] - kb_fetch 下载增强

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
- **隐私修正**：安装脚本与文档中的示例从「BiFeO3 畴壁导电」改为中性的「石墨烯化学气相沉积合成」，移除作者研究领域信息
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
