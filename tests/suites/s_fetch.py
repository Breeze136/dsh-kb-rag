"""下载闭环（kb_fetch，需要网络 + 模型）：DOI / arXiv / URL 形式 / 错误路径 / 下载即入库。

标了 slow（要联网、每个标识符几秒），默认不跑：`python tests/run.py --all --only fetch`。
离线时整体 SKIP，不会误报失败。

**本套件同时是 P0 的回归**：引擎的 `_node_doi_pdf_script()` 只在**引擎所在目录**旁找
`scripts/doi_pdf.mjs` / `tools/doi_pdf.mjs` / `doi_pdf.mjs`。找不到就静默退化成 Python urllib
兜底，而兜底**不认裸 arXiv ID**（`_candidate_sources` 返回空）→ arXiv 一律下载失败、
文件名也丢掉标题。所以这里必须断言：① 下载器可定位；② arXiv 标识符走的是 `source='arxiv'`。
"""
from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests" / "suites"))
from _helpers import Engine, load_engine                                # noqa: E402

SUITE = {"id": "fetch", "title": "下载闭环（DOI/arXiv/URL/错误路径/下载即入库）",
         "tags": ["slow"], "needs_models": True}

ARXIV_OLD = "arXiv:1306.5856"                 # 4 位.4 位（老式编号）
DOI_OA = "10.1038/s41467-017-01334-5"         # 开放获取，走出版商落地页


def _online(timeout=12):
    try:
        req = urllib.request.Request("https://arxiv.org/abs/1306.5856", method="HEAD",
                                     headers={"User-Agent": "kb-rag-tests/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return getattr(r, "status", 200) < 500
    except Exception:                                             # noqa: BLE001
        return False


def _by_id(resp):
    return {str(f.get("id")): f for f in (resp.get("files") or [])}


def run(ctx):
    # ── ① 离线也能查的回归：下载器必须可定位 ─────────────────────────────
    eng = load_engine("kbe_fetch_probe")
    script = eng._node_doi_pdf_script()
    has_node = script is not None
    ctx.check("引擎能定位 Node 下载器（否则 kb_fetch 会静默退化成 Python 兜底）",
              has_node, script or "None")
    if not has_node:
        ctx.info("提示：仓库布局下应为 <repo>/tools/doi_pdf.mjs；部署到工作区时安装器必须复制 "
                 "<工作区>/scripts/doi_pdf.mjs")
        ctx.skip("Node 下载器不可定位，天气以外的下载断言无意义")

    # ── ② 联网检查 ─────────────────────────────────────────────────────
    if not _online():
        ctx.skip("网络不可达（arxiv.org HEAD 失败）")

    kb = ctx.tmpdir("kb_fetch")
    dl = ctx.tmpdir("downloads_fetch")

    with Engine() as e:
        # ── ③ 正常下载：arXiv + DOI 各一条（同时覆盖两种解析路径） ────────
        r = e.call("fetch", {"kb_root": str(kb), "target_dir": str(dl),
                             "identifiers": [ARXIV_OLD, DOI_OA]})
        by = _by_id(r)
        a = by.get("1306.5856")     # 引擎归一化后会剥掉 arXiv: 前缀
        d = by.get(DOI_OA)
        ctx.check("arXiv 标识符下载成功且走的是 arXiv 直连（source=arxiv）",
                  bool(a) and a.get("status") == "downloaded" and a.get("source") == "arxiv",
                  (a or {}).get("source"))
        ctx.check("DOI 下载成功", bool(d) and d.get("status") == "downloaded", (d or {}).get("status"))
        for label, rec in (("arXiv", a), ("DOI", d)):
            p = Path(rec.get("path") or "")
            ctx.check("%s：落盘文件存在且是 PDF（%%PDF- 头）" % label,
                      p.is_file() and p.read_bytes()[:5] == b"%PDF-",
                      "%s (%s bytes)" % (p.name if p.name else "?", rec.get("bytes")))
        ctx.check("下载总数 2/2", r.get("downloaded") == 2, r.get("downloaded"))

        # ── ④ URL 形式输入：前缀应被归一化剥掉 ─────────────────────────────
        r2 = e.call("fetch", {"kb_root": str(kb), "target_dir": str(dl),
                              "identifiers": ["https://arxiv.org/abs/1306.5856"]})
        ctx.check("https://arxiv.org/abs/… 形式也能解析",
                  r2.get("downloaded") == 1, (r2.get("files") or [{}])[0].get("status"))

        # ── ⑤ 错误路径：坏标识符给出原因而不是抛异常 ──────────────────────
        r3 = e.call("fetch", {"kb_root": str(kb), "target_dir": str(dl),
                              "identifiers": ["not-a-real-id-xyz"]})
        f3 = (r3.get("files") or [{}])[0]
        ctx.check("坏标识符：status=failed 且带可读原因",
                  f3.get("status") == "failed" and len(str(f3.get("error") or "")) > 10,
                  str(f3.get("error"))[:110])
        ctx.check("坏标识符：不会误报成功", r3.get("downloaded") == 0, r3.get("downloaded"))

        # ── ⑥ 空列表：明确拒绝 ────────────────────────────────────────────
        r4 = e.call("fetch", {"kb_root": str(kb), "target_dir": str(dl), "identifiers": []})
        ctx.check("空 identifiers 明确拒绝",
                  r4.get("ok") is False and "必填" in str(r4.get("error") or ""),
                  str(r4.get("error"))[:80])

        # ── ⑦ 下载即入库：ingest=true ────────────────────────────────────
        r5 = e.call("fetch", {"kb_root": str(kb), "target_dir": str(dl),
                              "identifiers": [ARXIV_OLD], "ingest": True})
        ing = r5.get("ingest")
        ctx.check("ingest=true 时响应里带回入库结果（不是静默丢弃）",
                  isinstance(ing, dict) and isinstance(ing.get("totals"), dict), type(ing).__name__)
        if isinstance(ing, dict):
            ctx.check("入库结果里确实新增了文档", (ing.get("totals") or {}).get("added", 0) >= 1,
                      (ing.get("totals") or {}))
        docs = e.call("stats", {"kb_root": str(kb)}).get("docs")
        ctx.check("入库后的库里有文档（下载 → 入库闭环成立）", (docs or 0) >= 1, docs)
