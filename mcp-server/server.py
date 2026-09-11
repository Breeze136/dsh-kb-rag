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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine_client import (EngineClient, DEFAULT_KB_ROOT, render_json, render_sources,
                           render_ingest, render_stats, render_fetch, render_async,
                           render_status)

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    sys.stderr.write("缺少依赖：请先 `pip install mcp`（见 requirements.txt）\n")
    raise

mcp = FastMCP("kb-rag")
engine = EngineClient()


def _root(kb_root):
    return kb_root or DEFAULT_KB_ROOT


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


@mcp.tool()
async def kb_ingest(paths: list[str], kb_root: str = "", force: bool = False,
                    metadata_only: bool = False, rebuild: bool = False,
                    async_mode: bool = False) -> str:
    """把本地文档（PDF/TXT/MD/DOCX）导入知识库并建立索引。支持单个文件或目录（递归扫描）；按章节切分并抽取元数据（标题/作者/年份/DOI）；本地 bge-small 模型生成向量（数据持久化在 kb_root）。已入库且内容未变的文件自动跳过；同一内容（sha256 相同）在其他路径已入库时标记 duplicate 跳过（增量）。
    大批量自动转后台：当待处理文件很多（目录或 ≥KB_ASYNC_THRESHOLD 个文件，默认 25），本工具自动改用后台执行并立即返回 job_id——宿主单次调用超时（如 Kimi Work 60s）不影响入库，随后用 kb_status(job_id=...) 轮询直到 status=done 拿到 totals。文件少时同步执行直接返回结果。显式 async_mode=true 强制后台，false 强制同步。
    metadata_only=true：只刷新元数据（重抽标题/作者/年份/期刊/DOI，不重切块、不重嵌入；实测约 90 ms/篇）——引擎的解析改进不会自动作用于老库，用它让已有文档受益；rebuild=true：按库内现有路径原地重灌全部已入库文档（此时 paths 可省略；不要改成传目录，force 会绕过去重检测而重复入库）。两者可组合使用。"""
    payload = {"paths": paths, "kb_root": _root(kb_root), "force": force,
               "metadata_only": metadata_only, "rebuild": rebuild}
    effective_async = async_mode or _should_async(paths)
    if effective_async:
        return render_async(await engine.call("ingest_async", dict(payload, command="ingest")))
    return render_ingest(await engine.call("ingest", payload))


@mcp.tool()
async def kb_status(job_id: str, kb_root: str = "") -> str:
    """查询后台入库任务状态（配合 kb_ingest async_mode=true 使用）。job_id 来自 async 返回。running 时返回已处理进度；done 时返回入库 totals 与最近文件。"""
    return render_status(await engine.call("status", {"job_id": job_id, "kb_root": _root(kb_root)}))


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
async def kb_stats(kb_root: str = "") -> str:
    """查看知识库统计：文档数、分块数、向量数、最近入库列表及数据库位置。检索无命中时先调它确认库里有什么。"""
    return render_stats(await engine.call("stats", {"kb_root": _root(kb_root)}))


@mcp.tool()
async def kb_dedup(kb_root: str = "") -> str:
    """清理知识库中的重复文档：删除 sha256 与早期文档相同的后来入库项（保留最早 id），同步清除其分块/向量/缓存。返回 removed 与当前总数。反复调用安全。"""
    return render_json(await engine.call("dedup", {"kb_root": _root(kb_root)}))


@mcp.tool()
async def kb_clear(kb_root: str = "", confirm: bool = False) -> str:
    """清空知识库中的全部文献与索引（不可恢复）。必须显式传 confirm=true 才会执行，否则拒绝。"""
    return render_json(await engine.call("clear", {"kb_root": _root(kb_root), "confirm": confirm}))


@mcp.tool()
async def kb_fetch(identifiers: list[str], target_dir: str = "") -> str:
    """按 DOI / arXiv ID 把论文 PDF 下载到本地目录（默认 ~/.kb-rag/downloads，可用 target_dir 覆盖）。按标准元标签与公开 API 解析地址，顺序为：arXiv 直连 → 出版商正式版（落地页 citation_pdf_url；在校园网/机构订阅网络下可直接取得订阅版 PDF，无需额外配置）→ 落地页内常见 pdf 链接 → 开放获取兜底（Unpaywall / Crossref）。只做常规抓取，不绕过付费墙、不访问 Sci-Hub、不伪造凭据。下载后不会自动进 Zotero——需用户手动在 Zotero 里「文件→添加文件」或拖入该目录 PDF 入库。"""
    return render_fetch(await engine.call("fetch", {"identifiers": identifiers, "target_dir": target_dir or None}))


if __name__ == "__main__":
    mcp.run()
