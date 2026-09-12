#!/usr/bin/env python3
"""kb-rag 测试框架入口。

用法：
    python tests/run.py                 # 快测（不含 slow），跑完给汇总
    python tests/run.py --all           # 含 slow（全库数据驱动，约 8 分钟）
    python tests/run.py --only refs     # 只跑名字含 refs 的 suite
    python tests/run.py --list          # 列出所有 suite
    python tests/run.py --fail-fast     # 首个失败即停
    python tests/run.py --json out.json # 另外把报告写到指定路径

保底机制（详见 tests/guards.py 与 README）：
  · 跑前跑后比对仓库 tracked 文件哈希 → 测试改动仓库就硬失败
  · 双份引擎同哈希 + 提示镜像一致性 → 不通过直接失败（--fix-twin 可自动同步引擎副本）
  · 真实知识库**只读**：数据驱动用例只碰沙箱里的副本，跑完再验指纹
  · 每个 suite 有超时；缺模型/缺依赖/缺真实库 → SKIP 而不是失败
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import framework as fw                                             # noqa: E402
import guards                                                      # noqa: E402

# Windows 控制台默认 GBK：中文/特殊符号会直接抛 UnicodeEncodeError（并连带报告写不出来）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                             # noqa: BLE001
        pass

TIMEOUT_FAST = 300
TIMEOUT_SLOW = 1800


def main():
    ap = argparse.ArgumentParser(description="kb-rag 测试框架")
    ap.add_argument("--all", action="store_true", help="包含 slow（全库数据驱动）")
    ap.add_argument("--only", default=None, help="只跑文件名/标题含该子串的 suite")
    ap.add_argument("--list", action="store_true", help="只列出 suite")
    ap.add_argument("--fail-fast", action="store_true")
    ap.add_argument("--json", default=None, help="报告输出路径")
    ap.add_argument("--fix-twin", action="store_true", help="引擎双份不一致时自动同步副本")
    ap.add_argument("--workers", type=int, default=0,
                    help="slow 套件的并行进程数（默认 min(8, CPU/2)；1=串行）")
    ap.add_argument("--no-cache", action="store_true",
                    help="忽略 tests/_cache 里的逐篇指标缓存，全量重算")
    args = ap.parse_args()

    suites = fw.discover(pattern=args.only, include_slow=args.all)
    if args.list:
        for kind, path, meta in suites:
            print("%-6s %-34s %-16s %s" % (kind, path.name, ",".join(meta.get("tags", [])),
                                           meta.get("title", "")))
        return 0
    if not suites:
        print("没有匹配的 suite")
        return 2

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    sandbox = fw.SANDBOX_ROOT / stamp
    report_dir = fw.REPORTS_ROOT / stamp
    sandbox.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 78)
    print("kb-rag 测试框架 ｜ %s ｜ 仓库 %s @ %s" % (stamp, fw.REPO.name, guards.head_commit()))
    print("=" * 78)

    results = []
    fatal = []

    # —— 前置：保底检查（失败即整轮失败，因为后续结论都不可信）——
    print("\n[前置] 保底检查")
    ok, detail = guards.twin_engines_equal()
    if not ok and args.fix_twin:
        shutil.copy2(fw.REPO / "kb_engine.py", fw.REPO / "npm-package" / "kb_engine.py")
        ok, detail = guards.twin_engines_equal()
        print("  ·  已按 --fix-twin 同步引擎副本")
    print(("  PASS  " if ok else "  FAIL  ") + "双份引擎逐字节一致 | " + detail)
    if not ok:
        fatal.append("双份引擎不一致")

    ok, detail = guards.guidance_mirror_ok()
    print(("  PASS  " if ok else "  FAIL  ") + "提示镜像与 guidance.js 一致 | " + detail)
    if not ok:
        fatal.append("提示镜像漂移（跑 node tools/sync-host-guidance.mjs）")

    deps_ok, deps_detail = guards.python_deps()
    print(("  PASS  " if deps_ok else "  SKIP  ") + "Python 依赖 | " + deps_detail)
    models_ok, models_detail = guards.models_cached()
    print(("  PASS  " if models_ok else "  SKIP  ") + "模型缓存 | " + models_detail)

    real_kb = guards.default_real_kb()
    if real_kb is None:
        print("  SKIP  真实知识库（设 KB_RAG_REAL_KB 或在工作区放 .kb）")
        kb_copy = None
        kb_fp_before = None
    else:
        kb_fp_before = guards.kb_fingerprint(real_kb / "kb.sqlite")
        print("  PASS  真实知识库已定位（只读用；数据驱动用例跑副本）| %s" % real_kb)
        print("        docs=%s chunks=%s vecs=%s" % (kb_fp_before.get("docs"), kb_fp_before.get("chunks"),
                                                    kb_fp_before.get("vecs")))
        kb_copy = None

    repo_before = guards.repo_snapshot()

    # —— 跑 suite ——
    for kind, path, meta in suites:
        timeout = TIMEOUT_SLOW if "slow" in meta.get("tags", []) else TIMEOUT_FAST
        print("\n[%s] %s" % (meta["id"], meta.get("title", "")))
        t0 = time.time()
        try:
            if kind == "py":
                if meta.get("needs_models") and not models_ok:
                    rec = fw.new_record(meta)
                    rec["status"], rec["skipped"] = "skip", models_detail
                elif meta.get("needs_real_kb") and real_kb is None:
                    rec = fw.new_record(meta)
                    rec["status"], rec["skipped"] = "skip", "没有真实知识库"
                else:
                    if meta.get("needs_real_kb") and kb_copy is None:
                        kb_copy = guards.copy_real_kb(sandbox, real_kb)
                    rec = fw.run_py_suite(path, meta, timeout, sandbox,
                                          extras={"real_kb_copy": kb_copy,
                                                  "workers": args.workers,
                                                  "use_cache": not args.no_cache})
            else:
                rec = fw.run_node_suite(path, meta, timeout, sandbox)
        except fw.SkipSuite as e:
            rec = fw.new_record(meta)
            rec["status"], rec["skipped"] = "skip", str(e)
        except Exception as e:                                    # noqa: BLE001
            rec = fw.new_record(meta)
            rec["status"], rec["error"] = "error", "%s: %s" % (type(e).__name__, e)
        rec["seconds"] = rec.get("seconds") or round(time.time() - t0, 1)
        results.append(rec)
        if rec["status"] in ("fail", "error"):
            print("  -> %s（%.1fs）%s" % (rec["status"].upper(), rec["seconds"], rec.get("error") or ""))
        if args.fail_fast and rec["status"] in ("fail", "error"):
            break

    # —— 后置：保底复核 ——
    print("\n[后置] 保底复核")
    changed = guards.repo_diff(repo_before, guards.repo_snapshot())
    if changed:
        print("  FAIL  测试改动了仓库文件：%s" % ", ".join(changed[:8]))
        fatal.append("测试改动了仓库工作树")
    else:
        print("  PASS  仓库工作树未被测试改动")

    if real_kb is not None:
        kb_fp_after = guards.kb_fingerprint(real_kb / "kb.sqlite")
        same = kb_fp_after == kb_fp_before
        print(("  PASS  " if same else "  FAIL  ") + "真实知识库未被改动（指纹一致）")
        if not same:
            print("        before=%s" % kb_fp_before)
            print("        after =%s" % kb_fp_after)
            fatal.append("真实知识库被改动")
    else:
        print("  SKIP  真实知识库指纹复核")

    # —— 汇总 ——
    passed = [r for r in results if r["status"] == "pass"]
    failed = [r for r in results if r["status"] in ("fail", "error")]
    skipped = [r for r in results if r["status"] == "skip"]
    total_checks = sum(len(r["checks"]) for r in results)
    bad_checks = sum(1 for r in results for c in r["checks"] if not c["ok"])

    print("\n" + "=" * 78)
    print("汇总：suite %d ｜ 通过 %d ｜ 失败 %d ｜ 跳过 %d ｜ 断言 %d（失败 %d）｜ 用时 %.1fs"
          % (len(results), len(passed), len(failed), len(skipped), total_checks, bad_checks,
             sum(r["seconds"] for r in results)))
    for r in failed:
        bad = [c["label"] for c in r["checks"] if not c["ok"]]
        print("  FAIL %-18s %s" % (r["id"], r.get("error") or ("、".join(bad[:3]))))
    for r in skipped:
        print("  SKIP %-18s 跳过：%s" % (r["id"], r["skipped"]))
    if fatal:
        for f in fatal:
            print("  WARN 保底失败：%s" % f)
    print("报告目录：%s" % report_dir)
    print("=" * 78)

    payload = {"stamp": stamp, "commit": guards.head_commit(), "suites": results,
               "fatal": fatal, "summary": {"pass": len(passed), "fail": len(failed),
                                           "skip": len(skipped), "checks": total_checks,
                                           "checks_failed": bad_checks}}
    out = Path(args.json) if args.json else (report_dir / "report.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    (report_dir / "sandbox_path.txt").write_text(str(sandbox), encoding="utf-8")
    print("JSON：%s ｜ 沙箱：%s" % (out, sandbox))

    return 1 if (failed or fatal) else 0


if __name__ == "__main__":
    sys.exit(main())
