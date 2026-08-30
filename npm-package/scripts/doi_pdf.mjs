#!/usr/bin/env node
// doi_pdf.mjs — 给定 DOI,自动解析并下载 PDF(优先 OA,其次校园网订阅,靠 citation_pdf_url meta 而非按钮位置)
// 用法:
//   node doi_pdf.mjs "10.1038/s41586-018-0770-2" "10.1063/1.2753390"
//   node doi_pdf.mjs --file dois.txt
//   node doi_pdf.mjs --out "F:\Desktop\workspace\downloads" 10.xxxx/yyyy
// 输出:每个 DOI 一行 JSON {doi, ok, source, file, title, error}

import { writeFileSync, mkdirSync, existsSync, readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36";

// ---- 参数解析 ----
const args = process.argv.slice(2);
let outDir = resolve(process.cwd(), "downloads");
let dois = [];
for (let i = 0; i < args.length; i++) {
  if (args[i] === "--out") { outDir = resolve(args[++i]); continue; }
  if (args[i] === "--file") {
    const p = args[++i];
    const txt = readFileSync(p, "utf8");
    dois.push(...txt.split(/\r?\n/).map(s => s.trim()).filter(s => s && !s.startsWith("#")));
    continue;
  }
  if (args[i] === "--help" || args[i] === "-h") {
    console.log(`用法: node doi_pdf.mjs [--out 目录] [--file dois.txt] <DOI> <DOI> ...`);
    process.exit(0);
  }
  dois.push(args[i]);
}
mkdirSync(outDir, { recursive: true });

const norm = (d) => d.replace(/^https?:\/\/(dx\.)?doi\.org\//i, "").replace(/^https?:\/\/arxiv\.org\/abs\//i, "").replace(/^arXiv[:/]/i, "").replace(/[,.;]+$/, "").trim();

// 简易 cookie jar:Set-Cookie 只取 name=value
function cookiesFrom(res) {
  const out = [];
  const setc = res.headers.getSetCookie ? res.headers.getSetCookie() : [];
  for (const c of setc) { const p = c.split(";")[0]; if (p) out.push(p); }
  return out;
}

// 手动跟随重定向并全程携带 cookie(node fetch 跨重定向不保留 Set-Cookie,Nature 会因此报 cookies_not_supported)
async function requestOnce(url, headers) {
  return await fetch(url, { headers, redirect: "manual" });
}

function applyCookies(jar, res) {
  for (const c of cookiesFrom(res)) {
    const name = c.split("=")[0];
    jar = jar.filter(x => !x.startsWith(name + "="));
    jar.push(c);
  }
  return jar;
}

async function get(url, jar = []) {
  let current = url;
  for (let i = 0; i < 10; i++) {
    const headers = { "User-Agent": UA, Accept: "text/html,application/xhtml+xml,*/*" };
    if (jar.length) headers.Cookie = jar.join("; ");
    const r = await requestOnce(current, headers);
    jar = applyCookies(jar, r);
    if (r.status >= 300 && r.status < 400 && r.headers.get("location")) {
      current = new URL(r.headers.get("location"), current).href;
      continue;
    }
    return r;
  }
  throw new Error("too many redirects at " + url);
}

async function getBuf(url, jar = [], acceptPdf = true, depth = 0) {
  let current = url;
  let r;
  for (let i = 0; i < 10; i++) {
    const headers = { "User-Agent": UA };
    if (jar.length) headers.Cookie = jar.join("; ");
    r = await requestOnce(current, headers);
    jar = applyCookies(jar, r);
    if (r.status >= 300 && r.status < 400 && r.headers.get("location")) {
      current = new URL(r.headers.get("location"), current).href;
      continue;
    }
    break;
  }
  const ct = (r.headers.get("content-type") || "").toLowerCase();
  const buf = Buffer.from(await r.arrayBuffer());
  const isPdf = ct.includes("pdf") || (buf.length > 4 && buf.slice(0, 4).toString() === "%PDF");
  if (!isPdf && ct.includes("html")) {
    const head = buf.slice(0, 4096).toString("utf8");
    // Cloudflare JS 挑战:真实浏览器才能过,脚本无解,给出明确原因
    if (head.includes("Just a moment") || head.includes("cf-browser-verification") || head.includes("__cf_chl")) {
      return { ok: false, reason: "Cloudflare JS 挑战(需真实浏览器执行 JS)", url };
    }
    // Akamai Bot Manager:bm-verify 令牌跟随后仍 403 Access Denied(需 JS 算 _abck cookie)
    if (head.includes("Access Denied") || head.includes("akamai")) {
      return { ok: false, reason: "Akamai 反爬(Access Denied,需真实浏览器)", url };
    }
    // MDPI BlockMetrics:meta-refresh 给 bm-verify token,带 cookie 再跟一次即得真 PDF
    const m = head.match(/<meta[^>]+http-equiv=["']refresh["'][^>]+content=["']\d+;\s*URL=['"]([^'"]+)["']/i);
    if (m && depth < 3) {
      const next = new URL(unescapeHtml(m[1]), current).href;
      return await getBuf(next, jar, acceptPdf, depth + 1);
    }
    if (acceptPdf) {
      return { ok: false, reason: "出版商返回 HTML 而非 PDF(付费墙/需订阅/反爬)", url };
    }
  }
  return { ok: true, buf, ct, isPdf };
}

function unescapeHtml(s) {
  return s.replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&#39;/g, "'");
}

async function findPdf(doi) {
  const raw = (doi || "").trim();
  const d = norm(raw);
  const jar = [];
  const tries = [];

  // 0) arXiv 直连(原版缺失,补上:裸 ID / arXiv:ID / 10.48550/arXiv.ID / abs URL)
  const am = raw.match(/arxiv\.org\/abs\/(\d{4}\.\d{4,5})/i)
          || raw.match(/\b10\.48550\/arXiv\.(\d{4}\.\d{4,5})/i)
          || raw.match(/^arXiv[:/](\d{4}\.\d{4,5})/i)
          || raw.match(/^(\d{4}\.\d{4,5})$/);
  if (am) {
    const aid = am[1];
    tries.push({ source: "arxiv", url: `https://arxiv.org/pdf/${aid}`, title: null });
    return { doi: d, jar, tries, landingUrl: null };
  }

  // 1) 落地页 citation_pdf_url meta(出版商正式版优先:校园网可下订阅版 PDF;找不到再走 OA)
  let landing = null;
  let landingBlocked = null;
  try {
    landing = await get(`https://doi.org/${encodeURIComponent(d)}`, jar);
    const html = await landing.text();
    if (landing.status === 403 || /Just a moment|Access Denied|cf-browser-verification|__cf_chl/i.test(html.slice(0, 4096))) {
      landingBlocked = landing.status === 403 ? "403(Cloudflare/Akamai 反爬)" : "反爬挑战页";
    } else {
      let pdfUrl = null, title = null;
      const m = html.match(/<meta[^>]+(?:name|property)=["']citation_pdf_url["'][^>]*content=["']([^"']+)["']/i)
             || html.match(/<meta[^>]+content=["']([^"']+)["'][^>]*(?:name|property)=["']citation_pdf_url["']/i);
      if (m) pdfUrl = unescapeHtml(m[1]);
      const t = html.match(/<meta[^>]+(?:name|property)=["']citation_title["'][^>]*content=["']([^"']+)["']/i)
             || html.match(/<title>(.*?)<\/title>/i);
      if (t) title = unescapeHtml(t[1]).replace(/\s+/g, " ").trim();
      if (pdfUrl) tries.push({ source: "citation_pdf_url", url: new URL(pdfUrl, landing.url).href, title });
      // 落地页里常见的 pdf 链接模式(兜底,优先于 OA)
      for (const mm of html.matchAll(/href=["']([^"']*(?:pdfdirect|\.pdf|epdf|\/pdf\/|am-pdf)[^"']*)["']/gi)) {
        tries.push({ source: "pattern", url: new URL(unescapeHtml(mm[1]), landing.url).href, title });
      }
    }
  } catch (e) {
    landingBlocked = String(e?.cause?.code || e?.message || e);
  }

  // 2) Unpaywall(OA)
  try {
    const r = await fetch(`https://api.unpaywall.org/v2/${encodeURIComponent(d)}?email=researcher@university.edu`, { headers: { "User-Agent": UA } });
    if (r.ok) {
      const j = await r.json();
      const p = j.best_oa_location?.url_for_pdf || j.best_oa_location?.url;
      if (p) { tries.push({ source: "unpaywall", url: p, title: j.title }); }
    }
  } catch {}

  // 3) Crossref link 里的 PDF
  try {
    const r = await fetch(`https://api.crossref.org/works/${encodeURIComponent(d)}`, { headers: { "User-Agent": UA } });
    if (r.ok) {
      const j = await r.json();
      const m = j.message;
      const title = m?.title?.[0];
      for (const l of m?.link || []) {
        if (/pdf/i.test(l["content-type"] || "") || /\.pdf($|\?)/i.test(l.URL || "")) {
          tries.push({ source: "crossref", url: l.URL, title });
        }
      }
    }
  } catch {}

  return { doi: d, jar, tries, landingUrl: landing?.url, landingBlocked };
}

function safeName(s) {
  return (s || "").replace(/[\\/:*?"<>|\n\r\t]+/g, " ").replace(/\s+/g, " ").trim().slice(0, 160);
}

const results = [];
for (const raw of dois) {
  const d = norm(raw);
  const rec = { doi: d, ok: false, source: null, file: null, title: null, error: null, tried: 0 };
  try {
    const { jar, tries, landingUrl, landingBlocked } = await findPdf(d);
    // 去重,保留顺序
    const seen = new Set();
    const cands = tries.filter(t => t.url && !seen.has(t.url) && seen.add(t.url));
    rec.tried = cands.length;
    let done = false;
    for (const c of cands) {
      try {
        const g = await getBuf(c.url, jar);
        if (!g.ok) { rec.lastReason = g.reason; continue; }
        const title = c.title || d;
        const ext = g.isPdf ? ".pdf" : (g.ct.includes("pdf") ? ".pdf" : ".bin");
        const fn = `${safeName(title) || d.replace(/[/:]/g, "_")}${ext}`;
        const p = join(outDir, fn);
        writeFileSync(p, g.buf);
        rec.ok = true; rec.source = c.source; rec.file = p; rec.title = title; rec.contentType = g.ct;
        done = true;
        break;
      } catch (e) {
        rec.lastReason = String(e?.cause?.code || e?.message || e);
      }
    }
    if (!done) {
      const cause = rec.lastReason
        || (landingBlocked ? `落地页被反爬拦截(${landingBlocked})` : "无任何可下载源(无 OA 版本,Unpaywall/Crossref 均无 PDF 链接)");
      const link = /^\d{4}\.\d{4,5}$/.test(d) ? `https://arxiv.org/abs/${d}` : `https://doi.org/${d}`;
      rec.error = `${cause} —— 请用浏览器打开 ${link},在校园网/机构网络下下载 PDF,再用 kb_ingest 入库`;
    }
  } catch (e) {
    rec.error = String(e?.message || e);
  }
  results.push(rec);
  console.log(JSON.stringify(rec));
}

const ok = results.filter(r => r.ok).length;
console.log(`\n[doi_pdf] ${ok}/${results.length} 下载成功,输出目录: ${outDir}`);
process.exit(ok === results.length ? 0 : 2);
