# kb-rag 文献工具集

配合 `dsh-kb-rag` 知识库插件使用的辅助脚本,都在本目录。数据默认在 `<工作区>\.kb\kb.sqlite`(可用参数/`--db` 指定)。

## 1. doi_pdf.mjs — DOI → 下载 PDF(核心工具)

给定 DOI,自动找 PDF 并下载(**出版商正式版优先 → 开放获取兜底**)。**不依赖按钮位置**,靠标准 `citation_pdf_url` meta 标签 + Unpaywall + Crossref + 链接模式。

```powershell
# 单个(相对路径示例,请替换为你自己的目录)
node .\tools\doi_pdf.mjs --out downloads "10.1038/nature06932"

# 多个
node .\tools\doi_pdf.mjs --out downloads "10.1038/nature06932" "10.1038/nmat3223"

# 从文件批量(一行一个 DOI)
node .\tools\doi_pdf.mjs --file dois.txt
```

参数:
- `--out <目录>`:输出目录(默认 `./downloads`)
- `--file <txt>`:从文本文件读 DOI(一行一个,`#` 开头为注释)

**查找顺序(2026-09 重排,付费墙优先):**
1. arXiv 直连(裸 ID / `arXiv:ID` / `10.48550/arXiv.ID` / abs URL)
2. 落地页 `citation_pdf_url` meta(**出版商正式版**;校园网/机构 IP 可直接下订阅版 PDF)
3. 落地页内 `pdfdirect/.pdf/epdf//pdf/` 链接模式
4. Unpaywall(OA)兜底
5. Crossref 记录的 PDF 链接

已实现"手动跟随重定向 + 全程携带 cookie"(绕过 Nature 的 `cookies_not_supported`)、
MDPI 的 `bm-verify` 令牌跟随尝试、Cloudflare/Akamai 挑战页识别(明确报"需真实浏览器手动下载")。

**已知限制(实测,2026-09):**
- ❌ Cloudflare JS 挑战:Wiley(onlinelibrary)、Science(science.org)、Cambridge(cambridge.org) → 脚本 403 "Just a moment",需真实浏览器
- ❌ Akamai Bot Manager:MDPI(mdpi.com)`ak_bmsc` cookie 令牌跟随后仍 403 Access Denied,需真实浏览器
- ❌ APS(journals.aps.org,PRB/PRL)同样 403
- ✅ 以上场景请用 Zotero 浏览器插件抓取,再 `kb_zotero` 同步入库

## 2. 入库

下载到 `downloads` 目录后,在 DSH 对话里说"把 downloads 目录入库"即可(模型调用 `kb_ingest`)。
或直接给模型 DOI,让它一次性"下载 + 入库"。

## 3. 元数据修正(常用)

引擎从 PDF 内部抓的标题/年份经常是错的(如 `Microsoft Word - xxx.doc`、`untitled`、错年份)。
用 Crossref 权威数据一键修正:

```powershell
# --db 默认 <当前目录>/.kb/kb.sqlite,可用 --db 指定
python .\tools\kb_fix_meta.py 10.1038/nature06932
# 同名多版本时精确指定路径子串:
python .\tools\kb_fix_meta.py 10.1038/nmat3415 --like "ferroelectric memristor"
```

## 4. 其它

| 脚本 | 用途 |
|---|---|
| `kb_list.py` | 列出知识库全部文献清单(输出 `doc_list.txt`,默认读 `<当前目录>/.kb/kb.sqlite`) |
| `kb_cites.py` | 从库里文献的参考文献中抽取 Nature 系列引文(输出 `nature_cites.txt`) |
| `kb_fix_meta.py` | 按 DOI 用 Crossref 修正元数据 |

## 目录约定(示例)

```
<工作区>\
├─ tools\           ← 本目录(脚本)
├─ downloads\       ← DOI 下载的 PDF 中转站
└─ .kb\             ← 知识库(SQLite + 数据)
```
