# 未完成事项与审计记录（BACKLOG）

> 本文件记录**尚未修复**的已知问题、待验证项与验证方法。
> 已发布的变更写 [`CHANGELOG.md`](../CHANGELOG.md)；发布流程与隐私约定写 [`AGENTS.md`](../AGENTS.md)。
> 最近更新：1.6.5。

---

## 1. 1.6.5 修复清单（已发布，供对照）

来源：对 1.6.1 时期一次代码审计结论的逐条复核（复核基线为 1.6.4，逐行读码给出证据）。

| # | 问题 | 触发与后果 | 位置 | 修复 |
|---|---|---|---|---|
| 1 | 检索末尾写缓存没有锁容错 | 异步入库子进程持写锁时，**已算完**的检索以 `database is locked` 整次报错（DSH/MCP 表现为工具调用失败） | `_search_core` 尾部 `INSERT OR REPLACE INTO cache` | 只吞 `locked`/`busy`，其他 `OperationalError` 照旧上抛 |
| 2 | `kb_zotero` 整批单事务、从不写 progress | 异步整库迁移看不到进度；任务被杀把已入库文件**全部回滚**（0 篇落盘）；写锁窗口覆盖整批，放大问题 1 | `cmd_zotero` 循环 | 与 `cmd_ingest` 对齐：每篇 `_ingest_file` 后 `db.commit()` + `_prog()` |
| 3 | 异步启动校验形同虚设 | `Popen` 后**立刻** `poll()`；python 仅打开不存在的脚本也要数十毫秒才退出 → 最可能的启动失败抓不到，留下长期 `running` | `cmd_ingest_async` spawn 段 | `wait(timeout=0.5)` + Popen 前校验引擎脚本存在，失败即清 job 文件 |
| 4 | 引文链结束后章节被重置 | 一律重置为 `Front matter`（权重 1.0）。Nature 式论文正文 refs 与 Methods refs 是两段离散链，链后正文被标错章节 → `filters.section` 漏召回、排序权重掉档、§标签错误 | `chunk_document` 引文链分支 | 进入 References 时暂存 `(section, weight)`，链结束还原 |
| 5 | 图注块之后章节被重置（同类，复核时新发现） | Results 中插图之后的所有段落丢章节与权重 | 同上，`CAPTION_RE` 分支 | 图注仍独立成 `Figure/Table` 块，其后正文还原图注前章节 |
| 6 | `kb_clear` 清不掉原子写残留 | glob `*.json` 不匹配 `*.json.tmp`；进程在 rename 前被杀即留下永远清不掉的残留 | `cmd_clear` 的 `.kb-jobs` 清理 | glob 改 `*.json*` |
| 7 | 无页信息时返回 `[]` 而非 `None` | 污染"无页 = None"语义（当前调用方有 `len()` 守卫，无实际后果） | `_apply_ref_spans` | 保持 `None` |

同时订正了 `CHANGELOG` 1.6.2 节两处过宽断言（"并发读不再锁死"只覆盖 `cmd_ingest` 的 connect 路径；"启动即失败可感知"实际抓不到脚本路径错误）。

---

## 2. 已确认但**未**修复

### 2.1 后台任务没有自动失败终态（中）

- **现状**：`cmd_status` 只要 job/progress 文件存在就返回 `running`；`job.json` 的 mtime 超过 1 小时只**追加一句提示**，状态仍是 `running`（`kb_engine.py` 的 `cmd_status`，1.6.5 时约 L2741-2750）。
- **后果**：子进程被强杀或卡死时，调用方只能靠这句提示人工判断，没有可编程的终态。
- **待决策**：是否引入"心跳超时 → `stale`/`error`"的自动终态。若做，心跳依据已具备：progress 文件含 `processed`/`errors`/`chunks`，且是原子写，可用其 mtime 判定。
- **注意**：真正的长任务（整库 Zotero 迁移、大目录入库）可能长时间无产出，阈值不能取太小。

### 2.2 页码锚点是近似（低，已文档化，非缺陷）

- 无标题回退分块（`fallback_chunks`）各块页码取首段页，`page_end` 偏低；超长块被 `split_long` 切开后各片沿用整段范围。
- 已在 `docs/DESIGN.md`（分块锚点条目）与 `docs/OUTPUT-FORMAT.md`（页码降级表）声明为近似。

### 2.3 引擎进程退出行为未验证（低，待验证）

- 1.6.1 时期观测到"加载过嵌入模型的一次性引擎进程退出挂起（2 例，>10min）"；当前代码内**没有**任何退出处理（无 `atexit`、无 `os._exit`、无显式线程收尾）。
- **需要**：在允许起进程的环境跑 `python kb_engine.py run_job <job.json>`，观察退出码、耗时与是否有残留进程；确认后再决定是否加显式清理或强制退出。

### 2.4 `run_job` 与宿主命令白名单（待确认）

- `plugin/kbrag.plugin.json` 的 `engine.commands` 只列插件**实际调用**的命令（不含 `ingest_async`/`status`/`run_job`），这与"异步只走 MCP 侧"一致，非缺陷。
- **待确认**：若宿主按该白名单校验引擎子命令，引擎内部 `Popen(... run_job ...)` 是否会被策略拦截。本仓库无法判定，需要宿主侧确认。

### 2.5 尚未审计的区域（低）

- `npm-package/install.mjs` 与 `npm-package/scripts/install.ps1|sh`：只验证过 UTF-8 管道修复后不再复现 `WinError 123`，其余分支未审。
- `plugin/client.js` 的卡片视图。
- 上标角标识别（`_superscript_cites` / `_bracket_superscripts`）与 `_match_cite_lib` 的**误报率**：需要真实 PDF 语料评估；代码逻辑已读通，未发现确定性错误。

---

## 3. 验证方法（改引擎后必跑）

回归脚本在**本机工作区**（不随仓库发布，路径见交接记录），共 16 项检查，覆盖 1.6.5 的全部改动：

| 分组 | 检查 |
|---|---|
| 缓存撞锁（问题 1） | 占住写锁时时检索仍返回结果；无竞争时第二次调用命中 `cached` |
| Zotero 提交语义（问题 2） | 第二篇中断后：首篇已落盘（`docs>=1`）、进度文件已回写 `processed=1` |
| 章节还原（问题 4/5） | 夹具确实触发引文链（存在 `References` 块）；链前章节为 `Methods/1.2`；链后段落还原为 `Methods/1.2`；图注独立成 `Figure/Table` 且其后正文还原 |
| 异步启动（问题 3） | 子进程立即退出被识别为启动失败（`exit=N`）且不留 job 文件；引擎脚本缺失时直接失败 |
| 清理与语义（问题 6/7） | `kb_clear` 连 `*.json.tmp` 一起清；`pages=None` 时返回 `None`、有页时返回平行页列表 |

跑之前先确认：`kb_engine.py` 与 `npm-package/kb_engine.py` **逐字节一致**（`sha256`），两份都要能 `py_compile` 通过。

---

## 4. 记录约定

| 内容 | 写在哪 |
|---|---|
| 已发布版本的变更 | `CHANGELOG.md` |
| 发布流程、隐私扫描、发版后校验 | `AGENTS.md` |
| 未完成事项、审计结论、验证方法 | 本文件 |
