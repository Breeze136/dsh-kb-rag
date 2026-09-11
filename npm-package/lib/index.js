// dsh-kb-rag — static DSH plugin (Host half)
// 本地文献知识库 RAG：10 个模型工具 + 常驻 Python 引擎（随包分发 kb_engine.py）。
// 加载：部署的 cordis 组合中加入本包（cordis-plugin-loader 按 npm 包名解析）。
import { defineTool } from "@deepseek-ai/dsh-tools";
import { fileURLToPath } from "node:url";

const name = "kb-rag";
const inject = ["tools", "timer"];

const ENGINE_DIR = fileURLToPath(new URL("..", import.meta.url)); // package root
const ENGINE_PATH = fileURLToPath(new URL("../kb_engine.py", import.meta.url));

const SCOPE_NOTE = {
  kb: "范围：封闭知识库。仅基于库内文献作答；如需开放网络检索，用 kb_scope 切换范围。",
  both: "范围：知识库+全网。除本库内结果外，请再调用 web_search 检索开放网络，合并作答并分别标注来源。",
  web: "范围：仅全网。本次仅给出库内命中供参考；请以 web_search 结果为准作答。",
};
const STRICT_NOTE = '严格模式：答案仅允许基于本次检索返回的 evidence/results 内容；禁止补充库外知识、常识外延或未出现在证据中的文献与数据；证据不足时直接说明"根据现有资料无法回答"。';

function apply(ctx) {
  const subprocess = ctx.get("subprocess");
  if (subprocess === undefined) {
    console.error("[kb-rag] subprocess service unavailable; tools not registered");
    return;
  }

  let daemon = null;
  let spawning = null;
  let scopePref = "kb";
  let scopeDepth = "deep";
  let scopeStrict = false;
  let scopeAsked = false;
  let netEnv = "unknown";
  let netAsked = false;
  const userQuestions = ctx.get("userQuestions");

  // ---- 下载前网络环境探测:代理检测(env 变量;本机代理端口由引擎探测) ----
  function envProxyDetect() {
    const keys = ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"];
    const set = [];
    try {
      keys.forEach(function (k) { const v = process.env[k]; if (v) set.push(k + "=" + v); });
    } catch (e) { /* process 不可用时忽略 */ }
    return set;
  }

  // 下载前询问网络环境(校园网可下订阅版;家庭网络以 OA 为主),非阻塞,仅首次
  function askNetworkOnce(agent) {
    if (netAsked || userQuestions === undefined) return;
    netAsked = true;
    const request = {
      questions: [{
        id: "kb-net",
        header: "下载网络环境",
        question: "kb_fetch 下载前确认：当前网络环境？(付费墙期刊的订阅版 PDF 只有校园网/机构 IP 才能直接下)",
        options: [
          { label: "校园网/机构网络", description: "可下出版商订阅版 PDF，将优先尝试出版商正式版" },
          { label: "家庭网络", description: "以 OA 开放获取为主，付费墙文献会提示手动下载" },
          { label: "不确定", description: "两者都试：先出版商正式版，失败自动转 OA" },
        ],
      }],
    };
    if (agent !== undefined) request.agent = agent;
    Promise.race([
      userQuestions.ask(request).then(function (answer) {
        const picked = answer && answer.answers && answer.answers[0] && answer.answers[0].selected && answer.answers[0].selected[0];
        if (typeof picked === "string") {
          if (picked.indexOf("校园网") === 0) netEnv = "campus";
          else if (picked.indexOf("家庭") === 0) netEnv = "home";
          else netEnv = "unknown";
        }
        console.log("[kb-rag] download network env:", netEnv);
      }).catch(function (e) {
        console.error("[kb-rag] network question failed:", String(e));
      }),
      ctx.timeout(120000),
    ]);
  }

  // 入库数据版本：先问引擎要"旧解析器入库"的文档数（stats.stale_docs）；
  // 取不到（旧引擎/无库/调用失败）就静默跳过这条问题，绝不影响首次工具调用。
  function staleCountOf(kbRoot, exec) {
    return runEngine("stats", { kb_root: kbRoot }, exec).then(function (resp) {
      const n = resp ? Number(resp.stale_docs) : 0;
      return Number.isFinite(n) && n > 0 ? n : 0;
    }).catch(function (e) {
      console.error("[kb-rag] stale check skipped:", String(e));
      return 0;
    });
  }

  // 刷新库内旧数据：后台维护动作（不是工具调用，不渲染给模型），失败只写宿主日志。
  function refreshStale(kbRoot, exec, metaOnly) {
    const payload = metaOnly
      ? { kb_root: kbRoot, rebuild: true, metadata_only: true }
      : { kb_root: kbRoot, rebuild: true, async_if_large: true };
    runEngine("ingest", payload, exec).then(function (resp) {
      const jobId = resp && resp.job_id ? String(resp.job_id) : "";
      if (jobId) {
        console.log("[kb-rag] 全量重灌已转后台：job_id=" + jobId + "；用 kb_status(job_id=\"" + jobId + "\") 轮询进度，宿主调用超时不会中断后台任务");
        return;
      }
      const totals = (resp && resp.totals) || {};
      if (metaOnly) {
        console.log("[kb-rag] 元数据刷新完成：meta_updated=" + (totals.meta_updated || 0) + " / 失败 " + (totals.errors || 0));
      } else {
        console.log("[kb-rag] 重灌完成（引擎未转后台）：新增 " + (totals.added || 0) + " / 更新 " + (totals.updated || 0) + " / 失败 " + (totals.errors || 0));
      }
    }).catch(function (e) {
      console.error("[kb-rag] stale refresh failed:", String(e));
    });
  }

  function askScopeOnce(agent, exec, kbRoot) {
    if (scopeAsked || userQuestions === undefined) return;
    scopeAsked = true;
    const root = typeof kbRoot === "string" && kbRoot.length > 0 ? kbRoot : workspaceOf(exec) + "/.kb";
    staleCountOf(root, exec).then(function (stale) {
      const request = {
        questions: [{
          id: "kb-scope",
          header: "查询范围",
          question: "知识库查询的默认范围？",
          options: [
            { label: "仅封闭知识库（推荐）", description: "只检索本地文献库，结论只来自库内文献" },
            { label: "知识库+全网", description: "库内检索为主，开放网络（web_search）补充" },
            { label: "仅全网", description: "只用开放网络检索，不用知识库" },
          ],
        }, {
          id: "kb-depth",
          header: "检索深度",
          question: "检索与作答的深度？",
          options: [
            { label: "快速检索", description: "混合召回直出，跳过精排与引文扩展，亚秒级响应，适合事实性查询与单点数据检索" },
            { label: "深度检索（推荐）", description: "重排序 + 引文关联 + 相关文献全链路，跨文献综合论述，适合领域调研与综述性问题" },
          ],
        }],
      };
      if (stale > 0) {
        request.questions.push({
          id: "kb-stale",
          header: "入库数据版本",
          question: "库内有 " + stale + " 篇文档是用旧版解析器入库的（引擎的解析改进不会自动作用于已有数据）。是否刷新？",
          options: [
            { label: "暂不处理", description: "保持现状，随时可用 kb_ingest 的 metadata_only/rebuild 手动刷新" },
            { label: "只刷新元数据（推荐）", description: "秒级完成，仅重抽标题/作者/DOI，不重切块、不重嵌入" },
            { label: "全量重灌（较慢）", description: "重新解析并重新嵌入全部文档，期间会转后台，可用 kb_status 查进度" },
          ],
        });
      }
      if (agent !== undefined) request.agent = agent;
      return Promise.race([
        userQuestions.ask(request).then(function (answer) {
          const picked = answer && answer.answers && answer.answers[0] && answer.answers[0].selected && answer.answers[0].selected[0];
          if (typeof picked === "string" && picked.indexOf("仅封闭") === 0) scopePref = "kb";
          else if (typeof picked === "string" && picked.indexOf("知识库+全网") === 0) scopePref = "both";
          else if (typeof picked === "string" && picked.indexOf("仅全网") === 0) scopePref = "web";
          const pickedDepth = answer && answer.answers && answer.answers[1] && answer.answers[1].selected && answer.answers[1].selected[0];
          if (typeof pickedDepth === "string") {
              if (pickedDepth.indexOf("深度检索") === 0) scopeDepth = "deep"
              else if (pickedDepth.indexOf("快速检索") === 0) scopeDepth = "quick"
            };
          const pickedStale = answer && answer.answers && answer.answers[2] && answer.answers[2].selected && answer.answers[2].selected[0];
          if (typeof pickedStale === "string" && pickedStale.indexOf("只刷新元数据") === 0) refreshStale(root, exec, true);
          else if (typeof pickedStale === "string" && pickedStale.indexOf("全量重灌") === 0) refreshStale(root, exec, false);
          else if (typeof pickedStale === "string") console.log("[kb-rag] 旧数据暂不刷新（需要时用 kb_ingest 的 metadata_only / rebuild）");
          console.log("[kb-rag] query scope:", scopePref, "depth:", scopeDepth);
        }).catch(function (e) {
          console.error("[kb-rag] scope question failed:", String(e));
        }),
        ctx.timeout(120000),
      ]);
    }).catch(function (e) {
      console.error("[kb-rag] scope question failed:", String(e));
    });
  }

  function scopeWrapped(exec, engineCall, strict, kbRoot) {
    askScopeOnce(exec && exec.agent, exec, kbRoot);
    return engineCall.then(function (resp) {
      resp.scope = scopePref;
      resp.scope_note = SCOPE_NOTE[scopePref];
      resp.depth_note = scopeDepth === "quick" ? "快速检索" : "深度检索";
      resp.strict = strict === true;
      if (strict === true) resp.strict_note = STRICT_NOTE;
      return resp;
    });
  }

  function workspaceOf(exec) {
    try {
      const cwd = exec && exec.agent && exec.agent.session && exec.agent.session.header ? exec.agent.session.header.cwd : undefined;
      if (typeof cwd === "string" && cwd.length > 0) return cwd;
    } catch (e) { /* fall through */ }
    const sandboxPolicy = ctx.get("sandboxPolicy");
    if (sandboxPolicy !== undefined && typeof sandboxPolicy.workspaceRoot === "string" && sandboxPolicy.workspaceRoot.length > 0) {
      return sandboxPolicy.workspaceRoot;
    }
    return ENGINE_DIR;
  }

  const sleep = (ms) => ctx.timeout(ms);

  async function spawnDaemon(root, exec) {
    let python = "python";
    try {
      python = await subprocess.resolveExecutable("python", undefined, exec.signal);
    } catch (e) {
      console.error("[kb-rag] resolveExecutable python failed, using bare name:", String(e));
    }
    const handle = subprocess.spawn({
      argv: [python, ENGINE_PATH, "serve"],
      cwd: root,
      stdio: {
        stdin: "pipe",
        stdout: { maxBytes: 32 * 1024 * 1024, spill: { maxBytes: 128 * 1024 * 1024 } },
        stderr: { maxBytes: 2 * 1024 * 1024 },
      },
      graceMs: 5000,
    });
    const d = { root, handle, offset: 0, queue: Promise.resolve(), seq: 0, dead: false };
    handle.done.then(function (out) {
      d.dead = true;
      if (daemon !== d) return;
      if (out.exitCode !== 0) {
        const err = handle.collected.stderr !== undefined ? handle.collected.stderr.readFrom(0).text : "";
        console.error("[kb-rag] engine daemon exited", out.exitCode, String(err).slice(0, 300));
      }
    });
    return d;
  }

  async function perform(d, command, payload, exec) {
    if (d.dead) throw new Error("kb engine daemon is down; retry the call");
    const id = ++d.seq;
    try {
      d.handle.stdin.write(JSON.stringify({ id, command, payload }) + "\n");
    } catch (e) {
      d.dead = true;
      throw new Error("kb engine daemon write failed: " + String(e && e.message || e));
    }
    const longCommand = command === "ingest" || command === "zotero";
    const deadline = Date.now() + (longCommand ? 1800000 : 150000);
    while (true) {
      if (d.handle.collected.stdout === undefined) throw new Error("kb engine daemon has no stdout reader");
      const read = d.handle.collected.stdout.readFrom(d.offset);
      d.offset = read.nextOffset;
      if (read.text.length > 0) {
        for (const line of read.text.split("\n")) {
          const t = line.trim();
          if (t.length === 0) continue;
          let parsed = null;
          try { parsed = JSON.parse(t); } catch (e) { parsed = null; }
          if (parsed === null || parsed.id !== id) continue;
          if (parsed.ok !== true) throw new Error("kb engine error: " + String(parsed.error || "unknown").slice(0, 800));
          const resp = parsed.response;
          if (resp === undefined || resp === null || resp.ok !== true) {
            throw new Error("kb engine error: " + String((resp && resp.error) || "unknown").slice(0, 800));
          }
          return resp;
        }
        continue;
      }
      if (read.lossy) throw new Error("kb engine output truncated" + (read.spillPath ? " (spill " + read.spillPath + ")" : ""));
      if (d.dead) throw new Error("kb engine daemon exited before answering");
      if (exec && exec.signal && exec.signal.aborted) throw new Error("tool call aborted");
      if (Date.now() > deadline) throw new Error("kb engine daemon timed out");
      await sleep(10);
    }
  }

  async function runEngine(command, payload, exec) {
    await depsGate; // 等 startup 探测 / KB_AUTO_PIP 自动安装结束（一次性，后续调用零开销）
    if (Array.isArray(depStatus.missing) && depStatus.missing.length > 0) {
      throw new Error("kb-rag 缺少 Python 依赖: " + depStatus.missing.join(", ")
        + "。修复方式（任选其一）：① 在宿主终端执行 python -m pip install " + depStatus.missing.join(" ")
        + "；② 设置环境变量 KB_AUTO_PIP=1 后重启 DSH，插件将自动安装；③ 运行一键安装 npx --yes --package dsh-kb-rag -c 'dsh-kb-rag-install'（或随包脚本 scripts/install.sh / scripts/install.ps1）。");
    }
    const root = workspaceOf(exec);
    if (daemon !== null && daemon.root !== root) {
      const old = daemon;
      daemon = null;
      try { old.handle.terminate(); } catch (e) { /* ignore */ }
    }
    if (daemon === null || daemon.dead) {
      if (spawning === null) {
        spawning = spawnDaemon(root, exec).then(function (d) { daemon = d; spawning = null; return d; }, function (e) { spawning = null; throw e; });
      }
      await spawning;
    }
    const d = daemon;
    const call = d.queue.then(function () { return perform(d, command, payload, exec); });
    d.queue = call.then(function () {}, function () {});
    return call;
  }

  ctx.effect(() => () => {
    if (daemon !== null) {
      try { daemon.handle.terminate(); } catch (e) { /* ignore */ }
      daemon = null;
    }
  });

  // 启动时自动检测 Python 依赖（pymupdf / faiss-cpu / sentence-transformers / torch）。
  // 探测用 importlib.util.find_spec 一次性拿完整缺失清单（裸 import 链会在首个缺失处中断，只能看到一个）。
  // 默认只在宿主日志打印安装命令；设置环境变量 KB_AUTO_PIP=1 时自动执行 pip 安装（固定 argv，不进 shell）。
  // depsGate：首次工具调用先等探测/自动安装结束；确认缺失时直接返回可操作的错误，而不是让引擎子进程反复崩。
  const depStatus = { probed: false, missing: null };
  let depsGateResolve = null;
  const depsGate = new Promise(function (resolve) { depsGateResolve = resolve; });
  const autoPipEnabled = function () {
    try { return typeof process !== "undefined" && process.env && process.env.KB_AUTO_PIP === "1"; }
    catch (e) { return false; }
  };

  const DEP_PROBE_CODE = "import importlib.util, json; print(json.dumps([p for m, p in "
    + "(('fitz','pymupdf'),('faiss','faiss-cpu'),('sentence_transformers','sentence-transformers'),('torch','torch'))"
    + " if importlib.util.find_spec(m) is None]))";

  function readCollected(handle) {
    const out = { stdout: "", stderr: "" };
    try { if (handle.collected.stdout !== undefined) out.stdout = handle.collected.stdout.readFrom(0).text; } catch (e) { /* ignore */ }
    try { if (handle.collected.stderr !== undefined) out.stderr = handle.collected.stderr.readFrom(0).text; } catch (e) { /* ignore */ }
    return out;
  }

  async function resolvePython() {
    try {
      const resolved = await subprocess.resolveExecutable("python");
      if (typeof resolved === "string" && resolved.length > 0) return resolved;
    } catch (e) {
      console.error("[kb-rag] resolveExecutable python failed, using bare name:", String(e));
    }
    return "python";
  }

  async function probeMissing(python) {
    let handle;
    try {
      handle = subprocess.spawn({
        argv: [python, "-c", DEP_PROBE_CODE],
        cwd: ENGINE_DIR,
        stdio: { stdin: "ignore", stdout: { maxBytes: 65536 }, stderr: { maxBytes: 16384 } },
        graceMs: 5000,
      });
    } catch (e) {
      console.error("[kb-rag] dependency probe failed to spawn:", String(e && e.message || e));
      return null;
    }
    const out = await handle.done;
    if (out.exitCode !== 0) return null;
    try {
      return JSON.parse(readCollected(handle).stdout.trim().split("\n").pop());
    } catch (e) {
      return null;
    }
  }

  async function ensurePythonDeps() {
    const python = await resolvePython();
    const missing = await probeMissing(python);
    depStatus.probed = true;
    if (missing === null) {
      console.error("[kb-rag] dependency probe inconclusive; proceeding without gate");
      return;
    }
    depStatus.missing = missing;
    if (missing.length === 0) {
      console.log("[kb-rag] Python dependencies OK");
      return;
    }
    const installCmd = python + " -m pip install --disable-pip-version-check " + missing.join(" ");
    if (!autoPipEnabled()) {
      console.error("[kb-rag] Python dependencies missing: " + missing.join(", "));
      console.error("[kb-rag] Install with: " + installCmd);
      console.error("[kb-rag] (or set KB_AUTO_PIP=1 and restart DSH to let the plugin install them)");
      return;
    }
    console.log("[kb-rag] KB_AUTO_PIP=1 — auto-installing: " + missing.join(", "));
    let handle;
    try {
      handle = subprocess.spawn({
        argv: [python, "-m", "pip", "install", "--disable-pip-version-check"].concat(missing),
        cwd: ENGINE_DIR,
        stdio: { stdin: "ignore", stdout: { maxBytes: 1024 * 1024 }, stderr: { maxBytes: 1024 * 1024 } },
        graceMs: 1800000,
      });
    } catch (e) {
      console.error("[kb-rag] auto pip install failed to spawn:", String(e && e.message || e));
      return;
    }
    const out = await handle.done;
    if (out.exitCode !== 0) {
      const err = readCollected(handle).stderr;
      console.error("[kb-rag] auto pip install failed (exit " + out.exitCode + "): " + String(err).trim().slice(-500));
      return;
    }
    const still = await probeMissing(python);
    depStatus.missing = Array.isArray(still) ? still : [];
    if (depStatus.missing.length === 0) console.log("[kb-rag] auto pip install OK — dependencies ready");
    else console.error("[kb-rag] still missing after install: " + depStatus.missing.join(", ") + " — manual: " + installCmd);
  }

  ensurePythonDeps().catch(function (e) {
    console.error("[kb-rag] dependency check error:", String(e));
  }).then(function () {
    if (depsGateResolve !== null) depsGateResolve();
  });


  const kbRootOf = (args, exec) => typeof args.kb_root === "string" && args.kb_root.length > 0 ? args.kb_root : workspaceOf(exec) + "/.kb";
  const renderJson = (_args, value) => [{ type: "text", text: JSON.stringify(value) }];

  // 大批量入库：引擎已转后台，返回的是 job 句柄而不是入库结果——不能按入库结果渲染。
  const renderIngestAsync = (_args, value) => {
    if (value === null || typeof value !== "object") return [{ type: "text", text: String(value) }];
    const jobId = value.job_id ? String(value.job_id) : "";
    const lines = [];
    lines.push("**已转入后台处理**" + (jobId.length > 0 ? " · job_id " + jobId : ""));
    if (typeof value.pending_files === "number") lines.push("待处理文件 " + value.pending_files + " 篇");
    if (value.note) lines.push(String(value.note));
    // 引擎的 note 通常已经带了 kb_status 指引，此时不要再重复一遍
    const noteHasHint = typeof value.note === "string" && value.note.indexOf("kb_status") >= 0;
    if (!noteHasHint) {
      lines.push("");
      lines.push("用 kb_status(job_id=\"" + jobId + "\") 轮询进度；宿主调用超时不会中断后台任务，结果在完成时返回 totals。");
    }
    return [{ type: "text", text: lines.join("\n") }];
  };

  // 元数据刷新（metadata_only）：只更新 docs 的元数据字段，totals 里是 meta_updated 系列，
  // 没有 added/updated/chunks/vectors——按入库结果渲染会显示成"什么都没做"，必须单独渲染。
  const renderMetaRefresh = (_args, value) => {
    const totals = value.totals || {};
    const files = Array.isArray(value.files) ? value.files : [];
    const lines = [];
    lines.push("**元数据刷新完成** · 已刷新 " + (totals.meta_updated || 0) + " 篇"
      + "（其中 " + (totals.meta_changed || 0) + " 篇内容有变化）");
    const extra = [];
    if (totals.changed) extra.push("跳过（文件内容已变，需正常入库）" + totals.changed + " 篇");
    if (totals.not_indexed) extra.push("未入库 " + totals.not_indexed + " 篇");
    if (totals.errors) extra.push("失败 " + totals.errors + " 篇");
    if (extra.length > 0) lines.push(extra.join(" · "));
    const totalMs = typeof value.ms === "number" ? value.ms : 0;
    lines.push("总耗时 " + (totalMs >= 1000 ? (totalMs / 1000).toFixed(1) + "s" : totalMs + "ms")
      + " · 未重切块、未重嵌入");
    if (files.length > 0) {
      lines.push("");
      lines.push("**最近刷新（滚动）**");
      files.slice(-8).forEach(function (f) {
        const nm = String(f.path || "").split(/[\\/]/).pop();
        const bits = [f.status || ""];
        if (f.changed === true) bits.push("有变化");
        if (f.doi) bits.push("doi " + String(f.doi));
        if (typeof f.ms === "number") bits.push(f.ms + "ms");
        lines.push("· " + nm + " · " + bits.filter(Boolean).join(" · "));
      });
    }
    return [{ type: "text", text: lines.join("\n") }];
  };

  // 入库/Zotero 迁移：紧凑滚动视图——总览一行 + 最近 N 条（文件名 + 耗时），不甩大 JSON。
  const renderIngest = (_args, value) => {
    if (value === null || typeof value !== "object") return [{ type: "text", text: String(value) }];
    if (value.background === true || (value.job_id !== undefined && value.status === "running" && value.totals === undefined)) {
      return renderIngestAsync(_args, value);
    }
    if (value.mode === "metadata_only") return renderMetaRefresh(_args, value);
    // Zotero 预演（dry_run）：引擎返回 candidates + 全部候选清单，totals 全 0——
    // 按入库结果渲染会显示成"入库完成 新增 0…"，看起来像什么都没干。
    if (value.dry_run === true) {
      const cands = Array.isArray(value.files) ? value.files : [];
      const n = typeof value.candidates === "number" ? value.candidates : cands.length;
      const dry = [];
      dry.push("**Zotero 预演**（dry_run，未写入库）· 候选 " + n + " 篇");
      if (value.zotero_db) dry.push("zotero.sqlite：" + String(value.zotero_db));
      const missing = cands.filter(function (f) { return f.status === "missing"; }).length;
      if (missing > 0) dry.push("附件缺失（正常跳过）" + missing + " 篇");
      if (cands.length > 0) {
        dry.push("");
        dry.push("**候选（前 8 条）**");
        cands.slice(0, 8).forEach(function (f) {
          const nm = String(f.path || "").split(/[\\/]/).pop();
          const bits = [f.year ? String(f.year) : null, f.status || null].filter(Boolean).join(" · ");
          dry.push("· " + nm + (bits.length > 0 ? " · " + bits : ""));
        });
        if (n > cands.length) dry.push("…另 " + (n - cands.length) + " 篇");
      }
      dry.push("");
      dry.push("去掉 dry_run 即执行真实迁移；大批量会自动转后台，用 kb_status 轮询。");
      return [{ type: "text", text: dry.join("\n") }];
    }
    const totals = value.totals || {};
    const files = Array.isArray(value.files) ? value.files : [];
    const lines = [];
    lines.push("**入库完成** · 新增 " + (totals.added || 0) + " / 更新 " + (totals.updated || 0)
      + " / 跳过 " + (totals.skipped || 0) + " / 重复 " + (totals.duplicates || 0)
      + " / 失败 " + (totals.errors || 0));
    const totalMs = typeof value.ms === "number" ? value.ms : 0;
    lines.push("总耗时 " + (totalMs >= 1000 ? (totalMs / 1000).toFixed(1) + "s" : totalMs + "ms")
      + (value.embedding ? " · " + value.embedding : "")
      + (typeof totals.chunks === "number" ? " · " + totals.chunks + " 块 / " + (totals.vectors || 0) + " 向量" : ""));
    if (files.length > 0) {
      lines.push("");
      lines.push("**最近入库（滚动）**");
      const tail = files.slice(-8).reverse();
      tail.forEach(function (f) {
        const name = String(f.path || "").split(/[\\/]/).pop();
        const icon = f.status === "added" ? "✓" : (f.status === "skipped" ? "·" : (f.status === "duplicate" ? "≈" : (f.status === "error" || f.status === "missing" ? "✗" : "·")));
        const ms = typeof f.ms === "number" ? f.ms : 0;
        // 失败/缺失要给出原因：只显示"✗ 文件"会让用户完全不知道下一步该做什么
        const why = (f.status === "error" || f.status === "missing")
          ? (f.error ? " · " + String(f.error).slice(0, 160) : "")
          : (f.note && f.status === "changed" ? " · " + String(f.note).slice(0, 120) : "");
        lines.push(icon + " " + name + " · " + ms + "ms" + why);
      });
      const totalN = typeof value.files_total === "number" ? value.files_total : files.length;
      if (files.length > tail.length) lines.push("（共 " + totalN + " 个文件，仅显示最近 " + tail.length + " 条；完整统计见 kb_stats）");
    }
    if (value.note) lines.push(String(value.note));
    return [{ type: "text", text: lines.join("\n") }];
  };

  const renderStats = (_args, value) => {
    if (value === null || typeof value !== "object") return [{ type: "text", text: String(value) }];
    const lines = [];
    lines.push("**知识库统计** · " + (value.docs || 0) + " 文档 / " + (value.chunks || 0) + " 块 / " + (value.vectors || 0) + " 向量");
    if (value.db) lines.push("数据库：" + value.db);
    const recent = Array.isArray(value.recent) ? value.recent : [];
    if (recent.length > 0) {
      lines.push("");
      lines.push("**最近入库**");
      recent.slice(0, 10).forEach(function (r) {
        lines.push("- " + String(r.file || "").split(/[\\/]/).pop() + " · " + (r.year || "-") + " · " + (r.chunks || 0) + " 块");
      });
      if (recent.length > 10) lines.push("（共 " + recent.length + " 条，仅显示最近 10 条）");
    }
    return [{ type: "text", text: lines.join("\n") }];
  };

  // 后台任务轮询：running 给进度，done 给 totals + 最近文件，error/not_found 说明原因。
  const renderStatus = (_args, value) => {
    if (value === null || typeof value !== "object") return [{ type: "text", text: String(value) }];
    const jobId = value.job_id ? String(value.job_id) : "";
    const status = value.status ? String(value.status) : "unknown";
    const head = (title) => title + (jobId.length > 0 ? " · job_id " + jobId : "");
    const lines = [];
    if (status === "running") {
      lines.push(head("**后台任务进行中**"));
      const p = value.progress && typeof value.progress === "object" ? value.progress : {};
      lines.push("已处理 " + (p.processed || 0) + " 篇 · 错误 " + (p.errors || 0) + " · 分块 " + (p.chunks || 0));
      if (typeof value.note === "string" && value.note.length > 0) lines.push(String(value.note));
      return [{ type: "text", text: lines.join("\n") }];
    }
    if (status === "done") {
      lines.push(head("**后台任务完成**"));
      const result = value.result && typeof value.result === "object" ? value.result : {};
      const totals = result.totals && typeof result.totals === "object" ? result.totals : {};
      const totLabels = [["added", "新增"], ["updated", "更新"], ["skipped", "跳过"], ["errors", "失败"], ["duplicates", "重复"], ["chunks", "分块"], ["vectors", "向量"]];
      const parts = [];
      totLabels.forEach(function (kv) {
        const n = totals[kv[0]] || 0;
        if (n) parts.push(kv[1] + " " + n);
      });
      lines.push(parts.length > 0 ? parts.join(" · ") : "无变化（统计见 kb_stats）");
      const files = Array.isArray(result.files) ? result.files : [];
      if (files.length > 0) {
        lines.push("");
        lines.push("**最近处理**");
        files.slice(-5).reverse().forEach(function (f) {
          lines.push("- " + String(f.path || "").split(/[\\/]/).pop() + (f.status ? " · " + String(f.status) : ""));
        });
        const totalN = typeof result.files_total === "number" ? result.files_total : files.length;
        if (totalN > files.length) lines.push("（共 " + totalN + " 个文件，仅显示最近 " + files.length + " 条）");
      }
      return [{ type: "text", text: lines.join("\n") }];
    }
    if (status === "error") {
      lines.push(head("**后台任务失败**"));
      const result = value.result && typeof value.result === "object" ? value.result : {};
      const err = value.error || result.error;
      lines.push(err ? String(err) : "引擎未返回错误详情，请检查宿主日志后再重试。");
      return [{ type: "text", text: lines.join("\n") }];
    }
    lines.push(head("**未找到该任务**"));
    lines.push("job_id 未知，或任务记录已被清理（任务完成后结果文件保留，kb_clear 会一并清空）。");
    if (typeof value.note === "string" && value.note.length > 0) lines.push(String(value.note));
    return [{ type: "text", text: lines.join("\n") }];
  };

  const renderFetch = (_args, value) => {
    if (value === null || typeof value !== "object") return [{ type: "text", text: String(value) }];
    const lines = [];
    lines.push("**下载完成** · " + (value.downloaded || 0) + " / " + (value.total || 0) + " 篇");
    if (value.network) {
      const envLabel = value.network.env === "campus" ? "校园网/机构网络" : value.network.env === "home" ? "家庭网络" : "未确认";
      lines.push("网络环境：" + envLabel);
      const p = value.network.proxy || {};
      const proxyMsgs = [];
      if (Array.isArray(p.env) && p.env.length) proxyMsgs.push("环境变量代理");
      if (Array.isArray(p.localPorts) && p.localPorts.length) proxyMsgs.push("本机代理端口:" + p.localPorts.join(","));
      if (p.system === true) proxyMsgs.push("系统代理");
      if (proxyMsgs.length) lines.push("注意：检测到代理(" + proxyMsgs.join("; ") + ")——代理可能干扰下载(TLS/反爬)，如失败请关闭代理后重试");
    }
    if (value.target) lines.push("保存到：" + value.target);
    const files = Array.isArray(value.files) ? value.files : [];
    const fails = [];
    files.forEach(function (f) {
      const name = f.path ? String(f.path).split(/[\\/]/).pop() : String(f.id || "");
      if (f.status === "downloaded") {
        lines.push("✓ " + name);
      } else {
        fails.push(f);
        lines.push("✗ " + name + (f.error ? " · " + String(f.error).slice(0, 200) : ""));
      }
    });
    if (fails.length > 0) {
      lines.push("");
      lines.push("**未能自动下载 " + fails.length + " 篇** —— 失败原因已标注在上方（含打开链接），请在浏览器中打开对应 DOI 手动下载，再用 kb_ingest 入库（或 Zotero 抓取后同步）");
    }
    if (value.note) { lines.push(""); lines.push(String(value.note)); }
    return [{ type: "text", text: lines.join("\n") }];
  };

  const renderSources = (_args, value) => {
    if (value === null || typeof value !== "object") return [{ type: "text", text: String(value) }];
    const items = Array.isArray(value.evidence) ? value.evidence : (Array.isArray(value.results) ? value.results : []);
    if (items.length === 0) return [{ type: "text", text: JSON.stringify(value) }];
    const refRange = function (cs) {
      const ns = cs.map(function (c) { return c && c.n; }).filter(function (n) { return n !== null && n !== undefined; }).map(Number).sort(function (a, b) { return a - b; });
      const parts = [];
      let start = null, prev = null;
      ns.forEach(function (x) {
        if (start === null) { start = prev = x; }
        else if (x === prev + 1) { prev = x; }
        else { parts.push(start === prev ? String(start) : start + "–" + prev); start = prev = x; }
      });
      if (start !== null) parts.push(start === prev ? String(start) : start + "–" + prev);
      return parts.join(", ");
    };
    // score 仅在精排后显示（bge 余弦相似度可校准；RRF 融合分无绝对含义，显示反而误导）
    const scoreNote = value.reranker ? " · score " : "";
    const quick = value.depth === "quick";
    const lines = [];
    lines.push("**知识库来源 Top-" + items.length + "**" + (quick ? "（快速检索）" : (value.depth === "deep" ? "（深度检索）" : "")));
    // 实际使用的检索路径（引擎会因 mode 参数或向量不可用而降级）：写死"混合检索"会误导
    const MODE_LABEL = { hybrid: "混合检索", keyword: "关键词检索", vector: "向量检索" };
    lines.push((MODE_LABEL[value.mode_used] || "混合检索") + (value.reranker ? " · 精排 " + value.reranker.split(" ")[0] : "") + (value.cached === true ? " · 缓存命中" : "") + (typeof value.ms === "number" ? " · " + value.ms + "ms" : "") + (value.strict === true ? " · 严格模式" : "") + (value.dup_collapsed > 0 ? " · 已折叠 " + value.dup_collapsed + " 份同论文副本" : ""));
    // 引擎的语言提示（中文查询 + 几乎全英文库）：原样转达，提醒用英文术语重查
    if (typeof value.lang_note === "string" && value.lang_note.length > 0) {
      lines.push("提示：" + value.lang_note);
    }
    items.forEach(function (r, i) {
      const title = String(r.title || r.file || "");
      const doi = typeof r.doi === "string" && r.doi.length > 0 ? r.doi : null;
      const t = doi !== null ? "[" + title + "](https://doi.org/" + doi + ")" : title;
      const rest = [
        typeof r.authors === "string" && r.authors.length > 0 ? String(r.authors).split(";").map(function (s) { return s.trim(); }).filter(Boolean).slice(0, 3).join("; ") : null,
        r.year,
        r.journal,
        r.section ? ("§" + r.section) : null,
      ].filter(Boolean).join(" · ");
      lines.push("");
      lines.push((i + 1) + ". " + t + (rest.length > 0 ? " — " + rest : ""));
        lines.push("> " + String(r.snippet || "").slice(0, quick ? 200 : 280).replace(/\n/g, " "));
        if (quick) {
          // 快速检索：不带图注/引文链/搜索串；无 DOI 时补文件名供引用
          if (doi === null && r.file) lines.push("无 DOI · 文件：" + String(r.file));
          return;
        }
        if (typeof r.figure === "string" && r.figure.length > 0) {
        lines.push("↳ 图注坐标: " + String(r.figure).slice(0, 220));
      }
      // 引文关联：本证据的参考文献条目；库内命中（[库内]）优先展示，未命中折叠到汇总行
      if (Array.isArray(r.citations) && r.citations.length > 0) {
        const hits = r.citations.filter(function (c) { return c && c.lib && typeof c.lib === "object"; });
        const others = r.citations.filter(function (c) { return !(c && c.lib && typeof c.lib === "object"); });
        lines.push(hits.length > 0
          ? "↳ 引文补充（本证据的参考文献；[库内]=已在库内，可检索引用）"
          : "↳ 引文补充（本证据的参考文献，供补库/深读）");
        hits.slice(0, 5).concat(others.slice(0, 3)).forEach(function (c) {
          lines.push("  · [Ref " + c.n + "] " + String(c.text || "").slice(0, 150));
          if (c.lib) {
            const ldoi = typeof c.lib.doi === "string" && c.lib.doi.length > 0 ? c.lib.doi : null;
            const lt = ldoi !== null ? "[" + String(c.lib.title || "") + "](https://doi.org/" + ldoi + ")" : String(c.lib.title || "");
            const lmeta = [
              typeof c.lib.authors === "string" && c.lib.authors.length > 0 ? String(c.lib.authors).split(";").map(function (s) { return s.trim(); }).filter(Boolean).slice(0, 2).join("; ") : null,
              c.lib.year,
              c.lib.journal,
            ].filter(Boolean).join(" · ");
            let tail = "（即本证据的 Ref " + c.n + "，可检索引用）";
            if (typeof c.lib.zotero_key === "string" && c.lib.zotero_key.length > 0) {
              tail += " · [Zotero 打开](zotero://open-pdf/library/items/" + c.lib.zotero_key + ")";
            }
            lines.push("    [库内] " + lt + (lmeta.length > 0 ? "（" + lmeta + "）" : "") + tail);
          }
        });
        const rest = hits.slice(5).concat(others.slice(3));
        if (rest.length > 0) {
          lines.push("  ↳ 另有 " + rest.length + " 条引文未展开（Ref " + refRange(rest) + "），补库时可按编号定位");
        }
      }
      if (doi !== null) {
        lines.push("[DOI " + doi + "](https://doi.org/" + doi + ")" + (scoreNote !== "" ? scoreNote + r.score : ""));
      } else {
        lines.push("无 DOI" + (scoreNote !== "" ? scoreNote + r.score : "") + " · 文件：" + String(r.file || ""));
        if (typeof r.search === "string" && r.search.length > 0) {
          lines.push("↳ 搜索串（Scholar 可复制）: " + String(r.search).slice(0, 200));
        }
      }
      if (typeof r.path === "string" && r.path.length > 0) {
        lines.push(r.path);
      }
      if (typeof r.zotero_key === "string" && r.zotero_key.length > 0) {
        lines.push("[在 Zotero 中打开 PDF](zotero://open-pdf/library/items/" + r.zotero_key + ")");
      }
    });
    if (quick) {
      lines.push("");
      lines.push("（快速检索：直接输出查到的信息即可，一两句话答完，无需展开分析；需要深入背景时用 depth=deep 重查）");
      return [{ type: "text", text: lines.join("\n") }];
    }
    if (Array.isArray(value.related) && value.related.length > 0) {
      lines.push("");
      lines.push("**关联文献（可作补充建议）**");
      value.related.forEach(function (r) {
        const doi = typeof r.doi === "string" && r.doi.length > 0 ? r.doi : null;
        const t = doi !== null
          ? "[" + String(r.title || r.file || "") + "](https://doi.org/" + doi + ")"
          : String(r.title || r.file || "");
        const meta = [
          typeof r.authors === "string" && r.authors.length > 0 ? String(r.authors).split(";").map(function (s) { return s.trim(); }).filter(Boolean).slice(0, 2).join("; ") : null,
          r.year,
          r.journal,
        ].filter(Boolean).join(" · ");
        lines.push("- " + t + (meta.length > 0 ? " — " + meta : "") + "（" + String(r.reason || "内容相关") + "）");
      });
    }
    return [{ type: "text", text: lines.join("\n") }];
  };

  const presentQueryCall = (args) => ({ card: "generic", title: args.query, kind: "other", rawInput: args.query });

  const filterSchema = {
    type: "object",
    additionalProperties: false,
    description: "可选元数据预过滤。",
    properties: {
      authors: { type: "string", description: "作者子串匹配（如 Zhang）。" },
      title: { type: "string", description: "标题子串匹配。" },
      journal: { type: "string", description: "期刊子串匹配。注意：期刊字段目前只由 Zotero 迁移填充（publicationTitle/journalAbbreviation）；用 kb_ingest 建起来的库里该字段为 NULL，用它过滤通常零命中——想限定来源请改用 authors/year/title。" },
      kind: { type: "string", description: "文件类型：pdf/txt/md/docx。" },
      section: { type: "string", description: "章节子串匹配（如 Methods、Results、方法）。" },
      year: { oneOf: [{ type: "integer", description: "精确年份（如 2024）。" }, { type: "string", description: "年份比较式（如 \">=2020\"）。" }], description: "年份过滤。" },
    },
  };

  ctx.tools.register(defineTool({
    name: "kb_ingest",
    description: "把本地文档（PDF/TXT/MD/DOCX）导入 DSH 知识库并建立索引（轻量 RAG 工作流的入库步骤）。支持单个文件或目录（递归扫描并只处理 PDF/TXT/MD/DOCX）；按章节切分并抽取元数据（标题/作者/年份/DOI）；同时用本地 bge-small 模型生成向量（数据持久化在工作区/.kb）。已入库且内容未变的文件自动跳过；同一内容（sha256 相同）在其他路径已入库时标记为 duplicate 跳过（增量）。paths 用工作区内的相对路径或绝对路径。入库后用 kb_search 检索、kb_rag 问答、kb_stats 看统计。重复调用安全。metadata_only=true 只刷新元数据（秒级，不重切块/不重嵌入，适合引擎升级后让老库的标题/作者/DOI 生效）；rebuild=true 原地重灌库内全部已入库文档（不会因传目录而重复入库）；大批量会自动转后台并返回 job_id，用 kb_status 轮询。",
    parameters: {
      // paths 与 rebuild 二选一：rebuild=true 时引擎按库内现有路径重灌，不需要 paths。
      // 不标 required，缺两项时由引擎给出明确错误（"paths is required（或用 rebuild=true…）"）。
      paths: { type: "array", items: { type: "string" }, description: "要入库的文件或目录路径列表；rebuild=true 时可省略。" },
      kb_root: { type: "string", description: "知识库目录（默认：工作区下的 .kb）。" },
      force: { type: "boolean", description: "true 时强制重新解析并重新编码向量（默认 false）。" },
      metadata_only: { type: "boolean", description: "true 时只刷新元数据（重抽标题/作者/年份/期刊/DOI，秒级；不重切块、不重嵌入；内容已变的文件不动）。" },
      rebuild: { type: "boolean", description: "true 时原地重灌库内全部已入库文档（路径取自库内，可省略 paths）。大批量会自动转后台并返回 job_id。" },
    },
    output: { schema: { type: "json" }, render: renderIngest },
    timeoutMs: 1800000,
    execute(args, exec) {
      return runEngine("ingest", {
        paths: args.paths,
        kb_root: kbRootOf(args, exec),
        force: args.force === true,
        metadata_only: args.metadata_only === true,
        rebuild: args.rebuild === true,
        async_if_large: true,
      }, exec);
    },
  }));

  ctx.tools.register(defineTool({
    name: "kb_search",
    description: "在知识库中做混合检索（关键词 BM25 + 向量余弦，RRF 融合，×章节权重），返回最相关片段及精确来源（文件/标题/作者/年份/期刊/DOI/章节）。想在已入库文档中查找事实、数据或术语时优先于直接读文件（更省 token）。depth 双模式：quick（默认）=快速检索，混合召回直出、跳过精排与引文扩展，亚秒级响应，适合事实性查询；工具返回后立即作答，不展开背景与延伸分析；deep=深度检索，bge-reranker 精排 + 引文链 + 关联文献（适合领域调研与综述性问题）。query 用**英文术语串**——库内正文以英文为主，中文问句会让 BM25 关键词路空转、只靠向量侧跨语言匹配，命中明显更差；写法为 3–12 个词，结构「材料/体系 + 方法/工艺 + 性质/表征」（如 \"graphene CVD copper single crystal nucleation suppression\"），不要用整句问句，年份/期刊/作者请放 filters，需要中文文献时用用户原话另发一条中文查询；引擎按原样检索，不会替你翻译；mode 可选 keyword/vector/hybrid（默认 hybrid）；filters 支持 authors/year/section/title/journal/kind 元数据预过滤（year 可用 \">=2020\" 形式）；其中 journal 目前只由 Zotero 迁移填充，kb_ingest 入库的文档该字段为 NULL，用它过滤通常零命中。查询范围由会话开始时的范围询问或 kb_scope 工具控制；返回的 scope/scope_note 指明当前范围。strict 可选（true=严格模式：答案仅基于本次结果，禁止库外知识/常识外延；默认继承 kb_scope 设置）。回答用户时必须标注来源：引用要写成 markdown 链接格式 [作者, 年份, 期刊](https://doi.org/DOI)（用来源字段里的 doi，保证用户能点击打开）；若该来源无 DOI，引用写成 [作者, 年份, 文件名]（方括号内只放 PDF 文件名，不要使用任何 HTML 标签；文件名过长时可截断到约 60 字符）。无命中时先检查是否已入库（kb_stats）。相同查询命中缓存，零重计算。",
    parameters: {
      query: { type: "string", required: true, description: "检索词，**英文优先**：3–12 个英文术语，结构「材料/体系 + 方法/工艺 + 性质/表征」（如 \"graphene CVD copper single crystal nucleation suppression\"）；限定条件放 filters；引擎按原样检索、不翻译。需要中文文献时用中文原话另发一条查询。" },
      depth: { type: "string", enum: ["quick", "deep"], description: "quick=快速检索（默认：无精排/引文链/关联文献，响应最快，适合查个信息）；deep=深度检索（精排+引文链+关联文献，适合领域调研与综述性问题）。默认继承 kb_scope 的会话 depth 设置。" },
      top_k: { type: "integer", description: "返回结果数（默认 quick 3 / deep 5，上限 10）。" },
      snippet: { type: "integer", description: "片段长度字符数（默认 quick 300 / deep 400）。" },
      mode: { type: "string", enum: ["keyword", "vector", "hybrid"], description: "检索模式（默认 hybrid）。" },
      rerank: { type: "boolean", description: "是否启用 bge-reranker-base 精排（默认 quick 关 / deep 开）。" },
      related: { type: "boolean", description: "true 时附带 related 关联文献列表（默认 quick 关 / deep 开，供补充建议引用）。" },
      strict: { type: "boolean", description: "严格模式：true 时答案仅基于本次检索结果，禁止补充库外知识/常识外延（默认继承 kb_scope 的 strict 设置）。" },
      kb_root: { type: "string", description: "知识库目录（默认：工作区下的 .kb）。" },
      filters: filterSchema,
    },
    output: { schema: { type: "json" }, render: renderSources },
    presentCall: presentQueryCall,
    execute(args, exec) {
      const strict = args.strict === undefined ? scopeStrict : args.strict === true;
      const call = runEngine("search", {
        query: args.query,
        depth: args.depth === undefined ? scopeDepth : args.depth,
        top_k: args.top_k,
        snippet: args.snippet,
        mode: args.mode,
        rerank: args.rerank,
        related: args.related,
        filters: args.filters,
        kb_root: kbRootOf(args, exec),
      }, exec);
      return scopeWrapped(exec, call, strict, kbRootOf(args, exec));
    },
  }));

  ctx.tools.register(defineTool({
    name: "kb_rag",
    description: "在知识库中检索证据片段供当前模型直接作答：基于 evidence 回答问题，每个事实后标注引用编号 [n]（对应 evidence 下标）。引用一定要写成可点击的 markdown 链接：[作者, 年份, 期刊](https://doi.org/DOI)（用 evidence 条目的 doi 字段）；若 doi 为 null，引用写成 [作者, 年份, 文件名]（方括号内只放 PDF 文件名，不要使用任何 HTML 标签；文件名过长时可截断到约 60 字符）。depth 双模式：deep（默认）=深度检索，重排序 + 引文关联 + 相关文献全链路，回答可综合多篇展开论述（适合领域调研）；quick=快速检索，仅基于少量证据直接作答，不展开论述。strict 可选（true=严格模式：仅基于 evidence 作答，禁止补充库外知识/常识外延或未出现在 evidence 中的文献数据，证据不足直接说明无法回答；默认继承 kb_scope 设置，当前默认 false）。资料不足时明确回答\"根据现有资料无法回答\"；多源冲突时分别列出并说明来源。答案末尾的补充建议按来源分三列（哪列为空就整列省略）：①「库内可查（循引文找到）」——citations 里标 [库内] 的文献，必须写出关系链「《被引文献》(作者, 年份) 被 [证据编号] 的引文 Ref n 引用，已在库内可直接提问」；②「建议补库（循引文发现）」——citations 未命中库内的条目，注明被 Ref n 引用、尚不在库内，可用 Ref 编号定位下载；③「相关文献」——related 列表（同作者/同期刊/主题相似的库内文献，元数据相似）。每条推荐的理由必须写明属于哪种，引文关联的必须带关系链，不得混列；若库内缺少关键资料，明确指出应补充哪些文献/主题（用户重视此提示）。这是知识库 RAG 问答的唯一入口；查询范围由会话开始时的范围询问或 kb_scope 工具控制。",
    parameters: {
      query: { type: "string", required: true, description: "要回答的问题——请先把它转写成**英文检索词**再传入（3–12 词，术语优先，不要整句中文问句）：库内正文以英文为主，引擎按原样检索、不替你翻译。" },
      depth: { type: "string", enum: ["quick", "deep"], description: "deep=深度检索（默认：精排+引文链+关联文献，回答展开背景，适合不熟悉领域）；quick=快速检索（少量证据直接给答案，不展开）。默认继承 kb_scope 的会话 depth 设置。" },
      top_k: { type: "integer", description: "证据条数（默认 quick 2 / deep 3，上限 10）。" },
      rerank: { type: "boolean", description: "是否启用精排（默认 quick 关 / deep 开）。" },
      related: { type: "boolean", description: "true 时附带 related 关联文献列表供补充建议引用（默认 quick 关 / deep 开）。" },
      strict: { type: "boolean", description: "严格模式：true 时仅基于 evidence 作答，禁止库外知识补充（默认继承 kb_scope 的 strict 设置）。" },
      kb_root: { type: "string", description: "知识库目录（默认：工作区下的 .kb）。" },
      filters: filterSchema,
    },
    output: { schema: { type: "json" }, render: renderSources },
    presentCall: presentQueryCall,
    execute(args, exec) {
      const strict = args.strict === undefined ? scopeStrict : args.strict === true;
      const call = runEngine("rag", {
        query: args.query,
        depth: args.depth === undefined ? scopeDepth : args.depth,
        top_k: args.top_k,
        rerank: args.rerank,
        related: args.related,
        filters: args.filters,
        kb_root: kbRootOf(args, exec),
      }, exec);
      return scopeWrapped(exec, call, strict, kbRootOf(args, exec));
    },
  }));

  ctx.tools.register(defineTool({
    name: "kb_zotero",
    description: "把本地 Zotero 文献库中带 PDF 附件的文献批量迁移到知识库（轻量 RAG 工作流的 Zotero 接口）。读取 zotero.sqlite（默认自动定位 ~/Zotero、~/Documents/Zotero、%APPDATA% 配置；找不到时用 zotero_db 显式指定），解析每篇文献的元数据（标题/作者/年份/期刊/DOI）与 PDF 附件路径（storage 目录），逐篇解析入库并生成向量；已入库附件自动跳过，重复内容标记 duplicate 跳过（增量，可反复运行）。附件文件本体缺失的条目标记为 missing 并跳过（不尝试下载）。dry_run=true 时只列候选不写入；limit 限制迁移条数。",
    parameters: {
      zotero_db: { type: "string", description: "zotero.sqlite 显式路径（默认自动定位）。" },
      kb_root: { type: "string", description: "知识库目录（默认：工作区下的 .kb）。" },
      limit: { type: "integer", description: "迁移条数上限（默认全部）。" },
      force: { type: "boolean", description: "true 时强制重新解析已入库附件（默认 false）。" },
      dry_run: { type: "boolean", description: "true 时只列候选文献，不导入（默认 false）。" },
    },
    output: { schema: { type: "json" }, render: renderIngest },
    timeoutMs: 1800000,
    execute(args, exec) {
      return runEngine("zotero", {
        zotero_db: args.zotero_db,
        kb_root: kbRootOf(args, exec),
        limit: args.limit,
        force: args.force === true,
        dry_run: args.dry_run === true,
      }, exec);
    },
  }));

  ctx.tools.register(defineTool({
    name: "kb_dedup",
    description: "清理知识库中的重复文档：删除 sha256 与早期文档相同的后来入库项（保留最早 id）并同步清除其分块/向量/缓存。返回 removed 与当前总数。反复调用安全。",
    parameters: {
      kb_root: { type: "string", description: "知识库目录（默认：工作区下的 .kb）。" },
    },
    output: { schema: { type: "json" }, render: renderJson },
    execute(args, exec) {
      return runEngine("dedup", { kb_root: kbRootOf(args, exec) }, exec);
    },
  }));

  ctx.tools.register(defineTool({
    name: "kb_clear",
    description: "清空知识库中的全部文献与索引（文档/分块/向量/缓存全部删除，不可恢复；数据库文件保留结构）。必须显式传 confirm: true 才会执行（否则拒绝）。清空后可重新 kb_ingest 或 kb_zotero 重建。",
    parameters: {
      kb_root: { type: "string", description: "知识库目录（默认：工作区下的 .kb）。" },
      confirm: { type: "boolean", required: true, description: "必须显式传 true 确认清空全部文献。" },
    },
    output: { schema: { type: "json" }, render: renderJson },
    execute(args, exec) {
      return runEngine("clear", { kb_root: kbRootOf(args, exec), confirm: args.confirm === true }, exec);
    },
  }));

  ctx.tools.register(defineTool({
    name: "kb_fetch",
    description: "按 DOI / arXiv ID 把论文 PDF 下载到本地目录（默认 ~/.kb-rag/downloads，可用 target_dir 覆盖）。按标准元标签与公开 API 解析地址，顺序为：arXiv 直连 → 出版商正式版（落地页 citation_pdf_url；在校园网/机构订阅网络下可直接取得订阅版 PDF，无需额外配置）→ 落地页内常见 pdf 链接 → 开放获取兜底（Unpaywall / Crossref）。只做常规抓取，不绕过付费墙、不访问 Sci-Hub、不伪造凭据。下载后不会自动进 Zotero——需用户手动在 Zotero 里「文件→添加文件」或拖入该目录 PDF 入库。",
    parameters: {
      identifiers: { type: "array", required: true, items: { type: "string" }, description: "DOI 或 arXiv ID 列表（如 10.5555/12345679 或 arXiv:2401.00001）。" },
      target_dir: { type: "string", description: "下载目录（默认 ~/.kb-rag/downloads）。" },
    },
    output: { schema: { type: "json" }, render: renderFetch },
    timeoutMs: 300000,
    execute(args, exec) {
      askNetworkOnce(exec && exec.agent);
      const network = { env: netEnv, proxy: { env: envProxyDetect() } };
      return runEngine("fetch", { identifiers: args.identifiers, target_dir: args.target_dir, network: network }, exec);
    },
  }));

  ctx.tools.register(defineTool({
    name: "kb_scope",
    description: "设置/查看知识库查询范围、回答深度与严格模式（会话开始时也会询问一次范围）：scope：kb=仅封闭知识库；both=知识库+全网（kb 检索 + web_search 补充）；web=仅全网。depth 可选：quick=快速检索（亚秒级响应，直出结果）；deep=深度检索（重排序+引文关联全链路，跨文献综合论述）。strict 可选：true=严格模式（答案仅基于库内证据，禁止库外知识/常识外延）；false=关闭（默认 false）。用户说\"封闭库/全网/都要/严格只按库内/快速检索/深度检索\"等要求时，调本工具设定后再检索。",
    parameters: {
      scope: { type: "string", enum: ["kb", "both", "web"], description: "kb=仅封闭库；both=知识库+全网；web=仅全网。不传则只查看当前设置。" },
      depth: { type: "string", enum: ["quick", "deep"], description: "可选：同时设置检索深度。quick=快速检索；deep=深度检索。" },
      strict: { type: "boolean", description: "可选：同时设置严格模式。true=仅基于库内证据作答；false=关闭（默认）。" },
    },
    output: { schema: { type: "json" }, render: renderJson },
    execute(args, _exec) {
      // scope 是可选的：不传时本工具只返回当前会话设置（描述里承诺了"设置/查看"）
      if (args.scope !== undefined) scopePref = args.scope;
      if (args.depth !== undefined) scopeDepth = args.depth === "deep" ? "deep" : "quick";
      if (args.strict !== undefined) scopeStrict = args.strict === true;
      console.log("[kb-rag] query scope:", scopePref, "depth:", scopeDepth, "strict:", scopeStrict);
      return Promise.resolve({ ok: true, scope: scopePref, depth: scopeDepth, strict: scopeStrict, scope_note: SCOPE_NOTE[scopePref], depth_note: scopeDepth === "quick" ? "快速检索：kb_search 默认快速检索，kb_rag 可显式 depth=quick" : "深度检索：kb_search/kb_rag 均默认深度检索", strict_note: scopeStrict ? STRICT_NOTE : undefined });
    },
  }));

  ctx.tools.register(defineTool({
    name: "kb_stats",
    description: "查看知识库统计：文档数、分块数、向量数、最近入库列表及数据库位置。用于检查哪些文档已入库、索引状态；检索无命中时先调它确认库里有什么。",
    parameters: {
      kb_root: { type: "string", description: "知识库目录（默认：工作区下的 .kb）。" },
    },
    output: { schema: { type: "json" }, render: renderStats },
    execute(args, exec) {
      return runEngine("stats", { kb_root: kbRootOf(args, exec) }, exec);
    },
  }));

  ctx.tools.register(defineTool({
    name: "kb_status",
    description: "查询后台任务进度或结果。大批量入库（kb_ingest）会自动转后台并返回 job_id，用本工具轮询：running 时给出已处理篇数/错误数/分块数，done 时给出 totals 与最近 20 条文件，error/not_found 时说明原因。宿主调用超时不会中断后台任务。",
    parameters: {
      job_id: { type: "string", required: true, description: "后台任务 id（kb_ingest 转后台时返回的 job_id，12 位十六进制）。" },
      kb_root: { type: "string", description: "知识库目录（默认：工作区下的 .kb）。" },
    },
    output: { schema: { type: "json" }, render: renderStatus },
    execute(args, exec) {
      return runEngine("status", { job_id: args.job_id, kb_root: kbRootOf(args, exec) }, exec);
    },
  }));

  console.log("[kb-rag] static tools registered (v1.6.7): kb_ingest / kb_search / kb_rag / kb_zotero / kb_dedup / kb_clear / kb_fetch / kb_scope / kb_stats / kb_status");
}

export { apply, inject, name };
