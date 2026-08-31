#!/usr/bin/env python3
# kb_list.py — 列出知识库里全部文献(按年份倒序),输出到文本文件并打印统计。
# 用法:
#   python kb_list.py [db路径] [输出文件]
# 默认 db = 工作区下 .kb/kb.sqlite,输出 = 同目录 doc_list.txt
import sqlite3, sys, os

db = sys.argv[1] if len(sys.argv) > 1 else os.path.join(".kb", "kb.sqlite")
out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "doc_list.txt")

c = sqlite3.connect(db)
rows = c.execute(
    "SELECT year, title, authors, journal, doi, kind, chunk_count FROM docs ORDER BY year DESC, title"
).fetchall()

lines = [f"TOTAL DOCS: {len(rows)}"]
for i, (year, title, authors, journal, doi, kind, cc) in enumerate(rows, 1):
    y = year if year is not None else "----"
    lines.append(f"{i:4d}. [{y}] {(title or '(untitled)')[:120]}")
    if authors:
        lines.append(f"        {(authors or '')[:90]}")
    meta = f"        {(journal or '')}"
    if doi:
        meta += f" | doi:{doi}"
    if kind:
        meta += f" | {kind}"
    lines.append(f"{meta} | chunks={cc}")

with open(out, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print(f"共 {len(rows)} 篇,清单已写入: {out}")
