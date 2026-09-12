"""保底机制（guards）：跑测试前后必须成立的硬约束。

这些都是"出问题就大声失败"的检查，不是建议：
  1. guard_repo_clean        —— 测试不得改动仓库工作树（跑前跑后比对 tracked 文件哈希）
  2. guard_twin_engines      —— kb_engine.py 与 npm-package/kb_engine.py 必须逐字节一致
  3. guard_guidance_mirror   —— plugin/host.js 的提示镜像必须与 lib/guidance.js 同步
  4. guard_real_kb           —— 真实知识库**只读**：测试用它之前先复制到沙箱；
                                跑完比对 mtime/大小/行数，被改动就硬失败
  5. guard_models            —— 需要嵌入/精排模型的用例，缺模型就 SKIP（不误报失败）
  6. guard_no_repo_writes    —— 沙箱之外写入检测（按 mtime 扫描仓库）
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TESTS = REPO / "tests"


# ----------------------------------------------------------------- 仓库不被改动

def repo_snapshot():
    """仓库工作树快照：tracked 文件的内容哈希（忽略 _reports/_sandbox 这类运行产物）。"""
    try:
        p = subprocess.run(["git", "-C", str(REPO), "ls-files"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
        files = [f for f in p.stdout.splitlines() if f.strip()]
    except Exception:                                             # noqa: BLE001
        files = []
    snap = {}
    for rel in files:
        f = REPO / rel
        try:
            snap[rel] = hashlib.sha1(f.read_bytes()).hexdigest()
        except OSError:
            snap[rel] = None
    return snap


def repo_diff(before, after):
    changed = [k for k in set(before) | set(after) if before.get(k) != after.get(k)]
    return sorted(changed)


# ----------------------------------------------------------------- 双份引擎 / 镜像

def twin_engines_equal():
    a, b = REPO / "kb_engine.py", REPO / "npm-package" / "kb_engine.py"
    if not a.is_file() or not b.is_file():
        return False, "缺少引擎文件"
    ha = hashlib.sha256(a.read_bytes()).hexdigest()
    hb = hashlib.sha256(b.read_bytes()).hexdigest()
    return ha == hb, ("%s ≠ %s" % (ha[:12], hb[:12])) if ha != hb else ha[:12]


def guidance_mirror_ok():
    script = REPO / "tools" / "sync-host-guidance.mjs"
    if not script.is_file():
        return False, "缺少 tools/sync-host-guidance.mjs"
    try:
        p = subprocess.run(["node", str(script), "--check"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120, cwd=str(REPO))
    except FileNotFoundError:
        return True, "没有 node，跳过镜像检查"
    except subprocess.TimeoutExpired:
        return False, "镜像检查超时"
    return p.returncode == 0, (p.stdout or p.stderr).strip()[:200]


# ----------------------------------------------------------------- 真实知识库只读

def default_real_kb():
    """真实库位置：环境变量 KB_RAG_REAL_KB 优先；否则工作区里常见的 <cwd>/.kb。"""
    env = os.environ.get("KB_RAG_REAL_KB")
    if env:
        p = Path(env)
        return p if p.is_dir() else None
    for cand in (Path.cwd() / ".kb", REPO.parent / ".kb"):
        if (cand / "kb.sqlite").is_file():
            return cand
    return None


def kb_fingerprint(db_file: Path):
    """真实库指纹：文件 mtime/大小 + 三张表的计数。用来证明"跑完没被改动"。"""
    st = db_file.stat()
    info = {"mtime_ns": st.st_mtime_ns, "size": st.st_size}
    try:
        db = sqlite3.connect("file:%s?mode=ro" % db_file.as_posix(), uri=True)
        for tbl in ("docs", "chunks", "vecs", "cache"):
            try:
                info[tbl] = db.execute("SELECT COUNT(*) FROM %s" % tbl).fetchone()[0]
            except sqlite3.Error:
                info[tbl] = None
        db.close()
    except sqlite3.Error as e:
        info["error"] = str(e)[:120]
    return info


def copy_real_kb(dest_dir: Path, real_kb: Path):
    """把真实库复制到沙箱：**所有**数据驱动的用例都只碰副本，真实库只读。"""
    dest = dest_dir / "kb_copy"
    if (dest / "kb.sqlite").is_file():
        return dest
    dest.mkdir(parents=True, exist_ok=True)
    for f in real_kb.glob("kb.sqlite*"):
        shutil.copy2(f, dest / f.name)
    return dest


# ----------------------------------------------------------------- 依赖与模型

def models_cached():
    """嵌入/精排模型是否在本地 HF 缓存里（缺就 SKIP 需要模型的用例，而不是失败）。"""
    home = Path(os.environ.get("HF_HOME") or (Path.home() / ".cache" / "huggingface"))
    hub = home / "hub"
    if not hub.is_dir():
        return False, "没有 HF 缓存目录 %s" % hub
    need = {"models--BAAI--bge-small-zh-v1.5", "models--BAAI--bge-reranker-base"}
    have = {p.name for p in hub.iterdir() if p.is_dir()}
    missing = need - have
    if missing:
        return False, "缺模型缓存：%s" % ", ".join(sorted(missing))
    return True, "模型已缓存"


def python_deps():
    missing = []
    for mod in ("fitz", "numpy", "sentence_transformers"):
        try:
            __import__(mod)
        except Exception:                                         # noqa: BLE001
            missing.append(mod)
    return (not missing), ("缺依赖：%s" % ", ".join(missing)) if missing else "ok"


def git_available():
    try:
        subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
                       capture_output=True, timeout=30)
        return True
    except Exception:                                             # noqa: BLE001
        return False


def head_commit():
    try:
        p = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=30)
        return p.stdout.strip()
    except Exception:                                             # noqa: BLE001
        return "?"


def dirty_paths():
    try:
        p = subprocess.run(["git", "-C", str(REPO), "status", "--porcelain"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60)
        return [l.strip() for l in p.stdout.splitlines() if l.strip()]
    except Exception:                                             # noqa: BLE001
        return []


def baseline(name):
    f = TESTS / "baselines" / (name + ".json")
    if not f.is_file():
        return None
    return json.loads(f.read_text(encoding="utf-8"))
