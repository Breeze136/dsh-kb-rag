#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make_brand_assets.py — 生成 kb-rag 的封面图与产品图标。

在仓库根目录运行：

    python tools/make_brand_assets.py            # 写 docs/assets/brand/
    python tools/make_brand_assets.py --check    # 只校验已有产物的尺寸与体积

为什么是"脚本 + 图"而不是只有图：封面每改一次文案都要重排，手改 PNG 不可持续。
脚本把色板、字体、文案、版式坐标都写成常量，重跑即可得到同一张图。

字体只栅格化进 PNG，不随仓库分发：正文与图标用 Noto Serif SC（OFL，含 CJK），
等宽标签用 Consolas；找不到时可用 KB_BRAND_SERIF / KB_BRAND_MONO 指到别的字体文件。
图标是字标首字母 K；icon.svg 里的 K 是 Noto 的字形轮廓（OFL 允许嵌入），PNG 与 SVG
是同一个字形，不依赖查看者的系统字体。

依赖：Pillow；生成 icon.svg 另需 fontTools。Python 3.9+。
"""
import argparse
import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                            # noqa: BLE001
    pass

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "docs" / "assets" / "brand"

# 超采样倍数：先画 2 倍再 LANCZOS 缩回，文字边缘才干净。
S = 2
W, H = 1600, 800

# ---------------------------------------------------------------- 色板
# 蓝色主题：取答案页的 blue 预设（accent #1f5fa8 / tint #e8f0fa），
# 纸色、墨色、细线沿用答案页的印刷感，品牌与产品共用一套色。
LIGHT = dict(
    bg="#FBF9F4", ink="#191C1E", ink2="#3B4144", muted="#757065",
    accent="#1F5FA8", edge="#EDE6D8", shadow=(31, 45, 70, 30),
    card="#FFFDF8", line="#DDD5C6", tint="#E8F0FA", bar="#C7D1DF",
    plate_bg="#E8F0FA", plate_bg2="#DCE8F8", plate_bg3="#C9DBF2",
    plate_fg="#1F5FA8", plate_line="#C9DBF2",
)
DARK = dict(
    bg="#0F1218", ink="#EEF2F8", ink2="#C9D2DE", muted="#93A0B0",
    accent="#9CC3F0", edge="#222831", shadow=(0, 0, 0, 150),
    card="#171B22", line="#262C36", tint="#1E3350", bar="#2E3947",
    plate_bg="#223650", plate_bg2="#1B2C44", plate_bg3="#15243A",
    plate_fg="#9CC3F0", plate_line="#2E4463",
)
PALETTES = {"light": LIGHT, "dark": DARK}

# ---------------------------------------------------------------- 文案
# 只用石墨烯示例、占位期刊/年份（仓库 AGENTS.md 的公开内容约定）。
COPY = {
    "en": dict(
        eyebrow="LOCAL LITERATURE RAG",
        wordmark="kb-rag",
        tagline=["Every answer traces back to", "a section, a page and a DOI."],
        footer="PDF · Zotero → SQLite · hybrid + rerank · self-contained HTML",
    ),
    "zh": dict(
        eyebrow="本地文献知识库",
        wordmark="kb-rag",
        tagline=["每条答案都能追到", "章节、页码与 DOI。"],
        footer="PDF · Zotero → SQLite · 混合检索 + 精排 · 自包含 HTML",
    ),
}

# 正文里的解释图：先用一句话说清"是什么、解决什么"，再用一对卡片证明。
# 不放示例论文内容：问题/答案句子、期刊年份、DOI 值、作者都不出现，只用文字条 +
# 类别标签；两卡同一条高亮 + [1]，就是"这句答案出自那段原文"的视觉线索。
EXPLAIN = {
    "en": dict(
        eyebrow="LOCAL LITERATURE RAG",
        title=["Ask your PDFs.", "Never lose the source."],
        sub="Search your own PDFs and Zotero library — every answer is grounded in a passage, with its section, page and DOI.",
        answer_label="ANSWER",
        answer_bars=((452, False), (340, True), (420, False), (268, False)),
        answer_chip="chat card · HTML",
        source_label="[1] SOURCE",
        source_origin="from your library",
        source_bars=((440, False), (340, True), (420, False), (348, False)),
        source_tags="section · page · DOI",
        source_chip="citation chain · in library",
        footer="runs locally · nothing uploaded · no API cost",
    ),
    "zh": dict(
        eyebrow="本地文献知识库",
        title=["直接问你的 PDF，出处再也不会丢。"],
        sub="检索你自己的 PDF 与 Zotero 文库——每条答案都落在原文段落上，并带章节、页码与 DOI。",
        answer_label="答案",
        answer_bars=((452, False), (340, True), (420, False), (268, False)),
        answer_chip="对话卡片 · HTML",
        source_label="[1] 来源",
        source_origin="检索自你的文献库",
        source_bars=((440, False), (340, True), (420, False), (348, False)),
        source_tags="章节 · 页码 · DOI",
        source_chip="引文链 · 库内",
        footer="本地运行 · 不上传 · 无 API 费用",
    ),
}

# 故事页 2：怎么做到（本地索引 + 混合检索 + 精排）。
HOW = {
    "en": dict(
        eyebrow="HOW IT WORKS",
        title=["Data never leaves your machine.", "Retrieval costs zero tokens."],
        sub="Quick search 0.2–0.4 s, deep search 1–2 s. Index once — no model API.",
        s1=("PDF · Zotero", "your library"),
        s2=("chunks + vectors", "one SQLite file"),
        s3=("hybrid + rerank", "keyword + vector"),
        s4=("the passage", "section · page · DOI"),
        footer="chunks · local embeddings · hybrid + rerank · one SQLite file",
    ),
    "zh": dict(
        eyebrow="怎么做到",
        title=["数据不上传，检索 0 token。"],
        sub="快速检索 0.2–0.4 秒，深度检索 1–2 秒；索引只做一次，不调用模型 API。",
        s1=("PDF · Zotero", "你的文库"),
        s2=("分块 + 向量", "一个 SQLite 文件"),
        s3=("混合检索 + 精排", "关键词 + 向量"),
        s4=("原文段落", "章节 · 页码 · DOI"),
        footer="分块 · 本地向量 · 混合检索 + 精排 · 一个 SQLite 文件",
    ),
}

# 故事页 3：怎么用（建库 → 对话里提问 → 出成品页）。
# 只出现工具名与类别名；提问本身也画成文字条，不写示例问题。
FLOW = {
    "en": dict(
        eyebrow="HOW YOU USE IT",
        title=["Built-in HTML reports, instant and readable."],
        sub="Index once; every question returns a passage you can point at, and any answer can become a shareable page.",
        p1=("your library", "PDF · Zotero", "kb_ingest"),
        p2=("ask in the chat", "one question → passages", "kb_search · kb_rag"),
        p3=("share the page", "self-contained HTML", "answer_file"),
        footer="ingest once · ask · share — nothing leaves the machine",
    ),
    "zh": dict(
        eyebrow="怎么用",
        title=["内置 HTML 生成器，超快渲染，报告更好读。"],
        sub="文库只索引一次；每次提问都返回一段能指认的原文，任何答案都能变成一份可分享的页面。",
        p1=("你的文库", "PDF · Zotero", "kb_ingest"),
        p2=("在对话里提问", "一句话 → 原文段落", "kb_search · kb_rag"),
        p3=("把答案发出去", "自包含 HTML", "answer_file"),
        footer="入库一次 · 提问 · 分享 —— 数据不出本机",
    ),
}

# 正文图 4：答案形态。四张页面缩略，骨架不同；十种模板 id 全部写在脚注里。
TEMPLATES = {
    "en": dict(
        eyebrow="ANSWER TEMPLATES",
        title=["Ten templates.", "One engine."],
        sub="The engine lays out tables, bars, charts, KPI cards and timelines itself — no model call. Four of the ten:",
        tiles=(("instant", "one-screen answer"), ("compare", "table + bottom line"),
               ("numeric", "KPIs + bars"), ("timeline", "events or a curve")),
    ),
    "zh": dict(
        eyebrow="答案模板",
        title=["十种模板，一个引擎。"],
        sub="章节、表格、数值条、曲线、指标卡、时间线都由引擎排版，不调用模型。十种里的四种：",
        tiles=(("instant", "一屏直答"), ("compare", "对比表 + 结论"),
               ("numeric", "指标卡 + 数值条"), ("timeline", "时间线")),
    ),
}
TEMPLATE_IDS = ("instant · review · method · compare · numeric · timeline · "
                "concept · dispute · derivation · qa")

# 下一版故事（4 页）：问题 → 单元 → 并置 → 增量。
# 每页只讲一件事：标题 + 一句副标题 + 少量图内标签，信息分散而不是堆在一张图里。
LEDGER_P1 = {
    "en": dict(
        eyebrow="THE PROBLEM",
        title=["You read the paper.", "You keep the headline."],
        sub="Conditions, values and caveats sit in single sentences that never reach the abstract — and rarely survive the week.",
        left_caption="day 0 · what the paper says",
        right_caption="day 7 · what you recall",
        lost=("conditions", "values", "caveats"),
        arrow_label="one week",
        footer="the narrative stays; the details stay in the PDF",
    ),
    "zh": dict(
        eyebrow="问题",
        title=["论文读过了，", "记住的只有主线。"],
        sub="条件、数值与边界散落在单句里，不进摘要，也很难留过一周。",
        left_caption="第 0 天 · 论文里写的",
        right_caption="第 7 天 · 你还记得的",
        lost=("条件", "数值", "边界"),
        arrow_label="一周后",
        footer="主线留下了，细节还在 PDF 里",
    ),
}

LEDGER_P2 = {
    "en": dict(
        eyebrow="THE EVIDENCE",
        title=["One passage.", "The result, and where it sits."],
        sub="The card carries the metadata; the right side is the paragraph in the PDF, matched sentence in place.",
        result_label="RESULT",
        meta="Author A · 2024 · Carbon",
        quote="annealed at 900 °C for 10 min",
        quote_hi="900 °C",
        result_chip="matched in full text",
        pdf_label="IN THE PDF",
        page="p.4",
        connector="verbatim",
        footer="the snippet is not a summary; it is the paragraph, highlighted",
    ),
    "zh": dict(
        eyebrow="证据",
        title=["一段原文。", "结果卡片，和它在论文里的位置。"],
        sub="左边是结果卡片，右边是 PDF 里的那段原文，命中的句子还在原位。",
        result_label="结果",
        meta="Author A · 2024 · Carbon",
        quote="在 900 °C 退火 10 min",
        quote_hi="900 °C",
        result_chip="全文命中",
        pdf_label="论文原文",
        page="p.4",
        connector="逐字一致",
        footer="片段不是摘要，就是原文那一段，命中的句子还在原位",
    ),
}

LEDGER_P3 = {
    "en": dict(
        eyebrow="THE CHAIN",
        title=["What the passage cites,", "and what you already hold."],
        sub="Markers in the passage resolve to reference entries; cited works already in your library are marked.",
        passage_label="PASSAGE · p.4",
        citation_label="CITATIONS FROM THIS PASSAGE",
        line1="Oxidation above 800 °C",
        line2="has been reported [1,2].",
        line2_hi="[1,2]",
        note="in-text [n] → reference list → a DOI or a page",
        rows=(("Ref 1", "Author C · 2010 · Carbon", "in library", "open in Zotero"),
              ("Ref 2", "Author D · 2012 · Carbon", "not indexed", "")),
        collapsed="3 further citations · Ref 6-8",
        footer="in-library means the cited paper is indexed here and can be searched",
    ),
    "zh": dict(
        eyebrow="引文链",
        title=["这段原文引了谁，", "谁已经在你库里。"],
        sub="正文里的 [n] 会解析到参考文献条目；被引文献若已在库内，会直接标出来。",
        passage_label="原文 · 第 4 页",
        citation_label="这段原文的引文",
        line1="已有工作报道",
        line2="800 °C 以上会氧化 [1,2]。",
        line2_hi="[1,2]",
        note="正文 [n] → 参考文献条目 → DOI 或页码",
        rows=(("Ref 1", "Author C · 2010 · Carbon", "库内", "在 Zotero 中打开"),
              ("Ref 2", "Author D · 2012 · Carbon", "未入库", "")),
        collapsed="另有 3 条引文 · Ref 6-8",
        footer="「库内」表示这篇被引文献已经入库，可以直接检索",
    ),
}

LEDGER_P4 = {
    "en": dict(
        eyebrow="SIDE BY SIDE",
        title=["Four papers, one grid.", "Conflicts included."],
        sub="Conditions, values, limits and counterexamples line up. A blank cell means the text does not report it.",
        meta="Zotero · 37 papers · 4 shown",
        cols=("PAPER", "CONDITIONS", "VALUE", "BOUNDARY", "COUNTEREXAMPLE"),
        empty="not reported",
        rows=(
            ("Paper A", [("900 °C · 1 mbar", "p.4", "conf"), ("4×10⁴/cm²", "p.6", ""),
                         ("≤ 5 layers", "p.7", ""), None]),
            ("Paper B", [("700 °C · APCVD", "p.3", ""), ("3×10³/cm²", "p.5", ""),
                         ("monolayer", "p.5", ""), ("> 800 °C oxidises", "p.8", "conf")]),
            ("Paper C", [("plasma, 30 s", "p.2", ""), ("97 %", "p.6", ""),
                         None, ("not on Si", "p.9", "")]),
            ("Paper D", [("LPCVD 850 °C", "p.3", ""), None,
                         ("≤ 3 layers", "p.6", ""), None]),
        ),
        footer="≠ marks a conflict · blank means the text does not report it",
    ),
    "zh": dict(
        eyebrow="并置",
        title=["四篇论文，", "一张表。"],
        sub="条件、数值、边界与反例并排；空白表示原文没有报告。",
        meta="Zotero · 37 篇 · 展示 4 篇",
        cols=("论文", "条件", "数值", "边界", "反例"),
        empty="未报告",
        rows=(
            ("论文 A", [("900 °C · 1 mbar", "p.4", "conf"), ("4×10⁴/cm²", "p.6", ""),
                        ("≤ 5 层", "p.7", ""), None]),
            ("论文 B", [("700 °C · APCVD", "p.3", ""), ("3×10³/cm²", "p.5", ""),
                        ("单层", "p.5", ""), (">800 °C 会氧化", "p.8", "conf")]),
            ("论文 C", [("等离子 30 s", "p.2", ""), ("97 %", "p.6", ""),
                        None, ("硅基底不适用", "p.9", "")]),
            ("论文 D", [("LPCVD 850 °C", "p.3", ""), None,
                        ("≤ 3 层", "p.6", ""), None]),
        ),
        footer="≠ 表示两篇结论冲突 · 空白表示原文未报告",
    ),
}

LEDGER_P5 = {
    "en": dict(
        eyebrow="IT GROWS",
        title=["A new paper fills cells.", "It does not rewrite rows."],
        sub="When the library grows, only the affected cells change; a disagreement is reported, not averaged away.",
        meta="Zotero · 38 papers · 1 new",
        cols=("PAPER", "CONDITIONS", "VALUE", "BOUNDARY", "COUNTEREXAMPLE"),
        empty="not reported",
        new="NEW",
        rows=(
            ("Paper A", [("900 °C · 1 mbar", "p.4", "conf"), ("4×10⁴/cm²", "p.6", ""),
                         ("≤ 5 layers", "p.7", ""), None]),
            ("Paper B", [("700 °C · APCVD", "p.3", ""), ("3×10³/cm²", "p.5", ""),
                         ("monolayer", "p.5", ""), ("> 800 °C oxidises", "p.8", "conf")]),
            ("Paper C", [("plasma, 30 s", "p.2", ""), ("97 %", "p.6", ""),
                         None, ("not on Si", "p.9", "")]),
            ("Paper D", [("850 °C · LPCVD", "p.3", ""), ("6×10⁴/cm²", "p.5", ""),
                         ("≤ 3 layers", "p.6", ""), ("900 °C works", "p.9", "conf")]),
        ),
        footer="a new paper fills the cells it touches; nothing else moves",
    ),
    "zh": dict(
        eyebrow="会长",
        title=["新论文补格，", "不改写旧行。"],
        sub="文库增长时只更新受影响的单元格；和已有结论冲突就标出来，不取平均。",
        meta="Zotero · 38 篇 · 新增 1 篇",
        cols=("论文", "条件", "数值", "边界", "反例"),
        empty="未报告",
        new="新增",
        rows=(
            ("论文 A", [("900 °C · 1 mbar", "p.4", "conf"), ("4×10⁴/cm²", "p.6", ""),
                        ("≤ 5 层", "p.7", ""), None]),
            ("论文 B", [("700 °C · APCVD", "p.3", ""), ("3×10³/cm²", "p.5", ""),
                        ("单层", "p.5", ""), (">800 °C 会氧化", "p.8", "conf")]),
            ("论文 C", [("等离子 30 s", "p.2", ""), ("97 %", "p.6", ""),
                        None, ("硅基底不适用", "p.9", "")]),
            ("论文 D", [("850 °C · LPCVD", "p.3", ""), ("6×10⁴/cm²", "p.5", ""),
                        ("≤ 3 层", "p.6", ""), ("900 °C 可行", "p.9", "conf")]),
        ),
        footer="新论文只补它碰到的格子，其余不动",
    ),
}

# ---------------------------------------------------------------- 字体
WINDIR = Path(os.environ.get("WINDIR", r"C:\Windows"))
SERIF_CANDIDATES = [
    os.environ.get("KB_BRAND_SERIF"),
    WINDIR / "Fonts" / "NotoSerifSC-VF.ttf",
    WINDIR / "Fonts" / "NotoSerifSC-Regular.otf",
    Path("/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc"),
    WINDIR / "Fonts" / "georgia.ttf",
]
MONO_CANDIDATES = [
    os.environ.get("KB_BRAND_MONO"),
    WINDIR / "Fonts" / "consola.ttf",
    WINDIR / "Fonts" / "CascadiaMono.ttf",
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"),
]


def _pick(cands, label, env):
    for c in cands:
        if not c:
            continue
        p = Path(str(c)).expanduser()
        if p.exists():
            return p
    raise SystemExit(
        "找不到%s字体。装 Noto Serif SC（OFL），或用环境变量 %s 指向 font 文件。" % (label, env))


SERIF = _pick(SERIF_CANDIDATES, "衬线（Noto Serif SC）", "KB_BRAND_SERIF")
MONO = _pick(MONO_CANDIDATES, "等宽（Consolas）", "KB_BRAND_MONO")

_cache = {}


def serif(size, weight="Regular"):
    key = ("s", int(size), weight)
    if key not in _cache:
        f = ImageFont.truetype(str(SERIF), int(size))
        try:
            f.set_variation_by_name(weight)
        except Exception:                                    # noqa: BLE001
            pass                                             # 静态字体没有 weight 轴，按默认字重
        _cache[key] = f
    return _cache[key]


def mono(size):
    key = ("m", int(size))
    if key not in _cache:
        _cache[key] = ImageFont.truetype(str(MONO), int(size))
    return _cache[key]


# ---------------------------------------------------------------- 画布
class Canvas:
    """设计坐标（1600×800）→ 超采样画布，所有尺寸内部乘 S。"""

    def __init__(self, w=W, h=H, bg="#FFFFFF"):
        self.w, self.h = w, h
        self.im = Image.new("RGBA", (int(w * S), int(h * S)), bg)
        self.d = ImageDraw.Draw(self.im)

    def u(self, v):
        return int(round(v * S))

    def box(self, b):
        return tuple(self.u(v) for v in b)

    def rect(self, b, fill=None, outline=None, width=1):
        self.d.rectangle(self.box(b), fill=fill, outline=outline, width=max(1, self.u(width)))

    def rounded(self, b, r, fill=None, outline=None, width=1):
        self.d.rounded_rectangle(self.box(b), radius=self.u(r), fill=fill,
                                 outline=outline, width=max(1, self.u(width)))

    def line(self, xy, fill, width=1):
        self.d.line(self.box(xy), fill=fill, width=max(1, self.u(width)))

    def text(self, xy, s, font, fill, anchor="ls", tracking=0.0):
        if not s:
            return
        if not tracking:
            self.d.text((self.u(xy[0]), self.u(xy[1])), s, font=font, fill=fill, anchor=anchor)
            return
        x = xy[0]
        for ch in s:
            self.d.text((self.u(x), self.u(xy[1])), ch, font=font, fill=fill, anchor=anchor)
            x += (self.d.textlength(ch, font=font) + tracking * S) / S

    def width(self, s, font):
        return self.d.textlength(s, font=font) / S

def shadow(cv, b, r, offset, blur, color):
    layer = Image.new("RGBA", cv.im.size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    x0, y0, x1, y1 = cv.box(b)
    ox, oy = cv.u(offset[0]), cv.u(offset[1])
    ld.rounded_rectangle((x0 + ox, y0 + oy, x1 + ox, y1 + oy), radius=cv.u(r), fill=color)
    layer = layer.filter(ImageFilter.GaussianBlur(cv.u(blur)))
    cv.im.alpha_composite(layer)


# ---------------------------------------------------------------- K 标记
# 字标首字母，用 Noto Serif SC 的 K。OFL 允许把字形轮廓嵌进 icon.svg，
# 因此 PNG 与 SVG 是同一个字形，不靠查看者的系统字体。
K_WEIGHT = "Bold"
K_LETTER = "K"


def k_ink_box(box, fill_ratio=0.52):
    """在方框里放 K 的墨迹框：高度 = 方框高 × fill_ratio，宽度按字形比例。"""
    x0, y0, x1, y1 = box
    h = (y1 - y0) * fill_ratio
    w = h * 0.72
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


def draw_k(d, box, fill):
    """把 K 的墨迹恰好放进 box（基于实测 bbox 缩放，不靠 em 框猜）。"""
    x0, y0, x1, y1 = box
    trial = serif(int((y1 - y0) * 2.2), K_WEIGHT)
    bb = d.textbbox((0, 0), K_LETTER, font=trial)
    scale = (y1 - y0) / max(1, bb[3] - bb[1])
    f = serif(max(6, int(trial.size * scale)), K_WEIGHT)
    bb = d.textbbox((0, 0), K_LETTER, font=f)
    x = (x0 + x1) / 2 - (bb[0] + bb[2]) / 2
    y = (y0 + y1) / 2 - (bb[1] + bb[3]) / 2
    d.text((x, y), K_LETTER, font=f, fill=fill, anchor="la")


# ---------------------------------------------------------------- 封面
def render_cover(lang, theme):
    p = PALETTES[theme]
    c = COPY[lang]
    cv = Canvas(bg=p["bg"])

    # 纸面边界：README 的白底与纸色只差一点，细框把图定义成一张"版面"
    cv.rect((0.5, 0.5, W - 0.5, H - 0.5), outline=p["edge"], width=1)

    # 左栏：eyebrow（不再重复画一枚小 K，右下已经有一枚大的）
    if lang == "zh":
        cv.text((112, 125), c["eyebrow"], serif(20 * S, "Medium"), p["muted"], tracking=3)
    else:
        cv.text((112, 125), c["eyebrow"], mono(18 * S), p["muted"], tracking=4)

    # 字标
    cv.text((110, 330), c["wordmark"], serif(150 * S, "SemiBold"), p["ink"], tracking=-4)

    # 赭金短线
    cv.rect((112, 396, 208, 400), fill=p["accent"])

    # 两行 tagline
    for i, line in enumerate(c["tagline"]):
        cv.text((112, 452 + i * 54), line, serif(38 * S), p["ink2"])

    # 页脚一行技术栈
    if lang == "zh":
        cv.text((112, 706), c["footer"], serif(19 * S), p["muted"])
    else:
        cv.text((112, 706), c["footer"], mono(16 * S), p["muted"], tracking=0.6)

    # 右下：三张叠放的卡片，顶层是 K。不放示例内容 —— 具体问题、结论、来源每页
    # 都不同，印在品牌图上既不通用，也会被当成产品截图；叠放只表达"很多份答案"。
    front = (970, 210, 1340, 580)
    shadow(cv, front, 92, (0, 22), 22, p["shadow"])
    for box, fill in (((1016, 156, 1386, 526), p["plate_bg3"]),
                      ((993, 183, 1363, 553), p["plate_bg2"])):
        cv.rounded(box, 92, fill=fill, outline=p["plate_line"], width=1)
    cv.rounded(front, 92, fill=p["plate_bg"], outline=p["plate_line"], width=1)
    draw_k(cv.d, cv.box(k_ink_box(front, 0.50)), p["plate_fg"])

    out = cv.im.convert("RGB").resize((W, H), Image.LANCZOS)
    return out


# ---------------------------------------------------------------- 正文解释图
EXP_W, EXP_H = 1600, 760


def draw_bars(cv, x, bars, p, top=424, step=40, quote=False):
    """文字条：一条高亮 + [1]，表示"这句答案出自这段原文"。"""
    for i, (w, hl) in enumerate(bars):
        y = top + i * step
        if hl:
            cv.rounded((x - 6, y - 9, x + w + 12, y + 25), 9, fill=p["tint"])
        cv.rounded((x, y, x + w, y + 16), 3, fill=p["accent"] if hl else p["bar"])
        if hl:
            if quote:
                # 悬挂引号：这一段是原文原句，不是转述
                cv.text((x - 36, y + 15), "“", serif(30 * S, "SemiBold"), p["accent"])
            cv.text((x + w + 20, y + 14), "[1]", serif(15 * S, "SemiBold"), p["accent"])


def chip(cv, x, y, text, font, p):
    w = cv.width(text, font) + 46
    cv.rounded((x, y, x + w, y + 38), 10, fill=p["tint"])
    cv.d.ellipse(cv.box((x + 17, y + 16, x + 25, y + 24)), fill=p["accent"])
    cv.text((x + 35, y + 26), text, font, p["ink2"])
    return w


def render_explainer(lang, theme):
    """一图说清：这是什么（本地文献检索）→ 换来什么（答案带原文出处）。"""
    p = PALETTES[theme]
    c = EXPLAIN[lang]
    cv = Canvas(EXP_W, EXP_H, bg=p["bg"])
    cv.rect((0.5, 0.5, EXP_W - 0.5, EXP_H - 0.5), outline=p["edge"], width=1)

    if lang == "zh":
        cv.text((96, 88), c["eyebrow"], serif(20 * S, "Medium"), p["accent"], tracking=3)
    else:
        cv.text((96, 88), c["eyebrow"], mono(17 * S), p["accent"], tracking=4)
    for i, line in enumerate(c["title"]):
        cv.text((96, 164 + i * 62), line, serif(56 * S, "SemiBold"), p["ink"])
    cv.text((96, 236 if len(c["title"]) == 1 else 276), c["sub"],
            serif(24 * S), p["ink2"])

    left = (96, 326, 736, 686)
    right = (800, 326, 1504, 686)
    for box in (left, right):
        shadow(cv, box, 16, (0, 14), 18, p["shadow"])
        cv.rounded(box, 16, fill=p["card"], outline=p["line"], width=1)

    label_f = serif(16 * S, "Medium") if lang == "zh" else mono(15 * S)
    chip_f = serif(17 * S) if lang == "zh" else mono(14 * S)

    # 左卡：答案
    cv.rounded((132, 360, 168, 396), 9, fill=p["accent"])
    avatar_fg = p["bg"] if theme == "dark" else ICON_PAPER
    draw_k(cv.d, cv.box(k_ink_box((132, 360, 168, 396), 0.56)), avatar_fg)
    cv.text((190, 388), c["answer_label"], label_f, p["accent"],
            tracking=0 if lang == "zh" else 2)
    draw_bars(cv, 132, c["answer_bars"], p)
    chip(cv, 132, 624, c["answer_chip"], chip_f, p)

    # 右卡：原文
    cv.rounded((860, 360, 888, 396), 6, fill=p["card"], outline=p["line"], width=1)
    for k in range(3):
        cv.rect((866, 368 + k * 8, 882 - k * 2, 368 + k * 8 + 3), fill=p["bar"])
    cv.text((902, 388), c["source_label"], label_f, p["accent"],
            tracking=0 if lang == "zh" else 2)
    origin_f = serif(15 * S) if lang == "zh" else mono(14 * S)
    cv.text((1468, 388), c["source_origin"], origin_f, p["muted"], anchor="rs")
    draw_bars(cv, 860, c["source_bars"], p, quote=True)
    tag_f = serif(17 * S) if lang == "zh" else mono(15 * S)
    cv.text((860, 596), c["source_tags"], tag_f, p["muted"],
            tracking=0 if lang == "zh" else 0.5)
    chip(cv, 860, 624, c["source_chip"], chip_f, p)

    # 两卡之间的箭头：答案指向原文
    ay = 472
    cv.line((744, ay, 774, ay), p["accent"], 2)
    cv.d.polygon(cv.box([774, ay - 8, 788, ay, 774, ay + 8]), fill=p["accent"])

    if lang == "zh":
        cv.text((96, 730), c["footer"], serif(17 * S), p["muted"])
    else:
        cv.text((96, 730), c["footer"], mono(15 * S), p["muted"], tracking=0.5)

    return cv.im.convert("RGB").resize((EXP_W, EXP_H), Image.LANCZOS)


# ---------------------------------------------------------------- 故事页 2 / 3
def story_header(cv, p, lang, c):
    if lang == "zh":
        cv.text((96, 88), c["eyebrow"], serif(20 * S, "Medium"), p["accent"], tracking=3)
    else:
        cv.text((96, 88), c["eyebrow"], mono(17 * S), p["accent"], tracking=4)
    for i, line in enumerate(c["title"]):
        cv.text((96, 164 + i * 62), line, serif(56 * S, "SemiBold"), p["ink"])
    cv.text((96, 236 if len(c["title"]) == 1 else 276), c["sub"],
            serif(24 * S), p["ink2"])


def story_footer(cv, p, lang, text):
    if lang == "zh":
        cv.text((96, 730), text, serif(17 * S), p["muted"])
    else:
        cv.text((96, 730), text, mono(15 * S), p["muted"], tracking=0.5)


def arrow(cv, x1, x2, y, p):
    cv.line((x1, y, x2 - 14, y), p["accent"], 2)
    cv.d.polygon(cv.box([x2 - 14, y - 7, x2, y, x2 - 14, y + 7]), fill=p["accent"])


def icon_library(cv, cx, cy, p):
    for i, fill in enumerate((p["plate_bg3"], p["plate_bg2"], p["plate_bg"])):
        dx, dy = -i * 12, -i * 12
        cv.rounded((cx - 60 + dx, cy - 55 + dy, cx + 40 + dx, cy + 55 + dy), 8,
                   fill=fill, outline=p["plate_line"], width=1)
    for k in range(3):
        cv.rounded((cx - 40, cy - 30 + k * 20, cx + 20 - k * 6, cy - 22 + k * 20),
                   2, fill=p["bar"])


def icon_chunks(cv, cx, cy, p):
    cv.rounded((cx - 58, cy - 58, cx + 58, cy + 58), 12,
               fill=p["card"], outline=p["line"], width=1)
    for row in range(3):
        for col in range(3):
            x = cx - 41 + col * 30
            y = cy - 41 + row * 30
            hl = (row, col) == (1, 1)
            cv.rounded((x, y, x + 22, y + 22), 3,
                       fill=p["accent"] if hl else p["bar"])


def icon_funnel(cv, cx, cy, p):
    for k, w in enumerate((92, 62, 32)):
        y = cy - 52 + k * 24
        cv.rounded((cx - w / 2, y, cx + w / 2, y + 12), 3, fill=p["bar"])
    y = cy + 32
    cv.rounded((cx - 46, y - 6, cx + 46, y + 14), 6, fill=p["tint"])
    cv.rounded((cx - 42, y, cx + 42, y + 9), 2, fill=p["accent"])


def icon_passage(cv, cx, cy, p):
    cv.rounded((cx - 62, cy - 56, cx + 62, cy + 56), 10,
               fill=p["card"], outline=p["line"], width=1)
    for k, (w, hl) in enumerate(((84, False), (104, True), (70, False))):
        y = cy - 30 + k * 24
        if hl:
            cv.rounded((cx - 54, y - 6, cx - 54 + w + 12, y + 15), 7, fill=p["tint"])
        cv.rounded((cx - 50, y, cx - 50 + w, y + 9), 2,
                   fill=p["accent"] if hl else p["bar"])


def render_how(lang, theme):
    p = PALETTES[theme]
    c = HOW[lang]
    cv = Canvas(EXP_W, EXP_H, bg=p["bg"])
    cv.rect((0.5, 0.5, EXP_W - 0.5, EXP_H - 0.5), outline=p["edge"], width=1)
    story_header(cv, p, lang, c)

    xs = (110, 470, 830, 1190)
    for x, fn, (title, sub) in zip(xs, (icon_library, icon_chunks, icon_funnel, icon_passage),
                                   (c["s1"], c["s2"], c["s3"], c["s4"])):
        box = (x, 340, x + 300, 680)
        shadow(cv, box, 14, (0, 12), 16, p["shadow"])
        cv.rounded(box, 14, fill=p["card"], outline=p["line"], width=1)
        fn(cv, x + 150, 440, p)
        cv.text((x + 150, 570), title, serif(24 * S, "SemiBold"), p["ink"], anchor="ms")
        sub_f = serif(19 * S) if lang == "zh" else mono(16 * S)
        cv.text((x + 150, 604), sub, sub_f, p["muted"], anchor="ms")

    for i in range(3):
        arrow(cv, xs[i] + 308, xs[i + 1] - 8, 440, p)

    story_footer(cv, p, lang, c["footer"])
    return cv.im.convert("RGB").resize((EXP_W, EXP_H), Image.LANCZOS)


# ---------------------------------------------------------------- 故事页 3：怎么用
def window_chrome(cv, box, p, kind="window"):
    """面板顶部的窗口条：三个圆点；浏览器再加一颗地址胶囊。"""
    x0, y0, x1, _ = box
    cv.line((x0, y0 + 34, x1, y0 + 34), p["line"], 1)
    for i in range(3):
        cx, cy = x0 + 24 + i * 15, y0 + 17
        cv.d.ellipse(cv.box((cx - 3.5, cy - 3.5, cx + 3.5, cy + 3.5)), fill=p["bar"])
    if kind == "browser":
        cv.rounded((x0 + 110, y0 + 9, x1 - 24, y0 + 27), 9, fill=p["tint"])


def numbered(cv, cx, cy, n, p):
    """步骤序号：实心圆 + 反白数字。"""
    cv.d.ellipse(cv.box((cx - 15, cy - 15, cx + 15, cy + 15)), fill=p["accent"])
    cv.text((cx, cy), str(n), serif(19 * S, "SemiBold"), p["bg"], anchor="mm")


def scene_library(cv, box, p):
    x0, _, x1, _ = box
    icon_library(cv, (x0 + x1) / 2 + 22, box[1] + 128, p)


def scene_chat(cv, box, p):
    """一句提问（文字条）→ 回来的来源卡（[1] + 高亮条）。"""
    x0, y0, x1, _ = box
    cx = (x0 + x1) / 2
    cv.rounded((cx - 150, y0 + 56, cx + 150, y0 + 94), 8, fill=p["tint"])
    cv.rounded((cx - 132, y0 + 70, cx + 104, y0 + 80), 4, fill=p["bar"])
    card = (cx - 160, y0 + 112, cx + 160, y0 + 194)
    cv.rounded(card, 10, fill=p["bg"], outline=p["line"], width=1)
    cv.text((card[0] + 18, card[1] + 28), "[1]", mono(14 * S), p["accent"], tracking=0.5)
    cv.rounded((card[0] + 60, card[1] + 20, card[2] - 22, card[1] + 26), 3, fill=p["bar"])
    cv.rounded((card[0] + 18, card[1] + 42, card[2] - 18, card[1] + 48), 3, fill=p["bar"])
    cv.rounded((card[0] + 60, card[1] + 42, card[0] + 210, card[1] + 48), 3, fill=p["accent"])
    cv.rounded((card[0] + 18, card[1] + 58, card[0] + 168, card[1] + 64), 3, fill=p["bar"])


def scene_page(cv, box, p):
    """成品页：页眉、正文条、结论块 —— 只画骨架，不写正文。"""
    x0, y0, x1, _ = box
    sheet = (x0 + 52, y0 + 50, x1 - 52, y0 + 196)
    cv.rounded(sheet, 10, fill=p["bg"], outline=p["line"], width=1)
    inner_x0, inner_x1 = x0 + 76, x1 - 76
    cv.rounded((inner_x0, y0 + 66, inner_x0 + 46, y0 + 72), 3, fill=p["accent"])
    cv.rounded((inner_x0, y0 + 82, inner_x0 + 168, y0 + 90), 3, fill=p["bar"])
    for i, w in enumerate((200, 164, 186)):
        y = y0 + 106 + i * 16
        cv.rounded((inner_x0, y, inner_x0 + w, y + 8), 3, fill=p["bar"])
    cv.rounded((inner_x0, y0 + 160, inner_x1, y0 + 184), 6, fill=p["tint"])
    cv.rounded((inner_x0 + 12, y0 + 168, inner_x0 + 120, y0 + 176), 3, fill=p["accent"])


def render_flow(lang, theme):
    """怎么用：三块界面按步骤排开，序号代替箭头，别和"怎么做到"那页撞版式。"""
    p = PALETTES[theme]
    c = FLOW[lang]
    cv = Canvas(EXP_W, EXP_H, bg=p["bg"])
    cv.rect((0.5, 0.5, EXP_W - 0.5, EXP_H - 0.5), outline=p["edge"], width=1)
    story_header(cv, p, lang, c)

    panels = ((96, 336, 516, 696), (590, 336, 1010, 696), (1084, 336, 1504, 696))
    scenes = (scene_library, scene_chat, scene_page)
    kinds = ("window", "window", "browser")
    chip_f = serif(17 * S) if lang == "zh" else mono(14 * S)
    sub_f = serif(18 * S) if lang == "zh" else mono(16 * S)
    for i, (box, scene, kind, texts) in enumerate(
            zip(panels, scenes, kinds, (c["p1"], c["p2"], c["p3"]))):
        shadow(cv, box, 14, (0, 12), 16, p["shadow"])
        cv.rounded(box, 14, fill=p["card"], outline=p["line"], width=1)
        window_chrome(cv, box, p, kind)
        scene(cv, box, p)
        title, sub, tag = texts
        cx = (box[0] + box[2]) / 2
        cv.text((cx, 592), title, serif(24 * S, "SemiBold"), p["ink"], anchor="ms")
        cv.text((cx, 622), sub, sub_f, p["muted"], anchor="ms")
        w = cv.width(tag, chip_f) + 46
        chip(cv, cx - w / 2, 640, tag, chip_f, p)
        numbered(cv, cx, 312, i + 1, p)

    story_footer(cv, p, lang, c["footer"])
    return cv.im.convert("RGB").resize((EXP_W, EXP_H), Image.LANCZOS)


# ---------------------------------------------------------------- 正文图 4：答案形态
def tile_skeleton(cv, x, kind, p):
    """四张页面缩略的骨架：同一块画布里，用不同结构区分模板。"""
    if kind == "instant":                       # 一屏卡
        cv.rounded((x + 54, 388, x + 246, 548), 8, fill=p["bg"], outline=p["line"], width=1)
        cv.rounded((x + 70, 404, x + 180, 412), 3, fill=p["bar"])
        cv.rounded((x + 70, 424, x + 190, 432), 3, fill=p["bar"])
        cv.rounded((x + 70, 440, x + 160, 448), 3, fill=p["bar"])
        cv.rounded((x + 70, 470, x + 230, 502), 6, fill=p["tint"])
        cv.rounded((x + 82, 482, x + 150, 490), 3, fill=p["accent"])
    elif kind == "compare":                     # 表 + 结论
        cv.rounded((x + 40, 392, x + 260, 408), 3, fill=p["tint"])
        for y in (424, 452, 480, 508):
            cv.line((x + 40, y, x + 260, y), p["line"], 1)
        for gx in (x + 40, x + 115, x + 190, x + 260):
            cv.line((gx, 392, gx, 508), p["line"], 1)
        for row, y in enumerate((414, 438, 466, 494)):
            for col, (cx0, cx1) in enumerate(((x + 48, x + 106), (x + 123, x + 181),
                                              (x + 198, x + 252))):
                w = (cx1 - cx0) - 10 - row * 6 - col * 4
                cv.rounded((cx0, y, cx0 + w, y + 6), 3, fill=p["bar"])
        cv.rounded((x + 40, 518, x + 260, 546), 6, fill=p["tint"])
        cv.rounded((x + 52, 528, x + 176, 536), 3, fill=p["accent"])
    elif kind == "numeric":                     # 指标卡 + 数值条
        for i, box in enumerate(((x + 40, x + 104), (x + 118, x + 182), (x + 196, x + 260))):
            cv.rounded((box[0], 388, box[1], 432), 6, fill=p["tint"])
            cv.rounded((box[0] + 8, 416, box[1] - 8, 422), 3,
                       fill=p["accent"] if i == 0 else p["bar"])
        for i, w in enumerate((192, 150, 108, 64)):
            y = 452 + i * 20
            cv.rounded((x + 40, y, x + 232, y + 8), 3, fill=p["tint"])
            cv.rounded((x + 40, y, x + 40 + w, y + 8), 3,
                       fill=p["accent"] if i == 0 else p["bar"])
    else:                                        # timeline
        cv.rounded((x + 40, 410, x + 180, 418), 3, fill=p["bar"])
        cv.rounded((x + 40, 424, x + 140, 432), 3, fill=p["bar"])
        cv.line((x + 40, 470, x + 260, 470), p["line"], 2)
        for i, dx in enumerate((70, 120, 170, 220)):
            cv.d.ellipse(cv.box((x + dx - 5, 465, x + dx + 5, 475)), fill=p["accent"])
            cv.rounded((x + dx - 18, 486, x + dx + 18, 492), 3, fill=p["bar"])


def render_templates(lang, theme):
    """十种模板里挑四种，四种骨架并排；十种 id 全部写在脚注。"""
    p = PALETTES[theme]
    c = TEMPLATES[lang]
    cv = Canvas(EXP_W, EXP_H, bg=p["bg"])
    cv.rect((0.5, 0.5, EXP_W - 0.5, EXP_H - 0.5), outline=p["edge"], width=1)
    story_header(cv, p, lang, c)

    label_f = serif(24 * S, "SemiBold")
    cap_f = serif(18 * S) if lang == "zh" else mono(15 * S)
    for x, (name, cap) in zip((110, 470, 830, 1190), c["tiles"]):
        box = (x, 336, x + 300, 696)
        shadow(cv, box, 14, (0, 12), 16, p["shadow"])
        cv.rounded(box, 14, fill=p["card"], outline=p["line"], width=1)
        window_chrome(cv, box, p)
        tile_skeleton(cv, x, name, p)
        cv.text((x + 150, 592), name, label_f, p["ink"], anchor="ms")
        cv.text((x + 150, 622), cap, cap_f, p["muted"], anchor="ms")

    cv.text((96, 730), TEMPLATE_IDS, mono(14 * S), p["muted"], tracking=0.5)
    return cv.im.convert("RGB").resize((EXP_W, EXP_H), Image.LANCZOS)


# ---------------------------------------------------------------- 正文图 5：细节表（4 页）
def _dash_seg(cv, x0, y0, x1, y1, color, width=1, dash=8, gap=6):
    """虚线段：PIL 没有 dash 支持，用短线段拼。"""
    total = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
    if total <= 0:
        return
    ux, uy = (x1 - x0) / total, (y1 - y0) / total
    t = 0.0
    while t < total:
        e = min(t + dash, total)
        cv.line((x0 + ux * t, y0 + uy * t, x0 + ux * e, y0 + uy * e), color, width)
        t = e + gap


def _dash_rect(cv, box, color, width=1, dash=8, gap=6):
    x0, y0, x1, y1 = box
    _dash_seg(cv, x0, y0, x1, y0, color, width, dash, gap)
    _dash_seg(cv, x1, y0, x1, y1, color, width, dash, gap)
    _dash_seg(cv, x1, y1, x0, y1, color, width, dash, gap)
    _dash_seg(cv, x0, y1, x0, y0, color, width, dash, gap)


def _tag(cv, x, y, text, p, lang="en"):
    """实心小标签（accent 底、反白字），用于"新增"这类状态。"""
    f = serif(13 * S, "Medium") if lang == "zh" else mono(13 * S)
    w = cv.width(text, f) + 22
    cv.rounded((x, y - 2, x + w, y + 26), 6, fill=p["accent"])
    cv.text((x + 11, y + 17), text, f, p["bg"], tracking=0 if lang == "zh" else 0.6)


def _paper_card(cv, box, p, lang, solid, lost):
    """一张论文缩略：标题与摘要是"会记住的"，下方三行细节是"会丢的"。"""
    x0, y0, x1, y1 = box
    shadow(cv, box, 14, (0, 12), 16, p["shadow"])
    cv.rounded(box, 14, fill=p["card"], outline=p["line"], width=1)
    pad = 32
    cv.rounded((x0 + pad, y0 + 34, x0 + pad + 320, y0 + 46), 3, fill=p["ink"])
    for i, w in enumerate((470, 430, 452)):
        y = y0 + 72 + i * 22
        cv.rounded((x0 + pad, y, x0 + pad + w, y + 9), 3, fill=p["bar"])
    cv.line((x0 + pad, y0 + 152, x1 - pad, y0 + 152), p["line"], 1)
    for i, label in enumerate(lost):
        y = y0 + 176 + i * 38
        f = serif(15 * S) if lang == "zh" else mono(13 * S)
        cv.text((x0 + pad, y + 11), label, f, p["muted"],
                tracking=0 if lang == "zh" else 0.6)
        bx = x0 + pad + 130
        if solid:
            cv.rounded((bx, y, bx + 296, y + 12), 3, fill=p["bar"])
        else:
            _dash_rect(cv, (bx, y, bx + 296, y + 12), p["muted"], 1, 8, 6)


def _ledger_cell(cv, x0, x1, y0, y1, cell, p, lang, empty_label):
    """一个单元格：有值画值 + 页码；冲突加 ≠ 角标；没有值画"未报告"。"""
    if cell is None:
        cv.rect((x0 + 4, y0 + 4, x1 - 4, y1 - 4), fill=p["bg"])
        f = serif(15 * S) if lang == "zh" else mono(13 * S)
        cv.text(((x0 + x1) / 2, y0 + 40), empty_label, f, p["muted"], anchor="ms")
        return
    text, page, flag = cell
    if flag == "conf":
        cv.rounded((x0 + 6, y0 + 6, x1 - 6, y1 - 6), 8, fill=p["tint"])
        bx, by = x1 - 36, y0 + 23
        cv.d.ellipse(cv.box((bx - 14, by - 14, bx + 14, by + 14)), fill=p["accent"])
        cv.text((bx, by), "≠", mono(16 * S), p["bg"], anchor="mm")
    value_f = serif(19 * S)
    cv.text((x0 + 20, y0 + 42), text, value_f, p["ink"])
    cv.text((x0 + 20 + cv.width(text, value_f) + 12, y0 + 42), page,
            mono(13 * S), p["muted"], tracking=0.3)


def _ledger_table(cv, p, c, lang, highlight_new=False):
    """表格本体：页 3（并置）与页 4（增量）共用；页 4 把最后一行标成新增。"""
    top, row_h = 336, 74
    bottom = top + 50 + 4 * row_h
    box = (96, top, 1504, bottom)
    shadow(cv, box, 16, (0, 14), 18, p["shadow"])
    cv.rounded(box, 16, fill=p["card"], outline=p["line"], width=1)

    meta = c.get("meta")
    if meta:
        f = serif(15 * S) if lang == "zh" else mono(13 * S)
        tr = 1.0 if lang == "zh" else 0.8
        w = cv.width(meta, f) + tr * max(0, len(meta) - 1)
        cv.text((1504 - w, top - 20), meta, f, p["muted"], tracking=tr)

    xs = (96, 316, 616, 896, 1176, 1504)
    for x in xs[1:-1]:
        cv.line((x, top + 2, x, bottom - 2), p["line"], 1)

    head_f = serif(15 * S) if lang == "zh" else mono(13 * S)
    for i, label in enumerate(c["cols"]):
        cv.text((xs[i] + 20, top + 36), label, head_f, p["muted"],
                tracking=2 if lang == "zh" else 1.2)
    cv.line((98, top + 50, 1502, top + 50), p["line"], 1)

    for r, (paper, cells) in enumerate(c["rows"]):
        y0 = top + 50 + r * row_h
        y1 = y0 + row_h
        new = highlight_new and r == len(c["rows"]) - 1
        if new:
            cv.rect((99, y0 + 1, 1501, y1 - 1), fill=p["tint"])
            cv.rect((99, y0 + 1, 103, y1 - 1), fill=p["accent"])
            for x in xs[1:-1]:
                cv.line((x, y0, x, y1), p["line"], 1)
        if r:
            cv.line((98, y0, 1502, y0), p["line"], 1)
        pf = serif(19 * S, "Medium") if lang == "zh" else mono(16 * S)
        cv.text((xs[0] + 20, y0 + 44), paper, pf, p["ink"],
                tracking=0 if lang == "zh" else 0.5)
        if new:
            _tag(cv, xs[0] + 20 + cv.width(paper, pf) + 14, y0 + 18, c["new"], p, lang)
        for i, cell in enumerate(cells):
            _ledger_cell(cv, xs[i + 1], xs[i + 2], y0, y1, cell, p, lang, c["empty"])


def render_ledger_forget(lang, theme):
    """页 1：读过不等于记住——第 0 天细节都在，第 7 天只剩主线。"""
    p = PALETTES[theme]
    c = LEDGER_P1[lang]
    cv = Canvas(EXP_W, EXP_H, bg=p["bg"])
    cv.rect((0.5, 0.5, EXP_W - 0.5, EXP_H - 0.5), outline=p["edge"], width=1)
    story_header(cv, p, lang, c)

    cap_f = serif(17 * S) if lang == "zh" else mono(14 * S)
    cv.text((96, 320), c["left_caption"], cap_f, p["muted"],
            tracking=0 if lang == "zh" else 0.8)
    cv.text((884, 320), c["right_caption"], cap_f, p["muted"],
            tracking=0 if lang == "zh" else 0.8)

    _paper_card(cv, (96, 336, 716, 660), p, lang, True, c["lost"])
    _paper_card(cv, (884, 336, 1504, 660), p, lang, False, c["lost"])
    arrow(cv, 728, 872, 498, p)
    cv.text((800, 482), c["arrow_label"],
            serif(14 * S) if lang == "zh" else mono(12 * S), p["muted"], anchor="ms")

    story_footer(cv, p, lang, c["footer"])
    return cv.im.convert("RGB").resize((EXP_W, EXP_H), Image.LANCZOS)


def _quote_run(cv, x, y, quote, hi, p, size=22):
    """画一句原文，命中的片段垫 tint；返回整句宽度。左右两卡共用，保证逐字对齐。"""
    f = serif(size * S)
    pre = quote.split(hi)[0]
    w_pre, w_hi = cv.width(pre, f), cv.width(hi, f)
    cv.rounded((x + w_pre - 4, y - 19, x + w_pre + w_hi + 4, y + 5), 4, fill=p["tint"])
    cv.text((x, y), quote, f, p["ink"])
    return cv.width(quote, f)


def render_ledger_evidence(lang, theme):
    """页 2：左边一条检索结果，右边它在论文里的原位段落。"""
    p = PALETTES[theme]
    c = LEDGER_P2[lang]
    cv = Canvas(EXP_W, EXP_H, bg=p["bg"])
    cv.rect((0.5, 0.5, EXP_W - 0.5, EXP_H - 0.5), outline=p["edge"], width=1)
    story_header(cv, p, lang, c)

    left, right = (96, 336, 760, 660), (864, 336, 1504, 660)
    for box in (left, right):
        shadow(cv, box, 14, (0, 12), 16, p["shadow"])
        cv.rounded(box, 14, fill=p["card"], outline=p["line"], width=1)

    lab_f = serif(16 * S) if lang == "zh" else mono(13 * S)
    cv.text((132, 388), c["result_label"], lab_f, p["muted"],
            tracking=0 if lang == "zh" else 1.2)
    # 结果卡片：标题用实心条代替（不放真实论文标题），下面是元数据与命中句
    cv.rounded((132, 412, 572, 424), 3, fill=p["ink"])
    cv.rounded((132, 436, 452, 446), 3, fill=p["ink"])
    cv.text((132, 486), c["meta"], mono(14 * S), p["muted"], tracking=0.5)
    w = _quote_run(cv, 132, 528, c["quote"], c["quote_hi"], p)
    cv.text((132 + w + 12, 528), "[1]", serif(15 * S, "SemiBold"), p["accent"])
    chip(cv, 132, 574, c["result_chip"],
         serif(17 * S) if lang == "zh" else mono(14 * S), p)

    # 论文原位：页码 + 前后段落条，命中句保持原位
    cv.text((900, 388), c["pdf_label"], lab_f, p["muted"],
            tracking=0 if lang == "zh" else 1.2)
    cv.text((1468, 388), c["page"], mono(12 * S), p["muted"], anchor="rs")
    for y, bw in ((436, 420), (458, 470)):
        cv.rounded((900, y, 900 + bw, y + 10), 3, fill=p["bar"])
    _quote_run(cv, 900, 528, c["quote"], c["quote_hi"], p)
    for y, bw in ((556, 440), (578, 360)):
        cv.rounded((900, y, 900 + bw, y + 10), 3, fill=p["bar"])

    _dash_seg(cv, 766, 528, 858, 528, p["muted"], 1, 7, 6)
    cv.text((812, 512), c["connector"],
            serif(14 * S) if lang == "zh" else mono(12 * S), p["muted"], anchor="ms")

    story_footer(cv, p, lang, c["footer"])
    return cv.im.convert("RGB").resize((EXP_W, EXP_H), Image.LANCZOS)


def _badge(cv, x, y, text, p, lang, solid=True):
    """状态徽标：实心=库内，描边=未入库。宽度随文案，右对齐时先算宽。"""
    f = serif(14 * S) if lang == "zh" else mono(11 * S)
    w = cv.width(text, f) + (46 if solid else 40)
    if solid:
        cv.rounded((x, y, x + w, y + 30), 9, fill=p["tint"])
        cv.d.ellipse(cv.box((x + 14, y + 12, x + 20, y + 18)), fill=p["accent"])
        cv.text((x + 30, y + 21), text, f, p["accent"],
                tracking=0 if lang == "zh" else 0.5)
    else:
        cv.rounded((x, y, x + w, y + 30), 9, outline=p["line"], width=1)
        cv.text((x + 14, y + 21), text, f, p["muted"],
                tracking=0 if lang == "zh" else 0.5)
    return w


def render_ledger_chain(lang, theme):
    """页 3：引文链——正文里的 [n] 解析到参考文献，库内条目直接标出来。"""
    p = PALETTES[theme]
    c = LEDGER_P3[lang]
    cv = Canvas(EXP_W, EXP_H, bg=p["bg"])
    cv.rect((0.5, 0.5, EXP_W - 0.5, EXP_H - 0.5), outline=p["edge"], width=1)
    story_header(cv, p, lang, c)

    left, right = (96, 336, 700, 660), (764, 336, 1504, 660)
    for box in (left, right):
        shadow(cv, box, 14, (0, 12), 16, p["shadow"])
        cv.rounded(box, 14, fill=p["card"], outline=p["line"], width=1)

    lab_f = serif(16 * S) if lang == "zh" else mono(13 * S)
    cv.text((132, 388), c["passage_label"], lab_f, p["muted"],
            tracking=0 if lang == "zh" else 1.2)
    cv.text((132, 440), "“", serif(36 * S), p["muted"])
    lf = serif(24 * S)
    cv.text((170, 492), c["line1"], lf, p["ink"])
    pre = c["line2"].split(c["line2_hi"])[0]
    wpre = cv.width(pre, lf)
    cv.text((170, 534), pre, lf, p["ink"])
    cv.text((170 + wpre, 534), c["line2_hi"], serif(24 * S, "SemiBold"), p["accent"])
    cv.text((170, 590), c["note"],
            serif(14 * S) if lang == "zh" else mono(12 * S), p["muted"],
            tracking=0 if lang == "zh" else 0.4)

    head_f = serif(16 * S) if lang == "zh" else mono(13 * S)
    cv.text((800, 388), c["citation_label"], head_f, p["muted"],
            tracking=0 if lang == "zh" else 1.2)
    for i, (ref, meta, status, extra) in enumerate(c["rows"]):
        y = 424 + i * 96
        cv.text((800, y + 26), ref, mono(12 * S), p["accent"], tracking=0.5)
        cv.rounded((866, y + 16, 1250, y + 24), 3, fill=p["bar"])
        cv.text((866, y + 48), meta, mono(12 * S), p["muted"], tracking=0.4)
        f = serif(14 * S) if lang == "zh" else mono(11 * S)
        bw = cv.width(status, f) + (46 if i == 0 else 40)
        _badge(cv, 1468 - bw, y + 6, status, p, lang, solid=(i == 0))
        if extra:
            cv.text((1468, y + 54), extra,
                    serif(13 * S) if lang == "zh" else mono(11 * S), p["accent"], anchor="rs")
    cv.text((800, 632), c["collapsed"],
            serif(14 * S) if lang == "zh" else mono(12 * S), p["muted"],
            tracking=0 if lang == "zh" else 0.3)

    story_footer(cv, p, lang, c["footer"])
    return cv.im.convert("RGB").resize((EXP_W, EXP_H), Image.LANCZOS)

def render_ledger_grid(lang, theme):
    """页 3：并置——四篇一张表，冲突与空白都在表上。"""
    p = PALETTES[theme]
    c = LEDGER_P4[lang]
    cv = Canvas(EXP_W, EXP_H, bg=p["bg"])
    cv.rect((0.5, 0.5, EXP_W - 0.5, EXP_H - 0.5), outline=p["edge"], width=1)
    story_header(cv, p, lang, c)
    _ledger_table(cv, p, c, lang)
    story_footer(cv, p, lang, c["footer"])
    return cv.im.convert("RGB").resize((EXP_W, EXP_H), Image.LANCZOS)


def render_ledger_grow(lang, theme):
    """页 4：增量——新论文只补它碰到的格子，冲突照报。"""
    p = PALETTES[theme]
    c = LEDGER_P5[lang]
    cv = Canvas(EXP_W, EXP_H, bg=p["bg"])
    cv.rect((0.5, 0.5, EXP_W - 0.5, EXP_H - 0.5), outline=p["edge"], width=1)
    story_header(cv, p, lang, c)
    _ledger_table(cv, p, c, lang, highlight_new=True)
    story_footer(cv, p, lang, c["footer"])
    return cv.im.convert("RGB").resize((EXP_W, EXP_H), Image.LANCZOS)

# ---------------------------------------------------------------- 图标
ICON_BLUE = "#1F5FA8"
ICON_PAPER = "#FBF9F4"


def render_icon(size=512, pad=0.094, glyph=0.52):
    """透明底圆角方块 + 纸色 K。按尺寸单独渲染（小尺寸可收紧内边距、放大字形）。"""
    n = size * 4
    im = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    inset = n * pad
    d.rounded_rectangle((inset, inset, n - inset, n - inset),
                        radius=n * 0.22, fill=ICON_BLUE,
                        outline=(251, 249, 244, 28), width=max(1, int(round(n * 0.006))))
    draw_k(d, k_ink_box((inset, inset, n - inset, n - inset), glyph), ICON_PAPER)
    return im.resize((size, size), Image.LANCZOS)


def write_icon_svg(path, pad=48.0, glyph=0.52):
    """K 的轮廓从 Noto Serif SC Bold 抽出（OFL 允许嵌入），PNG 与 SVG 是同一字形。"""
    try:
        from fontTools.pens.boundsPen import BoundsPen
        from fontTools.pens.svgPathPen import SVGPathPen
        from fontTools.ttLib import TTFont
        from fontTools.varLib.instancer import instantiateVariableFont
    except ImportError:
        print("跳过 icon.svg：需要 fontTools（pip install fonttools）")
        return

    font = TTFont(str(SERIF))
    try:
        font = instantiateVariableFont(font, {"wght": 700})
    except Exception:                                        # noqa: BLE001
        pass
    gname = font.getBestCmap().get(ord(K_LETTER))
    if not gname:
        print("跳过 icon.svg：字体里没有 %s 字形" % K_LETTER)
        return
    gs = font.getGlyphSet()
    glyph_obj = gs[gname]

    bp = BoundsPen(gs)
    glyph_obj.draw(bp)
    if not bp.bounds:
        print("跳过 icon.svg：%s 轮廓为空" % K_LETTER)
        return
    gx0, gy0, gx1, gy1 = bp.bounds

    tile = 512 - 2 * pad
    ink_h = tile * glyph
    s = ink_h / (gy1 - gy0)
    tx = 256 - s * (gx0 + gx1) / 2
    ty = 256 + s * (gy0 + gy1) / 2

    sp = SVGPathPen(gs)
    glyph_obj.draw(sp)
    d = sp.getCommands()

    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" '
        'role="img" aria-label="kb-rag">\n'
        "  <title>kb-rag</title>\n"
        '  <rect x="48" y="48" width="416" height="416" rx="112" fill="%s" '
        'stroke="%s" stroke-opacity="0.12" stroke-width="3"/>\n'
        '  <path transform="translate(%.2f %.2f) scale(%.5f %.5f)" d="%s" fill="%s"/>\n'
        "</svg>\n" % (ICON_BLUE, ICON_PAPER, tx, ty, s, -s, d, ICON_PAPER)
    )
    path.write_text(svg, encoding="utf-8", newline="\n")


# ---------------------------------------------------------------- 产物
def build():
    OUT.mkdir(parents=True, exist_ok=True)
    print("衬线：%s · 等宽：%s" % (SERIF.name, MONO.name))
    for lang in ("en", "zh"):
        for theme in ("light", "dark"):
            im = render_cover(lang, theme)
            im.save(OUT / ("cover-%s-%s.png" % (lang, theme)), optimize=True)
            render_explainer(lang, theme).save(
                OUT / ("explainer-%s-%s.png" % (lang, theme)), optimize=True)
            render_how(lang, theme).save(
                OUT / ("explainer-how-%s-%s.png" % (lang, theme)), optimize=True)
            render_flow(lang, theme).save(
                OUT / ("explainer-flow-%s-%s.png" % (lang, theme)), optimize=True)
            render_templates(lang, theme).save(
                OUT / ("explainer-templates-%s-%s.png" % (lang, theme)), optimize=True)
            render_ledger_forget(lang, theme).save(
                OUT / ("explainer-ledger-1-%s-%s.png" % (lang, theme)), optimize=True)
            render_ledger_evidence(lang, theme).save(
                OUT / ("explainer-ledger-2-%s-%s.png" % (lang, theme)), optimize=True)
            render_ledger_chain(lang, theme).save(
                OUT / ("explainer-ledger-3-%s-%s.png" % (lang, theme)), optimize=True)
            render_ledger_grid(lang, theme).save(
                OUT / ("explainer-ledger-4-%s-%s.png" % (lang, theme)), optimize=True)
            render_ledger_grow(lang, theme).save(
                OUT / ("explainer-ledger-5-%s-%s.png" % (lang, theme)), optimize=True)
    render_icon(512, 0.094, 0.52).save(OUT / "icon-512.png", optimize=True)
    render_icon(180, 0.094, 0.52).save(OUT / "icon-180.png", optimize=True)
    render_icon(32, 0.050, 0.58).save(OUT / "icon-32.png", optimize=True)
    render_icon(256, 0.050, 0.56).save(OUT / "favicon.ico",
                                       sizes=[(16, 16), (32, 32), (48, 48)])
    write_icon_svg(OUT / "icon.svg")
    return check()


def check():
    """校验产物尺寸与体积；--check 只读不写。"""
    bad = []
    files = sorted(OUT.glob("cover-*.png")) + sorted(OUT.glob("explainer-*.png")) + \
        sorted(OUT.glob("icon-*.png")) + [OUT / "favicon.ico", OUT / "icon.svg"]
    for p in files:
        if not p.exists():
            bad.append("缺文件：%s" % p.name)
            continue
        size = p.stat().st_size
        if p.suffix == ".png":
            with Image.open(p) as im:
                wh = im.size
            print("%-24s %-11s %6.1f KB" % (p.name, "%dx%d" % wh, size / 1024.0))
            if p.name.startswith("cover-") and wh != (W, H):
                bad.append("%s 尺寸 %s，应为 %dx%d" % (p.name, wh, W, H))
            if p.name.startswith("explainer-") and wh != (EXP_W, EXP_H):
                bad.append("%s 尺寸 %s，应为 %dx%d" % (p.name, wh, EXP_W, EXP_H))
            if (p.name.startswith("cover-") or p.name.startswith("explainer-")) \
                    and size > 1024 * 1024:
                bad.append("%s 超过 1 MB（GitHub 图片上限）" % p.name)
        else:
            print("%-24s %-11s %6.1f KB" % (p.name, p.suffix, size / 1024.0))
    for e in bad:
        print("✗ " + e, file=sys.stderr)
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser(description="生成 kb-rag 封面与图标")
    ap.add_argument("--check", action="store_true", help="只校验 docs/assets/brand/ 里的产物")
    args = ap.parse_args()
    code = check() if args.check else build()
    if not args.check:
        print("\n输出目录：%s" % OUT)
    return code


if __name__ == "__main__":
    sys.exit(main())
