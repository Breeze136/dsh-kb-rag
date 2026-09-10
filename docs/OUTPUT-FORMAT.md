# 智能体 kb-rag 输出格式建议（v2）

> 变更目标：**去掉回答末尾的"点击原文打开"区块**；将"补充建议"升级为**引文关联建议**——
> 从回答所用证据的**正文引文标记 [n]** 出发，回溯该文献的参考文献列表，把被引文献推荐给用户，
> 并明确标注**出自哪一篇文献的哪一条引文**；同时**证据引用精确到文献中的第几段**。
> 状态：**已按方案 A + 段落列 + 页码锚点实施并通过引擎级实测**（schema v3：段落列 + PDF 页码列；
> References 保留、检索透传、引文解析、页码跳页）；**Nature 上标角标识别 + 三级 References
> 检测 + 引文关联库内匹配已实施**，三层渲染（MCP / DSH 插件 / npm 包）统一输出引文块与
> 「⭐ 库内命中」行。检测局限见 §5/§7。

---

## 1. 变更点一览

| 项 | 现状（v1） | 建议（v2） |
|---|---|---|
| 回答末尾 | 1) "关联文献（可作补充建议）"（元数据相似）2) "点击原文打开"链接区块 | **删除"点击原文打开"区块**；"补充建议"改为**引文关联建议** |
| 补充建议来源 | 库内同作者/同期刊/近年代/主题相似的**库内文献** | 检索证据**正文中实际引用的文献**（含库内与库外） |
| 出处标注 | 推荐理由（同作者/同期刊…） | **"出自 <证据文献名> 的引文 [n]"**，精确到条 |
| **证据位置（段落号）** | 无 | **隐式元数据**：`section` + `para_start/para_end` 只进工具 JSON，**正常回答不显示**；仅在"点开文献细看 / 用户追问'这句在文献哪里'"时按需暴露或用于定位 |
| 打开方式 | 末尾堆叠 zotero:// 桥接链接 | 不堆叠；需要时在单条建议上保留一个可点击入口（可选） |

保留不变：证据引用格式（DOI markdown 链接 / 无 DOI 文件名）、[n] 标注、严格模式、库外常识标注、检索说明。

**设计原则：平时不打扰，需要时精准。** 段落号/章节是"打开文献"时才用的辅助信息，默认不进入回答正文，避免引用噪音。

---

## 2. 查询/详细双模式（depth）

两个检索入口的默认深度不同，显式传参永远优先：

| | quick（查询模式·快查） | deep（详细模式） |
|---|---|---|
| 场景 | 用户就是要查个信息：文献数据、术语、事实 | 用户不了解领域、要背景与跨文献综合 |
| 默认入口 | `kb_search`（查信息入口） | `kb_rag`（问答入口） |
| 精排（bge-reranker） | 关 | 开 |
| 引文链 / 关联文献 | 关 | 开 |
| top_k / snippet | 3 / 300 | 5（search）3（rag）/ 400–600 |
| guidance | 直接给答案并标来源，不展开背景 | 综合多篇展开说明，给出背景与脉络 |
| 实测时延 | ~34ms（无模型推理） | ~4.3s（首次精排含模型加载） |

- 会话级默认 deep（会话启动时询问"回答深度"，推荐项为详细；用户说"快查"随时切换）；
  单次调用用 `depth=quick|deep` 覆盖。
- quick 的渲染尾注提示："已省略精排与引文链/关联文献；需要深入背景时用 depth=deep 重查"——
  给 agent 一个升级路径，而不是死胡同。
- 缓存 key 已含 depth 相关参数（top_k/snippet/rerank/related），quick/deep 自动隔离。

## 3. 新输出结构（模板）

```
## <回答正文>

每句事实后标注证据编号 [n]（对应 evidence 下标）；**正文不带段落号**。
多源冲突分别列出；资料不足明确说"根据现有资料无法回答"。

*（可选一行）检索说明：混合检索 · 精排 BAAI/bge-reranker-base · 命中 N 块*

**补充建议（按来源分三列，哪列为空就整列省略，全空整块省略）**
📚 **库内可查（循引文找到）**
- <《被引文献标题》(作者, 年份)> —— 被 [证据编号] 的引文 Ref k 引用；已在库内，可直接对其提问
📥 **建议补库（循引文发现）**
- <被引文献作者, 年份, 标题/期刊> —— 被 [证据编号] 的 Ref k 引用，尚不在库内（可用 Ref k 定位下载）
🔗 **相关文献**
- <相关文献>(作者, 年份, 期刊) —— 同作者/同期刊/主题相似（元数据相似，无引文关系）

规则：每条推荐的理由必须写明属于哪种；引文关联的两列必须带关系链（谁引谁、Ref 编号），
不得与元数据相似的相关文献混列；若全部证据均无可用引文，前两列省略。

*（严格模式）仅基于证据作答；证据不足不补充库外知识。
（非严格模式）库外常识补充处标注"非库内知识"。*
```

### 定位锚点：页码优先（隐式，按需暴露）

- **页码 = 首选锚点**（PDF 物理页码，1 基）：两栏/扫描排版 PDF 也可靠，用于
  `zotero://open-pdf/library/items/<key>?page=N` 一键跳页。段落号仅作降级辅助
  （两栏 PDF 文本层段落被合并时段号不可靠，实测 Nature 类论文全文仅 ~8 段）。
- **平时**：回答正文不显示页码/段号——零噪音。
- **打开文献细看时**：证据卡片携带 `§<section> · p.<页码>`（可折叠/悬浮），或直接
  Zotero 跳页（`?page=N`，Zotero 7 支持）。
- **追问时**：用户问"这句话在文献哪里？"→ agent 读证据 JSON 的 `page` 字段回答，
  例如："出自 [3]（Ederer & Spaldin 2005, PRB）的 §Results · p.4"。
- **数据支持**：`chunks` 表 `page_start/page_end`（schema v3），来自 read_document
  的 `meta['_paras']` 段落→页码映射；检索结果/evidence 带 `page` 字段。
  txt/md/docx 与旧数据无页码（NULL）→ 降级为仅章节。

### 示例（真实数据驱动的完整推演，v2 页码版）

> 问：BFO 的反铁磁序和自旋摆线是什么结构？这个结论能在原文哪里找到？
>
> **工具返回（MCP 渲染，agent 可见）：**
>
> ```
> **知识库来源 Top-2**
> 混合检索 · 精排 BAAI/bge-reranker-base · 386ms
>
> 1. [Switching the spin cycloid in BiFeO3 with an electric field](https://doi.org/10.1038/s41467-024-47232-5) — Peter Meisenheimer; Guy Moore · 2024 · Nature Communications · §Results · p.4
> > the spin rotates in the plane defined by k and P ... with a period of ~65 nm
> ↳ 引文补充（本证据的参考文献；⭐=已在库内，可检索引用）
>   · [Ref 4] Sosnowska, M. et al. J. Phys. C 15, 4835 (1982)
>     ⭐ 库内命中：[Spiral magnetic ordering in bismuth ferrite](https://doi.org/…)（Sosnowska · 1982 · J. Phys. C）（即本证据的 Ref 4，可检索引用）· [Zotero 打开](zotero://open-pdf/library/items/…)
>   · [Ref 9] Kadomtseva, A. et al. — 摆线磁电理论
>   ↳ 另有 3 条库外引文未展开（Ref 6–8），补库时可按编号定位
> [DOI 10.1038/s41467-024-47232-5](https://doi.org/10.1038/s41467-024-47232-5) · score 0.98
>
> 2. [Weak ferromagnetism and magnetoelectric coupling in bismuth ferrite](https://doi.org/10.1103/PhysRevB.71.060401) — Ederer; Spaldin · 2005 · Physical Review B · §Body · p.2
> > If the magnetic moments are oriented perpendicular to the {111} direction, the symmetry also permits a canting…
> [DOI 10.1103/PhysRevB.71.060401](https://doi.org/10.1103/PhysRevB.71.060401) · score 0.87
>
> **关联文献（可作补充建议）**
> - [The Experimentalist's Guide to the Cycloid](https://doi.org/10.1002/adma.202003711) — Burns et al. · 2020（同作者·主题相似 · score 4.4）
> ```
>
> **Agent 回答：**
>
> BFO 的磁结构是"G 型反铁磁 + 长程自旋摆线"的复合序：
> - 本征 **G 型反铁磁序**（Fe³⁺ 最近邻反平行，T_N≈643 K），自旋 ⊥[111] 时子晶格倾斜产生弱铁磁[2]；
> - 叠加**非公度自旋摆线**：自旋在 k 与 P 张成平面内旋转，**P∥[111]、k∥[1̄10]，周期 ≈65 nm**，
>   M(r)=m[cos(k·r)êₖ+sin(k·r)êₚ][1]。
>
> **引文补充建议（出自本次证据的参考文献）**
> - [Sosnowska et al., 1982, J. Phys. C](https://doi.org/10.1038/s41467-024-47232-5)（出自 Meisenheimer 2024 的引文 Ref 4：摆线首次中子发现）
> - [Kadomtseva 摆线磁电理论](https://doi.org/10.1038/s41467-024-47232-5)（出自 Meisenheimer 2024 的引文 Ref 9）
>
> **追问示例** —— "这个结论原文在哪？" → agent 读 `page` 字段：
> > 出自 [1]（Meisenheimer et al. 2024, Nat. Commun.）**§Results · 第 4 页**，点击直达：
> > **[在 Zotero 打开到 p.4](zotero://open-pdf/library/items/FVPJFV8Q?page=4)**
> > [2] 出自 Ederer & Spaldin 2005, PRB **§Body · 第 2 页**：[跳转 p.2](zotero://open-pdf/library/items/V2AKVNXW?page=2)

> 说明：`§Results · p.4` 中 p = **PDF 物理页码**（1 基），对两栏/扫描排版同样可靠（实测两栏
> PDF 全部块均带页码）；段落号仅作降级辅助。DSH 侧页码是工具 JSON 的隐式字段，平时正文不显示；
> MCP 侧因 agent 只读文本而显式呈现。txt/md/docx 与旧数据无页码（NULL）→ 自动退化为仅章节。

---

## 4. 引擎数据支持（实施前提，重点）

### 4.1 现状缺口：References 被丢弃

`kb_engine.py` 的 `chunk_document()`：

```python
if len(text) < 40 or w <= 0:
    continue          # ← References（weight 0）整块丢弃，库内无参考文献数据
```

因此当前**无法**做 [n] → 引文文本的映射。引文关联必须先把参考文献数据保留下来。

### 4.2 建议方案 A：References 分块入库但排除检索（改动最小）

1. `chunk_document()`：`w <= 0` 的分块**改为入库但标记 `weight = 0`**，不再 `continue`；
   检索侧 `keyword_ranking` / `vector_ranking` 的 SQL 已按 `weight` 加权，权重 0 天然不参与召回（验证 WHERE 不含 `weight>0` 时补上，或查询时显式 `weight > 0`）。
2. 新增 `_doc_references(db, doc_id)`：返回该文献 References 全文（按 seq 拼接）。
3. 新增引文解析 `_cite_map(ref_text)`：正则切分 `[n]` 条目（`^\s*\[?(\d{1,3})\]?\s*(.+)$` 或 `n. ` 序号行），得到 `{n: "Author, Year, Title, Journal..."}`。
4. 命中证据正文的 `[n]`（`re.findall(r"\[(\d{1,3})(?:[–-]\d{1,3})?\]", snippet)`，跳过参考文献编号错位/无条目）→ `_doc_references` → `_cite_map` → 取第 n 条。

### 4.3 建议方案 B：独立 citations 映射表（更强，适合后续做引文网络）

```sql
CREATE TABLE IF NOT EXISTS refs (
  id INTEGER PRIMARY KEY,
  doc_id INTEGER NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
  ref_no INTEGER NOT NULL,        -- 引文编号 [n]
  text TEXT NOT NULL              -- 该条引文全文
);
```

入库时对 References 分块逐条解析写入；`_doc_references` 直接查表（更快、可建索引、可去重）。
优点：为 ROADMAP 的"引文网络 / related-document"（P3-9）打基础；缺点：入库逻辑多一步解析。

> 建议先做方案 A（改动小、风险低），引文网络需求明确后再演进到 B。

### 4.4 库内/库外判定

- 被引文献若在库内：可用现有 `_search_core` 的标题/作者匹配（或直接查 docs.title LIKE）标记"库内，可点击"。
- 不在库内：仅文本展示（这正是"建议补充"的价值——提示用户补哪些文献）。

### 4.5 段落定位的数据支持（隐式元数据，实现前提）

**目标**：每条证据在**工具 JSON 层**携带 `section` + `para_start/para_end`（§章节 + 全局段落区间）；
渲染层默认不显示，仅在打开文献细看或用户追问时按需暴露（见 §2）。段落序号为**全局计数**
（从文献全文第一个段落到最后一个，References 除外），跨章节不重置。

1. **chunks 表新增两列**（`SCHEMA_VERSION` 1 → 2，走 `_migrate()` 的 `if cur < 2` 块）：

```sql
ALTER TABLE chunks ADD COLUMN para_start INTEGER;   -- 该块起始段落序号（全局）
ALTER TABLE chunks ADD COLUMN para_end INTEGER;     -- 该块结束段落序号（含）
```

2. **`chunk_document()` 记录段号**：段落切分处给每个 paragraph 一个全局序号 `para_no`；
   每个输出块记录 `(para_start, para_end)`：
   - 单段长文被 `split_long` 拆成多块 → 各块 `para_start == para_end`（同一段）；
   - 多段合并的块 → 记 `[首段, 末段]` 区间（如"第 8–10 段"）。
   - References 分块若保留（见 3.2），其段号照常记录，但检索与引用均不面向它。

3. **工具层透传，渲染层按需**：`_search_core` 结果 entry 携带 `section`/`para` 字段；
   渲染层**默认不展示**，挂到证据卡片的可折叠/悬浮位；模型在用户追问"这句在文献哪里"时
   直接读该字段回答。段落号不进回答正文（§2 设计原则）。

4. **旧数据降级（重要，与 zotero_key 不同）**：已有 chunks 的段号**无法回填**（入库时段落边界已
   合并进文本，无法重建精确段号）。因此：新列对旧块为 NULL → 追问时只能答"§章节"，无段落号；
   **重新入库（force）后才有段落号**。文档与 README 需说明此降级。

5. 可选增强：图注坐标（`_doc_captions`）与段落号可合并输出，如"§Results 第 8 段 · Fig. 3"；
   Zotero 定位（段落→页码映射就绪后）用 `zotero://open-pdf/library/items/<key>?page=N`。

---

## 5. 边界与降级

| 场景 | 行为 |
|---|---|
| 证据正文无 `[n]` 标记 | 引文建议区留空，提示"证据未含可引出的参考文献" |
| `[n]` 超出 References 条数 / 编号错位 | 跳过该条，不猜测；宁缺勿错 |
| 证据文献为书籍/无 References | 同上降级 |
| 库内匹配到被引文献 | 建议条目附「⭐ 库内命中」行：命中文献标题为 DOI 链接 + （作者 · 年份 · 期刊）+（即本证据的 Ref n，可检索引用）+ Zotero 打开（有 zotero_key 时）；命中条目排在引文块最前 |
| 证据来自**旧库**（page/para 为 NULL） | 页码/段号不可用：追问时只答"§章节"；重新入库（force）后恢复（隐式字段，不影响平时回答） |
| 同段信息被拆成多块 / 多段合并一块 | 段区间仅在被追问/展开时展示；页码区间同理（如 p.4–5） |
| 证据为 txt/md/docx（无 PDF 页） | `page` 为 NULL → 定位降级为章节 |
| 严格模式 | 引文建议也仅来自 evidence 文献的 References（仍属"基于证据"），不引入库外知识 |

## 6. 与相关文档的关系

- 本建议落地后：README "Citation Style" 表同步更新（去掉"End of answer: append a suggested additions note"，改为引文关联建议）；CHANGELOG 记 [1.6.0]。
- 依赖：`docs/MIGRATION.md` 的 schema 版本化（若走方案 B 需新增 `refs` 表 → `SCHEMA_VERSION` +1，新增 `if cur < 2` 迁移块）。

## 7. 实施记录与已知局限（引擎实测）

### 已实施（引擎 `kb_engine.py` / mcp-server / plugin 渲染）
- **查询/详细双模式（depth=quick|deep）**：`cmd_search` 默认 quick（无精排/关联文献，top_k 3、snippet 300），`cmd_rag` 默认 deep（精排+引文链+关联文献，guidance 分流：快查直给答案 / 详细展开背景）；显式传参优先，`kb_scope` 可设会话默认；渲染层 quick 压缩输出（无引文链/关联、短片段、尾注提示升级路径），实测 34ms vs 4.3s；
- `chunks` 表新增 `para_start`/`para_end`（SCHEMA v2）与 **`page_start`/`page_end`（SCHEMA v3，
  PDF 物理页码锚点）**；`_migrate()` 逐级自动迁移；
- `read_document` 产出 `meta['_paras'] = [(PDF页码, 段文本)]`（段落归属其起始页）；
  `chunk_document`/`fallback_chunks` 贯穿页码，返回 7 元组；
- References（weight 0）**保留入库**供引文关联，检索 SQL 显式 `c.weight > 0` 排除；
- References 检测**三级**：① 行首标题（references/bibliography/参考文献）+ 后半段 + 序号条目；
  ② 无标题时文末【真正连续】序号段（每段 ≥2 条目 + 过半条目带年份/et al 信号，挡住
  末页图注/坐标轴数字误报；≤1 个无条目尾段可跳过）；③ **递增条目链**（Nature 无标题
  References：标题是图形，且正文 refs 与 Methods refs 分两段离散出现；Science '1. Author'
  行首风格同理）——编号从 1 递增或续接上一条链、条目过半含文献信号、链尾在
  Acknowledgements/©/图注等停止行截止，边界段落自动切分；
- **Nature 系上标角标识别**：PDF 文本层把上标压平成 'graphene1,2'，引擎在 `read_document`
  按**字体度量**（字号 ≤ 行内正文 80% + 基线抬高 ≥ 12%）检测上标数字簇，转写为
  'graphene[1,2]' 方括号形式入库；宁缺勿错跳过作者行（≥3 个上标簇）、指数
  （锚点以数字结尾，如 10¹²）、单位标记（'1*' 等非纯数字簇）；
- `_INCITE_RE` 支持 `[n]` / `[n-m]` / **`[n,m]` 逗号列表**（ACS 风格与上标转写共用）；
  `_expand_incite_nums` 统一展开；
- References 长块按**行边界**切分（`split_refs`，保留 'N.' 行首锚点；原 `split_long`
  句边界拼接会把 2、3 号条目挤到行中间，导致长引文列表只能解析第 1 条）；
- `_parse_references` 多风格同时命中时取**编号链最完整**的模式（避免换行噪声风格劫持）；
- `_doc_references`/`_parse_references`/`_cited_refs`：正文 `[n]`（含区间/逗号列表）→
  该文献引文条目；
- **引文关联深挖（库内匹配）**：`_match_cite_lib` 把引文条目对到库内文献——① 引文文本
  中 DOI 精确命中；② 库内标题（归一化 ≥30 字符）整串出现在引文文本中；③ 首作者姓
  （≥6 字符，避开 Wang/Li 类大姓）+ 括号年份双命中。命中条目带 `lib` 字段
  （title/authors/year/journal/doi/zotero_key），三层渲染（MCP `engine_client.py` /
  DSH `plugin/host.js` / npm `lib/index.js`）统一输出
  「⭐ 库内命中：<被引文献（DOI 链接）>（作者 · 年份 · 期刊）（即本证据的 Ref n，可检索引用）· Zotero 打开」；未命中引文折叠：命中条目（≤5）优先，未命中只展开前 3 条 + 一行「另有 N 条引文未展开（Ref x–y）」汇总
  + Zotero 打开链接（有 zotero_key 时）；
- 检索结果 entry 新增 `para`、**`page`**（[起,止]）与 `citations`（[{n,text,lib?}]）；
- MCP 渲染：`§<章节> · p.<页码>`（有页显页，无页退段号）；引文补充建议块；
- Zotero 跳页：`zotero://open-pdf/library/items/<key>?page=N`（数据已就绪，宿主渲染可选加参）。

### 已知局限（如实记录）
1. **References 检出率取决于 PDF 文本层**：三级检测（标题 / 文末连续段 / 递增链）后，
   实测 11 篇各出版商 PDF：Wiley 综述 0→399 条、Nature Letter 8→37 条、中文期刊 0→90 条、
   Science 0→29 条全部可解析；仍有个别扫描版（无文本层）无法解析 → `citations` 为空。
   噪声残留：极少数被换行拆开的数字会被当成一条多余引文（如 Science 论文出现编号 673），
   不影响真实编号的映射。
2. ~~**Nature 系上标数字引用**（正文 "…text¹²" 无方括号）暂不解析~~ **已支持**（字体度量
   检测 + 方括号转写，见上）；残留风险：字母锚点的上标指数（如 x²）仍会当成引文编号，
   出现率低，且仅当该编号恰好在 References 中存在才暴露——宁缺勿错优先保真。
3. **合并大段的段落区间偏粗**：多段并入一节后按 1200 字符拆分，各块 `para` 为该节整段区间
   （如 [2,52]）；单段/少段块区间精确。段落号是隐式字段，偏粗不影响平时回答。
4. **旧库需 force 重灌才有角标与全新 References 切分**（上标转写发生在入库时的
   `read_document`；旧 chunks 无 `[n]` 标记与新增 References 切分）。
5. 渲染层（lib/index.js / host.js）已实现引文块（含库内命中行）；宿主侧可视化
   （折叠/悬浮/Zotero 定位）为后续项。
6. **引文关联库内匹配**：三种规则（DOI / 标题 / 姓+年份）均要求高置信，无标题且无 DOI
   的引文条目（部分 PRB 数字风格）可能不命中——宁缺勿错。
