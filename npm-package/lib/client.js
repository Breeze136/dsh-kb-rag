// kb-rag — 客户端半边（浏览器侧；npm 包形态）
//
// 为什么是这个格式：DSH 的客户端插件包不是普通 ESM，而是 **lazy-CJS bundle** ——
// 脚本执行时只在页面全局 `window.__ModuleLoader__` 上注册一个工厂，真正的副作用
// （注册插槽、注入 CSS）都在 factory 里、在被 materialize 时才跑。
// 依据：@deepseek-ai/dsh-client-modules 的类型说明与随包分发的客户端 bundle 形态
//      （`ClientPluginHandoff { id, factory }`，id 必须等于包名）。
//
// ⚠️ 待验证：官方没有给出第三方包手写该 bundle 的公开规范（是否接受手写、
//    require("react") 的外部化规则等）。本文件按随包 bundle 的等价形态书写，
//    需在真实 Web GUI 里确认能加载；加载失败**不影响**核心功能（工具与 markdown 来源链接
//    由宿主侧渲染，客户端半边只是把它渲染成卡片）。
//
// 提供三件事：
//   ① kb_search / kb_rag 的**来源卡片**（tool.call.toolview 插槽）：
//      优先用宿主给的 presentationMeta（结构化 sources），没有就退回解析结果文本里的 markdown 链接。
//   ② 等待态与入库卡片（同样是 tool.call.toolview 插槽）：
//      kb_search / kb_rag 在调用期间显示阶段 + 已等待秒数；kb_ingest / kb_zotero 有独立卡片，
//      调用中显示路径与开关，返回后显示 job_id / 待处理篇数 / totals —— 这两个是真正要等几分钟的调用。
//   ③ 会话标题栏的 **kb 指示条**（conversation.session.header.actions 插槽）：
//      显示 kb-rag 已启用，并把 /kb 可用命令写进 title（客户端读取宿主状态需要额外通道，暂不臆造）。

window.__ModuleLoader__.load({
  id: "dsh-kb-rag",
  factory: (require) => {
    var module = { exports: {} };
    var exports = module.exports;

    const React = require("react");
    const inject = ["slots"];

    // 客户端只做展示，术语与宿主保持一套：宿主 renderSources 里的 MODE_LABEL 与此一一对应。
    const DEPTH_LABEL = { quick: "快速检索", deep: "深度检索" };
    const MODE_LABEL = { hybrid: "混合检索", keyword: "关键词检索", vector: "向量检索" };
    const TEMPLATE_LABEL = {
      instant: "直答卡", review: "综述", method: "方法", compare: "对比表", numeric: "数值",
      timeline: "时间线", concept: "概念", dispute: "争议", derivation: "推导", qa: "问答",
    };

    // tool-call 的 arguments 可能是 JSON 字符串（流式拼装时也可能是半截），也可能是已解析对象。
    function parseArgs(block) {
      const raw = block ? block.arguments : undefined;
      if (raw === null || raw === undefined) return {};
      if (typeof raw === "object") return raw;
      try { return JSON.parse(String(raw || "{}")); } catch (e) { return {}; }
    }

    // ── 宿主契约（DSH 0.2.x）──────────────────────────────────────────────────
    // toolview 拿到的是 { phase: 'preparing' | 'start' | 'result', block, ... }：
    //   start   → block 是 StartedToolCall：{ phase:'start', argsRaw: '{"query":…}' }
    //   result  → block 是 ToolResultNode：{ kind:'tool-result', call:{argsRaw}, meta }
    // 旧实现按 block.type === 'tool-call' 判断，0.2 下 type 恒为 undefined ——
    // 结果四张卡片永远空白，而且 keyed slot 命中会**替换**掉通用工具行。
    function phaseOf(props) {
      const p = props ? props.phase : undefined;
      if (p === "result" || p === "preparing" || p === "start") return p;
      const b = props ? props.block : undefined;   // 兼容 0.1.x 宿主
      if (b && (b.kind === "tool-result" || b.type === "tool-result")) return "result";
      if (b && b.type === "tool-call") return "start";
      return b ? "start" : "unknown";
    }

    function argsOf(props) {
      const b = props ? props.block : undefined;
      if (b === null || b === undefined) return {};
      let raw = b.argsRaw;                                   // start 阶段
      if (raw === undefined && b.call && typeof b.call === "object") {
        raw = b.call.argsRaw;                                // result 阶段
      }
      if (raw === undefined) return parseArgs(b);            // 老宿主：block.arguments
      if (raw === null) return {};
      if (typeof raw === "object") return raw;
      try { return JSON.parse(String(raw || "{}")); } catch (e) { return {}; }
    }

    function baseName(p) {
      const parts = String(p || "").split(/[\\/]/).filter(Boolean);
      return parts.length > 0 ? parts[parts.length - 1] : String(p || "");
    }

    function fmtMs(ms) {
      if (typeof ms !== "number" || !isFinite(ms) || ms < 0) return "";
      return ms < 1000 ? Math.round(ms) + "ms" : (ms / 1000).toFixed(1) + "s";
    }

    // 等待秒表：只在 tool-call 阶段走，卡片换成结果后 effect 清理、读数归零。
    // 纯前端计时 —— 静态 npm 半边没有 host.call 通道，这是不依赖宿主的唯一进度信号。
    function useElapsed(active) {
      const [sec, setSec] = React.useState(0);
      React.useEffect(function () {
        if (active !== true) { setSec(0); return undefined; }
        const t0 = Date.now();
        const id = setInterval(function () {
          setSec(Math.floor((Date.now() - t0) / 1000));
        }, 1000);
        return function () { clearInterval(id); };
      }, [active]);
      return active === true ? sec : 0;
    }

    function apply(ctx) {
      const slots = ctx.get("slots");
      if (slots === undefined) return;

      const accent = "var(--ds-color-accent, #4a9eff)";
      const muted = "var(--ds-color-text-muted, #999)";
      const border = "var(--ds-color-border, rgba(128,128,128,0.4))";
      const styles = {
        box: { margin: "8px 0", fontSize: 12, lineHeight: 1.5, fontFamily: "inherit" },
        // 调用中的工具行：蓝色高亮，和结果卡/普通工具行区分开（只在 preparing/start 用）。
        waiting: {
          background: "var(--ds-color-accent-subtle, rgba(74,158,255,0.09))",
          border: "1px solid var(--ds-color-accent-border, rgba(74,158,255,0.28))",
          borderLeft: "3px solid " + accent,
          borderRadius: 6,
          padding: "8px 10px",
          boxShadow: "0 0 0 3px rgba(74,158,255,0.06)",
        },
        head: { fontWeight: 700, fontSize: 12, color: accent, marginBottom: 6 },
        meta: { color: muted, fontSize: 11, marginBottom: 4 },
        stage: { color: muted, fontSize: 11, marginTop: 2, marginBottom: 4 },
        card: { border: "1px solid " + border, borderLeft: "3px solid " + accent, borderRadius: 6,
                padding: "8px 10px", marginBottom: 8, background: "var(--ds-color-surface, transparent)" },
        src: { fontWeight: 600, fontSize: 12, wordBreak: "break-word" },
        rest: { color: muted, fontSize: 11, marginTop: 2 },
        link: { color: accent, fontSize: 11, textDecoration: "underline", cursor: "pointer" },
        warn: { color: "var(--ds-color-danger, #c33)", fontSize: 12 },
        empty: { color: muted, fontSize: 12 },
        chip: { fontSize: 11, padding: "2px 6px", borderRadius: 999,
                border: "1px solid " + border, color: muted, cursor: "default" },
      };

      // 结构化来源：宿主 output.presentationMeta 投影出来的 payload（顶层调用才有）
      function metaSources(block) {
        const meta = block && block.meta;
        if (meta === null || typeof meta !== "object") return null;
        return Array.isArray(meta.sources) ? meta : null;
      }

      // 退回路径：从结果文本里解析来源链接。宿主渲染的格式是「1. [标题](https://doi.org/...)」，
      // 旧写法按「[1] [标题](...)」匹配，**永远匹配不上** → 没有 presentationMeta 时卡片恒显示"无命中"。
      function textSources(block) {
        const content = Array.isArray(block && block.content) ? block.content : [];
        const text = content.map(function (c) {
          return (c && c.type === "text") ? String(c.text || "") : "";
        }).join("\n");
        const re = /(\d{1,3})\s*\.?\s*\[([^\]]{2,240})\]\(([^)\s]+)\)/g;
        const items = [];
        let m = null;
        while ((m = re.exec(text)) !== null) items.push({ idx: m[1], title: m[2], href: m[3] });
        return { sources: items, text: text };
      }

      // no-hit 理由：不能无条件写「精排最高分 X」—— 3.3.0 起判"无关"的两条闸门里，
      // `missing>=1` 压根没跑精排（quick 更从来没有 floor），那样等于把"查询词库里不存在"
      // 说成"分数不够"。分档与宿主 npm-package/lib/index.js 的 noHitReason 一致，
      // 需要的 relevance_rule / missing 由宿主 sourcesMeta 投影进 block.meta。
      function noHitReason(meta) {
        const rule = meta !== null && typeof meta.relevance_rule === "string" ? meta.relevance_rule : "";
        if (rule.indexOf("missing") === 0) {
          const n = (meta.missing === null || meta.missing === undefined) ? "?" : meta.missing;
          return "查询的关键词在库内不存在（缺失 " + n + " 个实词）";
        }
        if (rule.indexOf("over_floor") === 0) {
          const lim = rule.split("<").pop().trim() || "?";
          return "精排后达到地板分的片段不足 " + lim + " 条";
        }
        const ms = meta.max_score;
        if (ms === null || ms === undefined) return "";
        const fl = meta.floor;
        return "精排最高分 " + ms + (fl === null || fl === undefined ? "" : " < 地板 " + fl);
      }

      function SourceList(props) {
        const block = props ? props.block : undefined;
        const phase = phaseOf(props);
        const waiting = phase === "start" || phase === "preparing";
        const elapsed = useElapsed(waiting);
        if (block === undefined || block === null) return React.createElement("div", null);
        if (waiting) {
          const args = argsOf(props);
          const query = String(args.query || "");
          const depth = args.depth === "deep" ? "deep" : "quick";
          const tpl = (args.answer !== null && typeof args.answer === "object") ? args.answer.template : null;
          // 阶段文案只用 args 能判断的信息，不猜进度：模板 → 生成什么；deep → 精排链路；其余 → 混合召回。
          const stage = (tpl && TEMPLATE_LABEL[tpl])
            ? "生成" + TEMPLATE_LABEL[tpl] + "…"
            : (depth === "deep" ? "深度检索：精排 + 引文链…" : "快速检索：混合召回…");
          const bits = [stage];
          if (elapsed >= 2) bits.push("已等待 " + elapsed + "s");
          if (elapsed >= 10) bits.push("首次使用可能在下载模型（约 1.2 GB）");
          if (elapsed >= 90) bits.push("下载慢：设 HF_ENDPOINT 或预下载，见 README");
          return React.createElement("div", { style: Object.assign({}, styles.box, styles.waiting) },
            React.createElement("div", { style: styles.head },
              "检索中" + (query.length > 0 ? "：" + query.slice(0, 120) : "…")),
            React.createElement("div", { style: styles.stage }, bits.join(" · ")));
        }
        if (phaseOf(props) !== "result") return React.createElement("div", null);
        if (block.isError === true) {
          return React.createElement("div", { style: styles.box },
            React.createElement("div", { style: styles.warn }, "知识库检索失败"));
        }
        // 意图探针：只显示"库里有什么" + 可展开的形态提示，不把探针伪装成答案。
        const probe = (block.meta !== null && typeof block.meta === "object" && block.meta.probe === true)
          ? block.meta : null;
        if (probe !== null) {
          const kids = [];
          const bits = ["命中 " + (typeof probe.count === "number" ? probe.count : 0) + " 篇"];
          const msText = fmtMs(probe.ms);
          if (msText.length > 0) bits.push(msText);
          if (probe.cached === true) bits.push("缓存命中");
          kids.push(React.createElement("div", { key: "p0", style: styles.stage }, bits.join(" · ")));
          (Array.isArray(probe.hits) ? probe.hits : []).forEach(function (h, i) {
            kids.push(React.createElement("div", { key: "ph" + i, style: styles.rest },
              (i + 1) + ". " + String(h.title || "") + (h.year ? " · " + h.year : "")));
          });
          if (probe.count === 0) {
            kids.push(React.createElement("div", { key: "pn", style: styles.warn }, "库里没有相关资料"));
          } else {
            kids.push(React.createElement("div", { key: "pt", style: styles.meta }, "可展开：一句话 / 对比表 / 路线或综述"));
          }
          return React.createElement("div", { style: styles.box },
            React.createElement("div", { style: styles.head }, "意图探针"),
            kids);
        }
        const structured = metaSources(block);
        const parsed = structured === null ? textSources(block) : null;
        const sources = [];
        if (structured !== null) {
          structured.sources.forEach(function (s) {
            sources.push({
              idx: String(s.idx === undefined ? sources.length + 1 : s.idx),
              title: String(s.title || s.file || "(无标题)"),
              href: s.doi ? ("https://doi.org/" + s.doi) : null,
              rest: [s.authors ? String(s.authors).split(";")[0] : null, s.year,
                     s.section ? ("§" + s.section) : null].filter(Boolean).join(" · "),
            });
          });
        } else if (parsed !== null) {
          parsed.sources.forEach(function (s) {
            sources.push({ idx: s.idx, title: s.title, href: s.href, rest: null });
          });
        }

        // 结构化数据和文本链接都拿不到：宿主没有投影卡片数据（旧宿主/静态半边），
        // 或者这个结果本来就不是来源列表。这里**不能**渲染"无命中"——
        // 那会把"卡片没数据"说成"库里没有"，是会被用户当真的错误结论。
        if (structured === null && sources.length === 0) return React.createElement("div", null);

        const children = [];
        if (structured !== null) {
          // 文本通道早就有这些数字（ms / cached / mode_used），卡片此前只显示标题；
          // 把它们放到来源上方，等待的"值不值"和"快不快"在卡片里也能读到。
          const bits = [];
          if (structured.depth === "quick" || structured.depth === "deep") bits.push(DEPTH_LABEL[structured.depth]);
          if (structured.mode_used) bits.push(MODE_LABEL[structured.mode_used] || String(structured.mode_used));
          const ms = fmtMs(structured.ms);
          if (ms.length > 0) bits.push(ms);
          if (structured.cached === true) bits.push("缓存命中");
          if (bits.length > 0) children.push(React.createElement("div", { key: "m", style: styles.stage }, bits.join(" · ")));
        }
        const noHit = structured !== null ? structured.no_hit === true : false;
        if (noHit === true) {
          const reason = structured !== null ? noHitReason(structured) : "";
          children.push(React.createElement("div", { key: "nh", style: styles.warn },
            "库内无相关资料" + (reason ? "（" + reason + "）" : "")));
        }
        const closest = structured !== null && Array.isArray(structured.closest) ? structured.closest : [];
        if (noHit === true && closest.length > 0) {
          children.push(React.createElement("div", { key: "cl", style: styles.meta }, "库内最接近："));
          closest.slice(0, 5).forEach(function (c, i) {
            children.push(React.createElement("div", { key: "c" + i, style: styles.rest },
              "· " + String(c.title || "") + (c.year ? " · " + c.year : "")));
          });
        }
        sources.forEach(function (s) {
          children.push(React.createElement("div", { key: "s" + s.idx, style: styles.card },
            React.createElement("div", { style: styles.src },
              "[" + s.idx + "] ",
              s.href
                ? React.createElement("a", { href: s.href, target: "_blank", rel: "noreferrer", style: styles.link }, s.title)
                : s.title),
            s.rest ? React.createElement("div", { style: styles.rest }, s.rest) : null));
        });
        if (structured !== null && structured.verdict === "弱相关") {
          children.push(React.createElement("div", { key: "v", style: styles.meta }, "相关性偏弱：建议升级 depth=deep 再查一次"));
        }
        if (sources.length === 0 && noHit !== true) {
          children.push(React.createElement("div", { key: "e", style: styles.empty }, "无命中"));
        }
        return React.createElement("div", { style: styles.box },
          React.createElement("div", { style: styles.head }, "知识库来源"),
          children);
      }

      // kb_ingest / kb_zotero：这两个调用动辄分钟级（或转后台），此前完全没有卡片。
      // kind 由注册处注入，用来区分"入库"和"Zotero 迁移"的措辞。
      function IngestCard(props) {
        const block = props ? props.block : undefined;
        const kind = props && props.kind === "zotero" ? "zotero" : "ingest";
        const phase = phaseOf(props);
        const waiting = phase === "start" || phase === "preparing";
        const elapsed = useElapsed(waiting);
        if (block === undefined || block === null) return React.createElement("div", null);
        if (waiting) {
          const args = argsOf(props);
          const paths = Array.isArray(args.paths) ? args.paths : [];
          const flags = [];
          if (args.rebuild === true) flags.push("全量重灌");
          if (args.metadata_only === true) flags.push("只刷新元数据");
          if (args.force === true) flags.push("强制重解析");
          if (args.dry_run === true) flags.push("预演");
          const title = kind === "zotero" ? "Zotero 迁移中"
            : (args.rebuild === true ? "重灌中" : (args.metadata_only === true ? "元数据刷新中" : "入库中"));
          const bits = [];
          if (paths.length > 0) {
            const names = paths.slice(0, 2).map(baseName).filter(Boolean).join("、");
            bits.push(paths.length + " 个路径" + (names.length > 0 ? "：" + names + (paths.length > 2 ? " 等" : "") : ""));
          } else if (args.rebuild === true) {
            bits.push("库内全部文档");
          } else if (kind === "zotero") {
            bits.push("扫描 Zotero 库");
          }
          flags.forEach(function (f) { bits.push(f); });
          if (elapsed >= 2) bits.push("已等待 " + elapsed + "s");
          if (elapsed >= 10) bits.push("首次使用可能在下载模型（约 1.2 GB）");
          if (elapsed >= 90) bits.push("下载慢：设 HF_ENDPOINT 或预下载，见 README");
          return React.createElement("div", { style: Object.assign({}, styles.box, styles.waiting) },
            React.createElement("div", { style: styles.head }, title),
            React.createElement("div", { style: styles.stage }, bits.join(" · ") || "准备中…"));
        }
        if (phaseOf(props) !== "result") return React.createElement("div", null);
        if (block.isError === true) {
          return React.createElement("div", { style: styles.box },
            React.createElement("div", { style: styles.warn }, "入库失败"));
        }
        const meta = (block.meta !== null && typeof block.meta === "object") ? block.meta : null;
        // 没有结构化投影（旧宿主）：不另造卡片，结果文本里已经有完整的入库统计。
        if (meta === null) return React.createElement("div", null);

        const rows = [];
        let title = "";
        if (meta.background === true) {
          title = "已转入后台" + (meta.job_id ? " · job_id " + String(meta.job_id) : "");
          rows.push("待处理 " + (typeof meta.pending_files === "number" ? meta.pending_files : "?")
            + " 篇 · 用 kb_status 查进度，宿主超时不会中断");
        } else if (meta.dry_run === true) {
          title = "Zotero 预演 · 候选 " + (typeof meta.candidates === "number" ? meta.candidates : "?") + " 篇";
          rows.push("未写入库；去掉 dry_run 才会真正迁移");
        } else if (meta.mode === "metadata_only") {
          title = "元数据刷新完成 · 已刷新 " + (meta.meta_updated || 0) + " 篇";
          rows.push("其中 " + (meta.meta_changed || 0) + " 篇有变化"
            + (typeof meta.ms === "number" ? " · " + fmtMs(meta.ms) : ""));
        } else {
          title = (kind === "zotero" ? "迁移完成" : "入库完成") + " · 新增 " + (meta.added || 0) + " / 更新 " + (meta.updated || 0)
            + " / 跳过 " + (meta.skipped || 0) + (meta.duplicates ? " / 重复 " + meta.duplicates : "");
          const bits = [];
          const ms = fmtMs(meta.ms);
          if (ms.length > 0) bits.push(ms);
          if (typeof meta.chunks === "number") bits.push(meta.chunks + " 块 / " + (meta.vectors || 0) + " 向量");
          if (bits.length > 0) rows.push(bits.join(" · "));
        }
        const children = rows.map(function (t, i) {
          return React.createElement("div", { key: "r" + i, style: styles.stage }, t);
        });
        if (typeof meta.errors === "number" && meta.errors > 0) {
          children.push(React.createElement("div", { key: "err", style: styles.warn },
            "失败 " + meta.errors + " 篇（明细见结果文本）"));
        }
        return React.createElement("div", { style: styles.box },
          React.createElement("div", { style: styles.head }, title),
          children);
      }

      // 会话标题栏指示条：显示本会话的**实时**状态（范围/深度/工具数）。
      //
      // 数据走 Package 私有通道 host.call ⇄ 宿主 harness.handle("kb-state")。
      // 该通道**只存在于动态沙箱**（harness 是沙箱全局）。静态半边（本文件所在的 npm 形态）
      // 没有这条通道，所以这里必须做能力检测并退化为静态 "kb" —— 同一份源码两种形态都能跑，
      // 静态半边要拿到实时状态，需要先把会话状态改成可投影的事件溯源（见 mcp-server/README.md 之外的
      // docs 说明），那是独立的架构改动，不在本次范围内。
      // 退化时的字面量刻意做了区分，便于一眼看出卡在哪一步：
      //   "kb"                       通道不存在（静态半边，或宿主没注册 handler）
      //   "kb !"                     调用了但失败
      //   "kb · ?"                   通道通了但拿不到 sessionId（指示条没收到 props）
      //   "kb · kb · deep · 10/10"   正常
      function KbChip(props) {
        const sid = (props !== null && props !== undefined && typeof props.sessionId === "string")
          ? props.sessionId : "";
        const [st, setSt] = React.useState(null);
        const [bad, setBad] = React.useState(false);
        const [busy, setBusy] = React.useState(false);
        const hasChannel = (typeof host !== "undefined" && host !== null && typeof host.call === "function");
        // 点击 → 唤起宿主侧与"会话开场"同一套询问（范围/深度/陈旧数据），
        // 返回值就是询问后的新状态，直接拿来刷新胶囊。静态半边没有通道 → clickable=false，不可点。
        const onClick = function () {
          if (!hasChannel || busy === true) return;
          setBusy(true);
          Promise.resolve(host.call("kb-menu", { sessionId: sid })).then(function (v) {
            if (v !== null && typeof v === "object") { setSt(v); setBad(false); }
            setBusy(false);
          }, function () { setBad(true); setBusy(false); });
        };
        React.useEffect(function () {
          let alive = true;
          if (!hasChannel) return function () { alive = false; };
          Promise.resolve(host.call("kb-state", { sessionId: sid })).then(function (v) {
            if (!alive) return;
            if (v !== null && typeof v === "object") setSt(v);
            else setBad(true);
          }, function () { if (alive) setBad(true); });
          return function () { alive = false; };
        }, [sid]);

        let label = "kb-rag";
        let detail = "（状态通道不可用）";
        if (bad) {
          label = "kb-rag !";
          detail = "（取状态失败）";
        } else if (st !== null) {
          const tools = (typeof st.tools === "number" ? st.tools : "?") + "/10";
          if (st.enabled === false) {
            // 关闭态必须仍然是"可点的入口"：文案直接写出动作（动态半边可点；静态半边退化后仅作状态显示）
            label = "kb-rag 已关闭 · 点击开启";
            detail = "已关闭（不检索、也不拉引擎）。点这个胶囊即可重新开启";
          } else if (sid === "") {
            label = "kb-rag ?";
            detail = "（拿不到 sessionId，显示的是默认值）";
          } else {
            // 范围只在非默认时出现：默认的 kb 与产品名 kb-rag 语义重复
            const scopePart = (st.scope === "kb") ? "" : (String(st.scope) + " ");
            label = "kb-rag " + scopePart + st.depth + " " + tools;
            detail = "范围 " + st.scope + " · 深度 " + st.depth + " · 纪律 " + st.diligence + " · 工具 " + tools;
          }
        }
        const clickable = hasChannel;
        // 关闭态用警示色，让"这里可以点回来"一眼可见
        const closed = (bad !== true && st !== null && st.enabled === false);
        const closedColor = "var(--ds-color-danger, #c33)";
        const chipStyle = clickable
          ? { fontSize: 11, padding: "2px 6px", borderRadius: 999,
              border: "1px solid " + (closed ? closedColor : border),
              color: closed ? closedColor : muted,
              cursor: "pointer" }
          : styles.chip;
        return React.createElement("div", {
          style: chipStyle,
          onClick: clickable ? onClick : undefined,
          title: "kb-rag " + detail
            + (clickable ? "。**点击可改范围/深度**" : "")
            + "。命令：/kb status · /kb kb|both|web · /kb quick|deep · "
            + "/kb thorough|normal · /kb off [hard|search] · /kb on · /kb policy",
        }, busy === true ? label + " …" : label);
      }

      // kind 必须在组件外层定好：把它塞进 props 再 createElement，IngestCard 才能对两个工具复用同一套 hooks。
      const zoteroView = function (props) {
        return React.createElement(IngestCard, { block: props ? props.block : undefined, kind: "zotero" });
      };

      slots.inject("tool.call.toolview", () => slots.register(
        { name: "tool.call.toolview", key: "kb_rag" },
        (props) => React.createElement(SourceList, props),
      ));
      slots.inject("tool.call.toolview", () => slots.register(
        { name: "tool.call.toolview", key: "kb_search" },
        (props) => React.createElement(SourceList, props),
      ));
      slots.inject("tool.call.toolview", () => slots.register(
        { name: "tool.call.toolview", key: "kb_ingest" },
        (props) => React.createElement(IngestCard, props),
      ));
      slots.inject("tool.call.toolview", () => slots.register(
        { name: "tool.call.toolview", key: "kb_zotero" },
        zoteroView,
      ));
      slots.inject("conversation.session.header.actions", () => slots.register(
        { name: "conversation.session.header.actions", id: "kb-rag-chip", order: 90, label: "kb" },
        (props) => React.createElement(KbChip, props),
      ));
    }

    exports.apply = apply;
    exports.inject = inject;
    return module.exports;
  },
});
