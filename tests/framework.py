"""最小测试框架（零依赖，纯标准库）。

设计目标（对应"保底机制"）：
  · 每个用例都能被单独跑、单独复跑：run.py --only <子串>
  · 每个 suite 有超时（默认 300 s；标了 slow 的 1800 s），超时算失败而不是挂死
  · 失败不会中断整轮（默认继续跑完再汇总），--fail-fast 可改
  · 结果同时进终端、JSON 报告与每 suite 的日志文件，便于复现
  · suite 可以 skip（缺依赖/缺真实库/缺模型），skip 不算失败但会在汇总里单列

suite 约定（tests/suites/s_*.py）：
    SUITE = {"id": "chunking", "title": "...", "tags": ["fast"], "needs_models": False,
             "needs_real_kb": False}
    def run(ctx):            # ctx.check / ctx.skip / ctx.info / ctx.repo / ctx.sandbox
        ctx.check("标签", True, "细节")
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]          # <repo>/tests/framework.py → <repo>
TESTS = REPO / "tests"
SANDBOX_ROOT = TESTS / "_sandbox"
REPORTS_ROOT = TESTS / "_reports"


class SkipSuite(Exception):
    """suite 级跳过（缺真实库、缺模型、缺 node…）。"""


class Ctx:
    """一个 suite 的运行上下文与断言入口。"""

    def __init__(self, suite_id, record, sandbox: Path, extras=None):
        self.id = suite_id
        self.record = record
        self.sandbox = sandbox
        self.repo = REPO
        # 框架注入的只读资源（如真实库的沙箱副本），避免 suite 自己去碰真实库
        for k, v in (extras or {}).items():
            setattr(self, k, v)

    # —— 断言 ——
    def check(self, label, ok, detail=""):
        ok = bool(ok)
        self.record["checks"].append({"label": str(label), "ok": ok, "detail": str(detail)[:400]})
        print(("  PASS  " if ok else "  FAIL  ") + str(label) + ((" | " + str(detail)[:200]) if detail else ""),
              flush=True)
        return ok

    def info(self, msg):
        print("  ·  " + str(msg)[:300], flush=True)

    def skip(self, reason):
        raise SkipSuite(str(reason))

    # —— 工具 ——
    def tmpdir(self, name):
        p = self.sandbox / name
        p.mkdir(parents=True, exist_ok=True)
        return p

    def node(self, script: Path, *args, timeout=300):
        """跑一个 node 脚本，返回 (exit_code, stdout, stderr)。"""
        try:
            p = subprocess.run(["node", str(script), *[str(a) for a in args]],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=timeout, cwd=str(self.sandbox))
            return p.returncode, p.stdout, p.stderr
        except FileNotFoundError:
            self.skip("没有 node")
        except subprocess.TimeoutExpired:
            return 124, "", "TIMEOUT after %ss" % timeout

    def python(self, script: Path, *args, timeout=600, env=None):
        e = dict(os.environ)
        if env:
            e.update(env)
        try:
            p = subprocess.run([sys.executable, str(script), *[str(a) for a in args]],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=timeout, cwd=str(self.sandbox), env=e)
            return p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired:
            return 124, "", "TIMEOUT after %ss" % timeout


def new_record(suite):
    return {"id": suite["id"], "title": suite.get("title", suite["id"]),
            "tags": suite.get("tags", []), "status": "pending",
            "checks": [], "skipped": None, "error": None, "seconds": 0.0, "log": None}


def discover(pattern=None, include_slow=False):
    """发现 tests/suites 下的用例。返回 [(kind, path, suite_meta)]，kind ∈ {py, mjs}。"""
    out = []
    for p in sorted((TESTS / "suites").iterdir()):
        if p.suffix not in (".py", ".mjs") or p.name.startswith("_"):
            continue
        if pattern and pattern not in p.name:
            continue
        if p.suffix == ".py":
            meta = _load_py_meta(p)
            if meta is None:
                continue
            if "slow" in meta.get("tags", []) and not include_slow:
                continue
            out.append(("py", p, meta))
        else:
            meta = {"id": p.stem.lstrip("s_"), "title": p.stem, "tags": [], "node": True,
                    "slow": ".slow." in p.name}
            if meta["slow"] and not include_slow:
                continue
            out.append(("mjs", p, meta))
    return out


def _load_py_meta(path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("suite_" + path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:                                        # noqa: BLE001
        print("!! 导入 suite 失败 %s: %s" % (path.name, e))
        return None
    return getattr(mod, "SUITE", None)


def run_py_suite(path, meta, timeout, sandbox, extras=None):
    import importlib.util
    rec = new_record(meta)
    spec = importlib.util.spec_from_file_location("suite_" + path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    ctx = Ctx(meta["id"], rec, sandbox, extras)
    t0 = time.time()
    try:
        mod.run(ctx)
        rec["status"] = "pass" if all(c["ok"] for c in rec["checks"]) else "fail"
    except SkipSuite as e:
        rec["status"] = "skip"
        rec["skipped"] = str(e)
    except Exception as e:                                        # noqa: BLE001
        import traceback
        rec["status"] = "error"
        rec["error"] = "%s: %s" % (type(e).__name__, e)
        traceback.print_exc()
    rec["seconds"] = round(time.time() - t0, 1)
    if rec["seconds"] > timeout:
        rec["status"] = "fail"
        rec["error"] = "超过 suite 超时 %ss" % timeout
    return rec


NODE_RESULT_MARK = "__SUITE_RESULT__ "


def run_node_suite(path, meta, timeout, sandbox):
    """node suite 约定：最后一行打印 __SUITE_RESULT__ {"checks":[{"label":…,"ok":…}],"skip":"…"}"""
    rec = new_record(meta)
    t0 = time.time()
    try:
        p = subprocess.run(["node", str(path)], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, cwd=str(sandbox))
    except FileNotFoundError:
        rec["status"] = "skip"
        rec["skipped"] = "没有 node"
        return rec
    except subprocess.TimeoutExpired:
        rec["status"] = "fail"
        rec["error"] = "TIMEOUT after %ss" % timeout
        rec["seconds"] = round(time.time() - t0, 1)
        return rec
    out, err = p.stdout, p.stderr
    payload = None
    for line in out.splitlines():
        if line.startswith(NODE_RESULT_MARK):
            try:
                payload = json.loads(line[len(NODE_RESULT_MARK):])
            except Exception:                                     # noqa: BLE001
                payload = None
    if payload is None:
        rec["status"] = "error"
        rec["error"] = "node suite 没有输出结果标记（exit=%s）" % p.returncode
        rec["tail"] = (out[-500:] + err[-500:])
    elif payload.get("skip"):
        rec["status"] = "skip"
        rec["skipped"] = payload["skip"]
    else:
        rec["checks"] = [{"label": c.get("label", "?"), "ok": bool(c.get("ok")), "detail": c.get("detail", "")}
                         for c in payload.get("checks", [])]
        rec["status"] = "pass" if rec["checks"] and all(c["ok"] for c in rec["checks"]) else "fail"
    rec["seconds"] = round(time.time() - t0, 1)
    return rec
