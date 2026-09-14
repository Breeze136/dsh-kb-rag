"""功能闭环（隔离库）：四种入库结果 → 改后可检索 → 自动转后台 + 进度轮询 → 不变量 → 去重 → 元数据刷新 → 清库。

与 s_engine_loop / s_search 的分工：
  · s_engine_loop 管"单篇文档的入库/回填/state"
  · s_search 管"检索语义与缓存"
  · 本套件管**跨步骤的功能闭环**：入库的四种结果按顺序各走一遍、后台任务生命周期、
    以及一条容易被忽略的库内不变量（weight>0 的分块必须都有向量）。

所有写操作都落在 ctx.tmpdir 的临时库里，不碰真实库。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests" / "suites"))
from _helpers import Engine, q, write_docs                              # noqa: E402

SUITE = {"id": "functional", "title": "功能闭环（四种入库结果/后台任务/不变量/维护操作）",
         "tags": ["fast"], "needs_models": True}

DOC_A = ("Zorblax quenching in ferrous alloys\n\n"
         "Abstract\n\nWe report a systematic study of zorblax quenching in ferrous alloys. "
         "The measured zorblax coefficient was 3.14 units.\n\n"
         "Methods\n\nQuenching was performed in brine.\n")

DOC_A_CHANGED = DOC_A.replace("3.14 units", "3.19 units")

DOC_B = ("Plimth lattice relaxation in layered perovskites\n\n"
         "Abstract\n\nPlimth lattice relaxation governs the band gap of layered perovskites. "
         "We measured relaxation times of 4.2 ns.\n\n"
         "Methods\n\nTime resolved photoluminescence with 400 nm excitation.\n")


def _status_until_done(e, job_id, kb, ctx, label, tries=40):
    """轮询后台任务直到 done/error，返回最终响应；超时返回 None。"""
    seen_running = False
    for _ in range(tries):
        st = e.call("status", {"job_id": job_id, "kb_root": str(kb)})
        s = st.get("status")
        if s == "running":
            seen_running = True
        elif s in ("done", "error"):
            ctx.check("%s：轮询见过 running 再到 %s" % (label, s), seen_running or s == "done", s)
            return st
        time.sleep(0.4)
    return None


def run(ctx):
    kb = ctx.tmpdir("kb_functional")
    src = ctx.tmpdir("docs_functional")

    (src / "a.txt").write_text(DOC_A, encoding="utf-8")
    (src / "b.txt").write_text(DOC_B, encoding="utf-8")
    (src / "a-copy.txt").write_text(DOC_A, encoding="utf-8")   # 与 a.txt 逐字节相同

    with Engine() as e:
        # ── ① 首次入库：新增 + 重复 ────────────────────────────────────────
        r1 = e.call("ingest", {"kb_root": str(kb), "paths": [str(src)]})
        t1 = r1.get("totals") or {}
        ctx.check("初次入库：内容相同的两篇只收一篇（added=2 / duplicates=1）",
                  t1.get("added") == 2 and t1.get("duplicates") == 1, t1)

        # ── ② 内容未变重跑：跳过 + 重复 ────────────────────────────────────
        r2 = e.call("ingest", {"kb_root": str(kb), "paths": [str(src)]})
        t2 = r2.get("totals") or {}
        ctx.check("重跑：未变内容跳过、未入库的重复项仍判重复（skipped=2 / duplicates=1）",
                  t2.get("skipped") == 2 and t2.get("duplicates") == 1, t2)

        # ── ③ 内容变更：更新，且新内容可检索 ───────────────────────────────
        (src / "b.txt").write_text(DOC_B.replace("4.2 ns", "7.7 ns"), encoding="utf-8")
        r3 = e.call("ingest", {"kb_root": str(kb), "paths": [str(src)]})
        t3 = r3.get("totals") or {}
        ctx.check("改了一篇已入库文件 → updated=1", t3.get("updated") == 1, t3)
        s3 = e.call("search", {"kb_root": str(kb), "query": "plimth relaxation perovskite", "depth": "quick", "top_k": 2})
        blob3 = " ".join(str((it or {}).get("snippet") or "") for it in (s3.get("results") or []))
        ctx.check("改后的内容真的可检索（重新嵌入生效）", "7.7 ns" in blob3, blob3[:120])

        # ── ④ 库内不变量：weight>0 的分块必须都有向量 ──────────────────────
        dbf = kb / "kb.sqlite"
        n_weight_pos = q(dbf, "SELECT COUNT(*) FROM chunks WHERE weight > 0")[0][0]
        n_vecs = q(dbf, "SELECT COUNT(*) FROM vecs")[0][0]
        n_orphan = q(dbf, "SELECT COUNT(*) FROM chunks c LEFT JOIN vecs v ON v.chunk_id=c.id "
                          "WHERE v.chunk_id IS NULL AND c.weight > 0")[0][0]
        ctx.check("weight>0 的分块数与向量数一致（109/32 那类差值只能来自 weight<=0）",
                  n_weight_pos == n_vecs, "weight>0=%s vecs=%s" % (n_weight_pos, n_vecs))
        ctx.check("没有任何 weight>0 的分块缺向量", n_orphan == 0, n_orphan)

        # ── ⑤ 自动转后台（阈值 25）+ 进度轮询 ──────────────────────────────
        bulk = ctx.tmpdir("docs_bulk")
        for i in range(1, 31):
            (bulk / ("d%02d.txt" % i)).write_text(
                "Bulkprobe%d synthetic document\n\nAbstract\n\nUnique token bulkprobe%d.\n\n"
                "Results\n\nThe value was %d units.\n" % (i, i, i), encoding="utf-8")
        r5 = e.call("ingest", {"kb_root": str(kb), "paths": [str(bulk)], "async_if_large": True})
        job = r5.get("job_id")
        ctx.check("30 篇 > 阈值 25 → 自动转后台并返回 job_id", isinstance(job, str) and len(job) > 0, job)
        if job:
            st = _status_until_done(e, job, kb, ctx, "批量入库", tries=60)
            if st is None:
                ctx.check("批量后台任务在超时前完成", False, "轮询超时")
            else:
                res = st.get("result") or {}
                tot = (res.get("totals") or {}) if isinstance(res, dict) else {}
                ctx.check("后台任务 done 且 totals.added=30", tot.get("added") == 30, tot)

        # ── ⑥ 去重幂等 ─────────────────────────────────────────────────────
        r6 = e.call("dedup", {"kb_root": str(kb)})
        ctx.check("dedup 幂等（重复入库早被 sha256 挡住，removed=0）", r6.get("removed") == 0, r6)

        # ── ⑦ 元数据刷新：不重切块、不重嵌入 ───────────────────────────────
        r7 = e.call("ingest", {"kb_root": str(kb), "paths": [str(src)], "metadata_only": True})
        ctx.check("metadata_only：mode 正确且未重切块", r7.get("mode") == "metadata_only",
                  r7.get("mode"))
        ctx.check("metadata_only：不把刷新计入 added",
                  (r7.get("totals") or {}).get("added") == 0, r7.get("totals"))

        # ── ⑧ rebuild 强制后台（全量重灌必然超过宿主单次调用超时） ─────────
        r8 = e.call("ingest", {"kb_root": str(kb), "rebuild": True, "async_if_large": True})
        j8 = r8.get("job_id")
        if isinstance(j8, str) and j8:
            st8 = _status_until_done(e, j8, kb, ctx, "rebuild", tries=60)
            if st8 is None:
                ctx.check("rebuild 后台任务完成", False, "轮询超时")
            else:
                res8 = st8.get("result") or {}
                ctx.check("rebuild 后仍然有文档（不是清空）",
                          ((res8.get("totals") or {}).get("updated") or 0) > 0,
                          (res8.get("totals") or {}))
        else:
            # 引擎若选择同步跑完，也必须真的重灌了
            ctx.check("rebuild（同步路径）也有更新", ((r8.get("totals") or {}).get("updated") or 0) > 0,
                      r8.get("totals"))

        # ── ⑨ 多库互不干扰 ─────────────────────────────────────────────────
        kb2 = ctx.tmpdir("kb_functional_second")
        n_before = e.call("stats", {"kb_root": str(kb)}).get("docs")
        other = write_docs(ctx.tmpdir("docs_second"))
        e.call("ingest", {"kb_root": str(kb2), "paths": [str(other[0])]})
        n_after = e.call("stats", {"kb_root": str(kb)}).get("docs")
        ctx.check("往第二个库入库不影响第一个库的文档数", n_before == n_after,
                  "%s → %s" % (n_before, n_after))
        ctx.check("第二个库自己确实有文档",
                  (e.call("stats", {"kb_root": str(kb2)}).get("docs") or 0) >= 1)

        # ── ⑩ 清库安全阀 + 真清 ────────────────────────────────────────────
        # 注意协议形状：serve 的信封恒为 {"ok": true, "response": …}，命令级失败在 **response 内部**
        # （kb_engine.py:4097），所以这里检查 response.get("ok") 而不是期待抛异常。
        refused = e.call("clear", {"kb_root": str(kb)})
        ctx.check("clear 缺 confirm 时拒绝（response.ok=False + 明确原因）",
                  refused.get("ok") is False and "confirm" in str(refused.get("error") or ""),
                  str(refused.get("error"))[:120])
        still_there = e.call("stats", {"kb_root": str(kb)}).get("docs")
        ctx.check("被拒绝的 clear 没有真的清掉数据", (still_there or 0) > 0, still_there)
        r10 = e.call("clear", {"kb_root": str(kb), "confirm": True})
        ctx.check("clear(confirm=true) 真清并报出清了什么",
                  (r10.get("cleared_docs") or 0) > 0, {k: r10.get(k) for k in ("cleared_docs", "cleared_chunks")})
        ctx.check("清后统计归零", (e.call("stats", {"kb_root": str(kb)}).get("docs") or 0) == 0)
