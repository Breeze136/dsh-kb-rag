#!/usr/bin/env python3
# kb_fix_meta.py — 用 Crossref 权威元数据修正已入库文献(引擎从 PDF 内部抓的标题/年份常会错)。
# 用法:
#   python kb_fix_meta.py <DOI> [<DOI> ...] [--like 路径子串]
#     --like: 只更新 path 中包含该子串的文档(用于同一标题多个版本时精确定位);省略则按 path 中英文标题自动匹配。
import sqlite3, json, sys, urllib.request, urllib.parse, re

DB = r"F:\Desktop\workspace\.kb\kb.sqlite"

def slug(s):
    s = re.sub(r"[^a-zA-Z0-9 ]+", " ", (s or "").lower())
    return re.sub(r"\s+", " ", s).strip()[:40]

def fetch_crossref(doi):
    url = "https://api.crossref.org/works/" + urllib.parse.quote(doi)
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)["message"]

def main():
    args = sys.argv[1:]
    likes = {}
    for i in range(len(args)):
        if args[i] == "--like" and i + 1 < len(args):
            likes[args[i + 1]] = True
    dois = [a for a in args if not a.startswith("--") and a not in likes]

    c = sqlite3.connect(DB)
    for doi in dois:
        m = fetch_crossref(doi)
        title = m["title"][0]
        authors = "; ".join(f"{a.get('given','')} {a.get('family','')}".strip() for a in m.get("author", []))
        year = (m.get("issued") or {}).get("date-parts", [[None]])[0][0]
        journal = (m.get("container-title") or [""])[0]
        key = likes.get(doi, slug(title))
        like = f"%{key}%"
        cur = c.execute(
            "UPDATE docs SET title=?, authors=?, year=?, doi=?, journal=? WHERE path LIKE ? OR title LIKE ?",
            (title, authors, year, doi, journal, like, like),
        )
        c.commit()
        print(f"[{doi}] 更新 {cur.rowcount} 行 → {title} ({year}, {journal})")
    c.close()

if __name__ == "__main__":
    main()
