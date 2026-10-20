// dsh-kb-rag — static DSH plugin (Host half)
// 本地文献知识库 RAG：10 个模型工具 + 常驻 Python 引擎（随包分发 kb_engine.py）。
// 加载：部署的 cordis 组合中加入本包（cordis-plugin-loader 按 npm 包名解析）。
import { defineTool } from "@deepseek-ai/dsh-tools";
import { fileURLToPath } from "node:url";
import os from "node:os";
import { join as joinPath, extname } from "node:path";
import { readdirSync } from "node:fs";
import { guidedDescription, resultNotes, makeThrottle, policyInventory } from "./guidance.js";

const name = "kb-rag";
// subprocess 必须声明在 inject 里：cordis 只等待 inject 中的服务，而 loader 并发激活各 entry
// （cordis-plugin-loader 的 group 用 Promise.allSettled），本包体积小、往往在
// @deepseek-ai/dsh-subprocess-local 注册服务之前就执行 apply()，这时 ctx.get("subprocess")
// 是 undefined（未注册返回 undefined 而不抛错）→ 提前 return，10 个工具全部不注册。
const inject = ["tools", "timer", "subprocess"];

const ENGINE_DIR = fileURLToPath(new URL("..", import.meta.url)); // package root
const ENGINE_PATH = fileURLToPath(new URL("../kb_engine.py", import.meta.url));

const SCOPE_NOTE = {
  kb: "范围：封闭知识库。仅基于库内文献作答；如需开放网络检索，用 kb_scope 切换范围。",
  both: "范围：知识库+全网。除本库内结果外，请再调用 web_search 检索开放网络，合并作答并分别标注来源。",
  web: "范围：仅全网。本次仅给出库内命中供参考；请以 web_search 结果为准作答。",
};
const STRICT_NOTE = '严格模式：答案仅允许基于本次检索返回的 evidence/results 内容；禁止补充库外知识、常识外延或未出现在证据中的文献与数据；证据不足时直接说明"根据现有资料无法回答"。';

function apply(ctx) {
  // inject 已声明 subprocess，正常情况下这里一定有值；保留兜底只为诊断注入被改坏的场景。
  const subprocess = ctx.get("subprocess");
  if (subprocess === undefined) {
    console.error("[kb-rag] subprocess service unavailable despite inject; tools not registered");
    return;
  }

  // ---- 提示层：规则全部声明在 guidance.js，这里只做注入与节流记账 ----
  // （节流记录、会话状态、注册入口见下方"会话级状态"块）

  let daemon = null;
  let spawning = null;
  let netEnv = "unknown";

  // ── 会话级状态（#7）────────────────────────────────────────────────────────
  // 以前 scope/depth/strict 是 apply() 闭包里的**单份**变量：第二个会话起不再询问，
  // 某个会话改一次就污染整个 app（含并行 subagent）。现在按会话键隔离，
  // 并可持久化成工作区默认值（<workspace>/.kb-rag/state.json，由引擎代读写）。
  // 会话键来源：exec.agent —— dsh 的 Agent.id 就是 SessionId（@deepseek-ai/dsh-agent）。
  const SESSION_DEFAULTS = {
    // #7 两段式：会话默认深度也是 quick（第一跳），要详细时 agent 显式传 deep 或用 /kb deep。
    // 默认 deep 的代价是每次问答都先跑精排 + 引文链（约 +1.9 s），而多数提问只要一句结论。
    scope: "kb", depth: "quick", strict: false, enabled: true,
    diligence: "normal", askedAt: 0, netAsked: false,
    // 答案格式预设（/kb format）：auto = 由模型在 answer.template 里决定（默认）。
    format: "auto",
  };
  const sessionStates = new Map();
  // 会话相关表的条数上限：GUI 长跑（成百上千个会话）时不让它们无限增长。
  const SESSION_MAP_MAX = 100;
  function boundSessionMap(m) {
    while (m.size > SESSION_MAP_MAX) {
      const oldest = m.keys().next().value;
      if (oldest === undefined) break;
      m.delete(oldest);
    }
  }
  let persisted = {};          // state.json 里的默认值（引擎代读写，插件不直接碰文件系统）

  function sessionKey(exec) {
    const a = exec && exec.agent;
    const id = a && (a.id || a.sessionId);
    return (id === undefined || id === null) ? "__default__" : String(id);
  }

  function stateOf(exec) {
    const k = sessionKey(exec);
    let st = sessionStates.get(k);
    if (st === undefined) {
      st = Object.assign({}, SESSION_DEFAULTS, persisted);   // 会话初始值 = 持久化默认值
      sessionStates.set(k, st);
      boundSessionMap(sessionStates);
    }
    return st;
  }

  let persistedLoad = null;    // 首次读取工作区默认值的 Promise（避免"还没读完就判断"的竞态）
  function loadPersistedDefaults(kbRoot, exec) {
    if (persistedLoad !== null) return persistedLoad;
    persistedLoad = runEngine("state", { kb_root: kbRoot, action: "read" }, exec).then(function (resp) {
      const s = resp && resp.state;
      if (s !== null && typeof s === "object") persisted = s;
      return persisted;
    }).catch(function (e) {
      console.error("[kb-rag] state read skipped:", String(e));
      persistedLoad = null;      // 允许下一次调用再试
      return persisted;
    });
    return persistedLoad;
  }

  // 答案格式预设：只在模型**确实要出 HTML**（传了 answer）却没指定模板时补会话默认。
  // 没传 answer 时不强行生成 —— 否则"设了格式"会变成"每次检索都落盘一份 HTML"。
  const ANSWER_FORMATS = ["auto", "instant", "review", "method", "compare", "numeric",
    "timeline", "concept", "dispute", "derivation", "qa"];
  function applyFormatDefault(exec, answer) {
    const fmt = stateOf(exec).format;
    if (fmt === undefined || fmt === null || fmt === "auto") return answer;
    if (answer === null || typeof answer !== "object") return answer;
    if (answer.template !== undefined && answer.template !== null && answer.template !== "") return answer;
    return Object.assign({}, answer, { template: fmt });
  }

  function savePersistedDefaults(patch, kbRoot, exec) {
    Object.assign(persisted, patch);
    runEngine("state", { kb_root: kbRoot, action: "write", state: patch }, exec)
      .catch(function (e) { console.error("[kb-rag] state write failed:", String(e)); });
  }

  // 惰性读取 userQuestions（#13）：apply() 期一次性捕获会在"服务尚未注册"时把后续所有询问
  // 静默跳过；而**不能**把它写进 inject —— 它是可选服务，未注册会让插件永远 park
  //（把"丢 10 个工具"升级成"插件永不激活且无日志"）。
  let warnedNoUserQuestions = false;
  function userQuestionsNow() {
    const uq = ctx.get("userQuestions");
    if (uq === undefined && !warnedNoUserQuestions) {
      warnedNoUserQuestions = true;
      console.error("[kb-rag] userQuestions 服务当前不可用：跳过本次询问（可用 kb_scope / /kb 手动设置）");
    }
    return uq;
  }

  // ── 节流按会话隔离（R6）──────────────────────────────────────────────────
  // 规则里的 maxPerSession / once 本来就该是"每会话"；进程级 Map 会让第二个会话看不到
  // 开场提示。会话键随响应传进来（见 reg() 给响应打的 __session），所以无需改规则本身。
  const throttleBySession = new Map();
  function throttleFor(key) {
    const k = String(key || "__default__");
    let store = throttleBySession.get(k);
    if (store === undefined) { store = new Map(); throttleBySession.set(k, store); boundSessionMap(throttleBySession); }
    return makeThrottle((id) => store.get(id), (id, value) => { store.set(id, value); });
  }

  const toolDisposers = [];      // 硬关闭用：ctx.tools.register 返回的 disposer

  // 描述注入：agent 每轮都会读到工具描述，调用纪律就落在这里（规则来自 guidance.js）
  // diligence 由会话状态决定：用户明确要求"深挖"时，描述层换成"深挖流程"（不设调用上限）
  const tool = (spec, exec) => defineTool(Object.assign({}, spec, {
    description: guidedDescription(spec.name, spec.description, {
      diligence: exec === undefined ? "normal" : stateOf(exec).diligence,
    }),
  }));
  // 结果注入：把命中规则的提示追加到渲染文本末尾；tellUser 的那些标成"可转达给用户"
  const withNotes = (toolName, renderer) => (args, value) => {
    // 探针结果自带“下一步怎么做”的口径；不再叠结果层提示，否则 no-hit 提示会和它重复。
    if (value !== null && typeof value === "object" && value.probe === true) return renderer(args, value);
    const out = renderer(args, value);
    try {
      const notes = resultNotes(toolName, value, throttleFor(value && value.__session),
        { diligence: (value && value.__diligence) || "normal" });
      if (notes.lines.length === 0 && notes.userHints.length === 0) return out;
      const extra = notes.lines.slice();
      if (notes.userHints.length > 0) extra.push("（可转达给用户）" + notes.userHints.join(" "));
      const blocks = Array.isArray(out) ? out.slice() : [{ type: "text", text: String(out) }];
      const last = blocks[blocks.length - 1];
      if (last !== undefined && last !== null && last.type === "text" && typeof last.text === "string") {
        blocks[blocks.length - 1] = Object.assign({}, last, { text: last.text + "\n\n" + extra.join("\n") });
      } else {
        blocks.push({ type: "text", text: extra.join("\n") });
      }
      return blocks;
    } catch (e) {
      return out;   // 提示层出错绝不影响检索结果
    }
  };

  // 统一注册入口：① 软关闭（enabled=false 时直接返回"已关闭"，不拉起守护进程）
  // ② 给响应打上会话键与当前 diligence，供结果层节流/规则使用 ③ 收集 disposer 供硬关闭
  const reg = (spec) => {
    const inner = spec.execute;
    const wrapped = Object.assign({}, spec, {
      execute(args, exec) {
        const st = stateOf(exec);
        if (st.enabled === false) {
          return Promise.resolve({
            ok: true, kb_rag_disabled: true, scope: st.scope,
            note: "kb-rag 当前处于关闭状态（/kb on 可开启库内检索）",
          });
        }
        const out = inner(args, exec);
        const tag = (v) => {
          if (v !== null && typeof v === "object" && !Array.isArray(v)) {
            if (v.__session === undefined) v.__session = sessionKey(exec);
            if (v.__diligence === undefined) v.__diligence = st.diligence;
          }
          return v;
        };
        return (out !== null && typeof out === "object" && typeof out.then === "function")
          ? out.then(tag) : tag(out);
      },
    });
    const dispose = ctx.tools.register(wrapped);
    toolDisposers.push({ name: spec.name, dispose: dispose });
    return dispose;
  };

  // ---- 下载前网络环境探测:代理检测(env 变量;本机代理端口由引擎探测) ----
  function envProxyDetect() {
    const keys = ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"];
    const set = [];
    try {
      keys.forEach(function (k) { const v = process.env[k]; if (v) set.push(k + "=" + v); });
    } catch (e) { /* process 不可用时忽略 */ }
    return set;
  }

  // 下载前询问网络环境(校园网可下订阅版;家庭网络以 OA 为主),非阻塞,**每会话**首次
  function askNetworkOnce(agent, exec) {
    const st = stateOf(exec);
    const uq = userQuestionsNow();
    if (st.netAsked || uq === undefined) return;
    st.netAsked = true;
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
      uq.ask(request).then(function (answer) {
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

  // 入库数据版本：先问引擎要"旧解析器入库"的文档数（stats.stale_docs）与陈旧类型
  // （stats.stale_kind：'chunk' 会改变分块/向量，必须全量重灌；'meta' 秒级刷元数据即可）。
  // 取不到（旧引擎/无库/调用失败）就静默跳过这条问题，绝不影响首次工具调用。
  function staleCountOf(kbRoot, exec) {
    return runEngine("stats", { kb_root: kbRoot }, exec).then(function (resp) {
      const n = resp ? Number(resp.stale_docs) : 0;
      if (!Number.isFinite(n) || n <= 0) return { n: 0, kind: "none" };
      const kind = resp && resp.stale_kind === "meta" ? "meta" : "chunk";
      return { n: n, kind: kind };
    }).catch(function (e) {
      console.error("[kb-rag] stale check skipped:", String(e));
      return { n: 0, kind: "none" };
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

  // 把 state.json 里的持久化默认值回填到已存在的会话状态。
  // 为什么必须显式回填：stateOf() 在第一次工具调用时同步建状态，而 loadPersistedDefaults()
  // 是异步的 —— 建状态那会儿 persisted 还是空对象，之后没人再合并，于是"重启后第一个会话"
  // 永远用内置默认（/kb save 看起来没生效，format 之类的新字段也会丢）。
  const PERSISTED_KEYS = ["scope", "depth", "strict", "diligence", "format", "enabled"];
  function backfillPersisted(st) {
    if (st.backfilled === true) return;
    st.backfilled = true;
    PERSISTED_KEYS.forEach(function (k) {
      if (persisted[k] !== undefined) st[k] = persisted[k];
    });
  }

  function askScopeOnce(agent, exec, kbRoot) {
    const st = stateOf(exec);
    backfillPersisted(st);
    const uq = userQuestionsNow();
    if (st.askedAt || uq === undefined) return;
    // 用户已经表达过偏好（上次会话选过，或用 /kb save 存过默认值）→ 不再重复询问。
    // 这就是「会话开始问一次并记住」的落点：问一次、写进 state.json、以后各会话直接用。
    if (persisted.scope) {
      st.askedAt = Date.now();
      console.log("[kb-rag] scope 已记住（state.json）：", persisted.scope, "depth:", persisted.depth);
      return;
    }
    st.askedAt = Date.now();
    const root = typeof kbRoot === "string" && kbRoot.length > 0 ? kbRoot : workspaceOf(exec) + "/.kb";
    staleCountOf(root, exec).then(function (staleInfo) {
      const stale = staleInfo && staleInfo.n ? staleInfo.n : 0;
      const staleKind = staleInfo && staleInfo.kind ? staleInfo.kind : "none";
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
        // stale_kind='chunk' 时**不提供**"只刷新元数据"：分块/向量的改动刷新元数据不会生效，
        // 给用户一个无效选项比不给更糟（他会以为修好了）。
        const chunkLevel = staleKind === "chunk";
        const opts = [
          { label: "暂不处理", description: "保持现状，随时可用 kb_ingest 的 metadata_only/rebuild 手动刷新" },
        ];
        if (!chunkLevel) {
          opts.push({ label: "只刷新元数据（推荐）", description: "秒级完成，仅重抽标题/作者/DOI，不重切块、不重嵌入" });
        }
        opts.push({
          label: chunkLevel ? "全量重灌（必须）" : "全量重灌（较慢）",
          description: chunkLevel
            ? "本次改动会改变分块与向量，只刷元数据不生效；重新解析并重新嵌入全部文档，期间转后台，可用 kb_status 查进度"
            : "重新解析并重新嵌入全部文档，期间会转后台，可用 kb_status 查进度",
        });
        request.questions.push({
          id: "kb-stale",
          header: "入库数据版本",
          question: "库内有 " + stale + " 篇文档是用旧版解析器入库的（引擎的解析改进不会自动作用于已有数据）。是否刷新？",
          options: opts,
        });
      }
      if (agent !== undefined) request.agent = agent;
      return Promise.race([
        uq.ask(request).then(function (answer) {
          const picked = answer && answer.answers && answer.answers[0] && answer.answers[0].selected && answer.answers[0].selected[0];
          if (typeof picked === "string" && picked.indexOf("仅封闭") === 0) st.scope = "kb";
          else if (typeof picked === "string" && picked.indexOf("知识库+全网") === 0) st.scope = "both";
          else if (typeof picked === "string" && picked.indexOf("仅全网") === 0) st.scope = "web";
          const pickedDepth = answer && answer.answers && answer.answers[1] && answer.answers[1].selected && answer.answers[1].selected[0];
          if (typeof pickedDepth === "string") {
              if (pickedDepth.indexOf("深度检索") === 0) st.depth = "deep"
              else if (pickedDepth.indexOf("快速检索") === 0) st.depth = "quick"
            };
          const pickedStale = answer && answer.answers && answer.answers[2] && answer.answers[2].selected && answer.answers[2].selected[0];
          if (typeof pickedStale === "string" && pickedStale.indexOf("只刷新元数据") === 0) refreshStale(root, exec, true);
          else if (typeof pickedStale === "string" && pickedStale.indexOf("全量重灌") === 0) refreshStale(root, exec, false);
          else if (typeof pickedStale === "string") console.log("[kb-rag] 旧数据暂不刷新（需要时用 kb_ingest 的 metadata_only / rebuild）");
          // 用户明确选过的偏好写进工作区默认值：下次会话直接用，不再重复问
          savePersistedDefaults({ scope: st.scope, depth: st.depth }, root, exec);
          console.log("[kb-rag] query scope:", st.scope, "depth:", st.depth, "(已记住)");
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
    const st = stateOf(exec);
    // 先读完工作区默认值再决定要不要问：否则进程重启后的第一次检索会在 persisted 仍是空对象时
    // 就判断"没有记住偏好"，把用户已经答过的范围又问一遍。
    loadPersistedDefaults(kbRoot, exec).then(function () {
      askScopeOnce(exec && exec.agent, exec, kbRoot);
    }).catch(function (e) {
      console.error("[kb-rag] scope prompt skipped:", String(e));
    });
    return engineCall.then(function (resp) {
      const live = stateOf(exec);            // 询问可能在等待期间写入，这里重新取一次
      resp.scope = live.scope;
      resp.scope_note = SCOPE_NOTE[live.scope];
      resp.depth_note = live.depth === "quick" ? "快速检索" : "深度检索";
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

  // ── 默认入库目录（DSH 收件箱）与"先问一次" ──────────────────────────────────
  // 为什么不把"弹目录框"塞进工具：MCP 客户端、TUI、远程会话都没有 picker，
  // 阻塞式弹框会把这些入口卡死。这里只做「问一句 + 兜底默认」，路径仍可用 paths 覆盖。
  const INGEST_EXTS = [".pdf", ".txt", ".md", ".docx"];

  function dshHomeDir() {
    const env = process.env.DSH_HOME;
    if (typeof env === "string" && env.trim().length > 0) return env.trim();
    try { return joinPath(os.homedir(), ".dsh"); } catch (e) { return ".dsh"; }
  }

  // %DSH_HOME%/kb-rag/inbox（Windows: C:\Users\<你>\.dsh\kb-rag\inbox）
  function defaultInboxDir() {
    return joinPath(dshHomeDir(), "kb-rag", "inbox");
  }

  // 数可入库文件（只认扩展名，跳过点开头项；上限防大目录拖慢调用）
  function countIngestible(dir, cap) {
    const limit = typeof cap === "number" && cap > 0 ? cap : 500;
    let n = 0;
    const stack = [{ dir: dir, depth: 0 }];
    while (stack.length > 0 && n < limit) {
      const cur = stack.pop();
      let ents;
      try { ents = readdirSync(cur.dir, { withFileTypes: true }); } catch (e) { continue; }
      for (const ent of ents) {
        if (ent.name.startsWith(".")) continue;
        const p = joinPath(cur.dir, ent.name);
        if (ent.isDirectory()) {
          if (cur.depth < 6) stack.push({ dir: p, depth: cur.depth + 1 });
          continue;
        }
        if (ent.isFile() && INGEST_EXTS.includes(extname(ent.name).toLowerCase())) n += 1;
        if (n >= limit) break;
      }
    }
    return n;
  }

  // 不传 paths 时：交互式会话问一次（默认收件箱 / 当前工作区 / 先不入库），
  // 问不了（无 userQuestions、超时、用户没答）就用默认收件箱。
  function pickIngestPaths(exec) {
    const inbox = defaultInboxDir();
    const workspace = workspaceOf(exec);
    const inboxN = countIngestible(inbox);
    const workspaceN = workspace === inbox ? 0 : countIngestible(workspace);
    const fallback = { kind: "inbox", paths: [inbox] };
    const uq = userQuestionsNow();
    // 两边都空时问了也没意义：直接走默认路径，由调用方给出"把文件放进这里"的说明。
    if (uq === undefined || (inboxN === 0 && workspaceN === 0)) return Promise.resolve(fallback);
    const options = [
      { label: "默认收件箱 · " + inboxN + " 篇", description: inbox },
      { label: "先不入库", description: "本轮不改动知识库" },
    ];
    if (workspaceN > 0) options.splice(1, 0, { label: "当前工作区 · " + workspaceN + " 篇", description: workspace });
    const request = {
      questions: [{
        id: "kb-ingest-dir",
        header: "入库目录",
        question: "这次入库哪个目录？（不选就用默认收件箱）",
        options: options,
      }],
    };
    if (exec && exec.agent !== undefined) request.agent = exec.agent;
    if (exec && exec.signal !== undefined) request.signal = exec.signal;
    // 工具内的询问必须绑定本次调用：前台等待由客户端卡片承载（askTimed 带 callId），
    // 没有 callId 或客户端没接住时返回 pending → 用默认值，绝不把调用挂死。
    const pending = { pending: true };
    const asked = (exec && exec.callId !== undefined && typeof uq.askTimed === "function")
      ? uq.askTimed(request, exec.callId, 30000)
      : Promise.race([uq.ask(request), ctx.timeout(30000).then(function () { return pending; })]);
    return Promise.resolve(asked).then(function (answer) {
      if (answer === null || answer === undefined || answer.pending === true) return fallback;
      const picked = answer.answers && answer.answers[0] && answer.answers[0].selected && answer.answers[0].selected[0];
      if (typeof picked !== "string") return fallback;
      if (picked.indexOf("先不入库") === 0) return { kind: "cancel", paths: [] };
      if (picked.indexOf("当前工作区") === 0) return { kind: "workspace", paths: [workspace] };
      return fallback;
    }).catch(function (e) {
      console.error("[kb-rag] ingest dir question failed:", String(e));
      return fallback;
    });
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
      // 曾试过 "serve", "--warm"（启动即预热模型）：实测更慢（13.2 s → 45.9 s），
      // 见 docs/BACKLOG.md「首问延迟」。别再原样加回来。
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

  // 宿主对 output.schema = {type:"json"} 做无损序列化；对象里带 undefined 值会导致
  // 整个工具调用失败（"value is not lossless JSON"）。这里递归剔除 undefined，
  // 与 JSON.stringify 的语义一致（对象属性丢弃、数组元素转 null）。
  const dropUndefined = (v) => {
    if (Array.isArray(v)) return v.map(dropUndefined);
    if (v && typeof v === "object") {
      const o = {};
      for (const k of Object.keys(v)) {
        if (v[k] !== undefined) o[k] = dropUndefined(v[k]);
      }
      return o;
    }
    return v;
  };
  // 软关闭（/kb off）时，10 个工具都返回同一个 {ok:true, kb_rag_disabled:true, scope, note} 壳。
  // 不统一拦的话：统计显示成"0 文档"、入库显示"完成 0/0/0"、检索显示"没有返回片段"——
  // 把"已关闭"误报成"空库/无结果"，用户会照着这个结论去排查一个不存在的故障。
  const closedBlock = (value) => [{
    type: "text",
    text: "**kb-rag 已关闭**" + (value.scope ? "（范围 " + String(value.scope) + "）" : "")
      + " —— " + String(value.note || "库内检索与统计均已停用，用 `/kb on` 开启。"),
  }];

  const renderJson = (_args, value) => {
    if (value !== null && typeof value === "object" && value.kb_rag_disabled === true) return closedBlock(value);
    return [{ type: "text", text: JSON.stringify(dropUndefined(value)) }];
  };

  // 大批量入库：引擎已转后台，返回的是 job 句柄而不是入库结果——不能按入库结果渲染。
  const renderIngestAsync = (_args, value) => {
    if (value === null || typeof value !== "object") return [{ type: "text", text: String(value) }];
    if (value.kb_rag_disabled === true) return closedBlock(value);
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
    if (value !== null && typeof value === "object" && value.kb_rag_disabled === true) return closedBlock(value);
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
    if (value.kb_rag_disabled === true) return closedBlock(value);
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
    // 「N 块 / 0 向量」必须给原因：以前只有 embedding=null，用户看不出向量根本没建，
    // 也不知道检索已经退化成纯关键词（issue #2）。
    if (value.embedding_error) {
      lines.push("⚠ 未建向量（嵌入模型不可用：" + String(value.embedding_error).slice(0, 160)
        + "）—— 本次只建了关键词索引，检索会降级为纯关键词。修好环境后重跑 kb_ingest 即可补齐缺失向量（无需全量重建）。");
    }
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
    // 软关闭时 wrapper 只回 {ok, kb_rag_disabled, scope, note}，没有 docs/chunks/vectors。
    // 以前直接落到统计行，渲染成"0 文档 / 0 块 / 0 向量"——把"已关闭"谎报成"空库"，最误导的一种输出。
    if (value.kb_rag_disabled === true) return closedBlock(value);
    const lines = [];
    lines.push("**知识库统计** · " + (value.docs || 0) + " 文档 / " + (value.chunks || 0) + " 块 / " + (value.vectors || 0) + " 向量");
    if (value.db) lines.push("数据库：" + value.db);
    // 模型缓存预检：README 承诺"没下载会说明"，但引擎早就在 stats 里回了 models，
    // 渲染层一直没输出，用户只能看到第一次检索莫名等十几秒。
    const models = value.models !== null && typeof value.models === "object" ? value.models : null;
    if (models !== null) {
      const bits = [];
      [["embed", "嵌入"], ["rerank", "精排"]].forEach(function (kv) {
        const m = models[kv[0]];
        if (m === null || typeof m !== "object") return;
        const label = kv[1] + " " + (m.name || "?");
        if (m.installed === true) bits.push(label + " ✓");
        else if (m.partial === true) bits.push(label + " ⚠ 下载不完整（重跑安装器或删缓存后重试）");
        else bits.push(label + " ✗ 未下载（首次检索时下载）");
      });
      if (bits.length > 0) lines.push("模型缓存：" + bits.join(" · "));
    }
    // 向量链路状态：缺失向量是"检索退化成纯关键词"的直接信号，以前完全没渲染（issue #2）
    if (typeof value.embedding === "string" && value.embedding.length > 0) lines.push("嵌入模型：" + value.embedding);
    if (value.embedding_error) lines.push("⚠ 嵌入模型加载失败：" + String(value.embedding_error).slice(0, 160));
    // 计算设备：装了 GPU 却没吃上、或 OOM 已回退 CPU —— 这两种情况以前完全看不出来
    const dev = value.device !== null && typeof value.device === "object" ? value.device : null;
    if (dev !== null) {
      const bits = [];
      if (dev.embed_device) bits.push("嵌入=" + dev.embed_device);
      if (dev.rerank_device) bits.push("精排=" + dev.rerank_device);
      if (dev.gpu) bits.push(dev.gpu);
      if (dev.cuda_available === false) bits.push("CUDA 不可用（torch " + (dev.torch || "?") + "）→ 装 CUDA 版 torch 可加速");
      else if (dev.cuda_available === true && !dev.embed_device) bits.push("CUDA 可用（模型未加载）");
      if (dev.note) bits.push(dev.note);
      if (bits.length > 0) lines.push("计算设备：" + bits.join(" · "));
    }
    const health = value.health && typeof value.health === "object" ? value.health : null;
    if (health !== null && (Number(health.missing_vecs) > 0 || Number(health.orphan_chunks) > 0)) {
      lines.push("⚠ 索引不完整：缺失向量 " + (health.missing_vecs || 0) + " 块 · 孤儿分块 " + (health.orphan_chunks || 0)
        + " —— 缺失向量的分块不参与向量检索；重跑 kb_ingest 可补齐，或用 kb_ingest(rebuild=true) 全量重灌。");
    }
    // 整篇不可检索：这些文档在搜索里等于不存在，以前完全没有信号（References 判定误吞正文的后果）
    if (health !== null && Number(health.docs_without_retrievable_chunks) > 0) {
      const sample = Array.isArray(health.blind_sample) && health.blind_sample.length > 0
        ? "（示例：" + health.blind_sample.slice(0, 3).join("、") + "）" : "";
      lines.push("⚠ " + health.docs_without_retrievable_chunks + " 篇文档没有任何可检索分块" + sample
        + " —— 这些文档**查不到**（通常是解析时整篇被误判为参考文献）；需 kb_ingest(rebuild=true) 全量重灌。");
    }
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
    if (value.kb_rag_disabled === true) return closedBlock(value);
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
    if (value.kb_rag_disabled === true) return closedBlock(value);
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

  // #5 一等输出物：把答案渲染成自包含 HTML（引擎侧确定性渲染，不经 LLM）。
  // fields 的键是模板的 section id，refs 用本次 evidence 的 1 基下标（越界会被拦下并写进 warnings）。
  const answerSchema = {
    type: "object",
    additionalProperties: false,
    description: "可选：把本次答案渲染成自包含单文件 HTML 并落盘（引擎确定性渲染，不经 LLM），路径随结果返回，浏览器打开即可阅读/打印/分享。",
    properties: {
      stage: { type: "string", enum: ["instant", "detail"], description: "instant=第一跳极简直答卡（自动套用 instant 模板、锁死三个字段、300 字符总预算）；detail=第二跳完整模板（默认）。只想一句话直答时用 instant，比 detail 便宜得多。" },
      template: { type: "string", enum: ["instant", "review", "method", "compare", "timeline", "concept", "dispute", "derivation", "qa", "numeric"], description: "预设格式：instant 直答卡 / review 综述 / method 方法（含流程链）/ compare 横向对比（三线表 + 数值条）/ numeric 数值发现（关键数字卡 + 数值条 + 变化曲线）/ timeline 时间线（有数值画曲线，没数值排事件）/ concept 概念速查 / dispute 争议清单 / derivation 推导 / qa 概念答疑。**省略时用会话默认**（`/kb format <模板>` 设定，未设则 review）。各模板的字段名见工具描述末尾的「answer 格式速查」。" },
      layout: { type: "string", enum: ["card", "doc"], description: "card=卡片式（默认，适合速读）；doc=长文式（推导、答疑更合适）。" },
      query: { type: "string", description: "标题（默认取本次检索的 query）。" },
      summary: { type: "string", description: "一句话结论（模板首位有 bottom_line 时优先用 fields.bottom_line）。" },
      fields: { type: "object", additionalProperties: true, description: "章节内容：键 = 模板的 section id（如 compare 的 bottom_line/matrix/numbers/reading）。表格直接写 markdown 管道表字符串；数值块给 {label,value} 数组即可 —— 轴界、刻度、条长、排序、热度、单位解析都由引擎计算，不要自己算。未填的节会自动省略，所以只填能回答问题的节。" },
      budget: { type: "object", additionalProperties: false, description: "整封字符预算（渲染端强制截断，超出会在 warnings 里说明）。", properties: { max_total_chars: { type: "integer", description: "整封字符上限。" } } },
      theme: { type: "object", additionalProperties: true, description: "配色覆盖（可选）：{palette:'blue|green|ink|warm', accent:'#RRGGBB', overrides:{'--accent':'#..'}, css:'任意 CSS'}。不传则读工作区 .kb-rag/answer-theme.json；显式传的优先。" },
      answer_dir: { type: "string", description: "答案落盘目录（默认 ~/.kb-rag/answers）。" },
    },
  };

  // 意图探针（probe=true）：只回标题级命中，供 agent 先看一眼"库里有什么"，再决定要不要
  // 问用户要多深。不返回正文、不生成答案 —— 这一步的成本必须显著低于一次完整回答，
  // 否则"先探后问"就变成了额外的等待。
  const PROBE_TOP_K = 5;
  const probeRows = (value) => (Array.isArray(value && value.results) ? value.results
    : (Array.isArray(value && value.evidence) ? value.evidence : []));
  const toProbe = (value) => {
    const rows = probeRows(value);
    return {
      ok: !(value && value.ok === false),
      probe: true,
      query: value && value.query ? String(value.query) : "",
      count: rows.length,
      verdict: (value && value.verdict) || null,
      no_hit: Boolean(value && value.no_hit === true),
      ms: value && typeof value.ms === "number" ? value.ms : null,
      mode_used: (value && value.mode_used) || null,
      cached: Boolean(value && value.cached === true),
      lang_note: (value && typeof value.lang_note === "string") ? value.lang_note : null,
      // 探针也是 scopeWrapped 的产物，scope/strict 提示必须一起带过来；以前只留 lang_note，
      // scope=both/web 或 strict 下"先探后问"会看不到范围约束，等于白探。
      scope: (value && value.scope) || null,
      scope_note: (value && typeof value.scope_note === "string") ? value.scope_note : null,
      strict: Boolean(value && value.strict === true),
      strict_note: (value && typeof value.strict_note === "string") ? value.strict_note : null,
      hits: rows.slice(0, PROBE_TOP_K).map(function (r) {
        return {
          title: r && (r.title || r.file) ? String(r.title || r.file) : "(无标题)",
          authors: r && typeof r.authors === "string" && r.authors.length > 0 ? r.authors : null,
          year: r && r.year ? r.year : null,
          doi: r && typeof r.doi === "string" && r.doi.length > 0 ? r.doi : null,
          section: r && r.section ? r.section : null,
        };
      }),
      closest: Array.isArray(value && value.closest) ? value.closest.slice(0, 5) : [],
    };
  };

  // 结构化来源投影（output.presentationMeta）：客户端半边（lib/client.js）用它渲染来源卡片。
  // 为什么不塞进 render 的文本里：ContentBlockMap 只有 text/reasoning/image/tool-call/tool-result，
  // 没有自定义块类型；结构化数据的**正规通道**是 presentationMeta → 客户端 props.block.meta。
  const sourcesMeta = (_args, value) => {
    if (value === null || typeof value !== "object") return null;
    if (value.probe === true) {
      return {
        probe: true,
        count: typeof value.count === "number" ? value.count : 0,
        ms: typeof value.ms === "number" ? value.ms : null,
        cached: value.cached === true,
        no_hit: value.no_hit === true,
        hits: Array.isArray(value.hits) ? value.hits : [],
      };
    }
    const list = Array.isArray(value.evidence) ? value.evidence : (Array.isArray(value.results) ? value.results : []);
    return {
      verdict: value.verdict === undefined ? null : value.verdict,
      no_hit: value.no_hit === true,
      max_score: value.max_score === undefined ? null : value.max_score,
      floor: value.floor === undefined ? null : value.floor,
      // 客户端卡片要根据判"无关"的那条闸门分情况说理由（见 lib/client.js noHitReason）：
      // 少了这两个字段，卡片只能无条件写"精排最高分 X"，而 missing 闸门压根没跑精排。
      relevance_rule: value.relevance_rule === undefined ? null : value.relevance_rule,
      missing: value.missing === undefined ? null : value.missing,
      mode_used: value.mode_used || null,
      cached: value.cached === true,
      // 卡片要显示"多快、哪一档"：ms 与 depth 在文本通道里早就写了（见 renderSources），
      // 但此前没有进 presentationMeta，客户端卡片只能显示"知识库来源"四个字。
      ms: typeof value.ms === "number" ? value.ms : null,
      depth: (value.depth === "quick" || value.depth === "deep") ? value.depth : null,
      closest: Array.isArray(value.closest) ? value.closest.slice(0, 5) : [],
      sources: list.slice(0, 10).map(function (r, i) {
        return {
          idx: i + 1,
          title: r && (r.title || r.file) ? String(r.title || r.file) : null,
          doi: r && typeof r.doi === "string" && r.doi.length > 0 ? r.doi : null,
          authors: r && typeof r.authors === "string" ? r.authors : null,
          year: r && r.year ? r.year : null,
          section: r && r.section ? r.section : null,
          score: r && typeof r.score === "number" ? r.score : null,
        };
      }),
    };
  };

  // 入库 / Zotero 迁移的结构化投影（kb_ingest、kb_zotero 共用）：
  // 这两个调用最久（分钟级或转后台），但 1.6.8 的客户端一张卡都没有——用户在聊天里只看到
  // 一句"已转入后台"的文本。卡片要显示的 job_id / 待处理篇数 / totals 由这里投影，
  // 不让客户端去正则解析 renderIngest 的散文。
  const ingestMeta = (_args, value) => {
    if (value === null || typeof value !== "object") return null;
    const totals = (value.totals !== null && typeof value.totals === "object") ? value.totals : {};
    const num = (k) => (typeof totals[k] === "number" ? totals[k] : null);
    return {
      // 与 renderIngest 判后台的规则一致：background 标记，或 job_id + running + 没有 totals。
      background: value.background === true
        || (value.job_id !== undefined && value.status === "running" && value.totals === undefined),
      job_id: value.job_id ? String(value.job_id) : null,
      status: value.status ? String(value.status) : null,
      pending_files: typeof value.pending_files === "number" ? value.pending_files : null,
      mode: value.mode === "metadata_only" ? "metadata_only" : null,
      dry_run: value.dry_run === true,
      candidates: typeof value.candidates === "number" ? value.candidates : null,
      ms: typeof value.ms === "number" ? value.ms : null,
      added: num("added"), updated: num("updated"), skipped: num("skipped"),
      duplicates: num("duplicates"), errors: num("errors"),
      meta_updated: num("meta_updated"), meta_changed: num("meta_changed"),
      chunks: num("chunks"), vectors: num("vectors"),
    };
  };

  // 范围 / 严格提示：`scopeWrapped` 一直把 `scope_note`、`strict_note` 挂在响应上，但渲染器
  // 此前只打印了 `strict` 这个布尔值（渲染成"· 严格模式"四个字），**note 文本全被丢掉** ——
  // 后果是 scope=both（库+全网兜底）时模型不知道还要去调 web_search、scope=web 时也不知道
  // 该以网络结果为准，两档范围等于没生效。工具描述明确承诺"返回的 scope/scope_note 指明当前范围"，
  // 这里把它兑现。**无命中分支（提前 return）与正常分支都要加**，否则恰恰是库外查询看不到范围提示。
  const scopeHintLines = (value) => {
    const out = [];
    if (typeof value.scope_note === "string" && value.scope_note.length > 0) out.push(value.scope_note);
    if (value.strict === true && typeof value.strict_note === "string" && value.strict_note.length > 0) {
      out.push(value.strict_note);
    }
    return out;
  };

  // #4 知识块起止标记：每一处渲染、**每一条分支**都要包，否则消费者无法可靠地把
  // "知识库给出的内容"与助手自己的话分开。验收正则（行锚定）：
  // ^=== KB (BEGIN|END) ===$，且成对出现。客户端卡片走结构化 presentationMeta，
  // 退回路径的正则只认 `N. [标题](链接)`，标记不会被误匹配。
  const KB_BEGIN = "=== KB BEGIN ===";
  const KB_END = "=== KB END ===";
  const kbWrap = function (text) { return KB_BEGIN + "\n" + text + "\n" + KB_END; };
  // 把 relevance_rule 翻译成人话：不能无条件写"精排最高分 X < 地板 Y" —— 3.3.0 起
  // missing 闸门判无关时压根没跑精排（quick 更从来没有 floor），那样会打印 "? < ?"。
  const noHitReason = function (value) {
    const rule = typeof value.relevance_rule === "string" ? value.relevance_rule : "";
    if (rule.indexOf("missing") === 0) {
      const n = (value.missing === null || value.missing === undefined) ? "?" : value.missing;
      return "查询的关键词在库内不存在（缺失 " + n + " 个实词）";
    }
    if (rule.indexOf("over_floor") === 0) {
      const lim = rule.split("<").pop().trim() || "?";
      return "精排后达到地板分的片段不足 " + lim + " 条";
    }
    const ms = value.max_score;
    const fl = value.floor;
    if (ms !== null && ms !== undefined && fl !== null && fl !== undefined) {
      return "精排最高分 " + ms + " < 地板 " + fl;
    }
    return "库内没有相关资料";
  };

  // 探针结果：只列"库里有什么"，并在结果里写死下一步口径，避免 agent 每次现编问题。
  const renderProbe = (value) => {
    const ms = typeof value.ms === "number"
      ? (value.ms < 1000 ? Math.round(value.ms) + "ms" : (value.ms / 1000).toFixed(1) + "s") : "";
    const lines = ["**意图探针** · 命中 " + value.count + " 篇" + (ms.length > 0 ? " · " + ms : "")
      + (value.cached === true ? " · 缓存命中" : "")];
    if (value.count === 0) {
      lines.push(value.no_hit === true ? "库里没有相关资料（这是结论，不是故障）。" : "库里没有返回片段。");
    } else {
      value.hits.forEach(function (h, i) {
        lines.push((i + 1) + ". " + h.title
          + (h.authors ? " · " + String(h.authors).split(";")[0] : "")
          + (h.year ? " · " + h.year : "")
          + (h.section ? " · §" + h.section : ""));
      });
      if (value.count < 2) lines.push("只有 1 篇：不要提供“对比”选项。");
    }
    if (typeof value.lang_note === "string" && value.lang_note.length > 0) {
      lines.push("");
      lines.push("提示：" + value.lang_note);
    }
    scopeHintLines(value).forEach(function (t) { lines.push(t); });
    lines.push("");
    if (value.count === 0) {
      // 0 命中是结论，不是"还没选粒度"：这时再让 agent 去问"一句话/对比表/综述"
      // 与上一条自相矛盾，agent 会照着问一个没有内容可答的问题。
      lines.push("下一步：不要问粒度、不要换词穷举。直接说明库里没有相关资料，"
        + "并按 scope 设置决定是否转 web_search（both）或以网络结果为准（web）。");
    } else {
      lines.push("下一步：用户没说粒度时，先用一句话说明命中 + 初步结论，再问一次"
        + "（一句话 / 对比表 / 路线或综述，带默认项）；用户选定后直接用 kb_rag(depth=deep)，"
        + "不要重新探针、不要重复问。");
    }
    return [{ type: "text", text: kbWrap(lines.join("\n")) }];
  };

  // 标题去 TeX/HTML：arXiv 标题常带 $\mathrm{...}$，PDF 标题常带 <sub>；工具文本里也得可读。
  function plainTitle(s) {
    return String(s || "")
      .replace(/<\/?[a-zA-Z][^>]*>/g, "")
      .replace(/\\(?:mathrm|text|textrm|mathbf|mathit)\s*\{([^{}]*)\}/g, "$1")
      .replace(/\$/g, "")
      .replace(/[_^]\{?([0-9])\}?/g, "$1")
      .replace(/[{}]/g, "")
      .replace(/\s+/g, " ").trim();
  }

  const renderSources = (_args, value) => {
    if (value !== null && typeof value === "object" && value.kb_rag_disabled === true) return closedBlock(value);
    if (value !== null && typeof value === "object" && value.probe === true) return renderProbe(value);
    if (value === null || typeof value !== "object") return [{ type: "text", text: kbWrap(String(value)) }];
    const items = Array.isArray(value.evidence) ? value.evidence : (Array.isArray(value.results) ? value.results : []);
    // 相关性地板判为"无关"时：结果为空是**结论**而不是故障。必须把理由与"库内最接近的几篇"
    // 一起说出来，否则 agent 会以为检索失败、换词穷举（实测那种循环是最大的体验问题）。
    if (items.length === 0 && value.no_hit === true) {
      const lines = [];
      lines.push("**库内无相关资料**（" + noHitReason(value) + "）");
      // 纪律分档必须在这里也生效：规则层在深挖档会跳过 stopRule，把"无命中"换成补库循环（见 guidance.js
      // 的 no-hit-thorough）；但这段渲染两种档位都会跑，曾经无条件写"不要换词反复重试"——与深挖指引对撞。
      if (value.__diligence === "thorough") {
        lines.push("深挖模式：这一轮没命中，**不要收尾**。下一步按顺序做：① 换术语或放宽 filters 再检索；"
          + "② 从已命中结果的 citations 里取 DOI，用 kb_fetch({ identifiers: [doi], ingest: true }) 补库后重查；"
          + "③ 用 related 列表横向扩展。");
      } else {
        lines.push("请如实说明库里没有相关资料，并按 scope 设置转 web_search；不要反复改写同一句话重试。");
      }
      scopeHintLines(value).forEach(function (t) { lines.push(t); });
      const close = Array.isArray(value.closest) ? value.closest : [];
      if (close.length > 0) {
        lines.push("");
        lines.push("库内最接近的 " + close.length + " 篇（供你判断是否真的无关，不要当答案引用）：");
        close.forEach(function (c, i) {
          lines.push("  " + (i + 1) + ". " + String(c.title || "(无标题)") + (c.year ? " · " + c.year : "")
            + (c.section ? " · §" + c.section : ""));
        });
      }
      return [{ type: "text", text: kbWrap(lines.join("\n")) }];
    }
    // 有 items 但两个数组都空之外的"空结果"：库为空或过滤条件过严 —— 是配置问题，不是判定结论。
    if (items.length === 0) {
      const elines = ["**知识库没有返回任何片段**"];
      if (typeof value.note === "string" && value.note.length > 0) elines.push(value.note);
      return [{ type: "text", text: kbWrap(elines.join("\n")) }];
    }
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
    // #7 第一跳：instant 卡片放在最前面（结论先行）——agent 直接转达即可，不必再组织语言。
    // 这条也是通道 C 的降级形态：宿主不支持打开文件时，用户在对话里照样看得到答案。
    if (value.answer_file !== null && value.answer_file !== undefined
        && typeof value.answer_file === "object" && typeof value.answer_file.card === "string"
        && value.answer_file.card.length > 0) {
      lines.push(value.answer_file.card);
      lines.push("");
    }
    lines.push("**知识库来源 Top-" + items.length + "**" + (quick ? "（快速检索）" : (value.depth === "deep" ? "（深度检索）" : "")));
    // 实际使用的检索路径（引擎会因 mode 参数或向量不可用而降级）：写死"混合检索"会误导
    const MODE_LABEL = { hybrid: "混合检索", keyword: "关键词检索", vector: "向量检索" };
    lines.push((MODE_LABEL[value.mode_used] || "混合检索") + (value.reranker ? " · 精排 " + value.reranker.split(" ")[0] : "") + (value.cached === true ? " · 缓存命中" : "") + (typeof value.ms === "number" ? " · " + value.ms + "ms" : "") + (value.strict === true ? " · 严格模式" : "") + (value.dup_collapsed > 0 ? " · 已折叠 " + value.dup_collapsed + " 份同论文副本" : ""));
    // 引用写法示例：把第一条来源拼成「标题, 年份」，模型照抄即可。
    // 标题是最强的“这是检索结果”信号；只给规则时模型仍会写 [n] 或 [n](DOI)。
    try {
      const c0 = items[0] || {};
      const a0 = typeof c0.authors === "string" && c0.authors.trim()
        ? String(c0.authors).split(";")[0].trim() : "";
      const t0 = typeof c0.title === "string" && c0.title.trim() ? c0.title.trim() : "";
      const y0 = c0.year ? String(c0.year) : "年份未记录";
      const title0 = t0.length > 40 ? t0.slice(0, 40) + "…" : t0;
      const label0 = [title0 || a0 || "标题未记录", y0].join(", ");
      const cite0 = c0.doi ? "[" + label0 + "](https://doi.org/" + c0.doi + ")" : "（" + label0 + "）";
      lines.push("引用写法：正文里写 `[标题, 年份](DOI)`，例如 " + cite0 + "；不要只写 `[2]` 或把编号当链接文字。");
    } catch (e) { /* 示例是锦上添花，拼不出来不影响来源列表 */ }
    // 范围/严格提示放在来源之前：模型自上而下读，先看到"当前范围是什么、还要不要联网"
    scopeHintLines(value).forEach(function (t) { lines.push(t); });
    // 引擎的语言提示（中文查询 + 几乎全英文库）：原样转达，提醒用英文术语重查
    if (typeof value.lang_note === "string" && value.lang_note.length > 0) {
      lines.push("提示：" + value.lang_note);
    }
    // 引擎的降级原因（如"向量索引缺失，降级为纯关键词"）以前被丢弃：用户只看到检索方式从
    // 混合变成关键词，不知道原因。只在含"降级"时透传，避免重复"命中 N 块，返回 Top-N"这类常规文案。
    if (typeof value.note === "string" && value.note.indexOf("降级") >= 0) {
      lines.push("提示：" + String(value.note).replace(/命中 \d+ 块，返回 Top-\d+/, "").trim());
    }
    // 弱相关：给一次明确的升级机会（深查），而不是让 agent 自由发挥式重试
    if (value.verdict === "弱相关") {
      lines.push("提示：本次结果相关性偏弱（精排最高分 " + (value.max_score === undefined ? "?" : value.max_score)
        + " < " + (value.floor_weak === undefined ? "0.35" : value.floor_weak) + "）。最多再升一次 depth=deep；仍弱就按「库内无资料」处理。");
    }
    items.forEach(function (r, i) {
      const rawTitle = plainTitle(r.title || r.file || "");
      // 长标题（例如审稿意见文件把整段正文当标题）在列表里会占满屏；展示层截到首句/90 字。
      // 完整值仍在结构化来源（sourcesMeta）里，不影响引用与后续查询。
      const title = rawTitle.length > 90
        ? (rawTitle.split(/[。.]\s/)[0].slice(0, 90).replace(/[\s,;:，；：、]+$/, "") + "…")
        : rawTitle;
      const doi = typeof r.doi === "string" && r.doi.length > 0 ? r.doi : null;
      const t = doi !== null ? "[" + title + "](https://doi.org/" + doi + ")" : title;
      const authorShort = typeof r.authors === "string" && r.authors.length > 0
        ? String(r.authors).split(";").map(function (s) { return s.trim(); }).filter(Boolean).slice(0, 2).join("; ")
        : "";
      const journal = typeof r.journal === "string" && r.journal.trim().length > 0 ? r.journal.trim() : "";
      const year = r.year === undefined || r.year === null || r.year === "" ? "" : String(r.year);
      // 审稿意见/编辑决定信没有期刊，明确标出来，别让它冒充论文
      const isReport = /peer[- ]?review|reviewers?'? (comments|reports?)|decision letter/i.test(rawTitle);
      // 引用要素按"可信度"排序：期刊 → 年份 → 作者 → 章节。
      // 期刊与年份**永远出现**（缺失写"未记录"），这样每条来源都能一眼判断可信度；
      // 作者缺失时省略，避免每行都拖一串"作者未记录"。
      const cite = [
        journal || (isReport ? "审稿意见（非期刊文献）" : "期刊未记录"),
        year || "年份未记录",
        authorShort || null,
        r.section ? ("§" + r.section) : null,
      ].filter(Boolean).join(" · ");
      lines.push("");
      lines.push((i + 1) + ". " + t + " — " + cite);
        lines.push("> " + String(r.snippet || "").slice(0, quick ? 200 : 280).replace(/\n/g, " "));
        if (quick) {
          // 快速检索：不带图注/引文链；无 DOI 时补文件名与可点击检索链接，便于继续追查
          if (doi === null) {
            const qk = String(r.search || r.title || "").trim().slice(0, 200);
            const encK = qk.length > 0 ? encodeURIComponent(qk) : "";
            lines.push("无 DOI · 文件：" + String(r.file)
              + (encK ? " · [Semantic Scholar](https://www.semanticscholar.org/search?q=" + encK + ")"
                + " · [Crossref](https://search.crossref.org/?q=" + encK + ")" : ""));
          }
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
        // 元数据不全的文献（审稿意见、补充材料、扫描件）也要能"顺藤摸瓜"：
        // 给可点击的 Semantic Scholar / Crossref 检索链接，而不是只给一串要手工复制的文字。
        const sq = String(r.search || r.title || "").trim().slice(0, 200);
        if (sq.length > 0) {
          const enc = encodeURIComponent(sq);
          lines.push("↳ 检索：" + "[Semantic Scholar](https://www.semanticscholar.org/search?q=" + enc + ")"
            + " · [Crossref](https://search.crossref.org/?q=" + enc + ")");
          lines.push("↳ 检索串（可复制）：" + sq.slice(0, 120));
        }
      }
      // 这里曾经无条件再推一行 r.path：上一行已经印了"文件：<基名>"，绝对路径纯属重复，
      // 还每篇来源泄露一次本机目录结构。定位文件用基名 + 库根即可（kb_stats 里有库路径）。
      if (typeof r.zotero_key === "string" && r.zotero_key.length > 0) {
        lines.push("[在 Zotero 中打开 PDF](zotero://open-pdf/library/items/" + r.zotero_key + ")");
      }
    });
    // #5 通道 C：引擎已把答案渲染成自包含 HTML（只在传 answer 参数时出现）。
    // 抽成函数是为了 quick 分支也能打印路径 —— 之前 quick 在下面提前 return，
    // 结果"HTML 生成了但路径永远不显示"，只有 deep 才看得到（B6）。
    function pushAnswerFile() {
      const af = value.answer_file;
      if (af === null || af === undefined || typeof af !== "object") return;
      lines.push("");
      if (af.ok === true && typeof af.path === "string") {
        const clipped = Array.isArray(af.warnings) && af.warnings.length > 0
          ? " · 已按上限裁剪 " + af.warnings.length + " 处" : "";
        lines.push("**HTML 答案已生成**（自包含单文件，直接打开 / 打印 / 分享均可）：" + af.path
          + " · " + Math.round((af.bytes || 0) / 1024) + " KB · 模板 " + String(af.template || "") + clipped);
        lines.push("（浏览器打开该文件即可；同一信封重复渲染落到同一文件，不会堆垃圾）");
      } else {
        lines.push("**HTML 渲染失败**：" + String(af.error || "未知原因").slice(0, 200));
      }
    }
    // 收尾声明放在工具输出**最后**：模型对最后一条指令的遵循率明显高于开头那条。
    function pushLastLineNote() {
      lines.push("");
      lines.push("**回答最后一行必须逐字写**：`以上内容全部由 kb-rag 查询反馈信息整理。`"
        + "（有超出库内证据的推断时写：`以上内容全部由 kb-rag 查询反馈信息整理，推断部分为模型补充。`）");
    }
    if (quick) {
      pushAnswerFile();
      lines.push("");
      lines.push("（快速检索：直接输出查到的信息即可，一两句话答完，无需展开分析；需要深入背景时用 depth=deep 重查）");
      pushLastLineNote();
      return [{ type: "text", text: kbWrap(lines.join("\n")) }];
    }
    if (Array.isArray(value.related) && value.related.length > 0) {
      lines.push("");
      lines.push("**关联文献（可作补充建议）**");
      value.related.forEach(function (r) {
        const doi = typeof r.doi === "string" && r.doi.length > 0 ? r.doi : null;
        const t = doi !== null
          ? "[" + plainTitle(r.title || r.file || "") + "](https://doi.org/" + doi + ")"
          : plainTitle(r.title || r.file || "");
        const meta = [
          typeof r.authors === "string" && r.authors.length > 0 ? String(r.authors).split(";").map(function (s) { return s.trim(); }).filter(Boolean).slice(0, 2).join("; ") : null,
          r.year,
          r.journal,
        ].filter(Boolean).join(" · ");
        lines.push("- " + t + (meta.length > 0 ? " — " + meta : "") + "（" + String(r.reason || "内容相关") + "）");
      });
    }
    // #5 通道 C：引擎已把答案渲染成自包含 HTML（只在传 answer 参数时出现）。
    // 路径要原样给出（用户复制到浏览器就能开），另附体积与模板，便于判断值不值得点开。
    pushAnswerFile();
    pushLastLineNote();
    return [{ type: "text", text: kbWrap(lines.join("\n")) }];
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

  // 10 个工具集中注册：包成函数是为了支持**硬关闭**（运行时撤掉再恢复，不用重启 DSH）
  function registerAllTools() {
  reg(tool({
    name: "kb_ingest",
    description: "把本地文档（PDF/TXT/MD/DOCX）导入 DSH 知识库并建立索引（轻量 RAG 工作流的入库步骤）。支持单个文件或目录（递归扫描并只处理 PDF/TXT/MD/DOCX）；按章节切分并抽取元数据（标题/作者/年份/DOI）；同时用本地 bge-small 模型生成向量（数据持久化在工作区/.kb）。已入库且内容未变的文件自动跳过；同一内容（sha256 相同）在其他路径已入库时标记为 duplicate 跳过（增量）。paths 用工作区内的相对路径或绝对路径。**省略 paths 时入默认收件箱 <DSH_HOME>/kb-rag/inbox**（Windows 通常是 C:\\Users\\<你>\\.dsh\\kb-rag\\inbox）：交互式会话会先问一次（默认收件箱 / 当前工作区 / 先不入库），问不了（MCP、TUI、超时）就直接用默认收件箱；收件箱为空时明确报错并给出该路径，不会假装入库成功。入库后用 kb_search 检索、kb_rag 问答、kb_stats 看统计。重复调用安全。metadata_only=true 只刷新元数据（秒级，不重切块/不重嵌入，适合引擎升级后让老库的标题/作者/DOI 生效）；rebuild=true 原地重灌库内全部已入库文档（不会因传目录而重复入库）；大批量会自动转后台并返回 job_id，用 kb_status 轮询。",
    parameters: {
      // paths 与 rebuild 二选一：rebuild=true 时引擎按库内现有路径重灌，不需要 paths；
      // 都不传时走默认收件箱（先问一次，问不了用默认）。
      paths: { type: "array", items: { type: "string" }, description: "要入库的文件或目录路径列表。省略时入默认收件箱 <DSH_HOME>/kb-rag/inbox，交互式会话会先问一次；rebuild=true 时也不需要它。" },
      kb_root: { type: "string", description: "知识库目录（默认：工作区下的 .kb）。" },
      force: { type: "boolean", description: "true 时强制重新解析并重新编码向量（默认 false）。" },
      metadata_only: { type: "boolean", description: "true 时只刷新元数据（重抽标题/作者/年份/期刊/DOI，秒级；不重切块、不重嵌入；内容已变的文件不动）。" },
      rebuild: { type: "boolean", description: "true 时原地重灌库内全部已入库文档（路径取自库内，可省略 paths）。大批量会自动转后台并返回 job_id。" },
    },
    output: { schema: { type: "json" }, render: withNotes("kb_ingest", renderIngest), presentationMeta: ingestMeta },
    timeoutMs: 1800000,
    execute(args, exec) {
      const base = {
        kb_root: kbRootOf(args, exec),
        force: args.force === true,
        metadata_only: args.metadata_only === true,
        rebuild: args.rebuild === true,
        async_if_large: true,
      };
      if (args.rebuild === true || (Array.isArray(args.paths) && args.paths.length > 0)) {
        return runEngine("ingest", Object.assign({ paths: args.paths }, base), exec);
      }
      // 没给路径：先问一次再用默认收件箱（见 pickIngestPaths）。
      return pickIngestPaths(exec).then(function (pick) {
        if (pick.kind === "cancel") {
          throw new Error("用户选择先不入库：本轮未改动知识库。收件箱：" + defaultInboxDir());
        }
        const dir = pick.paths[0];
        if (countIngestible(dir, 1) === 0) {
          throw new Error("入库目录为空或不存在：" + dir
            + "（只处理 PDF/TXT/MD/DOCX）。把文件放进这个目录，或用 paths 指定别处。");
        }
        return runEngine("ingest", Object.assign({ paths: [dir] }, base), exec);
      });
    },
  }));

  reg(tool({
    name: "kb_search",
    description: "在知识库中做混合检索（关键词 BM25 + 向量余弦，RRF 融合，×章节权重），返回最相关片段及精确来源（文件/标题/作者/年份/期刊/DOI/章节）。想在已入库文档中查找事实、数据或术语时优先于直接读文件（更省 token）。depth 双模式：quick（默认）=快速检索，混合召回直出、跳过精排与引文扩展，亚秒级响应，适合事实性查询；工具返回后立即作答，不展开背景与延伸分析；deep=深度检索，bge-reranker 精排 + 引文链 + 关联文献（适合领域调研与综述性问题）。query 的语言要和库内正文一致：英文库先把中文问句转写成英文术语串（BM25 对中文空转，只靠向量侧跨语言匹配会明显变差）；中文库直接用中文术语；写法为 3–12 个词，结构「材料/体系 + 方法/工艺 + 性质/表征」（如 \"graphene CVD copper single crystal nucleation suppression\"），不要用整句问句，年份/期刊/作者请放 filters，需要中文文献时用用户原话另发一条中文查询；引擎按原样检索，不会替你翻译；mode 可选 keyword/vector/hybrid（默认 hybrid）；filters 支持 authors/year/section/title/journal/kind 元数据预过滤（year 可用 \">=2020\" 形式）；其中 journal 目前只由 Zotero 迁移填充，kb_ingest 入库的文档该字段为 NULL，用它过滤通常零命中。查询范围由会话开始时的范围询问或 kb_scope 工具控制；返回的 scope/scope_note 指明当前范围。strict 可选（true=严格模式：答案仅基于本次结果，禁止库外知识/常识外延；默认继承 kb_scope 设置）。回答用户时必须标注来源：正文引用统一写 `[文献标题, 年份](https://doi.org/DOI)`（用来源字段里的 doi，保证能点开；标题超过 40 字截断加「…」，年份缺失写「年份未记录」）；无 DOI 时写纯文本 `（标题, 年份, 文件名）`。**不要**只写 `[n]` 或把 `[n]` 当链接文字。无命中时先检查是否已入库（kb_stats）。相同查询命中缓存，零重计算。",
    parameters: {
      query: { type: "string", required: true, description: "检索词：3–12 个术语，语言与库内正文一致（英文库用英文术语串，中文库用中文术语），结构「材料/体系 + 方法/工艺 + 性质/表征」（如 \"graphene CVD copper single crystal nucleation suppression\"）；限定条件放 filters；引擎按原样检索、不翻译。需要中文文献时用中文原话另发一条查询。" },
      depth: { type: "string", enum: ["quick", "deep"], description: "quick=快速检索（默认：无精排/引文链/关联文献，响应最快，适合查个信息）；deep=深度检索（精排+引文链+关联文献，适合领域调研与综述性问题）。默认继承 kb_scope 的会话 depth 设置。" },
      top_k: { type: "integer", description: "返回结果数（默认 quick 3 / deep 5，上限 10）。" },
      snippet: { type: "integer", description: "片段长度字符数（默认 quick 300 / deep 400）。" },
      mode: { type: "string", enum: ["keyword", "vector", "hybrid"], description: "检索模式（默认 hybrid）。" },
      rerank: { type: "boolean", description: "是否启用 bge-reranker-base 精排（默认 quick 关 / deep 开）。短术语查询想要更准时传 true，代价约 +0.5–1.5s。" },
      related: { type: "boolean", description: "true 时附带 related 关联文献列表（默认 quick 关 / deep 开，供补充建议引用）。" },
      strict: { type: "boolean", description: "严格模式：true 时答案仅基于本次检索结果，禁止补充库外知识/常识外延（默认继承 kb_scope 的 strict 设置）。" },
      kb_root: { type: "string", description: "知识库目录（默认：工作区下的 .kb）。" },
      probe: { type: "boolean", description: "true 时只做意图探针：快速召回 Top-5 的标题/作者/年份，不返回正文、不生成答案（约 0.3s）。用于“开放问题但用户没说要多深”时先看一眼库里有什么、再决定是否问用户；事实问题不要用它。" },
      filters: filterSchema,
    },
    output: { schema: { type: "json" }, render: withNotes("kb_search", renderSources), presentationMeta: sourcesMeta },
    presentCall: presentQueryCall,
    execute(args, exec) {
      const strict = args.strict === undefined ? stateOf(exec).strict : args.strict === true;
      const root = kbRootOf(args, exec);
      if (args.probe === true) {
        const probe = runEngine("search", {
          query: args.query,
          depth: "quick",
          top_k: args.top_k === undefined ? PROBE_TOP_K : args.top_k,
          snippet: 40,
          mode: args.mode,
          filters: args.filters,
          kb_root: root,
        }, exec).then(toProbe);
        return scopeWrapped(exec, probe, strict, root);
      }
      const call = runEngine("search", {
        query: args.query,
        depth: args.depth === undefined ? stateOf(exec).depth : args.depth,
        top_k: args.top_k,
        snippet: args.snippet,
        mode: args.mode,
        rerank: args.rerank,
        related: args.related,
        filters: args.filters,
        kb_root: root,
      }, exec);
      return scopeWrapped(exec, call, strict, root);
    },
  }));

  reg(tool({
    name: "kb_rag",
    description: "在知识库中检索证据片段供当前模型直接作答：基于 evidence 回答问题，每个事实后写 `[文献标题, 年份](https://doi.org/DOI)`（标题/年份/DOI 都取对应 evidence 条目；标题超过 40 字截断加「…」，年份缺失写「年份未记录」）。DOI 为 null 时写纯文本 `（标题, 年份, 文件名）`（文件名过长截到约 60 字符）。**不要**把引用写成 `[n]`、`[n](DOI)` 或只写数字——`[n]` 只用于来源列表编号和 `answer.fields.refs`，正文里出现即为格式错误。depth 双模式：quick（**默认**，第一跳）=快速检索，仅基于少量证据直接作答，不展开论述；deep（第二跳）=深度检索，重排序 + 引文关联 + 相关文献全链路，回答可综合多篇展开论述（适合领域调研）。strict 可选（true=严格模式：仅基于 evidence 作答，禁止补充库外知识/常识外延或未出现在 evidence 中的文献数据，证据不足直接说明无法回答；默认继承 kb_scope 设置，当前默认 false）。资料不足时明确回答\"根据现有资料无法回答\"；多源冲突时分别列出并说明来源。答案末尾的补充建议按来源分三列（哪列为空就整列省略）：①「库内可查（循引文找到）」——citations 里标 [库内] 的文献，必须写出关系链「《被引文献》(作者, 年份) 被 [证据编号] 的引文 Ref n 引用，已在库内可直接提问」；②「建议补库（循引文发现）」——citations 未命中库内的条目，注明被 Ref n 引用、尚不在库内，可用 Ref 编号定位下载；③「相关文献」——related 列表（同作者/同期刊/主题相似的库内文献，元数据相似）。每条推荐的理由必须写明属于哪种，引文关联的必须带关系链，不得混列；若库内缺少关键资料，明确指出应补充哪些文献/主题（用户重视此提示）。回答**最后一行**固定写「以上内容全部由 kb-rag 查询反馈信息整理。」（有超出库内证据的推断就写「……整理，推断部分为模型补充。」）。这是知识库 RAG 问答的唯一入口；查询范围由会话开始时的范围询问或 kb_scope 工具控制。",
    parameters: {
      query: { type: "string", required: true, description: "要回答的问题：请先转写成 3–12 个检索词，语言与库内正文一致（英文库用英文术语，中文库用中文术语；不要整句中文问句）。引擎按原样检索、不替你翻译。" },
      depth: { type: "string", enum: ["quick", "deep"], description: "quick=快速检索（**默认**，第一跳：少量证据直接给答案，不展开）；deep=深度检索（第二跳：精排+引文链+关联文献，回答展开背景，适合跨文献综合）。默认继承 kb_scope 的会话 depth 设置。" },
      top_k: { type: "integer", description: "证据条数（默认 quick 2 / deep 3，上限 10）。" },
      rerank: { type: "boolean", description: "是否启用精排（默认 quick 关 / deep 开）。短术语查询想要更准时传 true，代价约 +0.5–1.5s。" },
      related: { type: "boolean", description: "true 时附带 related 关联文献列表供补充建议引用（默认 quick 关 / deep 开）。" },
      strict: { type: "boolean", description: "严格模式：true 时仅基于 evidence 作答，禁止库外知识补充（默认继承 kb_scope 的 strict 设置）。" },
      kb_root: { type: "string", description: "知识库目录（默认：工作区下的 .kb）。" },
      probe: { type: "boolean", description: "true 时只做意图探针（走 search）：快速召回 Top-5 的标题/作者/年份，不返回证据正文、不生成答案（约 0.3s）。用于“开放问题但用户没说要多深”时先看一眼库里有什么；确认粒度后再用正常调用作答。" },
      filters: filterSchema,
      answer: answerSchema,
    },
    output: { schema: { type: "json" }, render: withNotes("kb_rag", renderSources), presentationMeta: sourcesMeta },
    presentCall: presentQueryCall,
    execute(args, exec) {
      const strict = args.strict === undefined ? stateOf(exec).strict : args.strict === true;
      const root = kbRootOf(args, exec);
      if (args.probe === true) {
        const probe = runEngine("search", {
          query: args.query,
          depth: "quick",
          top_k: args.top_k === undefined ? PROBE_TOP_K : args.top_k,
          snippet: 40,
          mode: "hybrid",
          filters: args.filters,
          kb_root: root,
        }, exec).then(toProbe);
        return scopeWrapped(exec, probe, strict, root);
      }
      // 先等一次持久化默认值（/kb save 写进 state.json 的 scope/depth/format 等）再决定参数。
      // 否则新会话/重启后的**第一次**调用总是用内置默认：state 的异步加载比 execute 晚到，
      // `/kb format compare` 会看起来"设了但第一问不生效"。loadPersistedDefaults 自身缓存
      // 那个 promise，所以后续调用零成本。
      return loadPersistedDefaults(root, exec).then(function () {
        backfillPersisted(stateOf(exec));
        const call = runEngine("rag", {
          query: args.query,
          depth: args.depth === undefined ? stateOf(exec).depth : args.depth,
          top_k: args.top_k,
          rerank: args.rerank,
          related: args.related,
          filters: args.filters,
          answer: applyFormatDefault(exec, args.answer),
          kb_root: root,
        }, exec);
        return scopeWrapped(exec, call, strict, root);
      });
    },
  }));

  reg(tool({
    name: "kb_zotero",
    description: "把本地 Zotero 文献库中带 PDF 附件的文献批量迁移到知识库（轻量 RAG 工作流的 Zotero 接口）。读取 zotero.sqlite（默认自动定位 ~/Zotero、~/Documents/Zotero、%APPDATA% 配置；找不到时用 zotero_db 显式指定），解析每篇文献的元数据（标题/作者/年份/期刊/DOI）与 PDF 附件路径（storage 目录），逐篇解析入库并生成向量；已入库附件自动跳过，重复内容标记 duplicate 跳过（增量，可反复运行）。附件文件本体缺失的条目标记为 missing 并跳过（不尝试下载）。dry_run=true 时只列候选不写入；limit 限制迁移条数。",
    parameters: {
      zotero_db: { type: "string", description: "zotero.sqlite 显式路径（默认自动定位）。" },
      kb_root: { type: "string", description: "知识库目录（默认：工作区下的 .kb）。" },
      limit: { type: "integer", description: "迁移条数上限（默认全部）。" },
      force: { type: "boolean", description: "true 时强制重新解析已入库附件（默认 false）。" },
      dry_run: { type: "boolean", description: "true 时只列候选文献，不导入（默认 false）。" },
    },
    output: { schema: { type: "json" }, render: withNotes("kb_zotero", renderIngest), presentationMeta: ingestMeta },
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

  reg(tool({
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

  reg(tool({
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

  reg(tool({
    name: "kb_fetch",
    description: "按 DOI / arXiv ID 把论文 PDF 下载到本地目录（默认 ~/.kb-rag/downloads，可用 target_dir 覆盖）。按标准元标签与公开 API 解析地址，顺序为：arXiv 直连 → 出版商正式版（落地页 citation_pdf_url；在校园网/机构订阅网络下可直接取得订阅版 PDF，无需额外配置）→ 落地页内常见 pdf 链接 → 开放获取兜底（Unpaywall / Crossref）。只做常规抓取，不绕过付费墙、不访问 Sci-Hub、不伪造凭据。下载后不会自动进 Zotero——需用户手动在 Zotero 里「文件→添加文件」或拖入该目录 PDF 入库。落盘结构为 <目录>/<来源>/<年份>/<标题>__<内容哈希8>.pdf，并在 <目录>/manifest.json 维护索引（同一内容自动去重；旧版平铺文件保留原位，首次建索引时标记为 legacy）。",
    parameters: {
      identifiers: { type: "array", required: true, items: { type: "string" }, description: "DOI 或 arXiv ID 列表（如 10.5555/12345679 或 arXiv:2401.00001）。" },
      target_dir: { type: "string", description: "下载目录（默认 ~/.kb-rag/downloads）；文件按 <来源>/<年份>/ 分层存放。" },
      ingest: { type: "boolean", description: "true 时下载完直接入库到知识库（等价于紧接着调一次 kb_ingest；增量入库按 sha256 自动跳过已入库文件，重复调用安全）。深挖模式里用它减少往返。" },
    },
    output: { schema: { type: "json" }, render: renderFetch },
    timeoutMs: 300000,
    execute(args, exec) {
      askNetworkOnce(exec && exec.agent, exec);
      const network = { env: netEnv, proxy: { env: envProxyDetect() } };
      return runEngine("fetch", {
        identifiers: args.identifiers, target_dir: args.target_dir, network: network,
        // ingest=true：下载即入库（"深挖模式"里"找文献 → 入库 → 再查"少一次往返）
        ingest: args.ingest === true, kb_root: kbRootOf(args, exec),
      }, exec);
    },
  }));

  reg(tool({
    name: "kb_scope",
    description: "设置/查看知识库查询范围、回答深度与严格模式（**按会话隔离**；会话开始时也会询问一次范围）：scope：kb=仅封闭知识库；both=知识库+全网（kb 检索 + web_search 补充）；web=仅全网。depth 可选：quick=快速检索（亚秒级响应，直出结果）；deep=深度检索（重排序+引文关联全链路，跨文献综合论述）。strict 可选：true=严格模式（答案仅基于库内证据，禁止库外知识/常识外延）；false=关闭（默认 false）。diligence 可选：normal=默认（检索有调用上限、无命中即停）；thorough=**深挖模式**（用户明确要求「仔细找/慢慢来/别省时间/把相关文献都找齐」时设置：解除调用上限，允许「反复检索 → kb_fetch 补库 → 引文关联 → 增量入库 → 再查」的循环）。用户说\"封闭库/全网/都要/严格只按库内/快速检索/深度检索/深挖\"等要求时，调本工具设定后再检索。",
    parameters: {
      scope: { type: "string", enum: ["kb", "both", "web"], description: "kb=仅封闭库；both=知识库+全网；web=仅全网。不传则只查看当前设置。" },
      depth: { type: "string", enum: ["quick", "deep"], description: "可选：同时设置检索深度。quick=快速检索；deep=深度检索。" },
      strict: { type: "boolean", description: "可选：同时设置严格模式。true=仅基于库内证据作答；false=关闭（默认）。" },
      diligence: { type: "string", enum: ["normal", "thorough"], description: "可选：检索纪律。normal=默认（≤3 次调用、无命中即停）；thorough=深挖（用户明确要求彻底查找时用：不限调用次数，允许补库循环）。" },
      save: { type: "boolean", description: "可选：true 时把本次设置写进工作区默认值（<工作区>/.kb-rag/state.json），以后新会话直接生效。" },
    },
    output: { schema: { type: "json" }, render: renderJson },
    execute(args, exec) {
      const st = stateOf(exec);
      // scope 是可选的：不传时本工具只返回当前会话设置（描述里承诺了"设置/查看"）
      const patch = {};
      if (args.scope !== undefined) { st.scope = args.scope; patch.scope = args.scope; }
      if (args.depth !== undefined) { st.depth = args.depth === "deep" ? "deep" : "quick"; patch.depth = st.depth; }
      if (args.strict !== undefined) { st.strict = args.strict === true; patch.strict = st.strict; }
      if (args.diligence !== undefined) {
        st.diligence = args.diligence === "thorough" ? "thorough" : "normal";
        patch.diligence = st.diligence;
      }
      if (args.save === true && Object.keys(patch).length > 0) {
        savePersistedDefaults(patch, kbRootOf(args, exec), exec);
      }
      console.log("[kb-rag] query scope:", st.scope, "depth:", st.depth, "strict:", st.strict,
        "diligence:", st.diligence, "| session:", sessionKey(exec));
      return Promise.resolve({
        ok: true, scope: st.scope, depth: st.depth, strict: st.strict, diligence: st.diligence,
        saved: args.save === true && Object.keys(patch).length > 0,
        session: sessionKey(exec),
        scope_note: SCOPE_NOTE[st.scope],
        depth_note: st.depth === "quick" ? "快速检索：kb_search 默认快速检索，kb_rag 可显式 depth=quick" : "深度检索：kb_search/kb_rag 均默认深度检索",
        strict_note: st.strict ? STRICT_NOTE : "",
        diligence_note: st.diligence === "thorough"
          ? "深挖模式：不设检索调用上限；库内不足时按「kb_fetch 补库（ingest=true 可直接入库）→ 引文关联 → 增量入库 → 再查」循环，并把每轮新增/仍缺什么告诉用户。"
          : "默认纪律：一次提问最多 3 次检索，每次换实质策略；无命中就如实说明，不要反复重试。",
      });
    },
  }));

  reg(tool({
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

  reg(tool({
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

  console.log("[kb-rag] static tools registered (v1.6.8): kb_ingest / kb_search / kb_rag / kb_zotero / kb_dedup / kb_clear / kb_fetch / kb_scope / kb_stats / kb_status");
  }   // registerAllTools()

  registerAllTools();

  // ── /kb 命令（#9）：显式开关与状态可见性 ──────────────────────────────────
  // direct UI handler：不进模型、天然按 invocation.agent 会话隔离（CommandInvocation.agent）。
  // 三档关闭语义：软关闭（enabled=false，工具在但调用即返回"已关闭"，不拉守护进程，默认）/
  // 硬关闭（撤掉工具注册，运行时生效、无需重启）/ 半关闭（只关检索，保留入库与统计）。
  const SEARCH_TOOLS = ["kb_search", "kb_rag"];
  const TOTAL_TOOLS = 10;
  function disposeTools(pred) {
    const kept = [];
    toolDisposers.forEach(function (d) {
      if (pred(d.name)) { try { d.dispose(); } catch (e) { /* 已撤销则忽略 */ } }
      else kept.push(d);
    });
    toolDisposers.length = 0;
    kept.forEach(function (d) { toolDisposers.push(d); });
  }
  function toolsRegistered() { return toolDisposers.length; }

  // commands 是**可选**服务：只能用 ctx.get 做可选查询，不能写成 ctx.commands ——
  // Cordis 的 Guard 规定属性访问 ctx.commands 必须先声明 inject: ['commands']，而声明成
  // 硬依赖又会让本插件在缺少该服务时永远 park。写法与 plugin/host.js 的动态半边保持一致。
  const commands = ctx.get("commands");
  if (commands !== undefined && typeof commands.register === "function") {
    commands.register({
      name: "kb",
      description: "kb-rag 状态与控制：on / off / status / kb / both / web / quick / deep / strict / format / thorough / normal / save / policy",
      input: { hint: "status | on | off | soft | hard | kb | both | web | quick | deep | strict on|off | format <模板|auto> | thorough | normal | save | policy" },
      handler: function (invocation) {
        const exec = { agent: invocation && invocation.agent };
        const st = stateOf(exec);
        const root = kbRootOf({}, exec);
        const raw = (invocation && invocation.rawInput ? invocation.rawInput : "").trim().toLowerCase();
        const parts = raw.split(/\s+/).filter(Boolean);
        const cmd = parts[0] || "status";
        const arg = parts[1] || "";
        const lines = [];
        // 注意：这里改的都是**本会话**状态；只有 `/kb save` 才写进工作区默认值。
        // （第一版实现直接持久化，结果 `/kb both` 会污染之后所有新会话 —— 与"会话级状态"
        //   的目标正好相反。）
        const persistHint = "（本会话生效；`/kb save` 可存为新会话默认）";
        switch (cmd) {
          case "kb": case "both": case "web":
            st.scope = cmd; lines.push("范围已设为 " + cmd + " " + persistHint); break;
          case "quick": case "deep":
            st.depth = cmd; lines.push("深度已设为 " + cmd + " " + persistHint); break;
          case "strict":
            st.strict = (arg === "on" || arg === "true" || arg === "");
            lines.push("严格模式：" + (st.strict ? "开" : "关") + " " + persistHint);
            break;
          case "thorough": case "深挖":
            st.diligence = "thorough";
            lines.push("已进入**深挖模式**：不设检索调用上限；库内不足时按「kb_fetch(ingest=true) 补库 → 引文关联 → 增量入库 → 再查」循环，直到收敛或你喊停。" + persistHint);
            break;
          case "normal":
            st.diligence = "normal";
            lines.push("已回到默认纪律：一次提问最多 3 次检索、无命中即停。" + persistHint);
            break;
          case "on": {
            // 判据不能只看"工具数为 0"：半关闭（撤掉检索、留着入库/统计）之后 count 是 8，
            // 旧写法会认为"已经开着"而不重新注册，kb_search/kb_rag 就永远回不来了（实测抓到）。
            const needRepair = st.enabled === false || toolsRegistered() < TOTAL_TOOLS;
            st.enabled = true;
            if (needRepair) {
              disposeTools(function () { return true; });   // 先全撤，避免重复注册
              registerAllTools();
            }
            lines.push("kb-rag 已开启" + (needRepair ? "（工具已重新注册齐全，" + toolsRegistered() + "/10）"
                                                     : "（本来就是开着的）") + "。");
            break;
          }
          case "off":
            if (arg === "hard") {
              st.enabled = false;
              disposeTools(function () { return true; });
              lines.push("kb-rag 已**硬关闭**：10 个工具已从本进程所有会话撤销（/kb on 可恢复，无需重启）。");
            } else if (arg === "search") {
              st.enabled = true;
              disposeTools(function (n) { return SEARCH_TOOLS.indexOf(n) >= 0; });
              lines.push("kb-rag 已**半关闭**：kb_search / kb_rag 已从本进程所有会话撤掉，入库与统计仍可用（/kb on 恢复）。");
            } else {
              st.enabled = false;
              lines.push("kb-rag 已**软关闭**：工具仍在，但调用会直接返回「已关闭」（不拉守护进程）。");
            }
            break;
          case "policy":
            try {
              const inv = policyInventory();
              lines.push("**当前提示规则**（" + inv.length + " 条）");
              inv.forEach(function (r) {
                lines.push("- `" + r.id + "` · " + r.seat + (r.tools ? " · " + r.tools.join("/") : "")
                  + (r.tellUser ? " · 会给用户看" : "") + (r.throttle ? " · 节流 " + JSON.stringify(r.throttle) : ""));
                if (r.doc) lines.push("  " + r.doc);
              });
            } catch (e) { lines.push("规则清单读取失败：" + String(e)); }
            break;
          case "save":
            savePersistedDefaults({
              scope: st.scope, depth: st.depth, strict: st.strict,
              diligence: st.diligence, enabled: st.enabled, format: st.format,
            }, root, exec);
            lines.push("当前设置已写进工作区默认值（新会话直接生效）。");
            break;
          case "format": {
            if (!arg || arg === "?") {
              lines.push("当前答案格式：**" + (st.format || "auto") + "**"
                + "（可选：" + ANSWER_FORMATS.join(" / ") + "）");
              break;
            }
            if (ANSWER_FORMATS.indexOf(arg) < 0) {
              lines.push("未知格式 `" + arg + "`。可选：" + ANSWER_FORMATS.join(" / "));
              break;
            }
            st.format = arg;
            lines.push(arg === "auto"
              ? "答案格式已交回模型判断（默认）" + persistHint
              : "答案格式已设为 **" + arg + "**：kb_rag 生成 HTML 时若没显式指定模板，就用它" + persistHint);
            break;
          }
          case "status": default:
            break;
        }
        lines.push("");
        lines.push("**kb-rag 状态**（会话 " + sessionKey(exec) + "）");
        // 开关有四个档位（on / soft / search / hard，见 setEnabledState），以前只判 enabled 布尔
        // → 四种状态塌成两种标签，`/kb off hard` 卸掉全部工具后仍写"关（软关闭）"。
        // 不新增持久化字段：档位可以由 enabled + 实际注册的工具数**无损推出**（旧 state.json 同样适用）。
        const reg = toolsRegistered();
        const switchText = st.enabled === false
          ? (reg === 0
            ? "关（硬关闭：工具已卸载，仅 `/kb on` 可恢复）"
            : "关（软关闭：工具仍在注册表里，调用会返回\"已关闭\"）")
          : (reg < TOTAL_TOOLS ? "开（仅检索工具：写库类已卸载）" : "开");
        lines.push("- 范围 " + st.scope + " · 深度 " + st.depth + " · 严格 " + (st.strict ? "开" : "关")
          + " · 格式 " + (st.format || "auto")
          + " · 纪律 " + (st.diligence === "thorough" ? "深挖" : "默认")
          + " · 开关 " + switchText);
        lines.push("- 工具注册数：" + toolsRegistered() + " / 10");
        lines.push("- 状态文件：" + root + "/.kb-rag/state.json（`/kb save` 写入当前设置）");
        lines.push("- 可用：`/kb kb|both|web` 范围 · `/kb quick|deep` 深度 · `/kb strict on|off` · "
          + "`/kb format <模板|auto>` 答案格式 · `/kb thorough|normal` 纪律 · "
          + "`/kb off [hard|search]` · `/kb on` · `/kb policy` 提示规则");
        return { kind: "success", text: lines.join("\n") };
      },
    });
  } else {
    console.error("[kb-rag] commands 服务不可用：/kb 命令未注册（工具与检索不受影响）");
  }

  console.log("[kb-rag] ready (v1.6.8): 10 tools + " + (commands === undefined ? "no /kb command" : "/kb command"));
}

export { apply, inject, name };
