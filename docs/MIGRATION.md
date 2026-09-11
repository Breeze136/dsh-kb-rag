# kb-rag 数据库迁移文档（Schema Migration）

> 版本：适用于插件 1.0.5 → 1.6.6+ 的升级场景，以及未来所有跨版本升级。
> 状态：**版本化迁移已在引擎实现并通过实测**（新库首建 / 旧库 v0→v4 自动迁移 / zotero_key 回填 / 检索回归），代码见 `kb_engine.py` 的 `_migrate()`；本文档同步记录设计约束与扩展方法。

---

## 1. 目标

- 旧插件版本升级到新插件版本时，`.kb/kb.sqlite` 的表结构自动对齐新版本，**无人工干预、无数据丢失**。
- 升级幂等：重复运行、来回升降级都无害。
- 向后兼容：新库被旧引擎打开不报错（加列可空）。

## 2. 当前实现（已落地，勿删）

### 版本化迁移（已实现）

引擎每次连接时自动执行，实现在 `kb_engine.py` 的 `connect()` + `_migrate()`（`PRAGMA user_version` 门控，见 §4）：

```python
SCHEMA_VERSION = 4          # 与 VERSION（代码版本）独立；v1 结构补齐 / v2 段落号 / v3 PDF 页码 / v4 解析器版本标记

def connect(kb_root):
    root = Path(kb_root); root.mkdir(parents=True, exist_ok=True)
    db_file = root / "kb.sqlite"
    created = not db_file.exists()          # 必须在 sqlite3.connect 之前判断
    db = sqlite3.connect(str(db_file))
    db.row_factory = sqlite3.Row
    info = _migrate(db, created)            # 迁移 + 回填 + 孤儿清理，内部显式 commit()
    _LAST_CONNECT.update(info)              # stats 据此返回 schema_version/migration
    if not info["logged"]:
        _log_migration(info)                # 日志走 stderr，不污染 stdout JSON 协议
    return db
```

要点：
- **首次建库**：`_migrate` 里一次 `executescript(SCHEMA)` 建全表/索引并写 `user_version`，不残留半状态。
- **旧库升级**：按 `user_version` 逐级补块——`if cur < 1` 补齐缺失表/列（`ALTER TABLE ... ADD COLUMN zotero_key` try/except）并回填 `zotero_key`；`if cur < 2` 给 `chunks` 加段落定位列（`para_start` / `para_end`）；`if cur < 3` 加 PDF 页码列（`page_start` / `page_end`）；`if cur < 4` 给 `docs` 加解析器版本列（`indexed_with`，旧行 NULL，`kb_stats` 据此报 `stale_docs`）。四个块都只吞 `duplicate column`，锁冲突等其他 `OperationalError` 上抛（否则版本号已置新而列缺失，后续 INSERT 每行报 `no such column` 且永不再迁移）。
- **显式 `commit()`**：Python sqlite3 的 DML 处于隐式事务，`close()` 会**回滚**未提交修改——旧实现中 backfill/孤儿清理在只读命令下不落盘，重构已修复。
- **日志与提示**：迁移日志走 `sys.stderr`（stdout 是 JSON 协议线）；`kb_stats` 返回 `schema_version` / `migration` / `health`，供宿主与用户感知"库已升级/需重建"。

### 两层迁移机制

| 变更类型 | 机制 | 示例 |
|---|---|---|
| 新增表 | `CREATE TABLE IF NOT EXISTS`（SCHEMA 幂等，每次连接都执行） | docs / chunks / vecs / cache |
| 新增列 | `ALTER TABLE ... ADD COLUMN ...` + 按 `user_version` 门控的迁移块（只吞 `duplicate column`） | 1.5.0 `docs.zotero_key TEXT`；1.6.0 `chunks.para_start/para_end`；1.6.1 `chunks.page_start/page_end`；v4 `docs.indexed_with TEXT` |

### 为什么安全

1. **纯加法式变更**：加表、加可空列都不动旧数据；SQLite 不支持删列/改类型，引擎从不做破坏性 DDL。
2. **旧引擎写新库兼容**：旧版 INSERT 用显式列清单（不含新列）→ 新列落默认 NULL，不报错。
3. **无版本门控的幂等**：迁移语句每次连接都执行，不依赖"当前 schema 版本"判断，因此重复运行/往返升级都无害。

### 当前 schema（1.6.7，`user_version = 4`）

```sql
docs(id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE,
     title TEXT, authors TEXT, year INTEGER, journal TEXT, doi TEXT,
     kind TEXT, sha256 TEXT, size INTEGER, mtime REAL,
     chunk_count INTEGER, indexed_at REAL, zotero_key TEXT,     -- zotero_key 为 1.5.0 新增
     indexed_with TEXT);                                       -- v4 新增：写入该行的解析器版本
chunks(id INTEGER PRIMARY KEY, doc_id REFERENCES docs ON DELETE CASCADE,
       section TEXT NOT NULL, weight REAL NOT NULL DEFAULT 1.0, seq INTEGER NOT NULL, text TEXT NOT NULL,
       para_start INTEGER, para_end INTEGER,                    -- 1.6.0 新增（v2）：段落区间
       page_start INTEGER, page_end INTEGER);                   -- 1.6.1 新增（v3）：PDF 物理页码
vecs(chunk_id INTEGER PRIMARY KEY, vec BLOB NOT NULL);          -- float32 向量
cache(key TEXT PRIMARY KEY, payload TEXT NOT NULL, created REAL NOT NULL);
```

旧数据的两组定位列为 NULL：段落号/页码是**入库时**从 PDF 文本层算出的，需对旧文档执行 `kb_ingest(force=true)` 重灌才有。`docs.indexed_with` 对旧行同样是 NULL，它不参与检索，只用于 `kb_stats` 报出 `stale_docs`（`kb_ingest(metadata_only=true)` 即可补齐该列，无需重灌）。

## 3. 升级注意事项（数据层面）

### 3.1 `zotero_key` 对旧行是 NULL

1.5.0 只在 `kb_zotero` 迁移时写入该列；1.0.5 及更早入库的行该列为 NULL。**1.6.0 起，连接时自动按 storage 路径回填**（`_migrate()` 内 `backfill zotero_key`，只补 NULL，幂等）。若仍有 NULL，多为非 Zotero storage 路径（如 downloads 目录），属正常。

### 3.2 保持引擎版本一致

当前旧引擎写新库安全（新列可空）。**将来若新版本加入 `NOT NULL` 列或约束变更，旧引擎写入会报错**——升级后应保证宿主与引擎同版本，避免旧引擎再写库。

### 3.3 版本号语义

- `kb_engine.py` 的 `VERSION = "3.1.0"` 是**代码版本**（协议/功能层），**不是 schema 版本**。
- schema 版本建议用 SQLite 内置 `PRAGMA user_version` 单独维护（见下节）。

## 4. 破坏性变更的版本化迁移（骨架已实现）

当前 `_migrate()` 已实现 `user_version` 门控与 v0→v1、v1→v2、v2→v3、v3→v4 四个迁移块。若将来需要：改列类型、删列、合并表、加 `NOT NULL` 列、改约束 → 只需在 `_migrate()` 中**新增迁移块**：

### 4.1 迁移骨架（已实现于 kb_engine.py 的 `_migrate()`）

```python
SCHEMA_VERSION = 4          # 目标 schema 版本，随迁移脚本同步递增

def connect(kb_root):
    root = Path(kb_root); root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(root / "kb.sqlite"))
    db.row_factory = sqlite3.Row
    _migrate(db)
    db.execute("DELETE FROM vecs WHERE chunk_id NOT IN (SELECT id FROM chunks)")
    return db

def _migrate(db):
    v = db.execute("PRAGMA user_version").fetchone()[0]
    if v < 1:                                  # v0 → v1：初始表结构
        db.executescript(SCHEMA_V1)
        db.execute("PRAGMA user_version = 1")
    if v < 2:                                  # v1 → v2：破坏性变更示例（重建表）
        db.executescript("""
            BEGIN;
            CREATE TABLE docs_new (...新结构...);
            INSERT INTO docs_new SELECT ... FROM docs;
            DROP TABLE docs;
            ALTER TABLE docs_new RENAME TO docs;
            -- 重建依赖 docs 的索引/触发器
            COMMIT;
        """)
        db.execute("PRAGMA user_version = 2")
    # v < 3 ... 依此类推，每级迁移保持向前兼容且可单独验证
```

### 4.2 版本化迁移的约束

1. **每级迁移独立可验证**：每个 `if v < N` 块对应一次完整、可回滚的变更（建议事务包裹，失败即回滚）。
2. **逐级递增**：`user_version` 单调递增；不跳级、不倒退；迁移代码永不修改已发布的历史块。
3. **幂等保证**：以 `PRAGMA user_version` 为门控，重复运行只执行未达成的级数。
4. **向后兼容**：破坏性变更（删列/改类型）后，旧引擎将无法写新库——需要与宿主版本联动（见 3.2）。
5. **数据校验**：迁移后断言（行数、COUNT 抽样、`PRAGMA integrity_check`）。

### 4.3 变更原则（长期）

- 能加列就不要重建表；能加表就不要动列。
- 新增列一律可空或带默认值，保持旧引擎只读兼容。
- 每个发布版若改了 schema，必须：更新本文档 + 递增 `user_version` + 提供迁移脚本 + 更新设计文档中的 schema 图。

## 5. 升级验证清单

升级前（旧库）：
- [ ] 记录 `kb_stats`（文档数/分块数/向量数）作为基线
- [ ] 备份 `kb.sqlite`（或整库拷贝，102MB 级别）

升级后（新引擎首次打开）：
- [ ] `kb_stats` 数字与基线一致（证明无损迁移）
- [ ] 抽查检索：`kb_search` 命中与升级前一致
- [ ] `sqlite3 .kb/kb.sqlite "PRAGMA table_info(docs)"` 确认新列存在（如 zotero_key / indexed_with）
- [ ] 增量入库一条：确认 skipped/duplicate 判定正常
- [ ] （如用到 Zotero）关闭 Zotero 后跑 `kb_zotero`，确认 `zotero_key` 落库

## 6. 变更记录

| 版本 | schema 变更 | 迁移方式 |
|---|---|---|
| 1.0.5（旧） | docs 无 zotero_key | — |
| 1.5.0 | docs 增 `zotero_key TEXT`（可空） | `ALTER TABLE` + try/except，连接时自动执行 |
| 1.6.0 | `PRAGMA user_version` 门控；v1 = docs.zotero_key + 回填；v2 = chunks.para_start/para_end | `_migrate()` 版本化迁移（已实现，含迁移/健康提示；旧行段落号为 NULL，force 重入库恢复） |
| 1.6.1 | v3 = chunks.page_start/page_end（PDF 物理页码锚点，Zotero `?page=N` 跳页） | 同上，新增 `if cur < 3` 块（旧行页码为 NULL，force 重入库恢复） |
| v4（本版） | v4 = docs.indexed_with（写入该行的解析器版本 `VERSION/revN`，用于检测"老数据是用旧解析器写的"） | 同上，新增 `if cur < 4` 块（旧行为 NULL，`kb_stats` 据此报 `stale_docs`；`kb_ingest(metadata_only=true)` 即可原地补齐，无需重灌） |
| 未来 | （示例）改类型/删列 / refs 引文表（方案 B） | 在 `_migrate()` 新增 `if cur < N` 块（4.1 骨架） |
