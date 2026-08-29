#!/usr/bin/env node
// dsh-kb-rag — one-click installer CLI (npx-ready, cross-platform)
// 一条链：Python 依赖 → 引擎冒烟测试 → Node/pnpm → dsh 插件安装激活 → (可选)模型预下载。
// 用法：npx dsh-kb-rag-install [--profile <name>] [--mirror <pip镜像>] [--models] [--with-docx]
//                              [--dry-run] [--skip-pip] [--skip-node] [--skip-dsh] [-y]
// 环境变量：HF_ENDPOINT（模型镜像）、KB_EMBED_MODEL、KB_RERANK_MODEL、PIP_INDEX_URL。
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import readline from "node:readline";

const PKG_ROOT = dirname(fileURLToPath(new URL("..", import.meta.url)));
const ENGINE = join(PKG_ROOT, "kb_engine.py");
const IS_WIN = process.platform === "win32";
const TTY = process.stdout.isTTY === true;
const c = { bold: "", dim: "", green: "", yellow: "", red: "", cyan: "", reset: "" };
if (TTY) c.bold = "\x1b[1m", c.dim = "\x1b[2m", c.green = "\x1b[32m", c.yellow = "\x1b[33m", c.red = "\x1b[31m", c.cyan = "\x1b[36m", c.reset = "\x1b[0m";

const ok = (m) => console.log(`${c.green}[OK]${c.reset} ${m}`);
const warn = (m) => console.log(`${c.yellow}[!!]${c.reset} ${m}`);
const fail = (m) => console.log(`${c.red}[XX]${c.reset} ${m}`);
const step = (m) => console.log(`\n${c.bold}${c.cyan}== ${m}${c.reset}`);
const die = (m) => { fail(m); process.exit(1); };

// cmd.exe 上的 npm/pnpm/dsh 是 .cmd 脚本，Node 18.20+ 不允许无 shell 直接执行；
// 走 shell:true 时自行加引号，且所有进入命令串的参数都先经过校验/白名单。
function shellRun(cmd, args, opts) {
  if (!IS_WIN) return spawnSync(cmd, args, { stdio: "inherit", ...opts });
  const quote = (a) => `"${String(a).replace(/"/g, '""')}"`;
  const line = [cmd, ...args].map(quote).join(" ");
  return spawnSync(line, { stdio: "inherit", shell: true, ...opts });
}
function shellOut(cmd, args, opts) {
  if (!IS_WIN) return spawnSync(cmd, args, { encoding: "utf8", ...opts });
  const quote = (a) => `"${String(a).replace(/"/g, '""')}"`;
  const line = [cmd, ...args].map(quote).join(" ");
  return spawnSync(line, { encoding: "utf8", shell: true, ...opts });
}
function which(cmd) {
  const probe = IS_WIN ? shellOut("where", [cmd]) : spawnSync("command", ["-v", cmd], { encoding: "utf8", shell: true });
  return probe.status === 0 && String(probe.stdout || "").trim().length > 0;
}
function ask(text) {
  return new Promise((resolve) => {
    const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
    rl.question(text, (a) => { rl.close(); resolve(String(a).trim()); });
  });
}

// ---------------------------------------------------------------- args

const opts = { profile: "", mirror: process.env.PIP_INDEX_URL || "", models: false, withDocx: false, dryRun: false, skipPip: false, skipNode: false, skipDsh: false, yes: false };
{
  const argv = process.argv.slice(2);
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const next = () => (i + 1 < argv.length ? argv[++i] : die(`option ${a} needs a value`));
    if (a === "--profile" || a === "-p") opts.profile = next();
    else if (a === "--mirror" || a === "-i") opts.mirror = next();
    else if (a === "--models") opts.models = true;
    else if (a === "--with-docx") opts.withDocx = true;
    else if (a === "--dry-run") opts.dryRun = true;
    else if (a === "--skip-pip") opts.skipPip = true;
    else if (a === "--skip-node") opts.skipNode = true;
    else if (a === "--skip-dsh") opts.skipDsh = true;
    else if (a === "-y" || a === "--yes") opts.yes = true;
    else if (a === "-h" || a === "--help") {
      console.log("usage: npx dsh-kb-rag-install [--profile <name>] [--mirror <pip-mirror>] [--models] [--with-docx] [--dry-run] [--skip-pip] [--skip-node] [--skip-dsh] [-y]");
      console.log("env:    HF_ENDPOINT / KB_EMBED_MODEL / KB_RERANK_MODEL / PIP_INDEX_URL");
      process.exit(0);
    } else if (!a.startsWith("-") && opts.profile === "") {
      opts.profile = a; // 容忍位置参数：npx dsh-kb-rag-install myprofile
    } else {
      warn(`忽略无法识别的参数: ${a}`);
    }
  }
  if (opts.profile && !/^[A-Za-z0-9][A-Za-z0-9 ._-]*$/.test(opts.profile)) {
    die(`profile 名含命令行特殊字符，已拒绝: ${opts.profile}`);
  }
}

console.log(`${c.bold}  dsh-kb-rag · one-click installer${c.reset}`);
if (opts.dryRun) warn("dry-run: 只打印将执行的动作，不安装");

// ---------------------------------------------------------------- 1/5 python

step("1/5 定位 Python (>= 3.9)");
let PY = "";
for (const cand of IS_WIN ? ["python", "py", "python3"] : ["python3", "python"]) {
  const r = spawnSync(cand, ["-c", "import sys; print('%d.%d' % sys.version_info[:2])"], { encoding: "utf8" });
  if (r.status !== 0) {
    if (IS_WIN && cand !== "python") warn(`${cand} 存在但无法运行（可能是 Windows Store 存根），跳过`);
    continue;
  }
  const v = String(r.stdout || "").trim();
  const [major, minor] = v.split(".").map(Number);
  if (major > 3 || (major === 3 && minor >= 9)) { PY = cand; ok(`found: ${cand} (${v})`); break; }
  warn(`${cand} is ${v} (< 3.9), skipped`);
}
if (!PY) die("未找到 Python >= 3.9。请安装后重跑：https://www.python.org/downloads/");

// ---------------------------------------------------------------- 2/5 python deps

step("2/5 检查 Python 依赖");
const PROBE_CODE = "import importlib.util, json; print(json.dumps([p for m, p in "
  + "(('fitz','pymupdf'),('faiss','faiss-cpu'),('sentence_transformers','sentence-transformers'),('torch','torch')"
  + (opts.withDocx ? ",('docx','python-docx')" : "")
  + ") if importlib.util.find_spec(m) is None]))";
function probeMissing() {
  const r = spawnSync(PY, ["-c", PROBE_CODE], { encoding: "utf8" });
  if (r.status !== 0) return null;
  try { return JSON.parse(String(r.stdout || "").trim().split("\n").pop()); } catch (e) { return null; }
}
if (opts.skipPip) {
  warn("--skip-pip：跳过依赖安装");
} else {
  if (!opts.withDocx) {
    const r = spawnSync(PY, ["-c", "import importlib.util; print('ok' if importlib.util.find_spec('docx') else 'missing')"], { encoding: "utf8" });
    if (String(r.stdout || "").trim() === "missing") warn("python-docx 未装（可选，DOCX 原生解析；缺失时引擎自动回退 zip+regex）。加 --with-docx 一并安装");
  }
  const missing = probeMissing();
  if (missing === null) {
    warn("依赖探测失败（Python 环境异常？），跳过安装步骤");
  } else if (missing.length === 0) {
    ok("核心依赖齐全 (PyMuPDF / numpy / faiss / sentence-transformers / torch)");
  } else {
    warn(`缺失: ${missing.join(" ")}`);
    const args = ["-m", "pip", "install", "--disable-pip-version-check"];
    if (opts.mirror) args.push("-i", opts.mirror);
    args.push(...missing);
    console.log(`${c.dim}  ${PY} ${args.join(" ")}${c.reset}`);
    if (opts.dryRun) warn("dry-run：未执行");
    else {
      const r = spawnSync(PY, args, { stdio: "inherit" });
      if (r.status === 0) ok("依赖安装完成");
      else {
        warn("pip 全局安装失败，回退 --user 重试…");
        const r2 = spawnSync(PY, [...args.slice(0, -missing.length), "--user", ...missing], { stdio: "inherit" });
        if (r2.status === 0) ok("依赖已装到用户目录 (--user)");
        else die("pip 安装失败。可换镜像重试：npx dsh-kb-rag-install --mirror https://pypi.tuna.tsinghua.edu.cn/simple");
      }
    }
  }
}

// ---------------------------------------------------------------- 3/5 engine smoke test

step("3/5 引擎冒烟测试 (kb_engine.py stats)");
if (opts.dryRun) {
  warn("dry-run：跳过");
} else {
  const tmp = mkdtempSync(join(tmpdir(), "kbrag-smoke-"));
  const req = JSON.stringify({ kb_root: join(tmp, "kb").replace(/\\/g, "/") });
  const r = spawnSync(PY, [ENGINE, "stats"], { input: req + "\n", encoding: "utf8", maxBuffer: 8 * 1024 * 1024 });
  let parsed = null;
  try { parsed = JSON.parse(String(r.stdout || "").trim()); } catch (e) { /* fallthrough */ }
  if (parsed && parsed.ok === true) ok(`engine v${parsed.engine || "?"} 自检通过`);
  else die(`引擎冒烟测试失败: ${String(r.stderr || r.stdout || "").slice(0, 400)}`);
  rmSync(tmp, { recursive: true, force: true });
}

// ---------------------------------------------------------------- 4/5 node / pnpm

step("4/5 检查 Node.js / pnpm");
if (opts.skipNode) {
  warn("--skip-node：跳过");
} else {
  ok(`node ${process.version}`);
  if (which("pnpm")) {
    const pv = shellOut("pnpm", ["--version"]);
    ok(`pnpm ${String(pv.stdout || "").trim()}`);
  } else if (which("npm")) {
    warn("pnpm 未找到（DSH 官方插件流程需要 pnpm）");
    let go = opts.yes;
    if (!go && !opts.dryRun) go = (await ask("  现在自动安装 pnpm？[y/N] ")).match(/^(y|Y|yes|YES)$/) !== null;
    if (opts.dryRun) warn("dry-run：将执行 npm install -g pnpm");
    else if (go) {
      const r = shellRun("npm", ["install", "-g", "pnpm"]);
      if (r.status === 0 && which("pnpm")) ok(`pnpm ${String(shellOut("pnpm", ["--version"]).stdout || "").trim()} 已安装`);
      else warn("pnpm 安装失败，请手动：npm install -g pnpm");
    } else {
      warn("跳过（可稍后手动：npm install -g pnpm）");
    }
  } else {
    warn("npm 未找到，无法自动装 pnpm；请手动：npm install -g pnpm");
  }
}

// ---------------------------------------------------------------- 5/5 dsh plugin add

step("5/5 安装并激活 DSH 插件 (dsh plugin add)");
if (opts.skipDsh) {
  warn("--skip-dsh：跳过");
} else if (!which("dsh")) {
  warn("dsh CLI 未找到。插件市场路线：安装 dsh-plugin-registry 后在设置面板一键安装；或先安装 DSH 再重跑本命令");
} else {
  const withProfile = opts.profile !== "";
  if (!withProfile) warn("未指定 --profile，先尝试默认部署目录");
  const args = withProfile ? ["plugin", "--profile", opts.profile, "add", "dsh-kb-rag"] : ["plugin", "add", "dsh-kb-rag"];
  console.log(`${c.dim}  dsh ${args.join(" ")}${c.reset}`);
  if (opts.dryRun) warn("dry-run：未执行");
  else {
    const r = shellRun("dsh", args);
    if (r.status === 0) ok("插件已安装并自动激活（dsh.bundle 声明）");
    else if (!withProfile) warn("默认部署失败。请带 profile 重跑：npx dsh-kb-rag-install --profile <name>");
    else warn("dsh 安装失败——请检查上方输出（网络 / pnpm / profile 名）");
  }
}

// ---------------------------------------------------------------- optional model pre-download

if (opts.models) {
  const embed = process.env.KB_EMBED_MODEL || "BAAI/bge-small-zh-v1.5";
  const rerank = process.env.KB_RERANK_MODEL || "BAAI/bge-reranker-base";
  step(`可选：预下载模型 (${embed} / ${rerank})`);
  if (opts.dryRun) {
    warn("dry-run：跳过");
  } else {
    if (process.env.HF_ENDPOINT) console.log(`${c.dim}  HF_ENDPOINT=${process.env.HF_ENDPOINT}${c.reset}`);
    const code = [
      "import os, sys",
      "os.environ.setdefault('HF_HUB_DOWNLOAD_TIMEOUT', '60')",
      "try:",
      "    from sentence_transformers import SentenceTransformer, CrossEncoder",
      "    SentenceTransformer(sys.argv[1]); print('[ok] embed model ready')",
      "    CrossEncoder(sys.argv[2]); print('[ok] reranker ready')",
      "except Exception as e:",
      "    print('[fail] %s: %s' % (type(e).__name__, e)); sys.exit(1)",
    ].join("\n");
    const r = spawnSync(PY, ["-c", code, embed, rerank], { stdio: "inherit" });
    if (r.status === 0) ok("模型已就绪");
    else warn("模型下载失败——不影响安装；首次检索时会自动重试（受限网络先设 HF_ENDPOINT=https://hf-mirror.com）");
  }
}

// ---------------------------------------------------------------- summary

console.log(`\n${c.bold}──────────────────────────────────────────────`);
console.log(opts.dryRun ? `安装完成${c.reset}${c.yellow}（dry-run 演练，未做任何变更）${c.reset}` : `安装完成${c.reset}`);
console.log(`
下一步：
  1. 重启 DSH，打开一个新会话 —— 8 个 kb_* 工具自动注册
  2. 首次检索会弹出查询范围选择（封闭库 / 库+全网 / 仅全网）
  3. 入库：对话里说"把 <文献目录> 入库"（kb_ingest）或"同步 Zotero"（kb_zotero）
  4. 提问："BiFeO3 畴壁导电机制是什么？"（kb_rag，自动带 DOI 引用）

受限网络提示：pip 加速   npx dsh-kb-rag-install --mirror https://pypi.tuna.tsinghua.edu.cn/simple
              模型镜像   先 set HF_ENDPOINT=https://hf-mirror.com（macOS/Linux: export）`);
process.exit(0);
