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

VERSION = "3.5.0"   # 1.6.9：三线表 + 数值条/趋势图/流程链/关键数字（可视块）
SUPPORTED_EXTS = {".pdf", ".txt", ".md", ".markdown", ".docx"}

# 解析器版本：**只在改动会写进库的解析逻辑时 +1**（分块、元数据/标识符抽取、引文切分）。
# 每条 docs 记录写入 indexed_with；kb_stats 报告 stale_docs —— 因为增量入库按 sha256
# 跳过未变文件，引擎的解析改进不会自动作用于老库（实测：一处抽取改动漏了 49 篇的 DOI，
# 直到一次全量重灌才暴露）。注意判定只看 rev，不看引擎 VERSION：否则每次发版都会把
# 整个库标成陈旧，提示就变成噪音。
PARSER_REV = 5
PARSER_TOKEN = "%s/rev%d" % (VERSION, PARSER_REV)
# 构建指纹：把引擎自身字节的摘要并入**响应缓存 key**。
# 为什么需要：PARSER_TOKEN 只在版本号/解析器版本变化时才变，而"同版本重构建"
# （改补丁、重跑 build_v169.py）版本号通常不动 —— 旧响应会被原样喂回（实测：
# 放宽相关性闸门后，工具侧仍返回旧的"无关"结论，必须手动 DELETE FROM cache 才生效）。
# 文件摘要让任何字节级改动自动失效，跨版本升级也一并覆盖，无需再记"发版要清缓存"。
try:
    CACHE_SALT = hashlib.sha1(Path(__file__).read_bytes()).hexdigest()[:12]
except Exception:
    CACHE_SALT = PARSER_TOKEN
# 哪些 rev 改动了**分块/向量**（而不只是元数据）。升级提示据此决定给用户哪个建议：
# `metadata_only`（秒级刷元数据）对分块类改动**无效** —— 给一个无效选项比不给更糟。
# rev 5：References 判定护栏 + 整篇不可检索兜底（会改变分块与向量）。
CHUNK_AFFECTING_REVS = {4, 5}


def _load_workspace_engine_env():
    """工作区级引擎设置：`<工作区>/.kb-rag/engine.env` 的 KEY=VALUE 合并进 os.environ。

    为什么需要：换嵌入模型、换镜像这类设置应该跟着"库/工作区"走，而不是要求用户改
    系统环境变量再重启 DSH —— 后者在桌面版里几乎不可行。守护进程的 cwd 是工作区根，
    插件、MCP、CLI 三种入口都成立。已存在的环境变量优先（显式设置 > 工作区文件 >
    内置默认），所以临时排障仍可用 `KB_EMBED_MODEL=... python kb_engine.py ...` 覆盖。

    只读一次、失败静默：配置文件坏了不能连带把引擎启动弄挂。
    """
    try:
        path = Path(os.getcwd()) / ".kb-rag" / "engine.env"
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError:
        pass


_load_workspace_engine_env()


# B8：下载超时必须早于 huggingface_hub / sentence_transformers 的 import。
# hub 在 import 时把 HF_HUB_DOWNLOAD_TIMEOUT 固化进 constants；之后再 setdefault
# 只改环境变量，不会改已固化的常量（实测 env=600、实际仍 10s）。
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "600")


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
# 条目编号：允许字母前缀（补充材料常用 "S1." / "[S1]"，实测某篇整张文献表都是这种编号）
_REF_NUM = r"[A-Za-z]?\d{1,3}"


def _ref_num_key(raw):
    """编号串 → 整数键：'S12' → 12，'12' → 12；取不到数字返回 None。"""
    m = re.search(r"\d+", raw or "")
    return int(m.group(0)) if m else None


# 参考文献条目风格：'1. Author' / '1 Author' / '[1] Author'（Wiley） / '1Author'（紧贴式）
_REF_ENTRY_BRACKET_RE = re.compile(r"(?m)^\s*\[\s*(" + _REF_NUM + r")\s*\]\s+(?=\S)")
# 引文链条目行（stage-3 链式检测用）：'[N] ...' / 'N.\t...' / 'N.' 独占行 / 'N. Author...'
_REF_CHAIN_ENTRY_RE = re.compile(r"^\s*(?:\[\s*(" + _REF_NUM + r")\s*\]|(" + _REF_NUM + r")\.)(?:\s|\t|$)")
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


# 任意位置的"编号 + 大写/汉字起始"：用于识别**被抽成一大段**的文献表（条目不在行首）。
# 三种编号都要认：[N] / N. / N) / 紧贴式 '1J. Valasek'、'1Smith'（后者用 (?=[A-Z](?:\.|[a-z])) 限定，
# 这样 '2D materials' / '3D printing' / '4H-SiC' 仍被排除）。
_REF_INLINE_ENTRY_RE = re.compile(
    r"(?:^|[\s\n])(?:\[\s*(" + _REF_NUM + r")\s*\]|(" + _REF_NUM + r")[.)]|("
    + _REF_NUM + r")(?=[A-Z](?:\.|[a-z])))\s*(?=[A-Z\u4e00-\u9fff])")


def _ref_para_is_list(para):
    """整段是不是**文献表**（巨段版式专用，不要求编号严格递增）。

    实测有文档的文献表被抽成一两个巨段，且双栏排版让编号在抽取顺序上交叉（1,4,2,5…），
    递增链检测天然失效 —— 某篇 59 条、另一篇 54 条因此整表丢失。这里改用**密度 + 条目样比例**：
      ① 任意位置的"编号 + 大写起始"出现 ≥5 次，且每千字符密度 ≥3；
      ② 其中过半（至少 3 条）通过 _ref_entry_strong（人名在首位/年份/期刊卷页 DOI）。
    正文段落几乎不可能同时满足这两条（正文里编号条目稀疏、后面也不跟文献信息）。"""
    t = (para or "").strip()
    if len(t) < 400:
        return False
    ms = list(_REF_INLINE_ENTRY_RE.finditer(t))
    if len(ms) < 5:
        return False
    if len(ms) / max(0.001, len(t) / 1000.0) < 3.0:
        return False
    hits = 0
    for i, m in enumerate(ms):
        nxt = ms[i + 1].start() if i + 1 < len(ms) else min(m.end() + 250, len(t))
        if _ref_entry_strong(t[m.end():nxt]):
            hits += 1
    return hits >= max(3, int(len(ms) * 0.5))


def _inline_ref_spans(text, min_run=5, max_gap=1200):
    """识别**巨段版式**的文献表：在任意位置找"编号递增 + 条目像文献"的序列。

    实测有文档的整张文献表被 PDF 抽成 1–2 个巨段（条目不在行首、还会在词中间换行），
    行首锚定的链检测完全看不到 —— 某篇 190 条、另一篇 54 条因此整表丢失、引文关联失效。
    返回 [(起点字符位, 终点字符位)]。"""
    entries = [(m.start(0), _ref_num_of(m))
               for m in _REF_INLINE_ENTRY_RE.finditer(text or "")]
    runs, cur = [], []
    for pos, num in entries:
        if cur and num == cur[-1][1] + 1 and pos - cur[-1][0] < max_gap:
            cur.append((pos, num))
        else:
            if len(cur) >= min_run:
                runs.append(cur)
            cur = [(pos, num)]
    if len(cur) >= min_run:
        runs.append(cur)
    spans = []
    for ch in runs:
        hits = 0
        for k, (pos, _num) in enumerate(ch):
            nxt = ch[k + 1][0] if k + 1 < len(ch) else len(text)
            if _ref_entry_strong(text[pos:min(nxt, pos + 250)]):
                hits += 1
        if hits >= max(3, int(len(ch) * 0.5)):
            spans.append((ch[0][0], min(len(text), ch[-1][0] + 400)))
    return spans


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
            num = _ref_num_of(m)
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
    # 阶段 3b：巨段版式的文献表（条目不在行首，行首链检测看不到）
    joined = "\n\n".join(paragraphs)
    spans = _inline_ref_spans(joined)
    if spans:
        out_p, ref_paras, out_g = _apply_ref_spans(paragraphs, pages, spans)
        if ref_paras:
            return out_p, ref_paras, out_g
    # 阶段 3c：巨段 + **编号不严格递增**（双栏抽取交叉）——按段判定"这段就是文献表"
    hits = [i for i, p in enumerate(paragraphs) if _ref_para_is_list(p)]
    if hits:
        return paragraphs, set(hits), pages
    return paragraphs, set(), pages


# 位置口径：一律按**字符**占比，不用段落序号（正文段长、文献条目段短，两种口径能差一倍）。
# 实测：同一批文档里，标题的段落位置 26%–57% 对应字符位置 34%–74%。
REF_HEAD_MIN_CHAR_POS = 0.5     # 有标题的 References 不会出现在前半段
REF_CHAIN_MIN_CHAR_POS = 0.3    # 无标题的递增引文链起点门槛（Nature 系正文 refs 可能较早）
# 整篇不可检索时的兜底：写回哪一节的下限（太短的段落救出来也只是噪声）。
RESCUE_MIN_CHARS = 800
#
# 两条**试过并被实测否掉**的路线（留档，别再走一遍）：
#   ① REF_MAX_CHAR_SHARE（"参考文献超过 60% 字符就整段作废"）：综述类大文献表（某篇 190 条、
#      某篇 237 条）被一起丢掉，引文关联整表失效；
#   ② 逐段"散文退回"（把 References 区里明显散文的段落退回正文）：依赖"这段没检出条目"，
#      而条目样式认不全（`(1)`、`【1】`、作者-年份式都不认）→ 真文献表被误退。
# 三条路线的全库实测（316 篇，见 _cites_compare.py）：
#   有上限 + 无散文退回：命中率 79.2%，改后失效 11 篇
#   去上限 + 巨段通道 + 紧贴式修复：**81.6%**，失效 5 篇 ← 现在采用的组合
#   再去上限 + 开启散文退回：79.8%，失效 13 篇
# 正文被吞的代价由 RESCUE_MIN_CHARS 那条兜底承担：任何文档都不会从检索里消失。


def _ref_num_of(m):
    """从 _REF_INLINE_ENTRY_RE 的匹配里取编号（三个可选分组任一）。"""
    return _ref_num_key(m.group(1) or m.group(2) or m.group(3))


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
    # 注：这里曾有一道"把 References 区里明显是散文的段落退回正文"的工序（配合删除的
    # REF_MAX_CHAR_SHARE 上限），目的是让判定跑偏时正文还能被检索。**实测撤掉了**：
    #   状态①（有占比上限）：命中率 79.2%（引文关联），失效 11 篇
    #   状态②（去上限 + 巨段通道 + 紧贴式修复）：命中率 **82.0%**，失效 4 篇 ← 最优
    #   状态③（再开启散文退回）：命中率 79.8%，失效 13 篇
    # 原因是"散文"判据依赖"这段没检出条目"，而条目样式认不全（`(1)`、`【1】`、作者-年份式都不认），
    # 真文献表被误退 → 引文关联整表失效。正文被吞的代价由"整篇不可检索兜底"（见下）承担，
    # 那条兜底保证任何文档都不会从检索里消失。

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
    # 库级元信息（key/value）：目前只存 embed = {model, dim}。用 IF NOT EXISTS 建表，
    # 不需要升 SCHEMA_VERSION，也不会动老库的数据。
    db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
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

# 镜像可换：KB_HF_MIRROR 优先，其次 HF_ENDPOINT（可能指向别的镜像），最后用默认值。
_HF_MIRROR = (os.environ.get("KB_HF_MIRROR")
              or os.environ.get("HF_ENDPOINT")
              or "https://hf-mirror.com")


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
_PROBE = None       # 真实可用性探测结果：torch 说"有卡"不等于"能算"（None = 还没探过）
_VRAM_GB = None     # 显存总量（GB，缓存；None = 查不到）
_BATCH_EFFECTIVE = {}   # tag -> 实测能跑通的批大小（缩过一次就记住，后续调用不再重复撞）
# 设备异常说明：{tag: 说明}，例如 {"embed": "GPU 不可用（…）→ 已改用 CPU"}，让 kb_stats/kb_ingest 看得到。
# 用 dict 而不是单字符串：embed 与 rerank 各记各的，且**正常加载成功时能清掉**（否则一次瞬时故障
# 会让"已回退 CPU"永远挂在报告里，用户以为现在还在 CPU 上跑）。
_DEVICE_NOTE = {}


def _probe_device():
    """该在哪个设备上做可用性探测（与 _device_kwargs 的实际选择保持一致）。"""
    if KB_DEVICE.startswith("cuda"):
        return KB_DEVICE                      # 含 cuda:1 这类显式序号
    if KB_DEVICE == "mps":
        return "mps"
    return "cuda"


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


def _gpu_probe():
    """**真实算一次**再决定要不要用 GPU（每进程一次，结果缓存）。

    为什么需要：`torch.cuda.is_available()` 只说明"驱动报告有设备"，不保证能跑内核。
    实测能遇到的坑：驱动与 CUDA runtime 版本不匹配、容器/WSL 里设备可见但初始化失败、
    显卡被独占计算模式占用、cuDNN/cuBLAS 缺库。这些在 `is_available()==True` 时照样发生，
    若不先探，失败点会被推到"加载模型时"——代价更大、报错更难懂（三级加载链还会在 GPU 上
    连撞几次）。这里用一个 8×8 矩阵乘 + `.item()`（会隐式同步）做最小可用性验证，失败即粘性
    关闭 GPU 并把原因写进报告。`KB_GPU_PROBE=0` 可跳过探测（极端环境下想强行试 GPU 时用）。

    注意：探测本身会初始化 CUDA context（约 0.3–1 s），但这一步在"模型要上 GPU"时本来
    也要付；不加载模型、只体检（kb_stats）的路径不会碰它（见 device_report 的 torch 判断）。"""
    global _PROBE
    if _PROBE is not None:
        return _PROBE
    raw = (os.environ.get("KB_GPU_PROBE") or "1").strip().lower()
    if raw in ("0", "false", "off", "no"):
        _PROBE = True                     # 跳过探测：交给模型加载期兜底（_load_with_cpu_fallback）
        return _PROBE
    target = _probe_device()
    try:
        import torch
        if target.startswith("cuda") and not torch.cuda.is_available():
            _PROBE = False
            return _PROBE
        t = torch.zeros(8, 8, device=target)
        float((t @ t).sum().item())        # 真正下发一个内核并同步，光分配内存不算
        _PROBE = True
    except Exception as e:                 # noqa: BLE001 —— 设备侧任何异常都当"用不了"
        _PROBE = False
        _disable_gpu("probe", e)
        return _PROBE
    return _PROBE


def _vram_gb():
    """显存总量（GB）。查不到返回 None（不猜、不 import torch 副作用之外的任何东西）。"""
    global _VRAM_GB
    if _VRAM_GB is not None:
        return _VRAM_GB
    try:
        import torch
        if _cuda_available():
            props = torch.cuda.get_device_properties(0)
            total = getattr(props, "total_memory", None)
            if total:
                _VRAM_GB = round(float(total) / (1024 ** 3), 1)
    except Exception:                                          # noqa: BLE001
        _VRAM_GB = None
    return _VRAM_GB


def _gpu_usable(probe=True):
    """本进程还能不能尝试 GPU。一旦判定不可用就永久返回 False（sticky；`cmd_reload` 可重置）。

    与旧实现的关键差别：不再只信 `torch.cuda.is_available()`，而是**真算一次**（`_gpu_probe`）。
    显式 `cuda*` / `mps` 同样先探 —— 探测失败比"加载模型时才撞墙"便宜得多，也更早给出可读原因。

    `probe=False` 只回答"按当前已知信息能不能用"，**不触发探测**（也不因此关闭 GPU）：
    只读体检（kb_stats / device_report）不该有副作用 —— 一次瞬时故障不该被一次查询体检固化下来。"""
    if _GPU_OK is False:
        return False
    if KB_DEVICE == "cpu":
        return False
    if KB_DEVICE in ("", "auto", "none"):
        if not _cuda_available():
            return False
        return _gpu_probe() if probe else (_PROBE is not False)
    return _gpu_probe() if probe else (_PROBE is not False)


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
                   "driver", "no available kernel", "not compiled with cuda",
                   "mps", "rocm")     # 只加"几乎不会出现在别处"的词；"hip"/"metal" 会误伤 "chip"/"metallurgy"


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


def _model_on_gpu(model):
    """模型**实际**在哪。批大小默认值跟着它走，而不是跟着"策略上能不能用 GPU"：
    运行期回退 CPU 之后策略位仍是 True，只有模型自己的 device 说了实话。"""
    dev = str(getattr(model, "device", "") or "").lower()
    if "cuda" in dev or dev == "mps":
        return True
    if dev:
        return False                    # 明确的 cpu / cpu:0
    return _gpu_usable()                # 某些包装类没有 device → 退回策略判定


_BATCH_TIERS = {                        # 显存下限（GB） → 批大小；不在表内 = 用 gpu_default
    # 阈值只用来**保护小显存**，不惩罚正常显卡：实测 8GB 卡跑 128 没问题（见 BACKLOG §5.1 的吞吐基准），
    # 所以 8GB 及以上保持历史默认，只有 <8GB 才降级；<4GB（共享显存的轻薄本/MX 系）再降一档。
    "embed": ((4.0, 16), (8.0, 64)),
    "rerank": ((4.0, 8), (8.0, 32)),
}


def _gpu_batch(tag, gpu_default):
    """按显存给 GPU 批大小：小显存从小批起步，避开"先 OOM 再缩批"这条又慢又吵的路径。
    显存查不到时用历史默认（不改变未知环境的行为）。"""
    vram = _vram_gb()
    if vram is None:
        return gpu_default
    for limit, bs in _BATCH_TIERS.get(tag, ()):
        if vram < limit:
            return bs
    return gpu_default


def _free_cuda_cache():
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


_BATCH_DEFAULTS = {"embed": (32, 128), "rerank": (16, 64)}


def _shrink_ladder(bs):
    """缩批梯次：折半 → 下限 4。不像"一次掉到 4"那样白丢吞吐，也不做无谓的长链重试。"""
    out = []
    cand = max(4, bs // 2)
    if cand < bs:
        out.append(cand)
    if 4 < bs and 4 not in out:
        out.append(4)
    return out


def _with_device_retry(tag, model, run):
    """run(batch_size) → 结果。设备侧故障逐级兜底（每级都只对设备类故障生效）：
    ① 清 CUDA 缓存后**折半**重试；② 折半仍不行 → 缩到 4 再试一次；
    ③ 还不行 → 把模型移到 CPU、批大小回到 **CPU 默认**再试；④ 仍不行才把异常抛出去。
    能跑通的批大小会被**记住**（同一进程后续调用直接从它开始，不再重复撞同一堵墙），
    批大小的默认值跟着**模型实际所在设备**走（回退 CPU 后不会继续用 GPU 的 128）。
    覆盖的不只是 OOM，也包括运行中才暴露的 CUDA 错（内核不匹配、cuBLAS/cuDNN 报错、设备丢失）——
    否则一次瞬时故障就等于"这个会话再也用不了向量"（与 issue #2 的永久缓存同一个坑）。
    非设备故障（数据/格式问题）不兜底，原样上抛。"""
    cpu_default, gpu_default = _BATCH_DEFAULTS.get(tag, (16, 16))
    on_gpu = _model_on_gpu(model)
    key = (tag, "gpu" if on_gpu else "cpu")
    want = _gpu_batch(tag, gpu_default) if on_gpu else cpu_default
    bs = _BATCH_EFFECTIVE.get(
        key, _batch_size("KB_%s_BATCH" % tag.upper(), cpu_default, want))
    try:
        out = run(bs)
        _BATCH_EFFECTIVE.setdefault(key, bs)
        return out
    except Exception as e:
        if not _is_cuda_error(e):
            raise
        _free_cuda_cache()
        for cand in _shrink_ladder(bs):
            try:
                out = run(cand)
                _BATCH_EFFECTIVE[key] = cand          # 记住这次跑通的批大小
                return out
            except Exception as e2:
                if not _is_cuda_error(e2):
                    raise
                _free_cuda_cache()
        if not _fallback_cpu(model, tag):
            raise
        _BATCH_EFFECTIVE[(tag, "cpu")] = cpu_default
        return run(cpu_default)


# 旧名（补丁里叫 _with_oom_retry）：保留以免外部验证脚本失效
_with_oom_retry = _with_device_retry


def _set_device_note(tag, text):
    """记录/清除某条链路的设备异常说明（text=None 表示清除）。"""
    if text:
        _DEVICE_NOTE[tag] = text
    else:
        _DEVICE_NOTE.pop(tag, None)


def _device_note_text():
    # "probe" 也要带上：加载期回退与探测失败是两条不同的路径，用户看 note 时两条都该可见
    return "；".join("%s: %s" % (t, _DEVICE_NOTE[t])
                     for t in ("probe", "embed", "rerank") if t in _DEVICE_NOTE) or None


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


def _batch_report():
    """批大小是怎么定下来的：环境变量 > 实测记忆 > 显存分级 > 设备默认。"""
    out = {}
    for tag in ("embed", "rerank"):
        cpu_d, gpu_d = _BATCH_DEFAULTS.get(tag, (16, 16))
        item = {"cpu_default": cpu_d, "gpu_default": gpu_d,
                "env": os.environ.get("KB_%s_BATCH" % tag.upper())}
        if _cuda_available():
            item["gpu_tier"] = _gpu_batch(tag, gpu_d)
        eff = _BATCH_EFFECTIVE.get((tag, "gpu")) or _BATCH_EFFECTIVE.get((tag, "cpu"))
        if eff:
            item["effective"] = eff
        out[tag] = item
    return out


def device_report():
    """现在实际跑在哪、为什么 —— 让"装了 GPU 却没吃上"、"GPU 用不了已退 CPU"、"OOM 已回退"都可见。
    torch 未加载时不主动 import（kb_stats 要保持便宜），只报已加载模型的实际 device。"""
    info = {"requested": KB_DEVICE}
    if _GPU_OK is False:
        info["gpu_usable"] = False       # 已判定：不需要碰 torch 就能答
        if _GPU_WHY:
            info["gpu_disabled_reason"] = _GPU_WHY
    if _PROBE is not None:
        info["probe"] = "ok" if _PROBE else "failed"   # 真实算过一次的结果（不是 is_available）
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
            info.setdefault("gpu_usable", _gpu_usable(probe=False))   # 体检不触发探测（无副作用）
            if info["cuda_available"]:
                try:
                    info["gpu"] = torch.cuda.get_device_name(0)
                except Exception:
                    pass
                vram = _vram_gb()
                if vram:
                    info["vram_gb"] = vram
            info["batch"] = _batch_report()
        except Exception as e:
            info["torch_error"] = "%s: %s" % (type(e).__name__, str(e)[:120])
    for tag, model in (("embed", _EMBEDDER), ("rerank", _RERANKER)):
        if model is not None:
            dev = getattr(model, "device", None)
            if dev is not None:
                info[tag + "_device"] = str(dev)
    return info


def _ensure_hf_download_timeout():
    """把下载超时同时写进 env 与 hub 常量；必须在使用 hub 之前调用。"""
    os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "600")
    try:
        import huggingface_hub.constants as _hfc
        _hfc.HF_HUB_DOWNLOAD_TIMEOUT = int(os.environ["HF_HUB_DOWNLOAD_TIMEOUT"])
    except Exception:
        pass


_HF_ENDPOINT_SAVED = None


def _apply_hf_mirror():
    """Switch huggingface_hub to the CN mirror after a direct-download failure.
    HF_ENDPOINT is baked into huggingface_hub.constants at import time (ENDPOINT
    and the derived HUGGINGFACE_CO_URL_TEMPLATE), so setting os.environ alone is
    a no-op once hub is imported — patch the constants instead.

    保存原始 endpoint，镜像也失败时用 _restore_hf_endpoint() 回退，避免一次
    镜像失败把进程永久锁在镜像上（B18）。"""
    global _HF_ENDPOINT_SAVED
    if _HF_ENDPOINT_SAVED is None:
        saved = {"env": os.environ.get("HF_ENDPOINT")}
        try:
            import huggingface_hub.constants as _hfc
            saved["endpoint"] = getattr(_hfc, "ENDPOINT", None)
            saved["template"] = getattr(_hfc, "HUGGINGFACE_CO_URL_TEMPLATE", None)
        except Exception:
            pass
        _HF_ENDPOINT_SAVED = saved
    os.environ["HF_ENDPOINT"] = _HF_MIRROR
    try:
        import huggingface_hub.constants as _hfc
        _hfc.ENDPOINT = _HF_MIRROR
        _hfc.HUGGINGFACE_CO_URL_TEMPLATE = _HF_MIRROR + "/{repo_id}/resolve/{revision}/{filename}"
    except Exception:
        pass


def _restore_hf_endpoint():
    """恢复 _apply_hf_mirror() 之前的 endpoint；镜像失败后下一次 retry 回到直连。"""
    global _HF_ENDPOINT_SAVED
    saved = _HF_ENDPOINT_SAVED
    if saved is None:
        return
    if saved.get("env") is None:
        os.environ.pop("HF_ENDPOINT", None)
    else:
        os.environ["HF_ENDPOINT"] = saved["env"]
    try:
        import huggingface_hub.constants as _hfc
        if saved.get("endpoint") is not None:
            _hfc.ENDPOINT = saved["endpoint"]
        if saved.get("template") is not None:
            _hfc.HUGGINGFACE_CO_URL_TEMPLATE = saved["template"]
    except Exception:
        pass
    _HF_ENDPOINT_SAVED = None

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
        _ensure_hf_download_timeout()
        from sentence_transformers import SentenceTransformer
        # 慢网/受限网络：允许直接镜像优先，不再先撞直连；下载超时可拉长。
        if os.environ.get("KB_HF_MIRROR_FIRST", "").lower() in ("1", "true", "yes"):
            _apply_hf_mirror()
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
        try:
            return SentenceTransformer(name, **dkw)
        except Exception:
            _restore_hf_endpoint()   # 镜像也不行：回到直连，留给下次 retry
            raise

    try:
        model, fell_back = _load_with_cpu_fallback("embed", _build)
        # 上下文长度上限：bge-m3 之类长上下文模型的 max_seq_length 默认是 8192，
        # 而分块只有几百字符 —— 不截断会把每块 padding 到 8192 token，实测吞吐掉到
        # 0.8 块/秒、显存吃满只能 batch=1（一次全量重灌要 7 小时）。512 足够覆盖现有
        # 分块（最长约 1000 字符），可用 KB_EMBED_MAX_SEQ 覆盖。
        try:
            _seq = int(os.environ.get("KB_EMBED_MAX_SEQ", "512"))
            if _seq > 0 and hasattr(model, "max_seq_length"):
                model.max_seq_length = _seq
        except Exception:
            pass
        _EMBEDDER = model
        _EMBED_NAME = name
        _EMBED_ERR = None          # 之前失败过、这次成功：清掉旧错误，kb_stats 不再报
        if not fell_back:
            _set_device_note("embed", None)   # 正常加载成功 → 旧的"已退 CPU"记录不再成立
    except Exception as e:
        _EMBED_ERR = (f"{type(e).__name__}: {e}"[:200] +
                      "；模型未下载或下载失败。慢可设 HF_ENDPOINT=https://hf-mirror.com"
                      "（或代理）后重启 DSH，或先预下载模型、复制 HF 缓存；"
                      "也可用 KB_HF_MIRROR_FIRST=1 让镜像优先。")[:300]
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
        _ensure_hf_download_timeout()
        from sentence_transformers import CrossEncoder

        def _build(**dkw):
            if os.environ.get("KB_HF_MIRROR_FIRST", "").lower() in ("1", "true", "yes"):
                _apply_hf_mirror()
            try:
                return CrossEncoder(name, **dkw)
            except Exception as e:
                if _is_cuda_error(e):
                    raise                     # 设备问题：不要在同一条路上连撞两次
                _apply_hf_mirror()
                try:
                    return CrossEncoder(name, **dkw)
                except Exception:
                    _restore_hf_endpoint()
                    raise

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
        # 纯补向量（先灌文本、后装模型）时 added/updated/meta_changed 都是 0，
        # 但库已经能提供向量检索了；不失效的话同一查询会继续返回补向量前的降级答案（issue #6）。
        if (totals["added"] or totals["updated"] or totals["meta_changed"]
                or totals.get("vectors", 0) > 0):
            db.execute("DELETE FROM cache")
        vectors_missing = db.execute(
            "SELECT COUNT(*) AS n FROM chunks c LEFT JOIN vecs v ON v.chunk_id = c.id "
            "WHERE v.chunk_id IS NULL AND c.weight > 0").fetchone()["n"]
        db.commit()
    finally:
        db.close()
    # 同进程内的写入：立刻清掉内存缓存。跨进程的写入（另一个 CLI / 后台子进程在写同一个库）
    # 由 _search_core 的库文件指纹兜住，所以这里不是唯一防线，但是最快最确定的一层。
    _invalidate_caches("ingest added=%d updated=%d meta=%d vectors=%d"
                       % (totals["added"], totals["updated"], totals["meta_changed"],
                          totals.get("vectors", 0)))
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


def _kb_embed_meta(db):
    """库内记录的嵌入模型/维度；没有记录时返回 None（老库、纯关键词库）。"""
    try:
        row = db.execute("SELECT value FROM meta WHERE key = 'embed'").fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    try:
        data = json.loads(row["value"])
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _record_embed_meta(db, vec):
    """写向量时记录模型名与维度：检索期据此判断"库和模型是否配套"。"""
    try:
        dim = len(vec) if vec is not None else 0
        if not dim:
            return
        payload = json.dumps({"model": _EMBED_NAME or "",
                              "dim": int(dim)}, ensure_ascii=False)
        db.execute("INSERT INTO meta(key, value) VALUES('embed', ?) "
                   "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (payload,))
    except Exception:
        pass


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
    _record_embed_meta(db, vecs[0] if len(vecs) > 0 else None)
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
            # 与 skipped 分支同理：内容已在别处入库 ≠ 那篇的向量是齐的。模型不可用时入库过的
            # 文档同样会 0 向量，这里也补一次缺失向量（否则"重复内容"永远补不上，
            # 用户只能 rebuild=true 全量重灌）。
            n_vec = _embed_new_chunks(db, dup["id"])
            entry.update({"status": "duplicate", "of": dup["path"],
                          "vectors": n_vec,
                          "ms": round((time.time() - t0) * 1000)})
            totals["duplicates"] = totals.get("duplicates", 0) + 1
            totals["vectors"] += n_vec
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


def keyword_ranking(rows, query, ckey=None):
    """BM25 x section weight over candidate chunks; returns ranked (index, score).

    R9：`lowered` / `avgdl` 是 O(语料) 的重复计算（实测 20k 块里 BM25 是热查询最大的
    非模型开销，302 ms）。这里按语料签名缓存小写文本与平均长度，并把"df 一遍 + tf 一遍"
    合并成一遍 —— **打分公式与结果完全不变**（`count(term) > 0` 等价于 `term in text`），
    只是不再每个查询重算。`ckey=None` 时不缓存（保持旧调用方行为）。"""
    terms = extract_terms(query)
    if not terms:
        # 返回三元组（与正常路径一致）：原先这里返回二元组，而 _search_core 按三元组解包，
        # 于是一个"只有停用词"的查询（如 'the of and'）会抛 ValueError、整次检索返回错误响应。
        return [], "无法从 query 解析出可检索的关键词", None
    n = len(rows)
    ent = _BM25_CACHE.get(ckey) if ckey is not None else None
    if ent is not None and ent["n"] == n:
        lowered, avgdl = ent["lowered"], ent["avgdl"]
    else:
        texts = [r["text"] for r in rows]
        lowered = [t.lower() for t in texts]
        avgdl = sum(len(t) for t in texts) / max(1, n)
        if ckey is not None and n <= KB_CORPUS_CACHE_MAX:
            _cache_put(_BM25_CACHE, ckey, {"lowered": lowered, "avgdl": avgdl, "n": n})
    texts = [r["text"] for r in rows]
    k1, b = 1.2, 0.75
    scores = [0.0] * n
    best_term = [None] * n
    best_idf = [0.0] * n
    for term, _kind, tw in terms:
        counts = [lt.count(term) for lt in lowered]        # 一遍同时拿到 df 与 tf
        df = sum(1 for c in counts if c > 0)
        if df == 0:
            continue
        idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
        for i, tf in enumerate(counts):
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


def _relevance_signals(rows, query, ckey=None):
    """行为信号：missing 与 best_doc（见 KB_BEST_DOC_FLOOR 处的说明）。

    这**不是**打分，是"查询的词面能否被这个库接住"的检查，因此不依赖精排——
    quick 模式（不跑精排）同样能拿到判定，这是修复"默认入口没有 verdict"的关键。

    分词用 extract_terms（与 BM25 同一套，含 CJK 二元组），df 与 idf 也用引擎自己的公式；
    小写文本复用 keyword_ranking 的 _BM25_CACHE，不重复小写化。
    返回 None 表示无法判定（查询解析不出实词）。
    """
    terms = list(dict.fromkeys(t for t, _k, _w in extract_terms(query)))
    if not terms:
        return None
    n = len(rows)
    ent = _BM25_CACHE.get(ckey) if ckey is not None else None
    if ent is not None and ent["n"] == n:
        lowered = ent["lowered"]
    else:
        lowered = [r["text"].lower() for r in rows]
    idf_of, doc_terms, missing = {}, {}, 0
    for term in terms:
        counts = [lt.count(term) for lt in lowered]
        df = sum(1 for c in counts if c > 0)
        if df == 0:
            missing += 1
            continue
        idf_of[term] = math.log(1 + (n - df + 0.5) / (df + 0.5))
        for i, tf in enumerate(counts):
            if tf:
                d = rows[i]["doc_id"]
                s = doc_terms.get(d)
                if s is None:
                    s = doc_terms[d] = set()
                s.add(term)
    idf_all = sum(idf_of.values())
    best = 0.0
    for ts in doc_terms.values():
        s = sum(idf_of[t] for t in ts)
        if s > best:
            best = s
    return {"missing": missing, "n_terms": len(terms),
            "best_doc": round(best / idf_all, 4) if idf_all > 0 else 0.0}


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
    r"\[(\s*[A-Za-z]?\d{1,3}(?:\s*[–\-]\s*[A-Za-z]?\d{1,3})?"
    r"(?:\s*,\s*[A-Za-z]?\d{1,3}(?:\s*[–\-]\s*[A-Za-z]?\d{1,3})?)*)\]")
_REF_ENTRY_RE = re.compile(r"(?m)^\s*(" + _REF_NUM + r")\s*(?:[\.\)]\s+|\s+)(?=\S)")
# 紧贴式条目：数字后紧跟「大写字母 +（点 或 小写字母）」：
#   '1J. Valasek, Phys. Rev. …'（Wiley 式）✓   '1Smith J., Nature…' ✓
#   '2D materials' / '3D printing' / '4H-SiC' / '3C-SiC'（大写后是空格或连字符）✗
# 上一版写成 (?=[A-Z][a-z]) 把 Wiley 的 '1J. …' 也排除了，实测某篇综述 237 条只剩 5 条。
_REF_ENTRY_TIGHT_RE = re.compile(r"(?m)^\s*(" + _REF_NUM + r")(?=[A-Z](?:\.|[a-z]))")
_CITE_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s,;\"'<>\)\]]+", re.I)
_CITE_YEAR_RE = re.compile(r"\((\d{4})\)\s*[.\s]*$")


def _expand_incite_nums(group, cap=60):
    """"1", "1-3", "1,2,5", "1–3,7" -> 引文编号展开列表。"""
    nums = []
    for part in group.split(","):
        # 允许字母前缀（补充材料里的 [S1]/[S1–S3]）；展开结果只保留数字部分
        m = re.match(r"\s*[A-Za-z]?(\d{1,3})(?:\s*[–\-]\s*[A-Za-z]?(\d{1,3}))?\s*$", part)
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
                key = _ref_num_key(m.group(1))
                if key is not None:
                    refs[key] = body[:cap]
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
# ── 相关性判定（3.3.0，见 docs/BACKLOG.md §2.12）────────────────────────────────
# 旧判据是「池内 ≤25 块的**裸精排分 max** ≥ 0.10」，而 max 会被**单个离群块**劫持
# （BACKLOG §2.12 根因 1：命中块常是 front matter / 致谢 / 作者串这类
# "不承诺具体内容"的块，cross-encoder 给它们中等偏正分）。
#
# 现在两道闸门：
#   ① missing 闸门 —— 查询实词在语料里 df=0 的个数 ≥1 → 无关。
#      不依赖精排，所以 quick（kb_search 的默认入口）也有判定（过去恒为 null）。
#   ② 过地板块数闸门 —— 精排前 5 名里**过地板(0.10)的块数 < 2** → 无关。
#      单个离群块再也决定不了结论；要判"相关"至少要有两块独立证据。
#
# 58 条标定（316 篇真实库，见 calib/rerank_judgment.py）：
#    旧规则(raw max ≥0.10)        库内降级 0/20 ｜ 库外漏出 11/30
#    只上 missing 闸门             库内降级 0/20 ｜ 库外漏出 11/30（deep 不变，quick 从 0 变 19）
#    只上 块数闸门(<2)             库内降级 0/20 ｜ 库外漏出  5/30
#    **两道合用**                  库内降级 0/20 ｜ 库外漏出  1/30
# 唯一漏出的 `how do I calibrate a pH meter?` 前 5 块全高分（0.988/0.787/0.541/
# 0.430/0.223）—— 库里确实有 pH 计标定内容，属 BACKLOG 点名过的固有难例，不是误判。
#
# `best_doc`（查询 IDF 在单篇文档内的最大占比）**只暴露、不参与判定**：
# 本机语料含整本书（最大 1767 块），任意 6 个英文词都能在某一本里凑齐 → 恒为 1.0。
# 试过 8 类改进（密度/停用词/体积惩罚/窗口覆盖/章节过滤），无一能把两类分开，
# 最好一档的缝只有 0.003。详见 calib/ 下的标定脚本。
KB_MIN_RERANK = _env_float("KB_MIN_RERANK", 0.10)        # 裸精排分地板
KB_MIN_RERANK_WEAK = _env_float("KB_MIN_RERANK_WEAK", 0.35)   # 低于此值算"弱相关"
# 判"相关"所需的、精排前 5 名里过地板的**最少块数**（1 = 旧行为，单个离群块即可）
KB_RELEVANCE_MIN_CHUNKS = int(_env_float("KB_RELEVANCE_MIN_CHUNKS", 2))
# 逃生口：off 时关掉 missing 闸门（中文查询没有标定样本，引擎对中文走 CJK 二元组，
# missing 天然偏高，需要一条不改代码就能退回的路）
KB_RELEVANCE_RULE = os.environ.get("KB_RELEVANCE_RULE", "1").strip().lower() not in (
    "0", "false", "off", "no")
KB_BEST_DOC_FLOOR = _env_float("KB_BEST_DOC_FLOOR", 0.85)     # 仅标定入口，不参与判定
KB_RERANK_POOL = int(_env_float("KB_RERANK_POOL", 50))   # 精排候选池下限（top_k*5 取大）
KB_RERANK_CHARS = int(_env_float("KB_RERANK_CHARS", 1800))    # 每条候选喂给精排的字符数
# 单次查询扫描到多少块时给出"该做索引化了"的提示（只提示，不改行为）
KB_SCAN_WARN_CHUNKS = int(_env_float("KB_SCAN_WARN_CHUNKS", 100000))
# 查询响应缓存（cache 表）保留的最大行数：超过就按 created 修剪最旧的（每 200 次写入检查一次）
KB_QUERY_CACHE_MAX = int(_env_float("KB_QUERY_CACHE_MAX", 3000))
_CACHE_WRITES = 0
# 语料缓存（R9 的实用部分）：一次查询要把参与检索的分块（文本 + 向量）读进内存，
# 同一会话里反复查同一个库时这是纯重复劳动。缓存按 (库路径, 规模签名, 是否要向量, WHERE)
# 记账，签名变化（入库/清库/过滤条件变）即失效 —— 不改变任何排序结果，只省掉重复读取。
# 超过 KB_CORPUS_CACHE_MAX 块时不缓存（避免守护进程常驻几百 MB）。
KB_CORPUS_CACHE_MAX = int(_env_float("KB_CORPUS_CACHE_MAX", 60000))
# 缓存**条数**上限：key 里含过滤条件（where/args），而每条是整库分块的副本（文本 + 向量）。
# 不设上限时，用户试几种 filters 就能把守护进程撑到几百 MB —— 这是典型的"缓存自己变成问题"。
KB_CORPUS_CACHE_ENTRIES = int(_env_float("KB_CORPUS_CACHE_ENTRIES", 2))
KV_CORPUS_CACHE_ON = os.environ.get("KB_CORPUS_CACHE", "1").strip().lower() not in ("0", "false", "off", "no")
_CORPUS_CACHE = {}
# BM25 的小写文本 / 平均长度缓存（同一语料签名；见 keyword_ranking）
_BM25_CACHE = {}


def _db_file_signature(db_path):
    """库文件层面的变更指纹（主库 + WAL 的 mtime/大小）。

    为什么除了 SQL 侧签名还需要它：SQL 侧只看 chunks/docs 的规模与时间戳，**看不到
    "只改元数据字段"的更新** —— 实测 `metadata_only` 刷新后 title/authors/year 变了，
    但 MAX(chunks.id) / COUNT(docs) / MAX(indexed_at) 全都没变，检索继续返回旧值。
    文件指纹对任何一次提交都会变，也能覆盖**跨进程**写入（另一个 CLI 在往同一个库写时，
    进程内的失效钩子根本不会被调用）。"""
    sig = []
    for p in (db_path, db_path + "-wal"):
        try:
            st = os.stat(p)
            sig.append((st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append(None)
    return tuple(sig)


def _cache_put(store, key, value):
    """往内存缓存里放一条，并按 KB_CORPUS_CACHE_ENTRIES 淘汰最旧的插入项。

    key 里含过滤条件，每条又是整库分块的副本；不设条数上限时"多试几种 filters"就能把
    守护进程撑到几百 MB。淘汰按插入顺序（dict 保序），够用且零依赖。"""
    store[key] = value
    while len(store) > KB_CORPUS_CACHE_ENTRIES:
        oldest = next(iter(store))
        if oldest == key:
            break
        store.pop(oldest, None)


def _invalidate_caches(reason=""):
    """任何写库操作之后必须调用：内存里的语料 / BM25 / 中文占比 / 文档质心缓存都按"库内容"算。

    漏掉这一步的后果实测过（_test_cache_staleness.py）：元数据刷新后，同一守护进程里的
    检索仍返回旧的 title/authors/year，而 kb_stats 走另一条查询所以显示的是新值 ——
    这种"一半新一半旧"最难排查。"""
    n = (len(_CORPUS_CACHE) + len(_BM25_CACHE) + len(_CJK_SHARE_CACHE) + len(_REL_CENTROID))
    _CORPUS_CACHE.clear()
    _BM25_CACHE.clear()
    _CJK_SHARE_CACHE.clear()
    _REL_CENTROID.clear()
    if n and reason:
        _log("[kb-rag] 内存缓存已失效（%s）：语料/BM25/中文占比/质心共 %d 项" % (reason, n))
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
    # R9：每次查询要把参与检索的分块读进内存（BM25 需要文本、向量路需要向量），
    # 属于 O(库规模) 的设计。这里先做两件**零风险**的事：
    #   ① 纯关键词路不 JOIN vecs（省掉全部向量 BLOB）；
    #   ② 把扫描规模与读取耗时作为 scan 字段返回 —— 以前完全看不出来瓶颈在哪。
    # 十万块级需要真正的索引化（FTS5 / 倒排 + 向量分级），见 docs/BACKLOG.md。
    need_vec = (mode or "hybrid") != "keyword"
    vec_join = " LEFT JOIN vecs v ON v.chunk_id = c.id" if need_vec else ""
    cols = ("c.id AS cid, c.doc_id, c.text, c.section, c.weight, "
            "c.para_start, c.para_end, c.page_start, c.page_end, d.title, d.authors, "
            "d.year, d.journal, d.doi, d.path, d.kind, d.zotero_key" + (", v.vec" if need_vec else ""))
    t_load = time.time()
    corpus_hit = False
    rows = None
    try:
        db_path = str(db.execute("PRAGMA database_list").fetchall()[0][2] or "kb")
    except Exception:
        db_path = "kb"
    ckey = None
    if KV_CORPUS_CACHE_ON:
        try:
            # 签名要便宜又要可靠：
            #   MAX(chunks.id) 走主键 O(1)，任何"重灌/新增文档"都会抬升它；
            #   COUNT(docs) 只扫 docs（几百行），清库/去重会改变它；
            #   MAX(indexed_at) 覆盖"重灌但 id 未变"的情形；
            #   文件指纹（mtime/大小）覆盖**元数据字段更新**与**跨进程写入** —— 只靠前三项
            #   会漏掉 metadata_only 刷新（实测：检索返回旧 title）。
            # （不用 PRAGMA data_version：实测它只在同一连接内有效，新连接看不到他连接提交后的变化。）
            sig = db.execute("SELECT (SELECT COALESCE(MAX(id), 0) FROM chunks) AS mx, "
                             "(SELECT COUNT(*) FROM docs) AS dc, "
                             "(SELECT COALESCE(MAX(indexed_at), 0) FROM docs) AS ia").fetchone()
            ckey = (db_path, need_vec, int(sig["mx"]), int(sig["dc"]), float(sig["ia"]),
                    _db_file_signature(db_path), where, tuple(str(a) for a in args))
            cached = _CORPUS_CACHE.get(ckey)
            if cached is not None:
                rows = cached
                corpus_hit = True
        except Exception:
            ckey = None
    if rows is None:
        rows = db.execute(
            "SELECT " + cols + " FROM chunks c JOIN docs d ON d.id = c.doc_id" + vec_join + where,
            args).fetchall()
        if ckey is not None and len(rows) <= KB_CORPUS_CACHE_MAX:
            _cache_put(_CORPUS_CACHE, ckey, rows)
    load_ms = round((time.time() - t_load) * 1000)

    if not rows:
        # 这条早退路径过去缺 verdict/no_hit/closest/scan，上层渲染拿不到字段会退化成
        # "把原始 JSON 丢给模型"。字段在这里补齐，形状与正常路径一致。
        return {"query": query, "scored": 0, "results": [], "verdict": None, "no_hit": False,
                "max_score": None, "floor": None, "floor_weak": None, "closest": [],
                "missing": None, "best_doc": None, "relevance_rule": None,
                "note": "知识库为空或过滤条件过严。先用 kb_ingest 入库，或放宽 filters。",
                "ms": round((time.time() - t0) * 1000), "cached": False}

    # 维度守卫：库内向量与当前嵌入模型不配套时**明确报错**。
    # 旧行为是"向量检索失败 → 静默退成纯关键词"，用户只感觉到"检索变差了"。
    # 实测踩过：库用 384 维 MiniLM、引擎默认 512 维 bge-small，混跑且无任何提示。
    if mode != "keyword":
        _meta = _kb_embed_meta(db)
        if _meta and int(_meta.get("dim") or 0) > 0:
            try:
                _emb = get_embedder()
                _cur_dim = (_emb.get_sentence_embedding_dimension()
                            if _emb is not None else None)
            except Exception:
                _cur_dim = None
            if _cur_dim is not None and int(_cur_dim) != int(_meta["dim"]):
                # 抛异常而不是 return：cmd_serve 只在异常路径给顶层 ok:false，
                # return 会被包成 {"ok": true, "response": {...}}，宿主不会当错误处理。
                raise RuntimeError(
                    "库内向量为 %d 维（%s），当前嵌入模型输出 %d 维（%s），两者不可比。"
                    "把 KB_EMBED_MODEL 指回原模型，或用 kb_ingest(rebuild=true) 按当前模型重灌。"
                    % (int(_meta["dim"]), _meta.get("model") or "未记录",
                       int(_cur_dim), os.environ.get("KB_EMBED_MODEL", "内置默认")))

    # background zh->en translation is intentionally NOT wired in this build;
    # queries are searched as-is (CJK bigrams + ASCII terms both participate).

    cache_key = None
    if use_cache:
        # 向量数量并入 key（issue #6）：纯补向量不改 added/updated/meta_changed，但"有没有
        # 向量"会改变检索结果；COUNT(vecs) 变化即让旧响应自然失效，也覆盖跨进程补向量。
        vec_n = int(db.execute("SELECT COUNT(*) AS n FROM vecs").fetchone()["n"])
        cache_key = hashlib.sha1(json.dumps(
            [query, filters, top_k, snippet_w, mode, rerank_flag, RERANK_KEY_NAME, vec_n,
             related_flag, related_k,
             # 地板/精排参数会改变结论（verdict / 截断长度影响精排分），必须并入 key，
             # 否则改了阈值仍会复用旧响应（与"device=null 的旧缓存"同一类坑）
             KB_MIN_RERANK, KB_MIN_RERANK_WEAK, KB_RERANK_CHARS, KB_RERANK_POOL,
             # 行为判据的阈值与开关同样改变结论
             KB_BEST_DOC_FLOOR, KB_RELEVANCE_RULE, KB_RELEVANCE_MIN_CHUNKS,
             # 把解析器/引擎版本并入 key：升级后旧的缓存响应不该继续被复用
             # CACHE_SALT 是引擎字节摘要：同版本重建/热修也不会复用旧响应
             PARSER_TOKEN, CACHE_SALT],
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
    recall_idx = []

    if mode_used in ("keyword", "hybrid"):
        kw_ranked, best_term, _err = keyword_ranking(rows, query, ckey)
        if mode_used == "keyword":
            ranked = kw_ranked
        else:
            if not kw_ranked:
                note += "关键词无命中；"
            vecs_present = any(r["vec"] is not None for r in rows) if need_vec else False
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
                        # 召回侧留痕：两路各自的前 N 名。RRF 融合分会让"关键词强、
                        # 向量一般"的块掉队（实测：tip dragging 的命中块在 keyword 路
                        # 排第 5，混进 hybrid 后掉出 25 名池，精排根本没机会看到它）。
                        _pn = max(KB_RERANK_POOL, top_k * 5)
                        recall_idx = list(dict.fromkeys(
                            [i for i, _ in kw_ranked[:_pn]] + [i for i, _ in v_ranked[:_pn]]))
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
                ranked, best_term, _ = keyword_ranking(rows, query, ckey)
                note += _err2 + "，降级为纯关键词；"
            else:
                ranked = [(i, s, s) for i, s in ranked]     # 纯向量路：裸分 = 余弦
        except Exception as e:
            mode_used = "keyword"
            ranked, best_term, _ = keyword_ranking(rows, query, ckey)
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
            _pool_n = max(KB_RERANK_POOL, top_k * 5)
            # 候选池 = RRF 前 N ∪ 两路各自前 N：只取 RRF 前 N 会把关键词路的强命中
            # 挤出去（见上面 recall_idx 处的实测）。并集最多 2N，精排成本线性可控。
            cand_idx = list(dict.fromkeys([t[0] for t in ranked[:_pool_n]] + recall_idx))
            pool = [(i, 0.0, 0.0) for i in cand_idx]
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
    rule = None
    sig = _relevance_signals(rows, query, ckey) if KB_RELEVANCE_RULE else None
    # 精排前 5 名里过地板的块数：判定量的核心。用 max 会被单个离群块劫持（见常量处的注释）。
    over_floor = (sum(1 for t in ranked[:5] if t[2] >= KB_MIN_RERANK)
                  if reranker_used else None)
    # ── 相关性地板：只认精排分（quick 的 RRF 分无量纲，不参与判定）──────────────
    # missing 信号只写进 note 供诊断，不再作为闸门：CJK 二元组里的虚词天然 df=0，
    # 用它判"无关"会误杀中文整句（1.6.7 → 1.6.8 的回归）。真正的判据是精排分：
    # 最高分连裸地板都不到 → 库外问题，no_hit + 清空；有块过地板但不足 MIN_CHUNKS
    # → 弱相关，结果照常返回。
    _n_terms = max(1, int(sig.get("n_terms") or 1)) if sig else 1
    if reranker_used and raw_best is not None and raw_best < KB_MIN_RERANK:
        verdict, rule = "无关", "rerank_below_floor"
        no_hit = True
        note += "精排最高分 %.3f 低于地板 %.2f，判无关；" % (raw_best, KB_MIN_RERANK)
    # ── 第二道：过地板块数（只在真的跑过精排时生效，即 deep）────────────────────
    elif over_floor is not None and over_floor < KB_RELEVANCE_MIN_CHUNKS:
        # 有块过地板但不足 MIN_CHUNKS：判"弱相关"，结果照常返回。
        verdict, rule = "弱相关", "over_floor<%d" % KB_RELEVANCE_MIN_CHUNKS
        note += "精排前 5 名仅 %d 块过地板(%.2f)，判弱相关（结果照常返回）；" % (
            over_floor, KB_MIN_RERANK)
    elif over_floor is not None:
        # 只有真的跑过精排才做**正向**判定：quick 路的分数是 RRF 融合分（无量纲），
        # 与精排 logits 不是一个量纲，拿它比 0.10/0.35 只会得到噪声。
        if raw_best < KB_MIN_RERANK_WEAK:
            verdict, rule = "弱相关", "over_floor>=%d,raw<weak" % KB_RELEVANCE_MIN_CHUNKS
        else:
            verdict, rule = "相关", "over_floor>=%d" % KB_RELEVANCE_MIN_CHUNKS
        note += "精排前 5 名 %d 块过地板，最高 %.3f；" % (over_floor, raw_best)
    # 其余（quick 且 missing=0）：verdict 保持 None，只给结果不声称相关性
    if sig is not None:
        note += "信号 missing=%d best_doc=%.3f；" % (sig["missing"], sig["best_doc"])
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
            # 检索串要能拿去搜到原文：审稿意见/补充材料常把整段正文当标题（几百字），
            # 直接拼进去搜不到任何东西。先剥掉 "PEER REVIEW FILE / Reviewers' comments"
            # 这类前缀，只留首句并截到 120 字，再拼作者与年份。
            _t = r["title"] or ""
            # 前缀可能叠好几层（"PEER REVIEW FILE" → "Reviewers' comments:" →
            # "Reviewer #1 (Remarks to the Author):"），一次 re.sub 只剥一层。
            _junk = re.compile(r"(?i)^\s*(?:peer[- ]?review file"
                               r"|reviewers?'?\s+(?:comments?|reports?|feedback)\s*[:：\-—]?"
                               r"|reviewers?'?\s*(?:#\d+\s*)?(?:\([^)]*\))?\s*[:：\-—]?"
                               r"|decision letter|response to review(ers)?)\s*")
            for _ in range(4):
                _nxt = _junk.sub("", _t)
                if _nxt == _t:
                    break
                _t = _nxt
            _t = re.split(r"(?<=[.。])\s", _t)[0] if _t else ""
            _t = re.sub(r"\s+", " ", _t).strip()[:120]
            search_parts = [_t, first_author,
                            str(r["year"]) if r["year"] else ""]
            entry["search"] = " ".join(p for p in search_parts if p)
        results.append(entry)
        seed_doc_ids.append(r["doc_id"])
        if len(results) >= top_k:
            break

    # 无命中的最终口径：只有 results 为空才是 no_hit。verdict="无关" 但仍有候选时，
    # 结果照常返回；不能把 no_hit 当成"判无关"的同义词，否则调用方拿不到结果也看不到空态。
    no_hit = not results
    if no_hit:
        note += "库内无相关资料；"
    resp = {"query": query, "scored": len(ranked), "top_k": top_k,
            # 用户显式要 keyword 时 mode_used 也是 keyword —— 但这不是降级。
            # 把请求模式带回去，渲染层才能区分"我要的"与"被迫退的"。
            "mode_requested": mode or "hybrid", "mode_used": mode_used,
            "reranker": reranker_used, "results": results,
            # 本次检索实际用到的设备（模型此刻已加载，所以这里必然有真值）
            "device": device_report(),
            # 折叠掉的同论文副本数（渲染层可提示，便于用户知道库里存在多份副本）
            "dup_collapsed": dup_collapsed,
            # 相关性地板与结论：verdict='无关' + no_hit=true 时 results 为空、closest 给出
            # "最接近的 5 篇"。让"库里没有"成为不可误读的信号，而不是返回 Top-3 垃圾。
            "verdict": verdict, "no_hit": no_hit, "max_score": round(raw_best, 4) if raw_best is not None else None,
            "floor": floor, "floor_weak": KB_MIN_RERANK_WEAK if floor is not None else None,
            # 行为判据的可观测项：前端可据此解释"为什么判存疑"，标定脚本也直接读它们
            "missing": sig["missing"] if sig else None,
            "best_doc": sig["best_doc"] if sig else None,
            "relevance_rule": rule,
            # 精排前 5 名的**裸分**：判定量一直只用 max，而 max 会被单个离群块劫持
            # （BACKLOG §2.12 根因 1）。把整个分布暴露出来，才谈得上换判定量。
            "raw_top": [round(t[2], 4) for t in ranked[:5]] if ranked else [],
            # 精排前 5 名里过地板的块数 —— 现在的判定量（见 KB_RELEVANCE_MIN_CHUNKS）
            "over_floor": over_floor,
            "closest": closest,
            # 扫描规模与内存读取耗时：库变大时这是第一个瓶颈，以前完全不可见（R9）
            "scan": {"chunks": len(rows), "load_ms": load_ms, "need_vec": need_vec,
                     "corpus_cached": corpus_hit,
                     "warn": ("库内可检索分块 %d 块，单次查询已需 O(库规模) 扫描；"
                              "十万块级建议等索引化改造（见 docs/BACKLOG.md）" % len(rows))
                             if len(rows) > KB_SCAN_WARN_CHUNKS else None},
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
            # 查询缓存行数没有天然上界（每个不同查询一行、每行几 KB），而只有"入库/元数据变化"
            # 时会整体清空 —— 长期使用会一直涨。这里按写入次数偶尔修剪，保留最近
            # KB_QUERY_CACHE_MAX 条（默认 3000）。计数在进程内，不额外查库。
            global _CACHE_WRITES
            _CACHE_WRITES += 1
            if _CACHE_WRITES % 200 == 0:
                db.execute("DELETE FROM cache WHERE key NOT IN "
                           "(SELECT key FROM cache ORDER BY created DESC LIMIT ?)",
                           (KB_QUERY_CACHE_MAX,))
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
    depth = _depth_of(req, "quick")           # #7 两段式：kb_rag 第一跳也先给 quick 直答
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
    # #5 通道 C：同一次调用里把信封渲染成自包含 HTML（不经 LLM、无第二次 IPC 往返）。
    # 只有显式传 answer 才走这条路：不传时行为与 3.3.0 之前完全一致（零回归风险）。
    if req.get("answer"):
        try:
            resp["answer_file"] = _answer_finish(_answer_env(resp, req.get("answer")), req)
        except Exception as e:                                   # noqa: BLE001
            resp["answer_file"] = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    return resp


# ---------------------------------------------------------------- kb-answer（#5 通道 C）

# 需求草案把答案落在 `<kb_root>/answers/`。**不采用**：真实库目录是只读约定
# （`python tests/run.py --only static` 会校验指纹），引擎往库里写文件就破坏了它。
# 默认落到用户数据目录；`KB_ANSWER_DIR` 环境变量或工具参数 answer_dir 可覆盖。
_ANSWER_DIR_DEFAULT = "~/.kb-rag/answers"


def _answer_dir(req):
    """答案目录：顶层 answer_dir → answer 信封里的 answer_dir → KB_ANSWER_DIR → 默认。

    工具层把目录放在 answer 信封里（它是"答案规格"的一部分），命令层也可能直接传；
    两处都认，省得调用方记两个位置。
    """
    req = req or {}
    spec = req.get("answer") if isinstance(req.get("answer"), dict) else {}
    raw = (req.get("answer_dir") or (spec or {}).get("answer_dir")
           or os.environ.get("KB_ANSWER_DIR") or _ANSWER_DIR_DEFAULT)
    return Path(os.path.expanduser(str(raw)))


def _answer_verdict(resp):
    """复用 #6 的判定：no_hit / 弱相关 / 存疑 / 命中（信封的 verdict 取值）。"""
    if resp.get("no_hit") is True:
        return "no_hit"
    v = str(resp.get("verdict") or "")
    if v == "弱相关":
        return "weak"
    if v == "存疑":
        return "doubt"
    return "hit"


def _answer_page(p):
    """检索结果里的 page 是 [起,止]；渲染器只认一个字符串。"""
    if isinstance(p, (list, tuple)):
        vals = [x for x in p if x is not None]
        if not vals:
            return None
        return str(vals[0]) if len(set(vals)) == 1 else "%s–%s" % (vals[0], vals[-1])
    return None if p is None else str(p)


def _answer_evidence(resp, limit=10):
    """检索结果 → 渲染器的 evidence（字段名对齐 `_ans_evidence_html`）。"""
    out = []
    for r in (resp.get("evidence") or resp.get("results") or [])[:limit]:
        if not isinstance(r, dict):
            continue
        out.append({"title": r.get("title") or r.get("file") or "(无标题)",
                    "doi": r.get("doi") or None,
                    "authors": r.get("authors") or None,
                    "year": r.get("year"),
                    "journal": r.get("journal") or None,
                    "section": r.get("section") or None,
                    "page": _answer_page(r.get("page")),
                    "snippet": (r.get("snippet") or "")[:400] or None,
                    "score": r.get("score"),
                    "zotero_key": r.get("zotero_key") or None,
                    "citations": r.get("citations") or []})
    return out


def _answer_suggest(resp):
    """引文/关联文献 → 渲染器的三列补充建议（列名为空则整列省略）。

    与三层文本渲染的口径一致：① 引文命中库内；② 引文未命中（建议补库）；
    ③ related（同作者/同期刊/主题相似，无引文关系）。
    """
    in_lib, to_fetch = [], []
    for i, ev in enumerate(resp.get("evidence") or [], 1):
        if not isinstance(ev, dict):
            continue
        for c in (ev.get("citations") or []):
            if not isinstance(c, dict):
                continue
            lib = c.get("lib") or {}
            if lib:
                in_lib.append({"title": lib.get("title") or c.get("text") or "",
                               "doi": lib.get("doi"), "refs": [i], "in_lib": True,
                               "why": "被 [%d] 的引文 Ref %s 引用，已在库内" % (i, c.get("n"))})
            else:
                to_fetch.append({"title": (c.get("text") or "")[:120], "refs": [i],
                                 "why": "被 [%d] 的引文 Ref %s 引用，尚不在库内" % (i, c.get("n"))})
    related = []
    for r in (resp.get("related") or []):
        if isinstance(r, dict):
            related.append({"title": r.get("title") or "", "doi": r.get("doi"),
                            "why": r.get("reason") or "同作者 / 同期刊 / 主题相似"})
    return {"in_library": in_lib[:6], "to_fetch": to_fetch[:6], "related": related[:6]}


# 渲染端裁剪（#7 设计原理之二：**不在生成端约束，在渲染端裁剪**）。
# 模型可以随意生成，但到达用户的长度由引擎说了算：每字段 max_chars（模板可覆盖），
# 再按 budget.max_total_chars 整封收口 —— 第一跳的极简卡就靠这条锁死。
_ANSWER_KIND_LIMITS = {"note": 200, "text": 600, "list": 120, "steps": 140,
                       # 表格 / 图表 / 卡片类是**数据**不是句子：按句子的尺度截它们，
                       # 截掉的是最后几行数据（实测：一张 4 行对比表被截到 80 字符，
                       # 剩下两行），不是冗余修辞。所以按"结构"给额度。
                       "table": 2000, "chart": 900, "line": 900, "bars": 900, "rank": 900,
                       "flow": 900, "scalars": 900, "kpi": 900,
                       "cards": 180, "timeline": 120, "dispute": 320, "math": 240,
                       "figure": 300, "code": 1200, "key": 140, "qa": 520, "deriv": 320}
# 这些键截了就废了：图（base64）、链接、标识符
_ANSWER_NO_CLIP_KEYS = {"src", "b64", "href", "url", "doi", "zotero_key"}
_ANSWER_MIN_LIMIT = 30


def _answer_clip(value, limit, key=None, hits=None):
    """递归截断字符串。返回 (新值, 截断处数)。"""
    if hits is None:
        hits = [0]
    if isinstance(value, str):
        if key in _ANSWER_NO_CLIP_KEYS or len(value) <= limit:
            return value, hits
        hits[0] += 1
        return value[:max(1, limit - 1)].rstrip() + "…", hits
    if isinstance(value, dict):
        return {k: _answer_clip(v, limit, k, hits)[0] for k, v in value.items()}, hits
    if isinstance(value, list):
        return [_answer_clip(v, limit, key, hits)[0] for v in value], hits
    return value, hits


def _answer_apply_limits(env):
    """按模板 max_chars 截断 fields，再按 budget 整封收口。返回人话警告列表。"""
    tpl = _ans_TEMPLATES.get(env.get("template") or "review") or {}
    limits = {s["id"]: s.get("max_chars") for s in tpl.get("sections", [])}
    kinds = {s["id"]: s.get("kind", "text") for s in tpl.get("sections", [])}
    warns = []
    fields = env.get("fields")
    if not isinstance(fields, dict):
        return warns
    for fid, val in list(fields.items()):
        base = limits.get(fid) or _ANSWER_KIND_LIMITS.get(kinds.get(fid, "text"), 400)
        fields[fid], hits = _answer_clip(val, base)
        if hits[0]:
            warns.append("字段 %s 超长，已按上限 %d 字符截断 %d 处" % (fid, base, hits[0]))
    budget = env.get("budget") or {}
    total_max = budget.get("max_total_chars")
    if isinstance(total_max, int) and total_max > 0:
        # JSON 的结构字符（键名/引号/括号）也是预算的一部分，而且**压不掉**：
        # 比例必须按"内容预算"算，否则每轮只收窄一点点、8 轮还停在上限之上
        # （实测 323 → 305，永远够不着 300）。踩过两次，别再改成按模板上限算。
        overhead = len(json.dumps({k: "" for k in fields}, ensure_ascii=False))
        content_budget = max(_ANSWER_MIN_LIMIT * max(1, len(fields)), total_max - overhead)
        shrunk = 0
        for _ in range(8):
            sizes = {fid: len(json.dumps(val, ensure_ascii=False)) for fid, val in fields.items()}
            if sum(sizes.values()) + overhead <= total_max:
                break
            ratio = max(0.2, float(content_budget) / float(max(1, sum(sizes.values()))))
            for fid, val in list(fields.items()):
                fields[fid], _ = _answer_clip(val, max(_ANSWER_MIN_LIMIT, int(sizes[fid] * ratio)))
            shrunk += 1
        if shrunk:
            warns.append("整封超预算（上限 %d 字符），字段已收窄 %d 轮" % (total_max, shrunk))
    return warns


def _answer_env(resp, spec):
    """检索响应 + 模型填的 fields → 一个 kb-answer 信封。

    分工：**证据只由引擎填**（模型给的 refs 是本次 evidence 的 1 基下标，
    模型自带 evidence 会让编号与内容错位，所以一律以检索结果为准）；
    展示字段（fields/summary/title/template/layout）以模型填的为准。
    """
    spec = dict(spec or {})
    env = {k: v for k, v in spec.items() if k not in ("stage", "answer_dir")}
    if spec.get("stage") == "instant":
        env.setdefault("template", "instant")
        env.setdefault("budget", {"max_total_chars": 300})
        env.setdefault("depth", "quick")
    evs = _answer_evidence(resp)
    env["evidence"] = evs if evs else (env.get("evidence") or [])
    env.setdefault("verdict", _answer_verdict(resp))
    env.setdefault("query", resp.get("query") or "")
    env.setdefault("depth", resp.get("depth"))
    if env.get("hits") is None:
        env["hits"] = resp.get("hits") or (len(evs) or None)
    if env.get("ms") is None:
        env["ms"] = resp.get("ms_total") or resp.get("ms")
    if not env.get("reranker"):
        env["reranker"] = resp.get("reranker")
    if not env.get("suggest"):
        sug = _answer_suggest(resp)
        if any(sug.values()):
            env["suggest"] = sug
    env.setdefault("version", 1)
    env["_clip_warnings"] = _answer_apply_limits(env)
    return env


def _answer_card(env):
    """instant/review/... 的**文本层**形态：把填好的字段拼成一张可直接转达的卡片（#7 通道 B）。

    HTML 文件（通道 C）不依赖宿主能力，但"每次都得开文件"不是快路径 —— 这张卡片
    让 agent 一次调用就能把答案原样转达给用户。两侧都能到达，才叫无损降级：所以这里
    要覆盖**所有块类型**。旧实现只认 str 与 {text}，list / cards / table / rank 被整块
    丢掉，对话里只剩两三行，用户只能去开文件。
    """
    tpl = _ans_TEMPLATES.get(env.get("template") or "review") or {}
    fields = env.get("fields") if isinstance(env.get("fields"), dict) else {}
    budget = 2600
    out = []

    def _clip(s, n=300):
        s = str(s if s is not None else "").strip()
        return s if len(s) <= n else s[:n].rstrip() + "…"

    def _unit(u):
        u = str(u or "").strip()
        return (" " + u) if u else ""

    for spec in tpl.get("sections", []):
        sid = spec.get("id") or ""
        v = fields.get(sid)
        if v is None or v == "" or v == [] or v == {}:
            continue
        kind = spec.get("kind") or "text"
        label = spec.get("label") or sid
        body = []
        if isinstance(v, str):
            body = [v]
        elif isinstance(v, dict) and isinstance(v.get("text"), str) and kind in ("note", "text"):
            mark = ""
            if isinstance(v.get("refs"), list) and v["refs"]:
                mark = " [%s]" % ",".join(str(r) for r in v["refs"])
            body = [v["text"] + mark]
        elif isinstance(v, list):
            heads = [it.get("h") or it.get("title") for it in v if isinstance(it, dict)]
            if kind == "cards" or any(heads):
                for it in v:
                    if not isinstance(it, dict):
                        body.append("- " + _clip(it))
                        continue
                    h = it.get("h") or it.get("title") or ""
                    p = it.get("p") or it.get("body") or it.get("text") or ""
                    body.append("- **%s**：%s" % (_clip(h, 40), _clip(p)))
            else:
                for it in v:
                    if isinstance(it, dict):
                        # rank / bars 的条目是 {label, value, unit}，没有 text 字段：
                        # 按"label：value unit"渲染，否则对话卡片里只剩一个空 bullet。
                        if it.get("label") is not None or it.get("value") is not None:
                            body.append("- %s：%s%s" % (
                                _clip(it.get("label"), 40), _clip(it.get("value"), 40),
                                _unit(it.get("unit"))))
                        else:
                            body.append("- " + _clip(it.get("text") or it.get("p") or ""))
                    else:
                        body.append("- " + _clip(it))
        elif isinstance(v, dict):
            if kind == "table" and isinstance(v.get("columns"), list):
                cols = [str(c.get("label") or "") for c in v.get("columns") or []]
                if cols:
                    body.append("| " + " | ".join(cols) + " |")
                    body.append("|" + "---|" * len(cols))
                for row in v.get("rows") or []:
                    body.append("| " + " | ".join(_clip(c, 60) for c in row) + " |")
            elif isinstance(v.get("items"), list):
                for it in v["items"]:
                    if not isinstance(it, dict):
                        body.append("- " + _clip(it))
                        continue
                    body.append("- %s：%s%s" % (_clip(it.get("label"), 40),
                                                _clip(it.get("value"), 40),
                                                _unit(it.get("unit"))))
            elif isinstance(v.get("nodes"), list) or isinstance(v.get("steps"), list):
                nodes = v.get("nodes") if isinstance(v.get("nodes"), list) else v.get("steps")
                labels = []
                for n in nodes:
                    if isinstance(n, dict):
                        labels.append(str(n.get("label") or n.get("name") or n.get("text") or ""))
                    else:
                        labels.append(str(n))
                body.append(" → ".join(x for x in labels if x))
            elif isinstance(v.get("series"), list):
                body.append("（曲线数据见 HTML）")
        body = [b for b in body if str(b).strip()]
        if not body:
            continue
        out.append("**%s**：\n%s" % (label, "\n".join(body)))

    text = "\n\n".join(out)
    if len(text) > budget:
        text = text[:budget].rstrip() + "\n…（已截断，完整内容见 HTML）"
    return text


def _answer_slug(text, limit=40):
    s = re.sub(r'[\\/:*?"<>|\s]+', "-", str(text or "").strip())
    s = re.sub(r"-{2,}", "-", s).strip("-.")
    return (s[:limit].strip("-.") or "answer")


def _answer_index_html(entries):
    """答案目录的历史画廊：用户回来一眼看到最近生成的答案。"""
    cards = []
    for e in entries:
        vk, vl = _ans_VERDICT.get(e.get("verdict") or "hit", ("ok", "命中"))
        when = datetime.fromtimestamp(e["ts"]).strftime("%m-%d %H:%M") if e.get("ts") else ""
        cards.append('<a href="%s"><div class="t">%s</div><div class="d">%s · %s KB<br>'
                     '<span class="badge %s">%s</span> <span class="badge">%s</span>'
                     '<span class="badge">%s</span></div></a>'
                     % (_ans_esc(e.get("file", "")), _ans_esc(e.get("query") or e.get("file", "")),
                        _ans_esc(when), round((e.get("bytes") or 0) / 1024), vk, _ans_esc(vl),
                        _ans_esc(_ans_TEMPLATES.get(e.get("template") or "review",
                                                    {}).get("label", e.get("template") or "")),
                        _ans_esc(_ans_DEPTH.get(e.get("depth"), ""))))
    body = "\n".join([
        '<header class="doc">',
        '<div class="eyebrow">kb-answer v1 · 本地答案归档</div>',
        "<h1>已生成的答案</h1>",
        '<p class="sub">每次带 <code>answer</code> 参数的 kb_rag 调用都会在这里落一份自包含 HTML。'
        '按时间倒序；点开即看，可直接打印或分享。</p>',
        '<div class="metarow"><span class="badge ok">自包含单文件</span>'
        '<span class="badge">共 %d 份</span></div>' % len(entries),
        "</header>",
        '<section id="s1"><h2><span class="num">01</span>最近生成</h2>'
        '<div class="menu">%s</div></section>' % "".join(cards),
        '<footer class="doc">目录：%s</footer>' % _ans_esc(str(_ANSWER_DIR_DEFAULT)),
    ])
    return ("<!DOCTYPE html>\n<html lang=\"zh-CN\">\n<head>\n<meta charset=\"UTF-8\">\n"
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            "<title>kb-answer · 已生成的答案</title>\n<style>\n%s\n</style>\n</head>\n<body>\n"
            '<div class="sheet">\n%s\n</div>\n<script>\n%s\n</script>\n</body>\n</html>\n'
            % (_ans_CSS + _ans_CSS_BLOCKS + _ans_CSS_MOTION + _ans_CSS_INTERACT + _ans_CSS_SECTION,
               body, _ans_MOTION_JS + "\n" + _ans_JS_INTERACT))


def _answer_update_index(d, name, env, html):
    idx = d / "index.json"
    entries = []
    if idx.exists():
        try:
            entries = json.loads(idx.read_text(encoding="utf-8"))
        except Exception:                                        # noqa: BLE001
            entries = []
    entries = [e for e in entries if isinstance(e, dict) and e.get("file") != name]
    entries.append({"file": name, "template": env.get("template") or "review",
                    "query": env.get("query") or "", "verdict": env.get("verdict") or "hit",
                    "depth": env.get("depth"), "bytes": len(html.encode("utf-8")),
                    "ts": time.time()})
    entries.sort(key=lambda e: e.get("ts") or 0, reverse=True)
    entries = entries[:200]
    idx.write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8")
    (d / "index.html").write_text(_answer_index_html(entries), encoding="utf-8", newline="")


def _answer_finish(env, req, t0=None):
    """校验 → 渲染 → 落盘 → 刷新画廊。渲染是纯函数：同一信封两次渲染字节相同。"""
    problems = list(env.pop("_clip_warnings", []) or [])
    problems += _ans_validate(env) + _ans_validate_content(env)
    # 信封没带 theme 时读工作区配置（`.kb-rag/answer-theme.json` → state.json 的 theme）。
    # 必须在渲染前定下来，两遍渲染才会用同一套颜色。
    if not isinstance(env.get("theme"), dict) or not env.get("theme"):
        env["theme"] = _ans_load_theme(req)
    # 页脚要写"渲染多少毫秒"，只能先空跑一遍量出来，第二遍再写进去。
    # 渲染是纯函数、一次几毫秒；render_ms 属于信封，所以"同一信封字节相同"仍成立。
    # 文件名摘要取**不含 render_ms 的第一遍**，否则同一个问题每跑一次就多一个文件。
    _digest_src = None
    if env.get("render_ms") is None:
        _t_render = time.perf_counter()
        _digest_src = _ans_render(dict(env, render_ms=None))
        env["render_ms"] = max(1, round((time.perf_counter() - _t_render) * 1000))
    html = _ans_render(env)
    problems += _ans_selfcheck(html)
    d = _answer_dir(req)
    d.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256((_digest_src or html).encode("utf-8")).hexdigest()[:8]
    name = "%s_%s-%s.html" % (datetime.now().strftime("%Y-%m-%d"),
                              _answer_slug(env.get("query")), digest)
    path = d / name
    path.write_text(html, encoding="utf-8", newline="")
    _answer_update_index(d, name, env, html)
    out = {"ok": True, "path": str(path), "url": path.as_uri(),
           "bytes": len(html.encode("utf-8")), "dir": str(d),
           "template": env.get("template") or "review",
           "layout": env.get("layout") or "card", "warnings": problems,
           "card": _answer_card(env)}
    if t0 is not None:
        out["ms"] = round((time.time() - t0) * 1000)
    return out


def cmd_answer(req):
    """只渲染：不查库，把调用方给的信封（含 evidence）渲染成 HTML。

    与 kb_rag 的 answer 参数共用同一条路径；给"换个模板重渲"这类用法留的口子。
    """
    t0 = time.time()
    spec = req.get("answer") or req
    resp = {"evidence": req.get("evidence") or [], "results": req.get("results") or [],
            "verdict": req.get("verdict"), "no_hit": req.get("no_hit"),
            "depth": req.get("depth"), "hits": req.get("hits"),
            "ms_total": req.get("ms"), "query": req.get("query") or spec.get("query"),
            "related": req.get("related") or []}
    return _answer_finish(_answer_env(resp, spec), req, t0)


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


def _hf_cache_root():
    """HF 缓存根目录：HF_HUB_CACHE > HF_HOME/hub > huggingface_hub 常量 > 默认。"""
    cache = os.environ.get("HF_HUB_CACHE") or os.environ.get("HUGGINGFACE_HUB_CACHE")
    if cache:
        return Path(cache).expanduser()
    home = os.environ.get("HF_HOME")
    if home:
        return Path(home).expanduser() / "hub"
    try:
        import huggingface_hub.constants as _hfc
        return Path(_hfc.HF_HUB_CACHE)
    except Exception:
        return Path.home() / ".cache" / "huggingface" / "hub"


def _model_cache_state(name):
    """不加载模型，检查模型是否完整落盘；用于 kb_stats 和排障。

    修复 B19：
      - 本地模型目录（绝对/相对路径）直接检查 config/modules + 权重；
      - HF_HOME 分支尊重 HF_HUB_CACHE；
      - 只有 config.json、没有权重文件的半截下载不再报 installed=true。
    """
    raw = str(name or "")
    local = Path(os.path.expanduser(raw))
    patterns = ("*.safetensors", "*.bin", "*.onnx", "*.pt", "*.pth", "*.ckpt")

    def _has_weights(d):
        return any(any(p.is_file() for p in d.glob(pat)) for pat in patterns)

    if local.is_dir():
        has_cfg = (local / "config.json").is_file() or (local / "modules.json").is_file()
        has_w = _has_weights(local)
        return {"name": raw, "installed": bool(has_cfg and has_w),
                "partial": bool(has_cfg and not has_w),
                "cache_dir": str(local), "source": "local"}

    repo = "models--" + raw.replace("/", "--")
    root = _hf_cache_root() / repo
    snaps = sorted((root / "snapshots").glob("*")) if (root / "snapshots").is_dir() else []
    complete = [s for s in snaps
                if ((s / "config.json").is_file() or (s / "modules.json").is_file())
                and _has_weights(s)]
    partial = [s for s in snaps
               if ((s / "config.json").is_file() or (s / "modules.json").is_file())
               and not _has_weights(s)]
    return {"name": raw, "installed": bool(complete), "partial": bool(partial and not complete),
            "cache_dir": str(root), "snapshots": len(snaps), "source": "hf"}



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
        # 精排模型名此前只在检索响应与 reload 响应里出现，kb_stats 看不到 —— 排障时得先跑一次检索
        "reranker": _RERANK_NAME if _RERANKER is not None else None,
        "embedding_error": _EMBED_ERR,
        "vectors_missing": missing_vecs,
        "retry_secs": MODEL_RETRY_SECS,
        "device": device_report(),
        "models": {
            "embed": _model_cache_state(os.environ.get("KB_EMBED_MODEL", "BAAI/bge-small-zh-v1.5")),
            "rerank": _model_cache_state(os.environ.get("KB_RERANK_MODEL", "BAAI/bge-reranker-base")),
        },
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
    global _CUDA, _GPU_OK, _GPU_WHY, _PROBE, _VRAM_GB
    # 设备判定一并重置：用户可能刚装上 CUDA 版 torch 或修好驱动，reload 就该能重新尝试 GPU。
    # 否则"GPU 不可用"是粘性的，修好了也得重启 DSH —— 与 issue #2 的模型失败永久缓存同一类体验问题。
    # （模型若已在 CPU 上，要靠 drop_models=true 才会重新加载到 GPU；device_report 会如实显示。）
    _CUDA = None
    _GPU_OK = None
    _GPU_WHY = None
    _PROBE = None
    _VRAM_GB = None
    _BATCH_EFFECTIVE.clear()
    _DEVICE_NOTE.clear()
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
        # reload 之后设备落在哪、探测结果如何 —— 用户修环境后第一眼要看的就是这个
        "device": device_report(),
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
        if (totals["added"] or totals["updated"]
                or totals.get("vectors", 0) > 0):
            db.execute("DELETE FROM cache")
        db.commit()
    finally:
        db.close()
    _invalidate_caches("zotero added=%d updated=%d vectors=%d"
                       % (totals["added"], totals["updated"], totals.get("vectors", 0)))
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
    if removed:
        _invalidate_caches("dedup removed=%d" % len(removed))
    return {"ok": True, "removed": len(removed), "docs": n_docs,
            "chunks": n_chunks, "files": removed,
            "ms": round((time.time() - t0) * 1000)}


# ---------------------------------------------------------------- clear


def cmd_clear(req):
    """Wipe every doc/chunk/vector/cache row; destructive, requires confirm: true."""
    _REL_CENTROID.clear()
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
    _invalidate_caches("clear docs=%d chunks=%d" % (docs_n, chunks_n))
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
    """Ordered (url, name, kind) candidates: publisher PDF -> landing-page HTML -> OA fallback.

    arXiv -> direct; DOI -> publisher direct PDF -> citation_pdf_url from HTML -> Unpaywall OA.
    kind 用于 fetch 的落盘分层（见 FETCH_LAYOUT），与 Node 下载器的 source 取值对齐。"""
    import urllib.parse
    import urllib.request
    ident = (ident or "").strip()
    m = _ARXIV_RE.search(ident) or re.search(r"10\.48550/arXiv\.(\d{4}\.\d{4,5})", ident)
    if m:
        aid = m.group(1)
        return [("https://arxiv.org/pdf/" + aid, "arxiv_" + aid, "arxiv")]
    md = re.search(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", ident)
    doi = md.group(0).rstrip(".,;") if md else None
    if not doi:
        return []
    out = []
    pub = _publisher_pdf_url(doi)
    if pub:
        out.append((pub, doi, "publisher"))
    html_pdf = _extract_pdf_url_from_html(doi)
    if html_pdf and html_pdf != pub:
        out.append((html_pdf, doi, "landing"))
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
            out.append((url, name, "unpaywall"))
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


# ---------------------------------------------------------------- fetch 的落盘规范
# 规范（KB_FETCH_LAYOUT=v2，默认）：
#     <root>/<source>/<year>/<slug>__<hash8>.pdf
#     <root>/manifest.json          索引 + struct_version
#   source ∈ arxiv | publisher | landing | unpaywall | crossref | legacy | unknown
#   year   取不到写 unknown；hash8 = 内容 sha256 前 8 位（内容相同即同一个名字，天然幂等）
#
# 为什么统一在引擎侧落位：Node 下载器与 Python 兜底的**文件名规则本来就不同**
# （Node 保留空格与任意 Unicode、上限 160 字符、可能出 .bin；Python 只留 ASCII+汉字、
# 上限 60、固定 .pdf）。同一个 DOI 走不同路径会得到不同文件名。改成"下载到暂存目录 →
# 由引擎统一改名落位"之后，两条路径的产物一致，doi_pdf.mjs 也不用改。
#
# 兼容：旧版平铺文件**不移动**，首次构建 manifest 时扫出来回填为 source=legacy。
# 逃生口：KB_FETCH_LAYOUT=flat 回到旧的平铺行为。
FETCH_LAYOUT = os.environ.get("KB_FETCH_LAYOUT", "v2").strip().lower()
FETCH_MANIFEST = "manifest.json"
FETCH_STRUCT_VERSION = 2
_SOURCE_KIND = {"arxiv": "arxiv", "publisher": "publisher",
                "citation_pdf_url": "landing", "pattern": "landing", "landing": "landing",
                "unpaywall": "unpaywall", "crossref": "crossref"}


def _norm_source_kind(s):
    return _SOURCE_KIND.get((s or "").strip().lower(), "unknown")


def _slugify(s, limit=60):
    """文件名安全化：去掉路径分隔符与非法字符，空白折成 '-'。"""
    s = re.sub(r"[\\/:*?\"<>|\n\r\t]+", "-", str(s or ""))
    s = re.sub(r"\s+", "-", s.strip())
    s = re.sub(r"-{2,}", "-", s).strip("-._")
    return s[:limit] or "paper"


def _pdf_year_title(path):
    """尽力从 PDF 元数据取 (title, year)；任何异常都吞掉（元数据只是锦上添花）。"""
    try:
        import fitz
        with fitz.open(str(path)) as d:
            m = d.metadata or {}
        title = (m.get("title") or "").strip()
        ym = re.search(r"(19|20)\d{2}", "%s %s" % (m.get("creationDate") or "",
                                                   m.get("modDate") or ""))
        return (title or None, ym.group(0) if ym else None)
    except Exception:                                                 # noqa: BLE001
        return (None, None)


def _fetch_manifest(root):
    """读 manifest；不存在时扫 <root> 下的 PDF **回填**为 legacy（不动任何文件）。"""
    p = Path(root) / FETCH_MANIFEST
    if p.is_file():
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(m, dict) and isinstance(m.get("entries"), list):
                m.setdefault("struct_version", FETCH_STRUCT_VERSION)
                m.setdefault("backfilled", 0)
                return m
        except Exception:                                             # noqa: BLE001
            pass
    m = {"struct_version": FETCH_STRUCT_VERSION, "entries": [], "backfilled": 0}
    for f in sorted(Path(root).rglob("*.pdf")):
        try:
            rel = f.relative_to(root).as_posix()
        except Exception:                                             # noqa: BLE001
            continue
        if rel.startswith("."):
            continue
        m["entries"].append({"path": rel, "source": "legacy", "layout": "flat",
                             "bytes": f.stat().st_size})
    m["backfilled"] = len(m["entries"])
    return m


def _fetch_place(entry, root, hash_index=None):
    """按 v2 规范落位一个已下载文件。返回更新后的 entry（幂等）。

    `hash_index` 是 {sha256: 已有相对路径}：命中即**内容重复**——保留已有那份、
    删掉暂存文件，并把 entry 指回已有路径（下游 kb_ingest 会按 sha256 跳过）。"""
    import shutil
    src = Path(entry.get("path") or "")
    if not src.is_file():
        return entry
    h = hashlib.sha256(src.read_bytes()).hexdigest()
    ptitle, pyear = _pdf_year_title(src)
    title = (entry.get("title") or "").strip() or ptitle or ""
    ym = re.search(r"(19|20)\d{2}", title)
    year = pyear or (ym.group(0) if ym else None) or "unknown"
    kind = _norm_source_kind(entry.get("source"))
    slug = _slugify(title or entry.get("id") or "paper")
    dest_dir = Path(root) / kind / str(year)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / ("%s__%s.pdf" % (slug, h[:8]))
    rel = dest.relative_to(root).as_posix()
    out = dict(entry)
    out.update({"sha256": h, "source_kind": kind, "year": year, "slug": slug})
    if src.resolve() == dest.resolve():          # 已经在位（重复调用）
        out["path"] = str(dest)
        return out
    prev = (hash_index or {}).get(h)
    if prev and prev != rel:                     # 内容相同、只是名字不同 → 真去重
        src.unlink()
        out.update({"path": str(Path(root) / prev), "duplicate_of": prev})
        return out
    if dest.exists():
        src.unlink()                             # 目标同名已存在 → 内容一致（hash 同名）
    else:
        shutil.move(str(src), str(dest))
    out["path"] = str(dest)
    return out


def _fetch_organize(root, files):
    """整批落位 + 写 manifest。返回 (files, 统计信息)。"""
    if FETCH_LAYOUT != "v2":
        for f in files:
            f["layout"] = "flat"
        return files, {"layout": "flat", "moved": 0, "deduped": 0}
    m = _fetch_manifest(root)
    index = {e["path"]: e for e in m["entries"] if e.get("path")}
    hash_index = {e["sha256"]: e["path"] for e in index.values() if e.get("sha256")}
    moved = deduped = 0
    out = []
    for f in files:
        if f.get("status") != "downloaded" or not f.get("path"):
            out.append(f)
            continue
        nf = _fetch_place(f, root, hash_index)
        try:
            rel = Path(nf["path"]).relative_to(root).as_posix()
        except Exception:                                             # noqa: BLE001
            out.append(nf)
            continue
        nf["layout"] = "v2"
        dup = nf.get("duplicate_of")
        if dup:
            deduped += 1
        else:
            moved += 1
            index[rel] = {"path": rel, "id": nf.get("id"), "source": nf.get("source_kind"),
                          "year": nf.get("year"), "slug": nf.get("slug"),
                          "sha256": nf.get("sha256"), "bytes": nf.get("bytes"),
                          "layout": "v2"}
            hash_index[nf["sha256"]] = rel
        out.append(nf)
    m["entries"] = sorted(index.values(), key=lambda e: e.get("path") or "")
    m["struct_version"] = FETCH_STRUCT_VERSION
    m["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    info = {"layout": "v2", "moved": moved, "deduped": deduped,
            "entries": len(m["entries"]), "struct_version": FETCH_STRUCT_VERSION,
            "backfilled": m.get("backfilled", 0)}
    try:
        (Path(root) / FETCH_MANIFEST).write_text(
            json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:                                            # noqa: BLE001
        info["manifest_error"] = str(e)[:120]
    return out, info


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
    root = Path(target)
    root.mkdir(parents=True, exist_ok=True)
    # v2：先下到 <root>/.staging，再由引擎统一改名落位。
    # 为什么必须单独暂存：manifest 的「回填旧文件」是扫 <root> 的，若下载直接落在 <root>，
    # 刚下完的文件会被当成 legacy 回填进索引（实测踩过）。
    stage = (root / ".staging") if FETCH_LAYOUT == "v2" else root
    stage.mkdir(parents=True, exist_ok=True)
    target = str(stage)
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
                         "title": rec.get("title"),
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
            for url, name, kind in cands:  # 出版商正式版优先，OA 兜底
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
                        entry["source"] = kind          # 分层用（URL 另存，便于排查）
                        entry["source_url"] = url[:120]
                        if kind == "unpaywall":
                            entry["title"] = name
                        break
                except Exception:
                    continue
            if entry["status"] != "downloaded":
                entry["error"] = "付费墙且无 OA 版本：请在校园网/机构访问下到出版商页面手动下载后 kb_ingest 入库"
        files.append(entry)
    # 统一落位（v2 规范）并维护 manifest；flat 布局下原样返回
    files, layout_info = _fetch_organize(str(root), files)
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
    note = "已下载 %d/%d 篇到 %s。" % (downloaded, len(ids), root)
    if layout_info.get("layout") == "v2":
        note += ("落盘结构 v%d：<来源>/<年份>/<标题>__<内容哈希8>.pdf，索引见 %s/%s"
                 % (FETCH_STRUCT_VERSION, root, FETCH_MANIFEST))
        if layout_info.get("moved"):
            note += "；本批落位 %d 篇" % layout_info["moved"]
        if layout_info.get("deduped"):
            note += "；按内容哈希去重 %d 篇" % layout_info["deduped"]
        if layout_info.get("backfilled"):
            note += "；已把既有 %d 个 PDF 回填为 legacy（未移动）" % layout_info["backfilled"]
        if layout_info.get("manifest_error"):
            note += "；⚠ manifest 写入失败：%s" % layout_info["manifest_error"]
    elif layout_info.get("layout") == "flat":
        note += "（KB_FETCH_LAYOUT=flat：平铺存放，无 manifest）"
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
    return {"ok": True, "target": str(root), "total": len(ids), "downloaded": downloaded,
            "files": files, "note": note, "ingest": ingest_result,
            "layout": layout_info,
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
    # 答案格式预设（/kb format）：auto = 交给模型判断。白名单漏了它时，
    # 读写 state.json 都会把它丢掉 —— /kb format 看起来"设了但没生效"。
    "format": ("auto", "instant", "review", "method", "compare", "numeric",
               "timeline", "concept", "dispute", "derivation", "qa"),
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
            loaded = json.loads(path.read_text(encoding="utf-8-sig"))
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
    Keeps the embedding model loaded across requests (daemon mode).

    模型仍然**按需加载**（第一次检索时加载）。曾试过在这里预热，实测更慢：
    冷启动首问 13.2 s → 预热后 45.9 s，见 docs/BACKLOG.md「首问延迟」。
    """
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
















# ==== BEGIN kb-answer HTML renderer ====
# 原始生成链：proto/embed_kb_html.py ← proto/kb_html.py（原型 sha256 7f89ceedbf37098d）。
#
# 【1.6.9 起改为直接维护这个块】—— 原因：proto/ 没随包发布，拿不到原型的读者只能改这里，
# 而"生成物 / 不要手改"的说法会让下一个改它的人以为自己违规了。现在的规则是：
#   · 顶层符号一律 _ans_ 前缀（render/validate/esc/CSS 这些名字在引擎里会撞名）；
#   · 只依赖标准库；不引入任何外部请求（自检会拦 <link> / url( / http://）；
#   · 纯函数 —— 同一信封两次渲染必须字节相同（_answer_finish 靠它算文件名摘要）；
#   · 新增一种块 = 写一个 _ans_blk_xxx + 在 _ans_BLOCKS 注册 + 在 _ans_CSS_VIS 加样式。
#
# ⚠ kb_engine.py 是 build 的产物：改这里的文字 = 改用户看到的页面，但**下一次构建会覆盖它**。
#   要改文案 / 模板 / 渲染逻辑，改 v1.6.9-draft/build_v169.py 与 .py.parts/，然后重跑构建。
import html

# ------------------------------------------------------------------ 样式

_ans_CSS = r"""
  :root{
    --bg:#f4f6f9; --paper:#ffffff; --ink:#171a1f; --ink-2:#454d5a; --ink-3:#5e6774;
    --line:#e4e8ee; --line-strong:#c6cdd9;
    --accent:#1f5fa8; --accent-soft:#eaf2fb;
    --accent-2:#0d6b52; --accent-2-soft:#e8f5f0;
    --warn:#8a5a00; --warn-soft:#fdf5e4;
    --danger:#8a1f1f; --danger-soft:#fdecec;
    --radius:10px;
    /* 正文最大行宽：中文约 46 字/行。表格 / 图 / 卡片不受它约束。 */
    --measure:46em;
    --shadow:0 1px 2px rgba(16,24,40,.04), 0 1px 3px rgba(16,24,40,.06);
    --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,"Liberation Mono",monospace;
    --sans:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei","Source Han Sans SC","Noto Sans CJK SC",sans-serif;
  }
  *{box-sizing:border-box}
  html{-webkit-text-size-adjust:100%}
  body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);
       font-size:16px;line-height:1.78;font-feature-settings:"kern" 1;
       -webkit-font-smoothing:antialiased}
  h1,h2,h3{text-wrap:balance}
  p{text-wrap:pretty}
  /* 正文限宽。长行是中文阅读最大的负担——表格/图/卡片走全宽，正文不走。 */
  .lead,p,ul:not(.tl)>li,ol>li{max-width:var(--measure)}
  ol.ev>li,ul.tl>li{max-width:none}
  a:focus-visible,button:focus-visible,summary:focus-visible{
    outline:2px solid var(--accent);outline-offset:2px;border-radius:4px}
  ::selection{background:#cfe4fb}
  /* 容器收窄到 880：卡片宽度贴近正文行宽，右侧不出现大片空白 */
  .wrap{max-width:880px;margin:0 auto;padding:26px 20px 72px}

  header.doc{background:var(--paper);border:1px solid var(--line);border-radius:var(--radius);
    padding:32px 32px 26px;position:relative;overflow:hidden;box-shadow:var(--shadow)}
  header.doc::before{content:"";position:absolute;inset:0 0 auto 0;height:4px;
    background:linear-gradient(90deg,var(--accent),var(--accent-2))}
  .eyebrow{font-size:12.5px;letter-spacing:.14em;text-transform:uppercase;
    color:var(--accent);font-weight:700;margin:6px 0 10px}
  h1{font-size:31px;line-height:1.3;margin:0 0 12px;letter-spacing:-.01em}
  .sub{color:var(--ink-2);font-size:15.5px;margin:0 0 20px;max-width:76ch}
  .metarow{display:flex;flex-wrap:wrap;gap:8px}
  .badge{display:inline-flex;align-items:center;gap:6px;font-size:12.5px;color:var(--ink-2);
    background:#f2f4f7;border:1px solid var(--line);border-radius:999px;padding:4px 11px;white-space:nowrap}
  .badge b{color:var(--ink);font-weight:600}
  .badge.ok{background:var(--accent-2-soft);color:var(--accent-2);border-color:transparent}
  .badge.warn{background:var(--warn-soft);color:var(--warn);border-color:transparent}
  .badge.mut{background:var(--line);color:var(--ink-2);border-color:transparent}
  .dot{width:6px;height:6px;border-radius:50%;background:var(--accent-2);display:inline-block}

  .actions{display:flex;gap:10px;flex-wrap:wrap;margin:18px 0 0}
  button.act{font:inherit;font-size:14px;cursor:pointer;border:1px solid var(--line-strong);
    background:#fff;color:var(--ink);border-radius:8px;padding:8px 15px;transition:.15s}
  button.act:hover{background:var(--accent-soft);border-color:var(--accent);color:var(--accent)}

  section{background:var(--paper);border:1px solid var(--line);border-radius:var(--radius);
    padding:26px 32px 28px;margin-top:16px;box-shadow:var(--shadow)}
  h2{font-size:20px;margin:0 0 14px;letter-spacing:-.005em;display:flex;align-items:baseline;gap:11px}
  section ul:not(.tl)>li::marker{color:var(--accent)}
  section ol>li::marker{color:var(--accent);font-weight:600}
  h2 .num{font-family:var(--mono);font-size:13px;color:#fff;background:var(--accent);
    border-radius:6px;padding:2px 8px;line-height:1.5;flex:none;transform:translateY(-2px)}
  h3{font-size:16.5px;margin:26px 0 8px}
  h3:first-child{margin-top:0}
  .lead{color:var(--ink-2);font-size:15px;margin:0 0 18px}
  p{margin:0 0 14px}
  ul,ol{margin:0 0 14px;padding-left:1.5em}
  li{margin:0 0 8px}
  a{color:var(--accent);text-decoration:none;border-bottom:1px solid rgba(31,95,168,.28)}
  a:hover{border-bottom-color:var(--accent)}
  code{font-family:var(--mono);font-size:.88em;background:#f2f4f7;border:1px solid var(--line);
    border-radius:4px;padding:1px 5px}
  strong{font-weight:700}

  .tablewrap{overflow-x:auto;margin:0 0 18px;border:1px solid var(--line);
    border-radius:var(--radius);box-shadow:var(--shadow);background:#fff}
  .tablewrap::-webkit-scrollbar{height:9px}
  .tablewrap::-webkit-scrollbar-thumb{background:var(--line-strong);border-radius:5px}
  .tablewrap::-webkit-scrollbar-track{background:#f7f9fc}
  table{border-collapse:collapse;width:100%;font-size:14.2px;background:#fff}
  caption{caption-side:top;text-align:left;font-size:12.8px;color:var(--ink-3);
    padding:11px 14px 10px;background:#fbfcfe;border-bottom:1px solid var(--line)}
  th,td{padding:10px 14px;text-align:left;vertical-align:top;border-bottom:1px solid var(--line)}
  thead th{background:#f2f5f9;font-size:12.8px;letter-spacing:.02em;color:var(--ink-2);
    font-weight:700;white-space:nowrap;border-bottom:1px solid var(--line-strong)}
  tbody tr:nth-child(even){background:#fafbfd}
  tbody tr:last-child td{border-bottom:none}
  tbody tr:hover{background:var(--accent-soft)}
  td{font-variant-numeric:tabular-nums}
  td.k{font-weight:600;color:var(--ink)}
  /* 带行标题的表格：首列按内容收紧，把宽度让给数据列 */
  table:has(td.k) th:first-child,table:has(td.k) td:first-child{width:1%;white-space:nowrap}
  .hl{background:var(--accent-soft);font-weight:600;border-radius:4px;padding:1px 5px;color:#123f75;
    box-decoration-break:clone;-webkit-box-decoration-break:clone}
  .hl2{background:var(--accent-2-soft);font-weight:600;border-radius:4px;padding:1px 5px;color:#0a4f3d;
    box-decoration-break:clone;-webkit-box-decoration-break:clone}
  .hl-warn{background:var(--danger-soft);font-weight:600;border-radius:4px;padding:1px 5px;color:var(--danger);
    box-decoration-break:clone;-webkit-box-decoration-break:clone}
  .src{font-size:12.2px;color:var(--ink-3);line-height:1.5}

  .note{border-left:3px solid var(--accent);background:var(--accent-soft);border-radius:0 8px 8px 0;
    padding:14px 18px;margin:0 0 18px;font-size:14.6px;color:#123f75}
  .note.warn{border-left-color:#d09a2a;background:var(--warn-soft);color:var(--warn)}
  .note.ok{border-left-color:var(--accent-2);background:var(--accent-2-soft);color:#0a4f3d}
  .note.bad{border-left-color:#c0392b;background:var(--danger-soft);color:var(--danger)}
  .note p:last-child{margin-bottom:0}
  .note .t{display:block;font-weight:700;margin-bottom:4px}

  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(238px,1fr));gap:14px;margin:0 0 18px}
  .card{border:1px solid var(--line);border-radius:var(--radius);padding:15px 17px;background:#fbfcfe}
  .card h4{margin:0 0 6px;font-size:14.6px}
  .card p{font-size:14.2px;color:var(--ink-2);margin:0}
  .card .src{display:block;margin-top:8px}

  ol.refs{counter-reset:r;list-style:none;padding-left:0}
  ol.refs li{counter-increment:r;position:relative;padding-left:34px;font-size:14.3px;
    color:var(--ink-2);margin-bottom:9px}
  ol.refs li::before{content:"[" counter(r) "]";position:absolute;left:0;top:0;
    font-family:var(--mono);font-size:12.4px;color:var(--accent);font-weight:700}
  sup.c{font-family:var(--mono);font-size:11.5px;color:var(--accent);font-weight:700}

  /* 来源卡片（工具返回的证据列表） */
  ol.ev{counter-reset:e;list-style:none;padding-left:0;margin:0 0 18px}
  ol.ev > li{counter-increment:e;position:relative;padding:15px 18px 15px 50px;margin-bottom:12px;
    border:1px solid var(--line);border-radius:var(--radius);background:#fff;
    box-shadow:var(--shadow);transition:border-color .15s}
  ol.ev > li:hover{border-color:var(--line-strong)}
  ol.ev > li:last-child{margin-bottom:0}
  ol.ev > li::before{content:counter(e);position:absolute;left:15px;top:15px;width:24px;height:24px;
    border-radius:50%;background:var(--accent);color:#fff;font-family:var(--mono);
    font-size:12.4px;font-weight:700;line-height:24px;text-align:center}
  .ev-t{font-weight:600;font-size:15px;line-height:1.5;margin-bottom:2px}
  .ev-t a{color:var(--ink);border-bottom-color:var(--line-strong)}
  .ev-t a:hover{color:var(--accent);border-bottom-color:var(--accent)}
  .ev-m{font-size:12.6px;color:var(--ink-3)}
  .ev-q{margin:10px 0 0;padding:9px 13px;border-left:3px solid var(--line);background:#f7f9fc;
    border-radius:0 6px 6px 0;font-size:13.8px;color:var(--ink-2)}
  .ev-f{margin-top:9px;display:flex;gap:14px;flex-wrap:wrap;font-size:12.2px;color:var(--ink-3)}
  details.cite{margin-top:11px;font-size:13.4px}
  details.cite summary{cursor:pointer;color:var(--ink-3);font-size:12.6px;user-select:none;
    list-style:none;display:flex;align-items:center;gap:5px;width:fit-content}
  details.cite summary::-webkit-details-marker{display:none}
  details.cite summary::after{content:"▾";font-size:9.5px;opacity:.7}
  details.cite[open] summary::after{content:"▴"}
  details.cite summary:hover{color:var(--accent)}
  details.cite ul{margin:10px 0 0;padding-left:16px;border-left:2px solid var(--line)}
  details.cite li{margin-bottom:7px;line-height:1.6;font-size:13.2px}
  details.cite li:last-child{margin-bottom:0}
  .inlib{color:var(--accent-2);font-weight:700}

  /* 三列补充建议 */
  .cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px}
  .col{max-width:34em}
  .col h3{margin:0 0 10px;font-size:13.6px;padding-bottom:6px;border-bottom:2px solid var(--line)}
  .col h3 .n{font-weight:400;color:var(--ink-3)}
  .col ul{margin:0;padding-left:18px}
  .col li{margin-bottom:9px;font-size:14.2px}
  .col .why{display:block;color:var(--ink-3);font-size:12.4px;margin-top:2px}
  .tag{display:inline-block;padding:1px 7px;border-radius:4px;font-size:11.5px;font-weight:700;
    background:var(--accent-2-soft);color:var(--accent-2);vertical-align:1px;margin-right:5px}

  /* 时间线 */
  ul.tl{margin:0 0 18px;padding:0;list-style:none;position:relative}
  ul.tl::before{content:"";position:absolute;left:6px;top:8px;bottom:8px;width:2px;
    background:var(--line-strong)}
  ul.tl li{position:relative;padding:0 0 20px 32px;margin:0}
  ul.tl li:last-child{padding-bottom:0}
  ul.tl li::before{content:"";position:absolute;left:1px;top:8px;width:12px;height:12px;
    border-radius:50%;background:var(--paper);border:2.5px solid var(--accent)}
  ul.tl .yr{font-family:var(--mono);font-weight:700;font-size:13.4px;color:var(--accent);
    margin-right:9px}

  /* 争议块 */
  .issue{border:1px solid var(--line);border-left:3px solid #d09a2a;border-radius:var(--radius);
    padding:15px 18px;margin:0 0 14px;background:#fff}
  .issue:last-child{margin-bottom:0}
  .issue h4{margin:0 0 10px;font-size:15.2px}
  .pos{margin:0 0 9px;padding-left:14px;border-left:2px solid var(--line)}
  .pos:last-of-type{margin-bottom:0}
  .pos b{font-weight:700}
  .pos .who{color:var(--ink-3);font-size:13.4px}
  .issue .note{margin:11px 0 0;font-size:13.4px}

  footer.doc{margin-top:22px;text-align:center;font-size:12.8px;color:var(--ink-3);line-height:1.9}
  .toc{display:flex;flex-wrap:wrap;gap:8px;margin:0}
  .toc a{font-size:13.4px;color:var(--ink-2);background:#f7f9fc;border:1px solid var(--line);
    border-radius:999px;padding:5px 13px;border-bottom:1px solid var(--line)}
  .toc a:hover{color:var(--accent);border-color:var(--accent);background:var(--accent-soft)}

  .menu{display:grid;grid-template-columns:repeat(auto-fill,minmax(268px,1fr));gap:14px;margin:0}
  .menu a{display:block;padding:16px 18px;border:1px solid var(--line);border-radius:var(--radius);
    background:#fbfcfe;color:var(--ink);border-bottom:1px solid var(--line)}
  .menu a:hover{border-color:var(--accent);background:var(--accent-soft)}
  .menu .t{font-weight:700;font-size:15.4px;margin-bottom:5px}
  .menu .d{color:var(--ink-3);font-size:13px;line-height:1.7}

  @media print{
    :root{--bg:#fff}
    body{background:#fff;font-size:11.4pt;line-height:1.6}
    .wrap{max-width:none;padding:0}
    header.doc,section{border:none;border-radius:0;padding:0;margin:0 0 16pt;break-inside:auto}
    header.doc::before{display:none}
    .actions,.toc{display:none}
    h2{page-break-after:avoid;font-size:14pt;border-bottom:1.2pt solid var(--line-strong);padding-bottom:4pt}
    h3{page-break-after:avoid}
    .tablewrap{border:1px solid var(--line-strong);overflow:visible;page-break-inside:avoid}
    table{min-width:0;font-size:9.4pt}
    th,td{padding:5pt 7pt}
    thead{display:table-header-group}
    tr,li,.note,.card,.issue{page-break-inside:avoid}
    a{color:var(--ink);border:none}
    .note{background:#f7f9fc !important;color:var(--ink) !important;
          -webkit-print-color-adjust:exact;print-color-adjust:exact}
    .hl,.hl2,.hl-warn{background:transparent !important;color:var(--ink) !important;padding:0}
    details.cite summary{display:none}
    details.cite ul{display:block}
  }
"""

# 共享块样式：公式 / 图 / 要点盒 / 代码 / 文档间导航 / 问答
# 两种布局都用得到，所以不放进布局 CSS。
_ans_CSS_BLOCKS = r"""
  .nav{background:var(--accent-soft);border:1px solid #cfe6f7;border-radius:8px;
    padding:10px 18px;font-size:.93em;margin:0 0 22px}
  .nav a{font-weight:600}
  .math{margin:20px 0;text-align:center;overflow-x:auto;overflow-y:hidden}
  .math .eqno{display:block;text-align:right;font-family:var(--mono);font-size:12.4px;
    color:var(--ink-3);margin-top:4px}
  mjx-container{overflow-x:auto;overflow-y:hidden;max-width:100%}
  .math-fallback{display:none;font-family:var(--mono);font-size:13px;color:var(--ink-3);
    background:#f7f9fc;border:1px dashed var(--line-strong);border-radius:6px;
    padding:8px 12px;text-align:left;white-space:pre-wrap;word-break:break-word}
  .no-mathjax .math-fallback{display:block}
  .no-mathjax .math mjx-container,.no-mathjax .math .tex{display:none}
  figure{margin:24px 0;text-align:center}
  figure img{max-width:100%;border:1px solid var(--line);border-radius:8px;background:#fff}
  figcaption{color:var(--ink-3);font-size:13.2px;margin-top:8px;text-align:left;line-height:1.6}
  pre{background:#1e2430;color:#e6edf3;padding:16px 18px;border-radius:8px;overflow-x:auto;
    font-size:13.4px;line-height:1.58;margin:0 0 18px}
  pre code{background:none;color:inherit;padding:0;border:none;font-size:1em}
  .key{background:#eef7ff;border:1px solid #cfe6f7;border-radius:8px;padding:16px 20px;margin:0 0 18px}
  .key ul{margin:8px 0 0;padding-left:22px}
  .key li{margin:6px 0}
  .qa{border:1px solid var(--line);border-radius:var(--radius);padding:16px 20px;
    margin:0 0 16px;background:#fff}
  .qa:last-child{margin-bottom:0}
  .qa h4{margin:0 0 10px;font-size:15.4px;color:#0f4c72}
  .qa .a p{margin:0 0 10px}
  .qa .a p:last-child{margin-bottom:0}
  .qa .rel{margin-top:10px;font-size:13px;color:var(--ink-3)}
  .tag2{display:inline-block;background:#e8f3fa;color:var(--accent);border-radius:4px;
    padding:1px 8px;font-size:12px;margin-right:6px;vertical-align:1px}
"""

# 共享块样式：三线表 / 数值条 / 折线图 / 流程链 / 排行 / 关键数字
# 与 _ans_CSS_BLOCKS 同级：两种布局都用得到。整层追加在 _ans_CSS_BLOCKS 之后、
# _ans_CSS_V2 覆盖层之前 —— 覆盖层里的表格修正（sticky 表头、斑马纹变量）仍然生效。
_ans_CSS_VIS = r"""
  /* ---- 三线表（booktabs 风格）--------------------------------------------
     文字数据优先用三线表：只有顶线、表头下线、底线三条横线，没有竖线与斑马纹。
     期刊表格就是这么排的，扫读时眼睛只需要沿一列往下走，不会被网格打断。 */
  table.book{border-top:2px solid var(--ink);border-bottom:2px solid var(--ink)}
  table.book thead th{background:none;border-bottom:1px solid var(--ink);
    font-size:13px;letter-spacing:.01em;color:var(--ink);white-space:nowrap}
  table.book tbody tr{background:none !important}
  table.book tbody td{border-bottom:none;border-top:none}
  table.book tbody tr.sep td{border-top:1px solid var(--line-strong)}   /* 可选分组线，克制使用 */
  table.book tbody td.k{font-weight:600}
  /* 表下注（表格的脚注）: 数据来源、单位、显著性等 */
  p.tnote{font-size:12.4px;color:var(--ink-3);line-height:1.7;margin:9px 0 0;max-width:var(--measure)}
  p.tnote b{color:var(--ink-2)}

  /* ---- 数值条：一个量级一行，条长即大小 -------------------------------
     左右两列（数值、杂项）用固定宽度，所有行才能对齐成一张可竖向比较的表。 */
  .vbars{margin:0 0 18px}
  .vbars .vrow{display:grid;grid-template-columns:minmax(5.5em,11em) minmax(3em,6.5em) 1fr;
    gap:10px 14px;align-items:center;padding:6px 0}
  .vbars .vrow + .vrow{border-top:1px solid var(--line)}
  .vbars .vrow.head{padding:0 0 7px;border-top:none;border-bottom:1px solid var(--line-strong)}
  .vbars .vrow.head .vlbl,.vbars .vrow.head .vnum,.vbars .vrow.head .vaxhead{
    font-size:11.6px;letter-spacing:.06em;text-transform:uppercase;color:var(--ink-3);font-weight:700}
  .vbars .vlbl{font-size:14px;color:var(--ink);line-height:1.5}
  .vbars .vlbl .sub{font-size:12.2px;color:var(--ink-3);display:block}
  .vbars .rk{display:inline-flex;align-items:center;justify-content:center;width:18px;height:18px;
    border-radius:50%;font-size:11px;font-weight:700;background:var(--surface-3);color:var(--ink-2);
    margin-right:7px;vertical-align:1px}
  .vbars .rk.top{background:var(--accent-2-soft);color:var(--accent-2)}
  .vbars .vnum{text-align:right;font-variant-numeric:tabular-nums lining-nums;font-size:14.4px;
    font-weight:600;color:var(--ink);white-space:nowrap}
  .vbars .vnum .unit{font-weight:400;color:var(--ink-3);font-size:12.4px;margin-left:3px}
  .vbars .vnum .fold{display:block;font-weight:400;color:var(--ink-3);font-size:11.4px}
  .vbars .vtrack{position:relative;height:19px;
    background:linear-gradient(90deg,var(--surface-4),var(--surface-2));border-radius:4px;overflow:hidden}
  .vbars .vbar{position:absolute;left:0;top:3px;bottom:3px;border-radius:3px;min-width:2px;
    background:linear-gradient(90deg,var(--accent),#4b86c9)}
  .vbars .vbar.hi{background:linear-gradient(90deg,var(--accent-2),#3fa98a)}
  .vbars .vbar.mut{background:linear-gradient(90deg,var(--line-strong),#9aa5b4)}
  .vbars .vbar.warn{background:linear-gradient(90deg,#c9942b,#dbb45a)}
  .vbars .vbar.bad{background:linear-gradient(90deg,#b03a3a,#cf6262)}
  .vbars .vbar.thin{background:var(--ink-2)}
  .vbars .vbar .vl{position:absolute;right:6px;top:0;bottom:0;display:flex;align-items:center;
    font-size:11.4px;font-weight:600;color:#fff;text-shadow:0 1px 1px rgba(0,0,0,.22);white-space:nowrap}
  .vbars .vbar.short .vl{right:auto;left:calc(100% + 6px);color:var(--ink-2);text-shadow:none}
  .vbars .vobj{position:absolute;top:-2px;bottom:-2px;width:0;border-left:2px dashed var(--accent-2)}
  .vbars .vthr{position:absolute;top:-2px;bottom:-2px;width:0;border-left:2px dashed #b03a3a}
  .vbars .vaxis{position:relative;height:24px;margin-top:2px}
  .vbars .vaxis .tk{position:absolute;top:5px;font-size:11.4px;color:var(--ink-3);
    font-variant-numeric:tabular-nums;transform:translateX(-50%);white-space:nowrap}
  .vbars .vaxis .tk.first{transform:none}
  .vbars .vnote{grid-column:1/-1;font-size:12.4px;color:var(--ink-3);line-height:1.65;margin:-2px 0 2px}
  .vbars .vstats{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 12px;font-size:12.4px;color:var(--ink-2)}
  .vbars .vstats span{background:var(--surface-2);border:1px solid var(--line);border-radius:999px;
    padding:3px 10px;font-variant-numeric:tabular-nums}
  .vbars .vstats b{color:var(--ink)}
  .vlegend{display:flex;flex-wrap:wrap;gap:14px;font-size:12.4px;color:var(--ink-3);margin:8px 0 0}
  .vlegend i{display:inline-block;width:22px;height:0;border-top:2px dashed var(--accent-2);
    vertical-align:middle;margin-right:5px}
  .vlegend i.bad{border-top-color:#b03a3a}
  .vlegend i.sw{height:11px;border:none;border-radius:2px;
    background:linear-gradient(90deg,var(--accent),#4b86c9);vertical-align:-1px}

  /* ---- 折线图：内联 SVG，零外部请求 ----------------------------------- */
  figure.vchart{margin:0 0 18px}
  figure.vchart svg{width:100%;height:auto;display:block;background:var(--paper);
    border:1px solid var(--line);border-radius:var(--radius)}
  figure.vchart .vcap{font-size:12.6px;color:var(--ink-3);margin-top:8px;line-height:1.65}
  figure.vchart .vcap b{color:var(--ink-2)}
  .vchart-txt{font-family:var(--mono);font-size:12.6px;white-space:pre-wrap;word-break:break-word;
    background:var(--surface-4);border:1px dashed var(--line-strong);border-radius:6px;padding:10px 13px}

  /* ---- 流程链：横向节点 + 箭头，换行即换行（不做脆弱的 SVG 连线）-------- */
  .flow{margin:0 0 18px}
  .flow .fchain{display:flex;flex-wrap:wrap;align-items:center;gap:7px 8px}
  .flow .fnode{display:inline-flex;align-items:baseline;gap:7px;border:1px solid var(--line-strong);
    background:var(--paper);border-radius:999px;padding:7px 14px;font-size:13.8px;color:var(--ink);
    box-shadow:var(--shadow);line-height:1.4}
  .flow .fnode .n{font-family:var(--mono);font-size:11.4px;color:var(--ink-3);font-weight:700}
  .flow .fnode .st{font-size:11.2px;font-weight:700;padding:1px 7px;border-radius:999px}
  .flow .fnode .st.ok{background:var(--accent-2-soft);color:var(--accent-2)}
  .flow .fnode .st.bad{background:var(--danger-soft);color:var(--danger)}
  .flow .fnode .st.warn{background:var(--warn-soft);color:var(--warn)}
  .flow .fnode .st.note{background:var(--surface-3);color:var(--ink-2)}
  .flow .fnode.entry{background:var(--accent);border-color:var(--accent);color:#fff}
  .flow .fnode.entry .n{color:rgba(255,255,255,.75)}
  .flow .fnode.exit{border-color:var(--accent-2);border-width:1.5px}
  .flow .farrow{color:var(--line-strong);font-size:15px;flex:none}
  .flow .fback{display:flex;gap:9px;align-items:flex-start;margin-top:12px;padding:9px 14px;
    border:1px dashed #c9942b;border-radius:8px;background:var(--warn-soft);
    font-size:12.8px;color:var(--warn);line-height:1.6}
  .flow .fback .ic{font-weight:700;flex:none}
  .flow .fback.warn2{border-color:var(--line-strong);background:var(--surface-2);color:var(--ink-2)}
  .flow .fback b{color:inherit}
  .flow .fnote{font-size:12.4px;color:var(--ink-3);margin-top:10px;line-height:1.65}

  /* ---- 关键数字卡（KPI）---------------------------------------------- */
  .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));gap:12px;margin:0 0 18px}
  .kpis.one{grid-template-columns:minmax(0,340px)}
  .kpi{background:var(--surface-2);border:1px solid var(--line);border-radius:var(--radius);
    padding:13px 15px}
  .kpi .klbl{font-size:12.2px;color:var(--ink-3);line-height:1.5;margin-bottom:5px}
  .kpi .kval{font-size:26px;font-weight:700;line-height:1.15;color:var(--ink);
    font-variant-numeric:tabular-nums lining-nums;letter-spacing:-.01em}
  .kpi .kval .unit{font-size:14px;font-weight:500;color:var(--ink-2);margin-left:3px}
  .kpi .kval .pre{font-size:15px;font-weight:500;color:var(--ink-3);margin-right:2px}
  .kpi.ok .kval{color:var(--accent-2)} .kpi.warn .kval{color:var(--warn)}
  .kpi.bad .kval{color:var(--danger)}
  .kpi .kvrange{font-size:12.2px;color:var(--ink-3);margin-top:5px;font-variant-numeric:tabular-nums}
  .kpi .kvslope{font-size:12px;color:var(--ink-3);margin-top:4px}
  .kpi .kvthr{position:relative;height:15px;margin:8px 0 0}
  .kpi .kvthr .rail{position:absolute;left:0;right:0;top:6px;height:2px;background:var(--line)}
  .kpi .kvthr .mk{position:absolute;top:0;width:0;border-left:2px dashed #b03a3a;height:14px}
  .kpi .kvthr .mk2{position:absolute;top:0;width:0;border-left:2px solid var(--accent-2);height:14px}
  .kpi .kvthr .lbl{position:absolute;top:-2px;font-size:10.6px;color:var(--ink-3);white-space:nowrap}
  .kpi .ksrc{display:block;font-size:11.6px;color:var(--ink-3);margin-top:7px}

  /* ---- 表格数据可视化：格内条 / 热力着色 ------------------------------ */
  td .cellbar{display:block;position:relative;height:14px;min-width:54px}
  td .cellbar i{display:block;height:14px;border-radius:3px;
    background:linear-gradient(90deg,var(--accent),#4b86c9);opacity:.85}
  .vbars .vbar.thin{background:var(--ink-2)}
  /* i.S 与表格同一口径：用字符串长度近似体量，不做数值解析（列里可能混着 ">10" 这类文字） */
  td .cellbar i.S{background:var(--ink-2);opacity:.72;border-radius:2px;height:9px;margin-top:2px}
  td.heat{font-weight:600}
  @media print{
    /* 热力与条形承载的是数据本身，打印时必须保留 */
    td.heat,td .cellbar i,.kpi,.vbars .vbar,.vbars .vtrack,.kpi .kvthr .mk2{
      -webkit-print-color-adjust:exact;print-color-adjust:exact}
    table.book{border-top-width:1.6pt;border-bottom-width:1.6pt}
    table.book thead th{border-bottom-width:1pt}
    .flow .fnode{box-shadow:none}
    figure.vchart svg{border-color:var(--line-strong)}
  }
  @media (prefers-color-scheme:dark){
    .vbars .vtrack{background:linear-gradient(90deg,var(--surface-3),var(--surface-2))}
    .flow .fnode{background:var(--surface-2)}
    .flow .fnode.entry{background:var(--accent);color:#101418}
    .flow .fnode.entry .n{color:rgba(0,0,0,.55)}
    .vbars .vbar .vl{text-shadow:none;color:#0f1216}
    .kpi{background:var(--surface-2)}
    figure.vchart svg{background:var(--paper)}
  }
"""

# doc 布局：长文式（推导 / 答疑 / 教学笔记）。
# 只覆盖版式外壳，块级组件与 card 布局共用。
_ans_CSS_DOC = r"""
  body{background:#f0f2f5}
  .wrap{max-width:980px;background:var(--paper);padding:52px 60px 76px;
    box-shadow:0 1px 24px rgba(0,0,0,.08)}
  /* 长文布局里表格/公式更宽，正文行宽放宽到 50em 但仍留边 */
  .lead,p,ul:not(.tl)>li,ol>li{max-width:50em}
  header.doc{border:none;border-radius:0;padding:0;margin:0 0 6px;background:none}
  header.doc::before{display:none}
  h1{font-size:1.85em;line-height:1.35}
  .sub{margin-bottom:34px;font-size:.94em}
  .actions{margin:14px 0 0}
  section{background:none;border:none;border-radius:0;padding:0;margin:52px 0 0}
  h2{font-size:1.28em;border-bottom:2px solid var(--line);padding-bottom:8px;margin:0 0 14px}
  h2 .num{background:none;color:var(--accent);padding:0;transform:none;
    font-size:.86em;letter-spacing:0}
  h3{color:#0f4c72}
  .lead{font-size:1em}
  .card{background:#fbfcfe}
  footer.doc{text-align:left;border-top:1px solid var(--line);padding-top:18px;margin-top:60px}
  @media print{
    body{background:#fff}
    .wrap{box-shadow:none;padding:0;max-width:none;background:#fff}
    section{margin:0 0 16pt}
    h2{border-bottom:1.2pt solid var(--line-strong)}
  }
"""

# MathJax：只在信封真的用了公式时才注入（见 render 的 allow_mathjax）。
# 注意：这是本渲染器唯一的可选外部依赖；不带公式的输出保持零外部请求。
_ans_MATHJAX_URL = "https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-svg.js"
_ans_MATH_HEAD = (
    "<script>window.MathJax={tex:{inlineMath:[['$','$']],displayMath:[['$$','$$']]},"
    "svg:{fontCache:'global'}};</script>\n"
    '<script id="MathJax-script" async src="%s"></script>\n'
    # 故障域收窄：只监听 MathJax 这一个脚本的失败。曾经用全局 error 捕获，
    # 结果页面里任何一张 data: 图解码失败都会给 body 挂 no-mathjax，
    # **全页公式退化成裸 TeX** —— 公式正是这个工具的高价值内容。
    "<script>(function(){var s=document.getElementById('MathJax-script');"
    "function ok(){document.body.classList.remove('math-pending');"
    "document.body.classList.add('math-ready')}"
    "function fail(){document.body.classList.remove('math-pending');"
    "document.body.classList.add('no-mathjax')}"
    "if(s){s.addEventListener('error',fail)}"
    "setTimeout(function(){var m=window.MathJax;"
    "if(m&&m.startup&&m.startup.document){ok()}else{fail()}},4000)})();</script>"
    % _ans_MATHJAX_URL)

_ans_LAYOUTS = {"card": "", "doc": _ans_CSS_DOC}

# ------------------------------------------------------------------ v002 覆盖层
# 增量修正，**不动 v001 的结构**：同特异性下后写的胜出，所以整层追加在最后，
# 出问题删掉这一段即可回退。每条都对应一次实测（依据
# `docs/研究-视觉与体验-20261007.md`，含对比度实算与来源链接）：
#   ① 行宽：--measure 46em = 46 汉字，超 WCAG 1.4.8 的 CJK 上限（40）
#   ② 表格：斑马纹 1.04:1、表头底 1.09:1 等于没画；数字列未右对齐；sticky 被吞
#   ③ 打印：pre 深底浅字在纸上≈1.1:1 → 代码块消失；高亮被强制 transparent
#   ④ 暗色：纯 CSS 变量覆盖，零 JS、零闪烁
#   ⑤ sup.c 未重置 line-height → 带引注的行比别的行高一截
#   ⑥ 结论先行：主要结论要和 6 个元数据芯片抢注意力
#   ⑦ 强调色收敛：hover 与 .hl 同色 = 瞬时状态与持久语义混为一谈
#   ⑧ 中文排版：中西混排间距 / 标点挤压 / 避头尾（渐进增强，不支持则忽略）
#   ⑨ 焦点兜底、reduced-motion、语义地标、跳转链接
#   ⑩ .tex 加载前不可见（消 FOUT）、MathJax 故障域收窄、复制排除 UI
_ans_CSS_V2 = r"""
  :root{
    --measure:38em;          /* 38 汉字/行，给 40 留缓冲（em 在中文里 1em = 1 字） */
    --measure-doc:40em;
    --measure-narrow:34em;
    --mark:#fff2c2; --mark-ink:#5a4300;          /* 行内高亮与链接彻底分家 */
    --surface-2:#fbfcfe; --surface-3:#f2f5f9; --surface-4:#f7f9fc;
    --zebra:#eff3f9;                             /* 1.11:1：看得见但不抢戏 */
    --code-bg:#1e2430; --code-ink:#e6edf3;
  }
  body{text-autospace:normal;text-spacing-trim:normal;line-break:strict;overflow-wrap:anywhere}
  /* 数值条/统计/证据脚：以前只靠相邻文本节点，压缩后单位、倍数、score 会粘在一起 */
  .vbars .vstats{display:flex;flex-wrap:wrap;gap:8px}
  .vbars .vnum{display:flex;align-items:baseline;justify-content:flex-end;gap:4px;min-width:8.5em}
  .vbars .vnum .unit,.vbars .vnum .fold{white-space:nowrap}
  .ev-f{display:flex;flex-wrap:wrap;gap:4px 14px}
  p{text-wrap:wrap}                              /* pretty 对长文才有意义，全局开是浪费 */
  .lead{text-wrap:pretty}
  .sub{max-width:var(--measure)}
  .ev-q,details.cite li{max-width:var(--measure-narrow)}
  ol.ev>li,ul.tl>li{max-width:var(--measure)}

  /* ⑤ 上标引注不再顶高行框：line-height:0 把视觉效果与行盒计算解耦 */
  sup.c{font-size:.72em;line-height:0;vertical-align:.42em;font-family:var(--mono);
    color:var(--accent);font-weight:700;margin-left:1px}

  /* ⑦ 一个颜色一个职责：accent 只留给「可点击 + 章节编号」 */
  .hl{background:var(--mark);color:var(--mark-ink)}
  .hl2{background:var(--accent-2-soft);color:#0a4f3d}
  /* 编号芯片只在 card 布局做成实心（那里是卡片式语言）；doc 布局保留"裸数字"，
     否则 8 个深色方块会把长文的呼吸感压死。 */
  body.layout-card h2 .num{background:var(--ink);color:#fff}
  body.layout-doc h2 .num{background:none;color:var(--ink-2)}
  ol.ev>li::before{background:var(--ink-2)}
  a{color:var(--accent);text-decoration:underline;text-decoration-thickness:1px;
    text-underline-offset:2px;text-decoration-color:rgba(31,95,168,.35)}
  a:hover{text-decoration-color:currentColor}

  /* ② 表格从「有边框的网格」改成「可扫读的数据面板」 */
  .tablewrap{overflow:auto;max-height:min(72vh,760px)}
  thead th{white-space:normal;position:sticky;top:0;z-index:2;background:var(--surface-3);
    box-shadow:inset 0 -1px 0 var(--line-strong)}
  tbody tr:nth-child(even){background:var(--zebra)}
  tbody tr:hover{background:var(--surface-4)}     /* hover 用中性灰，不用强调色 */
  td.num,th.num{text-align:right;font-variant-numeric:tabular-nums lining-nums}
  th.num{padding-right:18px}
  caption{font-size:14.6px;font-weight:600;color:var(--ink);padding:12px 14px;
    background:var(--surface-2)}

  /* ⑥ 结论先行：主要结论是页面第二视觉层（仅次于标题） */
  .bluf{font-size:17.5px;line-height:1.62;font-weight:500;color:#0a3f31;
    background:var(--accent-2-soft);border-left:5px solid var(--accent-2);
    border-radius:0 8px 8px 0;padding:18px 22px;margin:0 0 14px;max-width:none}
  .bluf .t{display:block;font-size:12.5px;letter-spacing:.08em;text-transform:uppercase;
    opacity:.85;margin-bottom:6px}
  .bluf p{margin:0;max-width:none}
  /* 头部降噪：元数据芯片不再和结论争注意力 */
  h1{font-size:26px;line-height:1.34}
  .eyebrow{font-size:11.5px;letter-spacing:.12em}
  .metarow{gap:6px;font-size:12.5px}
  .badge{background:transparent;border:none;padding:2px 0;color:var(--ink-3)}
  .badge b{color:var(--ink-2)}
  .badge.ok,.badge.warn,.badge.mut{padding:4px 11px;border:1px solid transparent}
  .badge.ok{background:var(--accent-2-soft)}
  .badge.warn{background:var(--warn-soft)}
  .badge.mut{background:var(--surface-3)}
  .badge+.badge::before{content:"·";margin-right:6px;color:var(--line-strong)}
  .toc a{background:none;border:none;padding:2px 0;font-size:13px;color:var(--ink-2)}
  .toc a::before{content:"§";color:var(--line-strong);margin-right:4px}

  /* ⑨ 焦点兜底（两色焦点环：深色元件上也够 3:1）+ 动效尊重系统设置 */
  :where(a,button,summary,details,[tabindex]):focus-visible{
    outline:3px solid var(--accent);outline-offset:2px;border-radius:4px;
    box-shadow:0 0 0 1px #fff}
  :focus:not(:focus-visible){outline:none}
  @media (prefers-reduced-motion:reduce){
    *,*::before,*::after{animation-duration:.01ms !important;animation-iteration-count:1 !important;
      transition-duration:.01ms !important;scroll-behavior:auto !important}}
  .skip{position:absolute;left:-9999px}
  .skip:focus{position:static;display:inline-block;margin:8px;padding:8px 14px;background:#fff;
    border:2px solid var(--accent);border-radius:8px}

  /* ⑩ 公式：加载完成前不显示裸 TeX（消 FOUT 与位移）；JS 关了则由 <noscript> 兜底 */
  .math-pending .tex{visibility:hidden}
  .math-ready .tex{visibility:visible}

  /* ⑪ 第一跳（instant）：**答案占首屏**，其余格子退成小字行。
     通用排版（结论摘要 + 每格一卡）在 300 字符的直答卡上是纯负担：
     首屏先看到一张几乎空的目录卡，答案被挤到第二屏 —— 与"5 秒内给可用信息"相反。 */
  body.tpl-instant h1{font-size:23px;line-height:1.32}
  body.tpl-instant .sub{display:none}
  body.tpl-instant header.doc{padding:24px 28px 20px}
  body.tpl-instant .actions .act{font-size:13px;padding:6px 12px}
  body.tpl-instant footer.doc{margin-top:22px}
  .sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;
    clip:rect(0 0 0 0);white-space:nowrap;border:0}
  section.instant-lead{margin-top:14px;padding:0;background:none;border:none;box-shadow:none}
  section.instant-lead .bluf{font-size:19px;margin:0}
  .bluf.warn{background:var(--warn-soft);border-left-color:var(--warn);color:var(--warn)}
  .bluf.mut{background:var(--surface-3);border-left-color:var(--line-strong);color:var(--ink-2)}
  section.instant-row{display:flex;gap:14px;align-items:baseline;background:var(--paper);
    border:1px solid var(--line);border-radius:var(--radius);padding:12px 18px;margin-top:8px;
    box-shadow:var(--shadow)}
  section.instant-row .lbl{flex:0 0 4.4em;color:var(--ink-3);font-size:13px}
  section.instant-row .val{color:var(--ink-2)}

  /* ③ 打印：深色块换浅底、高亮保留、链接展开 URL、长表让浏览器分页 */
  @media print{
    body{background:#fff}
    .wrap{max-width:none;padding:0;box-shadow:none}
    header.doc{box-shadow:none}
    .actions,.skip,.toc{display:none}
    pre{background:#f7f9fc !important;color:#171a1f !important;
      border:1px solid var(--line-strong);font-size:9.4pt;line-height:1.45}
    pre code{color:inherit}
    .hl{background:#eef3fa !important;color:#123f75 !important;padding:0 2px;
      -webkit-print-color-adjust:exact;print-color-adjust:exact}
    .hl2{background:#e8f5f0 !important;color:#0a4f3d !important;
      -webkit-print-color-adjust:exact;print-color-adjust:exact}
    .hl-warn{background:#fdecec !important;color:#8a1f1f !important;
      -webkit-print-color-adjust:exact;print-color-adjust:exact}
    main a[href^="http"]::after{content:" <" attr(href) ">";font-size:8.6pt;color:#454d5a;
      word-break:break-all}
    .tablewrap{overflow:visible;max-height:none;page-break-inside:auto;break-inside:auto}
    thead{display:table-header-group}
    tr{page-break-inside:avoid;break-inside:avoid}
    @page{margin:18mm 16mm}
  }

  /* ④ 暗色：纯变量覆盖，零 JS、零闪烁（未表达偏好时 light 生效 = 现行行为） */
  @media (prefers-color-scheme:dark){
    :root{
      --bg:#0f1216;--paper:#191d24;--ink:#e8ebf0;--ink-2:#c3cad6;--ink-3:#a8b1c0;
      --line:#2b323d;--line-strong:#3a4150;--accent:#8ab4f8;--accent-soft:#1b2736;
      --accent-2:#6ee7b7;--accent-2-soft:#14251f;--warn:#f0c674;--warn-soft:#2a2317;
      --danger:#f28b82;--danger-soft:#2c1b1b;--mark:#4a3c14;--mark-ink:#ffe9a8;
      --surface-2:#1e232b;--surface-3:#222833;--surface-4:#1c2129;--zebra:#1e2430;
      --code-bg:#12151a;--code-ink:#dfe6ee;--shadow:none
    }
    section,header.doc,.qa,.tablewrap,table{background:var(--paper)}
    .card,figure img{background:var(--surface-2)}
    .key,.nav{background:var(--surface-2);border-color:var(--line)}
    pre{background:var(--code-bg);color:var(--code-ink)}
    .note{color:var(--ink)} .note.ok{color:var(--ink)} .note.warn{color:var(--warn)}
    .bluf{color:var(--ink)}
    .hl{background:var(--mark);color:var(--mark-ink)}
    .hl2{color:var(--accent-2)} .hl-warn{color:var(--danger)}
    .qa h4,h3{color:var(--accent)}
    code{background:var(--surface-3);border-color:var(--line)}
    a{text-decoration-color:rgba(138,180,248,.4)}
  }
"""

_ans_JS = r"""
function copyAll(){
  // 复制的是**正文**：把按钮、目录、跳转链接、导航这些 UI 剥掉再取文本。
  // 旧实现直接取 document.body.innerText，会把「🖨 打印 / 导出 PDF」这类按钮文案
  // 和整个目录一起复制进 Markdown —— 用户每次都得手工清理。
  var root = document.querySelector('main') || document.querySelector('.wrap') || document.body;
  var clone = root.cloneNode(true);
  var drop = clone.querySelectorAll('.actions,.toc,.skip,button,.nav');
  for (var i = drop.length - 1; i >= 0; i--) { drop[i].parentNode.removeChild(drop[i]); }
  var t = clone.innerText.replace(/\n{3,}/g,"\n\n").trim();
  var b = document.querySelector('.actions .act:last-child');
  if (!b) { return; }
  var o = b.textContent;
  var done = function(){ b.textContent='✔ 已复制'; setTimeout(function(){ b.textContent=o; },1600); };
  function fallback(){
    var ta=document.createElement('textarea'); ta.value=t; ta.style.position='fixed'; ta.style.opacity='0';
    document.body.appendChild(ta); ta.select();
    try{ document.execCommand('copy'); done(); }catch(e){ alert('复制失败，请手动选择文本。'); }
    document.body.removeChild(ta);
  }
  if(navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(t).then(done, fallback);
  } else { fallback(); }
}
"""

# ------------------------------------------------------------------ 预设模板
# 加一种格式 = 加一条。kind 决定用哪个块渲染器。

_ans_TEMPLATES: dict[str, dict] = {
    # #7 第一跳（两段式的 instant 段）：骨架锁死、字数上限写进模板 —— 模型只能填词，
    # 不能发挥。渲染端按 max_chars 强制截断（引擎侧 _answer_clip）。
    "instant": {
        "label": "要点直答",
        "eyebrow": "要点直答 · Instant",
        "desc": "结论 → 适用条件与限制 → 尚待解决的问题",
        "sections": [
            {"id": "answer", "label": "答案", "kind": "note", "variant": "ok", "max_chars": 120},
            {"id": "limit", "label": "限制", "kind": "text", "max_chars": 80},
            {"id": "followup", "label": "待解决", "kind": "text", "max_chars": 80},
        ],
    },
    "review": {
        "label": "综述式",
        "eyebrow": "文献检索汇总 · Review",
        "desc": "跨文献综合：主要结论 → 研究现状 → 技术路线 → 分歧与局限 → 评价",
        "sections": [
            {"id": "bottom_line", "label": "主要结论", "kind": "note", "variant": "ok"},
            {"id": "state", "label": "研究现状", "kind": "text"},
            {"id": "methods", "label": "技术路线", "kind": "cards"},
            {"id": "limits", "label": "分歧与局限", "kind": "list"},
            {"id": "takeaway", "label": "评价", "kind": "note", "variant": ""},
        ],
    },
    "method": {
        "label": "方法总结式",
        "eyebrow": "文献检索汇总 · Method",
        "desc": "单方法拆解：原理与方法 → 适用范围 → 工艺流程 → 参数与条件 → 局限性",
        "sections": [
            {"id": "what", "label": "原理与方法", "kind": "text"},
            {"id": "when", "label": "适用范围", "kind": "text"},
            # 流程链：nodes 给节点数组（或 ["甲","乙","丙"] 简写），edges 给回流/失败支路
            {"id": "steps", "label": "工艺流程", "kind": "flow"},
            {"id": "params", "label": "参数与条件", "kind": "table"},
            {"id": "figure", "label": "示意", "kind": "figure"},
            {"id": "caveats", "label": "局限性", "kind": "list"},
        ],
    },
    "compare": {
        "label": "横向对比",
        "eyebrow": "文献检索汇总 · Comparison",
        "desc": "横向对比：主要结论 → 对比表（列可标注 num/heat/bar）→ 数值对比 → 读表说明",
        "sections": [
            {"id": "bottom_line", "label": "主要结论", "kind": "note", "variant": "ok"},
            # 表格列可加：num 数字右对齐 / heat 列内热力 / bar 格内条（哪个大不用读数字）
            {"id": "matrix", "label": "对比表", "kind": "table"},
            {"id": "numbers", "label": "数值对比", "kind": "rank"},
            {"id": "reading", "label": "读表说明", "kind": "list"},
        ],
    },
    "timeline": {
        "label": "时间线",
        "eyebrow": "文献检索汇总 · Timeline",
        "desc": "按年份梳理研究脉络；有逐年数值则绘制趋势，否则列为事件",
        "sections": [
            {"id": "bottom_line", "label": "主要结论", "kind": "note", "variant": "ok"},
            # line 两种给法由渲染器自动选：{"x":[年份…],"series":[…]} 画曲线；
            # [{"year","text"}] 排事件时间轴。同一个字段，agent 不用换模板。
            {"id": "line", "label": "研究脉络", "kind": "timeline"},
        ],
    },
    # 数值型发现：读者只需要"哪个大、差几倍、有没有超标"时用这个，而不是写成段落
    "numeric": {
        "label": "数值发现",
        "eyebrow": "数值汇总 · Numbers",
        "desc": "主要结论 → 关键指标 → 数值对比 / 变化趋势 → 读图说明",
        "sections": [
            # bottom_line **必须排首位**：渲染器把首位的它提到「结论摘要」当主要结论，
            # 排在别处会让同一句结论在页面上出现两次（实测踩过）。所有模板都遵此约定。
            {"id": "bottom_line", "label": "主要结论", "kind": "note", "variant": "ok"},
            {"id": "kpis", "label": "关键指标", "kind": "scalars"},
            {"id": "numbers", "label": "数值对比", "kind": "rank"},
            {"id": "trend", "label": "变化趋势", "kind": "chart"},
            {"id": "reading", "label": "读图说明", "kind": "list"},
        ],
    },
    "concept": {
        "label": "概念速查卡",
        "eyebrow": "文献检索汇总 · Glossary",
        "desc": "术语 → 定义 → 出处",
        "sections": [
            {"id": "cards", "label": "术语", "kind": "cards"},
        ],
    },
    "dispute": {
        "label": "争议清单",
        "eyebrow": "文献检索汇总 · Open Questions",
        "desc": "多源冲突并列：争议点 → 各方立场 → 证据",
        "sections": [
            {"id": "bottom_line", "label": "主要结论", "kind": "note", "variant": "warn"},
            {"id": "issues", "label": "分歧点", "kind": "dispute"},
        ],
    },
    "derivation": {
        "label": "推导式",
        "eyebrow": "技术推导 · Derivation",
        "desc": "从约定到结果：体系与约定 → 逐步推导 → 结果 → 数值验证 → 常见误区",
        "sections": [
            {"id": "conventions", "label": "体系与约定", "kind": "text"},
            {"id": "deriv", "label": "推导", "kind": "deriv"},
            {"id": "figure", "label": "示意", "kind": "figure"},
            {"id": "result", "label": "结果", "kind": "key"},
            {"id": "code", "label": "代码接口", "kind": "code"},
            {"id": "verify", "label": "数值验证", "kind": "text"},
            {"id": "pitfalls", "label": "常见误区", "kind": "note", "variant": "warn"},
        ],
    },
    "qa": {
        "label": "概念答疑",
        "eyebrow": "概念答疑 · Q&A",
        "desc": "逐项说明：问题 → 解释 → 相关概念",
        "sections": [
            {"id": "intro", "label": "说明", "kind": "text"},
            {"id": "questions", "label": "问答", "kind": "qa"},
            {"id": "summary", "label": "结论", "kind": "key"},
        ],
    },
}

_ans_VERDICT = {
    "hit":    ("ok",   "命中"),
    "weak":   ("warn", "弱相关 · 不可当证据引用"),
    "doubt":  ("warn", "存疑 · 不可当证据引用"),
    "no_hit": ("mut",  "库内无相关资料"),
}
_ans_DEPTH = {"quick": "快速检索", "deep": "深度检索"}
_ans_SUGGEST_COLS = (("in_library", "库内可查（循引文找到）"),
                ("to_fetch", "建议补库（循引文发现）"),
                ("related", "相关文献"))


# ------------------------------------------------------------------ 渲染工具

def _ans_esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


def _ans_sup(refs) -> str:
    """正文里的上标引注：一次引用多个时写成 [1,2]。"""
    if not refs:
        return ""
    return '<sup class="c">[%s]</sup>' % ",".join(str(r) for r in refs)


_ans__MATH_SPAN = __import__("re").compile(r"\$([^$\n]{1,400})\$")


def _ans__md_inline(s) -> str:
    """只处理 `code`、**bold** 与行内公式 $...$；其余按纯文本转义。

    行内公式是唯一允许「不被转义」的内容，所以先切出来占位、转义完再放回
    （放回时仍要转义 TeX 本身，浏览器解码后 MathJax 读到的才是原文）。
    """
    import re
    s = str(s)
    spans: list = []

    def _stash(m):
        spans.append(m.group(1))
        return "\x00%d\x00" % (len(spans) - 1)

    out = _ans_esc(_ans__MATH_SPAN.sub(_stash, s))
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"\x00(\d+)\x00",
                 lambda m: '<span class="tex">$%s$</span>' % _ans_esc(spans[int(m.group(1))]), out)
    return out


def _ans_blk_note(v, spec, env):
    variant = spec.get("variant", "")
    title = spec.get("label") if spec.get("show_title") else ""
    cls = "note" + ((" " + variant) if variant else "")
    t = '<span class="t">%s</span>' % _ans_esc(title) if title else ""
    body = _ans__md_inline(v) if isinstance(v, str) else _ans__md_inline(v.get("text", ""))
    refs = "" if isinstance(v, str) else _ans_sup(v.get("refs"))
    return '<div class="%s">%s<p>%s%s</p></div>' % (cls, t, body, refs)


def _ans_blk_text(v, spec, env):
    if isinstance(v, str):
        return '<p class="lead">%s</p>' % _ans__md_inline(v)
    return '<p class="lead">%s%s</p>' % (_ans__md_inline(v.get("text", "")), _ans_sup(v.get("refs")))


def _ans_as_list(v):
    """块入口的类型归一：字符串当单元素、dict 当单条。

    旧实现直接 `for it in v` —— agent 把单元素数组写成字符串时就会"逐字 bullet"
    （实测：`compare.reading` 整节变成每个字一个 <li>）；`cards` 则会 AttributeError
    丢掉整页。类型归一放在这里，比让每个调用方"记得传数组"可靠得多。
    """
    if v is None or v == [] or v == {}:
        return []
    if isinstance(v, str):
        return [v]
    if isinstance(v, dict):
        return [v]
    try:
        return list(v)
    except TypeError:
        return [str(v)]


def _ans_blk_list(v, spec, env):
    v = _ans_as_list(v)
    lis = []
    for it in v:
        if isinstance(it, dict):
            lis.append("<li>%s%s</li>" % (_ans__md_inline(it.get("text", "")), _ans_sup(it.get("refs"))))
        else:
            lis.append("<li>%s</li>" % _ans__md_inline(it))
    return "<ul>%s</ul>" % "".join(lis)


def _ans_blk_steps(v, spec, env):
    v = _ans_as_list(v)
    lis = []
    for i, it in enumerate(v, 1):
        if isinstance(it, dict):
            lis.append('<li><span class="hl">%d</span> %s%s</li>'
                       % (i, _ans__md_inline(it.get("text", "")), _ans_sup(it.get("refs"))))
        else:
            lis.append('<li><span class="hl">%d</span> %s</li>' % (i, _ans__md_inline(it)))
    return "<ol style=\"padding-left:1.6em\">%s</ol>" % "".join(lis)


def _ans__col_num(c) -> bool:
    """该列是否按数字处理：显式 `num`/`numeric` 或 `align:"right"`。

    数字列必须右对齐 + 等宽数字，否则个位/十位/小数点不对齐，列内无法竖向比较
    （GOV.UK 表格组件为此专门有 numeric 变体）。
    """
    if not isinstance(c, dict):
        return False
    return bool(c.get("num") or c.get("numeric") or str(c.get("align") or "") == "right")


def _ans_parse_pipe_table(text):
    """Markdown 管道表 → {columns, rows}。

    agent 写表最自然的动作是直接敲一张 markdown 表（它本来就在写 markdown 答案），
    比让它在 JSON 里手工拼 columns/rows 数组快得多、也不会错行。这里把它接住。
    """
    if not isinstance(text, str) or "|" not in text:
        return None
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    rows_raw = [ln for ln in lines if ln.startswith("|") or "|" in ln]
    if len(rows_raw) < 2:
        return None

    def cells(ln):
        s = ln.strip()
        if s.startswith("|"):
            s = s[1:]
        if s.endswith("|"):
            s = s[:-1]
        return [c.strip() for c in s.split("|")]

    sep_i = None
    for i, ln in enumerate(rows_raw[:3]):
        if re.fullmatch(r"[\s|:\-]+", ln) and "-" in ln and "|" in ln:
            sep_i = i
            break
    if sep_i is None or sep_i != 1:
        return None
    cols = cells(rows_raw[0])
    if not cols:
        return None
    body = [cells(ln) for ln in rows_raw[2:]]
    body = [r for r in body if any(c for c in r)]
    if not body:
        return None
    columns = []
    for j, c in enumerate(cols):
        sample = [r[j] for r in body if j < len(r)]
        numeric = bool(sample) and all(re.fullmatch(r"[+\-−–—~]?\s*[\d.,]+\s*(?:[eE×xX*·^][+\-]?\d+)?\s*[\w%µΩ·/^⁻⁺²³]*",
                                                  s.strip() or "x") for s in sample)
        columns.append({"label": c or ("列%d" % (j + 1)), "num": numeric})
    return {"columns": columns, "rows": body}


def _ans_blk_table(v, spec, env):
    """三线表 + 列内数据可视化（heat=热力着色 / bar=格内条 / num=数字右对齐）。

    v 可以是 {columns, rows} 结构，**也可以直接是 markdown 管道表字符串** —— agent
    本来就在写 markdown，让它顺手敲一张表比让它在 JSON 里拼数组快得多，也不会错行。

    数值列（coll.num / align:"right"）自动右对齐并等宽对齐小数点；`bar` 列额外画一条
    格内条，`heat` 列按列内极值着色 —— 这样"哪个最大"不用读数字就能看出来。
    `rows` 里插一个 "-" 字符串表示分组分隔线（三线表里克制使用）。
    """
    _ans_from_md = isinstance(v, str)
    if _ans_from_md:
        parsed = _ans_parse_pipe_table(v)
        if parsed is None:
            return ('<pre><code>%s</code></pre>' % _ans_esc(v)) if "\n" in v \
                else _ans_blk_text(v, spec, env)
        v = parsed
    if not isinstance(v, dict):
        return _ans_blk_text(str(v), spec, env)
    cols = v.get("columns") or []
    # markdown 管道表没有列 spec：解析器会把数值列标成 num，这里给**第一个数值列**
    # 自动加格内条。旧行为只有显式 bar:true 才画条 —— 于是 agent 直接写 markdown 表时
    # 永远看不到可视化（自检"缺图元 cellbar"暴露的就是它）。
    if _ans_from_md and not any(isinstance(c, dict) and (c.get("bar") or c.get("heat") is not None)
                                for c in cols):
        for c in cols:
            if isinstance(c, dict) and c.get("num"):
                c["bar"] = True
                break
    book = v.get("style", "book") != "grid"
    num_cache = {}
    col_num = {}
    for j, c in enumerate(cols):
        col_num[j] = _ans__col_num(c) or bool(isinstance(c, dict) and c.get("bar"))

    def _cval(cell, j):
        """单元格数值：命中缓存；数字型格内条用数值，字符型退化成字符串长度。"""
        if isinstance(cell, dict):
            return _ans_num_or(_ans_num_parse(cell.get("value", cell.get("text"))), "hi")
        key = str(cell)
        if key not in num_cache:
            rec = _ans_num_parse(cell)
            num_cache[key] = _ans_num_or(rec, "hi") or (len(str(cell)) if str(cell).strip() else None)
        return num_cache[key]

    heat = {}
    for j in range(len(cols)):
        if not (isinstance(cols[j], dict) and (cols[j].get("heat") is not False and cols[j].get("heat"))):
            continue
        vals = [x for x in (_cval(c, j) for c in (r[j] if isinstance(r, list) and j < len(r) else None
                                                 for r in (v.get("rows") or []))) if x is not None]
        if len(vals) >= 2 and max(vals) > min(vals):
            heat[j] = (min(vals), max(vals))
    # 格内条列：按列归一化（列内最大者占满格宽）
    bar_cols = {}
    for j in range(len(cols)):
        if not (isinstance(cols[j], dict) and (cols[j].get("bar") is not False and cols[j].get("bar"))):
            continue
        vals = [x for x in (_cval(c, j) for c in (r[j] if isinstance(r, list) and j < len(r) else None
                                                 for r in (v.get("rows") or []))) if x is not None]
        mx = max(vals) if vals else 0
        bar_cols[j] = mx if mx and mx > 0 else 0

    head = []
    for j, c in enumerate(cols):
        c = c if isinstance(c, dict) else {"label": str(c)}
        head.append('<th scope="col"%s>%s</th>'
                    % (' class="num"' if (col_num.get(j) or j in bar_cols) else "",
                       _ans_esc(c.get("label", c.get("id", "")))))
    rows = []
    for row in (v.get("rows") or []):
        if isinstance(row, str) or row is None:
            if str(row).strip() in ("-", "--", "—"):
                rows.append('<tr class="sep"><td colspan="%d"></td></tr>' % max(1, len(cols)))
                continue
            rows.append("<tr><td>%s</td></tr>" % _ans__md_inline(row))
            continue
        tds = []
        for j, cell in enumerate(row):
            cj = cols[j] if j < len(cols) and isinstance(cols[j], dict) else {}
            classes = [x for x in ("k" if j == 0 and not col_num.get(0) else "",
                                   "num" if (col_num.get(j) or j in bar_cols) else "",
                                   "heat" if j in heat else "") if x]
            style = ""
            if j in heat:
                x = _cval(cell, j)
                lo, hi = heat[j]
                t = None if x is None else (0.5 if hi <= lo else (x - lo) / (hi - lo))
                bg = _ans_heat_bg(t, str(cj.get("tone") or "accent"))
                if bg:
                    style = ' style="background:%s"' % bg
            cls = (' class="%s"' % " ".join(classes)) if classes else ""
            if isinstance(cell, dict):
                base = _ans__md_inline(cell.get("text", "")) + _ans_sup(cell.get("refs"))
            else:
                base = _ans__md_inline(cell)
            inner = base
            if j in bar_cols and bar_cols[j]:
                x = _cval(cell, j)
                frac = max(3.0, min(100.0, (x / bar_cols[j]) * 100.0)) if x is not None else 0.0
                thin = ("S" if col_num.get(j) else "")
                barc = ('<span class="cellbar"><i%s style="width:%.1f%%"></i></span>'
                        % ((' class="%s"' % thin) if thin else "", frac))
                if str(base).strip():
                    inner = base + barc
                else:
                    inner = barc
            tds.append("<td%s%s>%s</td>" % (cls, style, inner))
        rows.append("<tr>%s</tr>" % "".join(tds))
    cap = '<caption>%s</caption>' % _ans__md_inline(v["caption"]) if v.get("caption") else ""
    note = '<p class="tnote">%s</p>' % _ans__md_inline(v["note"]) if v.get("note") else ""
    return ('<div class="tablewrap"><table class="%s">%s<thead><tr>%s</tr></thead>'
            '<tbody>%s</tbody></table></div>%s'
            % ("book" if book else "", cap, "".join(head), "".join(rows), note))




def _ans_blk_cards(v, spec, env):
    out = []
    for it in _ans_as_list(v):
        if not isinstance(it, dict):
            it = {"p": str(it)}
        src = it.get("src")
        srch = '<span class="src">%s%s</span>' % (_ans_esc(src), _ans_sup(it.get("refs"))) if (src or it.get("refs")) else ""
        out.append('<div class="card"><h4>%s</h4><p>%s</p>%s</div>'
                   % (_ans__md_inline(it.get("h", "")), _ans__md_inline(it.get("p", "")), srch))
    return '<div class="grid">%s</div>' % "".join(out)


def _ans_blk_timeline(v, spec, env):
    """时间轴：有逐年数值就画曲线，没有就排事件。

    一个字段两种画法，是为了省掉 agent 的一次判断：它只要把「年份 + 数值」填进来，
    渲染器自己决定这是趋势（曲线）还是事件（时间轴）。判断错了也不会崩 ——
    认得出来就画，认不出来就当事件列出来（纯字符串 "2013：…" 也接住）。
    """
    if isinstance(v, dict) and (v.get("series") or v.get("values") or v.get("lines")
                                or v.get("points")) and not v.get("events"):
        fig = _ans_blk_chart(v, spec, env)
        if fig:
            return fig
    evs = []
    if isinstance(v, dict):
        evs = v.get("events") or v.get("items") or []
    elif isinstance(v, list):
        evs = v
    lis = []
    for it in evs or []:
        if isinstance(it, dict):
            lis.append('<li><span class="yr">%s</span>%s%s</li>'
                       % (_ans_esc(it.get("year", "")), _ans__md_inline(it.get("text", "")),
                          _ans_sup(it.get("refs"))))
        else:
            s = str(it)
            m = re.match(r"^\s*((?:19|20)\d{2}[a-z]?)\s*[:：\-–—]?\s*(.*)$", s)
            if m:
                lis.append('<li><span class="yr">%s</span>%s</li>'
                           % (_ans_esc(m.group(1)), _ans__md_inline(m.group(2))))
            else:
                lis.append('<li><span class="yr">·</span>%s</li>' % _ans__md_inline(s))
    return '<ul class="tl">%s</ul>' % "".join(lis)


def _ans_blk_dispute(v, spec, env):
    out = []
    for it in v or []:
        pos = []
        for p in it.get("positions") or []:
            pos.append('<div class="pos"><b>%s</b> <span class="who">%s</span> %s%s</div>'
                       % (_ans_esc(p.get("claim", "")), _ans_esc(p.get("who", "")),
                          _ans__md_inline(p.get("detail", "")), _ans_sup(p.get("refs"))))
        note = '<div class="note warn">%s</div>' % _ans__md_inline(it["note"]) if it.get("note") else ""
        out.append('<div class="issue"><h4>%s</h4>%s%s</div>'
                   % (_ans_esc(it.get("issue", "")), "".join(pos), note))
    return "".join(out)


def _ans_blk_math(v, spec, env):
    """展示公式。v = TeX 字符串，或 {"tex":..,"no":"(3)","note":..}。

    公式**同时**输出两种形态：给 MathJax 的 `<span class="tex">$$..$$</span>`
    与给离线阅读的 `.math-fallback`（原始 TeX）。MathJax 没加载成功时
    body 会挂上 `no-mathjax`，CSS 自动切到 fallback —— 公式不会消失。
    """
    if isinstance(v, str):
        tex, no, note = v, None, None
    else:
        tex, no, note = v.get("tex", ""), v.get("no"), v.get("note")
    eqno = '<span class="eqno">%s</span>' % _ans_esc(no) if no else ""
    noteh = '<p class="src" style="margin-top:6px">%s</p>' % _ans__md_inline(note) if note else ""
    return ('<div class="math"><span class="tex">$$%s$$</span>'
            '<div class="math-fallback">%s</div>%s</div>%s'
            % (_ans_esc(tex), _ans_esc(tex), eqno, noteh))


_ans_FIGURE_MAX_KB = 200                                    # 单图内联上限（base64 之前）
_ans__FIGURE_MAX_CHARS = int(_ans_FIGURE_MAX_KB * 1024 * 4 / 3)  # base64 膨胀约 33%


def _ans_blk_figure(v, spec, env):
    """配图。v = {"src": "data:image/png;base64,...", "caption":.., "refs":[..]}

    超过上限**不内联**：base64 有 ~33% 膨胀、不能缓存、不能增量解码，单份文件动辄
    上 MB，会把"打开就有感觉"变成"打开要等"。降级成一句人话 + 图注，不留破图。
    """
    src = v.get("src") or ("data:image/%s;base64,%s" % (v.get("fmt", "png"), v.get("b64", "")))
    if len(src) > _ans__FIGURE_MAX_CHARS:
        return ('<div class="note warn"><span class="t">配图未内联</span><p>图片体积超过内联上限'
                '（%d KB &gt; %d KB），已省略。%s</p></div>'
                % (len(src) * 3 / 4 / 1024, _ans_FIGURE_MAX_KB,
                   _ans__md_inline(v.get("caption") or v.get("alt") or "（无图注）")))
    return ('<figure><img src="%s" alt="%s"><figcaption>%s%s</figcaption></figure>'
            % (_ans_esc(src), _ans_esc(v.get("alt", "")), _ans__md_inline(v.get("caption", "")), _ans_sup(v.get("refs"))))


def _ans_blk_code(v, spec, env):
    return '<pre><code>%s</code></pre>' % _ans_esc(v.get("text", "") if isinstance(v, dict) else v)


def _ans_blk_key(v, spec, env):
    """要点盒。v = {"title":..,"items":[str | {text,refs}]} 或直接 items 列表。"""
    if isinstance(v, dict):
        title, items = v.get("title"), _ans_as_list(v.get("items"))
    else:
        title, items = None, _ans_as_list(v)
    t = "<h4>%s</h4>" % _ans__md_inline(title) if title else ""
    lis = []
    for it in items:
        if isinstance(it, dict):
            lis.append("<li>%s%s</li>" % (_ans__md_inline(it.get("text", "")), _ans_sup(it.get("refs"))))
        else:
            lis.append("<li>%s</li>" % _ans__md_inline(it))
    return '<div class="key">%s<ul>%s</ul></div>' % (t, "".join(lis))


def _ans_blk_qa(v, spec, env):
    """问答块。v = [{"q":.., "a":[str | {text,refs}], "rel":..}]（rel = 相关概念/延伸）"""
    out = []
    for it in v or []:
        paras = []
        for p in (it.get("a") or []):
            if isinstance(p, dict):
                paras.append("<p>%s%s</p>" % (_ans__md_inline(p.get("text", "")), _ans_sup(p.get("refs"))))
            else:
                paras.append("<p>%s</p>" % _ans__md_inline(p))
        rel = '<div class="rel">%s</div>' % _ans__md_inline(it["rel"]) if it.get("rel") else ""
        out.append('<div class="qa"><h4>%s</h4><div class="a">%s</div>%s</div>'
                   % (_ans__md_inline(it.get("q", "")), "".join(paras), rel))
    return "".join(out)


def _ans_blk_deriv(v, spec, env):
    """推导步骤：v = [{"h":.., "p":.., "tex":.., "no":"(3)", "refs":[..]}]"""
    out = []
    for i, it in enumerate(v or [], 1):
        out.append("<h3><span class=\"hl\">%d</span> %s</h3>" % (i, _ans__md_inline(it.get("h", ""))))
        if it.get("p"):
            out.append("<p>%s%s</p>" % (_ans__md_inline(it["p"]), _ans_sup(it.get("refs"))))
        if it.get("tex"):
            out.append(_ans_blk_math({"tex": it["tex"], "no": it.get("no")}, spec, env))
    return "".join(out)


# ------------------------------------------------------------------ 可视块辅助
# 设计口径（#8）：**agent 只填数据，引擎负责一切计量**。
# 轴的上下界、刻度、条长、列热度、Top-N 排序、单位归一 —— 都在引擎侧算好。
# 理由是两侧成本不对称：引擎多算一次是零成本的纯函数，agent 多填一个字段是每轮
# 都要付的 token 与出错机会（轴界填错还会画出一张误导人的图）。
#
# 同时**宽容解析**：agent 把数字写成 "10⁶–10⁷"、"1e-3"、"$2.5\times10^4$"、
#   3.2 V 都接受；认不出来就把原文当一行文字渲染，绝不抛异常（渲染是最后一跳，
#   坏了整份答案就没了）。
_UNIT_MAP = {
    "\\%": "%", "\\mu": "µ", "\\Omega": "Ω", "\\times": "×",
    "\\pm": "±", "\\cdot": "·", "\\degree": "°", "\\circ": "°",
    "\\aa": "Å", "\\text": "", "\\mathrm": "", "\\,": " ", "\\;": " ",
}
_SUP_DIGITS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺", "0123456789-+")
_SUB_DIGITS = str.maketrans("₀₁₂₃₄₅₆₇₈₉₋₊", "0123456789-+")
_NUMCHARS = set("0123456789.eE+-×xX*/^()•·,")
_AXIS_SNAP = (1, 2, 2.5, 5, 10)
_AXIS_SUB = (0.1, 0.2, 0.25, 0.5, 1)

# 单位不被渲染成斜体的白名单（带撇号的复合单位、摩尔浓度、微米/微升、Å…
_UNIT_ITALIC_OK = set("mlLgGsAmolKcdeqΩ%")
_UNIT_MULTI = ("mol", "min", "Pa", "eV", "kW", "MW", "nm", "µm", "um", "kV", "mV",
               "mA", "µA", "Hz", "kHz", "MHz", "GHz", "Torr", "bar", "rpm", "at%", "wt%")


def _ans_tex_to_text(s) -> str:
    """TeX 片段 → 可读纯文本（只处理单位/数字里真会出现的那些记号）。"""
    t = str(s or "")
    for k, v in _UNIT_MAP.items():
        t = t.replace(k, v)
    t = t.replace("\\", "")
    return re.sub(r"\s+", " ", t).strip()


def _ans_fmt_num(x, as_power=False, force_sci=False) -> str:
    """数字 → 人读字符串。

    三种显示档，按"读者要拿它做什么"选：
      · as_power  整数次幂写成 10⁵ —— 对数轴刻度必须这样，否则 "1×10⁶ / 100,000 /
                  10,000" 混排，量级要逐个数字去数；
      · force_sci 一根轴跨了 4 个数量级时全部走科学计数，避免同一根轴上两种写法；
      · 默认      常规量级给千分位（10,000），极大/极小给 3 的倍数指数（1×10⁶）。
    """
    if not isinstance(x, float):
        return str(x)
    if x != x:                                                  # NaN
        return "—"
    if x == 0:
        return "0"
    a = abs(x)
    sup = str.maketrans("0123456789-+", "⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺")
    if as_power and a > 0:
        e = math.log10(a)
        if abs(e - round(e)) < 1e-9:
            return "10%s" % str(int(round(e))).translate(sup)
    if force_sci or a >= 1e6 or a < 1e-3:
        e = int(math.floor(math.log10(a)))
        e3 = int(round(e / 3.0)) * 3
        m = x / (10.0 ** e3)
        if 0.1 <= abs(m) < 1000:
            ms = ("%.1f" % m).rstrip("0").rstrip(".")
            return "%s×10%s" % (ms, str(e3).translate(sup))
    if a >= 1000:
        return "{:,}".format(int(round(x)))
    if a >= 1:
        return ("%.3f" % x).rstrip("0").rstrip(".")
    return ("%.4f" % x).rstrip("0").rstrip(".")


def _ans_num_normalize(s):
    """把各种科学计数写法折成 float() 认得的 `1.5e4` 形式。

    **为什么是一个左到右的扫描器，而不是几条 re.sub**：可选的乘号让 `re.sub` 之间会互相
    吃掉对方的结果 —— "3.2×10^4" 里 "10^4" 先被折成 "1e4"，于是变成 "3.2×1e4"（=32000 正确，
    但中间的 "3.2×" 被留下，后续匹配收成 3.2）；"10⁶" 先折成 "10e6"（=10⁷，错 10 倍）。
    用扫描器一次看清 "尾数 [×10] 指数"，这类"规则互相嵌套"的错就不可能出现。

    扫描规则（命中即整段消费）：
      `尾数 [×|x|*|·] 10 指数` → `尾数e指数`   （跳过乘号与底数）
      `尾数 指数`             → `尾数e指数`   （"3.2⁴"、"10^6"）
    """
    s = str(s)
    out, i, n = [], 0, len(s)
    mant = re.compile(r"\d+\.?\d*|\.\d+")
    # 乘号后面必须**连着**底数，指数另算：写成 `[*×]\s*10\s*<$指数>` 会把 "4×10³ cm⁻²" 里的
    # "³" 漏给外层（实测单位被解析成 "0³ cm⁻²"），因为底数 10 与指数之间的上标被拆散了。
    times = re.compile(r"\s*[*×xX·]\s*(?=10(?![.0-9]))")
    base10 = re.compile(r"10(?![.0-9])")
    caret = re.compile(r"\s*\^\s*\{?\s*([+-]?\d{1,3})\s*\}?")
    sup = re.compile(r"[⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺]+")

    def sup_digits(tok):
        return "".join(c.translate(_SUP_DIGITS) for c in tok)

    def exponent(text, pos):
        """"<pos> 处若是指数（^n 或上标），返回 (exponent_string, end)；否则 (None, pos)。"""
        mc = caret.match(text, pos)
        if mc:
            return mc.group(1), mc.end()
        ms = sup.match(text, pos)
        if ms:
            return sup_digits(ms.group(0)), ms.end()
        return None, pos

    while i < n:
        m = mant.match(s, i)
        if not m:
            out.append(s[i])
            i += 1
            continue
        num = m.group(0)
        j = m.end()
        mt = times.match(s, j)
        if mt:
            mb = base10.match(s, mt.end())
            if mb:
                exp, k = exponent(s, mb.end())
                if exp is not None:
                    out.append(num + "e" + exp)
                    i = k
                    continue
        exp, k = exponent(s, j)
        if exp is not None:
            # 底数就是 10（"10^6"）时把 10 折进指数；否则按 尾数×10^exp 处理
            out.append(("1e" + exp) if num in ("10", "10.0") else (num + "e" + exp))
            i = k
            continue
        out.append(num)
        i = j
    return "".join(out)


# 数字匹配。三条硬要求，每条都对应一次实测到的错值：
#   ① 尾数的写法会决定匹配停在哪：`\d*\.?\d+` 会停在 "3" 上，把 "3.2e4" 读成 3，
#      所以用 `\d+\.?\d*|\.\d+`；
#   ② 指数要**先窄后宽**：先认 `e±n`（规整后的真正形式），再认 `×10^n`（规整前的形态），
#      最后才认光秃秃的 `^n`。顺序反了，"3.2×10^4" 会被 `^n` 分支从 "3.2" 处收成 3.2；
#   ③ `×10` 形态下幂次是**必需**的，不能整体做成可选，否则匹配器接受 "…×10" 就收工。
_ANS_MANT = r"[+-]?(?:\d+\.?\d*|\.\d+)"
_ANS_EXP = (r"(?:[eE][+-]?\d{1,3}"
            r"|[*×xX·]\s*10\s*\^\s*\{?\s*[+-]?\d{1,3}\s*\}?"
            r"|\^\s*\{?\s*[+-]?\d{1,3}\s*\}?)?")
_ANS_NUM = _ANS_MANT + _ANS_EXP
_ANS_RANGE_SEP = r"(?:\.\.\.|…|至|到|~|～|[-–—])"


def _ans_num_spans(text):
    """按出现顺序找数字，返回 [(start, end, value, is_range)]。

    **在规整后的文本上匹配**（"10^6"、"3.2×10^4"、"10⁶" 都已折成 1e6 形式），
    这样一条正则就能覆盖所有科学计数写法，不必为每种写法开分支。
    """
    out = []
    i, n = 0, len(text)
    range_re = re.compile(r"(" + _ANS_NUM + r")\s*" + _ANS_RANGE_SEP + r"\s*(" + _ANS_NUM + r")")
    single_re = re.compile(_ANS_NUM)

    def f(s):
        try:
            return float(str(s).replace(",", ""))
        except (ValueError, TypeError):
            return None

    while i < n:
        m = range_re.match(text, i)
        if m:
            a, b = f(m.group(1)), f(m.group(2))
            if a is not None and b is not None:
                lo, hi = (a, b) if a <= b else (b, a)
                out.append((i, m.end(), 0.5 * (lo + hi), True, lo, hi))
                i = m.end()
                continue
        m = single_re.match(text, i)
        if m and m.group(0).strip():
            v = f(m.group(0))
            if v is not None:
                out.append((i, m.end(), v, False, v, v))
                i = m.end()
                continue
        i += 1
    return out


def _ans_num_parse(raw):
    """鲁棒数值解析。返回 {"v","nums","raw","unit","text"}；没有数字则 {"text":原文}。

    agent 会写成什么样是定不死的（"10⁶–10⁷"、"3.2 V"、"1e6"、"10^6"、"$2.5\\times10^4$"、
    ">50"、"未报告" 都会出现），所以目标不是"支持所有写法"，而是两条硬约束：
      ① 认得的写法必须解析**正确** —— 解析错比不解析更糟：图照画，只是画错；
      ② 认不得的必须**退回原文**当文字渲染，绝不编一个数出来。
    """
    out = {"v": None, "nums": [], "raw": "", "unit": "", "text": None, "is_range": False}
    if raw is None or isinstance(raw, bool):
        return out
    if isinstance(raw, (int, float)):
        v = float(raw)
        out.update(v=v, nums=[v], raw=_ans_fmt_num(v))
        return out
    if not isinstance(raw, str):
        return out

    work = clean(re.sub(r"\$+", "", raw.strip()))                # 行内公式的 $ 壳
    if not work:
        return out
    out["raw"] = work
    # 千分位先去掉："1,234" 里的逗号会让数字被切成两段（实测解析成 1）。
    # 只在"逗号后恰好三位数字"的位置去，避免误伤 "1,2" 这种列举。
    body = re.sub(r"(?<=\d),(?=\d{3}\b)", "", work).replace("\u2212", "-")
    norm = _ans_num_normalize(body)
    spans = _ans_num_spans(norm)
    if not spans:
        out["text"] = work
        return out
    out["nums"] = [s[2] for s in spans]
    out["v"] = spans[0][2]
    out["is_range"] = bool(spans[0][3])
    if out["is_range"]:
        # 区间的两个端点要能分别取到（画条时下界给起点、上界给右端）。
        # 不存端点、让调用方事后从文本里再猜一次，是"同一个问题解两遍而且解得不一样"。
        a, b = spans[0][2] - spans[0][4], spans[0][2] + spans[0][4]
        out["lo"], out["hi"] = (a, b) if a <= b else (b, a)
    else:
        out["is_range"] = bool(spans[0][3])
    out["lo"], out["hi"] = spans[0][4], spans[0][5]
    # 单位 = 最后一个数字之后剩下的文字（"4×10³ cm⁻²" → "cm⁻²"）：取不到再往前看
    # **必须用 norm 的索引**：spans 是在规整后的串上量出来的，而 "10⁴"→"1e4" 会改变长度
    # （实测：拿 body 去切会切出 "0³ cm⁻²"、"0^4" 这样的残段当单位）。
    tail = norm[spans[-1][1]:].strip(" \t,;:()[]{}（）、，。±·")
    if not tail and spans[0][0] > 0:
        tail = norm[:spans[0][0]].strip(" \t,;:()[]{}（）、，。±·<=≥≤>")
    if tail:
        out["unit"] = _ans_tex_to_text(tail).strip()[:12]
    return out


def _ans_fmt_with_unit(rec, unit="", dash="–") -> str:
    """(值或区间, 单位) → 显示字符串；单位优先取记录自带的。"""
    r = rec or {}
    u = r.get("unit") or unit or ""
    if r.get("text"):
        return str(r["text"])
    nums = r.get("nums") or []
    if len(nums) >= 2 and re.search(r"[-–—~]\s*[-+]?\d", r.get("raw") or ""):
        return "%s%s%s" % (_ans_fmt_num(nums[0]), dash, _ans_fmt_num(nums[1]))
    v = r.get("v")
    return _ans_fmt_num(v) if v is not None else ""


def _ans_num_or(rec, want="v", unit_div=None):
    """从**解析记录**里取数值：want='v' 代表值 / 'hi' 区间上界 / 'lo' 区间下界。

    注意入参是 _ans_num_parse 的返回记录，不是原始字符串也不是数字列表 ——
    早期版本从 `nums` 列表里取，`nums` 是"文本里出现的所有数字"（包含年份、阈值那些），
    max() 取到的是别处的数。端点必须由 span 直接给出。
    """
    r = rec if isinstance(rec, dict) else _ans_num_parse(rec)
    if not r or r.get("v") is None:
        return None
    v = {"v": r.get("v"), "hi": r.get("hi"), "lo": r.get("lo")}.get(want, r.get("v"))
    if v is None:
        return None
    if unit_div:
        v = v * unit_div
    return float(v)


def _ans_nice_ceil(v, frac=True):
    """把上界抬到 1/2/2.5/5/10 × 10ⁿ（轴读数必须是人能心算的数）。"""
    if v is None or v != v or v <= 0:
        return 1.0
    e = math.floor(math.log10(v))
    for m in _AXIS_SNAP:
        cand = m * (10.0 ** e)
        if cand >= v * (1 - 1e-9):
            return cand
    return 10.0 * (10.0 ** e)


def _ans_nice_ticks(lo, hi, n=5, log=False) -> list:
    """刻度数组：线性优先落在 1/2/2.5/5 的整数倍上，步长不足则退到 0.25 的倍数。"""
    if log:
        import math as _m
        a, b = max(lo, 1e-12), max(hi, 1e-12)
        e0, e1 = _m.floor(_m.log10(a)), _m.ceil(_m.log10(b))
        return [10.0 ** e for e in range(e0, e1 + 1)]
    if hi <= lo:
        return []
    span = float(hi - lo)
    for divs in (n, n - 1, n + 1, 4, 3, 6, 2):
        if divs < 2:
            continue
        coarse = span / float(divs)
        e = math.floor(math.log10(coarse))
        for m in _AXIS_SNAP:
            step = m * (10.0 ** e)
            if step * divs < span * 0.8:
                continue
            if abs(round(step / (10.0 ** e), 6) - 2.5) < 1e-9:
                continue                                       # 2.5 的倍数不给出干净刻度
            start = math.ceil(lo / step - 1e-9) * step
            ticks = []
            v = start
            while v <= hi + step * 1e-6 and len(ticks) < 30:
                ticks.append(round(v / step) * step)
                v += step
            if len(ticks) >= 3:
                return ticks
    for divs in (5, 4, 3, 6):
        step = span / float(divs)
        e = math.floor(math.log10(step))
        for m in _AXIS_SUB:
            s = m * (10.0 ** e)
            if abs(s - step) / step < 0.3:
                step = s
                break
        ticks, v, k = [], lo, 0
        while v <= hi + step * 1e-6 and k < 30:
            ticks.append(round(v / step) * step)
            v += step
            k += 1
        if len(ticks) >= 3:
            return ticks
    return [lo, (lo + hi) / 2.0, hi]


def _ans_scale_pct(v, lo, hi, log=False):
    """数值 → 0..1（用于条长与刻度定位）。log=True 时按对数轴换算。"""
    if v is None:
        return None
    if log and lo > 0 and hi > 0 and v > 0:
        span = math.log10(float(hi)) - math.log10(float(lo))
        return 0.0 if span <= 0 else (math.log10(float(v)) - math.log10(float(lo))) / span
    span = float(hi - lo)
    if span <= 0:
        return 0.0
    return (float(v) - lo) / span


def _ans_rgb(css):
    s = (css or "").strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    try:
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return (31, 95, 168)


_VAR_RGB = {"accent": (31, 95, 168), "accent-2": (13, 107, 82),
            "warn": (138, 90, 0), "danger": (138, 31, 31)}


def _ans_heat_bg(t, tone="accent"):
    """按强度 t∈[0,1] 生成热力底色。

    用 alpha 叠加而不是自己插值 RGB：暗色模式下 --paper 是深色，插值出来的淡色块
    会把文字埋掉；半透明叠加在两种模式下都自动适配。
    """
    if t is None:
        return ""
    tot = _VAR_RGB.get(tone, (31, 95, 168))
    al = 0.0 if t <= 0.001 else min(0.55, 0.06 + 0.49 * (t ** 0.85))
    return "rgba(%d,%d,%d,%.3f)" % (tot[0], tot[1], tot[2], al)


def _ans_table_value_cells(v):
    for row in (v.get("rows") or []):
        if isinstance(row, list):
            for cell in row:
                if isinstance(cell, dict):
                    yield cell


# ------------------------------------------------------------------ 块渲染器

def _ans_blk_bars(v, spec, env):
    """数值条：一个量级一行，条长即大小（rank / compare / 任何"谁多谁少"）。

    v 可给 items 列表，也可直接给列表。每项 {label|name, value|values|[..]|range,
    unit, obj|goal|target, thr|threshold|limit, note, tone, hi}。
    obj/thr 画成同一根条上的虚线标记（目标准线 / 超标红线），量程自动取
    max(最大值, 目标, 阈值) —— agent 不必自己定轴。
    """
    d = v if isinstance(v, dict) else {"items": v}
    items = d.get("items") or d.get("rows") or d.get("list") or []
    if not isinstance(items, list):
        return _ans_blk_list(v, spec, env)
    unit0 = d.get("unit") or ""
    if not items:
        return _ans_blk_text(str(d.get("note") or "（无数据）"), spec, env)

    recs, texts = [], []
    for it in items:
        if isinstance(it, dict):
            raw = it.get("value", it.get("values", it.get("range", it.get("v"))))
            if isinstance(raw, list) and raw and not isinstance(raw[0], (int, float)):
                texts.append({"label": it.get("label") or it.get("name") or "",
                              "text": " ".join(str(x) for x in raw)})
                continue
            rec = _ans_num_parse(raw)
            if rec.get("text") and rec.get("v") is None:
                texts.append({"label": it.get("label") or it.get("name") or "", "text": rec["text"]})
                continue
            rec["label"] = str(it.get("label") or it.get("name") or "")
            rec["unit"] = it.get("unit") or rec.get("unit") or unit0
            rec["note"] = it.get("note")
            rec["tone"] = it.get("tone") or it.get("variant") or ""
            rec["obj"] = _ans_num_parse(it.get("obj", it.get("goal", it.get("target")))).get("v")
            rec["thr"] = _ans_num_parse(it.get("thr", it.get("threshold", it.get("limit")))).get("v")
        else:
            rec = _ans_num_parse(it)
            rec["label"] = ""
            rec["unit"] = unit0
            if rec.get("v") is None:
                texts.append({"label": "", "text": rec.get("text") or str(it)})
                continue
        recs.append(rec)

    if not recs:
        return _ans_blk_list(texts, spec, env)

    vals = [r["v"] for r in recs if r.get("v") is not None]
    marks = [x for x in [r.get("obj") for r in recs] + [r.get("thr") for r in recs]
             if isinstance(x, (int, float))]
    vmax = max(vals) if vals else 1.0
    if marks:
        vmax = max(vmax, max(marks))
    log = bool(d.get("log"))
    if log:
        # 对数轴：取严格包住所有数值与标记的 10 的幂，条长才不会被压扁或溢出
        live = [v for v in (vals + marks) if v and v > 0]
        vmin = 10.0 ** math.floor(math.log10(max(min(live), 1e-12))) if live else 1.0
        top = 10.0 ** math.ceil(math.log10(max(live) if live else 10.0))
        if not (vmin < (max(live) if live else 1.0)):
            top = vmin * 10.0
    else:
        vmin = float(d.get("min") or 0.0)
        top = _ans_nice_ceil(abs(vmax) * 1.04)
    ticks = _ans_nice_ticks(vmin, top, 5, log=log)

    rows = []
    for i, r in enumerate(recs, 1):
        v, tone = r.get("v"), (r.get("tone") or "")
        vhi = r.get("hi")
        # 单值条从轴起点画到该值；以前 span 取 [v, v]，宽度恒 0（README 截图里四条全是空轨）。
        # 区间值仍然从下界画到上界；负值线性轴从 v 画到 0。
        if vhi is not None and v is not None and vhi > v * 1.02:
            span_lo, span_hi = v, vhi
            wide = True
        elif v is None:
            span_lo = span_hi = None
            wide = False
        elif log:
            span_lo, span_hi = vmin, v
            wide = False
        elif v < 0:
            span_lo, span_hi = v, 0.0
            wide = False
        else:
            span_lo, span_hi = 0.0, v
            wide = False
        p_lo = _ans_scale_pct(span_lo, vmin, top, log) or 0.0
        p_hi = _ans_scale_pct(span_hi, vmin, top, log)
        p_hi = p_lo if p_hi is None else p_hi
        p_lo = max(0.0, min(p_lo, 1.0))
        p_hi = max(p_lo, min(p_hi, 1.0))
        bar = ('<span class="vbar%s%s" style="left:%.4f%%;width:%.4f%%">'
               % (" hi" if tone == "hi" else (" warn" if tone == "warn" else
                  (" bad" if tone == "bad" else (" mut" if tone == "mut" else ""))),
                  " thin" if wide else "", p_lo * 100.0, max(p_hi - p_lo, 0.0) * 100.0))
        if wide:
            bar += '<span class="vl">%s</span>' % _ans_esc(
                "%s–%s" % (_ans_fmt_num(v), _ans_fmt_num(vhi)))
        bar += "</span>"
        marks_h = ""
        for key, cls in (("obj", "vobj"), ("thr", "vthr")):
            mv = r.get(key)
            if isinstance(mv, (int, float)):
                mp = _ans_scale_pct(mv, vmin, top, log)
                if mp is not None and 0.0 <= mp <= 1.0:
                    marks_h += '<span class="%s" style="left:%.4f%%"></span>' % (cls, mp * 100.0)
        num = _ans_fmt_with_unit(r, unit0)
        u = r.get("unit") or unit0
        numh = (_ans_esc(num) + ('<span class="unit">%s</span>' % _ans_esc(u)) if u else _ans_esc(num))
        if d.get("fold") and i <= len(recs) - 1 and v and recs[0].get("v"):
            topv = recs[0]["v"]
            if isinstance(topv, (int, float)) and topv:
                numh += '<span class="fold">%s×</span>' % _ans_fmt_num(round(topv / v, 1))
        lbl = _ans__md_inline(r.get("label") or "")
        if r.get("note"):
            lbl += '<span class="sub">%s</span>' % _ans__md_inline(r["note"])
        rows.append('<div class="vrow"><div class="vlbl">%s</div>'
                    '<div class="vnum">%s</div>'
                    '<div class="vtrack">%s%s</div></div>'
                    % (lbl, numh, marks_h, bar))

    for t in texts:
        rows.append('<div class="vrow"><div class="vlbl">%s</div>'
                    '<div class="vnum" style="grid-column:2/-1;text-align:left;font-weight:400">%s</div>'
                    '<div></div></div>'
                    % (_ans__md_inline(t.get("label") or ""), _ans__md_inline(t.get("text") or "")))

    axis = ""
    if ticks and d.get("axis", True):
        axis = '<div class="vaxis">%s</div>' % "".join(
            '<span class="tk%s" style="left:%.4f%%">%s</span>'
            % (" first" if abs((_ans_scale_pct(t, vmin, top, log) or 0.0)) < 0.02 else "",
               max(0.0, min(1.0, _ans_scale_pct(t, vmin, top, log) or 0.0)) * 100.0,
               _ans_esc(_ans_fmt_num(t)))
            for t in ticks)
    head = ('<div class="vrow head"><div class="vlbl">%s</div><div class="vnum">%s</div>'
            '<div class="vaxhead">%s</div></div>'
            % (_ans_esc(d.get("label") or ""), _ans_esc(d.get("value_label") or ""),
               _ans_esc(d.get("axis_label") or (d.get("unit_label") or ""))))

    stats = ""
    if d.get("stats") is not False and len(vals) >= 2:
        bits = ["n=<b>%d</b>" % len(vals)]
        top_i = max(range(len(recs)), key=lambda k: recs[k].get("v") or -1e18)
        bits.append("最高 <b>%s</b>%s" % (_ans_esc(_ans_fmt_with_unit(recs[top_i], unit0)),
                                         ("（%s）" % _ans_esc(recs[top_i].get("label"))) if recs[top_i].get("label") else ""))
        if all((r.get("v") or 0) > 0 for r in recs):
            bits.append("最大/最小 <b>%s×</b>" % _ans_fmt_num(round(max(vals) / min(vals), 1)))
        elif any((r.get("v") or 0) < 0 for r in recs) and any((r.get("v") or 0) > 0 for r in recs):
            bits.append("含负值")
        stats = '<div class="vstats">%s</div>' % " · ".join("<span>%s</span>" % b for b in bits)

    legend = ""
    if any(isinstance(r.get("obj"), (int, float)) for r in recs) or \
       any(isinstance(r.get("thr"), (int, float)) for r in recs):
        li = []
        if any(isinstance(r.get("obj"), (int, float)) for r in recs):
            li.append('<span><i></i>目标</span>')
        if any(isinstance(r.get("thr"), (int, float)) for r in recs):
            li.append('<span><i class="bad"></i>阈值</span>')
        legend = '<div class="vlegend">%s</div>' % "".join(li)

    note = '<p class="tnote">%s</p>' % _ans__md_inline(d["note"]) if d.get("note") else ""
    return ('<div class="vbars">%s%s%s%s%s%s</div>'
            % (stats, head, "".join(rows), axis, legend, note))


def _ans_chart_fallback(d):
    """画不出来时**把 agent 给的原值显示出来**，而不是一句"无法绘制"。

    渲染层是最后一跳：静默吞掉内容等于用户以为"这题本来就没数据"。把原值摊开，
    用户能立刻看出是 agent 填错了形状，也能读到本该看到的内容。
    """
    bits = []
    for key in ("series", "values", "points", "data", "lines"):
        v = (d or {}).get(key)
        if v is None:
            continue
        if isinstance(v, list):
            for item in v[:4]:
                if isinstance(item, dict):
                    bits.append((str(item.get("name") or item.get("label") or "") + " " +
                                 " ".join(str(x) for x in
                                          (item.get("values") or item.get("points") or [])[:8])).strip())
                elif isinstance(item, (list, tuple)):
                    bits.append(" ".join(str(x) for x in item[:8]))
                else:
                    bits.append(str(item))
        elif isinstance(v, dict):
            bits.append(" ".join(str(x) for x in (v.get("values") or [])[:8]) or str(v)[:80])
        else:
            bits.append(str(v)[:80])
    for key in ("x", "xs", "labels", "caption", "note"):
        v = (d or {}).get(key)
        if isinstance(v, list):
            bits.append("、".join(str(x) for x in v[:8]))
        elif v:
            bits.append(str(v)[:120])
    body = " ".join(b for b in bits if b.strip())
    return ('<div class="vchart-txt">%s</div>'
            % _ans__md_inline("（本块没被识别成可绘制的序列，原样列出）" + (body or "（空）")))


def _ans_chart_series(series):
    """把折线序列写成 SVG：网格 + 轴 + 折线 + 端点标注（全部由引擎算）。"""
    d = series if isinstance(series, dict) else {"values": series}
    xs = d.get("x") or d.get("xs") or d.get("labels")
    vals = d.get("values", d.get("points", d.get("data")))
    if isinstance(vals, list) and vals and isinstance(vals[0], dict):
        pts = [(p.get("x"), p.get("y")) for p in vals]
        xs = xs or [p[0] for p in pts]
        vals = [p[1] for p in pts]
    nums = []
    for raw in (vals or []):
        rec = _ans_num_parse(raw)
        nums.append(rec.get("v"))
    keep = [i for i, n in enumerate(nums) if isinstance(n, (int, float))]
    if len(keep) < 2:
        return None
    ys = [nums[i] for i in keep]
    if xs and len(xs) >= len(nums):
        labels = [str(xs[i]) for i in keep]
    else:
        labels = [str(i + 1) for i in keep]
    log = bool(d.get("log"))
    lo, hi = min(ys), max(ys)
    if log:
        # 对数轴：两端都取**严格包住数据**的 10 的幂，再把刻度裁剪到轴内。
        # 不这么做的话 ceil 出来的上界会高于数据最大值，而刻度循环仍以数据最大值
        # 为界 —— 于是一个超出轴范围的刻度（实测 "100"）会印在轴外面。
        lo = 10.0 ** math.floor(math.log10(max(lo, 1e-12)))
        hi = 10.0 ** math.ceil(math.log10(max(hi, 1e-12)))
    else:
        if lo > 0:
            lo = 0.0 if d.get("zero", True) else _ans_nice_ceil(lo) - _ans_nice_ceil(hi - lo)
            lo = max(0.0, lo)
        else:
            pad = (hi - lo) * 0.08 or 1.0
            lo, hi = lo - pad, hi + pad
        hi = _ans_nice_ceil(max(hi, lo + 1e-9) * 1.02)
    ticks = _ans_nice_ticks(lo, hi, 5, log=log)
    if ticks:
        lo, hi = min(lo, ticks[0]), max(hi, ticks[-1])

    # 绘图区留白：顶部 24 给最高那条刻度文字（15 时会被框线压住，实测"100"贴边），
    # 底部 52 给 x 标签（40 时标签贴到画布下沿）。
    W, H, pl, pr, pt, pb = 720.0, 294.0, 54.0, 15.0, 24.0, 52.0
    iw, ih = W - pl - pr, H - pt - pb
    n = len(keep)
    X = [pl + (iw * (j / float(n - 1)) if n > 1 else iw / 2.0) for j in range(n)]

    def Y(v):
        if log:
            a, b = math.log10(max(lo, 1e-12)), math.log10(max(hi, 1e-12))
            f = (math.log10(max(v, 1e-12)) - a) / (b - a) if b > a else 0.0
        else:
            f = (v - lo) / (hi - lo) if hi > lo else 0.0
        return pt + ih - f * ih

    out = ["<svg viewBox=\"0 0 %d %d\" role=\"img\" xmlns=\"http://www.w3.org/2000/svg\">"
           % (int(W), int(H))]
    out.append('<title>%s</title>' % _ans_esc(d.get("label") or d.get("title") or "数据趋势图"))
    for t in ticks:
        y = Y(t)
        # 轴外的刻度直接不出（几何检查会另外断言所有刻度都落在轴内）
        if not (pt - 0.5 <= y <= pt + ih + 0.5):
            continue
        out.append('<line x1="%.2f" y1="%.2f" x2="%.2f" y2="%.2f" stroke="#e2e6ec" stroke-width="1"/>'
                   % (pl, y, pl + iw, y))
        out.append('<text x="%.2f" y="%.2f" text-anchor="end" font-size="11.5" fill="#5e6774" '
                   'font-family="ui-monospace,Menlo,monospace">%s</text>'
                   % (pl - 9, y + 4.2, _ans_esc(_ans_fmt_num(t, as_power=log))))
    out.append('<line x1="%.2f" y1="%.2f" x2="%.2f" y2="%.2f" stroke="#c6cdd9" stroke-width="1.2"/>'
               % (pl, pt + ih, pl + iw, pt + ih))
    out.append('<line x1="%.2f" y1="%.2f" x2="%.2f" y2="%.2f" stroke="#c6cdd9" stroke-width="1.2"/>'
               % (pl, pt, pl, pt + ih))
    step = 1 if n <= 9 else (2 if n <= 16 else max(1, int(round(n / 8.0))))
    for j in range(n):
        if j % step and j != n - 1:
            continue
        out.append('<text x="%.2f" y="%.2f" text-anchor="middle" font-size="11" fill="#5e6774">%s</text>'
                   % (X[j], pt + ih + 17, _ans_esc(labels[j][:14])))
    for raw, tone in ((d.get("thr", d.get("threshold", d.get("limit"))), "#b03a3a"),
                      (d.get("obj", d.get("goal", d.get("target"))), "#0d6b52")):
        rec = _ans_num_parse(raw) if raw is not None else {}
        if rec.get("v") is None:
            continue
        y = Y(rec["v"])
        if pt - 1 <= y <= pt + ih + 1:
            out.append('<line x1="%.2f" y1="%.2f" x2="%.2f" y2="%.2f" stroke="%s" stroke-width="2" '
                       'stroke-dasharray="6 4"/>' % (pl, y, pl + iw, y, tone))
            out.append('<text x="%.2f" y="%.2f" font-size="11" fill="%s">%s</text>'
                       % (pl + iw - 4, y - 5, tone,
                          _ans_esc(d.get("thr_label") if tone == "#b03a3a" else d.get("obj_label")
                                   or ("阈值" if tone == "#b03a3a" else "目标"))))
    dsh = {"dashed": "5 4", "dash": "5 4", "dotted": "2 3"}.get(str(d.get("style") or "").lower(), "")
    out.append('<polyline fill="none" stroke="#1f5fa8" stroke-width="2.4" stroke-linejoin="round" '
               'stroke-linecap="round"%s points="%s"/>'
               % ((' stroke-dasharray="%s"' % dsh) if dsh else "",
                  " ".join("%.2f,%.2f" % (X[j], Y(ys[j])) for j in range(n))))
    for j in range(n):
        if n > 26 and j not in (0, n - 1):
            continue
        _tip = "%s：%s" % (labels[j], _ans_fmt_num(ys[j]))
        out.append('<circle cx="%.2f" cy="%.2f" r="%s" fill="#fff" stroke="#1f5fa8" stroke-width="2" '
                   'data-tip="%s"><title>%s</title></circle>'
                   % (X[j], Y(ys[j]), "3.2" if n <= 14 else "2.4",
                      _ans_esc(_tip), _ans_esc(_tip)))
    for j in ({0, n - 1} if n > 2 else set(range(n))):
        dy = -9.0 if j == n - 1 else 16.0
        anch = "end" if j == n - 1 else ("start" if j == 0 else "middle")
        out.append('<text x="%.2f" y="%.2f" text-anchor="%s" font-size="11.6" font-weight="700" '
                   'fill="#171a1f">%s</text>'
                   % (X[j], Y(ys[j]) + dy, anch, _ans_esc(_ans_fmt_num(ys[j]))))
    if d.get("y_label"):
        out.append('<text x="%.2f" y="%.2f" font-size="11" fill="#5e6774" '
                   'transform="rotate(-90 %.2f %.2f)">%s</text>'
                   % (13.0, pt + 8, 13.0, pt + 8, _ans_esc(str(d["y_label"])[:40])))
    out.append("</svg>")
    return "".join(out)


def _ans_blk_chart(v, spec, env):
    """折线/趋势图：几组数对进去，坐标轴、刻度、端点标注全由引擎算。

    最省事：{"x":["25 µm","50 µm"],"series":[{"name":"液相分压","values":[4.5,8.7]}]}
    也接受 series 直接是数字数组，或 points:[{x,y}]，或 values:[..]（单序列）。
    """
    d = v if isinstance(v, dict) else {"series": v}
    raw = d.get("series") or d.get("lines") or d.get("values") or d.get("points")
    if isinstance(raw, dict):
        raw = [raw]
    if isinstance(raw, list) and raw and not isinstance(raw[0], (dict, list, int, float)):
        # 序列里是文字：agent 给错了形状 —— 降级成文字，不画一张假图
        return _ans_chart_fallback(d)
    if not raw:
        if d.get("note"):
            return '<div class="vchart-txt">%s</div>' % _ans__md_inline(d["note"])
        return ""
    series = raw if isinstance(raw, list) else [raw]
    svgs, legend = [], []
    for i, s in enumerate(series[:3]):
        if isinstance(s, dict) and d.get("x") and not s.get("x") and not s.get("xs"):
            s = dict(s, x=d["x"])
        if isinstance(s, dict) and d.get("x_label") and not s.get("x_label"):
            s = dict(s, x_label=d["x_label"])
        if isinstance(s, dict) and d.get("y_label") and not s.get("y_label"):
            s = dict(s, y_label=d["y_label"])
        if isinstance(s, dict) and d.get("thr") is not None and "thr" not in s:
            s = dict(s, thr=d["thr"], thr_label=d.get("thr_label"))
        if isinstance(s, dict) and d.get("obj") is not None and "obj" not in s:
            s = dict(s, obj=d["obj"], obj_label=d.get("obj_label"))
        svg = _ans_chart_series(s)
        if not svg:
            continue
        if i:
            svg = svg.replace('stroke="#1f5fa8"', 'stroke="#0d6b52"')
        width = "100%" if len(series) == 1 else "calc(50%% - 6px)"
        wrap = ('<div style="flex:1 1 320px;min-width:280px">%s%s</div>'
                % (svg, ('<p class="tnote"><b>%s</b></p>'
                         % _ans__md_inline(s.get("name") or s.get("label") or ""))
                   if (s.get("name") or s.get("label")) and len(series) > 1 else ""))
        svgs.append(wrap)
        if len(series) > 1 and (s.get("name") or s.get("label")):
            legend.append('<span><i class="sw"%s></i>%s</span>'
                          % (' style="background:#0d6b52"' if i else "",
                             _ans_esc(s.get("name") or s.get("label") or "")))
    if not svgs:
        return _ans_chart_fallback(d)
    fig = ('<figure class="vchart"><div style="display:flex;gap:12px;flex-wrap:wrap">%s</div>%s%s</figure>'
           % ("".join(svgs),
              ('<div class="vlegend">%s</div>' % "".join(legend)) if legend else "",
              ('<p class="vcap">%s</p>' % _ans__md_inline(d["caption"])) if d.get("caption") else
              ('<p class="tnote">%s</p>' % _ans__md_inline(d["note"])) if d.get("note") else ""))
    return fig


def _ans_blk_kpi(v, spec, env):
    """关键数字卡：读者只想知道"所以呢"时看这一排。

    每项 {value, unit, label, range|sub, thr|threshold, dir|trend, tone, refs}。
    """
    d = v if isinstance(v, dict) else {"items": v}
    items = d.get("items") or d.get("cards") or d.get("kpis") or []
    if not isinstance(items, list):
        return _ans_blk_text(str(v), spec, env)
    cards = []
    for it in items:
        if not isinstance(it, dict):
            it = {"value": it}
        # display/text：原样显示。区间值（如 90–97）不能交给数值解析 —— 那会把区间折成
        # 中点 93.5，读者会以为这是一个实测值。数值仍走原来的解析/格式化。
        disp = it.get("display") if it.get("display") is not None else it.get("text")
        if disp is not None:
            rec = {}
            num = str(disp)
        else:
            rec = _ans_num_parse(it.get("value", it.get("v")))
            num = _ans_fmt_with_unit(rec, it.get("unit") or "")
        pre_s = ""
        tone = str(it.get("tone") or it.get("variant") or "")
        thr = _ans_num_parse(it.get("thr", it.get("threshold"))).get("v")
        obj = _ans_num_parse(it.get("obj", it.get("goal", it.get("target")))).get("v")
        thr_h = ""
        ref = thr if isinstance(thr, (int, float)) else obj
        if isinstance(ref, (int, float)) and isinstance(rec.get("v"), (int, float)) and ref > 0:
            pos = max(0.03, min(0.97, float(rec["v"]) / max(ref * 1.25, abs(float(rec["v"])), 1e-9)))
            cls = "mk" if isinstance(thr, (int, float)) else "mk2"
            lab = it.get("thr_label") or ("阈值 %s" % _ans_fmt_num(ref))
            right = pos > 0.62
            thr_h = ('<div class="kvthr"><span class="rail"></span>'
                     '<span class="%s" style="left:%.3f%%"></span>'
                     '<span class="lbl" style="%s:%.3f%%;transform:none">%s</span></div>'
                     % (cls, pos * 100.0, "right" if right else "left", pos * 100.0, _ans_esc(lab)))
        rng = it.get("range") or it.get("sub")
        slope = it.get("dir") or it.get("trend")
        slope_h = ""
        if slope:
            up = str(slope).lower() in ("up", "rise", "rising", "↑", "increase", "higher")
            down = str(slope).lower() in ("down", "fall", "falling", "↓", "decrease", "lower")
            slope_h = ('<div class="kvslope">%s</div>'
                       % _ans_esc(("↑ " if up else "↓ " if down else "→ ") + str(it.get("dir_note") or "")))
        refs = _ans_sup(it.get("refs"))
        cards.append('<div class="kpi %s"><div class="klbl">%s</div>'
                     '<div class="kval">%s%s%s</div>%s%s%s%s</div>'
                     % (_ans_esc(tone), _ans__md_inline(it.get("label") or ""),
                        ('<span class="pre">%s</span>' % _ans_esc(pre_s)) if pre_s else "",
                        _ans_esc(num),
                        ('<span class="unit">%s</span>' % _ans_esc(it.get("unit") or rec.get("unit") or ""))
                        if (it.get("unit") or rec.get("unit")) else "",
                        ('<div class="kvrange">%s</div>' % _ans__md_inline(rng)) if rng else "",
                        slope_h, thr_h,
                        ('<span class="ksrc">%s</span>' % refs) if refs else ""))
    if not cards:
        return ""
    return ('<div class="kpis%s">%s</div>' % (" one" if len(cards) == 1 else "", "".join(cards)))


def _ans_blk_flow(v, spec, env):
    """流程链：横向节点 + 箭头；失败/回流用虚线橙框写在下方。

    两种给法都对：
      {"nodes":["表面清洗","等离子活化","CVD 成核","单晶生长"],
       "edges":[{"from":2,"to":1,"kind":"fail","label":"成核密度不足 → 退回活化"}]}
      {"steps":[{"label":"表面清洗"},{"label":"等离子活化","state":"ok"}, ...]}
    换行时不做 SVG 连线（百分比连线一换行就错位），改用"节点 + 箭头 + 回流框"。
    """
    d = v if isinstance(v, dict) else {"nodes": v}
    nodes = d.get("nodes") or d.get("steps") or []
    if isinstance(nodes, dict):
        nodes = [nodes]
    if not isinstance(nodes, list) or not nodes:
        return _ans_blk_text(str(d.get("note") or ""), spec, env)
    edges = d.get("edges") or d.get("links") or []
    norm = []
    for it in nodes:
        if isinstance(it, dict):
            norm.append({"label": str(it.get("label") or it.get("name") or it.get("text") or ""),
                         "state": it.get("state") or it.get("tone") or "",
                         "note": it.get("note")})
        else:
            norm.append({"label": str(it), "state": "", "note": None})
    out = ['<div class="fchain">']
    for i, nd in enumerate(norm):
        if i:
            out.append('<span class="farrow" aria-hidden="true">→</span>')
        cls = "fnode"
        if nd["state"] in ("entry", "start", "first") or (i == 0 and str(d.get("mark_entry", "auto")) == "auto"):
            cls += " entry"
        elif nd["state"] in ("exit", "end", "goal", "done") or (i == len(norm) - 1 and str(d.get("mark_exit", "auto")) == "auto"):
            cls += " exit"
        inner = '<span class="n">%d</span><span class="t">%s</span>' % (i + 1, _ans__md_inline(nd["label"]))
        if nd["state"] and nd["state"] not in ("entry", "exit", "start", "end", "goal", "done", "first", "last"):
            inner += '<span class="st %s">%s</span>' % (_ans_esc(nd["state"]), _ans_esc(nd["state"]))
        if nd.get("note"):
            inner += '<span class="sub">%s</span>' % _ans__md_inline(nd["note"])
        out.append('<span class="%s">%s</span>' % (cls, inner))
    out.append("</div>")
    for e in (edges if isinstance(edges, list) else []):
        if not isinstance(e, dict):
            continue
        kind = str(e.get("kind") or e.get("type") or "fail").lower()
        a, b = e.get("from"), e.get("to")
        la = norm[a - 1]["label"] if isinstance(a, int) and 0 < a <= len(norm) else str(a or "")
        lb = norm[b - 1]["label"] if isinstance(b, int) and 0 < b <= len(norm) else str(b or "")
        txt = e.get("label") or e.get("text") or ""
        chain = ("%s ⇠ %s" % (lb, la)) if (la and lb) else ""
        out.append('<div class="fback%s"><span class="ic">%s</span><span>%s%s</span></div>'
                   % ("" if kind == "fail" else " warn2",
                      "↺" if kind == "fail" else "⇢",
                      ("<b>%s</b> · " % _ans_esc(chain)) if chain else "",
                      _ans__md_inline(txt)))
    if d.get("note"):
        out.append('<p class="fnote">%s</p>' % _ans__md_inline(d["note"]))
    return '<div class="flow">%s</div>' % "".join(out)

_ans_BLOCKS = {"note": _ans_blk_note, "text": _ans_blk_text, "list": _ans_blk_list, "steps": _ans_blk_steps,
          "table": _ans_blk_table, "cards": _ans_blk_cards, "timeline": _ans_blk_timeline,
          "dispute": _ans_blk_dispute, "math": _ans_blk_math, "figure": _ans_blk_figure,
          "code": _ans_blk_code, "key": _ans_blk_key, "qa": _ans_blk_qa, "deriv": _ans_blk_deriv,
          # #8 可视块：agent 只填数据；轴界、刻度、条长、排序、热度一律引擎侧算。
          # 别名是故意的 —— agent 记哪个名字都能用（bars/rank 同一个渲染器）。
          "bars": _ans_blk_bars, "rank": _ans_blk_bars, "chart": _ans_blk_chart,
          "line": _ans_blk_chart, "kpi": _ans_blk_kpi, "scalars": _ans_blk_kpi,
          "flow": _ans_blk_flow}


def _ans_evidence_html(evs) -> str:
    items = []
    for e in evs or []:
        title = _ans_esc(e.get("title", ""))
        if e.get("doi"):
            title = '<a href="https://doi.org/%s">%s</a>' % (_ans_esc(e["doi"]), title)
        meta = " · ".join(str(x) for x in (e.get("authors"), e.get("year"), e.get("journal")) if x)
        loc = ""
        if e.get("section") or e.get("page"):
            parts = []
            if e.get("section"):
                parts.append("§" + _ans_esc(e["section"]))
            if e.get("page"):
                parts.append("p." + _ans_esc(e["page"]))
            loc = " · " + " · ".join(parts)
        quote = '<div class="ev-q">%s</div>' % _ans_esc(e["snippet"]) if e.get("snippet") else ""
        foot = []
        if e.get("doi"):
            foot.append("<span>DOI %s</span>" % _ans_esc(e["doi"]))
        if e.get("score") is not None:
            foot.append("<span>score %s</span>" % _ans_esc(e["score"]))
        if e.get("zotero_key"):
            foot.append('<span><a href="zotero://open-pdf/library/items/%s">Zotero 打开</a></span>'
                        % _ans_esc(e["zotero_key"]))
        footh = '<div class="ev-f">%s</div>' % " · ".join(foot) if foot else ""
        cites = ""
        cs = e.get("citations") or []
        if cs:
            lis = []
            for c in cs:
                tx = _ans_esc(c.get("text", ""))
                if c.get("lib"):
                    tx = '<span class="inlib">[库内]</span> %s' % tx
                lis.append("<li>[Ref %s] %s</li>" % (_ans_esc(c.get("n")), tx))
            cites = ('<details class="cite"><summary>↳ 引文补充（本证据的参考文献，%d 条）</summary>'
                     "<ul>%s</ul></details>" % (len(cs), "".join(lis)))
        items.append('<li><div class="ev-t">%s</div><div class="ev-m">%s%s</div>%s%s%s</li>'
                     % (title, _ans_esc(meta), loc, quote, footh, cites))
    return '<ol class="ev">%s</ol>' % "".join(items)


def _ans_suggest_html(sug) -> str:
    cols = []
    for key, title in _ans_SUGGEST_COLS:
        items = (sug or {}).get(key) or []
        if not items:
            continue
        lis = []
        for it in items:
            t = _ans_esc(it.get("title", ""))
            if it.get("doi"):
                t = '<a href="https://doi.org/%s">%s</a>' % (_ans_esc(it["doi"]), t)
            tag = '<span class="tag">库内</span>' if it.get("in_lib") else ""
            why = '<span class="why">%s</span>' % _ans_esc(it["why"]) if it.get("why") else ""
            lis.append("<li>%s%s%s%s</li>" % (tag, t, _ans_sup(it.get("refs")), why))
        cols.append('<div class="col"><h3>%s <span class="n">(%d)</span></h3><ul>%s</ul></div>'
                    % (_ans_esc(title), len(items), "".join(lis)))
    if not cols:
        return ""
    return '<section id="sug"><h2><span class="num">S</span>补充建议</h2><div class="cols">%s</div></section>' % "".join(cols)


# ------------------------------------------------------------------ 主渲染

def _ans__lead_class(verdict) -> str:
    """结论块的底色按判定走 —— 库里没有资料却用"成功绿"，等于把坏消息画成好消息。"""
    return {"hit": "bluf", "weak": "bluf warn", "doubt": "bluf warn",
            "no_hit": "bluf mut"}.get(str(verdict or "hit"), "bluf")


def _ans__instant_sections(env, tpl, fields) -> list:
    """第一跳（instant）的紧凑排版：**答案就是页面主体**，其余格子退成小字行。

    为什么不用通用排版：instant 只有三个锁死的字段、预算 300 字符，套上"结论摘要 +
    每格一张卡"之后，用户在首屏看到的是一张几乎空的目录卡，答案被挤到第二屏 ——
    与"5 秒内先给可用信息"正好相反。这里让答案占据首屏，限制/追问做成两行说明。
    """
    out = []
    first = True
    for spec in tpl.get("sections", []):
        v = fields.get(spec.get("id"))
        if v in (None, "", []):
            continue
        text = v.get("text", "") if isinstance(v, dict) else str(v)
        refs = v.get("refs") if isinstance(v, dict) else None
        if first:
            out.append('<section id="s1" class="instant-lead">'
                       '<h2 class="sr-only">%s</h2>'
                       '<div class="%s"><p>%s%s</p></div></section>'
                       % (_ans_esc(spec.get("label") or ""), _ans__lead_class(env.get("verdict")),
                          _ans__md_inline(text), _ans_sup(refs)))
            first = False
        else:
            out.append('<section class="instant-row"><span class="lbl">%s</span>'
                       '<span class="val">%s%s</span></section>'
                       % (_ans_esc(spec.get("label") or ""), _ans__md_inline(text), _ans_sup(refs)))
    return out


# ── 动效层（1.6.8 / B30）──────────────────────────────────────────────────────
# 移植自 answer-style-demo.html 的三项：入场 reveal、数值条生长、目录滚动高亮。
# 约束：① 全部内联，单文件仍可断网打开；② prefers-reduced-motion 与打印时全部关闭
#      （否则打印会抓到 opacity:0 的起始帧，整页空白）；③ 不改 HTML 里的 inline 宽度，
#      verify_render 的几何断言只看静态 DOM，动效是浏览器侧的事。
_ans_CSS_MOTION = r"""
/* ---- 动效层：与 answer-style-demo.html 对齐 ---- */
/* demo 的做法：html.js 存在时才隐藏 section（无 JS 也能读），进入视口加 .in 再过渡。
   参数照抄 demo：位移 10px、.55s、cubic-bezier(.2,.8,.2,1)；hero 用 rise .7s 错峰。 */
html.kb-js main section { opacity: 0; transform: translateY(10px); }
html.kb-js main section.kb-in {
  opacity: 1; transform: none;
  transition: opacity .55s ease, transform .55s cubic-bezier(.2,.8,.2,1);
}
html.kb-js .vbars .vbar { transition: width .55s cubic-bezier(.2,.8,.2,1); }

@keyframes kbrise { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: none; } }
html.kb-js header.doc > * { animation: kbrise .7s cubic-bezier(.2,.8,.2,1) both; }
html.kb-js header.doc > *:nth-child(2) { animation-delay: .05s; }
html.kb-js header.doc > *:nth-child(3) { animation-delay: .1s; }
html.kb-js header.doc > *:nth-child(4) { animation-delay: .15s; }
html.kb-js header.doc > *:nth-child(5) { animation-delay: .2s; }

.toc a.on { color: var(--accent, #247bbf); font-weight: 600; }

/* 系统关了动画：不隐藏、不位移、不过渡（仍然完整可读） */
@media (prefers-reduced-motion: reduce) {
  html.kb-js main section { opacity: 1 !important; transform: none !important; transition: none !important; }
  html.kb-js header.doc > * { animation: none !important; }
  html.kb-js .vbars .vbar { transition: none !important; }
}
@media print {
  html.kb-js main section { opacity: 1 !important; transform: none !important; transition: none !important; }
  html.kb-js header.doc > * { animation: none !important; }
  .vbars .vbar { transition: none !important; }
}
"""

_ans_MOTION_JS = r"""
(function () {
  var mq = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
  if (mq && mq.matches) return;            // 系统关了动画：连 kb-js 都不加，页面保持静态

  var root = document.documentElement;
  root.classList.add('kb-js');             // 等价于 demo 的 html.js：有 JS 才启用隐藏态

  var sections = Array.prototype.slice.call(document.querySelectorAll('main section'));
  var grown = new WeakSet();

  function growBars(scope) {
    if (grown.has(scope)) return;
    grown.add(scope);
    Array.prototype.slice.call(scope.querySelectorAll('.vbars .vbar')).forEach(function (b) {
      var target = b.style.width || '0%';
      b.setAttribute('data-w', target);
      b.style.width = '0%';
      requestAnimationFrame(function () {
        requestAnimationFrame(function () { b.style.width = target; });
      });
    });
  }

  if ('IntersectionObserver' in window) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (!e.isIntersecting) return;
        e.target.classList.add('kb-in');
        growBars(e.target);
        io.unobserve(e.target);
      });
    }, { threshold: 0.08 });
    sections.forEach(function (s) { io.observe(s); });
  } else {
    root.classList.remove('kb-js');
  }

  // 打印前恢复最终态：动画中途打印会得到半透明块和空条
  window.addEventListener('beforeprint', function () {
    sections.forEach(function (s) { s.classList.add('kb-in'); });
    Array.prototype.slice.call(document.querySelectorAll('.vbars .vbar')).forEach(function (b) {
      var w = b.getAttribute('data-w');
      if (w !== null) b.style.width = w;
    });
  });

  // 目录滚动高亮：rootMargin 与 demo 一致
  var links = Array.prototype.slice.call(document.querySelectorAll('.toc a'));
  if (links.length && 'IntersectionObserver' in window) {
    var byId = {};
    links.forEach(function (a) {
      var href = a.getAttribute('href') || '';
      if (href.charAt(0) === '#') byId[href.slice(1)] = a;
    });
    var spy = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (!e.isIntersecting) return;
        links.forEach(function (a) { a.classList.remove('on'); });
        var a = byId[e.target.id];
        if (a) a.classList.add('on');
      });
    }, { rootMargin: '-18% 0px -74% 0px' });
    sections.forEach(function (s) { if (s.id) spy.observe(s); });
  }
})();
"""


_ans_CSS_INTERACT = r"""
/* ==== 通用交互层（B30）：hover 抬升 / 数据浮层 / 点击高亮 ==== */
.kb-tip{position:fixed;z-index:9999;max-width:min(320px,80vw);padding:7px 10px;border-radius:8px;
  background:rgba(23,26,31,.95);color:#fff;font-size:12.5px;line-height:1.45;pointer-events:none;
  opacity:0;transform:translateY(4px);transition:opacity .14s ease,transform .14s ease;
  box-shadow:0 8px 24px rgba(16,24,40,.22);white-space:normal}
.kb-tip.on{opacity:1;transform:none}
[data-tip]{cursor:help}
.kb-active{outline:2px solid var(--accent);outline-offset:2px;border-radius:6px}
@media (hover:hover) and (prefers-reduced-motion:no-preference){
  .kb-motion .kpi,.kb-motion .card,.kb-motion .qa,.kb-motion .fnode,
  .kb-motion .vrow,.kb-motion .ev>li,.kb-motion .act,.kb-motion .badge{
    transition:transform .18s ease,box-shadow .18s ease,border-color .18s ease,background-color .18s ease,filter .18s ease;
  }
  .kb-motion .kpi:hover,.kb-motion .card:hover,.kb-motion .qa:hover{
    transform:translateY(-2px);box-shadow:0 10px 24px rgba(16,24,40,.10);border-color:var(--line-strong);
  }
  .kb-motion .vrow:hover{background:var(--accent-soft);border-radius:6px}
  .kb-motion .vrow:hover .vbar{filter:saturate(1.25) brightness(1.04)}
  .kb-motion .fnode:hover{transform:translateY(-1px) scale(1.03);border-color:var(--accent);
    box-shadow:0 6px 16px rgba(16,24,40,.12)}
  .kb-motion .ev>li:hover{transform:translateX(2px);border-color:var(--line-strong)}
  .kb-motion table tbody tr:hover{background:var(--accent-soft)}
  .kb-motion .act:hover{transform:translateY(-1px);box-shadow:0 4px 12px rgba(16,24,40,.12)}
  .kb-motion .badge:hover{transform:translateY(-1px);border-color:var(--accent)}
}
@media print{.kb-tip{display:none!important}.kb-active{outline:none!important}
  .kpi,.card,.qa,.fnode,.vrow,.ev>li,.act,.badge{transform:none!important;box-shadow:none!important}}
"""

_ans_JS_INTERACT = r"""
(function () {
  var doc = document;
  var tip = doc.createElement('div');
  tip.className = 'kb-tip'; tip.id = 'kb-tip'; tip.setAttribute('role', 'tooltip');
  doc.body.appendChild(tip);
  var cur = null, timer = null;

  function clean(s) { return String(s || '').replace(/\s+/g, ' ').trim().slice(0, 260); }
  function tipText(el) {
    var t = el.getAttribute('data-tip');
    if (!t) {
      var row = el.closest ? el.closest('.vrow') : null;
      t = row ? row.textContent : el.textContent;
      t = clean(t);
    }
    return clean(t);
  }
  function place() {
    if (!cur) return;
    var r = cur.getBoundingClientRect();
    var tw = tip.offsetWidth, th = tip.offsetHeight;
    var x = r.left + r.width / 2 - tw / 2;
    var y = r.top - th - 8;
    if (y < 8) y = r.bottom + 8;
    x = Math.max(8, Math.min(x, window.innerWidth - tw - 8));
    tip.style.left = x + 'px';
    tip.style.top = y + 'px';
  }
  function show(el) {
    var t = tipText(el);
    if (!t) return;
    cur = el; tip.textContent = t; tip.classList.add('on'); place();
  }
  function hide() { cur = null; tip.classList.remove('on'); }

  var sel = '[data-tip],svg circle,.kpi,.card,.qa,.vrow,.vbar,.fnode,.ev>li,td,sup.ref,.badge,.act,ol.tl>li,ul.tl>li';
  Array.prototype.forEach.call(doc.querySelectorAll(sel), function (el) {
    if (!el.getAttribute('data-tip')) {
      var t = tipText(el);
      if (t) el.setAttribute('data-tip', t);
    }
    el.addEventListener('mouseenter', function () {
      if (timer) clearTimeout(timer);
      timer = setTimeout(function () { show(el); }, 80);
    });
    el.addEventListener('mouseleave', function () {
      if (timer) clearTimeout(timer);
      hide();
    });
    el.addEventListener('focus', function () { show(el); });
    el.addEventListener('blur', hide);
  });
  window.addEventListener('scroll', hide, { passive: true });
  window.addEventListener('resize', hide);

  Array.prototype.forEach.call(
    doc.querySelectorAll('.kpi,.card,.vrow,.fnode,.qa,.ev>li'),
    function (el) {
      el.addEventListener('click', function (e) {
        if (e.target.closest && e.target.closest('a,button')) return;
        el.classList.toggle('kb-active');
      });
    }
  );
})();
"""

_ans_CSS_SECTION = r"""
/* ==== 整块 section 悬停：结论摘要 / 正文大卡整体抬升 ==== */
@media (hover:hover) and (prefers-reduced-motion:no-preference){
  .kb-motion main > section{
    transition: opacity .45s ease, transform .22s ease, box-shadow .22s ease,
                border-color .22s ease, background-color .22s ease;
    will-change: transform;
  }
  .kb-motion main > section:hover{
    transform: translateY(-2px);
    box-shadow: 0 14px 32px rgba(16,24,40,.12);
    border-color: var(--line-strong);
    position: relative;
    z-index: 2;
  }
}
@media print{
  .kb-motion main > section{transform:none!important;box-shadow:none!important}
}
"""

_ans_CSS_DEMOALL = r"""
  :root{
    color-scheme:light;
    --bg:#e7e1d5;
    --paper:#fbf9f4;
    --card:#fffdf8;
    --hover:#f5f1e7;
    --ink:#191c1e;
    --ink-2:#3b4144;
    --muted:#757065;
    --line:#ddd5c6;
    --line-2:#ece6d9;
    --accent:#0d5c50;
    --accent-2:#0a4a40;
    --tint:#e6efe9;
    --gold:#a9832f;
    --warn:#8f3a2c;
    --link-line:rgba(13,92,80,.28);
    --shadow:0 40px 90px -60px rgba(45,38,24,.55);
    --serif:"Times New Roman",Times,"Microsoft YaHei","微软雅黑","Segoe UI Symbol","PingFang SC","Hiragino Sans GB",serif;
    --sans:"Times New Roman",Times,"Microsoft YaHei","微软雅黑","Segoe UI Symbol","PingFang SC","Hiragino Sans GB",sans-serif;
    --mono:"Times New Roman",Times,"Microsoft YaHei","微软雅黑","Segoe UI Symbol","PingFang SC","Hiragino Sans GB",monospace;
  }
  [data-theme="night"]{
    color-scheme:dark;
    --bg:#0c0e0c;
    --paper:#151713;
    --card:#1a1c17;
    --hover:#1f221c;
    --ink:#f0eadb;
    --ink-2:#cfc7b5;
    --muted:#98907f;
    --line:#2b2e27;
    --line-2:#232620;
    --accent:#83c7b2;
    --accent-2:#a9dcc9;
    --tint:#1c2a24;
    --gold:#d4ac64;
    --warn:#e0907c;
    --link-line:rgba(131,199,178,.35);
    --shadow:0 40px 90px -30px rgba(0,0,0,.9);
  }

  *{box-sizing:border-box}
  html{scroll-behavior:smooth}
  body{
    margin:0;background:var(--bg);color:var(--ink);
    font:15.5px/1.75 var(--sans);
    -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;
    transition:background-color .35s ease,color .35s ease;
  }
  html.locked{overflow:hidden}
  html.locked body{padding-right:var(--sbw,0px)}
  ::selection{background:rgba(13,92,80,.18)}
  [data-theme="night"] ::selection{background:rgba(131,199,178,.25)}
  a{color:var(--accent);text-decoration:none;border-bottom:1px solid var(--link-line);transition:border-color .18s ease}
  a:hover{border-bottom-color:var(--accent)}
  sup{font-size:.62em;line-height:0}
  :focus-visible{outline:2px solid var(--accent);outline-offset:3px;border-radius:2px}
  kbd{font:10px var(--mono);border:1px solid var(--line);border-radius:4px;padding:1px 5px;color:var(--muted);background:var(--paper);letter-spacing:0}

  /* 阅读进度 */
  .progress{position:fixed;inset:0 0 auto 0;height:2px;z-index:60;background:transparent;pointer-events:none}
  .progress i{display:block;height:100%;width:0;background:var(--accent);transition:width .12s linear}

  .sheet{
    max-width:1140px;margin:44px auto 64px;background:var(--paper);
    border:1px solid var(--line);box-shadow:0 1px 0 rgba(255,255,255,.85) inset,var(--shadow);
    padding:40px 60px 40px;
    transition:background-color .35s ease,border-color .35s ease,box-shadow .35s ease;
  }

  /* 页眉 */
  .rail{
    display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap;
    font:10.5px/1.4 var(--mono);letter-spacing:.14em;text-transform:uppercase;color:var(--muted);
    border-bottom:1px solid var(--line);padding-bottom:13px;
  }
  .brand{display:flex;align-items:center;gap:10px;letter-spacing:.12em}
  .mark{
    display:inline-grid;place-items:center;width:20px;height:20px;border-radius:3px;
    background:var(--accent);color:var(--paper);font:600 11px/1 var(--serif);letter-spacing:0;
  }
  .brand b{color:var(--ink-2);font-weight:500}
  .brand .sep{opacity:.5}
  .rail-right{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
  .fmt{opacity:.75;margin-left:6px;font-size:10px}

  .iconbtn{
    display:inline-flex;align-items:center;gap:6px;
    border:1px solid var(--line);background:var(--card);color:var(--muted);
    font:10.5px var(--mono);letter-spacing:.03em;
    padding:5px 9px;border-radius:8px;cursor:pointer;
    transition:color .18s ease,border-color .18s ease,background .18s ease,transform .12s ease;
  }
  .iconbtn:hover{color:var(--accent);border-color:var(--link-line);background:var(--paper)}
  .iconbtn:active{transform:translateY(1px)}
  .iconbtn svg{width:13px;height:13px;stroke:currentColor;fill:none;stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round}
  .iconbtn kbd{border-color:var(--line-2)}

  /* 标题区 */
  .hero{padding:32px 0 26px}
  .eyebrow{margin:0;font:10.5px/1.4 var(--mono);letter-spacing:.2em;text-transform:uppercase;color:var(--accent)}
  h1{
    font-family:var(--serif);font-weight:600;letter-spacing:-.012em;text-wrap:balance;
    font-size:clamp(30px,4.3vw,50px);line-height:1.14;margin:14px 0 14px;max-width:20em;
  }
  .deck{max-width:44em;margin:0;color:var(--ink-2);font-size:17px;line-height:1.7;text-wrap:pretty}
  .chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:18px}
  .chip{
    display:inline-flex;align-items:center;gap:6px;
    font:10.5px/1.5 var(--mono);color:var(--muted);
    border:1px solid var(--line);border-radius:999px;padding:3px 10px;background:var(--card);
  }
  .chip .dot{width:5px;height:5px;border-radius:50%;background:var(--accent);opacity:.65}
  .chip.accent{color:var(--accent);border-color:var(--link-line);background:var(--tint)}

  @media (prefers-reduced-motion: no-preference){
    .hero > *{animation:rise .7s cubic-bezier(.2,.8,.2,1) both}
    .hero > *:nth-child(2){animation-delay:.05s}
    .hero > *:nth-child(3){animation-delay:.1s}
    .hero > *:nth-child(4){animation-delay:.15s}
    @keyframes rise{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
  }

  /* 布局 */
  .layout{display:grid;grid-template-columns:176px minmax(0,1fr);gap:52px;margin-top:30px}
  aside{position:sticky;top:26px;align-self:start}
  .aside-label{margin:0 0 10px;font:10px/1.4 var(--mono);letter-spacing:.18em;text-transform:uppercase;color:var(--muted)}
  .toc{display:flex;flex-direction:column;border-top:1px solid var(--line-2)}
  .toc a{
    display:flex;gap:10px;align-items:baseline;padding:7px 0 7px 8px;
    border-bottom:1px solid var(--line-2);border-left:2px solid transparent;
    color:var(--ink-2);font-size:13px;
    transition:color .18s ease,border-color .18s ease,padding .18s ease;
  }
  .toc a:hover{color:var(--accent);border-left-color:var(--line)}
  .toc a.active{color:var(--accent);border-left-color:var(--accent);padding-left:12px}
  .toc .n{font:12px var(--mono);color:var(--muted);min-width:21px}
  .toc a.active .n{color:var(--accent)}

  .readrail{margin:14px 0 24px}
  .readbar{height:2px;background:var(--line-2);overflow:hidden;border-radius:2px}
  .readbar i{display:block;height:100%;width:0;background:var(--accent);transition:width .15s linear}
  .readpct{margin-top:6px;font:10px var(--mono);letter-spacing:.08em;color:var(--muted);display:flex;justify-content:space-between}

  .aside-meta{border-top:1px solid var(--line-2);padding-top:14px}
  .aside-meta dl{margin:0;display:grid;grid-template-columns:1fr;gap:6px}
  .aside-meta dt{font:10px var(--mono);letter-spacing:.12em;text-transform:uppercase;color:var(--muted)}
  .aside-meta dd{margin:0 0 6px;font:12px var(--mono);color:var(--ink-2)}
  .aside-note{margin:20px 0 0;font-size:11.5px;line-height:1.6;color:var(--muted);border-top:1px solid var(--line-2);padding-top:12px}

  main{min-width:0}
  section{padding:34px 0;border-top:1px solid var(--line)}
  section:first-child{border-top:0;padding-top:4px}
  section.pre{opacity:0;transform:translateY(10px)}
  section.in{opacity:1;transform:none;transition:opacity .55s ease,transform .55s cubic-bezier(.2,.8,.2,1)}
  .sec-head{display:flex;align-items:baseline;gap:12px;margin-bottom:18px}
  .sec-num{font:14px/1 var(--mono);letter-spacing:.08em;color:var(--accent);padding-top:1px}
  h2{font-family:var(--serif);font-weight:600;font-size:24px;letter-spacing:-.005em;margin:0}
  .sec-note{font-size:11.5px;color:var(--muted);margin-left:auto;font-family:var(--mono);letter-spacing:.04em}
  .linkbtn{border:0;background:none;padding:0;font:inherit;color:var(--accent);cursor:pointer;border-bottom:1px solid var(--link-line)}
  .linkbtn:hover{border-bottom-color:var(--accent)}

  .conclusion{
    margin:0;padding:4px 0 4px 22px;border-left:2px solid var(--accent);
    font-family:var(--serif);font-size:21px;line-height:1.62;color:var(--ink);max-width:30em;
  }
  .note{margin:14px 0 0;font-size:13px;color:var(--muted)}
  .note b{color:var(--ink-2);font-weight:600}

  .cite{border:0;background:none;padding:0 1px;font:10px var(--mono);color:var(--accent);cursor:pointer;vertical-align:baseline;line-height:0}
  .cite:hover{text-decoration:underline}

  .kpis{
    display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));
    border-top:1px solid var(--line-2);border-bottom:1px solid var(--line-2);margin-top:26px;
  }
  .kpi{padding:16px 18px 15px;border-left:1px solid var(--line-2)}
  .kpi:first-child{border-left:0;padding-left:0}
  .kpi .label{font:10px var(--mono);letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}
  .kpi .value{font-family:var(--serif);font-size:33px;line-height:1.15;margin-top:7px;letter-spacing:-.01em;font-variant-numeric:tabular-nums}
  .kpi .unit{font-family:var(--sans);font-size:12.5px;color:var(--muted);margin-left:5px;letter-spacing:0}
  .kpi .delta{font:11px var(--mono);color:var(--accent);margin-top:5px}
  .kpi .delta.mut{color:var(--muted)}

  /* 表格 */
  .table-wrap{overflow-x:auto;margin-top:2px}
  table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;min-width:560px;border-top:1px solid var(--ink-2)}
  th{
    font:10.5px var(--mono);letter-spacing:.1em;text-transform:uppercase;color:var(--muted);
    text-align:left;padding:10px 12px 9px;border-bottom:1px solid var(--ink-2);white-space:nowrap;
  }
  th.num,td.num{text-align:right}
  .sort{
    display:inline-flex;align-items:center;gap:6px;border:0;background:none;padding:0;
    font:inherit;letter-spacing:inherit;text-transform:inherit;color:inherit;cursor:pointer;
  }
  .sort:hover{color:var(--accent)}
  .sort .ar{font-size:9px;opacity:.35;transition:opacity .18s ease,transform .2s ease}
  th[aria-sort="ascending"] .ar,th[aria-sort="descending"] .ar{opacity:1;color:var(--accent)}
  th[aria-sort="descending"] .ar{transform:rotate(180deg)}

  td{padding:11px 12px;border-bottom:1px solid var(--line-2);vertical-align:baseline;transition:opacity .18s ease}
  tbody tr:last-child td{border-bottom:1px solid var(--ink-2)}
  td:first-child,th:first-child{padding-left:0}
  td.num{font:13px var(--mono);color:var(--ink-2)}
  tbody tr{cursor:pointer;transition:opacity .18s ease}
  tbody tr:hover td{background:var(--hover)}
  tbody tr:hover td:first-child{box-shadow:inset 2px 0 0 var(--accent)}
  tbody tr:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}
  tbody tr.dim td{opacity:.32}
  .hl{font-weight:600;color:var(--ink)}
  .heat-1{background:rgba(13,92,80,.06)}
  .heat-2{background:rgba(13,92,80,.13)}
  .heat-3{background:rgba(13,92,80,.21)}
  [data-theme="night"] .heat-1{background:rgba(131,199,178,.08)}
  [data-theme="night"] .heat-2{background:rgba(131,199,178,.15)}
  [data-theme="night"] .heat-3{background:rgba(131,199,178,.23)}
  tbody tr:hover td.heat-1,tbody tr:hover td.heat-2,tbody tr:hover td.heat-3{background:var(--hover)}

  /* 图 */
  svg{display:block;width:100%;height:auto;max-width:720px;overflow:visible}
  svg text{font-family:var(--mono)}
  .svglabel{font-size:11px;fill:var(--muted);letter-spacing:.02em}
  .svgval{font-size:12px;fill:var(--ink-2)}
  .svgtitle{font-size:10.5px;fill:var(--muted);letter-spacing:.14em;text-transform:uppercase}
  .gold{fill:var(--gold)}
  .grid{stroke:var(--line-2);stroke-width:1}
  .axis{stroke:var(--muted);stroke-width:1}
  .series{fill:none;stroke:var(--accent);stroke-width:2;stroke-linejoin:round}
  html.js .series{stroke-dasharray:1;stroke-dashoffset:1;transition:stroke-dashoffset 1.3s cubic-bezier(.2,.8,.2,1)}
  html.js section.in .series{stroke-dashoffset:0}
  .area-a{stop-color:var(--accent);stop-opacity:.18}
  .area-b{stop-color:var(--accent);stop-opacity:0}
  .dot{fill:var(--paper);stroke:var(--accent);stroke-width:2;transition:r .15s ease}
  .hit{fill:transparent}
  .pt{cursor:crosshair;transition:opacity .5s ease;transition-delay:var(--d,0s)}
  html.js .pt{opacity:0}
  html.js section.in .pt{opacity:1}
  .pt:hover .dot,.pt:focus-visible .dot{r:5.5;stroke-width:2.5}
  .pt:focus-visible .dot{stroke:var(--gold);stroke-width:3}
  .pt:focus-visible{outline:none}
  .tip{opacity:0;transition:opacity .15s ease;pointer-events:none}
  .tip.on{opacity:1}
  .tip rect{fill:var(--ink)}
  .tip text{fill:var(--paper);font-size:11px}
  .target{stroke:var(--gold);stroke-width:1;stroke-dasharray:3 4}
  .track{fill:var(--line-2)}
  .bar{fill:var(--ink-2);transition:width .55s cubic-bezier(.2,.8,.2,1)}
  .bar.acc{fill:var(--accent)}
  .brow{transition:opacity .18s ease;cursor:pointer}
  .brow:hover .svglabel,.brow:focus-visible .svglabel{fill:var(--ink-2)}
  .brow:focus-visible{outline:none}
  .brow:focus-visible .track{stroke:var(--accent);stroke-width:1}
  .brow.dim{opacity:.3}
  .off{display:none}

  .chart-head{display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap;margin:0 0 10px}
  .seg{display:inline-flex;gap:2px;padding:2px;border:1px solid var(--line);border-radius:999px;background:var(--card)}
  .seg button{border:0;background:transparent;color:var(--muted);font:11px var(--mono);padding:4px 12px;border-radius:999px;cursor:pointer;transition:background .18s ease,color .18s ease}
  .seg button[aria-pressed="true"]{background:var(--ink);color:var(--paper)}
  .chart-hint{font:10.5px var(--mono);color:var(--muted);letter-spacing:.04em}

  /* 流程 */
  .flow{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-top:4px}
  .node{
    display:inline-block;border:1px solid var(--line);border-radius:999px;padding:6px 14px;background:var(--card);
    font-size:13px;color:var(--ink-2);white-space:nowrap;transition:border-color .18s ease,transform .18s ease;
  }
  .node:hover{border-color:var(--accent);transform:translateY(-1px)}
  .node.acc{border-color:var(--link-line);background:var(--tint);color:var(--accent-2);font-weight:600}
  .arrow{color:#b9b09f;font-family:var(--mono);font-size:12px}
  .branch{margin:14px 0 0;padding-left:14px;border-left:1px dashed #c9bda8;font:11.5px/1.6 var(--mono);color:var(--warn);max-width:44em}

  /* 时间线 */
  .tl{list-style:none;margin:4px 0 0;padding:0}
  .tl li{display:grid;grid-template-columns:58px 1fr;gap:18px;padding:13px 0;border-top:1px solid var(--line-2)}
  .tl li:first-child{border-top:0;padding-top:0}
  .tl .yr{font:11px/1.6 var(--mono);color:var(--accent);letter-spacing:.04em}
  .tl .tt{margin:0;font-weight:600;color:var(--ink);font-size:14px}
  .tl .td{margin:2px 0 0;font-size:13px;color:var(--muted)}

  /* 来源 */
  .refs{list-style:none;margin:0;padding:0;counter-reset:r}
  .ref{counter-increment:r;border-top:1px solid var(--line-2)}
  .ref:first-child{border-top:0}
  .ref summary{display:grid;grid-template-columns:34px 1fr 18px;gap:10px;align-items:baseline;padding:12px 0;cursor:pointer;list-style:none}
  .ref summary::-webkit-details-marker{display:none}
  .ref summary::marker{content:""}
  .ref .rn{font:11px/1.9 var(--mono);color:var(--muted)}
  .ref .rn::before{content:"[" counter(r) "]"}
  .ref .rt{display:block;margin:0;font-family:var(--serif);font-size:15px;line-height:1.5;color:var(--ink)}
  .ref .rm{display:block;margin:3px 0 0;font:11px/1.7 var(--mono);color:var(--muted)}
  .ref-main{display:block}
  .ref .chev{font:12px var(--mono);color:var(--muted);transition:transform .2s ease}
  .ref[open] .chev{transform:rotate(45deg)}
  .ref summary:hover .rt{color:var(--accent)}
  .ref-body{padding:0 0 16px 44px;font-size:13.5px;color:var(--ink-2)}
  .ref-body p{margin:0 0 10px}
  .ref-actions{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
  .copy{border:1px solid var(--line);background:var(--card);color:var(--muted);font:10.5px var(--mono);padding:3px 9px;border-radius:999px;cursor:pointer;transition:color .18s ease,border-color .18s ease}
  .copy:hover{color:var(--accent);border-color:var(--accent)}
  .copy.ok{color:var(--accent);border-color:var(--accent)}

  .colophon{
    margin-top:10px;border-top:1px solid var(--line);padding-top:16px;
    display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap;
    font:10.5px/1.7 var(--mono);color:var(--muted);
  }
  .colophon .dash{flex:1;border-bottom:1px dotted var(--line);height:1em;min-width:24px}
  .keys{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
  .keys span{white-space:nowrap}

  /* 抽屉 */
  .drawer-bg{position:fixed;inset:0;z-index:80;background:rgba(15,13,9,.34);backdrop-filter:blur(3px);-webkit-backdrop-filter:blur(3px);opacity:0;pointer-events:none;transition:opacity .3s ease}
  .drawer-bg.on{opacity:1;pointer-events:auto}
  .drawer{
    position:fixed;top:0;right:0;height:100%;width:min(390px,94vw);z-index:90;
    background:var(--paper);border-left:1px solid var(--line);box-shadow:-30px 0 80px -40px rgba(0,0,0,.5);
    transform:translateX(102%);transition:transform .4s cubic-bezier(.2,.8,.2,1);
    display:flex;flex-direction:column;
  }
  .drawer.on{transform:none}
  .drawer-head{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:18px 22px;border-bottom:1px solid var(--line-2)}
  .drawer-head .k{font:10px var(--mono);letter-spacing:.18em;text-transform:uppercase;color:var(--muted)}
  .drawer-body{padding:20px 22px 30px;overflow:auto}
  .d-eyebrow{margin:0 0 6px;font:10px var(--mono);letter-spacing:.18em;text-transform:uppercase;color:var(--accent)}
  .d-title{margin:0 0 6px;font-family:var(--serif);font-size:30px;line-height:1.2}
  .d-big{margin:10px 0 18px;font-family:var(--serif);font-size:42px;line-height:1.05;letter-spacing:-.01em}
  .d-big .u{font-family:var(--mono);font-size:12px;color:var(--muted);margin-left:6px;letter-spacing:0}
  .d-list{display:grid;grid-template-columns:92px 1fr;gap:9px 12px;margin:0 0 18px;padding:16px 0;border-top:1px solid var(--line-2);border-bottom:1px solid var(--line-2)}
  .d-list dt{margin:0;font:10px var(--mono);letter-spacing:.12em;text-transform:uppercase;color:var(--muted)}
  .d-list dd{margin:0;font-size:13.5px;color:var(--ink-2)}
  .d-note{font-size:13px;line-height:1.7;color:var(--muted);margin:0 0 18px}
  .d-actions{display:flex;gap:8px;flex-wrap:wrap}
  .btn{
    display:inline-flex;align-items:center;gap:7px;border:1px solid var(--line);background:var(--card);
    color:var(--ink-2);font:11.5px var(--mono);padding:6px 12px;border-radius:8px;cursor:pointer;
    transition:color .18s ease,border-color .18s ease,background .18s ease;
  }
  .btn:hover{color:var(--accent);border-color:var(--link-line);background:var(--paper)}
  .btn.primary{background:var(--accent);border-color:var(--accent);color:var(--paper)}
  .btn.primary:hover{background:var(--accent-2);color:var(--paper)}

  /* 命令面板 */
  .palette-bg{position:fixed;inset:0;z-index:100;background:rgba(15,13,9,.4);backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px);opacity:0;pointer-events:none;transition:opacity .25s ease}
  .palette-bg.on{opacity:1;pointer-events:auto}
  .palette{
    position:fixed;top:11vh;left:50%;z-index:110;width:min(620px,92vw);
    transform:translateX(-50%) translateY(-8px) scale(.99);opacity:0;pointer-events:none;
    background:var(--paper);border:1px solid var(--line);border-radius:12px;
    box-shadow:0 50px 120px -50px rgba(0,0,0,.65);
    transition:opacity .2s ease,transform .2s cubic-bezier(.2,.8,.2,1);
    overflow:hidden;
  }
  .palette.on{opacity:1;pointer-events:auto;transform:translateX(-50%) translateY(0) scale(1)}
  .palette input{
    width:100%;border:0;background:none;padding:16px 18px;font:15px var(--sans);color:var(--ink);
    outline:none;border-bottom:1px solid var(--line-2);
  }
  .palette input::placeholder{color:var(--muted)}
  .palette ul{list-style:none;margin:0;padding:6px;max-height:46vh;overflow:auto}
  .palette li{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:9px 12px;border-radius:8px;font-size:13.5px;color:var(--ink-2);cursor:pointer}
  .palette li .k{font:10.5px var(--mono);color:var(--muted)}
  .palette li[aria-selected="true"]{background:var(--hover);color:var(--ink)}
  .palette li[aria-selected="true"] .k{color:var(--accent)}
  .palette-foot{display:flex;gap:14px;padding:9px 16px;border-top:1px solid var(--line-2);font:10px var(--mono);color:var(--muted)}

  /* 引用卡 */
  .citecard{
    position:fixed;z-index:105;width:300px;background:var(--paper);border:1px solid var(--line);border-radius:10px;
    box-shadow:0 28px 70px -34px rgba(0,0,0,.55);padding:13px 15px;
    opacity:0;pointer-events:none;transform:translateY(4px);transition:opacity .15s ease,transform .15s ease;
  }
  .citecard.on{opacity:1;pointer-events:auto;transform:none}
  .citecard .t{font-family:var(--serif);font-size:14px;line-height:1.45;margin:0}
  .citecard .m{font:10.5px var(--mono);color:var(--muted);margin:4px 0 10px}
  .citecard .btn{padding:4px 9px;font-size:10.5px}

  .toast{
    position:fixed;left:50%;bottom:26px;z-index:120;transform:translate(-50%,10px);
    background:var(--ink);color:var(--paper);font:11.5px var(--mono);letter-spacing:.02em;
    padding:8px 14px;border-radius:999px;opacity:0;pointer-events:none;
    transition:opacity .2s ease,transform .2s ease;
  }
  .toast.on{opacity:1;transform:translate(-50%,0)}

  ::view-transition-old(root),::view-transition-new(root){animation:none;mix-blend-mode:normal}

  @media (max-width:940px){
    .sheet{margin:0;border:0;padding:24px 18px 26px;box-shadow:none}
    .layout{grid-template-columns:1fr;gap:18px}
    aside{position:static;order:-1;border-bottom:1px solid var(--line);padding-bottom:14px}
    .toc{flex-direction:row;flex-wrap:wrap;gap:0 14px;border-top:0}
    .toc a{border-bottom:1px solid var(--line-2);border-left:0;padding:5px 0}
    .toc a.active{padding-left:0;border-left:0;box-shadow:inset 0 -2px 0 var(--accent)}
    .readrail,.aside-meta,.aside-note{display:none}
    .hero{padding-top:20px}
    h1{font-size:clamp(27px,7vw,38px)}
    .conclusion{font-size:19px}
    .kpi:first-child{padding-left:16px}
    .fmt{display:none}
    .drawer{width:min(360px,96vw)}
    .ref-body{padding-left:44px}
  }

  @media (prefers-reduced-motion: reduce){
    *{animation:none !important;transition:none !important}
    html{scroll-behavior:auto}
  }

  @media print{
    body{background:#fff}
    .progress,.toc,.readrail,.aside-note,.seg,.chart-hint,.copy,.rail-right,.drawer,.drawer-bg,.palette,.palette-bg,.citecard,.toast{display:none !important}
    .sheet{margin:0;border:0;box-shadow:none;max-width:none}
    html.js .series{stroke-dashoffset:0}
    html.js .pt{opacity:1}
    section{break-inside:avoid}
  }

/* ============ 兼容层：把引擎自有块对齐到 demo 的观感 ============ */
/* `.wrap` 承担 demo 的 `.sheet` 角色 */
.wrap{max-width:1080px;margin:32px auto 56px;padding:0 48px 40px;background:var(--paper);
  border:1px solid var(--line-2);box-shadow:var(--shadow);border-radius:2px}
.wrap > .rail{display:flex;align-items:center;justify-content:space-between;gap:16px;
  padding:14px 0;border-bottom:1px solid var(--line-2);font:11.5px var(--mono);
  letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
.wrap > .rail .brand{display:flex;align-items:center;gap:8px;color:var(--ink-2);
  text-transform:none;letter-spacing:0;font-size:13px}
.wrap > .rail .brand .mark{display:grid;place-items:center;width:20px;height:20px;border-radius:5px;
  background:var(--accent);color:var(--paper);font:600 11px var(--sans)}
.wrap > .rail .brand .sep{color:var(--line)}
.wrap > .hero{padding:38px 0 10px}
.wrap > .hero .eyebrow{margin:0;font:11px var(--mono);letter-spacing:.14em;
  text-transform:uppercase;color:var(--muted)}
.wrap > .hero h1{font-family:var(--serif);font-weight:600;font-size:clamp(30px,4.2vw,46px);
  line-height:1.14;letter-spacing:-.012em;margin:14px 0;max-width:20em;text-wrap:balance}
.wrap > .hero .deck{margin:0;color:var(--ink-2);font-size:17px;line-height:1.7;max-width:44em}
.wrap > .hero .chips{display:flex;flex-wrap:wrap;gap:8px;margin:20px 0 0}
.wrap > .hero .actions{margin-top:18px;display:flex;gap:8px}
.layout{display:grid;grid-template-columns:minmax(0,1fr);gap:0}
@media (min-width:900px){.layout{grid-template-columns:190px minmax(0,1fr);gap:44px}}
.layout > aside{padding:26px 0 0}
.aside-label{font:10.5px var(--mono);letter-spacing:.14em;text-transform:uppercase;
  color:var(--muted);margin:0 0 8px}
.aside-meta{margin-top:22px;font-size:11.5px;color:var(--muted)}
.aside-meta dt{font:10px var(--mono);letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
.aside-meta dd{margin:2px 0 10px;color:var(--ink-2)}
.aside-note{margin:18px 0 0;font-size:11px;line-height:1.6;color:var(--muted);
  border-top:1px solid var(--line-2);padding-top:12px}
.layout > main{padding:26px 0 0;min-width:0}
/* KPI 卡 */
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:0 24px;
  margin:18px 0 0;border-top:1px solid var(--line-2)}
.kpi{padding:14px 0 16px;border-bottom:1px solid var(--line-2)}
.kpi .klbl{font:10.5px var(--mono);letter-spacing:.12em;text-transform:uppercase;color:var(--muted)}
.kpi .kval{font-family:var(--serif);font-size:30px;line-height:1.2;margin-top:6px;
  letter-spacing:-.01em;font-variant-numeric:tabular-nums}
.kpi .kval .unit{font:12.5px var(--sans);color:var(--muted);margin-left:5px;letter-spacing:0}
.kpi .kvrange{font-size:12px;color:var(--muted);margin-top:4px}
.kpi .kvslope{font:12px var(--mono);color:var(--ink-2)}
/* 数值条 */
.vbars{margin:16px 0 0}
.vbars .vrow{display:flex;align-items:center;gap:10px;margin:6px 0}
.vbars .vlbl{width:9.5em;flex:none;font-size:13px;color:var(--ink-2)}
.vbars .vtrack{position:relative;flex:1;height:10px;background:var(--line-2);
  border-radius:999px;overflow:hidden}
.vbars .vbar{position:absolute;top:0;left:0;height:100%;background:var(--accent);border-radius:999px}
.vbars .vnum{font:12px var(--mono);color:var(--muted);min-width:4.5em;text-align:right}
/* 图表 / 表格 */
figure.vchart{margin:16px 0 0}
figure.vchart svg{width:100%;height:auto;display:block;max-width:100%}
figure.vchart figcaption{font:11.5px var(--mono);color:var(--muted);margin-top:8px;letter-spacing:.04em}
.tablewrap{overflow-x:auto;margin:16px 0 0}
.tnote{font:11.5px var(--mono);color:var(--muted);margin-top:8px;letter-spacing:.03em}
/* 流程 / 时间线 / 要点 / 卡片 / 来源 */
.flow{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin:6px 0 0}
.flow .fnode{display:inline-block;border:1px solid var(--line);background:var(--paper);
  border-radius:999px;padding:6px 12px;font-size:13px}
.flow .farrow{color:var(--muted);font-family:var(--mono)}
ul.tl{list-style:none;padding:0;margin:6px 0 0;border-left:1px solid var(--line);padding-left:16px}
ul.tl > li{position:relative;padding:0 0 16px}
ul.tl > li:before{content:"";position:absolute;left:-21px;top:6px;width:7px;height:7px;
  border-radius:50%;background:var(--accent)}
.key{border-left:2px solid var(--accent);padding:2px 0 2px 14px;margin:14px 0}
.key h4{margin:0 0 6px;font:600 10.5px var(--mono);letter-spacing:.12em;
  text-transform:uppercase;color:var(--muted)}
.key ul{margin:0;padding-left:18px;font-size:14px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;
  margin:6px 0 0}
.grid .card{border-top:2px solid var(--accent);padding:10px 0 0;background:none}
.grid .card h4{margin:0 0 4px;font-size:14px}
.grid .card p{margin:0;font-size:13.5px;color:var(--ink-2)}
ol.ev{list-style:none;padding:0;margin:0}
ol.ev > li{padding:14px 0;border-top:1px solid var(--line-2)}
ol.ev > li:first-child{border-top:0}
.bluf{background:var(--hover);border-left:2px solid var(--accent);padding:14px 18px}
.bluf .t{font:600 11px var(--mono);letter-spacing:.12em;text-transform:uppercase;color:var(--muted)}
.bluf p{margin:6px 0 0;font-family:var(--serif);font-size:17px;line-height:1.6}
/* ============ 交互层 2 ============ */
.progress{position:fixed;left:0;top:0;height:2px;width:100%;z-index:60;background:transparent}
.progress i{display:block;height:100%;width:0;background:var(--accent);transition:width .12s linear}
sup.c{cursor:pointer;border-bottom:1px solid var(--link-line);padding:0 1px}
sup.c:hover{color:var(--accent);border-bottom-color:var(--accent)}
ol.ev > li.flash{animation:kbflash 1.1s ease}
@keyframes kbflash{0%{background:var(--tint)}100%{background:transparent}}
table th[data-sort]{cursor:pointer;user-select:none}
table th[data-sort] .ar{font-size:9px;opacity:.35;margin-left:4px}
table th[data-sort].asc .ar,table th[data-sort].desc .ar{opacity:1;color:var(--accent)}
html{scroll-behavior:smooth}
@media (prefers-reduced-motion: reduce){html{scroll-behavior:auto}}
/* ============ 块级 hover 位移 ============ */
@media (hover:hover) and (prefers-reduced-motion:no-preference){
  table td:first-child{transition:padding-left .16s ease, box-shadow .16s ease}
  table tr{transition:background-color .16s ease}
  table tr:hover{background:var(--surface-4,var(--hover))}
  table tr:hover td:first-child{padding-left:16px;box-shadow:inset 3px 0 0 var(--accent)}
  .grid .card,.kpi,.key,.flow .fnode,figure.vchart,.vbars .vrow,ol.ev > li{
    transition:transform .18s ease,box-shadow .18s ease,border-color .18s ease,
               background-color .18s ease}
  .grid .card:hover,.kpi:hover,.key:hover{transform:translateY(-2px);
    box-shadow:0 10px 24px rgba(16,24,40,.10);border-color:var(--line-strong)}
  .flow .fnode:hover{transform:translateY(-2px);border-color:var(--accent);color:var(--accent)}
  .vbars .vrow:hover{transform:translateX(3px)}
  figure.vchart:hover{transform:translateY(-1px)}
  ol.ev > li:hover{transform:translateX(3px);background:var(--hover)}
}
@media print{
  table tr,.grid .card,.kpi,.key,.flow .fnode,.vbars .vrow,ol.ev > li,figure.vchart{
    transform:none!important;box-shadow:none!important}
  .progress{display:none!important}
}
"""

_ans_JS_FX2 = r"""
(function () {
  try {
    var prog = document.getElementById('kb-prog');
    if (prog) {
      var upd = function () {
        var h = document.documentElement;
        var max = (h.scrollHeight - h.clientHeight) || 1;
        prog.style.width = Math.min(100, Math.max(0, h.scrollTop / max * 100)) + '%';
      };
      window.addEventListener('scroll', upd, { passive: true });
      window.addEventListener('resize', upd);
      upd();
    }
  } catch (e) {}

  try {
    document.querySelectorAll('sup.c').forEach(function (sup) {
      sup.setAttribute('role', 'button');
      sup.setAttribute('tabindex', '0');
      var jump = function () {
        var n = (sup.textContent || '').replace(/[^0-9,]/g, '').split(',')[0];
        var ev = document.getElementById('ev');
        var items = document.querySelectorAll('ol.ev > li');
        var target = items[parseInt(n, 10) - 1];
        if (ev) ev.scrollIntoView({ behavior: 'smooth', block: 'start' });
        if (target) { target.classList.remove('flash'); void target.offsetWidth; target.classList.add('flash'); }
      };
      sup.addEventListener('click', jump);
      sup.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); jump(); }
      });
    });
  } catch (e) {}

  try {
    Array.prototype.slice.call(document.querySelectorAll('table')).forEach(function (tbl) {
      var head = tbl.tHead ? tbl.tHead.rows[0] : null;
      var body = tbl.tBodies[0];
      if (!head || !body) return;
      Array.prototype.slice.call(head.cells).forEach(function (th, idx) {
        th.setAttribute('data-sort', '1');
        th.setAttribute('title', '点击排序');
        th.addEventListener('click', function () {
          var dir = th.classList.contains('asc') ? -1 : 1;
          Array.prototype.slice.call(head.cells).forEach(function (o) {
            o.classList.remove('asc'); o.classList.remove('desc');
          });
          th.classList.add(dir === 1 ? 'asc' : 'desc');
          var num = function (s) { var m = String(s).replace(/,/g, '').match(/-?\d+(\.\d+)?/); return m ? parseFloat(m[0]) : null; };
          Array.prototype.slice.call(body.rows).sort(function (a, b) {
            var va = a.cells[idx] ? a.cells[idx].textContent.trim() : '';
            var vb = b.cells[idx] ? b.cells[idx].textContent.trim() : '';
            var na = num(va), nb = num(vb);
            if (na !== null && nb !== null) return (na - nb) * dir;
            return va.localeCompare(vb, 'zh') * dir;
          }).forEach(function (r) { body.appendChild(r); });
        });
      });
    });
  } catch (e) {}

  try {
    var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (!reduce && 'IntersectionObserver' in window) {
      var animate = function (el) {
        var node = el.firstChild;
        if (!node || node.nodeType !== 3) return;
        var txt = node.nodeValue || '';
        var m = txt.match(/^(\D*?)(\d[\d,]*)(.*)$/);
        if (!m) return;
        var target = parseFloat(m[2].replace(/,/g, ''));
        if (!isFinite(target) || target < 10) return;
        var t0 = performance.now(), dur = 820;
        var step = function (now) {
          var p = Math.min(1, (now - t0) / dur);
          node.nodeValue = m[1] + Math.round(target * (1 - Math.pow(1 - p, 3))) + m[3];
          if (p < 1) requestAnimationFrame(step);
          else node.nodeValue = m[1] + m[2] + m[3];
        };
        requestAnimationFrame(step);
      };
      var io = new IntersectionObserver(function (es) {
        es.forEach(function (e) {
          if (!e.isIntersecting) return;
          animate(e.target);
          io.unobserve(e.target);
        });
      }, { threshold: 0.4 });
      document.querySelectorAll('.kpi .kval').forEach(function (el) { io.observe(el); });
    }
  } catch (e) {}
})();
"""

_ans_DEMO_RAIL = (
    '<div class="rail"><span class="brand"><span class="mark">K</span><b>知识库答案页</b>'
    '<span class="sep">·</span>%s</span><span class="rail-right"><span class="fmt">FORMAT / %s</span>'
    '</span></div>')


def _ans_demo_shell(body: str, env: dict) -> str:
    """把旧 body 重排成 demo 骨架：rail → hero → layout(aside 目录+进度+检索信息) → main。"""
    body = '<div class="progress" aria-hidden="true"><i id="kb-prog"></i></div>' + body
    body = body.replace('<header class="doc">', '<header class="hero">', 1)
    body = body.replace('<p class="sub">', '<p class="deck">', 1)
    body = body.replace('<div class="metarow">', '<div class="chips">', 1)
    body = re.sub(r'<span class="badge(?: [a-z-]+)?"', '<span class="chip"', body)
    body = body.replace('<span class="chip">', '<span class="chip"><span class="dot"></span>', 1)
    body = body.replace('class="act"', 'class="iconbtn"')

    m_toc = re.search(r'<nav class="toc">([\s\S]*?)</nav>', body)
    toc_html = m_toc.group(1) if m_toc else ""
    if m_toc:
        body = body[:m_toc.start()] + body[m_toc.end():]
    toc_html = re.sub(
        r'<a href="#([^"]+)">(\d+) · ([^<]*)</a>',
        lambda m: '<a href="#%s"><span class="n">%s</span>%s</a>' % (m.group(1), m.group(2).zfill(2), m.group(3)),
        toc_html)

    body = body.replace(
        '<section id="s0"><h2><span class="num">00</span>结论摘要</h2>',
        '<section id="s0"><div class="sec-head"><span class="sec-num">00</span>'
        '<h2>结论摘要</h2><span class="sec-note">summary</span></div>', 1)
    body = re.sub(
        r'<section id="(s\d+)"><h2><span class="num">([^<]+)</span>([^<]+)</h2>',
        r'<section id="\1"><div class="sec-head"><span class="sec-num">\2</span>'
        r'<h2>\3</h2><span class="sec-note">section</span></div>',
        body)
    body = body.replace(
        '<section id="ev"><h2><span class="num">E</span>知识库来源</h2>',
        '<section id="ev"><div class="sec-head"><span class="sec-num">E</span>'
        '<h2>知识库来源</h2><span class="sec-note">references</span></div>', 1)

    m_head = re.search(r'<header class="hero">[\s\S]*?</header>', body)
    if m_head:
        head = m_head.group(0)
        m_eb = re.search(r'<p class="eyebrow">([^<]*)</p>', head)
        eyebrow = m_eb.group(1) if m_eb else ""
        body = body[:m_head.start()] + (_ans_DEMO_RAIL % (eyebrow, "ANSWER")) + head + body[m_head.end():]

    aside = '<div class="layout"><aside>'
    if toc_html:
        aside += '<p class="aside-label">目录</p><nav class="toc">%s</nav>' % toc_html
    aside += ('<div class="readrail" aria-hidden="true">'
              '<div class="readbar"><i id="kb-readbar"></i></div>'
              '<div class="readpct"><span>已读</span><span id="kb-readpct">0%</span></div></div>')
    rows = []
    if env.get("depth"):
        rows.append(("深度", _ans_esc(_ans_DEPTH.get(env["depth"], env["depth"]))))
    if env.get("hits") is not None:
        rows.append(("证据", "%s 块" % _ans_esc(env["hits"])))
    if env.get("ms") is not None:
        rows.append(("耗时", "%s ms" % _ans_esc(env["ms"])))
    rows.append(("渲染", "静态 HTML"))
    aside += '<div class="aside-meta"><p class="aside-label">检索</p><dl>%s</dl></div>' % "".join(
        "<dt>%s</dt><dd>%s</dd>" % (k, v) for k, v in rows)
    aside += ('<p class="aside-note">答案由 kb-rag 引擎从 kb-answer 信封确定性渲染；'
              '引用编号与右侧来源一一对应。</p></aside>')
    body = body.replace('<main id="main">', aside + '<main id="main">', 1)
    body = body.replace("</main>", "</main></div>", 1)
    return body


def _ans_elapsed(ms):
    """毫秒 → 人读的耗时：1 s 以下给 ms，以上给秒（一位小数）。"""
    try:
        v = float(ms)
    except (TypeError, ValueError):
        return ""
    if v >= 1000:
        return "%.1f s" % (v / 1000.0)
    return "%d ms" % int(round(v))


_ANS_PALETTE_LABELS = (("blue", "蓝"), ("green", "绿"), ("ink", "墨"), ("warm", "暖"))

# 四套预设只改主色系；夜间模式固定蓝，所以选择器用 :not([data-theme="night"])。
_ans_CSS_PALETTE = r"""
  html[data-palette="blue"]:not([data-theme="night"]){--accent:#1f5fa8;--accent-2:#174b86;--accent-soft:#eaf2fb;--tint:#e8f0fa;--link-line:rgba(31,95,168,.28)}
  html[data-palette="ink"]:not([data-theme="night"]){--accent:#2f3438;--accent-2:#22262a;--accent-soft:#eef0f1;--tint:#f0f1f2;--link-line:rgba(47,52,56,.28)}
  html[data-palette="warm"]:not([data-theme="night"]){--accent:#8a5a1f;--accent-2:#6e4617;--accent-soft:#f7efe2;--tint:#f6efe4;--link-line:rgba(138,90,31,.28)}
  .palpick{display:flex;justify-content:center;align-items:center;gap:8px;margin:0 0 10px;font:12px var(--mono);color:var(--muted)}
  .palpick select{font:12px var(--mono);color:var(--ink-2);background:var(--card);border:1px solid var(--line);border-radius:6px;padding:3px 6px}
  @media print{.palpick{display:none!important}}
"""

_ans_CSS_BARS = r"""
  /* 数值条的定位/间距：demo 样式只覆盖了 vtrack/vbar/vnum，vaxis 与单位/倍数间距
     仍靠旧 CSS，而旧 CSS 在 demo 重构后不再拼进产物 —— 结果轴标签全挤成一行、
     "300,000cm⁻²1×" 粘在一起。这里补一份最小规则，放在 demo CSS 之后。 */
  .vbars .vaxis{position:relative;height:24px;margin-top:2px}
  .vbars .vaxis .tk{position:absolute;top:5px;transform:translateX(-50%);white-space:nowrap;
    font:11.4px var(--mono);color:var(--muted)}
  .vbars .vaxis .tk.first{transform:none}
  .vbars .vstats{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 12px}
  .vbars .vstats span{white-space:nowrap}
  .vbars .vnum{display:flex;align-items:baseline;justify-content:flex-end;gap:5px;min-width:8.5em;
    font:12px var(--mono);color:var(--muted);text-align:right}
  .vbars .vnum .unit,.vbars .vnum .fold{white-space:nowrap;font-size:12px;color:var(--muted)}
  .ev-f{display:flex;flex-wrap:wrap;gap:4px 14px}
"""

_ans_JS_PALETTE = r"""
(function(){var s=document.getElementById('kb-palette');if(!s)return;var k='kb-palette';
try{var v=localStorage.getItem(k);if(v){document.documentElement.dataset.palette=v;s.value=v;}}catch(e){}
s.addEventListener('change',function(){document.documentElement.dataset.palette=s.value;
try{localStorage.setItem(k,s.value);}catch(e){}});})();
"""


def _ans_load_theme(req):
    """工作区主题：`.kb-rag/answer-theme.json`，其次 `state.json` 的 theme 字段。

    只在信封没带 theme 时调用。读不到就返回空 dict —— 主题坏掉不能连带把答案渲染弄挂。
    """
    paths = []
    kb_root = str((req or {}).get("kb_root") or ".kb")
    try:
        paths.append(Path(kb_root).expanduser().resolve().parent / ".kb-rag" / "answer-theme.json")
    except Exception:                                            # noqa: BLE001
        pass
    paths.append(Path.cwd() / ".kb-rag" / "answer-theme.json")
    seen = set()
    for p in paths:
        if str(p) in seen:
            continue
        seen.add(str(p))
        try:
            obj = json.loads(p.read_text(encoding="utf-8-sig"))
        except Exception:                                        # noqa: BLE001
            continue
        if isinstance(obj, dict):
            t = obj.get("theme") if isinstance(obj.get("theme"), dict) else obj
            if isinstance(t, dict):
                return t
    for p in (Path.cwd() / ".kb-rag" / "state.json",):
        try:
            obj = json.loads(p.read_text(encoding="utf-8-sig"))
            t = obj.get("theme") if isinstance(obj, dict) else None
            if isinstance(t, dict):
                return t
        except Exception:                                        # noqa: BLE001
            pass
    return {}


def _ans_theme_css(theme):
    """theme dict → (palette, 额外 CSS)。非法键/值静默丢弃，不阻断渲染。"""
    theme = theme if isinstance(theme, dict) else {}
    palette = str(theme.get("palette") or "").strip().lower()
    if palette not in ("blue", "green", "ink", "warm"):
        palette = ""
    parts = []
    accent = str(theme.get("accent") or "").strip()
    if re.match(r"^#[0-9A-Fa-f]{3,8}$", accent):
        parts.append(":root{--accent:%s}" % accent)
    ov = theme.get("overrides")
    if isinstance(ov, dict):
        decls = []
        for k, v in ov.items():
            k, v = str(k), str(v)
            if re.match(r"^--[A-Za-z0-9-]+$", k) and re.match(r"^[#A-Za-z0-9(),.%\s\-]+$", v):
                decls.append("%s:%s" % (k, v))
        if decls:
            parts.append(":root{%s}" % ";".join(decls))
    custom = theme.get("css")
    if isinstance(custom, str) and custom.strip():
        parts.append(custom.replace("</style", "<\\/style")[:20000])
    return palette, "\n".join(parts)


def _ans_render(env: dict) -> str:
    """kb-answer v1 信封 → 自包含 HTML。同一输入必须产生同一字节。"""
    tpl_id = env.get("template") or "review"
    tpl = _ans_TEMPLATES.get(tpl_id, _ans_TEMPLATES["review"])
    fields = env.get("fields") or {}
    # 第一跳（instant）走紧凑排版，见 _instant_sections 的说明
    compact = tpl_id == "instant" or env.get("compact") is True
    # 配色：信封 theme 优先，其次工作区文件（_answer_finish 已填进 env）；没写就是默认绿。
    _palette, _theme_css = _ans_theme_css(env.get("theme"))
    _pal_name = _palette or "green"

    # ---- 头部 ----
    badges = []
    vk, vl = _ans_VERDICT.get(env.get("verdict") or "hit", ("ok", "命中"))
    badges.append('<span class="badge %s">%s</span>' % (vk, _ans_esc(vl)))
    badges.append('<span class="badge"><b>%s</b></span>' % _ans_esc(tpl["label"]))
    if env.get("depth"):
        badges.append('<span class="badge">%s</span>' % _ans_esc(_ans_DEPTH.get(env["depth"], env["depth"])))
    if env.get("hits") is not None:
        badges.append('<span class="badge">命中 <b>%s</b> 块</span>' % _ans_esc(env["hits"]))
    if env.get("ms") is not None:
        badges.append('<span class="badge"><b>%s</b> ms</span>' % _ans_esc(env["ms"]))
    if env.get("reranker"):
        badges.append('<span class="badge">精排 <b>%s</b></span>' % _ans_esc(str(env["reranker"]).split(" ")[0]))

    h = []
    if env.get("nav"):
        h.append('<div class="nav">%s</div>' % " · ".join(
            '<a href="%s">%s</a>' % (_ans_esc(x.get("href", "#")), _ans_esc(x.get("text", "")))
            for x in env["nav"]))
    h.append('<main id="main">')                      # 语义地标：打印时展开链接 URL 靠它定位
    h += ['<header class="doc">',
          '<div class="eyebrow">%s</div>' % _ans_esc(env.get("eyebrow") or tpl["eyebrow"]),
          "<h1>%s</h1>" % _ans_esc(env.get("query", "")),
          '<p class="sub">%s</p>' % _ans_esc(env.get("sub") or tpl["desc"]),
          '<div class="metarow">%s</div>' % "".join(badges),
          '<div class="actions">',
          '<button class="act" onclick="window.print()">🖨 打印 / 导出 PDF</button>',
          '<button class="act" onclick="copyAll()">📋 复制全文为 Markdown</button>',
          "</div>", "</header>"]

    # ---- 00 结论摘要 ----
    # 模板若把 bottom_line 放首位（且是 note），就提到 00 里当"主要结论"，
    # 正文从 01 开始编号——与参考样例的结构一致，避免同一句结论出现两次。
    specs = list(tpl["sections"])
    head_spec = None
    if specs and specs[0]["id"] == "bottom_line" and specs[0]["kind"] == "note":
        head_spec = specs.pop(0)

    toc_items = []
    body_sections = []
    n = 0
    for spec in specs:
        v = fields.get(spec["id"])
        if v in (None, "", []):
            continue
        n += 1
        sid = "s%d" % n
        toc_items.append((sid, "%d · %s" % (n, spec["label"])))
        body_sections.append((sid, "%02d" % n, spec["label"], _ans_BLOCKS[spec["kind"]](v, spec, env)))

    if compact:
        # 第一跳：不建目录、不做每格一卡，答案直接落在首屏
        h += _ans__instant_sections(env, tpl, fields)
    else:
        hv = fields.get(head_spec["id"]) if head_spec else None
        # 模板首位没有 bottom_line、也没传 summary 时，00 节只剩目录：标题写着"结论摘要"
        # 却没有摘要，是页面上的假节。这时目录单独成块，正文从 01 开始。
        _has_lead = hv not in (None, "", []) or bool(env.get("summary"))
        if _has_lead:
            h.append('<section id="s0"><h2><span class="num">00</span>结论摘要</h2>')
        if hv not in (None, "", []):
            # 结论块不同于提示框：它是页面第二视觉层（仅次于标题）；底色按判定走，
            # 否则"库里没有"会顶着成功绿。所以走 bluf 变体。
            h.append(_ans_BLOCKS[head_spec["kind"]](
                hv, dict(head_spec, show_title=True, variant=_ans__lead_class(env.get("verdict"))), env))
        elif env.get("summary"):
            # summary = 引擎预填的主要结论。模板首位有 bottom_line 时以模型填的字段为准，
            # 字段为空才回落到这里（L3 预填 → L2 填空的降级阶梯）。
            h.append('<div class="bluf"><span class="t">主要结论</span><p>%s%s</p></div>'
                     % (_ans__md_inline(env["summary"]), _ans_sup(env.get("summary_refs"))))
        if toc_items:
            h.append('<nav class="toc">%s</nav>' % "".join(
                '<a href="#%s">%s</a>' % (i, _ans_esc(t)) for i, t in toc_items))
        if _has_lead:
            h.append("</section>")

        for sid, num, label, html_body in body_sections:
            h.append('<section id="%s"><h2><span class="num">%s</span>%s</h2>%s</section>'
                     % (sid, num, _ans_esc(label), html_body))

    h.append(_ans_suggest_html(env.get("suggest")))

    evs = env.get("evidence") or []
    if evs:
        h.append('<section id="ev"><h2><span class="num">%s</span>知识库来源</h2>%s</section>'
                 % ("E" if not toc_items else "E", _ans_evidence_html(evs)))

    h.append("</main>")
    _foot_time = []
    if env.get("ms") is not None:
        _foot_time.append("检索 %s" % _ans_elapsed(env["ms"]))
    if env.get("render_ms") is not None:
        _foot_time.append("渲染 %s" % _ans_elapsed(env["render_ms"]))
    _pal_opts = "".join('<option value="%s"%s>%s</option>'
                        % (k, ' selected' if k == _pal_name else '', label)
                        for k, label in _ANS_PALETTE_LABELS)
    h.append('<footer class="doc"><div class="palpick"><label for="kb-palette">配色</label>'
             '<select id="kb-palette" aria-label="配色">%s</select></div>%s<br>%s%s</footer>'
             % (_pal_opts,
                _ans_esc(env.get("footer") or
                    "由 kb-rag 引擎从 kb-answer 信封确定性渲染 · 协议 kb-answer v%s · 模板 %s"
                    % (env.get("version", 1), tpl_id)),
                "Ctrl/Cmd + P 可直接打印或导出为 PDF 分享",
                ("<br>%s" % " · ".join(_foot_time)) if _foot_time else ""))

    body = _ans_demo_shell("\n".join(h), env)
    layout = env.get("layout") or "card"
    # CSS_V2 追加在最后：同特异性下后写的胜出，覆盖层才能盖住布局 CSS
    css = (_ans_CSS_DEMOALL + _ans_CSS_MOTION + _ans_CSS_INTERACT
           + _ans_CSS_SECTION + _ans_CSS_PALETTE + _ans_CSS_BARS + "\n" + _theme_css)
    # 只有真的出现公式时才引入 MathJax —— 不带公式的输出保持零外部请求
    use_math = 'class="tex"' in body
    head_extra = _ans_MATH_HEAD if use_math else ""
    # JS 被禁用时 math-pending 会一直挂着 → 公式既非 MathJax 也非兜底，等于消失。
    # <noscript> 里补一条样式翻回来（渐进增强该有的样子）。
    noscript = ("<noscript><style>.math-pending .tex{visibility:visible}"
                ".math-pending .math-fallback{display:block}</style></noscript>\n") if use_math else ""

    return ("<!DOCTYPE html>\n<html lang=\"zh-CN\" data-palette=\"%s\">\n<head>\n<meta charset=\"UTF-8\">\n"
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            '<meta name="color-scheme" content="light dark">\n'
            "<title>%s · kb-answer</title>\n<style>\n%s\n</style>\n%s%s</head>\n"
            '<body%s>\n<a class="skip" href="#main">跳到正文</a>\n<div class="wrap">\n%s\n</div>\n'
            "<script>\n%s\n</script>\n</body>\n</html>\n"
            % (_pal_name, _ans_esc(env.get("query", "")[:60]), css, head_extra, noscript,
               (' class="math-pending layout-%s tpl-%s"' % (layout, tpl_id)) if use_math
               else (' class="layout-%s tpl-%s"' % (layout, tpl_id)), body,
               _ans_JS + "\n" + _ans_MOTION_JS + "\n" + _ans_JS_INTERACT + "\n" + _ans_JS_FX2
               + "\n" + _ans_JS_PALETTE))


def _ans_validate(env: dict) -> list:
    """渲染前的引注校验：[n] 必须落在 evidence 下标范围内（1 基）。

    这是 kb-answer 协议「渲染端裁剪」的一半——模型可能引用不存在的编号，
    渲染层必须挡住：**错号比没有引用更糟**，它会让溯源链断在最关键的一步。
    返回错误列表，空列表 = 通过。
    """
    n_ev = len(env.get("evidence") or [])
    used: list = []

    def walk(o):
        if isinstance(o, dict):
            r = o.get("refs")
            if isinstance(r, list):
                used.extend(r)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    for key in ("fields", "suggest"):
        walk(env.get(key))
    bad = sorted({x for x in used if not isinstance(x, int) or x < 1 or x > n_ev})
    if bad:
        return ["引注 [%s] 越界：本信封只有 %d 条证据" % (",".join(str(b) for b in bad), n_ev)]
    return []


# 只有这几类块的字符串是「原样 HTML」（不转义）：表格单元格、图注、代码。
# 其余块的内容都会过 _md_inline（转义一切，只放行 $..$ / `..` / **..**）。
# table 现在还要接受 markdown 管道表（内含 | 与缩进），它必须能走到 _ans_parse_pipe_table。
_ans_RAW_HTML_KINDS = {"table", "figure", "code"}
_ans__HTMLISH = __import__("re").compile(r"<\s*/?\s*[a-zA-Z]")


def _ans_validate_content(env: dict) -> list:
    """检测「内容里预埋 HTML」——它会被 _md_inline 转义成字面文本，静默毁掉排版。

    典型踩法：在 text 字段里写 `<sup class="c">[1]</sup>` 想加引注。正确做法是
    把编号放进同级的 `refs` 列表，由渲染器生成上标。
    """
    tpl = _ans_TEMPLATES.get(env.get("template") or "review") or {}
    kinds = {s["id"]: s.get("kind") for s in tpl.get("sections", [])}
    errs = []

    def walk(o, kind, path):
        if isinstance(o, dict):
            for k, v in o.items():
                walk(v, kind, "%s.%s" % (path, k))
        elif isinstance(o, list):
            for i, v in enumerate(o):
                walk(v, kind, "%s[%d]" % (path, i))
        elif isinstance(o, str) and kind not in _ans_RAW_HTML_KINDS and _ans__HTMLISH.search(o):
            errs.append("%s 里出现 HTML 标签，会被转义成字面文本：%s"
                        % (path or "内容", o[max(0, _ans__HTMLISH.search(o).start()):][:48]))

    for fid, val in (env.get("fields") or {}).items():
        walk(val, kinds.get(fid, "text"), "fields.%s" % fid)
    walk(env.get("suggest"), "suggest", "suggest")
    walk(env.get("summary"), "text", "summary")          # 也过 _md_inline，别忘了它
    return errs


def _ans_render_index(entries) -> str:
    cards = []
    for e in entries:
        tpl = _ans_TEMPLATES[e["template"]]
        vk, vl = _ans_VERDICT.get(e.get("verdict") or "hit", ("ok", "命中"))
        cards.append('<a href="%s"><div class="t">%s · %s</div><div class="d">%s<br>'
                     '<span class="badge %s">%s</span> <span class="badge">%s</span></div></a>'
                     % (_ans_esc(e["file"]), _ans_esc(tpl["label"]), _ans_esc(e["file"]),
                        _ans_esc(e["query"][:52]), vk, _ans_esc(vl), _ans_esc(_ans_DEPTH.get(e.get("depth"), ""))))
    h = ['<header class="doc">',
         '<div class="eyebrow">kb-answer v1 · HTML 输出格式</div>',
         "<h1>推荐输出格式 · 六种预设模板</h1>",
         '<p class="sub">同一份 kb-answer 信封，由引擎确定性渲染成自包含单文件 HTML。'
         '样式内联、无外部请求、支持深色打印；每种格式只由一条模板定义驱动，'
         "加格式不改渲染逻辑。</p>",
         '<div class="metarow">'
         '<span class="badge ok">自包含单文件</span>'
         '<span class="badge">零外部请求</span>'
         '<span class="badge">加格式 = 加一条定义</span>'
         '<span class="badge">示例内容为占位语料</span>'
         "</div>",
         '<div class="actions">'
         '<button class="act" onclick="window.print()">🖨 打印 / 导出 PDF</button>'
         "</div>", "</header>",
         '<section id="s0"><h2><span class="num">01</span>六种格式</h2>'
         '<div class="menu">%s</div></section>' % "".join(cards),
         '<footer class="doc">渲染器：<code>proto/kb_html.py</code>（纯函数、零依赖）· '
         "同一信封两次渲染字节相同</footer>"]
    return ("<!DOCTYPE html>\n<html lang=\"zh-CN\">\n<head>\n<meta charset=\"UTF-8\">\n"
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            "<title>推荐输出格式 · HTML 样例</title>\n<style>\n%s\n</style>\n</head>\n<body>\n"
            '<div class="wrap">\n%s\n</div>\n<script>\n%s\n</script>\n</body>\n</html>\n'
            % (_ans_CSS + _ans_CSS_BLOCKS, "\n".join(h), _ans_JS))


def _ans_selfcheck(page: str) -> list:
    errs = []
    from html.parser import HTMLParser
    void = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
            "meta", "param", "source", "track", "wbr"}

    class P(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack = []

        def handle_starttag(self, tag, attrs):
            if tag not in void:
                self.stack.append(tag)

        def handle_endtag(self, tag):
            if tag in void:
                return
            if not self.stack:
                errs.append("多余的 </%s>" % tag)
            elif self.stack[-1] != tag:
                errs.append("未配平：期望 </%s>，实际 </%s>" % (self.stack[-1], tag))
                if tag in self.stack:
                    while self.stack and self.stack.pop() != tag:
                        pass
            else:
                self.stack.pop()

    p = P()
    p.feed(page)
    if p.stack:
        errs.append("未闭合：%s" % ", ".join(p.stack))

    # 外部请求：只允许 data: 内嵌图；MathJax 仅在页面真的含公式时放行
    use_math = 'class="tex"' in page
    for m in __import__("re").finditer(r'\ssrc="([^"]*)"', page):
        u = m.group(1)
        if u.startswith("data:"):
            continue
        if use_math and u == _ans_MATHJAX_URL:
            continue
        errs.append("外部资源 src=%s" % u[:70])
    # 出网请求：先剥掉两类**不是请求**的 http(s) 出现，否则自检会咬自己的尾巴：
    #   · <a href="https://doi.org/..."> —— 用户点击的外链，页面本身不会去取；
    #   · xmlns="http://www.w3.org/2000/svg" —— SVG 命名空间声明，只是标识符。
    # 早期版本直接搜 "http://"，结果只要正文提到一个 http 地址就报错（实测被引文里的
    # http://arxiv.org 触发），于是这条自检只能被无视 —— 一个总在叫的警报比没有更糟。
    probe = __import__("re").sub(r'<a\b[^>]*>', "<a>", page)
    probe = probe.replace('xmlns="http://www.w3.org/2000/svg"', "xmlns=svg")
    for bad in ("<link", "@import", "url(", "http://", "https://"):
        if bad in probe:
            errs.append("出现外部依赖：%s" % bad)
    return errs
# ==== END kb-answer HTML renderer ====


# ---------------------------------------------------------------- main


def main():
    if len(sys.argv) < 2:
        sys.stdout.write(json.dumps({"ok": False, "error": "usage: kb_engine.py <ingest|search|rag|answer|stats|zotero|dedup|clear|fetch|reload|state|serve>"}))
        return 1
    command = sys.argv[1]
    if command == "serve":
        cmd_serve()
        return 0
    if command == "run_job":          # 后台任务子进程入口（argv 传 job 文件，不走 stdin）
        run_async_job(sys.argv[2] if len(sys.argv) > 2 else "")
        return 0
    handler = {"ingest": cmd_ingest, "ingest_async": cmd_ingest_async, "status": cmd_status,
               "search": cmd_search, "rag": cmd_rag, "answer": cmd_answer,
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
