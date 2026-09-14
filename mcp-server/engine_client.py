#!/usr/bin/env python3
"""Engine client + result rendering for the kb-rag MCP server.

This module is dependency-free (only stdlib) so it can be tested against the
real kb_engine.py without the `mcp` SDK installed. It spawns the engine's
`serve` daemon and forwards tool calls over its JSON-lines protocol.
"""
import asyncio
import collections
import json
import os
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent / "kb_engine.py"
#: 与 ENGINE 同一个对象，供调用方（server.py 的能力探测）表达"引擎是否存在"。
ENGINE_PATH = ENGINE
DEFAULT_KB_ROOT = os.environ.get("KB_RAG_ROOT", str(Path.home() / ".kb-rag"))
# 默认用当前解释器（MCP 服务由哪个 Python 拉起就用哪个），避免裸 "python" 命中错误解释器；
# 可用 KB_RAG_PYTHON 显式覆盖。
PYTHON = os.environ.get("KB_RAG_PYTHON") or sys.executable

#: stderr 尾部保留的块数与单次读取字节数：8×4KB 上限，足够容纳一个 traceback 的结尾。
_STDERR_CHUNK = 4096
_STDERR_KEEP = 8


class EngineClient:
    """Async client for the resident kb_engine.py daemon (JSON-lines protocol)."""

    def __init__(self, engine_path=None, python=None):
        self.engine_path = str(engine_path or ENGINE)
        self.python = python or PYTHON
        self.proc = None
        self.lock = asyncio.Lock()
        self.seq = 0
        self._buf = b""
        # 引擎 stderr 的尾部。之前是 DEVNULL —— 缺依赖时引擎会在启动瞬间死掉，
        # 调用方只看到"exited unexpectedly"，没有任何可自诊断的信息。
        self._stderr_tail = collections.deque(maxlen=_STDERR_KEEP)
        self._stderr_task = None

    async def _drain_stderr(self, proc):
        """持续读走子进程 stderr（必须读，否则管道写满会把引擎阻塞住），只留尾部。"""
        try:
            while True:
                chunk = await proc.stderr.read(_STDERR_CHUNK)
                if not chunk:
                    return
                self._stderr_tail.append(chunk)
        except Exception:                                         # noqa: BLE001
            return

    def stderr_tail(self, limit=2000):
        """stderr 尾部的可读文本（最多 limit 字符）；没有内容时返回空串。"""
        data = b"".join(self._stderr_tail)
        if not data:
            return ""
        return data.decode("utf-8", errors="replace").strip()[-limit:]

    async def ensure(self):
        if self.proc is not None and self.proc.returncode is None:
            return
        self.proc = await asyncio.create_subprocess_exec(
            self.python, self.engine_path, "serve",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._stderr_tail.clear()
        self._stderr_task = asyncio.ensure_future(self._drain_stderr(self.proc))

    def _death_reason(self):
        """引擎异常退出时的原因串：附 stderr 尾部，缺依赖这类问题能直接看出来。"""
        tail = self.stderr_tail()
        if not tail:
            return ""
        return "；引擎 stderr 尾部：%s" % tail

    async def _restart(self):
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            self._stderr_task = None
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
                    reason = self._death_reason()
                    await self._restart()
                    raise RuntimeError("kb engine daemon read failed: %s%s" % (e, reason)) from e
                if not line:
                    reason = self._death_reason()
                    await self._restart()
                    raise RuntimeError("kb engine daemon exited unexpectedly" + reason)
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


def render_async(resp):
    """async_mode 提交结果：job_id + 轮询指引。"""
    if not isinstance(resp, dict):
        return str(resp)
    if resp.get("ok") is False:
        return "**后台入库启动失败**：%s" % resp.get("error")
    lines = ["**后台入库已启动**"]
    if resp.get("job_id"):
        lines.append("job_id：%s" % resp["job_id"])
    if resp.get("note"):
        lines.append(str(resp["note"]))
    lines.append("下一步：调用 kb_status(job_id=\"%s\") 轮询，直到 status=done 返回 totals。" % resp.get("job_id", ""))
    return "\n".join(lines)


def render_status(resp):
    """kb_status 结果：running 显进度 / done 复用入库汇总。"""
    if not isinstance(resp, dict):
        return str(resp)
    if resp.get("ok") is False:
        return "**查询失败**：%s" % resp.get("error")
    st = resp.get("status")
    if st == "error":
        lines = ["**后台任务失败** · job_id=%s" % resp.get("job_id")]
        if resp.get("error"):
            lines.append(str(resp["error"]))
        if resp.get("note"):
            lines.append(str(resp["note"]))
        return "\n".join(l for l in lines if l)
    if st == "done":
        result = resp.get("result")
        if isinstance(result, dict) and result.get("ok") is False:
            # cmd_ingest 入参错误（如空 paths）等：result.ok=False 不是成功，如实呈现失败原因
            lines = ["**后台任务未成功** · job_id=%s" % resp.get("job_id")]
            if result.get("error"):
                lines.append(str(result["error"]))
            if resp.get("error"):
                lines.append(str(resp["error"]))
            return "\n".join(l for l in lines if l)
        head = "**后台入库完成** · job_id=%s" % resp.get("job_id")
        if isinstance(result, dict):
            body = render_ingest(result)
            return head + "\n" + body if body else head
        return head
    if st == "running":
        prog = resp.get("progress") or {}
        lines = ["**后台入库运行中** · job_id=%s" % resp.get("job_id")]
        if "processed" in prog:
            lines.append("已处理 %s 个文件 · 错误 %s · 已生成 %s 块" % (
                prog.get("processed"), prog.get("errors", 0), prog.get("chunks", 0)))
        lines.append(str(resp.get("note") or "请稍后重查"))
        return "\n".join(lines)
    lines = ["**任务状态：%s**" % st]
    if resp.get("note"):
        lines.append(str(resp["note"]))
    return "\n".join(lines)


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
            total_n = resp.get("files_total") or len(files)  # 截断后仍显示真实总数
            lines.append("（共 %d 个文件，仅显示最近 %d 条；完整统计见 kb_stats）" % (total_n, len(tail)))
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
    # ingest=true 时引擎会回传入库结果；旧版本只读 downloaded/target/files/note，
    # 入库到底成了几篇完全不可见 —— 调用方以为只是下载，实际库已经变了。
    ing = resp.get("ingest")
    if isinstance(ing, dict):
        lines.append("")
        lines.append("**下载后已入库**")
        lines.append(render_ingest(ing))
    elif ing:
        lines.append("")
        lines.append("**下载后入库结果**：%s" % str(ing)[:500])
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


def _ref_nums(ns):
    """[4,5,6,9] -> '4–6, 9'（引文汇总行用）。"""
    nums = sorted(int(n) for n in ns if n is not None)
    parts, start, prev = [], None, None
    for x in nums:
        if start is None:
            start = prev = x
        elif x == prev + 1:
            prev = x
        else:
            parts.append(str(start) if start == prev else "%d–%d" % (start, prev))
            start = prev = x
    if start is not None:
        parts.append(str(start) if start == prev else "%d–%d" % (start, prev))
    return ", ".join(parts)


def render_sources(resp):
    """Render search/rag results as markdown (port of the DSH renderSources)."""
    if not isinstance(resp, dict):
        return str(resp)
    items = resp.get("evidence") or resp.get("results") or []
    if not items:
        return render_json(resp)
    lines = []
    lines.append("**知识库来源 Top-%d**" % len(items))
    # 语言提示（引擎零成本检测）：query 含 CJK 且库内中文占比极低时给出改写建议，
    # 引擎按原样检索、不翻译，所以这条只提示、不改变结果。
    if isinstance(resp.get("lang_note"), str) and resp["lang_note"]:
        lines.append("提示：" + resp["lang_note"])
    # 查询/详细双模式：quick 压缩输出（无引文链/关联文献、短片段），deep 全量
    depth = resp.get("depth")
    quick = depth == "quick"
    # score 仅在精排后显示（bge 余弦相似度可校准；RRF 融合分无绝对含义，显示反而误导）
    score_note = " · score %s" if resp.get("reranker") else ""
    meta = []
    if quick:
        meta.append("快速检索")
    elif depth == "deep":
        meta.append("深度检索（deep）")
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
        loc = None
        # 页码是"快速定位"的首选锚点（PDF 物理页，可 Zotero ?page=N 跳页）；段落号降级辅助
        page = r.get("page")
        if isinstance(page, list) and len(page) == 2 and all(isinstance(x, int) for x in page):
            a, b = page
            loc = ("§%s · p.%d" % (r["section"], a)) if a == b else \
                  ("§%s · p.%d–%d" % (r["section"], a, b))
        else:
            para = r.get("para")
            if isinstance(para, list) and len(para) == 2 and all(isinstance(x, int) for x in para):
                a, b = para
                loc = ("§%s 第 %d 段" % (r["section"], a)) if a == b else \
                      ("§%s 第 %d–%d 段" % (r["section"], a, b))
            elif r.get("section"):
                loc = "§" + str(r["section"])
        rest = [x for x in [_authors_short(r.get("authors")), r.get("year"),
                             r.get("journal"), loc] if x]
        lines.append("")
        lines.append("%d. %s%s" % (i, t, (" — " + " · ".join(map(str, rest))) if rest else ""))
        if r.get("snippet"):
            lines.append("> " + str(r["snippet"])[:200 if quick else 280].replace("\n", " "))
        if quick:
            # 快速检索：不带图注/引文链/搜索串，只留来源与片段；无 DOI 时补文件名供引用
            if not doi and r.get("file"):
                lines.append("无 DOI · 文件：%s" % r["file"])
            continue
        if isinstance(r.get("figure"), str) and r["figure"]:
            lines.append("↳ 图注坐标: " + str(r["figure"])[:220])
        # 引文关联：本证据的参考文献条目；库内命中（[库内]）优先展示，未命中折叠到汇总行
        cites = r.get("citations")
        if isinstance(cites, list) and cites:
            hits = [c for c in cites if isinstance(c.get("lib"), dict)]
            others = [c for c in cites if not isinstance(c.get("lib"), dict)]
            lines.append("↳ 引文补充（本证据的参考文献；[库内]=已在库内，可检索引用）" if hits
                         else "↳ 引文补充（本证据的参考文献，供补库/深读）")
            for c in hits[:5] + others[:3]:
                lines.append("  · [Ref %s] %s" % (c.get("n"), str(c.get("text") or "")[:150]))
                lib = c.get("lib")
                if isinstance(lib, dict):
                    lt = str(lib.get("title") or "")
                    ldoi = lib.get("doi") if isinstance(lib.get("doi"), str) and lib.get("doi") else None
                    if ldoi:
                        lt = "[%s](https://doi.org/%s)" % (lt, ldoi)
                    lmeta = " · ".join(str(x) for x in [
                        _authors_short(lib.get("authors")), lib.get("year"),
                        lib.get("journal")] if x)
                    tail = "（即本证据的 Ref %s，可检索引用）" % c.get("n")
                    if lib.get("zotero_key"):
                        tail += " · [Zotero 打开](zotero://open-pdf/library/items/%s)" % lib["zotero_key"]
                    lines.append("    [库内] %s%s%s" % (
                        lt, ("（%s）" % lmeta) if lmeta else "", tail))
            rest = hits[5:] + others[3:]
            if rest:
                lines.append("  ↳ 另有 %d 条引文未展开（Ref %s），补库时可按编号定位" % (
                    len(rest), _ref_nums([c.get("n") for c in rest])))
        if doi:
            lines.append("[DOI %s](https://doi.org/%s)%s" % (
                doi, doi, score_note % r.get("score") if score_note else ""))
        else:
            lines.append("无 DOI%s · 文件：%s" % (
                score_note % r.get("score") if score_note else "", r.get("file") or ""))
            if isinstance(r.get("search"), str) and r["search"]:
                lines.append("↳ 搜索串（Scholar 可复制）: " + str(r["search"])[:200])
    if quick:
        lines.append("")
        lines.append("（快速检索：直接输出查到的信息即可，一两句作答，不展开分析；需深度调研时用 depth=deep 重查）")
        return "\n".join(lines)
    related = resp.get("related") or []
    if related:
        lines.append("")
        lines.append("**关联文献（可作补充建议）**")
        for r in related:
            title = str(r.get("title") or r.get("file") or "")
            doi = r.get("doi") if isinstance(r.get("doi"), str) and r["doi"] else None
            t = "[%s](https://doi.org/%s)" % (title, doi) if doi else title
            rest = [x for x in [_authors_short(r.get("authors"), 2), r.get("year"), r.get("journal")] if x]
            lines.append("- %s%s（%s）" % (
                t, (" — " + " · ".join(map(str, rest))) if rest else "",
                r.get("reason") or "内容相关"))
    return "\n".join(lines)
