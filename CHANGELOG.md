# Changelog

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
