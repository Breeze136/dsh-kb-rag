// dsh-kb-rag — static DSH plugin (Host half)
// 本地文献知识库 RAG：8 个模型工具 + 常驻 Python 引擎（随包分发 kb_engine.py）。
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
  let scopeStrict = false;
  let scopeAsked = false;
  const userQuestions = ctx.get("userQuestions");

  function askScopeOnce(agent) {
    if (scopeAsked || userQuestions === undefined) return;
    scopeAsked = true;
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
      }],
    };
    if (agent !== undefined) request.agent = agent;
    Promise.race([
      userQuestions.ask(request).then(function (answer) {
        const picked = answer && answer.answers && answer.answers[0] && answer.answers[0].selected && answer.answers[0].selected[0];
        if (typeof picked === "string" && picked.indexOf("仅封闭") === 0) scopePref = "kb";
        else if (typeof picked === "string" && picked.indexOf("知识库+全网") === 0) scopePref = "both";
        else if (typeof picked === "string" && picked.indexOf("仅全网") === 0) scopePref = "web";
        console.log("[kb-rag] query scope:", scopePref);
      }).catch(function (e) {
        console.error("[kb-rag] scope question failed:", String(e));
      }),
      ctx.timeout(120000),
    ]);
  }

  function scopeWrapped(exec, engineCall, strict) {
    askScopeOnce(exec && exec.agent);
    return engineCall.then(function (resp) {
      resp.scope = scopePref;
      resp.scope_note = SCOPE_NOTE[scopePref];
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

  // 启动时自动检测 Python 依赖（pymupdf / faiss / sentence-transformers / torch），
  // 缺失时在宿主日志里打印安装命令；不阻塞插件加载。
  function checkPythonDeps() {
    let python = "python";
    const probe = subprocess.resolveExecutable("python").catch(function () { /* keep bare name */ }).then(function (resolved) {
      if (typeof resolved === "string" && resolved.length > 0) python = resolved;
      let handle;
      try {
        handle = subprocess.spawn({
          argv: [python, "-c", "import fitz, faiss, sentence_transformers, torch"],
          cwd: ENGINE_DIR,
          stdio: {
            stdin: "ignore",
            stdout: { maxBytes: 4096 },
            stderr: { maxBytes: 16384 },
          },
          graceMs: 3000,
        });
      } catch (e) {
        console.error("[kb-rag] dependency check failed to spawn:", String(e && e.message || e));
        return;
      }
      handle.done.then(function (out) {
        if (out.exitCode === 0) {
          console.log("[kb-rag] Python dependencies OK");
          return;
        }
        const err = handle.collected.stderr !== undefined ? handle.collected.stderr.readFrom(0).text : "";
        const missing = [];
        if (String(err).includes("fitz")) missing.push("pymupdf");
        if (String(err).includes("faiss")) missing.push("faiss-cpu");
        if (String(err).includes("sentence_transformers")) missing.push("sentence-transformers");
        if (String(err).includes("torch")) missing.push("torch");
        console.error("[kb-rag] Python dependencies missing: " + (missing.length > 0 ? missing.join(", ") : "unknown module"));
        console.error("[kb-rag] Install with: pip install " + (missing.length > 0 ? missing.join(" ") : "pymupdf faiss-cpu sentence-transformers"));
        console.error("[kb-rag] " + String(err).trim().split("\n").slice(-2).join(" | ").slice(0, 500));
      });
    });
    probe.catch(function () { /* ignore */ });
  }
  checkPythonDeps();

  const kbRootOf = (args, exec) => typeof args.kb_root === "string" && args.kb_root.length > 0 ? args.kb_root : workspaceOf(exec) + "/.kb";
  const renderJson = (_args, value) => [{ type: "text", text: JSON.stringify(value) }];

  const renderSources = (_args, value) => {
    if (value === null || typeof value !== "object") return [{ type: "text", text: String(value) }];
    const items = Array.isArray(value.evidence) ? value.evidence : (Array.isArray(value.results) ? value.results : []);
    if (items.length === 0) return [{ type: "text", text: JSON.stringify(value) }];
    const lines = [];
    lines.push("**知识库来源 Top-" + items.length + "**");
    lines.push("混合检索" + (value.reranker ? " · 精排 " + value.reranker.split(" ")[0] : "") + (value.cached === true ? " · 缓存命中" : "") + (typeof value.ms === "number" ? " · " + value.ms + "ms" : "") + (value.strict === true ? " · 严格模式" : ""));
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
      lines.push("> " + String(r.snippet || "").slice(0, 280).replace(/\n/g, " "));
      if (doi !== null) {
        lines.push("[DOI " + doi + "](https://doi.org/" + doi + ")" + " · score " + r.score);
      } else {
        lines.push("无 DOI · score " + r.score + " · 文件：" + String(r.file || ""));
      }
    });
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
        lines.push("- " + t + (meta.length > 0 ? " — " + meta : "") + "（" + String(r.reason || "内容相关") + " · score " + r.score + "）");
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
      journal: { type: "string", description: "期刊子串匹配。" },
      kind: { type: "string", description: "文件类型：pdf/txt/md/docx。" },
      section: { type: "string", description: "章节子串匹配（如 Methods、Results、方法）。" },
      year: { oneOf: [{ type: "integer", description: "精确年份（如 2024）。" }, { type: "string", description: "年份比较式（如 \">=2020\"）。" }], description: "年份过滤。" },
    },
  };

  ctx.tools.register(defineTool({
    name: "kb_ingest",
    description: "把本地文档（PDF/TXT/MD/DOCX）导入 DSH 知识库并建立索引（轻量 RAG 工作流的入库步骤）。支持单个文件或目录（递归扫描并只处理 PDF/TXT/MD/DOCX）；按章节切分并抽取元数据（标题/作者/年份/DOI）；同时用本地 bge-small 模型生成向量（数据持久化在工作区/.kb）。已入库且内容未变的文件自动跳过；同一内容（sha256 相同）在其他路径已入库时标记为 duplicate 跳过（增量）。paths 用工作区内的相对路径或绝对路径。入库后用 kb_search 检索、kb_rag 问答、kb_stats 看统计。重复调用安全。",
    parameters: {
      paths: { type: "array", required: true, items: { type: "string" }, description: "要入库的文件或目录路径列表。" },
      kb_root: { type: "string", description: "知识库目录（默认：工作区下的 .kb）。" },
      force: { type: "boolean", description: "true 时强制重新解析并重新编码向量（默认 false）。" },
    },
    output: { schema: { type: "json" }, render: renderJson },
    timeoutMs: 1800000,
    execute(args, exec) {
      return runEngine("ingest", { paths: args.paths, kb_root: kbRootOf(args, exec), force: args.force === true }, exec);
    },
  }));

  ctx.tools.register(defineTool({
    name: "kb_search",
    description: "在知识库中做混合检索（关键词 BM25 + 向量余弦，RRF 融合，×章节权重，再经 bge-reranker-base 精排 Top-3），返回最相关片段及精确来源（文件/标题/作者/年份/期刊/DOI/章节/得分）。想在已入库文档中查找事实、数据或术语时优先于直接读文件（更省 token）。query 可以是术语、数值、化学式或中文短语；mode 可选 keyword/vector/hybrid（默认 hybrid）；filters 支持 authors/year/section/title/journal/kind 元数据预过滤（year 可用 \">=2020\" 形式）。查询范围由会话开始时的范围询问或 kb_scope 工具控制；返回的 scope/scope_note 指明当前范围。strict 可选（true=严格模式：答案仅基于本次结果，禁止库外知识/常识外延；默认继承 kb_scope 设置）。回答用户时必须标注来源：引用要写成 markdown 链接格式 [作者, 年份, 期刊](https://doi.org/DOI)（用来源字段里的 doi，保证用户能点击打开）；若该来源无 DOI，引用写成 [作者, 年份, 文件名]（方括号内只放 PDF 文件名，不要使用任何 HTML 标签；文件名过长时可截断到约 60 字符）。无命中时先检查是否已入库（kb_stats）。相同查询命中缓存，零重计算。",
    parameters: {
      query: { type: "string", required: true, description: "检索关键词或短语（中英文均可）。" },
      top_k: { type: "integer", description: "返回结果数（默认 5，上限 10）。" },
      snippet: { type: "integer", description: "片段长度字符数（默认 400）。" },
      mode: { type: "string", enum: ["keyword", "vector", "hybrid"], description: "检索模式（默认 hybrid）。" },
      rerank: { type: "boolean", description: "是否启用 bge-reranker-base 精排（默认 true）。" },
      related: { type: "boolean", description: "true 时附带 related 关联文献列表（同作者/同期刊/年份相近/主题相似，默认 true，供补充建议引用）。" },
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
        top_k: args.top_k,
        snippet: args.snippet,
        mode: args.mode,
        rerank: args.rerank,
        related: args.related,
        filters: args.filters,
        kb_root: kbRootOf(args, exec),
      }, exec);
      return scopeWrapped(exec, call, strict);
    },
  }));

  ctx.tools.register(defineTool({
    name: "kb_rag",
    description: "在知识库中检索证据片段（混合检索 + bge-reranker 精排，默认 Top-3）供当前模型直接作答：基于 evidence 回答问题，每个事实后标注引用编号 [n]（对应 evidence 下标）。引用一定要写成可点击的 markdown 链接：[作者, 年份, 期刊](https://doi.org/DOI)（用 evidence 条目的 doi 字段）；若 doi 为 null，引用写成 [作者, 年份, 文件名]（方括号内只放 PDF 文件名，不要使用任何 HTML 标签；文件名过长时可截断到约 60 字符）。strict 可选（true=严格模式：仅基于 evidence 作答，禁止补充库外知识/常识外延或未出现在 evidence 中的文献数据，证据不足直接说明无法回答；默认继承 kb_scope 设置，当前默认 false）。资料不足时明确回答\"根据现有资料无法回答\"；多源冲突时分别列出并说明来源。答案末尾的补充建议优先参考 related 关联文献列表（同作者/同期刊/年份相近/主题相似的库内文献）：若库内缺少关键资料，明确指出应补充哪些文献/主题（用户重视此提示）。这是知识库 RAG 问答的唯一入口；查询范围由会话开始时的范围询问或 kb_scope 工具控制。",
    parameters: {
      query: { type: "string", required: true, description: "自然语言问题（中英文均可）。" },
      top_k: { type: "integer", description: "证据条数（默认 3，上限 10）。" },
      rerank: { type: "boolean", description: "是否启用精排（默认 true）。" },
      related: { type: "boolean", description: "true 时附带 related 关联文献列表供补充建议引用（默认 true）。" },
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
        top_k: args.top_k,
        rerank: args.rerank,
        related: args.related,
        filters: args.filters,
        kb_root: kbRootOf(args, exec),
      }, exec);
      return scopeWrapped(exec, call, strict);
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
    output: { schema: { type: "json" }, render: renderJson },
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
    name: "kb_scope",
    description: "设置/查看知识库查询范围与严格模式（会话开始时也会询问一次范围）：scope：kb=仅封闭知识库；both=知识库+全网（kb 检索 + web_search 补充）；web=仅全网。strict 可选：true=严格模式（答案仅基于库内证据，禁止库外知识/常识外延）；false=关闭（默认 false，允许模型在证据不足处用一般知识补充并说明）。用户说\"封闭库/全网/都要/严格只按库内\"等要求时，调本工具设定后再检索。",
    parameters: {
      scope: { type: "string", required: true, enum: ["kb", "both", "web"], description: "kb=仅封闭库；both=知识库+全网；web=仅全网。" },
      strict: { type: "boolean", description: "可选：同时设置严格模式。true=仅基于库内证据作答；false=关闭（默认）。" },
    },
    output: { schema: { type: "json" }, render: renderJson },
    execute(args, _exec) {
      scopePref = args.scope;
      if (args.strict !== undefined) scopeStrict = args.strict === true;
      console.log("[kb-rag] query scope:", scopePref, "strict:", scopeStrict);
      return Promise.resolve({ ok: true, scope: scopePref, strict: scopeStrict, scope_note: SCOPE_NOTE[scopePref], strict_note: scopeStrict ? STRICT_NOTE : undefined });
    },
  }));

  ctx.tools.register(defineTool({
    name: "kb_stats",
    description: "查看知识库统计：文档数、分块数、向量数、最近入库列表及数据库位置。用于检查哪些文档已入库、索引状态；检索无命中时先调它确认库里有什么。",
    parameters: {
      kb_root: { type: "string", description: "知识库目录（默认：工作区下的 .kb）。" },
    },
    output: { schema: { type: "json" }, render: renderJson },
    execute(args, exec) {
      return runEngine("stats", { kb_root: kbRootOf(args, exec) }, exec);
    },
  }));

  console.log("[kb-rag] static tools registered: kb_ingest / kb_search / kb_rag / kb_zotero / kb_dedup / kb_clear / kb_scope / kb_stats");
}

export { apply, inject, name };
