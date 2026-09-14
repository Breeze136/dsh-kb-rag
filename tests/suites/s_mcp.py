"""MCP 交付面（离线）：功能隔离、kb_mcp_status、渲染器、引擎诊断、rebuild 转后台、3.9 兼容。

仓库原有的 12 个 suite 全都只测 DSH 半边与引擎；`mcp-server/` 这一整条交付面此前**没有任何
自动化覆盖**。本套件把这条线补上，全部离线、不需要真实库（数据类调用走临时库或打桩）。

隔离判定的表达方式是"注册结果"而不是"内部状态"：kb_mcp_status 怎么说不算数，
真在 FastMCP 上注册出几个工具才算数，所以那部分走子进程 + 真实 import。
"""
from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MCP = REPO / "mcp-server"

SUITE = {"id": "mcp", "title": "MCP 交付面（隔离/渲染/诊断/注册结果）",
         "tags": ["fast"], "needs_models": False}


def _load_server():
    """按路径加载 mcp-server/server.py（并把 mcp-server 放进 sys.path 让 engine_client/availability 可导入）。"""
    sys.path.insert(0, str(MCP))
    spec = importlib.util.spec_from_file_location("kb_mcp_server_under_test", MCP / "server.py")
    mod = importlib.util.module_from_spec(spec)
    with redirect_stdout(io.StringIO()):          # 注册期会往 stderr 打隔离日志，别污染测试输出
        spec.loader.exec_module(mod)
    return mod


def _register_in_subprocess(ctx, env_extra):
    """在子进程里真实 import server，回报实际注册到 FastMCP 的工具名。"""
    code = (
        "import sys, json;"
        "sys.path.insert(0, %r);"
        "import server;"
        "tm = getattr(server.mcp, '_tool_manager', None);"
        "names = sorted(tm._tools) if tm is not None and hasattr(tm, '_tools') else [];"
        "print('__REG__' + json.dumps(names))" % str(MCP)
    )
    env = dict(os.environ)
    env.update(env_extra)
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=120, env=env, cwd=str(ctx.sandbox))
    names = None
    for line in (p.stdout or "").splitlines():
        if line.startswith("__REG__"):
            names = json.loads(line[len("__REG__"):])
    return names, (p.stderr or "")


def run(ctx):
    # ── 缺 mcp SDK 就整体跳过 ──────────────────────────────────────────────
    try:
        import mcp  # noqa: F401
    except ImportError:
        ctx.skip("没装 mcp SDK（pip install mcp）")

    buf = io.StringIO()
    with redirect_stdout(buf):
        server = _load_server()
    stderr_log = buf.getvalue()

    # ── ① 基线：默认环境全部启用 ────────────────────────────────────────
    ctx.check("基线：10 个工具全部启用", len(server.ENABLED_TOOLS) == 10, len(server.ENABLED_TOOLS))
    ctx.check("基线：隔离数为 0", len(server.QUARANTINED_TOOLS) == 0, sorted(server.QUARANTINED_TOOLS))
    ctx.check("诊断工具 kb_mcp_status 永远启用（隔离结果唯一出口）",
              "kb_mcp_status" in server.ENABLED_TOOLS)

    # ── ② kb_mcp_status 必须把"路径/隔离/结构性差异"都讲清楚 ────────────
    status = asyncio.run(server.kb_mcp_status())
    ctx.check("kb_mcp_status 报出知识库路径", "知识库路径" in status)
    ctx.check("kb_mcp_status 报出结构性不可用清单", "结构性没有的能力" in status)
    ctx.check("kb_mcp_status 报出默认根目录的可判定状态（有库文件 或 未找到）",
              ("未找到 *.sqlite" in status) or ("默认库文档数" in status),
              [l for l in status.splitlines() if "库文件" in l or "文档数" in l][:1])

    # ── ③ 渲染器：kb_fetch 的 ingest 结果不得被丢弃（曾静默丢弃） ────────
    from engine_client import render_fetch
    out = render_fetch({"ok": True, "downloaded": 1, "total": 1, "target": "D:/x",
                        "files": [{"status": "downloaded", "path": "D:/x/a.pdf"}],
                        "ingest": {"totals": {"added": 1, "updated": 0, "skipped": 0,
                                              "duplicates": 0, "errors": 0,
                                              "chunks": 42, "vectors": 42}, "ms": 1234, "files": []}})
    ctx.check("render_fetch 渲染出「下载后已入库」", "下载后已入库" in out)
    ctx.check("render_fetch 带出新增计数", "新增 1" in out)

    # ── ④ 引擎启动即死时必须带出 stderr 尾部（曾把 stderr 丢进 DEVNULL） ──
    from engine_client import EngineClient
    bad = EngineClient(engine_path=str(ctx.sandbox / "no_such_engine.py"), python=sys.executable)

    async def _boom():
        try:
            await bad.call("stats", {"kb_root": str(ctx.sandbox)})
            return None
        except Exception as ex:                                     # noqa: BLE001
            return str(ex)
        finally:
            await bad._restart()

    msg = asyncio.run(_boom()) or ""
    ctx.check("引擎异常退出时错误里带出 stderr 尾部", "stderr 尾部" in msg, msg[:150])
    ctx.check("stderr 尾部能看到真实原因（找不到文件）",
              ("No such file" in msg) or ("can't open file" in msg), msg[:200])

    # ── ⑤ rebuild 必须转后台（同步跑必撞宿主单次调用超时） ──────────────
    seen = {}
    real_call = server.engine.call

    async def spy(command, payload):
        seen["command"] = command
        return {"job_id": "deadbeef1234", "note": "stub"}

    server.engine.call = spy
    try:
        r5 = asyncio.run(server.kb_ingest(rebuild=True, kb_root=str(ctx.sandbox)))
        ctx.check("rebuild 走 ingest_async 而不是同步 ingest",
                  seen.get("command") == "ingest_async", seen.get("command"))
        ctx.check("rebuild 返回 job_id 指引", "job_id" in r5, r5[:80])
        seen.clear()
        asyncio.run(server.kb_ingest(paths=["x.txt"], kb_root=str(ctx.sandbox)))
        ctx.check("小批量仍走同步 ingest", seen.get("command") == "ingest", seen.get("command"))
    finally:
        server.engine.call = real_call

    # ── ⑥ Python 3.9 兼容：注解必须是字符串（PEP 604 在定义期求值会 TypeError） ──
    ann = getattr(server.kb_search, "__annotations__", {})
    ctx.check("kb_search 的注解全是字符串（from __future__ import annotations 生效）",
              bool(ann) and all(isinstance(v, str) for v in ann.values()),
              {k: v for k, v in list(ann.items())[:3]})

    # ── ⑦ 隔离必须体现在**真实注册结果**上（子进程真 import） ────────────
    base, _ = _register_in_subprocess(ctx, {})
    ctx.check("无环境变量时真注册 10 个", base is not None and len(base) == 10, base)

    ex, _ = _register_in_subprocess(ctx, {"KB_MCP_EXCLUDE": "kb_zotero,kb_fetch"})
    ctx.check("KB_MCP_EXCLUDE=kb_zotero,kb_fetch → 真注册 8 个",
              ex is not None and len(ex) == 8, ex)
    ctx.check("被排除的工具确实不在注册结果里",
              ex is not None and "kb_zotero" not in ex and "kb_fetch" not in ex, ex)

    allow, _ = _register_in_subprocess(ctx, {"KB_MCP_TOOLS": "kb_search,kb_rag,kb_mcp_status"})
    ctx.check("KB_MCP_TOOLS 白名单 → 真注册恰好 3 个",
              allow == ["kb_mcp_status", "kb_rag", "kb_search"], allow)

    noprobe, _ = _register_in_subprocess(
        ctx, {"KB_MCP_NO_PROBE": "1", "KB_RAG_OFFLINE": "1"})
    ctx.check("KB_MCP_NO_PROBE=1 → 跳过能力探测，仍注册 10 个",
              noprobe is not None and len(noprobe) == 10, noprobe)
