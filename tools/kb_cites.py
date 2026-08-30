#!/usr/bin/env python3
# kb_cites.py — 从知识库 chunks 里抽取参考文献中引用 Nature 系列期刊的条目。
# 用法:
#   python kb_cites.py [db路径]
import sqlite3, re, sys, os

DB = sys.argv[1] if len(sys.argv) > 1 else r"F:\Desktop\workspace\.kb\kb.sqlite"

c = sqlite3.connect(DB)
rows = c.execute("select text from chunks").fetchall()

pat = re.compile(
    r'([A-Z][A-Za-z\-\'’ ]{2,40}?)(?:,| and| &|\.)[^.]{0,80}?'
    r'((?:Nature|Nat\.)\s+(?:Mater\.|Nanotechnol\.|Phys\.|Commun\.|Electron\.|Energy|Synthesis|Reviews\w*)?\s*\d{1,3}\s*,\s*\d{1,6}[–\-]\d{1,6}\s*\((\d{4})\))'
)

hits, seen = [], set()
for (text,) in rows:
    for m in pat.finditer(text or ""):
        s = " ".join(m.group(0).split())
        if s[:60] in seen:
            continue
        seen.add(s[:60])
        hits.append(s)

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nature_cites.txt")
with open(out, "w", encoding="utf-8") as f:
    f.write("\n".join(hits))
print(f"命中 {len(hits)} 条,已写入: {out}")
