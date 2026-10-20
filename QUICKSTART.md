# QUICKSTART — 五分钟上手（v1.6.8）

本页是 **DSH 插件形态** 的五分钟路线：装依赖 → 建库 → 检索 → 常见坑。想用 MCP（Claude Desktop / Kimi / Cursor 等桌面 agent）？看 [mcp-server/README.md](mcp-server/README.md)。

> 前置：Node.js ≥ 18；DSH 已装好且能启动（`dsh web` 启动的 profile 名就是 `web`）。Python 依赖由安装器处理，手动路线见文末。首次使用会下载模型（`BAAI/bge-small-zh-v1.5` + `bge-reranker-base`）到本地 HF 缓存。

## 第 1 分钟 · 安装

**方式 A · DSH 桌面端（推荐，不写命令）**

1. 打开 **设置 → 插件**。
2. 搜索 `kb-rag`（包名 `dsh-kb-rag`），选 **1.6.8** 安装。
3. 重启 DSH，新建会话。

桌面端需要 0.2 及以上。装完**重启 DSH、开新会话**，工具在会话创建时注入。

**方式 B · npx 一行（已装 Node 即可）**

```bash
npx dsh-kb-rag-install
```

安装器依次完成：Python 依赖 → 引擎冒烟测试 → Node/pnpm 检查（缺 pnpm 自动装，失败回退 corepack）→ `dsh plugin add` 安装激活（未指定 profile 时自动检测 `~/.dsh/profiles/`）→ 模型预下载（直连失败切 hf-mirror.com 重试）。`--no-models` 跳过预下载，`--profile web` 指定 profile，`--dry-run` 先演练。Windows 下参数会自动翻译。

**方式 C · 已有 dsh CLI**

```bash
dsh plugin --profile web add dsh-kb-rag@1.6.8
```

需要 pnpm 在 PATH；Python 依赖可设 `KB_AUTO_PIP=1` 让插件启动时装。

**方式 D · 下载仓库后运行脚本**

```bash
git clone https://github.com/Breeze136/dsh-kb-rag.git
cd dsh-kb-rag

# Windows（双击 install.cmd 或）：
powershell -NoProfile -ExecutionPolicy Bypass -File npm-package\scripts\install.ps1
# macOS / Linux / Git Bash：
./npm-package/scripts/install.sh
```

常用参数：`--profile <name>`、`--mirror <url>`（pip 镜像）、`--models`、`--with-docx`（可选的 DOCX 原生解析）、`--dry-run`。受限网络示例：

```bash
./npm-package/scripts/install.sh --profile myprofile --models --mirror https://pypi.tuna.tsinghua.edu.cn/simple
export HF_ENDPOINT=https://hf-mirror.com   # Windows: $env:HF_ENDPOINT
```

> **Windows、连 DSH 都还没装？** 到 [dsh-oneclick 发行页](https://github.com/Breeze136/dsh-oneclick/releases/latest) 下载 zip，**完整解压**后双击 `install.cmd`。Node.js、DSH 命令行工具、桌面快捷方式它会装好；中途问一句要不要知识库，回车即装。第三方脚本，非 DeepSeek 官方发布。

### 升级

DSH profile 是 pnpm 工作区，升级走同一条命令，并钉住版本：

```bash
dsh plugin --profile web add dsh-kb-rag@1.6.8
```

pnpm 的 `minimumReleaseAge` 会拒绝太新的版本，不钉版本时刚发版后可能解析到上一版。也可以重跑安装器。不要在同一 profile 里执行 `npm install dsh-kb-rag`：npm 与 pnpm 的目录布局会打架。

升级后重启 DSH、开新会话。1.6.8 没有 schema 变化，旧 `.kb` 直接可用；页码锚点与上标角标仍是对旧文档 `force` 重入库后才有（迁移只加列，不回填解析结果）。

## 第 2 分钟 · 建库

- **文件/文件夹**：说“把 `papers/` 入库” → `kb_ingest(paths=[...])`。内容未变的文件自动跳过；同一内容（sha256 相同）在其他路径已入库的标记 duplicate。
- **Zotero**：说“同步 Zotero” → `kb_zotero()`，自动定位 `zotero.sqlite`，带 PDF 附件的条目逐篇入库。先看会迁哪些：`kb_zotero(dry_run=true)`。附件本体缺失的条目标 `missing` 跳过。
- **按标识符补库**：`kb_fetch` 按 DOI / arXiv ID 下载 PDF，优先出版商正式版（校园网/机构订阅网络可直接下订阅版），失败回退开放获取；下载后拖进 Zotero 或直接 `kb_ingest`。
- **大批量**：待处理文件超过 25 个时自动转后台，返回 `job_id`，用 `kb_status` 轮询到 `done`。宿主超时不会中断。

## 第 3 分钟 · 检索与问答

1. 首次检索会问一次**查询范围**（封闭库 / 库+全网 / 仅全网）与**检索深度**（快速 / 深度）。随时可用 `kb_scope` 或 `/kb` 改。
2. 提问：“石墨烯是怎么用化学气相沉积合成的？” → `kb_rag`。默认 `quick`，只基于少量证据直接回答；要精排、引文关联与相关文献，传 `depth: "deep"` 或先 `/kb deep`。
3. 找片段：“搜 graphene CVD on copper” → `kb_search`，默认 `quick`，亚秒级；`deep` 走完整链路。
4. 命中带页码时渲染为「§章节 · p.N」，Zotero 来源的 PDF 可直接跳页。
5. 证据下方 `↳ 引文补充`：参考文献里已在库内的条目标 `[库内]`，库外的给出补库线索。
6. 想要一份能发出去的成品：说“做个对比表 / 出一份报告” → `kb_rag(answer={...})` 生成自包含 HTML，页脚有检索与渲染耗时。

## 第 4 分钟 · 常用操作

| 需求 | 说法 |
|---|---|
| 切换范围 | “切到知识库+全网”（`kb_scope`） |
| 严格模式 | “严格只按库内回答” |
| 检索深度 | “用快速检索” / “用深度检索”，或 `/kb quick` / `/kb deep` |
| 答案模板 | `/kb format compare`（或 review / numeric / timeline …） |
| 大库入库 | 超过 25 个待处理文件自动转后台，`kb_status` 轮询 |
| 增量同步 | 重复 `kb_ingest` / `kb_zotero`，自动跳过 |
| 清理重复 | “去重”（`kb_dedup`） |
| 清空重建 | “清空知识库”（`kb_clear`，需确认） |
| 看清单 | “看看库里有什么”（`kb_stats`，含模型缓存状态） |

## 第 5 分钟 · 常见坑

- **装完工具不出现**：工具在会话创建时注入 → 重启 DSH 并开**新**会话。
- **插件面板装不上**：1.6.7 及更早的 peer 范围与 0.2 不兼容，装 1.6.8。确认 `node_modules/dsh-kb-rag/package.json` 的版本是 1.6.8。
- **npx E404**：1.6.2 及更早没有同名微型包；1.6.3+ 已修。npm 缓存旧元数据时先 `npm cache clean --force`。
- **首次检索慢（约 10–15 秒）**：模型加载；守护进程只加载一次，之后热态 `quick` 0.3 s 左右、`deep` 1.5 s 左右。看 `kb_stats` 的 `models` 判断是没下载还是没加载。
- **模型下载卡住**：设 `HF_ENDPOINT=https://hf-mirror.com` 与 `KB_HF_MIRROR_FIRST=1`，或预下载；`HF_HUB_DOWNLOAD_TIMEOUT` 默认 600 秒。
- **工具报缺少 Python 依赖**：按返回里的命令装，或设 `KB_AUTO_PIP=1`，或重跑安装器（幂等）。
- **旧数据没有页码**：页码锚点是 schema v3 起的字段，旧行降级到章节；`kb_ingest(force=true)` 重入库后恢复。
- **Zotero 报 missing**：附件本体缺失，正常跳过，不联网下载。
- **工具输出不是卡片**：部分界面不渲染自定义卡片，不影响功能；用答复里的 DOI 链接与文件名。

## 手动路线（可选，老式动态插件）

不想跑脚本或环境受限时：

1. **环境**：Python 3.9+（建议 3.10+）＋ `pip install PyMuPDF faiss-cpu sentence-transformers numpy`；可选 `python-docx`（缺失时引擎回退 zip + regex 提取）。
2. **放引擎**：把 `kb_engine.py` 复制到 DSH 会话工作区根目录；再把 `tools/doi_pdf.mjs` 复制成 `<工作区>/scripts/doi_pdf.mjs`。引擎只在自身目录旁找这个下载器（`scripts/` → `tools/` → 同级），漏了它不会报错，而是静默退化成内置 Python 下载兜底：裸 arXiv ID 会失败，文件名也会丢标题。
3. **加载插件**：在 DSH 会话里 `cordis_define`：`code.host` ← `plugin/host.js`，`code.client` ← `plugin/client.js`，再 `cordis_run` 激活；或让模型代劳。
4. 之后照常走第 3、4 分钟。

> `plugin/` 是旧的动态插件形态，host 逻辑停在 1.6.7；新装用户请用上面的 npm 包路径。
