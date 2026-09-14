// 提示层规则：9 条规则 × 7 种真实响应形状 + 深挖分档 + 节流 + 会话提示 + 描述注入。
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { makeReporter, preparePkg } from './_nodehelper.mjs';

const rep = makeReporter();
const { dir } = preparePkg(process.cwd());
const g = await import(pathToFileURL(join(dir, 'lib', 'guidance.js')).href);

const mkThrottle = () => {
  const store = new Map();
  return g.makeThrottle((id) => store.get(id), (id, v) => store.set(id, v));
};

const cases = {
  normal: { ok: true, mode_used: 'hybrid', reranker: 'BAAI/bge-reranker-base', ms: 800,
            verdict: '相关', max_score: 0.98, results: [{ score: 1.12 }, { score: 0.86 }] },
  offKb: { ok: true, mode_used: 'hybrid', reranker: 'r', ms: 2683, verdict: '无关', no_hit: true,
           max_score: 0.0375, closest: [{ title: 'X', year: 2020 }], results: [] },
  weak: { ok: true, mode_used: 'hybrid', reranker: 'r', ms: 900, verdict: '弱相关', max_score: 0.22,
          results: [{ score: 0.22 }] },
  degraded: { ok: true, mode_used: 'keyword', embedding_error: 'ImportError: scipy',
              vectors_missing: 9000, ms: 320, results: [{ score: 0.4 }] },
  cjk: { ok: true, mode_used: 'hybrid', reranker: 'r', ms: 3600, lang_note: '库内正文以英文为主…',
         results: [{ score: 0.48 }] },
  slow: { ok: true, mode_used: 'hybrid', reranker: 'r', ms: 7400, results: [{ score: 0.9 }] },
  disabled: { ok: true, kb_rag_disabled: true, results: [] },
  // 真实引擎在 quick 模式下的形态（实测抓下来的）：`reranker` 键**在**、值是 `null`，
  // `verdict` 也是 `null`，分数是 RRF 融合分（无量纲，实测 0.03–0.05）。
  // 判据曾经写成 `resp.reranker !== undefined`，null 通不过这个检查，于是拿融合分去比
  // 为精排分标定的 0.10 阈值（精排：库外 0.004–0.038 / 库内 0.65–1.37），
  // 把一个高相关命中误判成"库内无相关资料"，还叫 agent 不要再换词重试。
  quickNullRerank: { ok: true, mode_used: 'hybrid', reranker: null, depth: 'quick', verdict: null,
                     no_hit: false, floor: null, ms: 12596,
                     results: [{ score: 0.048 }, { score: 0.0466 }] },
  // 对照组：真跑了精排且分数确实低 —— 这种才该判 no-hit（证明规则没被改废）
  rerankedLow: { ok: true, mode_used: 'hybrid', reranker: 'BAAI/bge-reranker-base', depth: 'deep',
                 verdict: null, no_hit: false, max_score: 0.032, results: [{ score: 0.032 }] },
};

const fired = (resp, diligence = 'normal') =>
  g.resultNotes('kb_search', resp, mkThrottle(), { diligence }).fired;

rep.check('正常命中不触发停损规则', fired(cases.normal).length === 0, JSON.stringify(fired(cases.normal)));
rep.check('库外 → no-hit', fired(cases.offKb).includes('no-hit'));
rep.check('库外 → 同时给用户提示',
  g.resultNotes('kb_search', cases.offKb, mkThrottle(), {}).userHints.length > 0);
rep.check('弱相关 → weak-hit', fired(cases.weak).includes('weak-hit'));
rep.check('向量降级 → degraded-vectors', fired(cases.degraded).includes('degraded-vectors'));
rep.check('中文命中差 → cjk-query', fired(cases.cjk).includes('cjk-query'));
rep.check('慢调用 → slow-call', fired(cases.slow).includes('slow-call'));
rep.check('已关闭 → disabled', fired(cases.disabled).includes('disabled'));
// quick 模式（reranker=null）不得用地板：分数不是一个量纲
rep.check('quick（reranker=null）不误报 no-hit', !fired(cases.quickNullRerank).includes('no-hit'),
          JSON.stringify(fired(cases.quickNullRerank)));
rep.check('quick（reranker=null）不误报 weak-hit', !fired(cases.quickNullRerank).includes('weak-hit'),
          JSON.stringify(fired(cases.quickNullRerank)));
rep.check('quick（reranker=null）没有任何相关性类停损规则',
          !['no-hit', 'no-hit-thorough', 'weak-hit'].some((id) => fired(cases.quickNullRerank).includes(id)),
          JSON.stringify(fired(cases.quickNullRerank)));
rep.check('quick（reranker=null）只可能因慢触发 slow-call（与相关性判定无关）',
          fired(cases.quickNullRerank).every((id) => id === 'slow-call'),
          JSON.stringify(fired(cases.quickNullRerank)));
// 对照组：真跑了精排且分数低 → 仍然判 no-hit
rep.check('精排后低分仍触发 no-hit（规则没被改废）', fired(cases.rerankedLow).includes('no-hit'),
          JSON.stringify(fired(cases.rerankedLow)));
rep.check('非检索工具不触发这些规则',
  g.resultNotes('kb_stats', cases.degraded, mkThrottle(), {}).fired.length === 0);
rep.check('kb_ingest 不触发检索口径的降级文案（规则限定 tools）',
  g.resultNotes('kb_ingest', cases.degraded, mkThrottle(), {}).fired.length === 0);

// 深挖分档
const thFired = fired(cases.offKb, 'thorough');
rep.check('深挖：不触发叫停类 no-hit', !thFired.includes('no-hit'), JSON.stringify(thFired));
rep.check('深挖：触发补库指引 no-hit-thorough', thFired.includes('no-hit-thorough'));
const thLines = g.resultNotes('kb_search', cases.offKb, mkThrottle(), { diligence: 'thorough' }).lines.join('\n');
rep.check('深挖文案含 kb_fetch 动作', /kb_fetch/.test(thLines));
rep.check('深挖文案不含"不要再换词"', !/不要再换词/.test(thLines));
// 措辞自洽：调用纪律第 1 条要求"每次必须换实质策略（中文→英文术语 / 放宽 filters / 换同义术语）"，
// 第 2 条却说"不要再换词连试" —— 同一个词在两行里一正一反，实测模型会在"换术语"和"别换词"之间空转。
const nhDefaultLines = g.resultNotes('kb_search', cases.offKb, mkThrottle(), {}).lines.join('\n');
rep.check('默认档 no-hit 用"不要再反复重试"而非"不要再换词重试"',
  /不要再反复重试/.test(nhDefaultLines) && !/不要再换词/.test(nhDefaultLines), nhDefaultLines.slice(0, 160));
rep.check('调用纪律不再出现"不要再换词"（与"必须换实质策略"矛盾）',
  !/不要再换词/.test(g.disciplineText('normal')) && /不要再反复连试/.test(g.disciplineText('normal')));

// 节流
const store = new Map();
const throttle = g.makeThrottle((id) => store.get(id), (id, v) => store.set(id, v));
const firedSeq = [];
for (let i = 1; i <= 4; i += 1) {
  firedSeq.push(g.resultNotes('kb_search', cases.offKb, throttle, {}).fired.length);
}
rep.check('no-hit 每会话最多 2 次（第 3 次静默）', firedSeq.join(',') === '1,1,0,0', firedSeq.join(','));

// 会话层一次性提示
const s1 = g.sessionNotes(mkThrottle());
const t2 = mkThrottle();
g.sessionNotes(t2);
rep.check('session-intro 只发一次', g.sessionNotes(t2).length === 0);
rep.check('session-intro 里写了 /kb 命令', /\/kb/.test(s1.map((s) => s.text).join('\n') || ''));

// 描述注入
const injected = g.guidedDescription('kb_search', '在知识库中做混合检索…');
rep.check('kb_search 描述含调用纪律', injected.includes('调用纪律'));
rep.check('kb_search 描述含深挖指引', /thorough/.test(injected));
rep.check('kb_stats 未被注入纪律', g.guidedDescription('kb_stats', 'X') === 'X');
rep.check('深挖档描述换成补库循环',
  /深挖模式/.test(g.disciplineText('thorough')) && /kb_fetch/.test(g.disciplineText('thorough')));

// 规则清单
const inv = g.policyInventory();
rep.check('规则清单非空且带 doc', inv.length >= 9 && inv.every((p) => p.id && p.seat), inv.length);
rep.check('结果层规则都限定了工具（避免误伤非检索工具）',
  inv.filter((p) => p.seat === 'result' && p.id !== 'disabled').every((p) => p.tools), JSON.stringify(inv));
rep.finish();
