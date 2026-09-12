"""（slow）引文关联命中率 vs 基线：文内 [n] 编号能否在文末找到对应条目。

数据来自 _realdata.get_dataset() —— 与 refs_real 共用同一次并行解析与缓存；
"改前"对照取 tests/baselines/cites.json 里记录的基线提交（git 历史）在同一批 PDF 上跑。
"""
from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests" / "suites"))
import guards                                                          # noqa: E402
from _realdata import get_dataset                                      # noqa: E402

SUITE = {"id": "cites_real", "title": "（slow）引文关联命中率 vs 基线",
         "tags": ["slow"], "needs_real_kb": True}


def _baseline_engine(commit, tmp: Path):
    """从 git 历史取基线版本引擎；取不到返回 None（只核对绝对指标）。"""
    if not commit:
        return None
    try:
        p = subprocess.run(["git", "-C", str(REPO), "show", "%s:kb_engine.py" % commit],
                           capture_output=True, timeout=60)
        if p.returncode != 0 or not p.stdout:
            return None
    except Exception:                                             # noqa: BLE001
        return None
    f = tmp / ("kb_engine_%s.py" % commit.replace("/", "_"))
    io.open(f, "w", encoding="utf-8", newline="\n").write(p.stdout.decode("utf-8"))
    return f


def run(ctx):
    base = guards.baseline("cites")
    if base is None:
        ctx.skip("没有 tests/baselines/cites.json")
    old_path = _baseline_engine(base.get("baseline_commit", ""), ctx.sandbox)
    if old_path is None:
        ctx.info("取不到基线提交 %s 的引擎：只核对绝对指标" % base.get("baseline_commit"))

    ds = get_dataset(ctx, REPO / "kb_engine.py", old_path, need_old=old_path is not None,
                     workers=getattr(ctx, "workers", 0), use_cache=getattr(ctx, "use_cache", True))
    st = ds["stats"]
    rows = [m for m in ds["metrics"] if m.get("ok")]
    ctx.info("文档 %d ｜ 复用缓存 %d ｜ 本次重算 %d（%s）｜ 解析失败 %d"
             % (st["docs"], st["reused"], st["computed"], st.get("mode"), st["errors"]))

    entries = sum(r["entries"] for r in rows)
    incites = sum(r["incites"] for r in rows)
    resolved = sum(r["resolved"] for r in rows)
    no_entries = sum(1 for r in rows if r["entries"] == 0)
    rate = resolved / max(1, incites)
    ctx.info("新实现：条目 %d ｜ 文内编号 %d ｜ 命中 %d（%.1f%%）｜ 解析不出条目的文档 %d"
             % (entries, incites, resolved, 100 * rate, no_entries))

    old_entries = old_resolved = 0
    regressed = 0
    have_old = all(r.get("entries_old") is not None for r in rows) if rows else False
    if have_old:
        old_entries = sum(r["entries_old"] for r in rows)
        old_resolved = sum(r["resolved_old"] for r in rows)
        old_incites = sum(r["incites_old"] or 0 for r in rows)
        old_rate = old_resolved / max(1, old_incites)
        regressed = sum(1 for r in rows if (r["entries_old"] or 0) > 0 and r["entries"] == 0)
        improved = sum(1 for r in rows if r["resolved"] > (r["resolved_old"] or 0))
        ctx.info("基线实现：条目 %d ｜ 命中 %d（%.1f%%）｜ 改后失效 %d 篇 ｜ 改后改善 %d 篇"
                 % (old_entries, old_resolved, 100 * old_rate, regressed, improved))
    else:
        old_rate = base.get("baseline_hit_rate", 0.0)

    ctx.check("解析失败数为 0", st["errors"] == 0, st["errors"])
    ctx.check("引文命中率不低于基线 - 0.5pp", rate >= base["hit_rate"] - 0.005,
              "%.4f vs 基线 %.4f" % (rate, base["hit_rate"]))
    ctx.check("文末可解析条目总数不低于基线的 97%", entries >= base["entries"] * 0.97,
              "%d vs 基线 %d" % (entries, base["entries"]))
    ctx.check("完全解析不出条目的文档数不超过基线", no_entries <= base["no_entries"],
              "%d vs 基线 %d" % (no_entries, base["no_entries"]))
    if have_old:
        ctx.check("相对基线实现：命中率有提升", rate > old_rate - 1e-9,
                  "%.4f vs 基线实现 %.4f" % (rate, old_rate))
        ctx.check("相对基线实现：改后失效的文档在基线记录的范围内",
                  regressed <= base.get("regressed_docs_max", 8), regressed)
