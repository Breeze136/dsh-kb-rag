"""（slow）全库切块健康度：核对"整篇不可检索"与"被吞"指标不回归。

数据来自 _realdata.get_dataset()（**一次并行解析，两个 slow 套件共享**，并按文件指纹缓存）：
  · 只从沙箱副本读 docs 表，真实库只读
  · 与 tests/baselines/refs_health.json 比对，超阈值即失败
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests" / "suites"))
import guards                                                          # noqa: E402
from _realdata import get_dataset                                      # noqa: E402

SUITE = {"id": "refs_real", "title": "（slow）全库切块健康度 vs 基线",
         "tags": ["slow"], "needs_real_kb": True}


def run(ctx):
    base = guards.baseline("refs_health")
    if base is None:
        ctx.skip("没有 tests/baselines/refs_health.json")
    ds = get_dataset(ctx, REPO / "kb_engine.py", None, need_old=False,
                     workers=getattr(ctx, "workers", 0), use_cache=getattr(ctx, "use_cache", True))
    rows = [m for m in ds["metrics"] if m.get("ok")]
    st = ds["stats"]
    ctx.info("文档 %d ｜ 复用缓存 %d ｜ 本次重算 %d（%s）｜ 解析失败 %d"
             % (st["docs"], st["reused"], st["computed"], st.get("mode"), st["errors"]))

    tot = sum(r["chunks"] for r in rows) or 1
    zero = sum(r["zero"] for r in rows)
    chars = sum(r["chars"] for r in rows) or 1
    zero_chars = sum(r["zero_chars"] for r in rows)
    blind = [r["id"] for r in rows if r["positive"] == 0]
    heavy = [r["id"] for r in rows if r["chunks"] and r["zero"] / r["chunks"] > 0.5]
    ctx.info("分块 %d ｜ weight=0 %d（%.2f%% 字符 %.2f%%）｜ 整篇不可检索 %d ｜ 被吞>50%% %d"
             % (tot, zero, 100 * zero / tot, 100 * zero_chars / chars, len(blind), len(heavy)))

    ctx.check("每篇文档都至少有一个可检索分块（不变量）", not blind, "缺失：%s" % blind[:8])
    ctx.check("解析失败数为 0", st["errors"] == 0, st["errors"])
    ctx.check("weight=0 占比不超过基线 + 1.5pp", zero / tot <= base["zero_share"] + 0.015,
              "%.4f vs 基线 %.4f" % (zero / tot, base["zero_share"]))
    ctx.check("被吞 >50% 的文档数不超过基线", len(heavy) <= base["heavy_docs"],
              "%d vs 基线 %d" % (len(heavy), base["heavy_docs"]))
    ctx.check("整篇不可检索篇数不超过基线", len(blind) <= base["blind_docs"],
              "%d vs 基线 %d" % (len(blind), base["blind_docs"]))
