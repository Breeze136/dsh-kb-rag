"""BM25 oracle：用一份**独立的参考实现**核对引擎的打分，防止"优化把公式改了"。

为什么不用 git 里上一版做对照：提交之后 HEAD 就等于当前版本，对照会退化成自己比自己。
这里直接把公式按规格重写一遍（k1=1.2 / b=0.75 / 子串 df / 章节权重），在合成语料上逐位比对。
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests" / "suites"))
from _helpers import load_engine                                       # noqa: E402

SUITE = {"id": "bm25", "title": "BM25 打分与独立 oracle 逐位一致", "tags": ["fast"]}

K1, B = 1.2, 0.75


def oracle_rank(rows, query, extract_terms):
    """按规格独立实现的 BM25（与引擎实现无共享代码，只共用 query→terms 的解析）。"""
    terms = extract_terms(query)
    n = len(rows)
    texts = [r["text"] for r in rows]
    lowered = [t.lower() for t in texts]
    avgdl = sum(len(t) for t in texts) / max(1, n)
    scores = [0.0] * n
    best_idf = [0.0] * n
    best_term = [None] * n
    for term, _kind, tw in terms:
        df = sum(1 for lt in lowered if term in lt)
        if df == 0:
            continue
        idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
        for i, lt in enumerate(lowered):
            tf = lt.count(term)
            if tf == 0:
                continue
            denom = tf + K1 * (1 - B + B * len(texts[i]) / avgdl)
            scores[i] += idf * tf * (K1 + 1) / denom * tw
            if idf > best_idf[i]:
                best_idf[i] = idf
                best_term[i] = term
    ranked = sorted(((i, scores[i] * rows[i]["weight"]) for i in range(n) if scores[i] > 0),
                    key=lambda x: x[1], reverse=True)
    return ranked, best_term


def run(ctx):
    k = load_engine("kbe_bm25")
    corpus = [
        ("Graphene domains nucleate on copper and coalesce into a continuous film under methane.",
         1.0),
        ("Raman spectroscopy of graphene: the 2D band tracks the layer number across the wafer.",
         1.5),
        ("Conductive domain walls in ferroelectric thin films probed by atomic force microscopy.",
         1.2),
        ("Magnon confinement in an antiferromagnetic oxide heterostructure studied by scattering.",
         1.0),
        ("Copper foil annealing in hydrogen before graphene growth controls the nucleation density.",
         1.2),
        ("A completely unrelated paragraph about sourdough fermentation and bread baking.",
         1.0),
    ]
    rows = [{"text": t, "weight": w} for t, w in corpus]

    for query in ("graphene copper coalescence", "domain wall ferroelectric",
                  "Raman layer number", "石墨烯 拉曼"):
        exp, exp_bt = oracle_rank(rows, query, k.extract_terms)
        got, got_bt, err = k.keyword_ranking(rows, query)
        ctx.check("BM25 与 oracle 逐位一致：%s（%d 条）" % (query, len(exp)), exp == got,
                  "exp=%s got=%s" % (exp[:3], got[:3]))
        ctx.check("best_term 与 oracle 一致：%s" % query, exp_bt == got_bt)

    # 缓存的 lowered/avgdl 不得改变结果（同一 ckey 连跑两次）
    again, _, _ = k.keyword_ranking(rows, "graphene copper coalescence", ("test",))
    base, _, _ = k.keyword_ranking(rows, "graphene copper coalescence")
    ctx.check("BM25 缓存不改变结果（第二次调用逐位一致）", again == base)

    # 确定性：重复调用完全一致
    a, _, _ = k.keyword_ranking(rows, "domain wall ferroelectric")
    b, _, _ = k.keyword_ranking(rows, "domain wall ferroelectric")
    ctx.check("BM25 确定性（重复调用一致）", a == b)

    # 空 query：明确给出原因而不是静默返回空
    empty, note, _ = k.keyword_ranking(rows, "the of and")
    ctx.check("无法解析关键词时返回说明", empty == [] and isinstance(note, str) and note, note)
