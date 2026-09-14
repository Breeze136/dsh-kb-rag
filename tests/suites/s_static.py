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

    # —— 两半边逻辑漂移：可选服务不得用属性访问 ——
    # 背景：1.6.7 的 npm 半边把 `ctx.get('commands')` 写成了 `ctx.commands`，而该名字没在
    # inject 里声明。Cordis 的 Guard 对未声明服务的**属性访问**直接抛错，于是整个插件树加载
    # 失败、profile 起不来（`cannot get property "commands" without inject`）。动态半边写对了，
    # 静态半边没有 —— 两份实现是手工并行维护的（没有生成脚本），所以会悄悄漂移。
    # 判据：每个半边的 `ctx.<name>` 属性访问，必须能用「本文件自己声明的 inject」或
    #       「plugin/host.js 也这么用（它在沙箱 Guard 下真跑过，用错会立刻抛）」解释。
    # 注意必须先剥注释：注释里写的 `ctx.commands` 和 `inject: ['commands']` 会把判据带偏。
    def _strip_js_comments(text):
        text = re.sub(r"/\*[\s\S]*?\*/", " ", text)
        return re.sub(r"(^|[^:])//[^\n]*", r"\1 ", text, flags=re.M)

    def _prop_names(text):
        return set(re.findall(r"\bctx\.([A-Za-z_$][\w$]*)", _strip_js_comments(text)))

    def _inject_names(text):
        out = set()
        for m in re.finditer(r"inject\s*[:=]\s*\[([^\]]*)\]", _strip_js_comments(text)):
            out |= set(re.findall(r"['\"]([^'\"]+)['\"]", m.group(1)))
        return out

    host_props = _prop_names((REPO / "plugin" / "host.js").read_text(encoding="utf-8"))
    for rel in ("npm-package/lib/index.js", "plugin/client.js", "npm-package/lib/client.js"):
        text = (REPO / rel).read_text(encoding="utf-8")
        unexplained = sorted(n for n in _prop_names(text)
                             if n not in _inject_names(text) and n not in host_props)
        ctx.check("%s 没有未声明 inject 的 ctx 属性访问（与 host.js 无漂移）" % rel,
                  not unexplained, "、".join(unexplained))

    # —— 部署指示必须带上 Node 下载器 ——
    # 引擎的 _node_doi_pdf_script() 只在**引擎所在目录**旁找 scripts/doi_pdf.mjs（其次 tools/、同级），
    # 而手工部署路线只把 kb_engine.py 放进工作区。漏了下载器**不会报错**，只会静默退化成内置的
    # Python 兜底：裸 arXiv ID 直接失败、文件名丢标题、候选源与反爬处理都变弱。
    # 所以「放引擎」这一步必须同时交代它 —— 这条断言就是替用户盯着这件事。
    qs = (REPO / "QUICKSTART.md").read_text(encoding="utf-8")
    step = next((l for l in qs.splitlines() if "放引擎" in l), "")
    ctx.check("QUICKSTART 的「放引擎」步骤同时交代了 doi_pdf.mjs（否则下载器静默失效）",
              "doi_pdf" in step, step[:140] or "没找到「放引擎」这一步")

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
