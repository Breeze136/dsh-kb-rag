"""引擎最小闭环（临时库，需要模型）：入库 → 删向量 → 重跑回填 → 统计 → reload → metadata_only → state → clear。"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests" / "suites"))
from _helpers import Engine, write_docs                                # noqa: E402

SUITE = {"id": "engine_loop", "title": "引擎最小闭环（入库/回填/统计/reload/state/clear）",
         "tags": ["fast"], "needs_models": True}


def run(ctx):
    kb = ctx.tmpdir("kb_loop")
    docs = write_docs(ctx.tmpdir("docs_loop"))
    e = Engine()
    try:
        # —— 入库 ——
        r = e.call("ingest", {"kb_root": str(kb), "paths": [str(docs[0])]})
        f0 = (r.get("files") or [{}])[0]
        ctx.check("入库 status=added", f0.get("status") == "added", f0.get("status"))
        ctx.check("建了向量", (f0.get("vectors") or 0) > 0, f0.get("vectors"))
        ctx.check("embedding_error 为空", r.get("embedding_error") is None, r.get("embedding_error"))
        ctx.check("vectors_missing=0", r.get("vectors_missing") == 0, r.get("vectors_missing"))
        ctx.check("device 有真值", isinstance(r.get("device"), dict)
                  and r["device"].get("embed_device") is not None, (r.get("device") or {}).get("embed_device"))
        ctx.check("indexed_with 是当前 rev", str(r.get("indexed_with", "")).endswith("/rev5"),
                  r.get("indexed_with"))

        # —— #3：删掉向量后重跑，skipped 分支必须回填 ——
        db = sqlite3.connect(str(kb / "kb.sqlite"))
        db.execute("DELETE FROM vecs")
        db.commit()
        n_before = db.execute("SELECT COUNT(*) FROM vecs").fetchone()[0]
        db.close()
        ctx.check("删后向量数=0", n_before == 0, n_before)
        r2 = e.call("ingest", {"kb_root": str(kb), "paths": [str(docs[0])]})
        f2 = (r2.get("files") or [{}])[0]
        ctx.check("内容未变 → status=skipped", f2.get("status") == "skipped", f2.get("status"))
        ctx.check("skipped 分支把向量补回来", (f2.get("vectors") or 0) > 0, f2.get("vectors"))
        ctx.check("全库 vectors_missing 归零", r2.get("vectors_missing") == 0, r2.get("vectors_missing"))

        # —— duplicate 分支同样补向量 ——
        dup_dir = ctx.tmpdir("docs_dup")
        same = dup_dir / "copy.txt"
        same.write_bytes(docs[0].read_bytes())
        db = sqlite3.connect(str(kb / "kb.sqlite"))
        db.execute("DELETE FROM vecs")
        db.commit()
        db.close()
        r3 = e.call("ingest", {"kb_root": str(kb), "paths": [str(same)]})
        f3 = (r3.get("files") or [{}])[0]
        ctx.check("同内容 → status=duplicate", f3.get("status") == "duplicate", f3.get("status"))
        ctx.check("duplicate 分支也回填向量", (f3.get("vectors") or 0) > 0, f3.get("vectors"))

        # —— 统计 ——
        st = e.call("stats", {"kb_root": str(kb)})
        h = st.get("health") or {}
        ctx.check("health.ok = True", h.get("ok") is True, h)
        ctx.check("health 含 docs_without_retrievable_chunks",
                  "docs_without_retrievable_chunks" in h, h.get("docs_without_retrievable_chunks"))
        ctx.check("刚入库 → stale_docs=0", st.get("stale_docs") == 0, st.get("stale_docs"))
        ctx.check("stale_kind=none", st.get("stale_kind") == "none", st.get("stale_kind"))

        # —— reload ——
        rl = e.call("reload", {"kb_root": str(kb), "drop_models": True, "rerank": True})
        ctx.check("reload 后嵌入可用", rl.get("embedding") is not None, rl.get("embedding"))
        ctx.check("reload 后精排可用", rl.get("reranker") is not None, rl.get("reranker"))
        ctx.check("retry_secs 是数字", isinstance(rl.get("retry_secs"), (int, float)), rl.get("retry_secs"))

        # —— metadata_only ——
        r4 = e.call("ingest", {"kb_root": str(kb), "rebuild": True, "metadata_only": True})
        ctx.check("mode=metadata_only", r4.get("mode") == "metadata_only", r4.get("mode"))
        ctx.check("metadata_only 不重复计数为 added", (r4.get("totals") or {}).get("added", 0) == 0,
                  r4.get("totals"))

        # —— state 命令 ——
        w = e.call("state", {"kb_root": str(kb), "action": "write",
                             "state": {"scope": "both", "diligence": "thorough", "bogus": 1}})
        ctx.check("state 写入成功且拒绝非法键", w.get("ok") is True and w.get("rejected") == ["bogus"],
                  w.get("rejected"))
        rd = e.call("state", {"kb_root": str(kb)})
        ctx.check("state 读回一致",
                  rd["state"].get("scope") == "both" and rd["state"].get("diligence") == "thorough",
                  rd.get("state"))
        ctx.check("state 文件落在 <工作区>/.kb-rag/state.json",
                  str(rd.get("path", "")).replace("\\", "/").endswith(".kb-rag/state.json"), rd.get("path"))

        # —— clear：必须显式确认 ——
        bad = e.call("clear", {"kb_root": str(kb)})
        ctx.check("clear 缺 confirm 时拒绝", bad.get("ok") is False, bad.get("error", "")[:60])
        cl = e.call("clear", {"kb_root": str(kb), "confirm": True})
        ctx.check("clear 清空文档", cl.get("cleared_docs", 0) >= 1, cl.get("cleared_docs"))
        st2 = e.call("stats", {"kb_root": str(kb)})
        ctx.check("clear 之后 docs=0", st2.get("docs") == 0, st2.get("docs"))
    finally:
        e.close()
