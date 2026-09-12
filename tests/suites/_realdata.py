"""真实库数据集的**一次性并行**计算 + 磁盘缓存（供 refs_real / cites_real 共用）。

为什么这么设计：
  · 两个 slow 套件都要"逐篇 PDF 重新切块"，各自跑一遍等于把 316 篇 PDF 解析两次（实测各 ~225 s）；
  · 单进程串行也用不满多核；
  · 第二次跑测试时绝大多数文档没变，完全没必要重算。
所以：一个文档只解析一次 → 同时产出"切块健康度"和"引文关联"两套指标 → 按
(路径, mtime, size, 引擎哈希, 旧引擎哈希) 记账缓存到 tests/_cache/。

缓存键里带引擎哈希与工具版本：改了引擎/解析器立刻全量重算，不会拿旧结果骗自己。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

TESTS = Path(__file__).resolve().parents[1]
CACHE_DIR = TESTS / "_cache"
CACHE_FILE = CACHE_DIR / "real_metrics.json"
CACHE_VERSION = 3              # 指标口径变化时 +1（避免旧缓存被误用）

_G = {}


def _load(path, name):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _init(engine_path, old_path):
    try:
        import fitz
        fitz.TOOLS.mupdf_display_errors(False)      # PDF 的 format error 噪声不必刷屏
    except Exception:                                    # noqa: BLE001
        pass
    _G["k"] = _load(engine_path, "kbe_worker_new")
    _G["old"] = _load(old_path, "kbe_worker_old") if old_path else None


def _work(job):
    """子进程：解析一篇 PDF，产出两套指标。job = (doc_id, path, need_old)"""
    doc_id, path, need_old = job
    k = _G["k"]
    out = {"id": doc_id, "ok": False, "error": None}
    try:
        text, pm = k.read_document(Path(path))
        paras = (pm or {}).get("_paras")
        chunks = k.chunk_document(text, paras=paras)
        pos = [c for c in chunks if c[1] > 0]
        refs_text = "\n".join(c[2] for c in chunks if c[0] == "References")
        body = "\n".join(c[2] for c in pos)
        refmap = k._parse_references(refs_text)
        nums = set()
        for m in k._INCITE_RE.finditer(body):
            nums.update(k._expand_incite_nums(m.group(1)))
        out.update({
            "ok": True,
            "chunks": len(chunks), "positive": len(pos), "zero": len(chunks) - len(pos),
            "zero_chars": sum(len(c[2]) for c in chunks if c[1] <= 0),
            "chars": sum(len(c[2]) for c in chunks),
            "entries": len(refmap), "incites": len(nums),
            "resolved": sum(1 for n in nums if n in refmap),
            "entries_old": None, "resolved_old": None, "incites_old": None,
        })
        if need_old and _G.get("old") is not None:
            old = _G["old"]
            och = old.chunk_document(text, paras=paras)
            orefs = "\n".join(c[2] for c in och if c[0] == "References")
            obody = "\n".join(c[2] for c in och if c[1] > 0)
            omap = old._parse_references(orefs)
            onums = set()
            for m in old._INCITE_RE.finditer(obody):
                onums.update(old._expand_incite_nums(m.group(1)))
            out["entries_old"] = len(omap)
            out["incites_old"] = len(onums)
            out["resolved_old"] = sum(1 for n in onums if n in omap)
    except Exception as e:                               # noqa: BLE001
        out["error"] = "%s: %s" % (type(e).__name__, str(e)[:120])
    return out


def _file_key(path, st):
    return "%s|%d|%d" % (path, st.st_mtime_ns, st.st_size)


def _sha(path):
    try:
        return hashlib.sha1(Path(path).read_bytes()).hexdigest()[:16]
    except OSError:
        return "?"


def doc_list(kb_copy: Path):
    """从沙箱副本读 docs 表（真实库只读）。"""
    db = sqlite3.connect("file:%s?mode=ro" % (kb_copy / "kb.sqlite").as_posix(), uri=True)
    db.row_factory = sqlite3.Row
    try:
        return [(r["id"], r["path"]) for r in db.execute("SELECT id, path FROM docs ORDER BY id")]
    finally:
        db.close()


def _shard_worker(in_file, out_file):
    """子进程入口：读一个分片的 job 列表，算完把结果写文件（不用管道，任何沙箱都能跑）。"""
    jobs = json.loads(Path(in_file).read_text(encoding="utf-8"))
    _init(jobs["engine"], jobs.get("old"))
    out = [_work(tuple(j)) for j in jobs["jobs"]]
    Path(out_file).write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return 0


def _run_shards(todo, engine_path, old_path, workers, sandbox_dir: Path):
    """把作业切成 workers 份，用子进程并行跑；返回结果列表（顺序与 todo 对齐）。

    为什么不是 ProcessPoolExecutor：受限环境下 Windows 的进程池要靠命名管道做 IPC，会被
    直接拒绝（实测 PermissionError WinError 5）；线程池又吃不到 GIL（实测 1.0×）。
    子进程 + 文件传结果既真并行又绕开了管道，是本环境里唯一走得通的路。
    """
    import subprocess
    import sys as _sys
    work_dir = sandbox_dir / "realdata_shards"
    work_dir.mkdir(parents=True, exist_ok=True)
    n = max(1, min(workers, len(todo)))
    shards = [todo[i::n] for i in range(n)]                 # 交错切分：大文件分布更均匀
    procs = []
    for i, shard in enumerate(shards):
        if not shard:
            continue
        jin = work_dir / ("jobs_%d.json" % i)
        jout = work_dir / ("out_%d.json" % i)
        jin.write_text(json.dumps({"engine": str(engine_path),
                                   "old": str(old_path) if old_path else None,
                                   "jobs": [list(j) for j in shard]}, ensure_ascii=False),
                       encoding="utf-8")
        procs.append((i, len(shard), subprocess.Popen(
            [_sys.executable, str(Path(__file__).resolve()), "--shard-worker", str(jin), str(jout)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)))
    results = []
    for i, cnt, p in procs:
        try:
            p.wait(timeout=1800)
        except subprocess.TimeoutExpired:
            p.kill()
        jout = work_dir / ("out_%d.json" % i)
        if jout.is_file():
            try:
                results.extend(json.loads(jout.read_text(encoding="utf-8")))
            except Exception:                                    # noqa: BLE001
                pass
    if len(results) != len(todo):                            # 子进程失败 → 串行兜底，绝不静默丢数据
        _init(str(engine_path), str(old_path) if old_path else None)
        done = {r.get("id") for r in results}
        results.extend(_work(j) for j in todo if j[0] not in done)
    return results


def get_dataset(ctx, engine_path: Path, old_path: Path | None, need_old=True, workers=0,
                use_cache=True):
    """返回 {"metrics": [...], "stats": {...}}；metrics 每项含两套指标。

    并行方式：**子进程分片**（见 _run_shards 的说明）。缓存按 (路径, mtime, size, 引擎哈希)
    记账：改了引擎立刻全量重算；文档没变就复用上次的逐篇指标。
    """
    docs = doc_list(Path(ctx.real_kb_copy))
    eng_sha, old_sha = _sha(engine_path), (_sha(old_path) if old_path else "-")
    # 注意：tag 里**不放** old_sha —— 两个 slow 套件一个要 old 指标、一个不要，
    # 放进 tag 会让后者永远命中不了缓存（实测：总耗时直接翻倍）。是否可复用按**逐条**校验。
    tag = "v%d|%s|%s" % (CACHE_VERSION, eng_sha, os.path.getsize(engine_path))

    cache = {}
    if use_cache and CACHE_FILE.is_file():
        try:
            loaded = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            if loaded.get("tag") == tag:
                cache = loaded.get("items") or {}
        except Exception:                                # noqa: BLE001
            cache = {}

    metrics, todo = [], []
    for doc_id, path in docs:
        try:
            st = os.stat(path)
            key = _file_key(path, st)
        except OSError:
            key = None
        hit = cache.get(key) if key else None
        if hit is not None and (not need_old or hit.get("entries_old") is not None):
            metrics.append(hit)
            continue
        todo.append((doc_id, path, need_old))

    workers = workers or max(1, min(8, (os.cpu_count() or 4)))
    mode = "缓存命中，无计算"
    fresh = []
    if todo:
        if workers == 1:
            _init(str(engine_path), str(old_path) if old_path else None)
            fresh = [_work(j) for j in todo]
            mode = "串行"
        else:
            fresh = _run_shards(todo, engine_path, old_path, workers, ctx.sandbox)
            mode = "%d 子进程分片" % min(workers, len(todo))
    metrics.extend(fresh)

    if use_cache and fresh:
        for m, (doc_id, path, _n) in zip(fresh, todo):
            try:
                st = os.stat(path)
                cache[_file_key(path, st)] = m
            except OSError:
                pass
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps({"tag": tag, "items": cache}, ensure_ascii=False),
                              encoding="utf-8")

    stats = {"docs": len(metrics), "reused": len(metrics) - len(fresh), "computed": len(fresh),
             "workers": workers if todo else 0, "mode": mode,
             "errors": sum(1 for m in metrics if not m.get("ok")),
             "old": old_sha != "-"}
    return {"metrics": metrics, "stats": stats}


if __name__ == "__main__":
    # 子进程入口（由 _run_shards 调用）
    import sys as _sys
    if len(_sys.argv) >= 4 and _sys.argv[1] == "--shard-worker":
        raise SystemExit(_shard_worker(_sys.argv[2], _sys.argv[3]))
    print("这是 _realdata 的子进程入口，请通过 tests/run.py 使用")
    raise SystemExit(2)
