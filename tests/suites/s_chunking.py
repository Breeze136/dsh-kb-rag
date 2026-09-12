"""References 判定与引文关联的**合成**用例（不依赖真实库、不依赖模型）。

这里放的是"曾经真的踩过"的版式：
  · Wiley 紧贴式 `1J. Valasek`（曾被正则排除，某篇综述 237 条只剩 5 条）
  · 正文编号列表 / 作者单位行 / `2D materials`（曾被误判成条目）
  · 标题与条目挤在一段的"流式"版式 / 巨段文献表（行首锚定检测看不到）
  · 补充材料 `S1.` 编号
  · 整篇被判 References（必须由兜底保证仍可检索）
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests" / "suites"))
from _helpers import load_engine                                       # noqa: E402

SUITE = {"id": "chunking", "title": "References 判定 / 引文关联（合成版式）", "tags": ["fast"]}


def sections(chunks):
    out = {}
    for sec, w, *_ in chunks:
        out.setdefault(sec, []).append(w)
    return out


def run(ctx):
    k = load_engine("kbe_chunk")

    # —— A. 有标题 + 常规条目：必须判出来 ——
    a = "\n\n".join([
        "Graphene growth on copper by chemical vapour deposition",
        "Abstract\n\nMonolayer graphene domains nucleate on copper foils and coalesce into a "
        "continuous film; Raman mapping confirms the layer number across the wafer in samples.",
        "Introduction\n\nChemical vapour deposition on copper is the standard route to large-area "
        "graphene, and domain coalescence controls the final sheet resistance of the film.",
        "Methods\n\nCopper foils were annealed at 1050 C in hydrogen, then methane was introduced "
        "for 30 minutes as the carbon source at a partial pressure of 20 mTorr.",
        "Results\n\nDomain coalescence reduces the sheet resistance of the transferred film and the "
        "Raman 2D/G ratio tracks the layer number across the whole wafer in our maps.",
        "References\n\n" + "\n".join(
            "%d. Author %s, Carbon %d, %d, %d. doi:10.5555/12345%03d" % (i, chr(64 + i), 2010 + i, i, 100 + i, i)
            for i in range(1, 13)),
    ])
    ch = k.chunk_document(a)
    sec = sections(ch)
    ctx.check("常规 References 被判出（weight 0 保留）",
              "References" in sec and all(w == 0 for w in sec["References"]), sorted(sec))
    ctx.check("正文仍有可检索分块", sum(1 for _s, w, *_ in ch if w > 0) >= 3)

    # —— B. Wiley 紧贴式 + 内容：条目与文内编号都要认 ——
    wiley = "REFERENCES\n\n1J. Valasek, Phys. Rev. 17(4), 475 (1921).\n" \
            "2D. R. Callaby, J. Appl. Phys. 36(9), 2751 (1965).\n" \
            "3Y. S. Kim, Appl. Phys. Lett. 86(10), 102907 (2005).\n"
    refs = k._parse_references(wiley)
    ctx.check("紧贴式 '1J. Valasek' 被认成条目", 1 in refs and "Valasek" in refs[1], sorted(refs))
    ctx.check("紧贴式条目数 ≥3（不再整表丢失）", len(refs) >= 3, len(refs))
    ctx.check("'2D materials' 不会被当成条目",
              not k._REF_ENTRY_TIGHT_RE.match("2D materials such as graphene"))
    ctx.check("'1J.' 与 '1Smith' 仍被认成条目",
              bool(k._REF_ENTRY_TIGHT_RE.match("1J. Valasek, Phys. Rev. 1, 1 (1920)."))
              and bool(k._REF_ENTRY_TIGHT_RE.match("1Smith J., Nature 411, 665 (2001).")))

    # —— C. 补充材料 S 编号（条目侧 + 文内引用侧）——
    srefs = k._parse_references("References:\nS1. Lee, C. et al. ACS Nano 4, 2695 (2010).\n"
                                "S2. Li, H. et al. Adv. Funct. Mater. 22, 1 (2012).\n")
    ctx.check("S1./S2. 编号被认成条目 1/2", sorted(srefs) == [1, 2], sorted(srefs))
    ctx.check("文内 [S3] 被识别为引用编号",
              "S3" in k._INCITE_RE.findall("as shown previously [S3] and [1,2]"))
    ctx.check("S 编号可展开（S1-S3 → 1,2,3）", k._expand_incite_nums("S1-S3") == [1, 2, 3])

    # —— D. 正文里的"像条目"内容：不许吞 ——
    b = "\n\n".join([
        "Layer-number assignment in graphene by Raman spectroscopy",
        "Abstract\n\nWe assign the layer number of graphene from the 2D and G band line shapes.",
        "1,2,3,*",
        "1 School of Materials Science, Example University, Example City 100000, China",
        "2 Key Laboratory of Example Physics, Example Institute, Example City 200000, China",
        "Introduction\n\n"
        "1. Introduction to layer assignment: the 2D band is sensitive to the number of layers.\n"
        "2. Experimental details: single-layer and bilayer regions were identified optically.\n"
        "3. Data analysis: the 2D/G intensity ratio was extracted for each region.\n"
        "4. Discussion: the ratio decreases as the layer number grows.\n"
        "5. Conclusion: Raman line shape is a reliable thickness probe.\n"
        "2D materials such as graphene and 3D graphite share the same in-plane lattice, while "
        "4H-SiC and 3C-SiC substrates serve as growth templates in the same experiment.",
        "Results\n\nThe 2D/G ratio of monolayer graphene is about 2.5, while bilayer regions show "
        "a ratio near 1.2 in the same map, measured at room temperature.",
    ])
    chb = k.chunk_document(b)
    ctx.check("正文编号列表/单位行没被判成 References",
              not any(s == "References" for s, *_ in chb), sorted(sections(chb)))
    ctx.check("整篇没有 weight<=0 的分块被丢弃", all(w > 0 for _s, w, *_ in chb))

    # —— E. 巨段文献表（条目不在行首 + 编号不严格递增）——
    giant = "Title of a review about graphene plasmonics\n\n" + (
        "The field has grown rapidly over the past decade and several reviews exist. ") * 3 + (
        "[1] A. Author, Nature Photonics 5, 1 (2011). [2] B. Author, Nano Letters 12, 2 (2012). "
        "[4] C. Author, ACS Nano 7, 3 (2013). [3] D. Author, Physical Review B 88, 4 (2013). "
        "[5] E. Author, Science 340, 5 (2013). [6] F. Author, Nature Materials 12, 6 (2013). ")
    chg = k.chunk_document(giant)
    refs_g = [t for s, _w, t, *_ in chg if s == "References"]
    ctx.check("巨段文献表（编号交叉）仍被识别", bool(refs_g), sorted(sections(chg)))

    # —— F. 兜底：整篇只剩 weight 0 时必须写回 ——
    patho = "\n\n".join([
        "Title only",
        "Acknowledgements\n\n" + ("The authors thank the facility staff for support. " * 20),
        "Appendix\n\n" + ("Supplementary derivation of the line-shape model. " * 20),
        "References\n\n" + "\n".join(
            "%d. Author %s, Journal of Examples %d, %d, %d. doi:10.5555/99999%03d"
            % (i, chr(64 + i), 2000 + i, i, 50 + i, i) for i in range(1, 20)),
    ])
    chp = k.chunk_document(patho)
    ctx.check("病态输入下仍存在 weight>0 分块（不变量）",
              any(w > 0 for _s, w, *_ in chp), sorted(sections(chp)))
    ctx.check("兜底块带 (rescued) 标记", any("rescued" in s for s, *_ in chp), sorted(sections(chp)))

    # —— G. 大文献表 >60% 字符：保留（不再一刀切作废）——
    big = "\n\n".join(["Short body paragraph with enough characters to survive chunking."] +
                      ["%d. Author %s, Journal of Examples %d, %d, %d. doi:10.5555/888%03d"
                       % (i, chr(64 + i), 1990 + i, i, i, i) for i in range(1, 25)])
    chbig = k.chunk_document(big)
    ctx.check("条目表占全文 >60% 字符时仍保留为 References",
              any(s == "References" for s, *_ in chbig), sorted(sections(chbig)))

    # —— H. 字符占比口径（不能用段落序号）——
    ctx.check("位置口径常量是字符占比", 0 < k.REF_HEAD_MIN_CHAR_POS < 1 and 0 < k.REF_CHAIN_MIN_CHAR_POS < 1,
              (k.REF_HEAD_MIN_CHAR_POS, k.REF_CHAIN_MIN_CHAR_POS))
