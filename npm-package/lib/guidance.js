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

/**
 * 这一轮检索到底跑没跑精排。
 *
 * **必须用真值判断，不能判 `!== undefined`**：引擎在没有精排时返回的是 `reranker: null`
 * （键在、值是 null），而 quick 模式就是这样。判 `!== undefined` 会让守卫失效，于是拿
 * RRF 融合分（无量纲，实测 0.03–0.05）去比**为精排分标定**的阈值 0.10（精排库外 0.004–0.038、
 * 库内 0.65–1.37），把一个高相关命中误判成"库内无相关资料"，还会叫 agent 不要再换词重试。
 * 渲染层（host 的 score 显示）用的就是真值判断，这里与它对齐。
 */
function hasReranker(resp) {
  return !!resp && typeof resp.reranker === 'string' && resp.reranker.length > 0;
}

function isEmptyResult(resp) {
  if (!resp || resp.ok !== true) return false;
  if (resp.no_hit === true || resp.verdict === '无关') return true;
  const list = resp.results || resp.evidence;
  if (Array.isArray(list) && list.length === 0) return true;
  // 引擎已经落地了地板：有 verdict 时以引擎为准（引擎在 quick 下返回 verdict=null）
  if (resp.verdict !== undefined && resp.verdict !== null) return false;
  const s = maxScore(resp);
  return s !== null && s < RELEVANCE_FLOOR.rerank && hasReranker(resp);
}

function isWeakResult(resp) {
  if (resp && resp.verdict === '弱相关') return true;
  const s = maxScore(resp);
  return s !== null && hasReranker(resp) && s >= RELEVANCE_FLOOR.rerank && s < 0.35;
}

// ---------------------------------------------------------------- 静态描述块

/** 调用纪律：拼进 kb_search / kb_rag 的描述末尾。每条都是"可执行动作"，不是泛泛要求。 */
export const SEARCH_DISCIPLINE = [
  '调用纪律（重要，违反会显著拖慢回答）：',
  `1. 一次提问最多调用本工具 ${MAX_SEARCH_CALLS_PER_QUESTION} 次，每次必须换实质策略（换术语：英文库转英文术语 / 放宽 filters / 换同义术语），不要反复改写同一句话。`,
  '2. 返回 verdict=无关（或结果分数低于阈值）时：库内确实没有 → 如实说明"库内无资料"，并按 scope 设置转 web_search；不要再反复连试。',
  '3. 返回 verdict=弱相关时：最多升一次 depth=deep；仍弱则按第 2 条处理。',
  '4. 用户说"先联网/快点/不用查库"→ 用 scope=web 或直接 web_search；用户说"库里有没有/只查库"→ scope=kb，只查一次。',
  '5. 检索语言和库内正文保持一致：英文库把中文问句转写成英文术语串（BM25 对中文空转）；中文库直接用中文术语。不确定时先按用户原话查一次，看返回的 lang_note。',
  '6. **该升级到 depth=deep 的三种情况**（其余保持 quick 直答）：① 用户明确要背景/展开/对比/"为什么"；'
    + '② 问题必须跨文献综合才答得完整（综述、路线对比、多源争议）；③ 证据不足或互相矛盾，需要说明局限。',
  '7. **正文引用格式（强制）**：每个事实后写 `[文献标题, 年份](https://doi.org/DOI)`；标题超过 40 字截断加「…」，'
    + '年份缺失写「年份未记录」。**不要**只写 `[n]`、`[n](DOI)` 或把数字当链接文字——`[n]` 只用于来源列表和'
    + '`fields.refs`。标题是最强的「这是检索结果」信号，期刊在来源列表里核对。',
  '8. **来源说明（必须放在回答最后一行）**：固定写 `以上内容全部由 kb-rag 查询反馈信息整理。`；'
    + '如果里面有明显超出库内证据的推导/推断，写成 `以上内容全部由 kb-rag 查询反馈信息整理，推断部分为模型补充。`'
    + '这是用户确认「这段是检索结果、不是模型自由发挥」的唯一标志，别省略、别改位置。',
].join('\n');

/** 默认值说明：让 agent 知道"不传参数会发生什么"。 */
export const DEFAULTS_NOTE = [
  '默认：深度=quick（亚秒级）。需要跨文献综合/精排时显式传 depth=deep（每次多约 1.9 s）。',
  '**两段式（默认就按这个走）**：第一跳先 quick 直答——一句话给结论（≤30 字、带 `[标题, 年份](DOI)`），零叙述、'
    + '不复述问题、不总结检索过程；用户追问"详细说说/展开/为什么"，或命中上面第 6 条的三种情况时，'
    + '第二跳再用 depth=deep 展开。慢的不是查库（quick 实测几十毫秒），是生成——少写过渡句就是快。',
  '要交一份能打开/能分享的成品时，在同一次调用里带 `answer`：引擎按预设模板把答案渲染成自包含 HTML '
    + '并落盘，路径随结果返回（不经 LLM、无第二次往返）。`answer:{stage:"instant"}` 自动锁死字段与 '
    + '300 字符预算（只填 refs 编号与短语）；`answer:{stage:"detail", template:"review|method|compare|…", fields:{…}}` '
    + '给第二跳的完整模板。',
].join('\n');

/** 意图自审：回答前先判断"要多深"，只在真正含糊时问一次。
 *  这是"先探针、再询问"的规则层——不是每次都问，而是先自审、再决定。 */
export const INTENT_REVIEW = [
  '意图自审（回答前过一遍，决定“答多深”；不是每次都问）：',
  'A. 用户已给粒度（“一句话/简短/详细/综述/对比表/深入/别展开”）→ 照做，不问。',
  'B. 事实单点（某个值、年份、定义、有没有）→ 直接 quick 回答：一句话 + 来源链接，不问。',
  'C. 开放问题（综述、路线、对比、选型、为什么、怎么设计）且用户没说粒度 → 先探后问，只问一次：',
  '   1) 先调 kb_search({probe:true}) 或 kb_rag({probe:true})：只回标题级命中（约 0.3 s，不生成答案）；',
  '   2) 用一句话说“库里有什么”（命中数 + 2–3 篇标题/年份）+ 一句初步结论；',
  '   3) 只问一个问题：2–3 个选项、每个写清产物与代价、给默认项。例如：',
  '      “要展开成 ① 三篇对比表 ② 方法路线 ③ 详细综述（约 5 s）吗？不选就按一句话。”',
  '   4) 用户选定后调 kb_rag({depth:"deep", answer:{template:"compare|method|review", …}})；',
  '      不重新探针、不重复问，沿用用户这次的选择。',
  'D. 禁止：同一话题问第二次；探针命中 <2 篇却提供“对比”；把问题写成开放式（“你想了解什么”）；',
  '   用户已说粒度还再问。探针 0 命中 → 直接说“库里没有”并给补库/联网出口，不提问。',
  'E. 最终答复前自检四条：① 粒度与用户选择/暗示一致；② 每个事实后有 `[标题, 年份](DOI)`、数字能在 evidence 里找到；',
  '   ③ 同一话题没有重复提问；④ 无资料时明确说没有 + 下一步，不编造。',
].join('\n');

/** 深挖模式（用户明确要求"彻底查"时）：解除调用上限，改成"补库 → 再查"的循环。
 *  触发方式：用户明说"仔细找/慢慢来/别省时间/把相关文献都找齐/穷尽"，或用 /kb thorough /
 *  kb_scope(diligence="thorough")。这一块的目的是：**不要为了省一次调用，把困难问题答成
 *  "库里没有"**。 */
export const THOROUGH_LOOP = [
  '**深挖模式已开启**（用户明确要求彻底查找；调用次数不设上限，优先把事情查透）：',
  '1. 逐术语、逐角度检索：同一问题可以换多组术语（语言与库内正文一致）、放宽或更换 filters，直到覆盖主题各个侧面。',
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

// ---------------------------------------------------------------- 答案格式速查表
//
// 为什么这张表必须出现在**工具描述**里：agent 从 schema 只能看到 `fields` 是个自由对象，
// 看不到某个模板到底有哪些 section id、每个块要什么形状。于是它只有两条路 —— 猜（错一次
// 就是一次失败往返，用户等两倍），或者先去读引擎源码（更贵）。把格式表放进描述，等于每轮
// 花几百 token 换掉一整类失败往返，净赚。
//
// 省 token 的是**文字**，不是**数据**：表格/图表/数值条的轴界、刻度、条长、热度、排序、
// 单位全部由引擎算，agent 只把数字和标签填进去。
//
// 成本分档（也写进提示里，避免 agent 默认把所有格子填满）：
//   · = 几句话；⚙ = 要填结构，只在用户真的要看数据时填

/** 每个模板的字段形状。id / section id 必须与引擎 _ans_TEMPLATES 保持一致。 */
export const ANSWER_TEMPLATES = [
  { id: 'instant', label: '要点直答', secs: ['answer·结论', 'limit·限制', 'followup·待解决'] },  { id: 'review', label: '综述', secs: ['bottom_line·主要结论', 'state·研究现状', 'methods·技术路线', 'limits·分歧与局限', 'takeaway·评价'] },
  { id: 'compare', label: '对比', secs: ['bottom_line·主要结论', 'matrix·对比表 ⚙', 'numbers·数值对比 ⚙', 'reading·读表说明'] },
  { id: 'numeric', label: '数值发现', secs: ['bottom_line·主要结论', 'kpis·关键指标 ⚙', 'numbers·数值对比 ⚙', 'trend·变化趋势 ⚙', 'reading·读图说明'] },
  { id: 'method', label: '方法', secs: ['what·方法', 'when·适用范围', 'steps·工艺流程 ⚙', 'params·参数与条件 ⚙', 'figure·配图', 'caveats·局限性'] },
  { id: 'timeline', label: '时间线', secs: ['bottom_line·主要结论', 'line·研究脉络 ⚙'] },
  { id: 'concept', label: '概念速查', secs: ['cards·术语'] },
  { id: 'dispute', label: '争议清单', secs: ['bottom_line·主要结论', 'issues·分歧点'] },
  { id: 'derivation', label: '推导', secs: ['conventions·约定', 'deriv·推导 ⚙', 'figure·图', 'result·结果', 'code·代码', 'verify·验证', 'pitfalls·坑'] },
  { id: 'qa', label: '答疑', secs: ['intro·说明', 'questions·问答 ⚙', 'summary·结论'] },
];

/** 每块写什么形状：一行一条，前面是块类型。 */
export const BLOCK_SHAPES = [
  'note / text  {"text":"…","refs":[1]}',
  'list         ["…","…"] 或 [{"text":"…","refs":[1]}]',
  'table        **直接写 markdown 管道表**（推荐）；或 {"columns":[{"label":"方式"},{"label":"密度","num":true}],'
    + '"rows":[["电抛光","4×10³"]]}。列可加 num（数字右对齐）/ heat（列内热力着色）/ bar（格内条）；缺数据写"未报告"',
  'rank / bars  {"unit":"cm⁻²","log":true,"fold":true,"items":[{"label":"电抛光","value":"4×10³","tone":"hi","thr":"1e4"}]}',
  'chart        {"x":["800","900"],"x_label":"温度(°C)","y_label":"覆盖率(%)","series":[{"name":"单层","values":[61,74],"thr":90}]}',
  'flow         {"nodes":["清洗","电抛光","氧气预处理","生长"],'
    + '"edges":[{"from":4,"to":3,"kind":"fail","label":"双层>5% → 退回"}]}',
  'kpi / scalars{"items":[{"label":"单层覆盖率","value":"97","unit":"%","tone":"ok","range":"82→97","dir":"up"}]}',
  'timeline     [{"year":"2019","text":"氧气预处理把成核压 2 个数量级"}]（排事件）'
    + ' 或 {"x":["2013","…","2020"],"series":[{"values":[1e6,8e2]}]}（画曲线）',
];

/** 拼成注入 kb_rag 描述的一段文本。 */
export function answerFormatText() {
  const lines = ['**answer 格式速查**（`fields` 的键就是下面每行的第一个词；⚙ = 要填结构）：'];
  for (const t of ANSWER_TEMPLATES) lines.push('· ' + t.id + ' ' + t.label + ' — ' + t.secs.join(' | '));
  lines.push('块形状：');
  for (const b of BLOCK_SHAPES) lines.push('  ' + b);
  lines.push('**行文口径**：小节内直接陈述事实与数据，不写过渡句、不写"本文/该研究探讨了…"这类套话；结论必须是可判真假的陈述（含数值、条件或机制），不是"具有重要意义"这种评价；数字与单位/条件同现；不确定的写"未报告"或"文献间不一致"，不要含糊其辞。');
  lines.push('省时间的三条：① **别把格子填满**，只填能回答用户问题的节，未填的自动省略；'
    + '② 文字节一句话就够，超长会被渲染端截断；'
    + '③ 数字原样写（"4×10³"、"10⁶–10⁷"、"3.2 V" 都认），**不要自己算轴界、排序或百分比** —— 那是引擎的活。');
  return lines.join('\n');
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
  {
    id: 'answer-format',
    seat: 'description',
    // 只挂 kb_rag：只有它接受 answer 参数，挂到 kb_search 上是白花的 token
    tools: ['kb_rag'],
    when: () => true,
    text: () => answerFormatText(),
    doc: '把答案模板与可视块的填写形状放进描述 —— 治"agent 猜字段名导致失败往返"（1.6.9 新增）',
  },
  {
    id: 'intent-review',
    seat: 'description',
    tools: ['kb_search', 'kb_rag'],
    when: () => true,
    text: () => INTENT_REVIEW,
    doc: '回答前自审粒度：事实直答、开放问题先探针再问一次（1.6.9 新增）',
  },

  // —— ② 结果层：条件触发，其中 tellUser 的那些要能被用户看到
  {
    id: 'no-hit',
    seat: 'result',
    tools: ['kb_search', 'kb_rag'],
    stopRule: true,          // 深挖模式下不适用（用户要求"查透"，不该在这里叫停）
    when: (resp) => isEmptyResult(resp),
    text: (resp) => {
      const s = maxScore(resp);
      return '⚠ 库内无相关资料' + (s !== null ? '（最高相似度 ' + s.toFixed(2) + '，低于阈值 ' + RELEVANCE_FLOOR.rerank + '）' : '')
        + '：不要再反复重试。请如实说明库内无资料，并按 scope 设置转 web_search。';
    },
    tellUser: true,
    userText: () => '库内没有找到相关资料 —— 你可以用 /kb both 开启联网兜底，或 /kb web 直接联网快答。',
    throttle: { maxPerSession: 2 },
    doc: '把"没有"变成不可误读的信号，并给用户一条可执行的出口（引擎 verdict=无关 时最准）',
  },
  {
    id: 'no-hit-thorough',
    seat: 'result',
    tools: ['kb_search', 'kb_rag'],
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
    tools: ['kb_search', 'kb_rag'],
    stopRule: true,
    when: (resp) => isWeakResult(resp),
    text: () => '提示：本次结果相关性偏弱。最多再升一次 depth=deep；仍弱则按"库内无资料"处理，不要连环换词。',
    throttle: { maxPerSession: 2 },
    doc: '弱命中时给一次明确的升级机会，避免 agent 自由发挥式重试（深挖模式不设限）',
  },
  {
    id: 'degraded-vectors',
    seat: 'result',
    // 只对检索类工具：kb_ingest / kb_zotero 也有 embedding_error 字段，但那里的文案由
    // renderIngest 自己给（"未建向量（原因）…"），用检索的口径会说成"本次检索已退化"，误导。
    tools: ['kb_search', 'kb_rag'],
    // 显式传 mode:"keyword" 时 mode_used 也是 keyword，但这不是降级 —— 只有"要 hybrid
    // 却被迫退成 keyword"才提示。请求模式由引擎放在 mode_requested 里（旧引擎没有该字段，
    // 按 hybrid 处理，保持原来的告警行为，不会漏报）。
    when: (resp) => Boolean(resp && (resp.embedding_error
      || (resp.mode_used === 'keyword' && (resp.mode_requested || 'hybrid') !== 'keyword')))
      || (resp && resp.vectors_missing > 0),
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
    tools: ['kb_search', 'kb_rag'],
    when: (resp) => typeof (resp && resp.lang_note) === 'string' && resp.lang_note.length > 0,
    text: () => '中文查询在英文库上关键词路基本空转：请用英文术语重查一次；若仍不满意，按 scope 转联网。',
    throttle: { cooldownMs: 300000 },
    doc: '中文提问命中差时给出一次明确的转写动作（不再由 agent 自己反复翻译）',
  },
  {
    id: 'slow-call',
    seat: 'result',
    tools: ['kb_search', 'kb_rag'],
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
