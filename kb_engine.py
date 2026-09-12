#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kb_engine.py — lightweight local RAG engine (v2, keyword BM25 + vector hybrid).

Design goals (from kb_system_design.md):
  - structured chunking: papers split by section (Abstract x1.5 / Methods x1.2),
    generic docs split by markdown heading or paragraph fallback;
  - incremental ingest: sha256 + size, unchanged files are skipped;
  - hybrid retrieval: in-memory BM25 (CJK-friendly) + local bge-small embeddings
    (FAISS IndexFlatIP) fused with RRF, metadata SQL pre-filter;
  - token economy: snippet-only results with exact provenance, query cache.

Protocol (DSH plugin <-> engine):
  python kb_engine.py <command>
  request JSON on stdin (UTF-8), response JSON on stdout (ensure_ascii).

Commands:
  ingest  {paths:[...], kb_root?:str, force?:bool}
  search  {query:str, top_k?:int, snippet?:int, mode?:'keyword'|'vector'|'hybrid',
           filters?:{authors?,title?,journal?,kind?,year?,section?}, kb_root?:str, cache?:bool}
  rag     {query:str, top_k?:int, filters?:{...}, kb_root?:str}
  stats   {kb_root?:str}
"""

import array
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

VERSION = "3.2.0"
SUPPORTED_EXTS = {".pdf", ".txt", ".md", ".markdown", ".docx"}

# 解析器版本：**只在改动会写进库的解析逻辑时 +1**（分块、元数据/标识符抽取、引文切分）。
# 每条 docs 记录写入 indexed_with；kb_stats 报告 stale_docs —— 因为增量入库按 sha256
# 跳过未变文件，引擎的解析改进不会自动作用于老库（实测：一处抽取改动漏了 49 篇的 DOI，
# 直到一次全量重灌才暴露）。注意判定只看 rev，不看引擎 VERSION：否则每次发版都会把
# 整个库标成陈旧，提示就变成噪音。
PARSER_REV = 5
PARSER_TOKEN = "%s/rev%d" % (VERSION, PARSER_REV)
# 哪些 rev 改动了**分块/向量**（而不只是元数据）。升级提示据此决定给用户哪个建议：
# `metadata_only`（秒级刷元数据）对分块类改动**无效** —— 给一个无效选项比不给更糟。
# rev 5：References 判定护栏 + 整篇不可检索兜底（会改变分块与向量）。
CHUNK_AFFECTING_REVS = {4, 5}

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
  id INTEGER PRIMARY KEY,
  path TEXT NOT NULL UNIQUE,
  title TEXT, authors TEXT, year INTEGER, journal TEXT, doi TEXT,
  kind TEXT, sha256 TEXT, size INTEGER, mtime REAL,
  chunk_count INTEGER, indexed_at REAL, zotero_key TEXT, indexed_with TEXT
);
CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY,
  doc_id INTEGER NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
  section TEXT NOT NULL,
  weight REAL NOT NULL DEFAULT 1.0,
  seq INTEGER NOT NULL,
  text TEXT NOT NULL,
  para_start INTEGER,
  para_end INTEGER,
  page_start INTEGER,
  page_end INTEGER
);
CREATE TABLE IF NOT EXISTS vecs (
  chunk_id INTEGER PRIMARY KEY,
  vec BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS cache (
  key TEXT PRIMARY KEY,
  payload TEXT NOT NULL,
  created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
"""

# 库结构（schema）版本：与引擎代码版本 VERSION 独立。
# v1: docs.zotero_key；v2: chunks.para_start/para_end（段落定位，隐式元数据）；
# v3: chunks.page_start/page_end（PDF 物理页码，证据锚点 → Zotero ?page=N 跳页）；
# v4: docs.indexed_with（入库时的解析器版本，用于检测"老数据是用旧解析器写的"）。
# PRAGMA user_version 记录库结构版本；破坏性变更需新增迁移块（见 docs/MIGRATION.md §4）。
SCHEMA_VERSION = 4

# 最近一次连接/迁移说明（cmd_stats 等据此给出迁移与健康提示；每次 connect 更新）
_LAST_CONNECT = {"created": False, "from_version": None, "to_version": None,
                 "actions": [], "backfilled_keys": 0, "logged": False}

# ---------------------------------------------------------------- sectioning

# (pattern, canonical section, weight) — first match wins.
SECTION_MAP = [
    (re.compile(r"abstract", re.I), "Abstract", 1.5),
    (re.compile(r"摘要"), "Abstract", 1.5),
    (re.compile(r"introduction|intro\b|background|related\s+work", re.I), "Introduction", 1.0),
    (re.compile(r"引言|背景|概述"), "Introduction", 1.0),
    (re.compile(r"methods?|materials?\s*(?:and|&)?\s*methods?|methodology|experiments?", re.I), "Methods", 1.2),
    (re.compile(r"方法|实验"), "Methods", 1.2),
    (re.compile(r"results?", re.I), "Results", 1.0),
    (re.compile(r"结果"), "Results", 1.0),
    (re.compile(r"discussions?", re.I), "Discussion", 1.0),
    (re.compile(r"讨论"), "Discussion", 1.0),
    (re.compile(r"conclusions?|summary", re.I), "Conclusion", 1.0),
    (re.compile(r"结论|总结|小结"), "Conclusion", 1.0),
    (re.compile(r"references|bibliography", re.I), "References", 0.0),
    (re.compile(r"参考文献"), "References", 0.0),
    (re.compile(r"acknowledg", re.I), "Acknowledgements", 0.0),
    (re.compile(r"致谢|附录|appendix", re.I), "Appendix", 0.0),
]

NUM_PREFIX = r"(?:\d+(?:\.\d+)*[\.\)、]?|[一二三四五六七八九十百]+[、．.])"

CAPTION_RE = re.compile(
    r"^\s*(fig(?:ure)?\.?\s*\d+|table\.?\s*\d+|scheme\.?\s*\d+|图\s*\d+|表\s*\d+)", re.I)

# ---------------------------------------------------------------- helpers


def clean(text):
    text = re.sub(r"\s+", " ", text)
    return text.replace("\x00", "").strip()


def match_section_prefix(line, allow_long=False):
    """Return (section, weight, rest_after_heading) if `line` starts with a heading.
    allow_long=True consumes the heading phrase even when body text follows inline
    (Science-style papers embed headings mid-paragraph)."""
    s = line.strip()
    if not s:
        return None
    if s.startswith("#"):
        s = s.lstrip("#").strip()
        for pat, name, weight in SECTION_MAP:
            m = pat.match(s)
            if m:
                return name, weight, s[m.end():].strip()
        return s[:40], 1.0, ""
    s2 = re.sub(r"^\s*" + NUM_PREFIX + r"\s*", "", s)
    for pat, name, weight in SECTION_MAP:
        m = pat.match(s2)
        if not m:
            continue
        if allow_long or len(s2) <= 80:
            return name, weight, s2[m.end():].strip()
    return None


INLINE_HEAD_RE = re.compile(
    r"(?<=[.!?])\s+(?=(?:Abstract|Introduction|Results|"
    r"Discussion(?:\s+and\s+outlook)?|Conclusions?|"
    r"Materials?\s+and\s+methods?|Methods|Outlook|Summary)\b)")


def split_inline_headings(p):
    """Split a paragraph at capitalized heading phrases embedded mid-paragraph."""
    return [s.strip() for s in INLINE_HEAD_RE.split(p)]


def split_long(text, limit=1000):
    """Split an overlong chunk at sentence boundaries."""
    parts, buf = [], ""
    for m in re.split(r"(?<=[。；.!?])\s+", text):
        if len(buf) + len(m) + 1 > limit and buf:
            parts.append(buf)
            buf = m
        else:
            buf = (buf + " " + m).strip()
    if buf:
        parts.append(buf)
    return parts


def split_refs(text, limit=1200):
    """按行边界切分 References 长块。

    引文条目解析（_parse_references 的 'N.' 行首锚点）依赖换行结构，
    不能用 split_long（句边界拼接会把 '2.'、'3.' 挤到行中间压平锚点）。
    超长单行（无换行的极端排版）退化为 split_long。"""
    if "\n" not in text:
        return split_long(text, limit=limit)
    lines = text.split("\n")
    parts, buf, n = [], [], 0
    for line in lines:
        if buf and n + len(line) + 1 > limit:
            parts.append("\n".join(buf))
            buf, n = [], 0
        buf.append(line)
        n += len(line) + 1
    if buf:
        parts.append("\n".join(buf))
    return [p for p in parts if p.strip()]


def _promote_abstract(sectioned):
    """Science-style papers often lack a literal 'Abstract' heading: promote the
    first long prose paragraph of a BOUNDED front-matter block to Abstract x1.5."""
    for i, (sec, w, paras) in enumerate(sectioned):
        if sec != "Front matter" or len(paras) < 2:
            continue
        if not any(s2 != "Front matter" for s2, _, _ in sectioned[i + 1:]):
            continue  # unbounded: the whole doc fell into front matter
        for j, (pno, p) in enumerate(paras):
            if j == 0:
                continue
            if 400 <= len(p) <= 3000:
                out = list(sectioned[:i])
                if j > 0:
                    out.append((sec, w, paras[:j]))
                out.append(("Abstract", 1.5, paras[j:]))
                out.extend(sectioned[i + 1:])
                return out
        break  # only the first front-matter block is the paper header
    return sectioned


_REF_HEAD_RE = re.compile(r"(?m)^\s*(references|bibliography|参考文献|引用文献)\b", re.I)
# 参考文献条目风格：'1. Author' / '1 Author' / '[1] Author'（Wiley） / '1Author'（紧贴式）
_REF_ENTRY_BRACKET_RE = re.compile(r"(?m)^\s*\[\s*(\d{1,3})\s*\]\s+(?=\S)")
# 引文链条目行（stage-3 链式检测用）：'[N] ...' / 'N.\t...' / 'N.' 独占行 / 'N. Author...'
_REF_CHAIN_ENTRY_RE = re.compile(r"^\s*(?:\[\s*(\d{1,3})\s*\]|(\d{1,3})\.)(?:\s|\t|$)")
# 引文链尾部的"停止行"：链末条内容到这些行截止（Acknowledgements / © / 图注 / Methods 等）
_REF_STOP_RE = re.compile(
    r"^(?:©|Letter\b|RESEARCH\b|ARTICLE\b|Article\b|Extended\s+Data\b|"
    r"Fig(?:ure)?\.?\s*\d|Table\.?\s*\d|Acknowledg\w*|Methods?\b|Materials\s+and\s+methods\b|"
    r"Data\s+availability\b|Author\s+contribution|Correspondence\b|Supplementary\b|"
    r"Online\s+Content\b|[Rr]eceived\b|References\b|Bibliography\b|Appendix\b|"
    r"Abstract\b|Introduction\b|Results?\b|Discussions?\b|Conclusions?\b|"
    r"致谢|参考文献|引用文献|方法|实验|结果|讨论|结论|附录|图\s*\d|表\s*\d)")


def _ref_entry_count(text):
    n = 0
    for pat in (_REF_ENTRY_RE, _REF_ENTRY_TIGHT_RE, _REF_ENTRY_BRACKET_RE):
        n += len(list(pat.finditer(text or "")))
    return n


# ---------------------------------------------------------------- 条目"像文献"的证据分
#
# 为什么需要它：判断"这段是不是参考文献"以前只看"像不像条目编号"（行首数字/方括号数字），
# 于是作者单位行（'1,2,3,*'）、正文编号列表、末页图注数字都能触发，触发后整篇按 References
# 处理（weight 0）→ 文档从检索里消失，而增量入库不会自愈。这里改成看**每条条目里的文献特征**：
# DOI / 卷-页 / 期刊缩写是强信号，单纯一个年份只是弱信号（图注、表格里也有年份）。

_REF_DOI_RE = re.compile(r"10\.\d{4,9}/")
_REF_VOLPAGE_RE = re.compile(r"\b\d{1,4}\s*,\s*\d{1,5}\b")
_REF_YEAR_PAREN_RE = re.compile(r"\((?:18|19|20)\d{2}[a-z]?\)")
_REF_YEAR_TOKEN_RE = re.compile(r"\b(?:18|19|20)\d{2}[a-z]?\b")
_REF_ETAL_RE = re.compile(r"\bet\s+al\.?\b")
_REF_JOURNAL_RE = re.compile(
    r"\b(?:Nature|Science|Phys\.|Phys\s+Rev|Appl\.|J\.|Chem\.|Rev\.|Adv\.|Nano\s+Lett|"
    r"Ferroelectrics|Carbon|ACS\s+Nano|PRB|PRL)\b")
_REF_SIGNAL_RE = re.compile(
    r"\b(?:18|19|20)\d{2}[a-z]?\b|\bet\s+al\.?\b|&\s+[A-Z]|\bdoi:|\bvol\.|\bpp\.|"
    r"\bProc\.|\bJ\.\s*[A-Z]|\bPhys\.|\bChem\.|\bNature\b|\bScience\b|\bAppl\.|\bRev\.")

# 参考文献条目的**版式特征**（由维护者给出的判别依据，比"数字+年份"稳得多）：
#   ① 条目以编号开头，编号在文档内**递增**（1 / 1. / [1]）
#   ② 整块通常落在文末
#   ③ **人名一定在首位**，后面依次是期刊缩写、年份、卷页等（其余位置可以变）
# 第 ③ 条是关键：正文里的编号列表（"1. Introduction…"、"3. Data analysis…"）编号后面
# 接的是小写词或抽象名词，几乎不可能是"姓, 首字母"或"首字母. 姓"。
_REF_NAME_FIRST_RE = re.compile(
    r"^(?:"
    r"(?:[A-Z][A-Za-z\u00c0-\u024f'’\-]{1,}(?:\s+(?:and|&)\s+[A-Z][A-Za-z'’\-]{1,})?)"   # Smith / Smith and Jones
    r"(?:\s*,\s*(?:[A-Z]\.\s*){0,3})"                                                    # , J. / , J. A.
    r"|(?:[A-Z]\.\s*){1,3}[A-Z][A-Za-z\u00c0-\u024f'’\-]{1,}"                           # J. Smith / J. A. Smith
    r"|(?:[A-Z][A-Za-z\u00c0-\u024f'’\-]{1,}\s+){1,2}[A-Z][A-Za-z'’\-]{1,}"             # Chinese-style pinyin "Wang Lei"
    r"|[A-Z][A-Za-z\u00c0-\u024f'’\-]{1,}\s+(?:et\s+al\.?)"                              # Smith et al.
    r")")


def _ref_entry_like(seg):
    """一条条目是否像参考文献 —— **主判据**（维护者给出的版式特征）：

    ① 编号在条目首位且在全文中递增（1 / 1. / [1]）
    ② 整块通常落在文末
    ③ **人名一定在首位**，其后依次是期刊缩写、年份、卷页等（其余位置可变）

    人名在首位是最强判别信号：正文编号列表（"1. Introduction…"）编号后接小写词或抽象名词，
    不可能长得像"姓, 首字母"。缺年份、或只有人名（致谢里的姓名串）都不算。"""
    s = (seg or "").lstrip()
    if not s or not _REF_NAME_FIRST_RE.match(s):
        return False
    if not _REF_YEAR_TOKEN_RE.search(s):
        return False
    return bool(_REF_JOURNAL_RE.search(s) or _REF_VOLPAGE_RE.search(s) or _REF_DOI_RE.search(s))


def _ref_entry_strong(seg):
    """条目判据（主判据 或 证据分兜底）。

    为什么要兜底：人名在首位是**英文期刊**的版式；中文文献（"张三, 物理学报, 2020, 69: 123"）、
    期刊名被抽取打散、只用姓氏缩写等情况下会判不出来，实测会让约 250 个引用块失去标注
    （退出引文关联的数据源）。兜底分支仍要求 DOI/卷页/年份这类实打实的证据，不是"像编号就算"。"""
    return _ref_entry_like(seg) or _ref_entry_evidence(seg) >= 0.35


def _refs_text_like(text, min_score=0.45):
    """整块"像参考文献列表"的**密度**判据 —— 对"流式条目"版式有效。

    逐条目正则锚在行首，所以 `References 1. Author … 2. Author …`（标题与全部条目挤在同一段）
    这种版式数不到条目。实测本机 316 篇里有 28 篇是这种，旧代码靠"≥1 条"的宽松门放进来，
    一旦把门收紧就会全部漏判（真参考文献不再入库 → 引文关联退化）。这里用整体信号密度兜住：
    年份数 + 卷-页/DOI 的绝对下限，再用 DOI/et al 与期刊缩写加分。

    反例（实测得分均为 0）：作者单位行、正文编号列表、图注块、含年份的普通正文段落。"""
    t = text or ""
    if len(t) < 200:
        return False
    years = len(_REF_YEAR_TOKEN_RE.findall(t))
    volpage = len(_REF_VOLPAGE_RE.findall(t))
    dois = len(_REF_DOI_RE.findall(t))
    if years < 4 or (volpage < 1 and dois < 1):
        return False
    etal = len(_REF_ETAL_RE.findall(t))
    journ = len(_REF_JOURNAL_RE.findall(t))
    score = (0.30 * min(1.0, years / 8.0) + 0.25 * min(1.0, volpage / 4.0)
             + 0.20 * min(1.0, (dois + etal) / 4.0) + 0.15 * min(1.0, journ / 6.0))
    return score >= min_score


def _ref_entry_evidence(seg):
    """一条条目"像文献"的程度（0..1）。seg = 条目起点后的 ≤250 字。"""
    seg = seg or ""
    score = 0.0
    if _REF_DOI_RE.search(seg):
        score += 0.5                                  # DOI：几乎不可能是别的
    if _REF_VOLPAGE_RE.search(seg):
        score += 0.3                                  # 卷, 页
    if _REF_YEAR_PAREN_RE.search(seg) or _REF_SIGNAL_RE.search(seg):
        score += 0.3                                  # 括号年份 / et al / & X / 期刊缩写
    if re.search(r"\bet\s+al\.?\b|&\s+[A-Z]", seg):
        score += 0.2                                  # 作者串信号
    return min(1.0, score)


def _ascending_ref_spans(text, min_chain=6, max_gap=900):
    """无标题 References：全文任意位置（偏后半）的递增条目链。

    Nature 系论文的参考文献没有可检索的标题行（标题是图形），且正文 refs（1..30）
    与 Methods refs（31..37）分成两段、中间隔着正文/图注；Science/PRB 的 '1. Author'
    行首风格同理。这里按"编号从 1 开始递增（或续接上一条已接受链）、条目过半含
    年份/et al 信号"识别引文链，返回 [(链首字符位, 链尾字符位)]。"""
    lines = []
    pos = 0
    for line in text.split("\n"):
        lines.append((pos, line))
        pos += len(line) + 1
    entries = []
    for (pos, line) in lines:
        m = _REF_CHAIN_ENTRY_RE.match(line)
        if m:
            num = int(m.group(1) or m.group(2))
            entries.append((pos, num))
    chains, cur = [], []
    for (pos, num) in entries:
        if cur and num == cur[-1][1] + 1 and pos - cur[-1][0] < max_gap:
            cur.append((pos, num))
        else:
            if len(cur) >= min_chain:
                chains.append(cur)
            cur = [(pos, num)]
    if len(cur) >= min_chain:
        chains.append(cur)
    spans, last_num = [], 0
    for ch in chains:
        first = ch[0][1]
        if first != 1 and first != last_num + 1:
            continue  # 不从头开始也不续接：多为 Methods 编号步骤等，宁缺勿错
        if ch[0][0] < len(text) * REF_CHAIN_MIN_CHAR_POS:
            continue  # 引文链不会出现在全文前半段（正文编号小节多在更前面）
        if not _chain_citation_like(text, ch):
            continue
        spans.append((ch[0][0], _chain_end(text, ch[-1][0], lines)))
        last_num = ch[-1][1]
    return spans


def _chain_citation_like(text, ch, min_frac=0.5):
    """链上条目是否像参考文献：按"编号递增 + 人名在首位 + 年份 + 期刊/卷页/DOI"判定。

    这是唯一的条目判据（旧实现是"宽松正则 + 抬高条数门槛"两套，收紧了就丢掉整条链）。
    正文编号列表在"人名在首位"这一条上直接出局。"""
    hits = 0
    for k, (pos, _num) in enumerate(ch):
        nxt = ch[k + 1][0] if k + 1 < len(ch) else len(text)
        if _ref_entry_strong(text[pos:min(nxt, pos + 250)]):
            hits += 1
    return hits >= max(3, int(len(ch) * min_frac))


def _chain_end(text, last_pos, lines):
    """链末条内容截止：空行或停止行（Acknowledgements / © / 图注 / Methods 等）。"""
    end = last_pos
    for (pos, line) in lines:
        if pos < last_pos:
            continue
        if pos > last_pos + 4000:
            break
        if pos > last_pos and (not line.strip() or _REF_STOP_RE.match(line.strip())):
            break
        end = pos + len(line)
    return end


def _apply_ref_spans(paragraphs, pages, spans):
    """把字符级引文链区间映射到段落（边界段落按区间切开）。

    返回 (新段落列表, refs 段落下标集合, 平行页列表)；pages 为 None 时保持 None。"""
    joined = "\n\n".join(paragraphs)
    offs, pos = [], 0
    for p in paragraphs:
        offs.append(pos)
        pos += len(p) + 2
    ref_paras = set()
    out_p, out_g = [], []
    for i, p in enumerate(paragraphs):
        s, e = offs[i], offs[i] + len(p)
        cuts = sorted((max(cs, s), min(ce, e)) for (cs, ce) in spans if cs < e and s < ce)
        if not cuts:
            out_p.append(p)
            if pages is not None:
                out_g.append(pages[i])
            continue
        cur = s
        for (cs, ce) in cuts:
            if cs > cur:
                out_p.append(joined[cur:cs].strip())
                if pages is not None:
                    out_g.append(pages[i])
            piece = joined[cs:ce].strip()
            if piece:
                out_p.append(piece)
                if pages is not None:
                    out_g.append(pages[i])
                ref_paras.add(len(out_p) - 1)
            cur = max(cur, ce)
        if cur < e:
            out_p.append(joined[cur:e].strip())
            if pages is not None:
                out_g.append(pages[i])
    # pages 为 None 时保持 None 语义（原先返回空列表 []，会让下游把"无页信息"
    # 误当成"有页列表但为空"；当前调用方都有 len() 守卫，属预防性修正）
    return out_p, ref_paras, (None if pages is None else out_g)


def _refs_block_like(text, min_entries=4, min_frac=0.5, min_evidence=1.2):
    """段块整体是否像参考文献列表。

    主判据 = **人名在首位的条目占比**（维护者给出的版式特征，见 _ref_entry_like）：
    编号后接人名 + 年份 + 期刊/卷页/DOI，这是参考文献；编号后接小写词或抽象名词的
    正文编号列表直接出局。
    兜底判据 = 逐条目证据分（DOI/卷页/年份）总分，用于人名版式不标准的库（中文文献、
    只用首字母、期刊名被抽取打散等），此时要求证据总分达标以免"一堆弱信号凑数"。"""
    ms = sorted(list(_REF_ENTRY_RE.finditer(text or "")) +
                list(_REF_ENTRY_TIGHT_RE.finditer(text or "")) +
                list(_REF_ENTRY_BRACKET_RE.finditer(text or "")),
                key=lambda m: m.start())
    if len(ms) < min_entries:
        return False
    name_first = 0
    ev = []
    for i, m in enumerate(ms):
        nxt = ms[i + 1].start() if i + 1 < len(ms) else min(m.end() + 250, len(text))
        seg = text[m.end():nxt]
        if _ref_entry_strong(seg):
            name_first += 1
        ev.append(_ref_entry_evidence(seg))
    if name_first >= max(2, int(len(ms) * min_frac)):
        return True
    hits = sum(1 for e in ev if e >= 0.3)
    return hits >= max(2, int(len(ms) * min_frac)) and sum(ev) >= min_evidence


def _find_ref_index(paragraphs, pages=None):
    """后置 References 兜底：在段落列表中定位参考文献。
    返回 (段落列表, refs 段落下标集合, 平行页列表)。
    三阶段：
    1) 有标题：文档后半段中最后一个带 references/bibliography/参考文献 行首标题、
       且标题之后出现序号条目的段（避免表格单元格里的 "references" 字样），在该行拆段，
       标题之后全部段落归 References；
    2) 无标题：文末【真正连续】的序号条目高密度段（每段 ≥2 条目；允许跳过 1 个
       尾部噪声段。修复：旧实现用 n-run_start 判断"成片"，单段公式噪声即可劫持）；
    3) Nature 式无标题引文链：递增 'N.' 条目链（正文 refs + Methods refs 两段离散链），
       见 _ascending_ref_spans。

    位置口径：一律按**字符**占比（不再用段落序号）—— 正文段长、文献条目段短，两种口径
    在同一篇里能差一倍，实测有 11 篇真参考文献因为"段落位置"过早而被整条漏掉。"""
    n = len(paragraphs)
    offsets, pos = [], 0
    for p in paragraphs:
        offsets.append(pos)
        pos += len(p) + 1
    total_chars = max(1, pos)
    idx = None
    for i, p in enumerate(paragraphs):
        m = _REF_HEAD_RE.search(p)
        # 标题必须落在字符位置 50% 之后（参考文献很少更早出现）；只看位置、不看段落序号
        if not m or offsets[i] < total_chars * REF_HEAD_MIN_CHAR_POS:
            continue
        tail = p[m.start():] + "\n" + "\n".join(paragraphs[i + 1:i + 6])
        # 旧判据是"tail 里有 ≥1 条像条目"，于是标题行 + 作者单位行就能骗过它。
        # 现在两条通道任一成立才算（标题本身已是强证据）：
        #   ① 逐条目证据分（条目在行首的常规版式）
        #   ② 整块密度判据（标题与条目同段的"流式"版式，逐条目正则数不到）
        if _refs_block_like(tail, min_entries=3, min_frac=0.5, min_evidence=0.9) or _refs_text_like(tail):
            idx = i                        # 取最后一个同时满足条件的段
    if idx is not None:
        m = _REF_HEAD_RE.search(paragraphs[idx])
        pg = pages[idx] if pages else None
        body = paragraphs[idx][:m.start()].strip()
        refp = paragraphs[idx][m.start():].strip()
        newp, newpg = [], []
        if body:
            newp.append(body); newpg.append(pg)
        newp.append(refp); newpg.append(pg)
        out_p = paragraphs[:idx] + newp + paragraphs[idx + 1:]
        out_g = pages
        if pages is not None:
            out_g = pages[:idx] + newpg + pages[idx + 1:]
        start = idx + (1 if body else 0)
        return out_p, set(range(start, len(out_p))), out_g
    # 阶段 2：文末连续序号条目高密度段（从文末向前走，允许跳过 1 个尾部噪声段）
    i = n - 1
    if i >= 0 and _ref_entry_count(paragraphs[i]) < 2:
        i -= 1                             # 最多跳过 1 个无条目尾段（如版权行）
    run_start = None
    while i >= 0 and _ref_entry_count(paragraphs[i]) >= 2:
        run_start = i
        i -= 1
    if run_start is not None and n - run_start >= 2 and \
            _refs_block_like("\n\n".join(paragraphs[run_start:])):
        return paragraphs, set(range(run_start, n)), pages
    # 阶段 3：Nature 式无标题递增引文链（离散多段）
    spans = _ascending_ref_spans("\n\n".join(paragraphs))
    if spans:
        out_p, ref_paras, out_g = _apply_ref_spans(paragraphs, pages, spans)
        if ref_paras:
            return out_p, ref_paras, out_g
    return paragraphs, set(), pages


# 参考文献最多占全文**字符**的比例。超过就判定过宽并放弃 References 判定（见 chunk_document）。
# 为什么按字符而不是段落数：很多 PDF 把每条参考文献单独成段，于是"段落占比"会在**真参考文献**
# 上飙到 60%（实测 id=302：60% 段但只有 29% 字符），而"吞掉整篇"的病例是 66%–100% 字符。
# 实测对照组（真参考文献、判得对）：字符占比 28%–41%。
REF_MAX_CHAR_SHARE = 0.6
# 位置口径：一律按**字符**占比，不用段落序号（正文段长、文献条目段短，两种口径能差一倍）。
# 实测：同一批文档里，标题的段落位置 26%–57% 对应字符位置 34%–74%。
REF_HEAD_MIN_CHAR_POS = 0.5     # 有标题的 References 不会出现在前半段
REF_CHAIN_MIN_CHAR_POS = 0.3    # 无标题的递增引文链起点门槛（Nature 系正文 refs 可能较早）
# 整篇不可检索时的兜底：写回哪一节的下限（太短的段落救出来也只是噪声）。
RESCUE_MIN_CHARS = 800


def chunk_document(full_text, paras=None):
    """Section-aware chunking; falls back to paragraph merging.
    返回 [(section, weight, text, para_start, para_end, page_start, page_end)]。
    paras：可选 [(PDF物理页码或None, 段落文本)]，来自 read_document 的 meta['_paras']；
    - 段落号为全局计数（从文献第一个段落到最后一个，References 除外），跨章节不重置；
    - 页码为 PDF 物理页码（1 基），段落归属其起始页；txt/md/docx 无页（None）；
    - References（weight 0）保留入库供引文关联使用（检索时按 weight>0 排除）；
    - 后置兜底：行级 references/bibliography/参考文献 标题之后的全部内容归为 References。"""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", full_text) if p.strip()]
    pages = None
    if paras is not None:
        pages = [pg for pg, _ in paras]
        paragraphs = [t for _, t in paras]
    paragraphs, ref_paras, pages = _find_ref_index(paragraphs, pages)
    # 总量上限：按**字符占比**判断"这次判定是不是把整篇都算成参考文献了"。
    # 超限时**宁可少判**：退回"没有 References"，让整篇按正文索引 —— 判错的代价
    # （正文查不到）远大于少判（引文关联少一点数据源）。
    total_chars = sum(len(p) for p in paragraphs) or 1
    ref_chars = sum(len(paragraphs[i]) for i in ref_paras if i < len(paragraphs))
    if ref_chars > REF_MAX_CHAR_SHARE * total_chars:
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", full_text) if p.strip()]
        if paras is not None:
            pages = [pg for pg, _ in paras]
            paragraphs = [t for _, t in paras]
        else:
            pages = None
        ref_paras = set()

    def _pg_of(pno):
        """段落序号 -> PDF 页码（无页信息返回 None）。"""
        if pages is None or not (0 < pno <= len(pages)):
            return None
        return pages[pno - 1]

    sectioned = []  # (section, weight, [(para_no, text)])
    section, weight = "Front matter", 1.0
    resume_section, resume_weight = "Front matter", 1.0   # 引文链打断前的章节，链结束还原
    buf = []
    structured = False
    para_no = 0

    def flush():
        nonlocal section, weight, buf
        if buf:
            sectioned.append((section, weight, buf))
        buf = []

    for pi, p in enumerate(paragraphs):
        para_no += 1
        if pi in ref_paras:
            # 引文链段（无标题 References，可能离散多段）：整段归 References，不做 heading 切分
            if section != "References":
                structured = True
                flush()
                resume_section, resume_weight = section, weight   # 记住被打断的章节
                section, weight = "References", 0.0
            buf.append((para_no, p))
            continue
        if section == "References":
            # 引文链结束：**还原**被打断的章节，而不是一律重置为 Front matter。
            # Nature 式论文的正文 refs 与 Methods refs 是两段离散区间，链后有正文；
            # 原实现会把这段正文标成 §Front matter 且权重掉到 1.0，导致
            # section 过滤（如 Methods）漏召回、排序权重偏低、结果里 §标签错误。
            flush()
            section, weight = resume_section, resume_weight
        segs = [(para_no, s) for s in split_inline_headings(p) if s]
        for j, (pno, seg) in enumerate(segs):
            hit = match_section_prefix(seg, allow_long=(j > 0))
            if hit is not None:
                structured = True
                flush()
                section, weight, rest = hit
                if rest:
                    buf.append((pno, rest))
                continue
            if CAPTION_RE.match(seg):
                structured = True
                flush()
                # 图注独立成块，但**不要**把后续正文重置为 Front matter：
                # Results 中插图的图注之后的段落原会被标成 §Front matter/1.0
                # （与引文链同类缺陷），这里同样还原被打断的章节。
                caption_resume = (section, weight)
                section, weight = "Figure/Table", 1.0
                buf.append((pno, seg))
                flush()
                section, weight = caption_resume
                continue
            buf.append((pno, seg))
    flush()

    if not structured:
        return fallback_chunks(paragraphs, ref_paras=ref_paras, pages=pages)

    sectioned = _promote_abstract(sectioned)

    def _emit(sec, w, paras, force_weight=None):
        """把一节切成分块。force_weight 用于兜底写回（见下）。"""
        ww = w if force_weight is None else force_weight
        ps, pe = paras[0][0], paras[-1][0]
        pgs = [pg for pg in (_pg_of(pno) for (pno, _) in paras) if pg is not None]
        pg_s = min(pgs) if pgs else None
        pg_e = max(pgs) if pgs else None
        # References 保留换行结构（引文条目按行切分）；其余章节照常 clean 压平
        if sec == "References" and force_weight is None:
            body = "\n".join(t for _, t in paras)
        else:
            body = clean(" ".join(t for _, t in paras))
        if len(body) < 40:
            return []
        if ww <= 0 and sec != "References":
            return []                    # 仅 References 保留（引文关联数据源），其余权重 0 章节丢弃
        if len(body) > 1200:
            if sec == "References" and force_weight is None:
                pieces = split_refs(body)          # 行边界切分，保留 'N.' 行首锚点
            else:
                pieces = split_long(body)
            return [(sec, ww, piece, ps, pe, pg_s, pg_e) for piece in pieces]
        return [(sec, ww, body, ps, pe, pg_s, pg_e)]

    chunks = []
    for sec, w, paras in sectioned:
        chunks.extend(_emit(sec, w, paras))

    # ── 整篇不可检索兜底 ──────────────────────────────────────────────────────
    # 若一篇文档**没有任何 weight > 0 的分块**，它在检索里等于不存在（向量也只给 weight>0 的
    # 分块建），而用户完全无从察觉。References 判定一旦整体跑偏（作者单位行/编号正文/算法伪代码
    # 触发），就会得到这个结果。这里做最后一道保证：宁可把被判成 References 的内容按正文
    # 权重写回，也不能让整篇消失。section 名带 (rescued) 后缀，便于在结果里一眼看出发生过兜底。
    if not any(c[1] > 0 for c in chunks):
        for sec, w, paras in reversed(sectioned):
            body_len = len(clean(" ".join(t for _, t in paras)))
            if body_len < RESCUE_MIN_CHARS:
                continue
            rescued = _emit("%s (rescued)" % sec, 1.0, paras, force_weight=1.0)
            if rescued:
                chunks = rescued
                break
    return chunks


def fallback_chunks(paragraphs, low=300, high=800, ref_paras=None, pages=None):
    """Paragraph merging with sentence-level splitting for oversized blocks.
    返回 [(section, weight, text, para_start, para_end, page_start, page_end)]，
    段落号为全局序号；pages 为平行页列表（无页信息传 None）。
    ref_paras：References 段落下标集合（可离散多段，weight 0，入库供引文关联）。"""
    pieces = []                      # (para_no, text)

    def _pg_of(pno):
        if pages is None or not (0 < pno <= len(pages)):
            return None
        return pages[pno - 1]

    for i, p in enumerate(paragraphs, start=1):
        if ref_paras and (i - 1) in ref_paras:
            pieces.append((i, p))    # References 段落整段保留（不拆分）
            continue
        if len(p) > high:
            pieces.extend((i, piece) for piece in split_long(p, limit=high))
        else:
            pieces.append((i, p))
    chunks, buf = [], []             # buf: [(para_no, text)]
    ref_buf = []

    def buf_len():
        return len(clean(" ".join(t for _, t in buf)))

    for pno, p in pieces:
        if ref_paras and (pno - 1) in ref_paras:
            ref_buf.append((pno, p))
            continue
        if buf and buf_len() + len(p) + 1 > high and buf_len() >= low:
            pg = _pg_of(buf[0][0])
            chunks.append(("Body", 1.0, clean(" ".join(t for _, t in buf)),
                           buf[0][0], buf[-1][0], pg, pg))
            buf = [(pno, p)]
        else:
            buf.append((pno, p))
    if buf:
        pg = _pg_of(buf[0][0])
        chunks.append(("Body", 1.0, clean(" ".join(t for _, t in buf)),
                       buf[0][0], buf[-1][0], pg, pg))
    if ref_buf:
        # References 保留换行结构（引文条目按行切分）
        pg = _pg_of(ref_buf[0][0])
        chunks.append(("References", 0.0, "\n".join(t for _, t in ref_buf),
                       ref_buf[0][0], ref_buf[-1][0], pg, pg))
    return chunks


# ---------------------------------------------------------------- extraction


def read_document(path):
    """Return (text, pdf_meta_or_None). Raises on unsupported/corrupt input."""
    ext = Path(path).suffix.lower()
    if ext == ".pdf":
        import fitz  # PyMuPDF
        doc = fitz.open(str(path))
        try:
            pages_text = []
            for page in doc:
                txt = page.get_text()
                sups = _superscript_cites(page)
                if sups:
                    txt = _bracket_superscripts(txt, sups)
                pages_text.append(txt)
            text = "\n".join(pages_text)
            meta = dict(doc.metadata or {})
            # First-page signals for reliable identifier extraction.
            p1 = pages_text[0] if pages_text else ""
            meta["_page1_text"] = p1
            meta["_page1_title"] = _largest_font_title(doc[0]) if len(doc) else None
            # XMP 元数据：部分出版商的 PDF 正文里根本不印 DOI，只写在 XMP 里
            # （实测 Science Advances / RSC / Nature 系 16 篇），作为 DOI 的第二来源。
            try:
                meta["_xmp"] = doc.get_xml_metadata() or ""
            except Exception:
                meta["_xmp"] = ""
            # 段落 → PDF 物理页码（1 基）映射：段落归属其起始页（_paras=[(page, text)]）
            paras = []
            for pno, ptext in enumerate(pages_text, start=1):
                for seg in re.split(r"\n\s*\n", ptext):
                    seg = seg.strip()
                    if seg:
                        paras.append((pno, seg))
            meta["_paras"] = paras
        finally:
            doc.close()
        return text or "", meta
    if ext in {".txt", ".md", ".markdown"}:
        raw = Path(path).read_bytes()
        for enc in ("utf-8-sig", "utf-8", "gb18030"):
            try:
                return raw.decode(enc), None
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace"), None
    if ext == ".docx":
        try:
            import docx
            d = docx.Document(str(path))
            return "\n".join(p.text for p in d.paragraphs), None
        except ImportError:
            return _docx_fallback(path), None
    raise ValueError(f"unsupported file type: {ext}")


def _superscript_cites(page):
    """Nature 系上标数字引用检测（角标识别）。

    PDF 文本层会把上标角标压平成紧贴单词的普通数字（如 'graphene1,2'），
    丢失"这是引用"的信号；只有字体度量还能救：上标 span 的字号 ≈ 行内正文的
    70% 且基线抬高。这里按 (锚点尾部, 上标簇文本) 返回命中，供
    _bracket_superscripts 转写成 '[1,2]' 方括号形式，让 _INCITE_RE 能解析。

    宁缺勿错：跳过作者行（一串通讯/同等贡献上标）、锚点以数字结尾（指数
    10¹² 之类）、含非数字/逗号/连字符的簇（如作者单位 '1*'）。"""
    try:
        d = page.get_text("dict")
    except Exception:
        return []
    out = []
    for b in d.get("blocks", []):
        if b.get("type") != 0:
            continue
        for l in b.get("lines", []):
            spans = [s for s in (l.get("spans") or []) if s.get("text")]
            if len(spans) < 2:
                continue
            body = max(s["size"] for s in spans)
            if body < 4:
                continue
            base = [s["origin"][1] for s in spans if s["size"] >= body * 0.9]
            if not base:
                continue
            baseline = min(base)
            small = [s["size"] <= body * 0.80 and s["origin"][1] <= baseline - body * 0.12
                     for s in spans]
            if not any(small):
                continue
            # 相邻小上标 span 合并为簇（'1' + ',' + '2' -> 一个簇）
            groups, i = [], 0
            while i < len(spans):
                if small[i]:
                    j = i
                    while j + 1 < len(spans) and small[j + 1]:
                        j += 1
                    groups.append((i, j))
                    i = j + 1
                else:
                    i += 1
            if len(groups) >= 3:
                continue  # 作者行/致谢名单：一串上标，宁缺勿错
            for (a, b2) in groups:
                cluster = "".join(spans[k]["text"] for k in range(a, b2 + 1)).strip()
                core = re.sub(r"\s+", "", cluster)
                if not re.fullmatch(r"[\d,;–\-—]+", core) or not any(c.isdigit() for c in core):
                    continue  # '1*'、'a,b' 等非纯数字角标（单位/脚注字母）
                if len(re.findall(r"\d{1,3}(?!\d)", core)) > 8:
                    continue
                if a == 0:
                    continue  # 行首无锚点
                tail = spans[a - 1]["text"].rstrip()[-12:]
                if not tail or tail[-1].isdigit():
                    continue  # 锚点以数字结尾：多为指数（10¹²）
                out.append((tail, cluster))
    return out


def _bracket_superscripts(text, cites, cap=60):
    """把已识别的上标引用簇写成方括号形式：'graphene1,2' -> 'graphene[1,2]'。

    只有在锚点尾部 + 簇文本能整串在页面文本中找到时才替换（span 拼接与
    get_text 的行内拼接一致），找不到就跳过，不猜位置。"""
    n = 0
    for tail, cluster in cites:
        if n >= cap:
            break
        core = re.sub(r"\s+", "", cluster)
        new_text, k = text, 0
        for needle in (tail + cluster, tail + core):
            if needle == tail + "[" + core + "]":
                continue
            new_text, k = re.subn(re.escape(needle), tail + "[" + core + "]", text)
            if k:
                break
        if k:
            text = new_text
            n += k
    return text


def _largest_font_title(page):
    """The real title is usually the largest-font line(s) near the top of page 1."""
    try:
        d = page.get_text("dict")
    except Exception:
        return None
    lines = []
    for block in d.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = line.get("spans") or []
            if not spans:
                continue
            size = max(s["size"] for s in spans)
            txt = "".join(s["text"] for s in spans).strip()
            if txt:
                lines.append((size, txt))
    if not lines:
        return None
    top = lines[:15]
    maxsize = max(s for s, _ in top)
    cands = [t for s, t in top if abs(s - maxsize) < 0.5 and len(t) >= 8]
    return " ".join(cands) if cands else None


def _docx_fallback(path):
    import zipfile
    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", errors="replace")
        xml = re.sub(r"<w:p[ >]", "\n", xml)
        return re.sub(r"<[^>]+>", "", xml)
    except Exception:
        return ""


_TITLE_BAD = {"untitled", "无标题", "标题", "作者", "author", "unknown", "论文",
              "document", "无题", "title", "untitled document"}
# PDF /Author 里常见的非人名值（实测真实库里有 "user"、"Administrator"、"aipuser"）：
# 出版商账号、系统账号、软件名。命中即弃用 → 回退到文件名解析。
_AUTHOR_BAD = {"作者", "author", "unknown", "authors", "none", "佚名",
               "user", "administrator", "admin", "owner", "guest", "anonymous",
               "windows", "microsoft", "adobe", "acrobat", "pdf", "pc", "home"}
_AUTHOR_JUNK_RE = re.compile(
    r"^[a-z]{0,12}user$"                      # user / aipuser / scipubuser 这类账号
    r"|^(?:the\s+)?(?:administrator|admin|owner|guest|anonymous)$"
    r"|^(?:windows|microsoft|adobe|acrobat|foxit)\b", re.I)
_WORD_PREFIX_RE = re.compile(r"^microsoft\s+(?:word|powerpoint|excel)\s*[-–—:：]?\s*", re.I)
_JUNK_TITLE_RE = re.compile(
    r"(^|/)(preprint|manuscript|submission|document\d*|latest corrections|formatted)\b|"
    r"\.(docx?|pptx?|tex|cls|pdf|indd|idml|qxd?|fm|fmx|ai|psd)$|"
    r"^arxiv\s*:|^template\s+for|^article\s+type\s*:?|^sample\b.*\barticle\b|"
    r"^intechopen|^page\s+\d+\s+of|^[\w\-]+\.(docx?|pptx?|tex)$|"
    r"^[\w]+(_[\w]+)+$|^doi\s*:",
    re.I)
# PDF 的 /Author 常常是排版/制作人员（实测某 Nature 系 PDF 的 /Author 是排版员 "Smith, John"），
# 形如单个 "姓, 名"；只有当首页或文件名同时给出多作者信号时才判定为生产信息并弃用。
_PROD_AUTHOR_RE = re.compile(r"^[A-Z][A-Za-z'’\-]+,\s*[A-Z][A-Za-z'’\-]*(?:\s+[A-Z]\.?)?$")
_HEAD_SKIP_RE = re.compile(
    r"^(?:abstract\b|introduction\b|doi\b|https?://|www\.|"
    r"fig(?:ure)?\.?\s*\d|table\.?\s*\d|scheme\.?\s*\d|"
    r"corresponding\s+author|received\b|accepted\b|published\b|"
    r"issn\b|isbn\b|copyright\b|©|journal\s+of\b|vol(?:ume)?\.?\s*\d|"
    r"arxiv\s*:|template\s+for|manuscript|sample\b|article\s+type|"
    r"page\s+\d+\s+of|intechopen|submitted|preprint)",
    re.I)
_DIGITONLY_LINE = re.compile(r"^[\d\s\-–—.,;:()\[\]{}]+$")


def _usable_title(s):
    s = (s or "").strip()
    if not s or s.lower() in _TITLE_BAD:
        return None
    s = _WORD_PREFIX_RE.sub("", s).strip()
    if not s or s.lower() in _TITLE_BAD:
        return None
    s = _FN_ZLIB_RE.sub("", s).strip()   # 剥 "(Z-Library)" 等书库尾巴
    if not s or s.lower() in _TITLE_BAD:
        return None
    # 封面大字号行有时把标题重复两遍（如 "Advanced Computing ... Advanced Computing ..."）
    half = len(s) // 2
    if len(s) >= 24 and s[:half].strip() == s[half:].strip():
        s = s[:half].strip()
    if _JUNK_TITLE_RE.search(s):
        return None
    words = [w for w in s.split() if re.search(r"[A-Za-z]", w)]
    cjk_n = len(re.findall(r"[\u4e00-\u9fff]", s))
    # 有效标题：≥2 个拉丁词，或 ≥4 个连续拉丁字母，或 ≥4 个汉字（支持纯中文标题，如学位论文/中文期刊）
    if len(words) < 2 and not re.search(r"[A-Za-z]{4,}", s) and cjk_n < 4:
        return None
    if _DIGITONLY_LINE.match(s):
        return None
    return s


def _first_page_title(text):
    """Conservative first-page title heuristic: the first plausible prose line."""
    if not text:
        return None
    for line in text[:3000].split("\n")[:14]:
        l = " ".join(line.split())
        if not (10 <= len(l) <= 320):
            continue
        if _HEAD_SKIP_RE.match(l) or _DIGITONLY_LINE.match(l):
            continue
        words = [w for w in l.split() if re.search(r"[A-Za-z]", w)]
        cjk_n = len(re.findall(r"[\u4e00-\u9fff]", l))
        if len(words) >= 2 or re.search(r"[A-Za-z]{4,}", l) or cjk_n >= 6:
            return l
    return None


def _clean_authors(s):
    if not s:
        return None
    s = re.sub(r"\s+", " ", s).strip().strip(".,;:")
    if not s or s.lower() in _AUTHOR_BAD or _AUTHOR_JUNK_RE.match(s):
        return None
    return s or None


def _looks_production_author(author, page1, stem):
    """判断 PDF /Author 是否为排版/制作信息（而非作者）。

    判据（保守，宁可漏判不可错判）：形如单个 "姓, 名"（如 "Smith, John"，占位示例），
    且首页出现 "et al" / " & " 或文件名带 "和"/"&" 等多作者信号。
    实测案例：某 Nature 系 PDF 的 /Author 是排版员，导致库内作者字段错误，
    进而使"引文库内匹配"的作者+年份规则失效。"""
    a = (author or "").strip()
    if not a or not _PROD_AUTHOR_RE.match(a):
        return False
    p1 = page1 or ""
    multi = (re.search(r"\bet\s+al\b", p1, re.I) is not None
             or " & " in p1
             or re.search(r"[和&]", stem or "") is not None)
    return bool(multi)


def _clean_year(value):
    try:
        y = int(value)
    except (TypeError, ValueError):
        return None
    if 1900 <= y <= datetime.now().year + 1:
        return y
    return None


_DOI_RE = re.compile(r"\b(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)")
_ARXIV_RE = re.compile(r"(?:arxiv\.org/(?:abs|pdf)/|arxiv:\s*)(\d{4}\.\d{4,5})", re.I)

# ---- 文件名命名习惯解析（Zotero / 下载站 / 中文习惯） ----
# "Author - YYYY - Title.pdf"（Zotero 导出 / 常规下载站）
_FN_AUTHOR_YEAR_RE = re.compile(
    r"^(?P<author>.+?)\s*-\s*(?P<year>(?:19|20)\d{2})\s*-\s*(?P<title>.+)$")
# 尾巴 "(Z-Library)" / "(z-library.sk, 1lib.sk, z-lib.sk)"
_FN_ZLIB_RE = re.compile(r"\s*\([^()]*z[-_ ]?lib[^()]*\)\s*$", re.I)
# 尾巴 "(作者1, 作者2)"（下载站书库命名，如 "(Ashim Kumar Bain, Prem Chand)"）
_FN_TRAIL_AUTHORS_RE = re.compile(r"\s*\([^()]*,[^()]*\)\s*$")


def _filename_title(stem):
    """从文件名提取标题：剥 '作者 - 年份 - ' 前缀、(Z-Library) 与 (作者1,作者2) 尾巴；
    中文习惯 '作者-标题'（无年份）且标题部分以汉字为主时同样拆分。"""
    t = stem.strip()
    m = _FN_AUTHOR_YEAR_RE.match(t)
    if m:
        t = m.group("title")
    else:
        parts = t.split("-", 1)
        if len(parts) == 2:
            a, tt = parts[0].strip(), parts[1].strip()
            if (tt and len(re.findall(r"[\u4e00-\u9fff]", tt)) >= 6
                    and a and len(a) <= 20):
                t = tt
    t = _FN_ZLIB_RE.sub("", t)
    t = _FN_TRAIL_AUTHORS_RE.sub("", t)
    t = re.sub(r"^[\s\-_]+|[\s\-_]+$", "", t)
    return t or stem.strip()


def _filename_author(stem):
    """从文件名提取作者（保守）：'作者 - 年份 - 标题'、中文 '作者-标题'、
    '(作者1, 作者2)'（下载站书库）、'(作者)'（中文单作者）。"""
    m = _FN_AUTHOR_YEAR_RE.match(stem)
    if m:
        a = re.sub(r"\s+", " ", m.group("author").strip())
        return a or None
    parts = stem.split("-", 1)
    if len(parts) == 2:
        a, t = parts[0].strip(), parts[1].strip()
        if (t and len(re.findall(r"[\u4e00-\u9fff]", t)) >= 6 and a and len(a) <= 20):
            return a
    s = _FN_ZLIB_RE.sub("", stem)                      # 先剥书库尾巴
    m2 = re.search(r"\(\s*([^()]*,[^()]*)\s*\)\s*$", s)   # "(作者1, 作者2)"
    if m2:
        names = [n.strip().strip("，,;") for n in m2.group(1).split(",") if n.strip()]
        if names and all(2 <= len(n) <= 40 for n in names):
            return "; ".join(names[:6])
    m3 = re.search(r"\(\s*([\u4e00-\u9fff]{2,6})\s*\)", s)   # "(赵巍胜)"
    if m3:
        return m3.group(1)
    return None


def extract_meta(path, text, pdf_meta=None):
    title = authors = journal = doi = None
    year = None
    page1 = ""
    if pdf_meta:
        title = _usable_title(pdf_meta.get("title"))
        authors = _clean_authors(pdf_meta.get("author"))
        # 年份不取 creationDate：PDF 被重新编码时该日期是"文件生成时间"而非出版年份（实测 2018 文献被误取 2026）。
        # 年份一律从首页/正文前段提取（下方 year 回退逻辑），并经过 _clean_year 合理性校验。
        page1 = pdf_meta.get("_page1_text") or ""
        if not title:
            ft = _usable_title(pdf_meta.get("_page1_title"))
            if ft and not _HEAD_SKIP_RE.match(ft) and not _JUNK_TITLE_RE.search(ft):
                title = ft
    if not title:
        title = _first_page_title(page1 or text)
    stem = Path(path).stem
    if not title:
        # 文件名回退：剥 '作者 - 年份 - ' 前缀与 (Z-Library)/(作者1,作者2) 尾巴，兼容中文 '作者-标题'
        title = _usable_title(_filename_title(stem)) or stem
    if authors and _looks_production_author((pdf_meta or {}).get("author"), page1, stem):
        # PDF /Author 是排版/制作人员（形如 "Smith, John" 的单人姓名）：弃用，交给下面的文件名回退
        authors = None
    if not authors:
        # 作者回退：文件名 '作者 - 年份 - 标题' / 中文 '作者-标题' / '(作者1,作者2)' / '(作者)'（保守）
        authors = _filename_author(stem)
    if title and len(title.split()) < 3 and len(title) <= 24:
        # 短/过泛标题偏好更长候选（如 pdf_meta 只剩期刊名 "Carbon"，文件名有完整标题）
        ft2 = _usable_title(_filename_title(stem))
        if ft2 and len(ft2.split()) >= 3 and len(ft2) > len(title):
            title = ft2

    # Identifier extraction, scoped to the title page and stopping at the
    # bibliography: a DOI here is the paper's own, not a reference's.
    scope = page1 or text[:3000]
    m = re.search(r"\breferences\b|\bbibliography\b", scope, re.I)
    if m:
        scope = scope[:m.start()]
    md = re.search(r"doi:\s*(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)", scope, re.I) or _DOI_RE.search(scope)
    if md:
        doi = md.group(1).rstrip(".,;")
    if not doi:
        # XMP 兜底：出版商 PDF 常把 DOI 只放在 XMP（dc:identifier / prism:doi / pdfx 等）。
        # 放在首页文本之后、arXiv 之前——XMP 是文档自身的元数据，不会像"全篇搜索"那样
        # 命中参考文献里别人的 DOI。
        xmp = (pdf_meta or {}).get("_xmp") or ""
        if xmp:
            mx = _DOI_RE.search(xmp)
            if mx:
                doi = mx.group(1).rstrip(".,;")
    if not doi:
        ma = _ARXIV_RE.search(page1 or text[:3000])
        if ma:
            doi = "10.48550/arXiv." + ma.group(1)
    # ---- 年份级联（优先级从可靠到兜底）----
    # ① 文件名年份（Zotero 命名习惯最可靠，如 "Kirkland - 2020 - ..."）
    if year is None:
        my = _FN_AUTHOR_YEAR_RE.match(stem)
        if my:
            year = _clean_year(my.group("year"))
    # ② 正文上下文年份（© / Copyright / Vol / ISSN / received 等日期语境的年份优先）
    if year is None:
        ctx = re.search(
            r"(?:©|\(c\)|copyright|vol(?:ume)?\.?|issn|isbn|received|accepted|published)\b"
            r"[^\n]{0,60}?\b(19|20)\d{2}", scope, re.I)
        if ctx:
            year = _clean_year(ctx.group(1))
    # ③ 括号年份（(2008) / [2008]，常见于期刊页眉/引证信息）
    if year is None:
        yp = re.search(r"[\(\[]\s*(19|20)\d{2}\s*[\)\]]", scope)
        if yp:
            year = _clean_year(yp.group(1))
    # ④ 首个裸年份（最后手段）
    if year is None:
        my2 = re.search(r"\b(19|20)\d{2}\b", scope)
        year = _clean_year(my2.group(0)) if my2 else None
    # ⑤ 过老的裸年份通常是正文引述（如 "in 1971 Leon Chua..."），用 creationDate 修正
    #    （仅当 creationDate 落在合理年代，避免重编码日期污染）
    if year is not None and year < 1990:
        cd = _creation_year(pdf_meta)
        if cd is not None and cd >= 1990:
            year = cd
    # ⑥ 完全无年份信号时 creationDate 兜底（书籍/扫描件无文字年份时；容忍重编码风险）
    if year is None:
        year = _creation_year(pdf_meta)
    return title, authors, year, journal, doi


def read_first_page(path):
    """元数据刷新专用的轻量读取：只取首页文本 + PDF 元数据 + XMP，不做全文解析、
    不做上标角标转写（那些只影响正文与引文解析）。

    为什么可行：extract_meta 的标识符/年份/标题判据全部落在 `scope = page1` 与 pdf_meta 上
    （文件名年份、© / Vol / ISSN 上下文年份、括号年份、裸年份都取 scope）。因此元数据结果与
    全量解析一致，而成本从约 0.3–1 s/篇 降到约 90 ms/篇（实测 312 篇 28–30 s）。

    非 PDF、首页无文本层、或读取异常时**退回** read_document()，保证行为不退化。"""
    p = Path(path)
    if p.suffix.lower() != ".pdf":
        return read_document(path)
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(str(p))
        try:
            meta = dict(doc.metadata or {})
            page1 = doc[0].get_text() if len(doc) else ""
            if not page1.strip():
                doc.close()
                return read_document(path)
            meta["_page1_text"] = page1
            meta["_page1_title"] = _largest_font_title(doc[0]) if len(doc) else None
            try:
                meta["_xmp"] = doc.get_xml_metadata() or ""
            except Exception:
                meta["_xmp"] = ""
            return page1, meta
        finally:
            try:
                doc.close()
            except Exception:
                pass
    except Exception:
        return read_document(path)


def _creation_year(pdf_meta):
    """从 PDF creationDate 提取年份（仅作兜底，不作首选）。"""
    try:
        m = re.search(r"(?:D:)?(19|20)\d{2}", (pdf_meta or {}).get("creationDate") or "")
        if m:
            return _clean_year(m.group(0)[-4:])
    except Exception:
        pass
    return None


# ---------------------------------------------------------------- storage


def connect(kb_root):
    root = Path(kb_root)
    root.mkdir(parents=True, exist_ok=True)
    db_file = root / "kb.sqlite"
    created = not db_file.exists()
    db = sqlite3.connect(str(db_file))
    db.row_factory = sqlite3.Row
    info = _migrate(db, created)
    info["db"] = str(db_file.resolve())
    _LAST_CONNECT.update(info)
    if not info["logged"]:
        _log_migration(info)
        info["logged"] = True
    if os.environ.get("KB_SQLITE_WAL") == "1":   # 可选 WAL：同步 .kb 目录时保持默认 DELETE 模式更安全
        try:
            db.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
    return db


def _log_migration(info):
    """迁移/建库日志。走 stderr：引擎 stdout 是 JSON 协议线，不能混入日志。"""
    if info["created"]:
        _log("[kb-rag] 知识库已创建（schema v%s）: %s" % (info["to_version"], info.get("db")))
    elif info["from_version"] != info["to_version"]:
        _log("[kb-rag] 知识库已自动迁移 v%s -> v%s：%s" % (
            info["from_version"], info["to_version"], "、".join(info["actions"]) or "无"))
        if info.get("backfilled_keys"):
            _log("[kb-rag] 已按 Zotero 存储路径回填 %d 个 zotero_key" % info["backfilled_keys"])
    elif info.get("backfilled_keys"):
        _log("[kb-rag] 已按 Zotero 存储路径回填 %d 个 zotero_key" % info["backfilled_keys"])


def _log(msg):
    try:
        print(msg, file=sys.stderr, flush=True)
    except Exception:
        pass


def _migrate(db, created):
    """版本化迁移：PRAGMA user_version 门控，幂等，纯加法优先。

    - 首次建库：一次建全表/索引并写版本号（不残留半状态）。
    - 旧库升级：v0 -> v1 补齐缺失表/列，回填 zotero_key，清理孤儿向量。
    - 未来破坏性变更：新增 `if cur < N` 迁移块（重建表），见 docs/MIGRATION.md §4。
    返回迁移说明：{created, from_version, to_version, actions, backfilled_keys}。
    """
    cur = db.execute("PRAGMA user_version").fetchone()[0] or 0
    info = {"created": created, "from_version": cur, "to_version": SCHEMA_VERSION,
            "actions": [], "backfilled_keys": 0, "logged": False}
    if created:
        # 首次建库：一次建全表 + 索引，写版本号
        db.executescript(SCHEMA)
        db.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
        db.commit()
        info["actions"].append("create_full_schema")
        return info
    # v0 -> v1：旧版（1.0.x ~ 1.4.x）库，补齐缺失的表/列
    if cur < 1:
        db.executescript(SCHEMA)                     # 幂等：缺的表/索引补齐，已存在的不动
        try:
            db.execute("ALTER TABLE docs ADD COLUMN zotero_key TEXT")
            info["actions"].append("add docs.zotero_key")
        except sqlite3.OperationalError as ex:
            if "duplicate column" not in str(ex):
                raise                                 # 锁冲突等真实错误上抛，勿静默吞
        db.execute("PRAGMA user_version = 1")
    # v1 -> v2：chunks 段落定位列（旧数据为 NULL，重入库后才有段号）
    if cur < 2:
        for col in ("para_start", "para_end"):
            try:
                db.execute("ALTER TABLE chunks ADD COLUMN %s INTEGER" % col)
                info["actions"].append("add chunks.%s" % col)
            except sqlite3.OperationalError as ex:
                # 仅幂等重入（列已存在）时吞掉；锁冲突等其他 OperationalError 必须上抛，
                # 否则版本号已置新而列缺失，后续 INSERT 每行都报 no such column 且永不重迁移
                if "duplicate column" not in str(ex):
                    raise
        db.execute("PRAGMA user_version = 2")
    # v2 -> v3：chunks 页码列（PDF 物理页码锚点；旧数据为 NULL，重入库后才有）
    if cur < 3:
        for col in ("page_start", "page_end"):
            try:
                db.execute("ALTER TABLE chunks ADD COLUMN %s INTEGER" % col)
                info["actions"].append("add chunks.%s" % col)
            except sqlite3.OperationalError as ex:
                if "duplicate column" not in str(ex):
                    raise
        db.execute("PRAGMA user_version = 3")
    # v3 -> v4：docs.indexed_with（解析器版本标记；旧行 NULL = 未知/陈旧，
    # 由 kb_stats 的 stale_docs 报出，用户可选择 metadata_only 刷新或全量重灌）
    if cur < 4:
        try:
            db.execute("ALTER TABLE docs ADD COLUMN indexed_with TEXT")
            info["actions"].append("add docs.indexed_with")
        except sqlite3.OperationalError as ex:
            if "duplicate column" not in str(ex):
                raise
        db.execute("PRAGMA user_version = 4")
    # zotero_key 回填：旧行按 Zotero storage 路径提取附件 key（幂等，只补 NULL）
    n = db.execute(
        "SELECT COUNT(*) AS n FROM docs WHERE zotero_key IS NULL AND path LIKE ?",
        ("%\\Zotero\\storage\\%",)).fetchone()["n"]
    if n:
        db.execute(
            "UPDATE docs SET zotero_key = substr(path, instr(path, '\\storage\\') + 9, 8) "
            "WHERE zotero_key IS NULL AND path LIKE ?",
            ("%\\Zotero\\storage\\%",))
        info["backfilled_keys"] = n
        info["actions"].append("backfill zotero_key")
    # 孤儿向量清理（连接时兜底）。写语句在库被异步入库等事务持锁时会阻塞至
    # busy timeout 后抛 database is locked —— 读命令的 connect 不应因此失败/长等，
    # 清理是可推迟的维护操作：以短超时探测，撞锁即跳过，留给下次连接/写命令再清。
    prev_timeout = db.execute("PRAGMA busy_timeout").fetchone()[0]
    try:
        db.execute("PRAGMA busy_timeout = 500")  # 500ms：宁可推迟清理也不让读连接干等 5s
        db.execute("DELETE FROM vecs WHERE chunk_id NOT IN (SELECT id FROM chunks)")
        info["actions"].append("cleanup orphan vecs")
    except sqlite3.OperationalError as ex:
        if "locked" not in str(ex).lower():
            raise
        info["actions"].append("cleanup orphan vecs deferred (db locked)")
    finally:
        try:
            db.execute("PRAGMA busy_timeout = %d" % prev_timeout)
        except Exception:
            pass
    # 显式提交：Python sqlite3 的 DML 在隐式事务中，close() 会回滚未提交修改
    # （backfill/孤儿清理必须在命令处理前落盘；PRAGMA user_version 不受此影响但一并提交无害）
    db.commit()
    return info


# ---------------------------------------------------------------- embeddings

_EMBEDDER = None
_EMBED_ERR = None
_EMBED_ERR_AT = 0.0
_EMBED_NAME = None

BGE_QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："

_HF_MIRROR = "https://hf-mirror.com"


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


# 模型加载失败的重试间隔（秒）。失败状态以前在守护进程内**永久**缓存：依赖装好、模型就位后
# 同一进程依然返回 None（表现为"修了还是没用"，必须杀掉守护进程或重启 DSH 才恢复，见 issue #2）。
# 0 = 每次调用都重试；负数 = 永不重试（旧行为）。守护进程内可用 reload 命令立即清缓存重试。
MODEL_RETRY_SECS = _env_float("KB_MODEL_RETRY_SECS", 120.0)


def _retry_due(err_at):
    """失败缓存是否已过期（决定这次调用是否重新尝试加载模型）。"""
    if MODEL_RETRY_SECS < 0:
        return False
    return (time.time() - err_at) >= MODEL_RETRY_SECS


# ---------------------------------------------------------------- device / batch
#
# 设备策略：默认 auto = **不改动** sentence-transformers 的选择 —— 解释器里装的是 CUDA 版
# torch 且 GPU 真能用，模型就自动加载到 GPU（引擎不需要为此写任何调用代码）。
# KB_DEVICE=cpu 可强制 CPU（显存紧张 / 排障 / 与其它吃显存的程序共存）；
# =cuda / =cuda:1 / =mps 显式指定，**失败也会自动退回 CPU**（见 _load_with_cpu_fallback）。
# 批大小可配：GPU 上 batch=32 喂不饱算力，未配置时按设备自动给默认值。
#
# 兜底的硬约束（本模块的设计不变量，改动时不要破坏）：
#   ① GPU 不存在 / CUDA 不可用 → 直接用 CPU，**不做任何重试**；
#   ② GPU 在但用不了（驱动或内核不匹配、cuDNN/cuBLAS 报错、加载期显存不足）→ 退回 CPU
#      一次，并把本进程的 GPU 标记为不可用，后续加载不再尝试（不死磕同一堵墙）；
#   ③ 运行中出 CUDA 类错误（含 OOM）→ 清缓存缩批重试 → 仍失败则把模型移到 CPU 继续跑；
#   ④ 设备探测一律缓存：torch.cuda.is_available() 每进程最多问一次（失败结果同样缓存）；
#   ⑤ 任何兜底都不能把"模型确实不可用"（文件缺失、格式错）也吞掉 —— 只有 CUDA 特征串走兜底。

KB_DEVICE = (os.environ.get("KB_DEVICE") or "auto").strip().lower()
_CUDA = None        # torch.cuda.is_available() 的缓存（None = 还没问过）
_GPU_OK = None      # None = 待定；False = 本进程已判定 GPU 不可用（sticky，不再尝试）
_GPU_WHY = None     # 判定不可用的原因（进 device_report，便于排障）
# 设备异常说明：{tag: 说明}，例如 {"embed": "GPU 不可用（…）→ 已改用 CPU"}，让 kb_stats/kb_ingest 看得到。
# 用 dict 而不是单字符串：embed 与 rerank 各记各的，且**正常加载成功时能清掉**（否则一次瞬时故障
# 会让"已回退 CPU"永远挂在报告里，用户以为现在还在 CPU 上跑）。
_DEVICE_NOTE = {}


def _device_kwargs():
    """给模型构造器的 device 参数。优先级：
    ① 本进程已判定 GPU 不可用 → **显式 CPU**（连上游的自动选择都不给它机会，避免又撞一次）；
    ② auto（默认）→ 不传 device，交给 sentence-transformers 自动选择；
    ③ 显式 cuda* 但没有可用 GPU → 直接 CPU，不做注定失败的尝试（不死磕）。"""
    if _GPU_OK is False:
        return {"device": "cpu"}
    if KB_DEVICE in ("", "auto", "none"):
        return {}
    if KB_DEVICE.startswith("cuda") and not _cuda_available():
        return {"device": "cpu"}
    return {"device": KB_DEVICE}


def _cuda_available():
    """torch 是否报告 CUDA 可用。**每进程只探测一次**（失败结果也缓存），避免反复死磕。"""
    global _CUDA
    if _CUDA is None:
        try:
            import torch
            _CUDA = bool(torch.cuda.is_available())
        except Exception:
            _CUDA = False
    return _CUDA


def _gpu_usable():
    """本进程还能不能尝试 GPU。一旦判定不可用就永久返回 False（sticky）。"""
    if _GPU_OK is False:
        return False
    if KB_DEVICE == "cpu":
        return False
    if KB_DEVICE in ("", "auto", "none"):
        return _cuda_available()
    return True                        # 显式 cuda/mps：先试一次，失败由 _disable_gpu 关掉


def _disable_gpu(tag, err):
    """把本进程的 GPU 标记为不可用：后续加载直接走 CPU，不再反复尝试同一堵墙（不死磕）。"""
    global _GPU_OK, _GPU_WHY
    _GPU_OK = False
    _GPU_WHY = "%s: %s" % (type(err).__name__, str(err)[:120])
    _set_device_note(tag, "GPU 不可用（%s）→ 已改用 CPU，本次不再尝试 GPU" % _GPU_WHY)
    return False


# CUDA 类故障的特征串。只认这些：普通故障（文件缺失、模型格式错、依赖缺失）照常上抛，
# 不被兜底掩盖成"悄悄降级"。
_CUDA_ERR_HINTS = ("out of memory", "cuda", "cudnn", "cublas", "cufft", "nccl",
                   "no kernel image", "device-side assert", "gpu", "nvml",
                   "driver", "no available kernel", "not compiled with cuda")


def _is_cuda_error(err):
    """是否是设备侧故障（显存不足 / 内核或驱动不匹配 / cuDNN/cuBLAS 报错 / 无可用设备）。"""
    msg = str(err).lower()
    return any(h in msg for h in _CUDA_ERR_HINTS)


def _is_oom(err):
    """（保留旧名）仅判定显存不足。"""
    return "out of memory" in str(err).lower()


def _load_with_cpu_fallback(tag, build):
    """build(**device_kwargs) → (模型, 是否走了 CPU 兜底)。

    这是"GPU 用不了也不能让整条链路死掉"的关键一层：加载期就撞 CUDA（驱动/内核/cuDNN/
    加载期显存）时，如果不兜底，下面的三级加载链（本地缓存 → 下载 → 镜像）会**在 GPU 上
    连撞三次**全部失败，最终把模型判成不可用 —— 而 CPU 明明能跑。"""
    try:
        return build(**_device_kwargs()), False
    except Exception as e:
        if not _is_cuda_error(e):
            raise                          # 非设备故障：不兜底，避免掩盖真实原因
        _free_cuda_cache()
        _disable_gpu(tag, e)
        return build(device="cpu"), True   # 显式 CPU 再试一次；这一次不再碰 GPU


def _batch_size(env_name, cpu_default, gpu_default):
    """批大小可配；未配时按设备给默认值（`KB_EMBED_BATCH` / `KB_RERANK_BATCH`）。"""
    raw = os.environ.get(env_name)
    if raw:
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            pass
    return gpu_default if _gpu_usable() else cpu_default


def _free_cuda_cache():
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


_BATCH_DEFAULTS = {"embed": (32, 128), "rerank": (16, 64)}


def _with_device_retry(tag, model, run):
    """run(batch_size) → 结果。设备侧故障逐级兜底：
    ① 清 CUDA 缓存 + 缩批到 4 重试；② 仍失败 → 把模型移到 CPU 再试；③ 还不行才把异常抛出去。
    覆盖的不只是 OOM，也包括运行中才暴露的 CUDA 错（内核不匹配、cuBLAS/cuDNN 报错、设备丢失）——
    否则一次瞬时故障就等于"这个会话再也用不了向量"（与 issue #2 的永久缓存同一个坑）。
    非设备故障（数据/格式问题）不兜底，原样上抛。"""
    cpu_default, gpu_default = _BATCH_DEFAULTS.get(tag, (16, 16))
    bs = _batch_size("KB_%s_BATCH" % tag.upper(), cpu_default, gpu_default)
    try:
        return run(bs)
    except Exception as e:
        if not _is_cuda_error(e):
            raise
        _free_cuda_cache()
        try:
            return run(min(4, bs))
        except Exception as e2:
            if not _is_cuda_error(e2):
                raise
            if not _fallback_cpu(model, tag):
                raise
            return run(min(4, bs))


# 旧名（补丁里叫 _with_oom_retry）：保留以免外部验证脚本失效
_with_oom_retry = _with_device_retry


def _set_device_note(tag, text):
    """记录/清除某条链路的设备异常说明（text=None 表示清除）。"""
    if text:
        _DEVICE_NOTE[tag] = text
    else:
        _DEVICE_NOTE.pop(tag, None)


def _device_note_text():
    return "；".join("%s: %s" % (t, _DEVICE_NOTE[t])
                     for t in ("embed", "rerank") if t in _DEVICE_NOTE) or None


def _move_model_to_cpu(model):
    """把模型移到 CPU：CrossEncoder 与 SentenceTransformer 的 API 不一样 ——
    SentenceTransformer（bi-encoder）有 .to()，而 CrossEncoder 包的是 HF 模型、
    本身不一定有，需要退到 model.model.to()。逐个候选尝试，成功后同步 device 属性。
    没有这层，_fallback_cpu 会在 CrossEncoder 上抛 AttributeError，
    OOM 兜底反而把原始 OOM 变成"回退也失败"（issue #2 里那个坑的同类）。"""
    last = None
    for target in (model, getattr(model, "model", None)):
        to = getattr(target, "to", None)
        if not callable(to):
            continue
        try:
            to("cpu")
        except Exception as e:
            last = e          # 换下一个候选继续试
            continue
        try:
            import torch
            if hasattr(model, "device"):
                model.device = torch.device("cpu")
        except Exception:
            pass
        return True
    raise AttributeError("model has no usable .to('cpu')" if last is None else str(last))


def _fallback_cpu(model, tag):
    """运行期兜底：把已加载的模型移到 CPU，让这次调用能跑完。
    注意这里**不**全局关掉 GPU（只有加载期就撞墙才 _disable_gpu）：另一个模型可能仍然好用。"""
    try:
        _move_model_to_cpu(model)
        _set_device_note(tag, "CUDA 故障 → 该模型已回退 CPU（重启守护进程或 reload 后会自动重试 GPU）")
        _free_cuda_cache()
        return True
    except Exception as e:
        _set_device_note(tag, "CUDA 故障且回退 CPU 失败（%s: %s）" % (type(e).__name__, str(e)[:80]))
        return False


def device_report():
    """现在实际跑在哪、为什么 —— 让"装了 GPU 却没吃上"、"GPU 用不了已退 CPU"、"OOM 已回退"都可见。
    torch 未加载时不主动 import（kb_stats 要保持便宜），只报已加载模型的实际 device。"""
    info = {"requested": KB_DEVICE}
    if _GPU_OK is False:
        info["gpu_usable"] = False       # 已判定：不需要碰 torch 就能答
        if _GPU_WHY:
            info["gpu_disabled_reason"] = _GPU_WHY
    note = _device_note_text()
    if note:
        info["note"] = note
    if "torch" not in sys.modules:
        # 不为了体检去 import torch（约 1–3 s + 数百 MB）；跑一次入库/深查后这里就有真值了
        info["hint"] = ("torch 尚未加载：跑一次 kb_ingest 或 depth=deep 的检索后，"
                        "此处会显示 torch / cuda_available / gpu 与各模型实际 device")
    if "torch" in sys.modules:
        try:
            import torch
            info["torch"] = torch.__version__
            info["cuda_available"] = bool(torch.cuda.is_available())
            info.setdefault("gpu_usable", _gpu_usable())
            if info["cuda_available"]:
                try:
                    info["gpu"] = torch.cuda.get_device_name(0)
                except Exception:
                    pass
        except Exception as e:
            info["torch_error"] = "%s: %s" % (type(e).__name__, str(e)[:120])
    for tag, model in (("embed", _EMBEDDER), ("rerank", _RERANKER)):
        if model is not None:
            dev = getattr(model, "device", None)
            if dev is not None:
                info[tag + "_device"] = str(dev)
    return info


def _apply_hf_mirror():
    """Switch huggingface_hub to the CN mirror after a direct-download failure.
    HF_ENDPOINT is baked into huggingface_hub.constants at import time (ENDPOINT
    and the derived HUGGINGFACE_CO_URL_TEMPLATE), so setting os.environ alone is
    a no-op once hub is imported — patch the constants instead."""
    os.environ.setdefault("HF_ENDPOINT", _HF_MIRROR)
    try:
        import huggingface_hub.constants as _hfc
        _hfc.ENDPOINT = _HF_MIRROR
        _hfc.HUGGINGFACE_CO_URL_TEMPLATE = _HF_MIRROR + "/{repo_id}/resolve/{revision}/{filename}"
    except Exception:
        pass


def get_embedder():
    """Lazy singleton; prefers the local HF cache, downloads with mirror auto-retry.

    失败不再永久缓存：超过 MODEL_RETRY_SECS 会重新尝试加载，因此修好依赖/模型后
    不需要重启守护进程（issue #2）。"""
    global _EMBEDDER, _EMBED_ERR, _EMBED_ERR_AT, _EMBED_NAME
    if _EMBEDDER is not None:
        return _EMBEDDER
    if _EMBED_ERR is not None and not _retry_due(_EMBED_ERR_AT):
        return None
    name = os.environ.get("KB_EMBED_MODEL", "BAAI/bge-small-zh-v1.5")

    def _build(**dkw):
        """三级加载（本地缓存 → 直连下载 → 镜像下载）。CUDA 类错误立刻上抛，
        交给 _load_with_cpu_fallback 退 CPU —— 不在 GPU 上把三级链逐条撞一遍。"""
        from sentence_transformers import SentenceTransformer
        try:
            return SentenceTransformer(name, local_files_only=True, **dkw)
        except Exception as e:
            if _is_cuda_error(e):
                raise
        try:
            return SentenceTransformer(name, **dkw)
        except Exception as e:
            if _is_cuda_error(e):
                raise
        _apply_hf_mirror()  # direct download failed; retry via mirror
        return SentenceTransformer(name, **dkw)

    try:
        model, fell_back = _load_with_cpu_fallback("embed", _build)
        _EMBEDDER = model
        _EMBED_NAME = name
        _EMBED_ERR = None          # 之前失败过、这次成功：清掉旧错误，kb_stats 不再报
        if not fell_back:
            _set_device_note("embed", None)   # 正常加载成功 → 旧的"已退 CPU"记录不再成立
    except Exception as e:
        _EMBED_ERR = f"{type(e).__name__}: {e}"[:300]
        _EMBED_ERR_AT = time.time()
    return _EMBEDDER


def encode(texts, is_query=False, cjk=False):
    """嵌入；批大小按设备自动选（`KB_EMBED_BATCH` 可覆盖），OOM 有缩批/回退兜底。"""
    model = get_embedder()
    if model is None:
        raise RuntimeError("embedding model unavailable: " + (_EMBED_ERR or "unknown"))
    prefix = BGE_QUERY_PREFIX if is_query and cjk else ""
    if prefix:
        texts = [prefix + t for t in texts]

    def _run(bs):
        return model.encode(texts, normalize_embeddings=True, batch_size=bs,
                            show_progress_bar=False).astype("float32")

    return _with_device_retry("embed", model, _run)


def pack_vec(v):
    return array.array("f", v.tolist()).tobytes()


def unpack_vec(b):
    return __import__("numpy").frombuffer(b, dtype="float32")


# ---------------------------------------------------------------- reranker

_RERANKER = None
_RERANK_ERR = None
_RERANK_ERR_AT = 0.0
_RERANK_NAME = None


def get_reranker():
    """Stage-2 scorer: cached bge-reranker-base Cross-Encoder first, then a
    download with mirror auto-retry (bounded), then the local bge-large-en bi-encoder.

    失败同样按 MODEL_RETRY_SECS 重试（issue #2）。"""
    global _RERANKER, _RERANK_ERR, _RERANK_ERR_AT, _RERANK_NAME
    if _RERANKER is not None:
        return _RERANKER
    if _RERANK_ERR is not None and not _retry_due(_RERANK_ERR_AT):
        return None
    name = os.environ.get("KB_RERANK_MODEL", "BAAI/bge-reranker-base")
    try:  # already cached locally?
        from sentence_transformers import CrossEncoder
        _RERANKER, fell_back = _load_with_cpu_fallback(
            "rerank", lambda **dkw: CrossEncoder(name, local_files_only=True, **dkw))
        _RERANK_NAME = name
        _RERANK_ERR = None
        if not fell_back:
            _set_device_note("rerank", None)
        return _RERANKER
    except Exception as e0:
        if _is_cuda_error(e0):
            _RERANK_ERR = f"cross-encoder: {str(e0)[:120]}"
            _RERANK_ERR_AT = time.time()
            return None
    try:  # bounded download attempt; direct first, then auto-retry via the CN mirror
        os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "120")
        from sentence_transformers import CrossEncoder

        def _build(**dkw):
            try:
                return CrossEncoder(name, **dkw)
            except Exception as e:
                if _is_cuda_error(e):
                    raise                     # 设备问题：不要在同一条路上连撞两次
                _apply_hf_mirror()
                return CrossEncoder(name, **dkw)

        _RERANKER, fell_back = _load_with_cpu_fallback("rerank", _build)
        _RERANK_NAME = name
        _RERANK_ERR = None
        if not fell_back:
            _set_device_note("rerank", None)
        return _RERANKER
    except Exception as e1:
        _RERANK_ERR = f"cross-encoder: {str(e1)[:120]}"
        _RERANK_ERR_AT = time.time()
    try:  # offline fallback: large bi-encoder re-scoring
        from sentence_transformers import SentenceTransformer
        _RERANKER, fell_back = _load_with_cpu_fallback(
            "rerank", lambda **dkw: SentenceTransformer("BAAI/bge-large-en-v1.5",
                                                        local_files_only=True, **dkw))
        _RERANK_NAME = "BAAI/bge-large-en-v1.5 (bi-encoder)"
        if not fell_back:
            _set_device_note("rerank", None)
        return _RERANKER
    except Exception as e2:
        _RERANK_ERR = f"{_RERANK_ERR}; bi-encoder: {str(e2)[:120]}"
        _RERANK_ERR_AT = time.time()
    return None


def rerank(query, texts):
    """Scores (query, text) pairs; returns (scores list, model name).

    批大小按设备自动选（`KB_RERANK_BATCH` 可覆盖，GPU 上 16 偏小）；OOM 处理同 encode()。"""
    model = get_reranker()
    if model is None:
        raise RuntimeError("reranker unavailable: " + (_RERANK_ERR or "unknown"))
    import numpy as np
    cross = bool(_RERANK_NAME and "bi-encoder" not in _RERANK_NAME)

    def _run(bs):
        if cross:
            pairs = [[query, t[:KB_RERANK_CHARS]] for t in texts]
            scores = model.predict(pairs, batch_size=bs, show_progress_bar=False)
            return np.asarray(scores, dtype="float32").flatten().tolist(), _RERANK_NAME
        q = model.encode([query], normalize_embeddings=True)
        d = model.encode([t[:KB_RERANK_CHARS] for t in texts], normalize_embeddings=True)
        return (d @ q.T).flatten().tolist(), _RERANK_NAME

    return _with_device_retry("rerank", model, _run)


# ---------------------------------------------------------------- ingest


def _count_candidates(paths, limit=None):
    """轻量统计待处理文件数（只看扩展名，不读内容）。limit 用于提前退出。"""
    n = 0
    for p in paths or []:
        p = Path(p)
        try:
            if p.is_dir():
                for f in p.rglob("*"):
                    if f.is_file() and f.suffix.lower() in SUPPORTED_EXTS:
                        n += 1
                        if limit and n >= limit:
                            return n
            elif p.is_file() and p.suffix.lower() in SUPPORTED_EXTS:
                n += 1
                if limit and n >= limit:
                    return n
        except OSError:
            continue
    return n


def cmd_ingest(req):
    _REL_CENTROID.clear()
    t0 = time.time()
    kb_root = req.get("kb_root") or ".kb"
    force = bool(req.get("force"))
    metadata_only = bool(req.get("metadata_only"))   # 只刷新元数据：不重切块、不重嵌入
    rebuild = bool(req.get("rebuild"))               # 按库内现有路径原地重灌全部文档
    paths = req.get("paths") or []
    progress_path = req.get("progress_path")   # 异步任务专用：后台进程逐文件回写进度
    db = connect(kb_root)
    if rebuild and not paths:
        # 原地重灌：从库里取现有路径，而不是让调用方传目录——force 会绕过去重检测
        # （见 _ingest_file 的 `not force` 分支），传目录会把内容重复的文件重复入库。
        paths = [r["path"] for r in db.execute("SELECT path FROM docs ORDER BY id").fetchall()]
        force = True
    if not paths:
        db.close()
        return {"ok": False,
                "error": "paths is required（或用 rebuild=true 重灌库内全部已入库文档）"}
    # 大批量自动转后台。计数放在引擎里完成，宿主无需访问文件系统：
    # - progress_path 非空 = 本进程就是那个后台任务，绝不再 fork（防递归）；
    # - metadata_only 不转后台：它是秒级操作（约 90 ms/篇，312 篇约 30 s），同步返回能让调用方
    #   直接拿到 meta_updated 统计；
    # - rebuild 用**库内篇数**作为待处理量（不能按目录数文件：rebuild 不传 paths）。全量重灌是
    #   分钟级操作，必须转后台，否则会长时间占住守护进程的请求队列（后续工具调用全部排队）。
    if req.get("async_if_large") and not progress_path and not metadata_only:
        try:
            threshold = int(os.environ.get("KB_ASYNC_THRESHOLD", "25"))
        except ValueError:
            threshold = 25
        pending = len(paths) if rebuild else _count_candidates(paths, limit=threshold + 1)
        if pending > threshold:
            # 限流计数在"阈值+1"处提前退出（只为判断是否超标）。这个数字会出现在提示里，
            # 显示成 26 而实际 100 篇会误导用户，所以超标后再完整数一次（目录扫描很便宜）。
            if not rebuild and pending == threshold + 1:
                pending = _count_candidates(paths)
            db.close()
            sub = dict(req)
            sub.pop("async_if_large", None)
            sub["command"] = "ingest"
            resp = cmd_ingest_async(sub)
            if isinstance(resp, dict):
                resp["background"] = True
                resp["pending_files"] = pending
            return resp
    files = []
    totals = {"added": 0, "updated": 0, "skipped": 0, "errors": 0, "duplicates": 0,
              "chunks": 0, "vectors": 0,
              "meta_updated": 0, "meta_changed": 0, "changed": 0, "not_indexed": 0}
    processed = 0
    vectors_missing = 0   # 全库"有分块但没向量"的块数：向量链路没跑通的直接证据（issue #2）

    def _prog():
        if progress_path:
            try:
                # 原子写：临时文件 + rename，避免轮询方读到半截 JSON
                tmp = progress_path + ".tmp"
                Path(tmp).write_text(json.dumps(
                    {"status": "running", "processed": processed,
                     "errors": totals["errors"], "chunks": totals["chunks"]},
                    ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, progress_path)
            except Exception:
                pass

    try:
        for p in paths:
            p = Path(p)
            if not p.exists():
                files.append({"path": str(p), "status": "error", "error": "not found"})
                totals["errors"] += 1
                processed += 1
                _prog()
                continue
            candidates = sorted(p.rglob("*")) if p.is_dir() else [p]
            for f in candidates:
                if not f.is_file() or f.suffix.lower() not in SUPPORTED_EXTS:
                    continue
                if metadata_only:
                    _refresh_meta_file(db, f, files, totals)
                else:
                    _ingest_file(db, f, force, files, totals)
                processed += 1
                # 逐文件 commit：把 SQLite 写锁窗口从"整批"缩到"单文件+嵌入"，
                # 避免异步入库运行期间同库的一切命令（含只读）在 connect 处撞锁
                # （子进程首个 INSERT 即开事务，原实现到全部文件处理完才 commit）。
                # 部分成功是合理语义：files[] 逐条带 status，异常时未 commit 文件自然回滚。
                db.commit()
                _prog()
        if totals["added"] or totals["updated"] or totals["meta_changed"]:
            db.execute("DELETE FROM cache")  # 索引或元数据变化都让查询缓存失效
        vectors_missing = db.execute(
            "SELECT COUNT(*) AS n FROM chunks c LEFT JOIN vecs v ON v.chunk_id = c.id "
            "WHERE v.chunk_id IS NULL AND c.weight > 0").fetchone()["n"]
        db.commit()
    finally:
        db.close()
    emb = get_embedder()
    resp = {
        "ok": True,
        "kb_root": str(Path(kb_root).resolve()),
        # 大批量入库时 files 可能很大（数百条 × 每条 ~150B），响应只回最近 20 条以压缩 JSON
        # （Kimi Work 等 MCP 宿主有 60s/体积限制；完整统计在 totals，files_total 为真实总数）
        "files": files[-20:],
        "files_total": len(files),
        "totals": totals,
        "mode": "metadata_only" if metadata_only else ("rebuild" if rebuild else "ingest"),
        "indexed_with": PARSER_TOKEN,
        "embedding": _EMBED_NAME if emb is not None else None,
        # 向量链路不可用必须显式说明：以前只回 embedding=null，渲染层只剩"N 块 / 0 向量"，
        # 用户既不知道原因、也不知道检索已经降级（issue #2）。
        "embedding_error": None if emb is not None else (_EMBED_ERR or "embedding model unavailable"),
        "vectors_missing": vectors_missing,
        "retry_secs": MODEL_RETRY_SECS,
        # 现在跑在 CPU 还是 GPU、torch 是不是 CUDA 版、有没有 OOM 回退过 —— 一次入库就能看清
        "device": device_report(),
        "ms": round((time.time() - t0) * 1000),
    }
    return resp


def _refresh_meta_file(db, f, files, totals):
    """只刷新元数据（`metadata_only`）：重跑解析与 extract_meta，UPDATE docs 的元数据字段
    与 indexed_with，**不重切块、不重嵌入**（无需模型，实测约 90 ms/篇；312 篇约 30 s）。

    存在的理由：增量入库按 sha256 跳过未变文件，所以引擎改进元数据抽取后老库不会自愈；
    本函数提供一条秒级、可反复执行的刷新通道。内容已变的文件不动（记 changed），
    因为元数据必须与已入库的正文一致——那属于真正的入库。"""
    t0 = time.time()
    entry = {"path": str(f)}
    try:
        data = f.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        key = str(f.resolve())
        row = db.execute("SELECT id, sha256, title, doi FROM docs WHERE path = ?",
                         (key,)).fetchone()
        if row is None:
            entry.update({"status": "not_indexed", "ms": round((time.time() - t0) * 1000)})
            totals["not_indexed"] += 1
            files.append(entry)
            return
        if row["sha256"] != sha:
            entry.update({"status": "changed",
                          "note": "文件内容已变，元数据刷新跳过（请用 kb_ingest 重入库）",
                          "ms": round((time.time() - t0) * 1000)})
            totals["changed"] += 1
            files.append(entry)
            return
        text, pdf_meta = read_first_page(f)   # 只读首页：元数据判据全在 page1 + pdf_meta 上
        if not text.strip():
            raise ValueError("no text extracted")
        title, authors, year, journal, doi = extract_meta(f, text, pdf_meta)
        # 绝不用**空值**覆盖已有的非空值：Zotero 迁移写入的 doi/journal/zotero_key 本来就不在
        # PDF 首页里，无条件覆盖会把它们抹掉（实测丢过 2 篇 DOI）。用非空的新值替换旧值仍然允许，
        # 那正是刷新通道的用途（修正 ".indd" 这类脏标题）。
        prev = db.execute("SELECT title,authors,year,journal,doi FROM docs WHERE id=?",
                          (row["id"],)).fetchone()

        def keep(new, old):
            return old if (new is None or (isinstance(new, str) and not new.strip())) else new

        title = keep(title, prev["title"])
        authors = keep(authors, prev["authors"])
        year = keep(year, prev["year"])
        journal = keep(journal, prev["journal"])
        doi = keep(doi, prev["doi"])
        prev_doi = prev["doi"] or None
        changed = (title != prev["title"]) or ((doi or None) != prev_doi)
        db.execute("UPDATE docs SET title=?,authors=?,year=?,journal=?,doi=?,indexed_with=? "
                   "WHERE id=?",
                   (title, authors, year, journal, doi, PARSER_TOKEN, row["id"]))
        totals["meta_updated"] += 1
        if changed:
            totals["meta_changed"] += 1
        entry.update({"status": "meta_updated", "changed": changed, "title": title,
                      "year": year, "doi": doi, "ms": round((time.time() - t0) * 1000)})
    except Exception as e:  # 单篇失败不能中断整批
        totals["errors"] += 1
        entry.update({"status": "error", "error": f"{type(e).__name__}: {e}"[:300],
                      "ms": round((time.time() - t0) * 1000)})
    files.append(entry)


def _embed_new_chunks(db, doc_id):
    """Embed chunks that have no vector yet; returns count (0 when model absent)."""
    if get_embedder() is None:
        return 0
    rows = db.execute(
        "SELECT id, text FROM chunks WHERE doc_id = ? AND weight > 0 AND id NOT IN "
        "(SELECT chunk_id FROM vecs)", (doc_id,)).fetchall()
    if not rows:
        return 0
    vecs = encode([r["text"] for r in rows])
    db.executemany("INSERT OR REPLACE INTO vecs(chunk_id, vec) VALUES(?, ?)",
                   [(r["id"], pack_vec(v)) for r, v in zip(rows, vecs)])
    return len(rows)


def _ingest_file(db, f, force, files, totals, meta=None):
    t0 = time.time()
    entry = {"path": str(f)}
    try:
        data = f.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        key = str(f.resolve())
        row = db.execute("SELECT id, sha256 FROM docs WHERE path = ?", (key,)).fetchone()
        if row is not None and row["sha256"] == sha and not force:
            n = db.execute(
                "SELECT COUNT(*) AS n FROM chunks WHERE doc_id = ?", (row["id"],)).fetchone()["n"]
            # 补齐历史缺失的向量：模型不可用时入库不会建向量，而增量入库会跳过内容未变的文件，
            # 所以"修好环境后重跑 kb_ingest"以前永远补不回来（只能 rebuild=true 全量重灌，
            # 312 篇约 322 s）——见 issue #2。向量齐全时这只是一条 SELECT，代价可忽略。
            n_vec = _embed_new_chunks(db, row["id"])
            entry.update({"status": "skipped", "chunks": n, "vectors": n_vec,
                          "ms": round((time.time() - t0) * 1000)})
            totals["vectors"] += n_vec
            totals["skipped"] += 1
            files.append(entry)
            return
        dup = db.execute("SELECT id, path FROM docs WHERE sha256 = ? AND path != ?",
                         (sha, key)).fetchone()
        if dup is not None and not force:  # same content already indexed elsewhere
            entry.update({"status": "duplicate", "of": dup["path"],
                          "ms": round((time.time() - t0) * 1000)})
            totals["duplicates"] = totals.get("duplicates", 0) + 1
            files.append(entry)
            return
        text, pdf_meta = read_document(f)
        if not text.strip():
            raise ValueError("no text extracted")
        title, authors, year, journal, doi = extract_meta(f, text, pdf_meta)
        zotero_key = None
        if meta:  # authoritative metadata override (e.g. Zotero)
            title = meta.get("title") or title
            authors = meta.get("authors") or authors
            year = meta.get("year") or year
            journal = meta.get("journal") or journal
            doi = meta.get("doi") or doi
            zotero_key = meta.get("_zotero_key")
        paras = (pdf_meta or {}).get("_paras")          # [(PDF页码或None, 段文本)]
        chunks = chunk_document(text, paras=paras)
        seen, uniq = set(), []
        for sec, w, t, ps, pe, gs, ge in chunks:
            h = hashlib.sha1(t.encode("utf-8")).hexdigest()
            if h in seen:
                continue
            seen.add(h)
            uniq.append((sec, w, t, ps, pe, gs, ge))
        chunks = uniq
        st = f.stat()
        if row is not None:
            db.execute(
                "UPDATE docs SET title=?,authors=?,year=?,journal=?,doi=?,kind=?,"
                "sha256=?,size=?,mtime=?,chunk_count=?,indexed_at=?,zotero_key=?,"
                "indexed_with=? WHERE id=?",
                (title, authors, year, journal, doi, f.suffix.lower().lstrip("."),
                 sha, st.st_size, st.st_mtime, len(chunks), time.time(), zotero_key,
                 PARSER_TOKEN, row["id"]))
            doc_id = row["id"]
            db.execute("DELETE FROM vecs WHERE chunk_id IN "
                       "(SELECT id FROM chunks WHERE doc_id = ?)", (doc_id,))
            db.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
            status = "updated"
        else:
            cur = db.execute(
                "INSERT INTO docs(path,title,authors,year,journal,doi,kind,sha256,"
                "size,mtime,chunk_count,indexed_at,zotero_key,indexed_with) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (key, title, authors, year, journal, doi, f.suffix.lower().lstrip("."),
                 sha, st.st_size, st.st_mtime, len(chunks), time.time(), zotero_key,
                 PARSER_TOKEN))
            doc_id = cur.lastrowid
            status = "added"
        db.executemany(
            "INSERT INTO chunks(doc_id,section,weight,seq,text,para_start,para_end,page_start,page_end) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            [(doc_id, sec, w, i, t, ps, pe, gs, ge)
             for i, (sec, w, t, ps, pe, gs, ge) in enumerate(chunks)])
        n_vec = _embed_new_chunks(db, doc_id)
        totals[status] += 1
        totals["chunks"] += len(chunks)
        totals["vectors"] += n_vec
        entry.update({"status": status, "chunks": len(chunks), "vectors": n_vec,
                      "title": title, "year": year,
                      "ms": round((time.time() - t0) * 1000)})
    except Exception as e:  # per-file failure must not kill the batch
        totals["errors"] += 1
        entry.update({"status": "error", "error": f"{type(e).__name__}: {e}"[:300],
                      "ms": round((time.time() - t0) * 1000)})
    files.append(entry)


# ---------------------------------------------------------------- search

STOP = {"the", "a", "an", "of", "and", "or", "in", "on", "for", "with", "is",
        "are", "was", "were", "be", "to", "by", "from", "as", "at", "that",
        "this", "these", "those", "we", "they", "it", "et", "al", "their", "its"}

FILTER_COLS = {
    "authors": "d.authors", "title": "d.title", "journal": "d.journal",
    "kind": "d.kind", "year": "d.year", "section": "c.section",
}

RRF_K = 60


def extract_terms(query):
    """CJK phrases + bigrams, plus ASCII words (numbers, formulas, terms)."""
    terms, seen = [], set()

    def add(term, kind, w):
        if (term, kind) in seen:
            return
        seen.add((term, kind))
        terms.append((term, kind, w))

    for run in re.findall(r"[\u4e00-\u9fff]+", query):
        r = run.lower()
        add(r, "phrase", 2.0 if len(r) <= 4 else 1.5)
        if len(r) >= 2:
            for i in range(len(r) - 1):
                add(r[i:i + 2], "word", 1.0)
    for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-+.#%]*", query):
        w = w.lower().strip(".-")
        if len(w) < 2 or w in STOP:
            continue
        add(w, "word", 1.0)
    return terms


def _norm_filter_text(s):
    """filters 文本归一化：连字符/下划线/连续空格 → 单空格，并小写。

    实测现场：标题 'Electric-field control of local ferromagnetism' 用不同写法查过 3 遍
    （含 2 次零命中）—— 库里存的是连字符版，查询里是空格版。SQLite 的 LIKE 对 ASCII 本来
    就大小写不敏感，真正的坑是连字符/下划线/空格的不一致。"""
    return re.sub(r"\s+", " ", re.sub(r"[\-_]+", " ", (s or "").strip().lower()))


def _sq_norm(expr):
    """SQL 侧的同一套归一化（列名由内部常量给出，不含用户输入，无注入面）。"""
    return ("lower(replace(replace(replace(replace(replace(%s,'-',' '),'_',' '),"
            "'  ',' '),'   ',' '),'    ',' '))" % expr)


_NORM_SQL = {"title": _sq_norm("d.title"), "authors": _sq_norm("d.authors"),
             "journal": _sq_norm("d.journal")}


def build_where(filters):
    where, args = [], []
    for key, col in FILTER_COLS.items():
        v = filters.get(key)
        if v is None or v == "":
            continue
        if key == "year":
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                where.append(f"{col} = ?")
                args.append(int(v))
            elif isinstance(v, str):
                m = re.match(r"^\s*(>=|<=|>|<|=)?\s*(\d{4})\s*$", v)
                if m:
                    where.append(f"{col} {m.group(1) or '='} ?")
                    args.append(int(m.group(2)))
            continue
        if key == "authors":
            # 作者按**分词 AND**匹配："Smith J" 要能命中库里存的 "Smith, J.; Jones, B."
            # （整串子串匹配在标点/顺序不同时必然零命中——实测穷举 author filter 是 agent
            # "来回找"的典型原因之一）
            for tok in [t for t in _norm_filter_text(v).split(" ") if t]:
                where.append(f"{_NORM_SQL['authors']} LIKE ?")
                args.append("%" + tok + "%")
            continue
        expr = _NORM_SQL.get(key, col)
        where.append(f"{expr} LIKE ?")
        args.append("%" + _norm_filter_text(v) + "%")
    return (" WHERE " + " AND ".join(where)) if where else "", args


def make_snippet(text, term, width):
    if term is None or term not in text.lower():
        return text[:width] + ("…" if len(text) > width else "")
    i = text.lower().find(term)
    start = max(0, i - width // 3)
    if start > 0:
        # 对齐到词边界起截：避免 "…ng1, Emma" 这类半个名字/单词开头
        ws = text.find(" ", start)
        if 0 <= ws <= start + 40:
            start = ws + 1
    end = min(len(text), start + width)
    if end < len(text):
        we = text.rfind(" ", start, end)
        if we > start:
            end = we
    pre = "…" if start > 0 else ""
    post = "…" if end < len(text) else ""
    return pre + text[start:end].strip() + post


def keyword_ranking(rows, query):
    """BM25 x section weight over candidate chunks; returns ranked (index, score)."""
    terms = extract_terms(query)
    if not terms:
        return [], "无法从 query 解析出可检索的关键词"
    texts = [r["text"] for r in rows]
    lowered = [t.lower() for t in texts]
    n = len(rows)
    avgdl = sum(len(t) for t in texts) / max(1, n)
    k1, b = 1.2, 0.75
    scores = [0.0] * n
    best_term = [None] * n
    best_idf = [0.0] * n
    for term, _kind, tw in terms:
        df = sum(1 for lt in lowered if term in lt)
        if df == 0:
            continue
        idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
        for i, lt in enumerate(lowered):
            tf = lt.count(term)
            if tf == 0:
                continue
            denom = tf + k1 * (1 - b + b * len(texts[i]) / avgdl)
            scores[i] += idf * tf * (k1 + 1) / denom * tw
            if idf > best_idf[i]:
                best_idf[i] = idf
                best_term[i] = term
    ranked = sorted(((i, scores[i] * rows[i]["weight"]) for i in range(n)
                     if scores[i] > 0), key=lambda x: x[1], reverse=True)
    return ranked, best_term, None


def vector_ranking(rows, qvec, k=30):
    """FAISS IndexFlatIP cosine ranking; returns ranked (index, score)."""
    import faiss
    import numpy as np
    idx_of = []
    mats = []
    for i, r in enumerate(rows):
        v = r["vec"]
        if v is None:
            continue
        arr = unpack_vec(v)
        if arr.shape[0] == 0:
            continue
        idx_of.append(i)
        mats.append(arr)
    if not mats:
        return [], "no vectors indexed (embedding model unavailable at ingest time)"
    index = faiss.IndexFlatIP(mats[0].shape[0])
    index.add(np.vstack(mats))
    k = min(k, len(mats))
    scores, ids = index.search(np.asarray([qvec], dtype="float32"), k)
    ranked = [(idx_of[int(ids[0][j])], float(scores[0][j]))
              for j in range(k) if ids[0][j] >= 0]
    return ranked, None


def rrf_fuse(kw_ranked, v_ranked):
    fused = {}
    for rank, (i, _score) in enumerate(kw_ranked):
        fused[i] = fused.get(i, 0.0) + 1.0 / (RRF_K + rank + 1)
    for rank, (i, _score) in enumerate(v_ranked):
        fused[i] = fused.get(i, 0.0) + 1.0 / (RRF_K + rank + 1)
    return sorted(fused.items(), key=lambda x: x[1], reverse=True)


# ------------------------------------------------------- caption association

_FIGREF_RE = re.compile(r"(?:fig(?:ure|s)?\.?\s*|图\s*)(\d+)([a-zA-Z])?(?!\d)", re.I)
_CAPTION_NUM_RE = re.compile(r"\d+")
_INCITE_RE = re.compile(
    r"\[(\d{1,3}(?:\s*[–\-]\s*\d{1,3})?(?:\s*,\s*\d{1,3}(?:\s*[–\-]\s*\d{1,3})?)*)\]")
_REF_ENTRY_RE = re.compile(r"(?m)^\s*(\d{1,3})\s*(?:[\.\)]\s+|\s+)(?=\S)")
# 紧贴式条目（'1Smith J., Nature…'）：数字后**紧跟一个"大写+小写"的词**才算条目。
# 旧写法是 (?=[A-Z])，于是正文里的 '2D materials' / '3D printing' / '4H-SiC' / '3C-SiC'
# 也被当成条目编号 —— 这是 References 误判吞正文的一个真实来源。
_REF_ENTRY_TIGHT_RE = re.compile(r"(?m)^\s*(\d{1,3})(?=[A-Z][a-z])")
_CITE_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s,;\"'<>\)\]]+", re.I)
_CITE_YEAR_RE = re.compile(r"\((\d{4})\)\s*[.\s]*$")


def _expand_incite_nums(group, cap=60):
    """"1", "1-3", "1,2,5", "1–3,7" -> 引文编号展开列表。"""
    nums = []
    for part in group.split(","):
        m = re.match(r"\s*(\d{1,3})(?:\s*[–\-]\s*(\d{1,3}))?\s*$", part)
        if not m:
            continue
        a = int(m.group(1))
        b = int(m.group(2) or m.group(1))
        if b < a:
            b = a
        if b > a + 50:
            b = a + 50
        nums.extend(range(a, b + 1))
    return nums[:cap]


def _doc_references(db, doc_id, cache=None):
    """该文献 References 分块拼接文本（引文关联数据源）；带 per-search 缓存。"""
    key = ("refs", doc_id)
    if cache is not None and key in cache:
        return cache[key]
    parts = [r["text"] for r in db.execute(
        "SELECT text FROM chunks WHERE doc_id = ? AND section = 'References' ORDER BY seq",
        (doc_id,)).fetchall()]
    text = "\n".join(parts)
    if cache is not None:
        cache[key] = text
    return text


def _parse_references(text, cap=400):
    """References 文本 -> {编号: 引文文本}。支持 '1. ' / '1 ' / '[1] ' / '1Author' 四种风格。

    多种风格同时命中条目时，取"编号链最完整"的模式（含 1 且前后相连的编号最多）——
    避免换行噪声风格（如年被拆行后 '10 Domains ...' 行首）劫持真实条目。"""
    if not text:
        return {}
    best, best_score = {}, -1
    for pat in (_REF_ENTRY_RE, _REF_ENTRY_BRACKET_RE, _REF_ENTRY_TIGHT_RE):
        ms = list(pat.finditer(text))
        if len(ms) < 2 and len(text) > 200:
            continue  # 单条匹配不可靠，跳过该模式
        refs = {}
        for i, m in enumerate(ms):
            end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
            body = text[m.end():end].strip()
            body = re.sub(r"\s+", " ", body)
            if body:
                refs[int(m.group(1))] = body[:cap]
        if not refs:
            continue
        keys = set(refs)
        score = (1 if 1 in keys else 0) + sum(1 for kk in keys if kk - 1 in keys)
        if score > best_score:
            best, best_score = refs, score
    return best


def _cited_refs(db, doc_id, chunk_text, cache):
    """取本块正文引用的 [n] 对应参考文献条目（含范围/逗号列表展开），供引文关联建议使用。

    条目里若带 DOI 就一并给出（`doi` 字段）：agent 可以直接 kb_fetch 把它拉进库，
    这是"库内只有一两篇 → 循引文补库"这条深挖路径的关键一步。"""
    refs = _parse_references(_doc_references(db, doc_id, cache))
    if not refs:
        return []
    nums = []
    for m in _INCITE_RE.finditer(chunk_text or ""):
        nums.extend(_expand_incite_nums(m.group(1)))
    out, seen = [], set()
    for n in nums:
        if n in refs and n not in seen:
            seen.add(n)
            m = _REF_DOI_RE.search(refs[n])
            out.append({"n": n, "text": refs[n], "doi": m.group(0) if m else None})
        if len(out) >= 8:
            break
    _annotate_lib_matches(db, doc_id, out, cache)
    return out


def _lib_docs(db, cache=None):
    """库内全部文献的轻量元数据行（引文关联库内匹配的数据源）；带 per-search 缓存。"""
    if cache is not None and "libdocs" in cache:
        return cache["libdocs"]
    rows = db.execute(
        "SELECT id, title, authors, year, journal, doi, zotero_key FROM docs").fetchall()
    rows = [dict(r) for r in rows]
    if cache is not None:
        cache["libdocs"] = rows
    return rows


def _cite_norm(s):
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (s or "").lower())


def _match_cite_lib(cite_text, libdocs):
    """引文条目文本 -> 库内文献（宁缺勿错）。

    三级匹配：① 引文文本里的 DOI 精确命中；② 库内标题（归一化后 ≥30 字符）
    整串出现在引文文本里；③ 首作者姓（≥6 字符，排除 Wang/Li 类大姓）+ 括号年份
    双命中。命中即返回 doc dict，否则 None。"""
    if not cite_text:
        return None
    for doi in _CITE_DOI_RE.findall(cite_text):
        doi_n = doi.rstrip(".,;").lower()
        for d in libdocs:
            if d.get("doi") and d["doi"].lower() == doi_n:
                return d
    ct = _cite_norm(cite_text)
    for d in libdocs:
        dt = _cite_norm(d.get("title"))
        if len(dt) >= 30 and dt in ct:
            return d
    first_seg = re.split(r"[,;]", cite_text.strip(), 1)[0]
    mw = re.search(r"[A-Za-z\u4e00-\u9fff]{3,}", first_seg)  # 跳过缩写/序号，取首作者姓
    my = _CITE_YEAR_RE.search(cite_text.strip())
    if mw and len(mw.group(0)) >= 6 and my:
        surname = mw.group(0)
        year = int(my.group(1))
        for d in libdocs:
            if d.get("year") == year and surname.lower() in (d.get("authors") or "").lower():
                return d
    return None


def _annotate_lib_matches(db, doc_id, cites, cache):
    """引文关联深挖：被引条目若命中库内文献，标注 lib 字段，写出"谁引谁"的关系数据。

    渲染层据此显示引文关联：本证据文献的引文 [n] == 库内某篇文献（可直接检索/
    引用/打开），即 <证据文献> --引用[n]--> <库内文献>。"""
    if not cites:
        return
    try:
        lib = _lib_docs(db, cache)
    except Exception:
        return
    if not lib:
        return
    for c in cites:
        d = _match_cite_lib(c.get("text"), lib)
        if d and d["id"] != doc_id:
            c["lib"] = {"id": d["id"], "title": d["title"], "authors": d["authors"],
                        "year": d["year"], "journal": d["journal"], "doi": d["doi"],
                        "zotero_key": d.get("zotero_key") or None}


def _doc_captions(db, doc_id):
    """{figure_number: caption_text} for one doc, from 'Figure/Table' chunks."""
    caps = {}
    for r in db.execute(
            "SELECT text FROM chunks WHERE doc_id = ? AND section = 'Figure/Table'",
            (doc_id,)).fetchall():
        m = CAPTION_RE.match(r["text"])
        if not m:
            continue
        n = _CAPTION_NUM_RE.search(m.group(1))
        if n:
            caps[n.group(0)] = r["text"]
    return caps


# ------------------------------------------------------- related literature
# (kb_root, doc_id) -> centroid vector (float32 array); cleared on any ingest mutation.
_REL_CENTROID = {}


def _author_tokens(authors):
    return {t.lower() for t in re.split(r"[;,/&]+", authors or "") if t.strip() and
            len(t.strip()) > 1 and not t.strip().lower() in ("et al", "al", "and")}


def _doc_centroid(db, kb_key, doc_id):
    key = (kb_key, doc_id)
    if key in _REL_CENTROID:
        return _REL_CENTROID[key]
    rows = db.execute(
        "SELECT v.vec FROM chunks c JOIN vecs v ON v.chunk_id = c.id "
        "WHERE c.doc_id = ?", (doc_id,)).fetchall()
    mats = []
    for r in rows:
        arr = unpack_vec(r["vec"])
        if arr.shape[0] > 0:
            mats.append(arr)
    if not mats:
        return None
    import numpy as np
    c = np.vstack(mats).mean(axis=0).astype("float32")
    _REL_CENTROID[key] = c
    return c


def related_docs(db, kb_key, seed_doc_ids, related_k=5):
    """Metadata + centroid-similarity association over the docs not already in results."""
    if not seed_doc_ids:
        return []
    import numpy as np
    seeds = []
    for did in seed_doc_ids:
        c = _doc_centroid(db, kb_key, did)
        if c is not None:
            seeds.append(c)
    seed_c = np.vstack(seeds).mean(axis=0) if seeds else None
    seed_meta = {}
    for r in db.execute(
            "SELECT id, authors, journal, year FROM docs WHERE id IN (%s)"
            % ",".join("?" * len(seed_doc_ids)), list(seed_doc_ids)).fetchall():
        seed_meta[r["id"]] = r
    seed_authors = set()
    seed_journals = set()
    seed_years = []
    for sm in seed_meta.values():
        seed_authors |= _author_tokens(sm["authors"])
        if sm["journal"]:
            seed_journals.add(sm["journal"].strip().lower())
        try:
            seed_years.append(int(sm["year"]))
        except (TypeError, ValueError):
            pass
    cands = db.execute(
        "SELECT id, title, authors, year, journal, doi, path, zotero_key FROM docs "
        "WHERE id NOT IN (%s)" % ",".join("?" * len(seed_doc_ids)),
        list(seed_doc_ids)).fetchall()
    scored = []
    for c in cands:
        meta = 0.0
        reasons = []
        shared = _author_tokens(c["authors"]) & seed_authors
        if shared:
            meta += 2.0
            reasons.append("同作者")
        if c["journal"] and c["journal"].strip().lower() in seed_journals:
            meta += 1.5
            reasons.append("同期刊")
        try:
            cy = int(c["year"])
            if seed_years:
                dist = min(abs(cy - sy) for sy in seed_years)
                meta += max(0.0, 1.0 - dist / 10.0)
                if dist <= 3:
                    reasons.append("年份相近")
        except (TypeError, ValueError):
            pass
        vec = 0.0
        if seed_c is not None:
            cc = _doc_centroid(db, kb_key, c["id"])
            if cc is not None:
                vec = float(np.dot(seed_c, cc) / (np.linalg.norm(seed_c) * np.linalg.norm(cc) + 1e-9)) * 2.0
                if vec > 0.7:
                    reasons.append("主题相似")
        score = round(meta + vec, 4)
        if score <= 0:
            continue
        scored.append({
            "file": Path(c["path"]).name,
            "path": c["path"],
            "title": c["title"],
            "authors": c["authors"],
            "year": c["year"],
            "journal": c["journal"],
            "doi": c["doi"],
            "zotero_key": c["zotero_key"] or None,
            "score": score,
            "reason": "、".join(reasons[:2]) if reasons else "内容相关",
        })
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:related_k]


_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_CJK_SHARE_CACHE = {}   # {db 路径: 中文占比}，按守护进程生命周期缓存

# ---------------------------------------------------------------- 相关性地板
#
# 目的：库外问题不该返回 Top-3 垃圾（那会让模型"看起来有据可依"）。
# 阈值来自**本机真实库实测**（316 篇、12 个库内问题 vs 8 个库外问题）：
#   裸精排分：库内 min 0.685 / 中位 0.996 ｜ 库外 max 0.072 / 中位 0.020 → 空隙极大，
#             0.05–0.35 之间的任何阈值在这批样本上都零误判；默认取 0.10（偏保守，
#             宁可漏判"无关"也不误杀弱命中）。
#   最高余弦：库内 min 0.691 ｜ 库外 max 0.688 → **基本重叠**。所以纯向量 / 纯关键词路
#             **不做**"无关"判定（verdict=null）：拿余弦当地板只是在刀尖上赌。
# 引擎返回 verdict（相关 / 弱相关 / 无关）+ no_hit + max_score + closest。
KB_MIN_RERANK = _env_float("KB_MIN_RERANK", 0.10)        # 裸精排分地板
KB_MIN_RERANK_WEAK = _env_float("KB_MIN_RERANK_WEAK", 0.35)   # 低于此值算"弱相关"
KB_RERANK_POOL = int(_env_float("KB_RERANK_POOL", 20))   # 精排候选池下限（top_k*5 取大）
KB_RERANK_CHARS = int(_env_float("KB_RERANK_CHARS", 1800))    # 每条候选喂给精排的字符数
# 缓存 key 里用**配置名**而不是运行时的 _RERANK_NAME：后者在模型加载前是 None、加载后变成
# 模型名，于是"某进程第一次 deep 检索"写下的缓存行永远命中不了（key 已经变了）。
RERANK_KEY_NAME = os.environ.get("KB_RERANK_MODEL", "BAAI/bge-reranker-base")


def _library_cjk_share(db):
    """库内正文的中文占比（抽样估算，按库缓存一次）。

    用途：判断"中文 query 在这库里是否吃亏"——BM25 只对 CJK 二元组建索引，若库内正文
    几乎全英文，中文查询的关键词那一路等于空转，只能靠向量侧跨语言匹配，命中会明显变差。
    采样 400 个可检索分块，成本可忽略。"""
    try:
        key = str(db.execute("PRAGMA database_list").fetchall()[0][2] or "kb")
    except Exception:
        key = "kb"
    if key in _CJK_SHARE_CACHE:
        return _CJK_SHARE_CACHE[key]
    share = 0.0
    try:
        rows = db.execute("SELECT text FROM chunks WHERE weight > 0 LIMIT 400").fetchall()
        tot = cjk = 0
        for r in rows:
            t = r["text"] or ""
            tot += len(t)
            cjk += len(_CJK_RE.findall(t))
        if tot:
            share = cjk / tot
    except Exception:
        share = 0.0
    _CJK_SHARE_CACHE[key] = share
    return share


def _search_core(db, query, top_k, snippet_w, filters, mode, use_cache, rerank_flag=True,
                  related_flag=True, related_k=5):
    t0 = time.time()
    where, args = build_where(filters)
    # References（weight 0）只作引文关联数据源，不参与检索
    where = (where + " AND c.weight > 0") if where else " WHERE c.weight > 0"
    rows = db.execute(
        "SELECT c.id AS cid, c.doc_id, c.text, c.section, c.weight, "
        "c.para_start, c.para_end, c.page_start, c.page_end, d.title, d.authors, "
        "d.year, d.journal, d.doi, d.path, d.kind, d.zotero_key, v.vec "
        "FROM chunks c JOIN docs d ON d.id = c.doc_id "
        "LEFT JOIN vecs v ON v.chunk_id = c.id" + where, args).fetchall()

    if not rows:
        return {"query": query, "scored": 0, "results": [],
                "note": "知识库为空或过滤条件过严。先用 kb_ingest 入库，或放宽 filters。",
                "ms": round((time.time() - t0) * 1000), "cached": False}

    # background zh->en translation is intentionally NOT wired in this build;
    # queries are searched as-is (CJK bigrams + ASCII terms both participate).

    cache_key = None
    if use_cache:
        cache_key = hashlib.sha1(json.dumps(
            [query, filters, top_k, snippet_w, mode, rerank_flag, RERANK_KEY_NAME,
             related_flag, related_k,
             # 地板/精排参数会改变结论（verdict / 截断长度影响精排分），必须并入 key，
             # 否则改了阈值仍会复用旧响应（与"device=null 的旧缓存"同一类坑）
             KB_MIN_RERANK, KB_MIN_RERANK_WEAK, KB_RERANK_CHARS, KB_RERANK_POOL,
             # 把解析器/引擎版本并入 key：升级后旧的缓存响应不该继续被复用
             PARSER_TOKEN],
            sort_keys=True, ensure_ascii=True, default=str).encode("utf-8")).hexdigest()
        hit = db.execute("SELECT payload FROM cache WHERE key = ?", (cache_key,)).fetchone()
        if hit is not None:
            data = json.loads(hit["payload"])
            data["cached"] = True
            data["ms"] = 0
            return data

    mode_used = mode or "hybrid"
    note = ""
    ranked = []
    best_term = None

    if mode_used in ("keyword", "hybrid"):
        kw_ranked, best_term, _err = keyword_ranking(rows, query)
        if mode_used == "keyword":
            ranked = kw_ranked
        else:
            if not kw_ranked:
                note += "关键词无命中；"
            vecs_present = any(r["vec"] is not None for r in rows)
            if not vecs_present:
                mode_used = "keyword"
                ranked = kw_ranked
                note += "向量索引缺失，降级为纯关键词；"
            else:
                try:
                    qvec = encode([query], is_query=True,
                                  cjk=bool(re.search(r"[\u4e00-\u9fff]", query)))[0]
                    v_ranked, _err2 = vector_ranking(rows, qvec, k=max(20, top_k * 5))
                    if _err2:
                        mode_used = "keyword"
                        ranked = kw_ranked
                        note += _err2 + "，降级为纯关键词；"
                    else:
                        fused = rrf_fuse(kw_ranked, v_ranked)
                        # 三元组 (候选下标, 加权分, **裸分**)：裸分必须一路带出来 ——
                        # 相关性地板只能按裸分判（加权分被章节权重乘过 1.0–1.5，量纲会错位）
                        ranked = [(i, s * rows[i]["weight"], s) for i, s in fused]
                except Exception as e:
                    mode_used = "keyword"
                    ranked = kw_ranked
                    note += f"向量检索失败({str(e)[:120]})，降级为纯关键词；"

    elif mode_used == "vector":
        try:
            qvec = encode([query], is_query=True,
                          cjk=bool(re.search(r"[\u4e00-\u9fff]", query)))[0]
            ranked, _err2 = vector_ranking(rows, qvec, k=max(20, top_k * 5))
            if _err2:
                mode_used = "keyword"
                ranked, best_term, _ = keyword_ranking(rows, query)
                note += _err2 + "，降级为纯关键词；"
            else:
                ranked = [(i, s, s) for i, s in v_ranked]     # 纯向量路：裸分 = 余弦
        except Exception as e:
            mode_used = "keyword"
            ranked, best_term, _ = keyword_ranking(rows, query)
            note += f"向量检索失败({str(e)[:120]})，降级为纯关键词；"
    else:
        return {"ok": False, "error": f"unknown mode: {mode_used}"}

    # 统一成三元组 (候选下标, 加权分, 裸分)：纯关键词路（含各种降级）的分本身就是裸分，
    # 三种路都必须带裸分，后面才能用同一条"相关性地板"判定。
    ranked = [(t[0], t[1], t[1]) if len(t) == 2 else t for t in ranked]

    # stage 2: rerank the fused pool with a stronger local scorer
    reranker_used = None
    if rerank_flag and len(ranked) > top_k:
        try:
            pool = ranked[:max(KB_RERANK_POOL, top_k * 5)]
            cand_idx = [t[0] for t in pool]
            texts = [rows[i]["text"] for i in cand_idx]
            rscores, reranker_used = rerank(query, texts)
            reranked = sorted(zip(cand_idx, rscores), key=lambda x: x[1], reverse=True)
            # 精排分是**裸分**（cross-encoder 输出），加权分照旧给排序/展示用
            ranked = [(i, s * rows[i]["weight"], s) for i, s in reranked]
        except Exception as e:
            note += f"精排不可用({str(e)[:100]})；"

    # ── 相关性地板（verdict）：只有精排分能干净分离库内/库外 ─────────────────────
    # 实测（本机 316 篇真实库，12 个库内问题 vs 8 个库外问题）：
    #   裸精排分：库内 min 0.685 / 中位 0.996 ｜ 库外 max 0.072 / 中位 0.020 → 空隙巨大
    #   最高余弦：库内 min 0.691 ｜ 库外 max 0.688 → **基本重叠**，任何余弦阈值都在刀尖上，
    #             所以纯向量/纯关键词路不做"无关"判定（verdict=null），只给分数让人判断。
    raw_best = max((t[2] for t in ranked), default=None)
    floor = KB_MIN_RERANK if reranker_used else None
    verdict = None
    no_hit = False
    if floor is not None and raw_best is not None:
        if raw_best < floor:
            verdict, no_hit = "无关", True
        elif raw_best < KB_MIN_RERANK_WEAK:
            verdict = "弱相关"
        else:
            verdict = "相关"
        note += "精排最高分 %.3f（地板 %.2f）；" % (raw_best, floor)
    closest = [{"title": rows[t[0]]["title"], "year": rows[t[0]]["year"],
                "section": rows[t[0]]["section"], "score": round(t[1], 4)}
               for t in ranked[:5]] if ranked else []
    if no_hit:
        # 无命中时给"最接近的 5 篇"：把"没有"变成可转述的信息，避免 agent 换词穷举
        note += "库内无相关资料；"

    results = []
    seed_doc_ids = []
    caption_cache = {}
    seen_docs = set()
    seen_dois = set()      # 同一论文的不同 PDF 版本（内容不同 → sha256 去重不合并）
    dup_collapsed = 0
    for i, score, _raw in ([] if no_hit else ranked):
        r = rows[i]
        if r["doc_id"] in seen_docs:
            continue  # 结果层去重：同一篇只保留最高分的一块，避免 Top-K 被同一篇占据
        seen_docs.add(r["doc_id"])
        # 论文级去重：两份 PDF 是不同的 doc（各自 sha256），会各占一个结果位；有 DOI 时按
        # 归一化 DOI（大小写不敏感）折叠，保留得分最高的一条，并从后续候选里补齐 Top-K。
        dkey = (r["doi"] or "").strip().lower().rstrip(".,;")
        if dkey:
            if dkey in seen_dois:
                dup_collapsed += 1
                continue
            seen_dois.add(dkey)
        entry = {
            "file": Path(r["path"]).name,
            "path": r["path"],
            "title": r["title"],
            "authors": r["authors"],
            "year": r["year"],
            "journal": r["journal"],
            "doi": r["doi"],
            "zotero_key": r["zotero_key"] or None,
            "section": r["section"],
            "para": [r["para_start"], r["para_end"]] if r["para_start"] is not None else None,
            "page": [r["page_start"], r["page_end"]] if r["page_start"] is not None else None,
            "score": round(score, 4),
            "snippet": make_snippet(r["text"], best_term[i] if best_term else None, snippet_w),
        }
        # 引文关联：本块正文引用 [n] -> 该文献 References 对应条目（隐式元数据，按需暴露）
        cites = _cited_refs(db, r["doc_id"], r["text"], caption_cache)
        if cites:
            entry["citations"] = cites
        if r["section"] != "Figure/Table":
            fm = _FIGREF_RE.search(r["text"])
            if fm:
                if r["doc_id"] not in caption_cache:
                    caption_cache[r["doc_id"]] = _doc_captions(db, r["doc_id"])
                caps = caption_cache[r["doc_id"]]
                if fm.group(1) in caps:
                    entry["figure"] = "Fig. %s%s — %s" % (
                        fm.group(1), fm.group(2) or "", caps[fm.group(1)][:140])
        if not r["doi"]:
            first_author = (r["authors"] or "").split(";")[0].split(",")[0].strip()
            search_parts = [r["title"], first_author,
                            str(r["year"]) if r["year"] else ""]
            entry["search"] = " ".join(p for p in search_parts if p)
        results.append(entry)
        seed_doc_ids.append(r["doc_id"])
        if len(results) >= top_k:
            break

    resp = {"query": query, "scored": len(ranked), "top_k": top_k,
            "mode_used": mode_used, "reranker": reranker_used, "results": results,
            # 本次检索实际用到的设备（模型此刻已加载，所以这里必然有真值）
            "device": device_report(),
            # 折叠掉的同论文副本数（渲染层可提示，便于用户知道库里存在多份副本）
            "dup_collapsed": dup_collapsed,
            # 相关性地板与结论：verdict='无关' + no_hit=true 时 results 为空、closest 给出
            # "最接近的 5 篇"。让"库里没有"成为不可误读的信号，而不是返回 Top-3 垃圾。
            "verdict": verdict, "no_hit": no_hit, "max_score": round(raw_best, 4) if raw_best is not None else None,
            "floor": floor, "floor_weak": KB_MIN_RERANK_WEAK if floor is not None else None,
            "closest": closest,
            "note": (note + f"命中 {len(ranked)} 块，返回 Top-{len(results)}") if len(ranked) else (note or "无命中"),
            "cached": False,
            "ms": round((time.time() - t0) * 1000)}
    if related_flag and results:
        try:
            kb_key = str(db.execute("PRAGMA database_list").fetchall()[0][2] or "kb")
            if seed_doc_ids:
                resp["related"] = related_docs(db, kb_key, seed_doc_ids, related_k)
        except Exception as e:
            resp["related_error"] = str(e)[:200]
    # 语言提示：零成本检测（只判断是否含 CJK + 库内中文占比），**不改写查询**。
    # 归一化的责任在调用方模型（引擎按原样检索，见文件顶部说明）。
    if _CJK_RE.search(query or ""):
        share = _library_cjk_share(db)
        if share < 0.10:
            resp["lang_note"] = (
                "库内正文以英文为主（抽样中文占比约 %.1f%%），本次已按原样检索：BM25 关键词路"
                "基本空转，命中主要由向量侧跨语言匹配决定。建议改用英文术语重查，"
                "或用 depth=deep 深查。" % (share * 100))
    # 无命中/弱命中也入缓存：否则 agent 换词重试时每次都要重跑一遍同样的空结果。
    # 索引或元数据变化时 cmd_ingest 会 DELETE FROM cache，key 又含解析器/引擎 token，
    # 所以"负结果"不会陈旧。
    if use_cache and cache_key is not None:
        try:
            db.execute("INSERT OR REPLACE INTO cache(key, payload, created) VALUES(?,?,?)",
                       (cache_key, json.dumps(resp, ensure_ascii=True), time.time()))
        except sqlite3.OperationalError as ex:
            # 缓存只是加速：异步入库的子进程正持有写锁时会撞锁，而结果已经算完——
            # 不能因为写缓存失败让整次检索作废（与上面 related_docs 的容错一致）。
            # 锁定/busy 以外的 OperationalError 仍然上抛，不掩盖真实故障。
            if "locked" not in str(ex).lower() and "busy" not in str(ex).lower():
                raise
    return resp


def _depth_of(req, default):
    """快速/深度双模式：quick=快速检索（无精排/无关联文献/更少更短），deep=深度检索（全链路）。

    显式传参（top_k/snippet/rerank/related）永远优先于模式缺省。"""
    depth = req.get("depth") or default
    return depth if depth in ("quick", "deep") else default


def _depth_flag(req, key, depth, quick_default, deep_default):
    """rerank/related 的模式化缺省：未传或传 null 时按 depth 取，显式传 bool 就尊重。

    MCP 客户端未传的参数会以 null 编进请求（server 侧已剔除，raw 协议兜底）。"""
    if key in req and req.get(key) is not None:
        return req.get(key) is not False
    return quick_default if depth == "quick" else deep_default


def cmd_search(req):
    t0 = time.time()
    query = (req.get("query") or "").strip()
    if not query:
        return {"ok": False, "error": "query is required"}
    depth = _depth_of(req, "quick")           # kb_search 是查信息入口：默认快速检索
    quick = depth == "quick"
    top_k = min(max(int(req.get("top_k") or (3 if quick else 5)), 1), 10)
    snippet_w = min(max(int(req.get("snippet") or (300 if quick else 400)), 100), 2000)
    filters = req.get("filters") or {}
    mode = req.get("mode") or "hybrid"
    use_cache = req.get("cache") is not False
    rerank_flag = _depth_flag(req, "rerank", depth, False, True)
    related_flag = _depth_flag(req, "related", depth, False, True)
    related_k = min(max(int(req.get("related_k") or 5), 1), 10)
    db = connect(req.get("kb_root") or ".kb")
    try:
        resp = _search_core(db, query, top_k, snippet_w, filters, mode, use_cache,
                            rerank_flag, related_flag, related_k)
        db.commit()
    finally:
        db.close()
    resp["ok"] = True
    resp["depth"] = depth
    resp["ms_total"] = round((time.time() - t0) * 1000)
    return resp


def cmd_rag(req):
    """Evidence assembly for the DSH kb_rag tool; generation is the model's job."""
    query = (req.get("query") or "").strip()
    if not query:
        return {"ok": False, "error": "query is required"}
    depth = _depth_of(req, "deep")            # kb_rag 是问答入口：默认深度检索
    quick = depth == "quick"
    top_k = min(max(int(req.get("top_k") or (2 if quick else 3)), 1), 10)
    snippet_w = 400 if quick else 600
    filters = req.get("filters") or {}
    rerank_flag = _depth_flag(req, "rerank", depth, False, True)
    related_flag = _depth_flag(req, "related", depth, False, True)
    related_k = min(max(int(req.get("related_k") or 5), 1), 10)
    db = connect(req.get("kb_root") or ".kb")
    try:
        resp = _search_core(db, query, top_k, snippet_w, filters, "hybrid", True,
                            rerank_flag, related_flag, related_k)
        db.commit()
    finally:
        db.close()
    resp["ok"] = True
    resp["depth"] = depth
    resp["evidence"] = resp.pop("results")
    if quick:
        resp["guidance"] = ("快速检索：拿到 evidence 立即作答，不要长思考/长推理——"
                            "用户是单点查询。回答控制在一两句内：直接给查到的文献信息/数据/术语，"
                            "标注来源编号 [n]（对应 evidence 下标）即可；"
                            "禁止背景铺垫、延伸分析、二次检索、追加说明；"
                            "用户想要更多背景/延伸时再改用 depth=deep 重查。")
    else:
        resp["guidance"] = (
            "深度检索：基于 evidence 作答，每个事实标注来源编号 [n]（对应 evidence 下标），"
            "可综合多篇证据展开论述（适合领域调研，注意给出背景与脉络）；"
            "资料不足时明确回答\"根据现有资料无法回答\"；多源冲突时分别列出；"
            "答案末尾按来源分三列给补充建议（哪列为空就整列省略，全空则整块省略）："
            "①「库内可查（循引文找到）」：evidence 各条 citations 里标 [库内] 的文献——"
            "必须写出关系链：《被引文献》(作者, 年份) 被 [证据编号] 《证据文献》的引文 Ref n 引用，"
            "并注明已在库内、可直接对其提问；"
            "②「建议补库（循引文发现）」：citations 里未命中库内的条目——写成 (作者, 年份, 期刊/标题)，"
            "注明被 [证据编号] 的 Ref n 引用、尚不在库内，需要时用 Ref 编号定位下载；"
            "③「相关文献」：related 列表（同作者/同期刊/主题相似，元数据相似，无引文关系）。"
            "每条推荐的理由必须写明属于哪种，引文关联的必须带关系链，不得把三种混为一列。")
    return resp


# ---------------------------------------------------------------- stats


def _rev_of(token):
    """从 indexed_with（形如 '3.2.0/rev5'）里取 rev；无标记的老行返回 0。"""
    m = re.search(r"/rev(\d+)", token or "")
    return int(m.group(1)) if m else 0


def _stale_kind(stale_revs):
    """陈旧数据该给哪种升级建议：
    'none' 无陈旧；'chunk' 需要全量重灌（区间 (旧 rev, 当前 rev] 里有一个改动分块的 rev）；
    'meta'  只刷元数据即可。区间为空（同 rev）不会进到这里。"""
    if not stale_revs:
        return "none"
    for rev in stale_revs:
        if rev == 0:
            return "chunk"        # 没有标记的老行：无从判断，按最保守的重灌处理
        if any(r in CHUNK_AFFECTING_REVS for r in range(rev + 1, PARSER_REV + 1)):
            return "chunk"
    return "meta"


def cmd_stats(req):
    t0 = time.time()
    db = connect(req.get("kb_root") or ".kb")
    try:
        docs_n = db.execute("SELECT COUNT(*) AS n FROM docs").fetchone()["n"]
        chunks_n = db.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
        chars_n = db.execute(
            "SELECT COALESCE(SUM(LENGTH(text)),0) AS n FROM chunks").fetchone()["n"]
        vecs_n = db.execute("SELECT COUNT(*) AS n FROM vecs").fetchone()["n"]
        orphan_chunks = db.execute(
            "SELECT COUNT(*) AS n FROM chunks WHERE doc_id NOT IN (SELECT id FROM docs)"
        ).fetchone()["n"]
        missing_vecs = db.execute(
            "SELECT COUNT(*) AS n FROM chunks c LEFT JOIN vecs v ON v.chunk_id = c.id "
            "WHERE v.chunk_id IS NULL AND c.weight > 0").fetchone()["n"]
        # 整篇不可检索：一篇文档一个 weight>0 的分块都没有 → 它在检索里等于不存在。
        # 这类问题以前**完全不可见**（用户既搜不到、也没有任何提示），现在进 health。
        blind_sql = ("SELECT d.path FROM docs d WHERE NOT EXISTS "
                     "(SELECT 1 FROM chunks c WHERE c.doc_id = d.id AND c.weight > 0)")
        blind_docs = db.execute(
            "SELECT COUNT(*) AS n FROM docs d WHERE NOT EXISTS "
            "(SELECT 1 FROM chunks c WHERE c.doc_id = d.id AND c.weight > 0)").fetchone()["n"]
        blind_sample = [Path(r["path"]).name for r in
                        db.execute(blind_sql + " ORDER BY d.id LIMIT 5").fetchall()]
        rows = db.execute(
            "SELECT path,title,authors,year,kind,chunk_count,indexed_at "
            "FROM docs ORDER BY indexed_at DESC LIMIT 20").fetchall()
        # 陈旧数据：入库时用的解析器版本（rev）与当前不一致。取 '/rev' 之后的部分做**精确**
        # 比较，避免 'rev2' 前缀误匹配 'rev20'；NULL / 无标记的老行都算陈旧。
        stale_sql = ("WHERE COALESCE(substr(indexed_with, instr(indexed_with, '/rev') + 4), '') <> ?")
        stale_docs = db.execute(
            "SELECT COUNT(*) AS n FROM docs " + stale_sql, (str(PARSER_REV),)).fetchone()["n"]
        stale_revs = {_rev_of(r["indexed_with"]) for r in
                      db.execute("SELECT indexed_with FROM docs " + stale_sql,
                                 (str(PARSER_REV),)).fetchall()}
        stale_sample = [Path(r["path"]).name for r in db.execute(
            "SELECT path FROM docs " + stale_sql + " ORDER BY id LIMIT 5",
            (str(PARSER_REV),)).fetchall()]
    finally:
        db.close()
    health = {"orphan_chunks": orphan_chunks, "missing_vecs": missing_vecs,
              "docs_without_retrievable_chunks": blind_docs, "blind_sample": blind_sample,
              "ok": orphan_chunks == 0 and missing_vecs == 0 and blind_docs == 0}
    migration = dict(_LAST_CONNECT)
    migration.pop("logged", None)
    return {
        "ok": True,
        "db": str((Path(req.get("kb_root") or ".kb") / "kb.sqlite").resolve()),
        "schema_version": SCHEMA_VERSION,
        "parser_rev": PARSER_REV,
        "indexed_with": PARSER_TOKEN,
        # >0 表示库里有文档是用旧解析器入库的：增量入库不会自愈。
        # stale_kind 决定给用户哪个建议：'chunk' = 必须全量重灌（分块/向量会变），
        # 'meta' = 秒级刷元数据即可。给"无效的选项"比不给更糟，所以这里要分清楚。
        "stale_docs": stale_docs,
        "stale_sample": stale_sample,
        "stale_kind": _stale_kind(stale_revs),
        "chunk_affecting_revs": sorted(CHUNK_AFFECTING_REVS),
        "migration": migration,
        "health": health,
        # 向量链路状态：只反映本进程已有的加载结果，不主动加载模型（kb_stats 要便宜）。
        # missing_vecs / embedding_error 一起给出"检索为什么退化成纯关键词"的直接证据（issue #2）。
        "embedding": _EMBED_NAME if _EMBEDDER is not None else None,
        "embedding_error": _EMBED_ERR,
        "vectors_missing": missing_vecs,
        "retry_secs": MODEL_RETRY_SECS,
        "device": device_report(),
        "docs": docs_n,
        "chunks": chunks_n,
        "vectors": vecs_n,
        "chars": chars_n,
        "recent": [{
            "file": Path(r["path"]).name, "path": r["path"], "title": r["title"],
            "authors": r["authors"], "year": r["year"], "kind": r["kind"],
            "chunks": r["chunk_count"], "indexed_at": r["indexed_at"],
        } for r in rows],
        "ms": round((time.time() - t0) * 1000),
    }


def cmd_reload(req):
    """清掉模型加载失败的缓存并立刻重试（无需杀掉守护进程，issue #2）。

    drop_models=true 连已加载的模型一起释放（下次使用时重新加载）；rerank=true 顺带探测
    精排模型（默认只探测嵌入模型：精排模型可能触发 GB 级下载，不该被一次 reload 带出来）。"""
    global _EMBEDDER, _EMBED_ERR, _EMBED_ERR_AT, _EMBED_NAME
    global _RERANKER, _RERANK_ERR, _RERANK_ERR_AT, _RERANK_NAME
    if req.get("drop_models"):
        _EMBEDDER = None
        _EMBED_NAME = None
        _RERANKER = None
        _RERANK_NAME = None
    _EMBED_ERR = None
    _EMBED_ERR_AT = 0.0
    _RERANK_ERR = None
    _RERANK_ERR_AT = 0.0
    emb = get_embedder()
    rr = get_reranker() if req.get("rerank") else _RERANKER
    return {
        "ok": True,
        "embedding": _EMBED_NAME if emb is not None else None,
        "embedding_error": None if emb is not None else (_EMBED_ERR or "embedding model unavailable"),
        "reranker": _RERANK_NAME if rr is not None else None,
        "reranker_error": None if rr is not None else _RERANK_ERR,
        "retry_secs": MODEL_RETRY_SECS,
    }


# ---------------------------------------------------------------- zotero

def _find_zotero_db(explicit):
    import glob
    if explicit:
        p = Path(explicit)
        return str(p) if p.is_file() else None
    for c in [os.path.expanduser("~/Zotero/zotero.sqlite"),
              os.path.expanduser("~/Documents/Zotero/zotero.sqlite")]:
        if os.path.isfile(c):
            return c
    for p in glob.glob(os.path.expandvars(r"%APPDATA%\Zotero\Zotero\Profiles\*\zotero\zotero.sqlite")):
        return p
    return None


def _zotero_entries(zdb):
    """[(absolute_path, meta_dict, parent_type), ...] for every stored PDF attachment."""
    conn = sqlite3.connect(f"file:{zdb}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    data_dir = str(Path(zdb).parent)
    rows = conn.execute("""
        SELECT ia.itemID, ia.parentItemID, ia.path, ia.linkMode, i.key AS itemKey,
               it.typeName AS parentType
        FROM itemAttachments ia
        JOIN items i ON i.itemID = ia.itemID
        LEFT JOIN deletedItems d ON d.itemID = ia.itemID
        LEFT JOIN items pi ON pi.itemID = ia.parentItemID
        LEFT JOIN itemTypes it ON it.itemTypeID = pi.itemTypeID
        WHERE d.itemID IS NULL AND ia.contentType = 'application/pdf'
    """).fetchall()
    entries = []
    for r in rows:
        path = r["path"] or ""
        if path.startswith("storage:"):  # stored file: storage/<itemKey>/<name>
            fname = path[len("storage:"):]
            full = os.path.join(data_dir, "storage", r["itemKey"], fname)
        elif path.startswith("attachments:") or os.path.isabs(path):  # linked file
            full = path[len("attachments:"):] if path.startswith("attachments:") else path
            if not os.path.isabs(full):
                full = os.path.join(data_dir, full)
        else:
            continue  # imported URL / web snapshot: no local file
        meta = _zotero_meta(conn, r["parentItemID"])
        meta["_zotero_key"] = r["itemKey"]
        entries.append((full, meta, r["parentType"]))
    conn.close()
    return entries


def _zotero_meta(conn, parent_id):
    if parent_id is None:
        return {}
    fields = {}
    for row in conn.execute("""
            SELECT f.fieldName AS name, idv.value AS value
            FROM itemData id JOIN fields f ON f.fieldID = id.fieldID
            JOIN itemDataValues idv ON idv.valueID = id.valueID
            WHERE id.itemID = ?""", (parent_id,)):
        fields[row["name"]] = row["value"]
    creators = []
    for row in conn.execute("""
            SELECT c.lastName AS ln, c.firstName AS fn, ct.creatorType AS role
            FROM itemCreators ic JOIN creators c ON c.creatorID = ic.creatorID
            JOIN creatorTypes ct ON ct.creatorTypeID = ic.creatorTypeID
            WHERE ic.itemID = ? ORDER BY ic.orderIndex""", (parent_id,)):
        if (row["role"] or "").lower() == "author":
            creators.append(f"{row['fn'] or ''} {row['ln'] or ''}".strip())
    date = fields.get("date") or ""
    m = re.search(r"(19|20)\d{2}", date)
    return {
        "title": fields.get("title"),
        "authors": "; ".join(c for c in creators if c) or None,
        "year": int(m.group(0)) if m else None,
        "journal": fields.get("publicationTitle") or fields.get("journalAbbreviation"),
        "doi": fields.get("DOI"),
    }


def cmd_zotero(req):
    _REL_CENTROID.clear()
    """Migrate Zotero library entries with PDF attachments into the KB."""
    t0 = time.time()
    kb_root = req.get("kb_root") or ".kb"
    zdb = _find_zotero_db(req.get("zotero_db"))
    if not zdb:
        return {"ok": False,
                "error": "未找到 zotero.sqlite（默认位置 ~/Zotero、~/Documents/Zotero、%APPDATA% 配置文件均未命中）。请用 zotero_db 参数显式指定路径。"}
    limit = req.get("limit")
    force = bool(req.get("force"))
    dry_run = bool(req.get("dry_run"))
    try:
        entries = _zotero_entries(zdb)
    except Exception as e:
        return {"ok": False, "error": f"读取 Zotero 数据库失败: {type(e).__name__}: {e}"[:300]}
    if limit:
        entries = entries[: int(limit)]
    db = connect(kb_root)
    files = []
    totals = {"added": 0, "updated": 0, "skipped": 0, "errors": 0, "missing": 0,
              "duplicates": 0, "chunks": 0, "vectors": 0}
    processed = 0
    progress_path = req.get("progress_path")   # 异步任务专用：与 cmd_ingest 同语义

    def _prog():
        if progress_path:
            try:
                # 原子写：临时文件 + rename，避免轮询方读到半截 JSON
                tmp = progress_path + ".tmp"
                Path(tmp).write_text(json.dumps(
                    {"status": "running", "processed": processed,
                     "errors": totals["errors"], "chunks": totals["chunks"]},
                    ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, progress_path)
            except Exception:
                pass

    try:
        for full, meta, ptype in entries:
            if not os.path.isfile(full):
                totals["missing"] += 1
                files.append({"path": full, "status": "missing", "type": ptype})
                processed += 1
                _prog()
                continue
            if dry_run:
                files.append({"path": full, "status": "candidate", "type": ptype,
                              "title": meta.get("title"), "year": meta.get("year")})
                processed += 1
                _prog()
                continue
            _ingest_file(db, Path(full), force, files, totals, meta)
            processed += 1
            # 与 cmd_ingest 对齐的逐文件 commit：整批单事务有两个后果——
            # ① 任务被杀（宿主强杀/断电）时把已入库文件全部回滚；
            # ② 写锁窗口覆盖整批，异步入库期间同库所有写命令都要等满 busy timeout。
            db.commit()
            _prog()
        if totals["added"] or totals["updated"]:
            db.execute("DELETE FROM cache")
        db.commit()
    finally:
        db.close()
    return {"ok": True, "zotero_db": zdb, "candidates": len(entries),
            # dry_run 语义是"预览全部候选"，不截断；真实迁移才只回最近 20 条压缩 JSON
            "dry_run": dry_run,
            "files": files if dry_run else files[-20:],
            "files_total": len(files),
            "totals": totals,
            "ms": round((time.time() - t0) * 1000)}


# ---------------------------------------------------------------- dedup


def cmd_dedup(req):
    _REL_CENTROID.clear()
    """Remove docs whose sha256 duplicates an earlier doc (keeps lowest id)."""
    t0 = time.time()
    db = connect(req.get("kb_root") or ".kb")
    removed = []
    try:
        dups = db.execute("""
            SELECT id, path, sha256 FROM docs
            WHERE sha256 IS NOT NULL AND sha256 IN (
                SELECT sha256 FROM docs WHERE sha256 IS NOT NULL
                GROUP BY sha256 HAVING COUNT(*) > 1)
            ORDER BY sha256, id""").fetchall()
        keep = {}
        for r in dups:
            if r["sha256"] not in keep:
                keep[r["sha256"]] = r["id"]
            else:
                removed.append({"id": r["id"], "path": r["path"]})
        for r in removed:
            db.execute("DELETE FROM vecs WHERE chunk_id IN "
                       "(SELECT id FROM chunks WHERE doc_id = ?)", (r["id"],))
            db.execute("DELETE FROM chunks WHERE doc_id = ?", (r["id"],))
            db.execute("DELETE FROM docs WHERE id = ?", (r["id"],))
        if removed:
            db.execute("DELETE FROM cache")
        db.commit()
        n_docs = db.execute("SELECT COUNT(*) AS n FROM docs").fetchone()["n"]
        n_chunks = db.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
    finally:
        db.close()
    return {"ok": True, "removed": len(removed), "docs": n_docs,
            "chunks": n_chunks, "files": removed,
            "ms": round((time.time() - t0) * 1000)}


# ---------------------------------------------------------------- clear


def cmd_clear(req):
    _REL_CENTROID.clear()
    """Wipe every doc/chunk/vector/cache row; destructive, requires confirm: true."""
    t0 = time.time()
    if req.get("confirm") is not True:
        return {"ok": False,
                "error": "清空全部文献是破坏性操作且不可恢复：请显式传 confirm: true 确认"}
    db = connect(req.get("kb_root") or ".kb")
    try:
        docs_n = db.execute("SELECT COUNT(*) AS n FROM docs").fetchone()["n"]
        chunks_n = db.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
        db.execute("DELETE FROM vecs")
        db.execute("DELETE FROM chunks")
        db.execute("DELETE FROM docs")
        db.execute("DELETE FROM cache")
        db.commit()
        db.execute("VACUUM")
        db.commit()
        # 一并清空后台任务残留（job/progress/result 旧文件会让 kb_status 读出陈旧结果）
        try:
            jdir = _jobs_dir(req.get("kb_root") or ".kb")
            if jdir.exists():
                for f in jdir.glob("*.json*"):   # 含原子写的 *.json.tmp 残留
                    f.unlink(missing_ok=True)
        except Exception:
            pass
        db_path = str((Path(req.get("kb_root") or ".kb") / "kb.sqlite").resolve())
    finally:
        db.close()
    return {"ok": True, "cleared_docs": docs_n, "cleared_chunks": chunks_n,
            "db": db_path,
            "note": "已清空全部文献与索引（含后台任务记录），可用 kb_ingest 或 kb_zotero 重建。",
            "ms": round((time.time() - t0) * 1000)}


# ---------------------------------------------------------------- fetch (download OA PDF by identifier)


_BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"


def _publisher_pdf_url(doi):
    """Canonical publisher PDF URL from a DOI prefix, or None (fall to OA).

    For paywalled journals this URL only yields a PDF when the network has
    institutional/campus access; otherwise it returns an HTML paywall and we
    fall back to OA. No paywall bypass."""
    m = re.match(r"10\.\d{4,9}/(.+)", doi)
    suffix = m.group(1) if m else ""
    if doi.startswith("10.1038/"):
        return "https://www.nature.com/articles/%s.pdf" % suffix
    if doi.startswith("10.1126/"):
        return "https://www.science.org/doi/pdf/%s" % doi
    if doi.startswith("10.1002/"):
        return "https://onlinelibrary.wiley.com/doi/pdfdirect/%s" % doi
    if doi.startswith("10.1021/"):
        return "https://pubs.acs.org/doi/pdf/%s" % doi
    if doi.startswith("10.1007/"):
        return "https://link.springer.com/content/pdf/%s.pdf" % doi
    if doi.startswith("10.1088/"):
        return "https://iopscience.iop.org/article/%s/pdf" % doi
    return None


def _extract_pdf_url_from_html(doi):
    """Fetch the article landing page (via doi.org redirect) and extract the canonical
    PDF URL from the <meta name="citation_pdf_url"> tag. Works when the network can
    reach the publisher HTML (e.g. campus/institutional IP); returns None otherwise."""
    import urllib.request
    import urllib.parse
    try:
        land = "https://doi.org/" + urllib.parse.quote(doi)
        req = urllib.request.Request(land, headers={"User-Agent": _BROWSER_UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            html = r.read(2 * 1024 * 1024).decode("utf-8", "ignore")
    except Exception:
        return None
    m = re.search(r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
    if not m:
        m = re.search(r'<meta[^>]+content=["\']([^"\']+\.pdf[^"\']*)["\'][^>]+name=["\']citation_pdf_url["\']', html, re.I)
    return m.group(1) if m else None


def _candidate_sources(ident):
    """Ordered (url, name) candidates: publisher PDF -> landing-page HTML -> OA fallback.

    arXiv -> direct; DOI -> publisher direct PDF -> citation_pdf_url from HTML -> Unpaywall OA."""
    import urllib.parse
    import urllib.request
    ident = (ident or "").strip()
    m = _ARXIV_RE.search(ident) or re.search(r"10\.48550/arXiv\.(\d{4}\.\d{4,5})", ident)
    if m:
        aid = m.group(1)
        return [("https://arxiv.org/pdf/" + aid, "arxiv_" + aid)]
    md = re.search(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", ident)
    doi = md.group(0).rstrip(".,;") if md else None
    if not doi:
        return []
    out = []
    pub = _publisher_pdf_url(doi)
    if pub:
        out.append((pub, doi))
    html_pdf = _extract_pdf_url_from_html(doi)
    if html_pdf and html_pdf != pub:
        out.append((html_pdf, doi))
    try:
        u = ("https://api.unpaywall.org/v2/%s?email=%s"
             % (urllib.parse.quote(doi),
                os.environ.get("UNPAYWALL_EMAIL") or "kbrag.demo@gmail.com"))
        req = urllib.request.Request(u, headers={"User-Agent": "kb-rag/1.0"})
        with urllib.request.urlopen(req, timeout=25) as r:
            d = json.loads(r.read().decode("utf-8"))
        loc = d.get("best_oa_location") or {}
        url = loc.get("url_for_pdf") or loc.get("url")
        name = (d.get("title") or "paper")[:80]
        if url:
            out.append((url, name))
    except Exception:
        pass
    return out


def _download_bytes(url):
    import urllib.request
    req = urllib.request.Request(url, headers={
        "User-Agent": _BROWSER_UA,
        "Accept": "application/pdf,*/*"})
    with urllib.request.urlopen(req, timeout=60) as r:
        ct = (r.headers.get("content-type") or "").lower()
        if "text/html" in ct:
            raise RuntimeError("paywall/HTML page, not a PDF")
        return r.read()


def _local_proxy_ports():
    """Probe common local proxy ports (clash 7890/7897, v2ray 10808/10809, socks 1080)."""
    import socket
    open_ports = []
    for port in (7890, 7897, 10809, 10808, 1080):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.6)
        try:
            s.connect(("127.0.0.1", port))
            open_ports.append(port)
        except Exception:
            pass
        finally:
            s.close()
    return open_ports


def _node_doi_pdf_script():
    """Locate the bundled Node downloader (doi_pdf.mjs) next to the engine, or None.

    Node 版比 Python urllib 强在:Node fetch(undici)TLS 指纹更接近浏览器、
    手动重定向 + 全程 cookie jar(绕过 Nature cookies_not_supported)、
    候选源多(Unpaywall / Crossref PDF link / citation_pdf_url meta / 页面 pdf 链接)。"""
    import shutil
    if shutil.which("node") is None:
        return None
    here = Path(__file__).resolve().parent
    for cand in (here / "scripts" / "doi_pdf.mjs",
                 here / "tools" / "doi_pdf.mjs",
                 here / "doi_pdf.mjs"):
        if cand.is_file():
            return str(cand)
    return None


def cmd_fetch(req):
    """Download open-access PDFs by DOI / arXiv ID into a local dir (pull-only).

    首选 Node 下载器(doi_pdf.mjs,随包分发);Node 不可用或漏掉的标识符回退 Python urllib。"""
    import urllib.parse
    import subprocess
    t0 = time.time()
    ids = req.get("identifiers") or []
    if isinstance(ids, str):
        ids = [i.strip() for i in re.split(r"[,\s;]+", ids) if i.strip()]
    if not ids:
        return {"ok": False, "error": "identifiers 必填：给一个 DOI 或 arXiv ID 列表"}
    def _norm_fetch_id(i):
        # 与 Node doi_pdf.mjs 的 norm() 保持一致,避免同标识符被 Node/Python 双跑
        i = re.sub(r"^https?://(dx\.)?doi\.org/", "", i, flags=re.I)
        i = re.sub(r"^https?://arxiv\.org/abs/", "", i, flags=re.I)
        i = re.sub(r"^arxiv[:/]", "", i, flags=re.I)
        return i.rstrip(".,;").strip()

    ids = [_norm_fetch_id(i) for i in ids]
    target = req.get("target_dir") or str(Path.home() / ".kb-rag" / "downloads")
    Path(target).mkdir(parents=True, exist_ok=True)
    files = []
    node_note = None
    script = _node_doi_pdf_script()
    if script:
        try:
            p = subprocess.run(["node", script, "--out", target] + ids,
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=900)
            for line in p.stdout.splitlines():
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                entry = {"id": rec.get("doi") or "?",
                         "status": "downloaded" if rec.get("ok") else "failed",
                         "path": rec.get("file"),
                         "source": rec.get("source"),
                         "error": rec.get("error") or rec.get("lastReason") or None}
                if entry["status"] == "downloaded" and not entry["path"]:
                    entry["status"] = "failed"
                    entry["error"] = entry["error"] or "下载成功但未返回文件路径"
                if entry["status"] == "downloaded" and entry["path"]:
                    try:
                        entry["bytes"] = Path(entry["path"]).stat().st_size
                    except Exception:
                        pass
                files.append(entry)
        except Exception as e:
            node_note = "Node 下载器执行失败(%s)，已回退 Python 下载路径" % str(e)[:80]
    # Python 兜底:Node 未运行或漏掉的标识符(如 Node 中途崩溃)
    handled = set(f["id"] for f in files)
    for ident in ids:
        if ident in handled:
            continue
        entry = {"id": ident, "status": "failed", "path": None}
        cands = _candidate_sources(ident)
        if not cands:
            entry["error"] = "无可用下载源（付费墙且 Unpaywall 无 OA 记录，或非 DOI/arXiv）——校园网/机构访问请手动下载后 kb_ingest 入库"
        else:
            for url, name in cands:  # 出版商正式版优先，OA 兜底
                try:
                    data = _download_bytes(url)
                    if data[:4].startswith(b"%PDF"):
                        safe = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", "_", name or ident).strip("_")[:60]
                        fname = safe + ".pdf"
                        path = Path(target) / fname
                        path.write_bytes(data)
                        entry["status"] = "downloaded"
                        entry["path"] = str(path)
                        entry["bytes"] = len(data)
                        entry["source"] = url[:90]
                        break
                except Exception:
                    continue
            if entry["status"] != "downloaded":
                entry["error"] = "付费墙且无 OA 版本：请在校园网/机构访问下到出版商页面手动下载后 kb_ingest 入库"
        files.append(entry)
    downloaded = sum(1 for f in files if f["status"] == "downloaded")
    # 网络环境 + 代理检测(用于下载提示;host 端已问过用户并传 env)
    import urllib.request as _ur
    net = req.get("network") or {}
    net_env = net.get("env") or "unknown"
    sys_proxy = _ur.getproxies()
    host_ports = net.get("proxy", {}).get("localPorts") or []
    proxy_report = {"env": net.get("proxy", {}).get("env") or [],
                    "localPorts": sorted(set(host_ports) | set(_local_proxy_ports())),
                    "system": bool(sys_proxy)}
    if sys_proxy:
        proxy_report["systemDetail"] = {k: v for k, v in list(sys_proxy.items())[:3]}
    note = "已下载 %d/%d 篇到 %s。" % (downloaded, len(ids), target)
    if net_env == "campus":
        note += "校园网/机构网络：出版商正式版优先（可含订阅版 PDF）。"
    elif net_env == "home":
        note += "家庭网络：以 OA 开放获取为主，付费墙期刊需校园网/机构访问或手动下载。"
    if bool(sys_proxy) or bool(proxy_report["env"]) or bool(proxy_report["localPorts"]):
        note += "检测到代理（系统/环境变量/本机端口），代理可能干扰下载（TLS/反爬），如失败请关闭代理后重试。"
    note += "付费墙文献不自动绕过：请在浏览器打开对应 DOI 手动下载后 kb_ingest 入库；下载好的 PDF 用「文件 → 添加文件」手动导入 Zotero。"
    if node_note:
        note += " " + node_note
    # ingest=true：下载即入库（"深挖模式"里 agent 要反复"找文献 → 入库 → 再查"，
    # 少一次往返就少一轮；增量入库会按 sha256 自动跳过已入库的文件，重复调用安全）
    ingest_result = None
    if req.get("ingest") and downloaded:
        paths = [f["path"] for f in files if f["status"] == "downloaded" and f.get("path")]
        if paths:
            try:
                ingest_result = cmd_ingest({"kb_root": req.get("kb_root") or ".kb", "paths": paths})
                ingest_result.pop("device", None)      # 响应里不必重复一整块设备信息
                note += " 已直接入库 %d 篇（可用 kb_search 立即检索）。" % len(paths)
            except Exception as e:
                ingest_result = {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:200])}
                note += " 入库失败：%s" % str(ingest_result["error"])[:120]
    return {"ok": True, "target": target, "total": len(ids), "downloaded": downloaded,
            "files": files, "note": note, "ingest": ingest_result,
            "network": {"env": net_env, "proxy": proxy_report},
            "ms": round((time.time() - t0) * 1000)}


# ---------------------------------------------------------------- 工作区状态文件

# 允许持久化的键与取值（插件侧会话默认值；写别的键一律忽略，避免状态文件变成杂物堆）
STATE_KEYS = {
    "scope": ("kb", "both", "web"),
    "depth": ("quick", "deep"),
    "strict": (True, False),
    "enabled": (True, False),
    "diligence": ("normal", "thorough"),
}


def _state_path(kb_root):
    """<workspace>/.kb-rag/state.json —— 与 .kb 同级，用户可见、可手改、跨会话记住。"""
    return Path(kb_root or ".kb").resolve().parent / ".kb-rag" / "state.json"


def cmd_state(req):
    """读写工作区状态文件。

    为什么由引擎代写：插件跑在宿主进程里、受 sandboxPolicy 约束，直接写文件不保险；
    引擎本来就在工作区里读写（.kb / .kb-jobs），这条路更干净。
    action: read（默认）/ write（带 state 对象）/ reset。
    """
    path = _state_path(req.get("kb_root"))
    cur = {}
    try:
        if path.is_file():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cur = {k: v for k, v in loaded.items() if k in STATE_KEYS or k == "throttle"}
    except Exception:
        cur = {}
    action = req.get("action") or "read"
    if action == "read":
        return {"ok": True, "path": str(path), "state": cur}
    if action == "reset":
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return {"ok": True, "path": str(path), "state": {}, "reset": True}
    if action == "write":
        patch = req.get("state") or {}
        if not isinstance(patch, dict):
            return {"ok": False, "error": "state 必须是对象"}
        rejected = []
        for k, v in patch.items():
            if k == "throttle":
                if isinstance(v, dict):
                    cur["throttle"] = v
                else:
                    rejected.append(k)
                continue
            allowed = STATE_KEYS.get(k)
            if allowed is None:
                rejected.append(k)
                continue
            if v not in allowed:
                rejected.append(k)
                continue
            cur[k] = v
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            return {"ok": False, "error": "写入失败 %s: %s" % (type(e).__name__, str(e)[:150])}
        return {"ok": True, "path": str(path), "state": cur,
                "rejected": rejected or None}
    return {"ok": False, "error": "unknown action: %s" % action}


# ------------------------------------------------- async ingest (MCP 60s 超时解药)

def _jobs_dir(kb_root):
    """后台任务目录（与 kb.sqlite 同级的 .kb-jobs/）。"""
    return Path(kb_root) / ".kb-jobs"


# job_id 由 uuid4().hex[:12] 生成：固定 12 位小写十六进制。查询入口据此校验，
# 阻止含路径分隔/.. 段的 job_id 目录穿越读取库外 JSON（cmd_status 用 job_id 拼文件路径）。
_JOB_ID_RE = None  # 惰性编译，避免 import 时开销


def _valid_job_id(job_id):
    import re
    global _JOB_ID_RE
    if _JOB_ID_RE is None:
        _JOB_ID_RE = re.compile(r"^[0-9a-f]{12}$")
    return bool(_JOB_ID_RE.match(job_id))


def cmd_ingest_async(req):
    """异步任务提交（ingest/zotero 通用）：fork 独立子进程跑任务，父进程立即返回 job_id。
    适用于 MCP/Kimi 等有单次调用超时（60s）的宿主：提交即返回，kb_status 轮询。
    注意：子进程与主进程并发写同一 kb.sqlite；任务期间同库的写命令会撞锁失败
    （读命令经逐文件 commit + 孤儿清理容错后可正常连接）。"""
    import subprocess, uuid
    kb_root = req.get("kb_root") or ".kb"
    command = req.get("command") or "ingest"          # 后台任务要执行的引擎命令
    job_id = uuid.uuid4().hex[:12]
    jdir = _jobs_dir(kb_root)
    try:
        jdir.mkdir(parents=True, exist_ok=True)
        job = {"id": job_id, "command": command, "payload": dict(req),
               "result_path": str(jdir / (job_id + ".result.json")),
               "progress_path": str(jdir / (job_id + ".progress.json"))}
        jf = jdir / (job_id + ".job.json")
        jf.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        script = os.path.abspath(sys.argv[0]) if (sys.argv and sys.argv[0]) else os.path.abspath(__file__)
        if not os.path.isfile(script):
            # 脚本路径不存在时直接失败并清掉 job 文件：否则 kb_status 会长期读到 running
            jf.unlink(missing_ok=True)
            return {"ok": False, "error": "后台任务启动失败：找不到引擎脚本 %s" % script}
        proc = subprocess.Popen([sys.executable, script, "run_job", str(jf)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # 启动校验：等一小段窗口再读退出码。原实现是 Popen 后**立刻** poll()，
        # 那基本抓不到失败——python.exe 即使只是"打开不存在的脚本"也要数十毫秒才退出，
        # 于是最可能的启动失败（路径/解释器错误）仍会留下永不结束的 running 任务。
        try:
            rc = proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            rc = None
        except Exception:
            rc = None
        if rc is not None:
            try:
                jf.unlink(missing_ok=True)
                (jdir / (job_id + ".progress.json")).unlink(missing_ok=True)
            except Exception:
                pass
            return {"ok": False, "job_id": job_id,
                    "error": "后台任务进程启动即退出（exit=%s）。请检查引擎脚本路径与 Python 环境。" % rc}
    except Exception as e:
        return {"ok": False, "error": "启动后台任务进程失败: %s: %s" % (type(e).__name__, e)}
    return {"ok": True, "job_id": job_id, "status": "running",
            "kb_root": str(Path(kb_root).resolve()),
            "note": "后台任务已启动（job_id=%s）。用 kb_status(job_id=...) 轮询进度；"
                    "宿主 60s 超时不影响后台任务。" % job_id}


def run_async_job(jobfile):
    """后台子进程入口：读 job 文件 -> 执行对应引擎命令（逐文件回写进度）-> 写结果文件。"""
    job = {}
    try:
        job = json.loads(Path(jobfile).read_text(encoding="utf-8"))
        payload = dict(job.get("payload") or {})
        payload["progress_path"] = job.get("progress_path")
        command = job.get("command") or "ingest"
        handler = {"ingest": cmd_ingest, "zotero": cmd_zotero}.get(command)
        if handler is None:
            raise ValueError("unknown async command: %s" % command)
        result = handler(payload)
        result.setdefault("ok", True)
        out = {"status": "done", "job_id": job.get("id"), "result": result}
    except Exception as e:
        out = {"status": "error", "job_id": job.get("id", "?"),
               "error": "%s: %s" % (type(e).__name__, e)}
    rp = job.get("result_path") if isinstance(job, dict) else None
    if not rp:
        # job 文件本身不可解析/无 result_path：无法告知轮询方，退而求其次
        # 在 job 文件旁写一个 error 结果，让 kb_status 至少能返回"任务失败"而非永久 running
        try:
            jf = Path(jobfile)
            rp = str(jf.with_name((job.get("id") if isinstance(job, dict) and job.get("id") else "unknown") + ".result.json"))
        except Exception:
            rp = None
    if rp:
        try:
            # 原子写：先写临时文件再 rename，避免 kb_status 读到半截 JSON
            tmp = rp + ".tmp"
            Path(tmp).write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, rp)
        except Exception:
            pass
    return out


def cmd_status(req):
    """查询后台任务：done 时返回完整结果（totals + files 最近 20 条），running 时返回进度。"""
    job_id = (req.get("job_id") or "").strip()
    kb_root = req.get("kb_root") or ".kb"
    if not job_id:
        return {"ok": False, "error": "job_id 必填（来自 kb_ingest async_mode 或 ingest_async 的返回）"}
    if not _valid_job_id(job_id):
        return {"ok": False, "error": "job_id 非法（应为 12 位十六进制，来自 kb_ingest async_mode 的返回）"}
    jdir = _jobs_dir(kb_root)
    rp = jdir / (job_id + ".result.json")
    if rp.exists():
        try:
            data = json.loads(rp.read_text(encoding="utf-8"))
            resp = {"ok": True, "job_id": job_id, "status": data.get("status", "done")}
            if data.get("error"):
                resp["error"] = data["error"]
            if isinstance(data.get("result"), dict):
                resp["result"] = data["result"]
            # 已完结的任务：清理 job/progress 中间文件，避免 .kb-jobs 永久累积
            # （result.json 保留供后续 kb_status 读取，kb_clear 会一并清空目录）
            for suffix in (".job.json", ".progress.json"):
                try:
                    (jdir / (job_id + suffix)).unlink(missing_ok=True)
                except Exception:
                    pass
            return resp
        except Exception as e:
            return {"ok": False, "error": "读取任务结果失败: %s" % e}
    pp = jdir / (job_id + ".progress.json")
    prog = None
    if pp.exists():
        try:
            prog = json.loads(pp.read_text(encoding="utf-8"))
        except Exception:
            pass
    if (jdir / (job_id + ".job.json")).exists() or pp.exists():
        # 心跳近似：首个 progress 在首文件处理完后才写；若任务卡在模型加载/锁等环节
        # 会长时间零产出。job.json mtime 超过 1 小时仍无 result，视为可能崩溃/卡死。
        try:
            jf = jdir / (job_id + ".job.json")
            age_h = (time.time() - jf.stat().st_mtime) / 3600 if jf.exists() else 0
        except Exception:
            age_h = 0
        note = ("任务运行中，请稍后重查（完成时返回 totals）"
                if age_h < 1 else
                "任务已超过 1 小时无更新，可能卡死或子进程已退出；可稍后再查，或检查任务日志后重试")
        return {"ok": True, "job_id": job_id, "status": "running", "progress": prog,
                "note": note}
    return {"ok": True, "job_id": job_id, "status": "not_found",
            "note": "未找到该任务（job 目录: %s）" % jdir}


# ---------------------------------------------------------------- serve


def cmd_serve():
    """Line-delimited JSON server: {id, command, payload} -> {id, ok, response|error}.
    Keeps the embedding model loaded across requests (daemon mode)."""
    stdout = sys.stdout.buffer
    for raw in sys.stdin.buffer:
        line = raw.strip()
        if not line:
            continue
        try:
            req = json.loads(line.decode("utf-8"))
        except Exception as e:
            stdout.write(json.dumps(
                {"id": None, "ok": False, "error": f"bad request JSON: {e}"},
                ensure_ascii=True).encode() + b"\n")
            stdout.flush()
            continue
        rid = req.get("id")
        handler = {"ingest": cmd_ingest, "ingest_async": cmd_ingest_async, "status": cmd_status,
                   "search": cmd_search, "rag": cmd_rag,
                   "stats": cmd_stats, "zotero": cmd_zotero,
                   "dedup": cmd_dedup, "clear": cmd_clear, "fetch": cmd_fetch,
                   "reload": cmd_reload, "state": cmd_state}.get(req.get("command"))
        try:
            if handler is None:
                raise ValueError(f"unknown command: {req.get('command')}")
            resp = handler(req.get("payload") or {})
            resp.setdefault("ok", True)
            resp["engine"] = VERSION
            out = {"id": rid, "ok": True, "response": resp}
        except Exception as e:
            out = {"id": rid, "ok": False, "error": f"{type(e).__name__}: {e}"}
        stdout.write(json.dumps(out, ensure_ascii=True).encode() + b"\n")
        stdout.flush()


# ---------------------------------------------------------------- main


def main():
    if len(sys.argv) < 2:
        sys.stdout.write(json.dumps({"ok": False, "error": "usage: kb_engine.py <ingest|search|rag|stats|zotero|dedup|clear|fetch|reload|state|serve>"}))
        return 1
    command = sys.argv[1]
    if command == "serve":
        cmd_serve()
        return 0
    if command == "run_job":          # 后台任务子进程入口（argv 传 job 文件，不走 stdin）
        run_async_job(sys.argv[2] if len(sys.argv) > 2 else "")
        return 0
    handler = {"ingest": cmd_ingest, "ingest_async": cmd_ingest_async, "status": cmd_status,
               "search": cmd_search, "rag": cmd_rag,
               "stats": cmd_stats, "zotero": cmd_zotero,
               "dedup": cmd_dedup, "clear": cmd_clear, "fetch": cmd_fetch,
               "reload": cmd_reload, "state": cmd_state}.get(command)
    if handler is None:
        sys.stdout.write(json.dumps({"ok": False, "error": f"unknown command: {command}"}))
        return 1
    try:
        raw = sys.stdin.buffer.read()
        req = json.loads(raw.decode("utf-8")) if raw.strip() else {}
    except Exception as e:
        sys.stdout.write(json.dumps({"ok": False, "error": f"bad request JSON: {e}"}))
        return 1
    try:
        resp = handler(req)
        resp.setdefault("ok", True)
        resp["engine"] = VERSION
        sys.stdout.write(json.dumps(resp, ensure_ascii=True))
        return 0
    except Exception as e:
        sys.stdout.write(json.dumps(
            {"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=True))
        return 1


if __name__ == "__main__":
    sys.exit(main())
