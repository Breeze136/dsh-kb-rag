# QUICKSTART — 五分钟上手（v1.6.2）

本页是 **DSH 插件形态** 的 5 分钟路线：装依赖 → 建库 → 检索 → 常见坑。想用 MCP（Claude Desktop / Kimi / Cursor 等桌面 agent）？直接看 [mcp-server/README.md](mcp-server/README.md)。

> 前置：Node.js ≥ 18；DSH 已装好且能启动（`dsh web` 启动对应的 profile 名就是 `web`）。Python 依赖由安装器自动处理；手动路线见文末。首次使用会联网下载模型（`BAAI/bge-small-zh-v1.5` + `bge-reranker-base`）到本地 HF 缓存。

## 第 1 分钟 · 一键安装（推荐）

**方式 A · npx 一行（最快，已装 Node 即可，直接复制）**：

```bash
npx --yes --package dsh-kb-rag -c "dsh-kb-rag-install --profile web"
```

> ⚠️ **最常踩的坑**：裸写 `npx dsh-kb-rag-install` 会 **E404** —— npx 会去找一个*名为 `dsh-kb-rag-install` 的包*（不存在）。必须带 `--package dsh-kb-rag`。`web` 是最常见的 profile 名；启动命令不同就把 `web` 换成你的名字（不确定时看 `~/.dsh/profiles/`，Windows 为 `C:\Users\<你>\.dsh\profiles\`）。

脚本自动完成：Python 依赖 → 引擎冒烟测试 → Node/pnpm 检查（缺 pnpm 自动装）→ `dsh plugin add` 安装并激活。（Windows 下参数自动翻译，统一用 `--profile` / `--mirror` / `--models` / `--dry-run` 风格即可。）

**方式 B · 下载仓库后运行脚本**：

```bash
git clone https://github.com/Breeze136/dsh-kb-rag.git
cd dsh-kb-rag

# Windows（双击 install.cmd 或）：
powershell -NoProfile -ExecutionPolicy Bypass -File npm-package\scripts\install.ps1
# macOS / Linux / Git Bash：
./npm-package/scripts/install.sh
```

常用参数（两种脚本等价）：`--profile <name>` 指定 DSH profile；`--mirror <url>` 走 pip 镜像；`--models` 顺带预下载模型；`--with-docx` 装可选的 DOCX 原生解析；`--dry-run` 只演练。受限网络示例：

```bash
./npm-package/scripts/install.sh --profile myprofile --models --mirror https://pypi.tuna.tsinghua.edu.cn/simple
export HF_ENDPOINT=https://hf-mirror.com   # 模型镜像（Windows: set 或 $env:HF_ENDPOINT）
```

**方式 C · 已有 dsh CLI**：`dsh plugin --profile web add dsh-kb-rag`（需 pnpm on PATH；Python 依赖设 `KB_AUTO_PIP=1` 重启后由插件自动装）。

装完**重启 DSH、开新会话**（工具在会话创建时注入），跳到「第 2 分钟 · 建库」。

### 升级到最新版

```bash
npm cache clean --force   # 清掉 latest 元数据缓存（避免拉到旧版）
npx --yes --package dsh-kb-rag -c "dsh-kb-rag-install --profile web"
```

钉版本用 `--package dsh-kb-rag@1.6.2`。升级完同样要重启 DSH、开新会话；旧 `.kb` 库 schema 自动迁移（`PRAGMA user_version` 门控，见 `docs/MIGRATION.md`）。

## 第 2 分钟 · 建库

- **文件夹/文件入库**：说"把 `D:/papers` 文件夹入库" → `kb_ingest(paths=[...])`。已入库且内容未变的文件自动跳过；同一内容（sha256 相同）在其他路径已入库的标记 duplicate 跳过——重复跑是增量同步，安全。
- **Zotero 迁移**：说"同步 Zotero" → `kb_zotero()`（自动定位 zotero.sqlite，带 PDF 附件的条目逐篇入库）。想先看会迁哪些：`kb_zotero(dry_run=true)`。附件文件缺失的条目标 `missing` 跳过（正常）。
- **按标识符补库（可选）**：`kb_fetch` 按 DOI / arXiv ID 下载 OA PDF（出版商正式版优先），下载后把 PDF 拖进 Zotero 或直接让 `kb_ingest` 入库。

## 第 3 分钟 · 检索与问答

1. 首次检索会自动弹出**查询范围**选择（封闭库 / 库+全网 / 仅全网），也可随时 `kb_scope` 切换；"严格只按库内回答"= 严格模式。
2. 提问："石墨烯是怎么用化学气相沉积合成的？" → `kb_rag`（默认 Top-3 证据，自动带编号引用 + 可点击 DOI）。
3. 找片段："搜 graphene CVD on copper" → `kb_search`（片段 + 精确来源）。
4. 命中带页码时渲染为「§章节 · p.N」——库里有 Zotero 来源的 PDF，可直接 `zotero://open-pdf?page=N` 一键跳页。

## 第 4 分钟 · 常用操作

| 需求 | 说法 |
|---|---|
| 切换范围 | "切到知识库+全网"（kb_scope） |
| 严格模式 | "严格只按库内回答" |
| 增量同步 | 重复 kb_ingest / kb_zotero（自动跳过） |
| 清理重复 | "去重"（kb_dedup） |
| 清空重建 | "清空知识库"（kb_clear，需确认） |
| 看清单 | "看看库里有什么"（kb_stats） |

## 第 5 分钟 · 常见坑

- **装完工具不出现**：工具在会话创建时注入 → 重启 DSH 并开**新**会话
- **npx E404**：见第 1 分钟 ⚠️，必须 `--package dsh-kb-rag`
- **首次检索慢（约 15s）**：模型加载（守护进程只加载一次，之后亚秒级）
- **工具报"缺少 Python 依赖"**：按工具返回里的命令 `python -m pip install <缺的包>`；或设 `KB_AUTO_PIP=1` 重启 DSH 自动装；或重跑一键安装（幂等，可反复跑）
- **旧数据没有页码**：页码锚点是 schema v3 起的字段，旧行页码为 NULL、自动降级；`kb_ingest(force=true)` 重入库后恢复
- **Zotero 报 missing**：附件文件本体缺失（未下载），正常跳过，不尝试联网下载
- **工具输出不是卡片**：部分界面不渲染自定义卡片，不影响使用——靠答复中的 DOI 链接与文件名点击

## 手动路线（可选，老式动态插件）

不想跑脚本/命令、或环境受限时：

1. **环境**：Python 3.9+（建议 3.10+）＋ `pip install PyMuPDF faiss-cpu sentence-transformers numpy`；可选 `python-docx`（DOCX 原生解析，缺失时引擎自动回退 zip+regex 提取）
2. **放引擎**：把 `kb_engine.py` 复制到你的 DSH 会话工作区根目录（插件按会话工作区自动定位）
3. **加载插件**：在 DSH 会话中 `cordis_define`：`code.host` ← `plugin/host.js` 内容、`code.client` ← `plugin/client.js` 内容，再 `cordis_run` 激活（客户端半首次需审批）；或直接说"加载 kb-rag 插件，代码在工作区 kb-rag/plugin/ 下"让模型代劳
4. 然后照常走第 3、4 分钟的流程
