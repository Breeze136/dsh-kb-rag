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
