#!/usr/bin/env python3
"""kb-rag MCP server — 本地文献知识库 RAG，通过 Model Context Protocol (stdio) 暴露。

与 DSH 插件共用同一个 kb_engine.py 引擎（常驻守护进程）。9 个工具：
kb_ingest（支持 async_mode 后台执行）/ kb_zotero / kb_search / kb_rag / kb_stats /
kb_dedup / kb_clear / kb_fetch / kb_status（轮询后台任务）。

配置为 stdio MCP server 后，Claude Desktop / Cherry Studio / Kimi / DeepSeek /
Zcode 等支持 MCP 的桌面 agent 都可直接调用。运行前先装依赖：pip install -r requirements.txt

并发说明：引擎为单守护进程、JSON-lines 逐行协议 —— EngineClient 用 asyncio.Lock 串行化调用
（一次仅一个引擎请求在途，避免行交错损坏协议）。MCP 宿主（如 Kimi Work）并发触发的工具调用
会在该锁上排队，不会并发冲击引擎。若宿主有执行超时（如 Kimi Work 60s），请分批入库
（建议每次 ≤5 个文件夹/目录），大批量全量入库请换用无超时限制的环境（如 Kimi Code）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

if sys.version_info < (3, 9):                                     # noqa: UP036
    sys.stderr.write("kb-rag MCP server 需要 Python 3.9+（当前 %s）\n" % sys.version.split()[0])
    raise SystemExit(2)

sys.path.insert(0, str(Path(__file__).resolve().parent))

import availability                                               # noqa: E402

from engine_client import (EngineClient, DEFAULT_KB_ROOT, render_json, render_sources,
                           render_ingest, render_stats, render_fetch, render_async,
                           render_status, ENGINE_PATH)

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    sys.stderr.write("缺少依赖：请先 `pip install mcp`（见 requirements.txt）\n")
    raise

mcp = FastMCP("kb-rag")
engine = EngineClient()

# ── 功能隔离 ────────────────────────────────────────────────────────────────
# 不可用的能力不注册工具（硬隔离），而不是注册一个必然报错的空壳；被隔离的名字与原因
# 由 kb_mcp_status 报告，所以"工具少了"始终可查。结构性差异见 availability.STRUCTURAL_GAPS。
CAPABILITIES = availability.probe_environment(engine_available=ENGINE_PATH.is_file())
ENABLED_TOOLS, QUARANTINED_TOOLS, CAPABILITY_NOTES = availability.quarantine(CAPABILITIES)


def _register(fn):
    """按隔离结论注册工具：不在 ENABLED_TOOLS 里的直接跳过。"""
    if fn.__name__ in ENABLED_TOOLS:
        mcp.tool()(fn)
    else:
        sys.stderr.write("[kb-rag-mcp] 工具 %s 已隔离：%s\n"
                         % (fn.__name__, QUARANTINED_TOOLS.get(fn.__name__, "未启用")))
    return fn


def _root(kb_root):
    return kb_root or DEFAULT_KB_ROOT


def _local_doc_count(db_path):
    """直接只读打开 sqlite 取文档数：状态查询不该有副作用（不拉引擎、更不建库）。"""
    import sqlite3
    try:
        uri = "file:%s?mode=ro" % str(db_path).replace("\\", "/")
        con = sqlite3.connect(uri, uri=True, timeout=1.0)
        try:
            return int(con.execute("select count(*) from docs").fetchone()[0])
        finally:
            con.close()
    except Exception:                                                 # noqa: BLE001
        return None


def _should_async(paths):
    """估算待入库文件数：目录递归扫描、单文件计 1。超过阈值自动转后台，
    避免宿主单次调用超时（Kimi Work 60s 等）导致大批量入库被掐断。
    计数只看扩展名，不做内容读取——轻量、无引擎往返。KB_ASYNC_THRESHOLD 可覆盖（默认 25）。"""
    import os
    try:
        threshold = int(os.environ.get("KB_ASYNC_THRESHOLD", "25"))
    except ValueError:
        threshold = 25
    exts = {".pdf", ".txt", ".md", ".markdown", ".docx"}
    n = 0
    for p in paths or []:
        p = str(p)
        if os.path.isdir(p):
            for root, _dirs, files in os.walk(p):
                for f in files:
                    if os.path.splitext(f)[1].lower() in exts:
                        n += 1
                        if n > threshold:
                            return True
        else:
            n += 1
    return n > threshold


@_register
async def kb_ingest(paths: list[str] | None = None, kb_root: str = "", force: bool = False,
                    metadata_only: bool = False, rebuild: bool = False,
                    async_mode: bool = False) -> str:
    """把本地文档（PDF/TXT/MD/DOCX）导入知识库并建立索引。支持单个文件或目录（递归扫描）；按章节切分并抽取元数据（标题/作者/年份/DOI）；本地 bge-small 模型生成向量（数据持久化在 kb_root）。已入库且内容未变的文件自动跳过；同一内容（sha256 相同）在其他路径已入库时标记 duplicate 跳过（增量）。
    大批量自动转后台：当待处理文件很多（目录或 ≥KB_ASYNC_THRESHOLD 个文件，默认 25），本工具自动改用后台执行并立即返回 job_id——宿主单次调用超时（如 Kimi Work 60s）不影响入库，随后用 kb_status(job_id=...) 轮询直到 status=done 拿到 totals。文件少时同步执行直接返回结果。显式 async_mode=true 强制后台，false 强制同步。
    metadata_only=true：只刷新元数据（重抽标题/作者/年份/期刊/DOI，不重切块、不重嵌入；实测约 90 ms/篇）——引擎的解析改进不会自动作用于老库，用它让已有文档受益；rebuild=true：按库内现有路径原地重灌全部已入库文档（此时 paths 可省略，本参数已改为可选；不要改成传目录，force 会绕过去重检测而重复入库）。rebuild=true 时本工具**强制后台执行**并直接返回 job_id（全量重灌必然超过宿主单次调用超时），用 kb_status 轮询。两者可组合使用。"""
    payload = {"paths": paths or [], "kb_root": _root(kb_root), "force": force,
               "metadata_only": metadata_only, "rebuild": rebuild}
    # rebuild 会重灌库内全部已入库文档，同步跑必然撞宿主单次调用超时 → 强制后台，
    # 与 DSH 半边 async_if_large 的行为对齐（返回 job_id，用 kb_status 轮询）。
    effective_async = async_mode or rebuild or _should_async(paths)
    if effective_async:
        return render_async(await engine.call("ingest_async", dict(payload, command="ingest")))
    return render_ingest(await engine.call("ingest", payload))


@_register
async def kb_status(job_id: str, kb_root: str = "") -> str:
    """查询后台入库任务状态（配合 kb_ingest async_mode=true 使用）。job_id 来自 async 返回。running 时返回已处理进度；done 时返回入库 totals 与最近文件。"""
    return render_status(await engine.call("status", {"job_id": job_id, "kb_root": _root(kb_root)}))


@_register
async def kb_zotero(zotero_db: str = "", kb_root: str = "", limit: int = 0, force: bool = False,
                    dry_run: bool = False, async_mode: bool = False) -> str:
    """把本地 Zotero 文献库中带 PDF 附件的文献批量迁移到知识库。读取 zotero.sqlite（默认自动定位 ~/Zotero 等；找不到时用 zotero_db 显式指定），解析每篇元数据与 PDF 附件路径，逐篇解析入库并生成向量。已入库跳过、重复标记 duplicate（增量）。附件缺失标记 missing 跳过。dry_run=true 只列候选不写入。
    async_mode=true 时整库迁移在后台执行并立即返回 job_id（宿主单次调用超时，如 Kimi Work 60s，下迁移数百篇建议开启），随后用 kb_status(job_id=...) 轮询直到 status=done。dry_run 与 async_mode 不应同时使用。"""
    if async_mode:
        return render_async(await engine.call("ingest_async", {
            "command": "zotero", "zotero_db": zotero_db, "kb_root": _root(kb_root),
            "limit": limit or None, "force": force, "dry_run": dry_run}))
    return render_ingest(await engine.call("zotero", {
        "zotero_db": zotero_db, "kb_root": _root(kb_root), "limit": limit or None,
        "force": force, "dry_run": dry_run}))


@_register
async def kb_search(query: str, depth: str = "quick", top_k: int | None = None,
                    snippet: int | None = None, mode: str = "hybrid",
                    rerank: bool | None = None, related: bool | None = None,
                    kb_root: str = "", authors: str = "", title: str = "",
                    journal: str = "", kind: str = "", section: str = "",
                    year: str = "") -> str:
    """在知识库中做混合检索（关键词 BM25 + 向量余弦 RRF 融合），返回最相关片段及精确来源（文件/标题/作者/年份/期刊/DOI/章节）。depth 双模式：quick（默认）=快速检索，查到信息马上给——工具返回后立即作答，一两句话直接给用户要的信息，不展开背景不做延伸分析；deep=深度检索，bge-reranker 精排 + 引文链 + 关联文献（适合领域调研与综述性问题）。query 用**英文术语串**——库内正文以英文为主，中文问句会让 BM25 关键词路空转、只靠向量侧跨语言匹配，命中明显更差；写法为 3–12 个词，结构「材料/体系 + 方法/工艺 + 性质/表征」（如 "graphene CVD copper single crystal nucleation suppression"），不要用整句问句，年份/期刊/作者请放 filters，需要中文文献时用用户原话另发一条中文查询；引擎按原样检索，不会替你翻译。mode 可选 keyword/vector/hybrid（默认 hybrid）。filters 用 authors/title/journal/kind/section/year（year 可用 ">=2020" 形式）做元数据预过滤；其中 journal 目前只由 Zotero 迁移填充，kb_ingest 入库的文档该字段为 NULL，用它过滤通常零命中。回答用户时必须标注来源：有 DOI 用 [作者, 年份, 期刊](https://doi.org/DOI)，无 DOI 用 [作者, 年份, 文件名]。"""
    filters = {k: v for k, v in [("authors", authors), ("title", title), ("journal", journal),
                                 ("kind", kind), ("section", section), ("year", year)] if v}
    call = {"query": query, "depth": depth, "mode": mode,
            "kb_root": _root(kb_root), "filters": filters}
    # None 表示未传：不入请求，让引擎按 depth 取模式化缺省（top_k/snippet/rerank/related）
    for key, val in [("top_k", top_k), ("snippet", snippet), ("rerank", rerank), ("related", related)]:
        if val is not None:
            call[key] = val
    resp = await engine.call("search", call)
    return render_sources(resp)


@_register
async def kb_rag(query: str, depth: str = "deep", top_k: int | None = None,
                 rerank: bool | None = None, related: bool | None = None,
                 kb_root: str = "", authors: str = "", title: str = "",
                 journal: str = "", kind: str = "", section: str = "",
                 year: str = "") -> str:
    """在知识库中检索证据片段供直接作答：基于 evidence 回答，每个事实标注引用编号 [n]。depth 双模式：deep（默认）=深度检索，精排+引文链+关联文献全开，回答可跨文献综合论述（适合领域调研）；quick=快速检索，仅基于少量证据直接给答案、不展开。filters 用 authors/title/journal/kind/section/year（year 可用 ">=2020" 形式）做元数据预过滤；其中 journal 目前只由 Zotero 迁移填充，kb_ingest 入库的文档该字段为 NULL，用它过滤通常零命中。引用写成可点击 markdown：[作者, 年份, 期刊](https://doi.org/DOI)；无 DOI 写成 [作者, 年份, 文件名]。资料不足明确说\"根据现有资料无法回答\"；多源冲突分别列出。答案末尾的补充建议按来源分三列（哪列为空就整列省略）：①「库内可查（循引文找到）」——citations 里标 [库内] 的文献，必须写出关系链"《被引文献》(作者, 年份) 被 [证据编号] 的引文 Ref n 引用，已在库内"；②「建议补库（循引文发现）」——citations 未命中条目，注明被 Ref n 引用、尚不在库内；③「相关文献」——related 列表（元数据相似）。每条推荐的理由必须写明属于哪种，引文关联的必须带关系链，不得混列。"""
    filters = {k: v for k, v in [("authors", authors), ("title", title), ("journal", journal),
                                 ("kind", kind), ("section", section), ("year", year)] if v}
    call = {"query": query, "depth": depth,
            "kb_root": _root(kb_root), "filters": filters}
    for key, val in [("top_k", top_k), ("rerank", rerank), ("related", related)]:
        if val is not None:
            call[key] = val
    resp = await engine.call("rag", call)
    return render_sources(resp)


@_register
async def kb_stats(kb_root: str = "") -> str:
    """查看知识库统计：文档数、分块数、向量数、最近入库列表及数据库位置。检索无命中时先调它确认库里有什么。"""
    return render_stats(await engine.call("stats", {"kb_root": _root(kb_root)}))


@_register
async def kb_dedup(kb_root: str = "") -> str:
    """清理知识库中的重复文档：删除 sha256 与早期文档相同的后来入库项（保留最早 id），同步清除其分块/向量/缓存。返回 removed 与当前总数。反复调用安全。"""
    return render_json(await engine.call("dedup", {"kb_root": _root(kb_root)}))


@_register
async def kb_clear(kb_root: str = "", confirm: bool = False) -> str:
    """清空知识库中的全部文献与索引（不可恢复）。必须显式传 confirm=true 才会执行，否则拒绝。"""
    return render_json(await engine.call("clear", {"kb_root": _root(kb_root), "confirm": confirm}))


@_register
async def kb_fetch(identifiers: list[str], target_dir: str = "", ingest: bool = False,
                   kb_root: str = "") -> str:
    """按 DOI / arXiv ID 把论文 PDF 下载到本地目录（默认 ~/.kb-rag/downloads，可用 target_dir 覆盖）。按标准元标签与公开 API 解析地址，顺序为：arXiv 直连 → 出版商正式版（落地页 citation_pdf_url；在校园网/机构订阅网络下可直接取得订阅版 PDF，无需额外配置）→ 落地页内常见 pdf 链接 → 开放获取兜底（Unpaywall / Crossref）。只做常规抓取，不绕过付费墙、不访问 Sci-Hub、不伪造凭据。下载后不会自动进 Zotero——需用户手动在 Zotero 里「文件→添加文件」或拖入该目录 PDF 入库。"""
    # 网络环境与 DSH 半边对齐：宿主会告诉引擎当前是校园网还是家宽，从而给出正确的取用提示。
    # MCP 侧没有宿主可问，改用 KB_RAG_NET_ENV 显式声明（未声明即 unknown，引擎按 unknown 处理）。
    return render_fetch(await engine.call("fetch", {
        "identifiers": identifiers, "target_dir": target_dir or None,
        "ingest": bool(ingest), "kb_root": kb_root or None,
        "network": {"env": os.environ.get("KB_RAG_NET_ENV") or None},
    }))


@_register
async def kb_mcp_status() -> str:
    """查看本 MCP 服务的能力与**功能隔离**结果：哪些工具已启用、哪些被隔离及原因、本机环境探测结果、当前知识库路径与库是否存在，以及 DSH 插件侧有而 MCP 结构性没有的能力清单。工具"不见了"或报错难懂时先调它。不查引擎、不写盘。"""
    lines = ["**kb-rag MCP 服务状态**"]
    lines.append("Python：%s · 引擎：%s（%s）" % (
        CAPABILITIES.get("python"), ENGINE_PATH.name,
        "存在" if CAPABILITIES.get("engine") else "缺失"))
    lines.append("")
    lines.append("**已启用工具（%d）**" % len(ENABLED_TOOLS))
    lines.append(" · ".join(ENABLED_TOOLS) or "（无）")
    if QUARANTINED_TOOLS:
        lines.append("")
        lines.append("**已隔离工具（%d）** —— 不注册，调用时工具不存在" % len(QUARANTINED_TOOLS))
        for name in sorted(QUARANTINED_TOOLS):
            lines.append("- %s：%s" % (name, QUARANTINED_TOOLS[name]))
    if CAPABILITY_NOTES:
        lines.append("")
        lines.append("**能力缺口（工具仍可用，但会退化）**")
        for n in CAPABILITY_NOTES:
            lines.append("- " + n)
    lines.append("")
    lines.append("**知识库路径**")
    root = Path(DEFAULT_KB_ROOT)
    dbs = sorted(p.name for p in root.glob("*.sqlite")) if root.is_dir() else []
    lines.append("- 默认根目录：%s（%s）" % (root, "存在" if root.is_dir() else "不存在"))
    lines.append("- 库文件：%s" % (", ".join(dbs) if dbs else "未找到 *.sqlite"))
    if dbs:
        n = _local_doc_count(root / dbs[0])
        if n is not None:
            lines.append("- 默认库文档数：%s" % n)
    # 只报"默认库存在/不存在"是不够的：**存在但和 DSH 用的不是同一个库**才是最常见的坑
    # ——两边都非空，读到的却是完全不同的语料，而且不会有任何报错。
    cwd_kb = Path.cwd() / ".kb"
    cwd_db = cwd_kb / "kb.sqlite"
    try:
        different = cwd_db.is_file() and cwd_db.resolve() != (root / dbs[0]).resolve() if dbs else cwd_db.is_file()
    except OSError:
        different = False
    if different:
        n_cwd = _local_doc_count(cwd_db)
        lines.append("- ⚠ 当前工作目录下还有另一个库：%s（%s 文档）。DSH 插件半边默认用**这一个**，"
                     "而本服务默认用上面的那个 —— 不显式传 kb_root 时两者读的不是同一批文献。"
                     % (cwd_db, "?" if n_cwd is None else n_cwd))
    elif not dbs:
        lines.append("- ⚠ 默认根目录没有库文件：如果你在 DSH 侧入库过，库在**工作区/.kb** 下，"
                     "两条交付面默认不是同一个库 —— 请显式传 kb_root 或设 KB_RAG_ROOT。")
    lines.append("- 覆盖方式：KB_RAG_ROOT 环境变量，或每次调用传 kb_root")
    lines.append("")
    lines.append("**DSH 侧有、MCP 结构性没有的能力（%d 项）**" % len(availability.STRUCTURAL_GAPS))
    for g in availability.STRUCTURAL_GAPS:
        lines.append("- %s —— MCP 侧：%s" % (g["capability"], g["mcp"]))
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
