// kb-rag agent guidance framework — 声明式提示层（零依赖，纯数据 + 纯函数）
//
// 为什么有这一层：给 agent 的"调用纪律"、给用户的"摩擦提示"会越来越多，散落在
// 10 个 defineTool 的描述和渲染函数里必然失控。这里把每条提示变成一条**规则数据**，
// 由三个统一的注入点消费：
//
//   ① seat: 'description' —— 静态文本并入工具描述（agent 每轮都会读到）
//   ② seat: 'result'      —— 命中条件时并入工具结果（含 tellUser 标记，可由客户端渲染给用户）
//   ③ seat: 'session'     —— 每会话一次的引导/开场提示
//
// 加一条新规则 = 往 POLICIES 里加一个对象（见文件末的模板），不需要改宿主逻辑。
//
// 规则对象字段：
//   id        唯一标识（也用于节流记账与 /kb policy 打印）
//   seat      注入点：'description' | 'result' | 'session'
//   tools     适用范围（工具名数组；seat=description 时必填）
//   when      (resp, ctx) => boolean   命中条件（seat=result/session 用）
//   text      (resp, ctx) => string    要注入的文本
//   tellUser  true 时这条同时是"给用户看的提示"（客户端/转达消费）
//   throttle  { once?: boolean, cooldownMs?: number, maxPerSession?: number }
//   doc       一句话说明这条规则解决什么问题（给维护者看，也用于生成文档）

/** 相关性下限（与引擎侧 KB_MIN_COS / KB_MIN_RERANK 对齐；引擎落地前先用这里的阈值判定）。 */
export const RELEVANCE_FLOOR = {
  rerank: 0.10,   // 精排分：实测库外 0.004–0.038，库内 0.65–1.37
  comment: '低于此值视为"库内无相关资料"；引擎侧落地后以引擎返回的 verdict 为准',
};

/** 单次提问的检索调用上限（写进描述，用于止损）。 */
export const MAX_SEARCH_CALLS_PER_QUESTION = 3;

const CJK_RE = /[\u4e00-\u9fff]/;

function maxScore(resp) {
  const list = (resp && (resp.results || resp.evidence)) || [];
  const scores = list.map((r) => Number(r && r.score)).filter((n) => Number.isFinite(n));
  return scores.length ? Math.max(...scores) : null;
}

function isEmptyResult(resp) {
  if (!resp || resp.ok !== true) return false;
  if (resp.no_hit === true || resp.verdict === '无关') return true;
  const list = resp.results || resp.evidence;
  if (Array.isArray(list) && list.length === 0) return true;
  const s = maxScore(resp);
  return s !== null && s < RELEVANCE_FLOOR.rerank && resp.reranker !== undefined;
}

function isWeakResult(resp) {
  if (resp && resp.verdict === '弱相关') return true;
  const s = maxScore(resp);
  return s !== null && resp && resp.reranker !== undefined && s >= RELEVANCE_FLOOR.rerank && s < 0.35;
}

// ---------------------------------------------------------------- 静态描述块

/** 调用纪律：拼进 kb_search / kb_rag 的描述末尾。每条都是"可执行动作"，不是泛泛要求。 */
export const SEARCH_DISCIPLINE = [
  '调用纪律（重要，违反会显著拖慢回答）：',
  `1. 一次提问最多调用本工具 ${MAX_SEARCH_CALLS_PER_QUESTION} 次，每次必须换实质策略（中文→英文术语 / 放宽 filters / 换同义术语），不要反复改写同一句话。`,
  '2. 返回 verdict=无关（或结果分数低于阈值）时：库内确实没有 → 如实说明"库内无资料"，并按 scope 设置转 web_search；不要再换词连试。',
  '3. 返回 verdict=弱相关时：最多升一次 depth=deep；仍弱则按第 2 条处理。',
  '4. 用户说"先联网/快点/不用查库"→ 用 scope=web 或直接 web_search；用户说"库里有没有/只查库"→ scope=kb，只查一次。',
  '5. 中文提问先转写成英文术语再检索（库内正文以英文为主，BM25 对中文空转）；转写一次即可。',
].join('\n');

/** 默认值说明：让 agent 知道"不传参数会发生什么"。 */
export const DEFAULTS_NOTE = [
  '默认：深度=quick（亚秒级）。需要跨文献综合/精排时显式传 depth=deep（每次多约 1.9 s）。',
].join('\n');

/** 深挖模式（用户明确要求"彻底查"时）：解除调用上限，改成"补库 → 再查"的循环。
 *  触发方式：用户明说"仔细找/慢慢来/别省时间/把相关文献都找齐/穷尽"，或用 /kb thorough /
 *  kb_scope(diligence="thorough")。这一块的目的是：**不要为了省一次调用，把困难问题答成
 *  "库里没有"**。 */
export const THOROUGH_LOOP = [
  '**深挖模式已开启**（用户明确要求彻底查找；调用次数不设上限，优先把事情查透）：',
  '1. 逐术语、逐角度检索：同一问题可以换多组英文术语、放宽或更换 filters，直到覆盖主题各个侧面。',
  '2. 库内只有一两篇相关文献时，**不要把"就这么点"当成结论**：从结果的 citations 里取 DOI，',
  '   用 kb_fetch({ identifiers: [doi], ingest: true }) 下载并**直接入库**，再对新增文献继续检索。',
  '3. 循引文与 related 横向展开：新入库的文献再查一次引文关联，把它们的参考文献也纳入候选。',
  '4. 每轮把「新增了什么 / 还缺什么」简短告诉用户，直到收敛（没有新文献、没有新结论）或用户喊停。',
  '5. 增量入库按 sha256 自动跳过已入库文件，重复调用安全；大批量会自动转后台，用 kb_status 轮询。',
  '6. 只有在**确实把所有角度都检索完**之后，才可以说"库内无相关资料"。',
].join('\n');

/** 描述层文本按纪律分派：默认档给纪律，深挖档给循环。
 *  注意：工具描述是**注册时定死**的（不随会话变化），所以两种档都要在描述里交代清楚，
 *  否则深挖模式下的描述还在说"最多 3 次"，与运行期行为自相矛盾。 */
export function disciplineText(diligence) {
  const pointer = '注：用户明确要求彻底查找时（"仔细找/慢慢来/别省时间/把相关文献都找齐"），'
    + '先 kb_scope({ diligence: "thorough" })（或让用户用 /kb thorough）——该模式下上述调用上限解除，'
    + '改为「反复检索 → kb_fetch(ingest=true) 补库 → 引文关联 → 增量入库 → 再查」的循环。';
  if (diligence === 'thorough') return THOROUGH_LOOP + '\n' + DEFAULTS_NOTE;
  return SEARCH_DISCIPLINE + '\n' + DEFAULTS_NOTE + '\n' + pointer;
}

// ---------------------------------------------------------------- 规则表

export const POLICIES = [
  // —— ① 描述层：每次请求都读得到，用于约束行为
  {
    id: 'search-discipline',
    seat: 'description',
    tools: ['kb_search', 'kb_rag'],
    when: () => true,
    text: (_resp, ctx) => disciplineText(ctx && ctx.diligence),
    doc: '限制 agent 的检索轮次并教它按用户意图选范围/深度（治"来回找"）；深挖模式换成补库循环',
  },

  // —— ② 结果层：条件触发，其中 tellUser 的那些要能被用户看到
  {
    id: 'no-hit',
    seat: 'result',
    stopRule: true,          // 深挖模式下不适用（用户要求"查透"，不该在这里叫停）
    when: (resp) => isEmptyResult(resp),
    text: (resp) => {
      const s = maxScore(resp);
      return '⚠ 库内无相关资料' + (s !== null ? '（最高相似度 ' + s.toFixed(2) + '，低于阈值 ' + RELEVANCE_FLOOR.rerank + '）' : '')
        + '：不要再换词重试。请如实说明库内无资料，并按 scope 设置转 web_search。';
    },
    tellUser: true,
    userText: () => '库内没有找到相关资料 —— 你可以用 /kb both 开启联网兜底，或 /kb web 直接联网快答。',
    throttle: { maxPerSession: 2 },
    doc: '把"没有"变成不可误读的信号，并给用户一条可执行的出口（引擎 verdict=无关 时最准）',
  },
  {
    id: 'no-hit-thorough',
    seat: 'result',
    thoroughOnly: true,      // 只在深挖模式生效：把"没有"转成"下一步补库"
    when: (resp) => isEmptyResult(resp),
    text: (resp) => {
      const close = Array.isArray(resp && resp.closest) ? resp.closest : [];
      const hint = close.length > 0
        ? '库内最接近的是「' + String(close[0].title || '').slice(0, 40) + '」，可据其主题/作者再试一组术语。'
        : '';
      return '深挖模式：本轮没命中，**不要收尾**。下一步按顺序做：① 换术语或放宽 filters 再检索；'
        + '② 从已命中结果的 citations 里取 DOI，用 kb_fetch({ identifiers: [doi], ingest: true }) 补库后重查；'
        + '③ 用 related 列表横向扩展。' + hint;
    },
    throttle: { maxPerSession: 3 },
    doc: '深挖模式下把"无命中"变成"继续补库"的动作指令（用户明确要求查透时用）',
  },
  {
    id: 'weak-hit',
    seat: 'result',
    stopRule: true,
    when: (resp) => isWeakResult(resp),
    text: () => '提示：本次结果相关性偏弱。最多再升一次 depth=deep；仍弱则按"库内无资料"处理，不要连环换词。',
    throttle: { maxPerSession: 2 },
    doc: '弱命中时给一次明确的升级机会，避免 agent 自由发挥式重试（深挖模式不设限）',
  },
  {
    id: 'degraded-vectors',
    seat: 'result',
    when: (resp) => Boolean(resp && (resp.embedding_error || resp.mode_used === 'keyword')) || (resp && resp.vectors_missing > 0),
    text: (resp) => {
      const why = resp.embedding_error ? '（' + String(resp.embedding_error).slice(0, 120) + '）' : '';
      return '注意：向量链路当前不可用' + why + '，本次检索已退化为纯关键词，跨语言/同义检索会明显变差。'
        + '请在回答中说明这一点，并提示用户修好环境后重跑 kb_ingest 可补齐缺失向量。';
    },
    tellUser: true,
    userText: () => '当前向量索引不可用，检索已退化为关键词匹配（/kb 可查看状态；修好环境后重跑入库可补齐）。',
    throttle: { cooldownMs: 600000 },
    doc: '把 issue #2 的静默降级变成显式告知（引擎 embedding_error/mode_used 落地后自动生效）',
  },
  {
    id: 'cjk-query',
    seat: 'result',
    when: (resp) => typeof (resp && resp.lang_note) === 'string' && resp.lang_note.length > 0,
    text: () => '中文查询在英文库上关键词路基本空转：请用英文术语重查一次；若仍不满意，按 scope 转联网。',
    throttle: { cooldownMs: 300000 },
    doc: '中文提问命中差时给出一次明确的转写动作（不再由 agent 自己反复翻译）',
  },
  {
    id: 'slow-call',
    seat: 'result',
    when: (resp) => Number(resp && resp.ms) > 5000,
    text: (resp) => '本次检索耗时 ' + Math.round(resp.ms / 1000) + ' s（deep 会跑精排）。快速问答可用 depth=quick；只有需要综述时才用 deep。',
    throttle: { cooldownMs: 600000 },
    doc: '把"慢"归因讲清楚，引导 agent 在有需要时才用 deep',
  },
  {
    id: 'disabled',
    seat: 'result',
    when: (resp) => Boolean(resp && resp.kb_rag_disabled),
    text: () => 'kb-rag 当前处于关闭状态，本次调用未检索。请直接使用 web_search 或告知用户可用 /kb on 开启库内检索。',
    tellUser: true,
    userText: () => 'kb-rag 已关闭 —— 输入 /kb on 可开启库内检索。',
    doc: '软关闭状态下给 agent 与用户各一条明确指令（配合三档关闭语义）',
  },

  // —— ③ 会话层：每会话一次的开场/引导
  {
    id: 'session-intro',
    seat: 'session',
    when: () => true,
    text: () => [
      'kb-rag 已启用（默认库内快查）。可选范围与深度如下，选项后括号内是等价命令：',
      '· 库内快查（推荐）/kb quick · 只查本地文献库，亚秒级',
      '· 库内 + 联网兜底 /kb both · 库里没有就上网补',
      '· 只联网快答 /kb web · 不查库，直接 web_search',
      '· 本次不用 /kb off · 需要时再开',
    ].join('\n'),
    throttle: { once: true },
    doc: '首次会话边问边教：把命令写在选项里，用户不用先学命令',
  },
];

// ---------------------------------------------------------------- 三个注入点

/**
 * ① 描述注入：把 seat='description' 的规则拼进工具描述。
 * @param {string} toolName 工具名
 * @param {string} description 原描述
 * @returns {string} 注入后的描述
 */
export function guidedDescription(toolName, description) {
  const blocks = POLICIES
    .filter((p) => p.seat === 'description' && (!p.tools || p.tools.indexOf(toolName) >= 0))
    .filter((p) => (typeof p.when !== 'function' ? true : p.when({}, { tool: toolName })))
    .map((p) => p.text({}, { tool: toolName }))
    .filter(Boolean);
  return blocks.length ? description + '\n\n' + blocks.join('\n\n') : description;
}

/**
 * ② 结果注入：返回命中规则产出的提示行。
 * @param {string} toolName 工具名
 * @param {object} resp 引擎响应
 * @param {object} [state] 节流状态适配器（见 makeThrottle）
 * @returns {{lines: string[], userHints: string[], fired: string[]}}
 */
export function resultNotes(toolName, resp, state, ctx) {
  const lines = [];
  const userHints = [];
  const fired = [];
  const thorough = Boolean(ctx && ctx.diligence === 'thorough');
  for (const p of POLICIES) {
    if (p.seat !== 'result') continue;
    if (p.tools && p.tools.indexOf(toolName) < 0) continue;
    // 纪律分档：深挖模式下**不适用**"叫停类"规则（用户明确要求查透），
    // 只保留 thoroughOnly 的补库指引；默认档反之（不出现深挖指引，避免无谓地催着补库）。
    if (thorough && p.stopRule) continue;
    if (!thorough && p.thoroughOnly) continue;
    let hit = false;
    try {
      hit = typeof p.when === 'function' ? p.when(resp, { tool: toolName, diligence: thorough ? 'thorough' : 'normal' }) === true : false;
    } catch (e) {
      hit = false;   // 规则本身出错绝不能影响检索结果
    }
    if (!hit) continue;
    if (state && !state.allow(p.id, p.throttle)) continue;
    const text = p.text(resp, { tool: toolName });
    if (text) lines.push(text);
    if (p.tellUser && typeof p.userText === 'function') {
      const u = p.userText(resp, { tool: toolName });
      if (u) userHints.push(u);
    }
    fired.push(p.id);
  }
  return { lines, userHints, fired };
}

/** ③ 会话注入：返回本会话该发的一次性提示（无则空数组）。 */
export function sessionNotes(state) {
  const out = [];
  for (const p of POLICIES) {
    if (p.seat !== 'session') continue;
    if (state && !state.allow(p.id, p.throttle)) continue;
    const text = p.text({}, {});
    if (text) out.push({ id: p.id, text, tellUser: p.tellUser === true });
  }
  return out;
}

/**
 * 节流记账器：按会话隔离；宿主把状态存哪由宿主决定（内存 Map / state.json / 二者叠加）。
 * @param {(id: string) => object} read
 * @param {(id: string, value: object) => void} write
 */
export function makeThrottle(read, write) {
  return {
    allow(id, throttle) {
      if (!throttle) return true;
      const rec = read(id) || {};
      const now = Date.now();
      if (throttle.once && rec.once) return false;
      if (throttle.maxPerSession !== undefined && (rec.count || 0) >= throttle.maxPerSession) return false;
      if (throttle.cooldownMs !== undefined && rec.at && now - rec.at < throttle.cooldownMs) return false;
      rec.count = (rec.count || 0) + 1;
      rec.at = now;
      if (throttle.once) rec.once = true;
      write(id, rec);
      return true;
    },
  };
}

/** 维护者视图：/kb policy 用，列出当前生效规则，便于确认"到底会说什么"。 */
export function policyInventory() {
  return POLICIES.map((p) => ({ id: p.id, seat: p.seat, tools: p.tools || null, tellUser: p.tellUser === true, throttle: p.throttle || null, doc: p.doc || '' }));
}

// ---------------------------------------------------------------- 加一条新规则（模板）
//
// {
//   id: 'my-rule',                 // 唯一；节流记账与 /kb policy 都用它
//   seat: 'result',                // 'description' | 'result' | 'session'
//   tools: ['kb_search'],          // 可选：限定工具
//   when: (resp) => resp.foo > 0,  // 命中条件（纯函数，出异常会被框架吞掉）
//   text: (resp) => '……',          // 给 agent 的文本
//   tellUser: true,                // 可选：同时给用户一句
//   userText: () => '……',
//   throttle: { cooldownMs: 600000 },   // once / cooldownMs / maxPerSession
//   doc: '一句话说明这条解决什么问题',
// }
