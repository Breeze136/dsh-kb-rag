"""静态一致性：语法、同哈希、JSON、提示镜像、manifest 与引擎命令表对齐。

这些是"发布前必然要过"的硬门槛，也是最便宜的保底：任何一处不一致都说明仓库处于半改状态。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests" / "suites"))
from _helpers import ENGINE, load_engine                                # noqa: E402

SUITE = {"id": "static", "title": "静态一致性（语法/哈希/JSON/镜像/manifest）", "tags": ["fast"]}


def _node_check(path: Path):
    p = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return p.returncode, (p.stderr or p.stdout).strip()[:200]


def run(ctx):
    # —— Python 语法 ——
    for rel in ("kb_engine.py", "npm-package/kb_engine.py", "mcp-server/server.py"):
        f = REPO / rel
        p = subprocess.run([sys.executable, "-m", "py_compile", str(f)], capture_output=True, text=True)
        ctx.check("py_compile %s" % rel, p.returncode == 0, (p.stderr or "")[:200])

    # —— 双份引擎同哈希（前置检查已查一遍，这里作为用例留痕）——
    a = (REPO / "kb_engine.py").read_bytes()
    b = (REPO / "npm-package" / "kb_engine.py").read_bytes()
    ctx.check("kb_engine.py 与 npm 副本逐字节一致", a == b)

    # —— JS 语法 ——
    for rel in ("npm-package/lib/index.js", "npm-package/lib/guidance.js", "npm-package/lib/client.js",
                "tools/sync-host-guidance.mjs"):
        rc, err = _node_check(REPO / rel)
        ctx.check("node --check %s" % rel, rc == 0, err)

    # —— host.js / 动态客户端半边：宿主按"函数体"加载，用 new Function 验 ——
    js = ("const fs=require('fs');"
          "for (const f of ['plugin/host.js','plugin/client.js']) {"
          " new Function(fs.readFileSync(f,'utf8')); console.log('ok '+f); }")
    p = subprocess.run(["node", "-e", js], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=str(REPO))
    ctx.check("host.js 与动态 client.js 作为函数体可解析", p.returncode == 0, (p.stderr or "")[:200])

    # —— JSON ——
    for rel in ("npm-package/package.json", "plugin/kbrag.plugin.json"):
        try:
            json.loads((REPO / rel).read_text(encoding="utf-8"))
            ctx.check("JSON 可解析 %s" % rel, True)
        except Exception as e:                                    # noqa: BLE001
            ctx.check("JSON 可解析 %s" % rel, False, str(e)[:160])

    # —— manifest 的 engine.commands 必须都在引擎的命令表里 ——
    eng = load_engine("kbe_static")
    manifest = json.loads((REPO / "plugin/kbrag.plugin.json").read_text(encoding="utf-8"))
    cmds = (manifest.get("engine") or {}).get("commands") or []
    src = ENGINE.read_text(encoding="utf-8")
    registered = set(re.findall(r'"(\w+)": cmd_\w+', src))
    # serve 不在 handler 表里，由 main() 的 `if command == "serve"` 分支处理
    if 'command == "serve"' in src:
        registered.add("serve")
    missing = [c for c in cmds if c not in registered]
    ctx.check("manifest 的 engine.commands 都在引擎命令表里", not missing,
              "缺登记：%s（引擎有：%s）" % (missing, sorted(registered)))
    ctx.check("引擎命令表里的命令也都在 manifest 里（双向一致）",
              not [c for c in registered if c not in cmds],
              "manifest 未列：%s" % sorted(c for c in registered if c not in cmds))

    # —— 工具清单与注册数 ——
    tools = manifest.get("tools") or []
    ctx.check("manifest 列出 10 个工具", len(tools) == 10, len(tools))
    idx = (REPO / "npm-package" / "lib" / "index.js").read_text(encoding="utf-8")
    reg = re.findall(r'reg\(tool\(\{\s*\n\s*name: "(\w+)"', idx)
    ctx.check("lib/index.js 注册的工具与 manifest 一致", sorted(reg) == sorted(tools),
              "注册 %s" % sorted(reg))

    # —— 引擎版本/解析器版本 ——
    ctx.check("引擎 VERSION 与 package.json 同步（3.2.0）", eng.VERSION == "3.2.0", eng.VERSION)
    ctx.check("PARSER_REV 已提升到 5（References 判定改动）", eng.PARSER_REV == 5, eng.PARSER_REV)
    ctx.check("CHUNK_AFFECTING_REVS 含 5", 5 in eng.CHUNK_AFFECTING_REVS,
              sorted(eng.CHUNK_AFFECTING_REVS))

    # —— 隐私约定（AGENTS.md）：tracked 文件里不得出现本机路径 ——
    tracked = subprocess.run(["git", "-C", str(REPO), "ls-files"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace").stdout.split()
    bad = []
    pat = re.compile(r"C:\\+Users\\+[A-Za-z0-9_.\-]+|/home/[A-Za-z0-9_.\-]+|/Users/[A-Za-z0-9_.\-]+")
    for rel in tracked:
        f = REPO / rel
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        m = pat.search(text)
        if m:
            bad.append("%s: %s" % (rel, m.group(0)))
    ctx.check("tracked 文件里没有本机绝对路径", not bad, "；".join(bad[:5]))
