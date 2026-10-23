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
        title=["直接问你的 PDF。", "出处再也不会丢。"],
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
        title=["From PDFs to passages,", "on your machine."],
        sub="Index once, then every question runs hybrid keyword + vector search and a local reranker.",
        s1=("PDF · Zotero", "your library"),
        s2=("chunks + vectors", "one SQLite file"),
        s3=("hybrid + rerank", "keyword + vector"),
        s4=("the passage", "section · page · DOI"),
        footer="chunks · local embeddings · hybrid + rerank · one SQLite file",
    ),
    "zh": dict(
        eyebrow="怎么做到",
        title=["从 PDF 到原文段落，", "全在本机。"],
        sub="索引只做一次；之后每次提问都走关键词 + 向量混合检索，再本地精排。",
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
        title=["Add your PDFs. Ask.", "Share the answer."],
        sub="Index once; every question returns a passage you can point at, and any answer can become a shareable page.",
        p1=("your library", "PDF · Zotero", "kb_ingest"),
        p2=("ask in the chat", "one question → passages", "kb_search · kb_rag"),
        p3=("share the page", "self-contained HTML", "answer_file"),
        footer="ingest once · ask · share — nothing leaves the machine",
    ),
    "zh": dict(
        eyebrow="怎么用",
        title=["把 PDF 放进来，提问，", "把答案发出去。"],
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
        title=["十种模板，", "一个引擎。"],
        sub="章节、表格、数值条、曲线、指标卡、时间线都由引擎排版，不调用模型。十种里的四种：",
        tiles=(("instant", "一屏直答"), ("compare", "对比表 + 结论"),
               ("numeric", "指标卡 + 数值条"), ("timeline", "时间线")),
    ),
}
TEMPLATE_IDS = ("instant · review · method · compare · numeric · timeline · "
                "concept · dispute · derivation · qa")

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
    cv.text((96, 276), c["sub"], serif(24 * S), p["ink2"])

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
    cv.text((96, 276), c["sub"], serif(24 * S), p["ink2"])


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
