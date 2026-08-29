#!/usr/bin/env python3
"""kb-rag MCP server — 本地文献知识库 RAG，通过 Model Context Protocol (stdio) 暴露。

与 DSH 插件共用同一个 kb_engine.py 引擎（常驻守护进程）。7 个工具：
kb_ingest / kb_zotero / kb_search / kb_rag / kb_stats / kb_dedup / kb_clear。

配置为 stdio MCP server 后，Claude Desktop / Cherry Studio / Kimi / DeepSeek /
Zcode 等支持 MCP 的桌面 agent 都可直接调用。运行前先装依赖：pip install -r requirements.txt
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine_client import EngineClient, DEFAULT_KB_ROOT, render_json, render_sources, render_ingest, render_stats

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    sys.stderr.write("缺少依赖：请先 `pip install mcp`（见 requirements.txt）\n")
    raise

mcp = FastMCP("kb-rag")
engine = EngineClient()


def _root(kb_root):
    return kb_root or DEFAULT_KB_ROOT


@mcp.tool()
async def kb_ingest(paths: list[str], kb_root: str = "", force: bool = False) -> str:
    """把本地文档（PDF/TXT/MD/DOCX）导入知识库并建立索引。支持单个文件或目录（递归扫描）；按章节切分并抽取元数据（标题/作者/年份/DOI）；本地 bge-small 模型生成向量（数据持久化在 kb_root）。已入库且内容未变的文件自动跳过；同一内容（sha256 相同）在其他路径已入库时标记 duplicate 跳过（增量）。入库后用 kb_search 检索、kb_rag 问答、kb_stats 看统计。"""
    return render_ingest(await engine.call("ingest", {"paths": paths, "kb_root": _root(kb_root), "force": force}))


@mcp.tool()
async def kb_zotero(zotero_db: str = "", kb_root: str = "", limit: int = 0, force: bool = False, dry_run: bool = False) -> str:
    """把本地 Zotero 文献库中带 PDF 附件的文献批量迁移到知识库。读取 zotero.sqlite（默认自动定位 ~/Zotero 等；找不到时用 zotero_db 显式指定），解析每篇元数据与 PDF 附件路径，逐篇解析入库并生成向量。已入库跳过、重复标记 duplicate（增量）。附件缺失标记 missing 跳过。dry_run=true 只列候选不写入。"""
    return render_ingest(await engine.call("zotero", {
        "zotero_db": zotero_db, "kb_root": _root(kb_root), "limit": limit or None,
        "force": force, "dry_run": dry_run}))


@mcp.tool()
async def kb_search(query: str, top_k: int = 5, snippet: int = 400, mode: str = "hybrid",
                    rerank: bool = True, related: bool = True, kb_root: str = "",
                    authors: str = "", title: str = "", journal: str = "", kind: str = "",
                    section: str = "", year: str = "") -> str:
    """在知识库中做混合检索（关键词 BM25 + 向量余弦 RRF 融合 + bge-reranker 精排），返回最相关片段及精确来源（文件/标题/作者/年份/期刊/DOI/章节/得分）。query 可以是术语、数值、化学式或中文短语。mode 可选 keyword/vector/hybrid（默认 hybrid）。filters 用 authors/title/journal/kind/section/year（year 可用 \">=2020\" 形式）做元数据预过滤。related=true 附带关联文献列表（同作者/同期刊/年份相近/主题相似）。回答用户时必须标注来源：有 DOI 用 [作者, 年份, 期刊](https://doi.org/DOI)，无 DOI 用 [作者, 年份, 文件名]。"""
    filters = {k: v for k, v in [("authors", authors), ("title", title), ("journal", journal),
                                 ("kind", kind), ("section", section), ("year", year)] if v}
    resp = await engine.call("search", {
        "query": query, "top_k": top_k, "snippet": snippet, "mode": mode,
        "rerank": rerank, "related": related, "kb_root": _root(kb_root), "filters": filters})
    return render_sources(resp)


@mcp.tool()
async def kb_rag(query: str, top_k: int = 3, rerank: bool = True, related: bool = True,
                 kb_root: str = "", authors: str = "", title: str = "", journal: str = "",
                 kind: str = "", section: str = "", year: str = "") -> str:
    """在知识库中检索证据片段（混合检索 + 精排，默认 Top-3）供直接作答：基于 evidence 回答，每个事实标注引用编号 [n]。引用写成可点击 markdown：[作者, 年份, 期刊](https://doi.org/DOI)；无 DOI 写成 [作者, 年份, 文件名]。资料不足明确说\"根据现有资料无法回答\"；多源冲突分别列出。答案末尾的补充建议参考 related 关联文献列表。"""
    filters = {k: v for k, v in [("authors", authors), ("title", title), ("journal", journal),
                                 ("kind", kind), ("section", section), ("year", year)] if v}
    resp = await engine.call("rag", {
        "query": query, "top_k": top_k, "rerank": rerank, "related": related,
        "kb_root": _root(kb_root), "filters": filters})
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


if __name__ == "__main__":
    mcp.run()
