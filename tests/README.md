# kb-rag 测试框架

一条命令跑完全部验证，并把"保底机制"做成硬检查 —— 出问题就大声失败，不静默通过。

```bash
python tests/run.py              # 快测（不含 slow，约 1 分钟；缺模型/依赖的用例自动 SKIP）
python tests/run.py --all        # 含 slow：全库切块健康度 + 引文关联命中率 + 下载闭环（要联网）
python tests/run.py --only fetch # 只跑下载闭环（会真去 arxiv/出版商下 PDF，离线自动 SKIP）
python tests/run.py --only refs  # 只跑名字含 refs 的 suite
python tests/run.py --list       # 列出全部 suite
python tests/run.py --fail-fast  # 首个失败即停
python tests/run.py --workers 1  # 强制串行（排查用）
python tests/run.py --no-cache   # 忽略逐篇指标缓存，全量重算
python tests/run.py --json out.json
```

**想把功能验收交给框架时，跑这三条就够**：`--only functional`（入库/后台/维护闭环）、
`--only mcp`（MCP 交付面）、`--all --only fetch`（下载）。三者都能反复跑，写操作全在沙箱临时库。

**耗时（本机 316 篇真实 PDF 实测）**：快测 ~50 s ｜ `--all` 冷跑 ~130 s、热跑 ~60 s。
（最初是单进程串行、两个 slow 套件各解析一遍全库 = 498 s；见下面的"为什么这么快"。）

## 为什么这么快

| 措施 | 效果 |
|---|---|
| **一次解析、两个套件共享** | refs_real 与 cites_real 都要"逐篇重新切块"，原来各跑一遍；现在一篇 PDF 只解析一次，同时产出切块健康度与引文关联两套指标 |
| **子进程分片并行** | 默认 `min(8, CPU)` 个子进程，交错切分作业、结果写文件回传。**不用 ProcessPoolExecutor**：受限环境下 Windows 进程池要靠命名管道做 IPC，会被直接拒绝（实测 `PermissionError WinError 5`）；线程池又吃不到 GIL（实测 1.0×）。子进程 + 文件是这里唯一走得通的路 |
| **逐篇指标缓存** | `tests/_cache/real_metrics.json` 按 `(路径, mtime, size, 引擎哈希)` 记账：文档没变就复用，引擎一改立刻全量重算（不会拿旧结果骗自己） |
| 子进程失败兜底 | 任何分片进程挂掉/超时，框架会把缺失的作业**串行重算**补上，绝不静默丢数据 |

真实库位置：`KB_RAG_REAL_KB=<目录>` 覆盖；否则自动找 `<cwd>/.kb`、`<仓库上级>/.kb`。
**找不到真实库时**，依赖它的 slow 用例会 SKIP，快测照常跑。

## 保底机制（tests/guards.py）

| 机制 | 做法 | 触发条件 |
|---|---|---|
| **测试不许改仓库** | 跑前跑后对全部 tracked 文件做哈希快照并比对 | 有变化 → 整轮失败并列出文件 |
| **真实知识库只读** | 数据驱动用例只碰沙箱里的**副本**；跑完比对真实库指纹（mtime/大小/docs/chunks/vecs/cache 计数） | 指纹变化 → 整轮失败并打印前后值 |
| **双份引擎同哈希** | `kb_engine.py` 与 `npm-package/kb_engine.py` 逐字节比较（`--fix-twin` 可自动同步副本） | 不一致 → 整轮失败 |
| **提示镜像不漂移** | 跑 `tools/sync-host-guidance.mjs --check` | 漂移 → 整轮失败 |
| **超时** | 每个 suite 单独超时（快测 300 s、slow 1800 s），超时算失败而不是挂死 | 超时 → fail |
| **缺依赖不误报** | 缺模型缓存 / 缺 PyMuPDF 等 → SKIP 并写明缺什么 | 不算失败，汇总里单列 |
| **基线回归** | `tests/baselines/*.json` 记录验收值（整篇不可检索=0、weight=0 占比、引文命中率…），slow 用例按容差比对 | 超阈值 → fail |
| **可复现** | 每轮写 `tests/_reports/<时间戳>/report.json` + 沙箱路径；`--only` 可单独复跑失败的 suite | —— |

沙箱、报告与逐篇指标缓存都在 `tests/_sandbox/`、`tests/_reports/`、`tests/_cache/` 下（已在 `.gitignore` 里）。

## 套件一览

| suite | 依赖 | 覆盖 |
|---|---|---|
| `static` | 无 | py_compile / node --check / host.js 作函数体 / JSON / 双份引擎同哈希 / manifest 的 engine.commands 与引擎命令表对齐 / 工具清单 / 版本号 / **tracked 文件里不得有本机绝对路径**（AGENTS.md 隐私约定） |
| `chunking` | 无 | References 判定的合成版式：常规条目、Wiley 紧贴式 `1J. Valasek`、`S1.` 补充材料编号、正文编号列表/单位行/`2D`·`3D` 不得误判、巨段文献表、大文献表不被一刀切、整篇不可检索兜底 |
| `bm25` | 无 | 用**独立参考实现**逐位核对 BM25（k1=1.2/b=0.75/子串 df/章节权重）+ 缓存不改变结果 + 确定性 |
| `gpu` | 无（假模型） | 无 GPU→CPU、显式 cuda 无卡→CPU、加载期 CUDA 错→退 CPU 且**粘性关闭**、探测不被反复死磕、非设备故障不得被兜底掩盖、运行期 OOM 与非 OOM→缩批→CPU、CrossEncoder 形态的 `.to()` 兼容 |
| `engine_loop` | 模型 | 入库 → 删向量 → `skipped` 回填 → `duplicate` 也回填 → stats/health → `reload` → `metadata_only` → `state` 读写与非法键 → `clear` 需 confirm |
| `functional` | 模型 | **跨步骤功能闭环**（临时库）：入库四种结果各走一遍（新增/重复/跳过/更新）、改文件后新内容可检索、**库内不变量**（weight>0 的分块必须都有向量）、30 篇自动转后台 + `status` 轮询到 done、`dedup` 幂等、`metadata_only`、`rebuild` 转后台、多库互不干扰、`clear` 安全阀与真清 |
| `mcp` | 无（离线） | **MCP 交付面**（此前零覆盖）：基线 10 工具、`kb_mcp_status` 内容、`render_fetch` 带出入库结果、引擎异常时带出 stderr 尾部、`rebuild` 走 `ingest_async`、3.9 注解为字符串，以及隔离的**真实注册结果**（子进程真 import：`KB_MCP_EXCLUDE`→8、白名单→3、`KB_MCP_NO_PROBE`→10） |
| `fetch`（slow） | 网络 + 模型 | **下载闭环**：下载器可定位（否则静默退化成 Python 兜底）、arXiv 走 `source=arxiv`、DOI 落地页、URL 形式归一化、坏标识符给原因不误报、空列表拒绝、`ingest=true` 下载即入库；离线自动 SKIP |
| `search` | 模型 | filters 归一化（连字符/空格/大小写/作者分词 AND/年份）、相关性地板（库内 verdict=相关 / 库外 no_hit+closest / quick 不硬判）、负结果入缓存、语料缓存命中与**条数上限**、元数据改写后不得返回旧值、**跨进程写入**必须失效 |
| `plugin_harness` | node | stub ctx 加载插件：inject、10 个工具、描述注入、四个渲染器、`/kb` 状态卡与会话隔离、软/硬/半关闭与重新注册、深挖模式分档 |
| `host_half` | node | **动态半边**（`plugin/host.js` 按函数体求值，harness 用真实 `sandboxDefineTool`，会校验 schema/渲染块/JSON 可克隆）：inject 不得含可选服务、10 个工具、镜像描述注入、`withNotes` 结果注入（no-hit/降级/已关闭/深挖分档）、`/kb` 三档关闭与会话隔离、`commands` 缺失兜底，以及**两半 10 个工具逐字段一致**（描述/参数/输出 schema/timeoutMs/呈现器） |
| `guidance` | node | 9 条规则 × 7 种响应形状、深挖分档、节流（每会话 2 次）、会话一次性提示、描述注入、规则都要限定工具 |
| `client_half` | node | 两个客户端半边（npm bundle 与动态插件函数体）**各跑同一组断言**：bundle 协议、插槽注册、结构化 meta 与文本退回两条渲染路径、无命中理由/弱相关/指示条、空 props，并比对两半的实现片段与中文文案集合 |
| `refs_real`（slow） | 真实库 | 全库重新切块：整篇不可检索必须 0、weight=0 占比与"被吞 >50%"不超基线 |
| `cites_real`（slow） | 真实库 | 引文关联命中率不低于基线 −0.5pp、条目总数 ≥ 基线 97%、解析不出条目的文档数不增加、相对基线实现有提升 |

`refs_real` / `cites_real` 的对照基线记录在 `tests/baselines/*.json`（含"改前"提交号，用于改前/改后对比）。

## 加一个 suite

```python
# tests/suites/s_mycase.py
SUITE = {"id": "mycase", "title": "……", "tags": ["fast"], "needs_models": False}
def run(ctx):
    ctx.check("断言描述", True, "细节")
    ctx.skip("缺某依赖时这样跳过")     # 抛出后本 suite 记为 skip
```

node suite 约定：最后打印一行 `__SUITE_RESULT__ {"checks":[{"label":…,"ok":…}]}`（用 `tests/suites/_nodehelper.mjs` 的 `makeReporter()` 最省事）。

## 与仓库发布流程的关系

- 发布前至少跑 `python tests/run.py --all`（含两个 slow）；`static` 里的隐私扫描与 `AGENTS.md` 的脱敏约定同源。
- 本框架全部使用合成题材（石墨烯/铁电）做示例，不含真实文献标题、私人路径或 DOI。
