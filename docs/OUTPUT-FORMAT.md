# 智能体 kb-rag 输出格式建议（v2）

> 变更目标：**去掉回答末尾的"点击原文打开"区块**；将"补充建议"升级为**引文关联建议**——
> 从回答所用证据的**正文引文标记 [n]** 出发，回溯该文献的参考文献列表，把被引文献推荐给用户，
> 并明确标注**出自哪一篇文献的哪一条引文**；同时**证据引用精确到文献中的第几段**。
> 状态：**已按方案 A + 段落列实施并通过引擎级实测**（schema v2、段落号、References 保留、
> 检索透传、引文解析）；渲染层与宿主展示为后续项。检测局限见 §4。

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

## 2. 新输出结构（模板）

```
## <回答正文>

每句事实后标注证据编号 [n]（对应 evidence 下标）；**正文不带段落号**。
多源冲突分别列出；资料不足明确说"根据现有资料无法回答"。

*（可选一行）检索说明：混合检索 · 精排 BAAI/bge-reranker-base · 命中 N 块*

**引文补充建议（出自本次证据的参考文献）**
- <被引文献作者, 年份, 标题/期刊>（出自 <证据文献> 的引文 [k]）
- <…>
- 若全部证据均无可用引文：提示"本次检索的证据未含可引出的参考文献"

*（严格模式）仅基于证据作答；证据不足不补充库外知识。
（非严格模式）库外常识补充处标注"非库内知识"。*
```

### 段落定位（隐式，按需暴露）

- **平时**：回答正文不出现任何"§章节 / 第几段"字样——零噪音。
- **打开文献细看时**：渲染层可在证据卡片/文献入口上以**可折叠或悬浮**方式携带
  `§<section> 第 <a>–<b> 段`（如 tooltip、details 展开、或点开文献后的定位栏），不占回答视野。
- **追问时**：用户问"这句话在文献哪里？"→ 模型读取证据 JSON 的 `section`/`para` 字段回答，
  例如："出自 [3]（Ederer & Spaldin 2005, PRB）的 §Results 第 8 段"。
- **Zotero 定位展望（可选）**：若后续实现"段落→页码"映射，可用
  `zotero://open-pdf/library/items/<key>?page=N` 一键跳到对应页（Zotero 7 支持 `?page=`）。

### 示例（基于本库真实内容）

> 问：BFO 反铁磁序结构？
>
> 答：BFO 本征为 G 型反铁磁序（T_N≈643 K）[1]；其上叠加非公度自旋摆线，周期≈62–65 nm，
> 波矢 k∥[1̄10]、极化 P∥[111]，M(r)=m[cos(k·r)êₖ+sin(k·r)êₚ][3]……
> （段落号不进正文；用户追问时："[3] 出自 Ederer & Spaldin 2005, PRB，§Body 第 3 段 / §Results 第 8 段"）
>
> **引文补充建议**
> - Burns et al., 2020, Adv. Mater.（出自 Burns 2020 的引文 [22–25]：Sosnowska/Zvezdin 的自旋摆线热力学理论系列）
> - Lebeugle et al., 2007, Phys. Rev. B（出自 Ederer & Spaldin 2005 的引文 [14,15]：弱铁磁的对称性来源）

> 说明：上例"出自…引文"即引擎从证据文献的 References 中提取的条目；若该被引文献已在库内，
> 可在建议后附带库内 Zotero 入口（可选），不在库内则仅给文本，供用户补库。

---

## 3. 引擎数据支持（实施前提，重点）

### 3.1 现状缺口：References 被丢弃

`kb_engine.py` 的 `chunk_document()`：

```python
if len(text) < 40 or w <= 0:
    continue          # ← References（weight 0）整块丢弃，库内无参考文献数据
```

因此当前**无法**做 [n] → 引文文本的映射。引文关联必须先把参考文献数据保留下来。

### 3.2 建议方案 A：References 分块入库但排除检索（改动最小）

1. `chunk_document()`：`w <= 0` 的分块**改为入库但标记 `weight = 0`**，不再 `continue`；
   检索侧 `keyword_ranking` / `vector_ranking` 的 SQL 已按 `weight` 加权，权重 0 天然不参与召回（验证 WHERE 不含 `weight>0` 时补上，或查询时显式 `weight > 0`）。
2. 新增 `_doc_references(db, doc_id)`：返回该文献 References 全文（按 seq 拼接）。
3. 新增引文解析 `_cite_map(ref_text)`：正则切分 `[n]` 条目（`^\s*\[?(\d{1,3})\]?\s*(.+)$` 或 `n. ` 序号行），得到 `{n: "Author, Year, Title, Journal..."}`。
4. 命中证据正文的 `[n]`（`re.findall(r"\[(\d{1,3})(?:[–-]\d{1,3})?\]", snippet)`，跳过参考文献编号错位/无条目）→ `_doc_references` → `_cite_map` → 取第 n 条。

### 3.3 建议方案 B：独立 citations 映射表（更强，适合后续做引文网络）

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

### 3.4 库内/库外判定

- 被引文献若在库内：可用现有 `_search_core` 的标题/作者匹配（或直接查 docs.title LIKE）标记"库内，可点击"。
- 不在库内：仅文本展示（这正是"建议补充"的价值——提示用户补哪些文献）。

### 3.5 段落定位的数据支持（隐式元数据，实现前提）

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

## 4. 边界与降级

| 场景 | 行为 |
|---|---|
| 证据正文无 `[n]` 标记 | 引文建议区留空，提示"证据未含可引出的参考文献" |
| `[n]` 超出 References 条数 / 编号错位 | 跳过该条，不猜测；宁缺勿错 |
| 证据文献为书籍/无 References | 同上降级 |
| 库内匹配到被引文献 | 建议条目附"库内可打开"标记（保留单个可点击入口，不堆叠区块） |
| 证据来自**旧库**（para_start/para_end 为 NULL） | 段落号不可用：追问时只答"§章节"；重新入库（force）后恢复段落号（隐式字段，不影响平时回答） |
| 同段信息被拆成多块 / 多段合并一块 | 段区间（"第 8 段"或"第 8–10 段"）仅在被追问/展开时展示 |
| 严格模式 | 引文建议也仅来自 evidence 文献的 References（仍属"基于证据"），不引入库外知识 |

## 5. 与相关文档的关系

- 本建议落地后：README "Citation Style" 表同步更新（去掉"End of answer: append a suggested additions note"，改为引文关联建议）；CHANGELOG 记 [1.6.0]。
- 依赖：`docs/MIGRATION.md` 的 schema 版本化（若走方案 B 需新增 `refs` 表 → `SCHEMA_VERSION` +1，新增 `if cur < 2` 迁移块）。

## 6. 实施记录与已知局限（引擎实测）

### 已实施（引擎 `kb_engine.py`）
- `chunks` 表新增 `para_start`/`para_end`（SCHEMA v2，`_migrate()` `if cur < 2` 块自动迁移）；
- `chunk_document`/`fallback_chunks` 记录全局段落号，返回 5 元组；
- References（weight 0）**保留入库**供引文关联，检索 SQL 显式 `c.weight > 0` 排除；
- References 检测两级：① 行首标题（references/bibliography/参考文献）+ 后半段 + 序号条目；
  ② 无标题时**文末连续高密度序号段**（Wiley `[n]` / 常规 `n.` / 紧贴 `nAuthor` 三风格）；
- `_doc_references`/`_parse_references`/`_cited_refs`：正文 `[n]`（含区间 `[a–b]`）→ 该文献引文条目；
- 检索结果 entry 新增 `para`（[起,止]）与 `citations`（[{n,text}]，隐式字段，按需暴露）。

### 已知局限（如实记录）
1. **References 检出率取决于 PDF 文本层**：无标题 + 多栏排版的论文（实测 Wiley 综述）只能
   保住文末一段参考文献（编号 156-285），正文引用 1-155 无法解析 → `citations` 为空。
2. **Nature 系上标数字引用**（正文 "…text¹²" 无方括号）暂不解析（`_INCITE_RE` 只认 `[n]`）；
   如需支持需加"上标数字→引文"映射（风险：正文数字误判，宁缺勿错）。
3. **合并大段的段落区间偏粗**：多段并入一节后按 1200 字符拆分，各块 `para` 为该节整段区间
   （如 [2,52]）；单段/少段块区间精确。段落号是隐式字段，偏粗不影响平时回答。
4. **旧库段落号为 NULL**（无法回填）：追问时只答章节；`force` 重入库后恢复。
5. 渲染层（lib/index.js）未改：`para`/`citations` 已随工具 JSON 返回，模型可直接用于
   "第几段"追问与"引文补充建议"；宿主侧可视化（折叠/悬浮/Zotero 定位）为后续项。
