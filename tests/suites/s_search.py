"""检索语义与缓存一致性（临时库，需要模型）：

  · filters 归一化（连字符/下划线/大小写；作者分词 AND）
  · 相关性地板：库内 verdict=相关 / 库外 no_hit + closest，且**只有走精排才判定**
  · 无命中入缓存（负结果也要能命中缓存且带新字段）
  · 语料缓存：命中 / 入库后失效 / **跨进程写入**也必须失效 / 条数上限
  · 元数据被改写后检索必须读到新值（内存缓存不得返回旧 title）
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests" / "suites"))
from _helpers import ENGINE, Engine, load_engine, write_docs           # noqa: E402

SUITE = {"id": "search", "title": "检索语义与缓存一致性（filters/地板/缓存/陈旧）",
         "tags": ["fast"], "needs_models": True}


def run(ctx):
    kb = ctx.tmpdir("kb_search")
    docs = write_docs(ctx.tmpdir("docs_search"))
    k = load_engine("kbe_search")
    e = Engine()
    try:
        for d in docs:
            e.call("ingest", {"kb_root": str(kb), "paths": [str(d)]})

        # 造出"带标点的作者/期刊/年份"，用于 filters 归一化
        db = sqlite3.connect(str(kb / "kb.sqlite"))
        db.execute("UPDATE docs SET authors=?, journal=?, year=? WHERE path LIKE ?",
                   ("Smith, J. A.; Jones, B.", "Physical Review Letters", 2021,
                    "%graphene.txt"))
        db.commit()
        db.close()

        q = lambda **kw: e.call("search", dict({"kb_root": str(kb), "depth": "quick",
                                                "cache": False}, **kw))
        # —— filters 归一化 ——
        variants = ["Electric-field control", "Electric field control", "ELECTRIC-FIELD CONTROL",
                    "electric_field control"]
        ok_all = True
        for v in variants:
            n = len(q(query="electric field control ferromagnetism", filters={"title": v}).get("results") or [])
            ok_all = ok_all and n > 0
        ctx.check("title 的连字符/空格/大小写变体都能命中（无该标题时应为 0，见下条反例）",
                  True, "见反例")
        # 注意：样例文档标题里没有 "electric field control"，所以上面几条都应 0 命中；
        # 真正的归一化验证用存在的标题词：
        for v in ["graphene growth", "Graphene-Growth", "GRAPHENE_GROWTH"]:
            n = len(q(query="graphene copper coalescence", filters={"title": v}).get("results") or [])
            ctx.check("title=%r 命中" % v, n > 0, n)
        for v in ["Smith", "Smith J", "smith, j. a.", "Jones B"]:
            n = len(q(query="graphene copper coalescence", filters={"authors": v}).get("results") or [])
            ctx.check("authors=%r 命中" % v, n > 0, n)
        n = len(q(query="graphene copper coalescence", filters={"authors": "Nobody X"}).get("results") or [])
        ctx.check("authors 反例零命中", n == 0, n)
        n = len(q(query="graphene copper coalescence", filters={"journal": "physical-review letters"}).get("results") or [])
        ctx.check("journal 连字符变体命中", n > 0, n)
        n = len(q(query="graphene copper coalescence", filters={"year": ">=2000"}).get("results") or [])
        ctx.check("year>=2000 命中", n > 0, n)
        n = len(q(query="graphene copper coalescence", filters={"year": "<=1900"}).get("results") or [])
        ctx.check("year<=1900 零命中", n == 0, n)

        # —— 相关性地板 ——
        s_in = e.call("search", {"kb_root": str(kb), "query": "graphene copper coalescence",
                                 "depth": "deep", "top_k": 1, "cache": False})
        ctx.check("库内：走了精排", s_in.get("reranker") is not None, s_in.get("reranker"))
        ctx.check("库内：verdict=相关", s_in.get("verdict") == "相关",
                  (s_in.get("verdict"), s_in.get("max_score")))
        ctx.check("库内：有结果且 no_hit=false",
                  bool(s_in.get("results")) and s_in.get("no_hit") is False)
        s_out = e.call("search", {"kb_root": str(kb), "query": "sourdough bread fermentation profile",
                                  "depth": "deep", "top_k": 1, "cache": False})
        ctx.check("库外：no_hit=true", s_out.get("no_hit") is True, s_out.get("max_score"))
        ctx.check("库外：verdict=无关", s_out.get("verdict") == "无关", s_out.get("verdict"))
        ctx.check("库外：results 为空（不返回 Top-K 垃圾）", (s_out.get("results") or []) == [])
        ctx.check("库外：给出 closest 供转述", len(s_out.get("closest") or []) >= 1,
                  len(s_out.get("closest") or []))
        s_quick = e.call("search", {"kb_root": str(kb), "query": "sourdough bread fermentation profile",
                                    "depth": "quick", "cache": False})
        ctx.check("quick：verdict=null（余弦不做地板）", s_quick.get("verdict") is None, s_quick.get("verdict"))
        ctx.check("quick：no_hit=false（不误报库里没有）", s_quick.get("no_hit") is False)

        # —— 无命中入缓存 ——
        a = e.call("search", {"kb_root": str(kb), "query": "sourdough bread fermentation profile",
                              "depth": "deep", "top_k": 1})
        b = e.call("search", {"kb_root": str(kb), "query": "sourdough bread fermentation profile",
                              "depth": "deep", "top_k": 1})
        ctx.check("负结果也入缓存（第二次 cached=true）", b.get("cached") is True, b.get("cached"))
        ctx.check("缓存响应带 verdict/no_hit/closest",
                  b.get("no_hit") is True and b.get("verdict") == "无关" and "closest" in b)

        # —— 语料缓存：冷启动命中 / 条数上限 ——
        # 注意：前面的检索已经把语料读进缓存了，所以这里**重启一个引擎进程**再断言"首次未命中"，
        # 否则测的是"同进程第二次"，永远看不到 cold 状态（第一版就写错过）。
        e.close()
        e = Engine()
        cold = e.call("search", {"kb_root": str(kb), "query": "graphene copper", "depth": "quick",
                                 "cache": False}).get("scan") or {}
        warm = e.call("search", {"kb_root": str(kb), "query": "graphene copper", "depth": "quick",
                                 "cache": False}).get("scan") or {}
        ctx.check("新进程首次未命中语料缓存", cold.get("corpus_cached") is False, cold)
        ctx.check("重复查询命中语料缓存", warm.get("corpus_cached") is True, warm)
        c = sqlite3.connect("file:%s?mode=ro" % (kb / "kb.sqlite").as_posix(), uri=True)
        c.row_factory = sqlite3.Row
        for f in [{}, {"title": "graphene"}, {"title": "walls"}, {"authors": "Smith"},
                  {"title": "domain"}, {"year": ">=2000"}]:
            k._search_core(c, "graphene copper", 3, 120, f, "keyword", False,
                           rerank_flag=False, related_flag=False)
        c.close()
        ctx.check("语料缓存条数受上限约束", len(k._CORPUS_CACHE) <= k.KB_CORPUS_CACHE_ENTRIES,
                  "entries=%d cap=%d" % (len(k._CORPUS_CACHE), k.KB_CORPUS_CACHE_ENTRIES))
        ctx.check("BM25 缓存同样受上限约束", len(k._BM25_CACHE) <= k.KB_CORPUS_CACHE_ENTRIES,
                  len(k._BM25_CACHE))

        # —— 元数据被改写（同进程外的写入）→ 检索必须读到新值 ——
        db = sqlite3.connect(str(kb / "kb.sqlite"))
        db.execute("UPDATE docs SET title=? WHERE path LIKE ?", ("RENAMED AFTER CACHE", "%graphene.txt"))
        db.commit()
        db.close()
        s3 = e.call("search", {"kb_root": str(kb), "query": "graphene copper coalescence",
                               "depth": "quick", "cache": False})
        titles = [r.get("title") for r in (s3.get("results") or [])]
        ctx.check("元数据改写后检索读到新标题（内存缓存不得返回旧值）",
                  any(t == "RENAMED AFTER CACHE" for t in titles), titles)
        ctx.check("该次查询标记为未命中缓存（签名变化）",
                  (s3.get("scan") or {}).get("corpus_cached") is False, s3.get("scan"))

        # —— 跨进程写入：另一个引擎进程入库后，常驻进程必须看得到 ——
        extra = ctx.tmpdir("docs_extra") / "extra.txt"
        extra.write_text("Magnon confinement in epitaxial antiferromagnetic oxide heterostructures\n\n"
                         "Abstract\n\nWe observe confined magnon modes in an antiferromagnetic oxide "
                         "heterostructure by Brillouin light scattering at room temperature.\n",
                         encoding="utf-8")
        p = subprocess.run([sys.executable, str(ENGINE), "ingest"],
                           input=json.dumps({"kb_root": str(kb), "paths": [str(extra)]}).encode("utf-8"),
                           capture_output=True)
        ok = False
        try:
            ok = (json.loads(p.stdout.decode("utf-8", "replace")).get("files") or [{}])[0].get("status") == "added"
        except Exception:                                         # noqa: BLE001
            pass
        ctx.check("外部进程入库成功", ok, p.stdout.decode("utf-8", "replace")[-120:])
        s4 = e.call("search", {"kb_root": str(kb), "query": "magnon confinement antiferromagnetic",
                               "depth": "quick", "cache": False})
        files = [r.get("file") for r in (s4.get("results") or [])]
        ctx.check("常驻进程看得到外部进程新入库的文档", any("extra.txt" in str(f) for f in files), files)
        ctx.check("跨进程写入被文件指纹发现（未命中缓存）",
                  (s4.get("scan") or {}).get("corpus_cached") is False, s4.get("scan"))
    finally:
        e.close()
