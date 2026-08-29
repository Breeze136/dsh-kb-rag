#!/usr/bin/env python3
"""Engine client + result rendering for the kb-rag MCP server.

This module is dependency-free (only stdlib) so it can be tested against the
real kb_engine.py without the `mcp` SDK installed. It spawns the engine's
`serve` daemon and forwards tool calls over its JSON-lines protocol.
"""
import asyncio
import json
import os
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent / "kb_engine.py"
DEFAULT_KB_ROOT = os.environ.get("KB_RAG_ROOT", str(Path.home() / ".kb-rag"))
PYTHON = os.environ.get("KB_RAG_PYTHON") or "python"


class EngineClient:
    """Async client for the resident kb_engine.py daemon (JSON-lines protocol)."""

    def __init__(self, engine_path=None, python=None):
        self.engine_path = str(engine_path or ENGINE)
        self.python = python or PYTHON
        self.proc = None
        self.lock = asyncio.Lock()
        self.seq = 0
        self._buf = b""

    async def ensure(self):
        if self.proc is not None and self.proc.returncode is None:
            return
        self.proc = await asyncio.create_subprocess_exec(
            self.python, self.engine_path, "serve",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def _restart(self):
        if self.proc is not None:
            try:
                self.proc.kill()
            except Exception:
                pass
            try:
                await self.proc.wait()
            except Exception:
                pass
            self.proc = None

    async def _readline(self):
        """Read one newline-delimited line with NO 64KB cap (engine responses can be large)."""
        while b"\n" not in self._buf:
            chunk = await self.proc.stdout.read(65536)
            if not chunk:
                line, self._buf = self._buf, b""
                return line
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return line

    async def call(self, command, payload):
        async with self.lock:
            await self.ensure()
            self.seq += 1
            rid = self.seq
            req = json.dumps({"id": rid, "command": command, "payload": payload},
                             ensure_ascii=True)
            try:
                self.proc.stdin.write((req + "\n").encode("utf-8"))
                await self.proc.stdin.drain()
            except Exception as e:
                await self._restart()
                raise RuntimeError("kb engine daemon write failed: %s" % e) from e
            while True:
                try:
                    line = await self._readline()
                except Exception as e:
                    await self._restart()
                    raise RuntimeError("kb engine daemon read failed: %s" % e) from e
                if not line:
                    await self._restart()
                    raise RuntimeError("kb engine daemon exited unexpectedly")
                try:
                    msg = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                if msg.get("id") != rid:
                    continue
                if msg.get("ok") is not True:
                    raise RuntimeError("kb engine error: %s" % str(msg.get("error"))[:500])
                return msg.get("response", {})


def _authors_short(authors, n=3):
    if not authors:
        return None
    parts = [p.strip() for p in str(authors).split(";") if p.strip()]
    return "; ".join(parts[:n]) or None


def render_json(resp):
    return json.dumps(resp, ensure_ascii=False, default=str, indent=2)


def render_ingest(resp):
    """Compact rolling view for ingest/zotero: one summary line + recent tail."""
    if not isinstance(resp, dict):
        return str(resp)
    totals = resp.get("totals") or {}
    files = resp.get("files") or []
    lines = []
    lines.append("**入库完成** · 新增 %s / 更新 %s / 跳过 %s / 重复 %s / 失败 %s" % (
        totals.get("added", 0), totals.get("updated", 0), totals.get("skipped", 0),
        totals.get("duplicates", 0), totals.get("errors", 0)))
    total_ms = resp.get("ms", 0) or 0
    timing = "%.1fs" % (total_ms / 1000) if total_ms >= 1000 else "%dms" % total_ms
    extra = []
    if resp.get("embedding"):
        extra.append(str(resp["embedding"]))
    if isinstance(totals.get("chunks"), int):
        extra.append("%d 块 / %d 向量" % (totals["chunks"], totals.get("vectors", 0)))
    lines.append("总耗时 %s%s" % (timing, (" · " + " · ".join(extra)) if extra else ""))
    if files:
        lines.append("")
        lines.append("**最近入库（滚动）**")
        tail = list(reversed(files[-8:]))
        icon = {"added": "✓", "skipped": "·", "duplicate": "≈", "error": "✗", "missing": "✗"}
        for f in tail:
            name = str(f.get("path") or "").replace("\\", "/").split("/")[-1]
            st = f.get("status", "·")
            ms = f.get("ms", 0) or 0
            lines.append("%s %s · %dms" % (icon.get(st, "·"), name, ms))
        if len(files) > len(tail):
            lines.append("（共 %d 个文件，仅显示最近 %d 条；完整统计见 kb_stats）" % (len(files), len(tail)))
    if resp.get("note"):
        lines.append(str(resp["note"]))
    return "\n".join(lines)


def render_fetch(resp):
    """Compact kb_fetch view: downloaded/total + per-file status + Zotero reminder."""
    if not isinstance(resp, dict):
        return str(resp)
    lines = []
    lines.append("**下载完成** · %s / %s 篇" % (resp.get("downloaded", 0), resp.get("total", 0)))
    if resp.get("target"):
        lines.append("保存到：%s" % resp["target"])
    for f in resp.get("files") or []:
        icon = "✓" if f.get("status") == "downloaded" else "✗"
        name = str(f.get("path") or "").replace("\\", "/").split("/")[-1] if f.get("path") else str(f.get("id") or "")
        lines.append("%s %s%s" % (icon, name, (" · " + str(f["error"])[:90]) if f.get("error") else ""))
    if resp.get("note"):
        lines.append("")
        lines.append(str(resp["note"]))
    return "\n".join(lines)


def render_stats(resp):
    """Compact kb_stats view: totals + recent tail, not the full recent list."""
    if not isinstance(resp, dict):
        return str(resp)
    lines = []
    lines.append("**知识库统计** · %s 文档 / %s 块 / %s 向量" % (
        resp.get("docs", 0), resp.get("chunks", 0), resp.get("vectors", 0)))
    if resp.get("db"):
        lines.append("数据库：%s" % resp["db"])
    recent = resp.get("recent") or []
    if recent:
        lines.append("")
        lines.append("**最近入库**")
        for r in recent[:10]:
            name = str(r.get("file") or "").replace("\\", "/").split("/")[-1]
            lines.append("- %s · %s · %s 块" % (name, r.get("year") or "-", r.get("chunks") or 0))
        if len(recent) > 10:
            lines.append("（共 %d 条，仅显示最近 10 条）" % len(recent))
    return "\n".join(lines)


def render_sources(resp):
    """Render search/rag results as markdown (port of the DSH renderSources)."""
    if not isinstance(resp, dict):
        return str(resp)
    items = resp.get("evidence") or resp.get("results") or []
    if not items:
        return render_json(resp)
    lines = []
    lines.append("**知识库来源 Top-%d**" % len(items))
    meta = []
    if resp.get("reranker"):
        meta.append("精排 " + str(resp["reranker"]).split(" ")[0])
    if resp.get("cached") is True:
        meta.append("缓存命中")
    if isinstance(resp.get("ms"), (int, float)):
        meta.append("%dms" % resp["ms"])
    if resp.get("strict") is True:
        meta.append("严格模式")
    if meta:
        lines.append(" · ".join(meta))
    for i, r in enumerate(items, 1):
        title = str(r.get("title") or r.get("file") or "")
        doi = r.get("doi") if isinstance(r.get("doi"), str) and r["doi"] else None
        t = "[%s](https://doi.org/%s)" % (title, doi) if doi else title
        rest = [x for x in [_authors_short(r.get("authors")), r.get("year"),
                             r.get("journal"), ("§" + r["section"]) if r.get("section") else None] if x]
        lines.append("")
        lines.append("%d. %s%s" % (i, t, (" — " + " · ".join(map(str, rest))) if rest else ""))
        if r.get("snippet"):
            lines.append("> " + str(r["snippet"])[:280].replace("\n", " "))
        if isinstance(r.get("figure"), str) and r["figure"]:
            lines.append("↳ 图注坐标: " + str(r["figure"])[:220])
        if doi:
            lines.append("[DOI %s](https://doi.org/%s) · score %s" % (doi, doi, r.get("score")))
        else:
            lines.append("无 DOI · score %s · 文件：%s" % (r.get("score"), r.get("file") or ""))
            if isinstance(r.get("search"), str) and r["search"]:
                lines.append("↳ 搜索串（Scholar 可复制）: " + str(r["search"])[:200])
    related = resp.get("related") or []
    if related:
        lines.append("")
        lines.append("**关联文献（可作补充建议）**")
        for r in related:
            title = str(r.get("title") or r.get("file") or "")
            doi = r.get("doi") if isinstance(r.get("doi"), str) and r["doi"] else None
            t = "[%s](https://doi.org/%s)" % (title, doi) if doi else title
            rest = [x for x in [_authors_short(r.get("authors"), 2), r.get("year"), r.get("journal")] if x]
            lines.append("- %s%s（%s · score %s）" % (
                t, (" — " + " · ".join(map(str, rest))) if rest else "",
                r.get("reason") or "内容相关", r.get("score")))
    return "\n".join(lines)
