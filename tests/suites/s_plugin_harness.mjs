// 插件载荷：stub ctx 加载 lib/index.js，验证工具注册、渲染、/kb 命令、三档关闭与会话隔离。
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { makeCtx, makeReporter, preparePkg } from './_nodehelper.mjs';

const sandbox = process.cwd();
const rep = makeReporter();
const { dir, toolsMode } = preparePkg(sandbox);
console.log('  ·  插件包：' + dir + ' ｜ dsh-tools: ' + toolsMode);

const mod = await import(pathToFileURL(join(dir, 'lib', 'index.js')).href);
const { ctx, registered, commands } = makeCtx();
mod.apply(ctx);

rep.check('inject 含 subprocess（issue #1）', mod.inject.includes('subprocess'), JSON.stringify(mod.inject));
rep.check('注册 10 个工具', registered.length === 10, registered.map((t) => t.name).join(' '));
const byName = Object.fromEntries(registered.map((t) => [t.name, t]));
const namesNow = () => registered.map((t) => t.name);

// 描述层
for (const n of ['kb_search', 'kb_rag']) {
  const d = byName[n].description || '';
  rep.check(n + ' 描述含调用纪律', d.includes('调用纪律'), d.length + ' 字符');
  rep.check(n + ' 描述含深挖指引（描述注册时定死，两档都要交代）', /thorough/.test(d));
}
rep.check('kb_stats 未被注入纪律', !(byName.kb_stats.description || '').includes('调用纪律'));

// 渲染层
const renderSearch = byName.kb_search.output.render;
const normal = { ok: true, depth: 'quick', mode_used: 'hybrid', ms: 320, results: [{ title: 'T', score: 1.2 }] };
rep.check('正常命中不追加停损提示', !/库内无相关资料|可转达给用户/.test(JSON.stringify(renderSearch({}, normal))));
const noHit = { ok: true, depth: 'deep', mode_used: 'hybrid', reranker: 'x', ms: 2400, results: [],
  no_hit: true, verdict: '无关', max_score: 0.03, floor: 0.1, floor_weak: 0.35,
  closest: [{ title: 'Alpha paper', year: 2020, section: 'Results' }] };
const nhText = JSON.stringify(renderSearch({}, Object.assign({ __session: 'X' }, noHit)));
rep.check('无命中渲染理由', /库内无相关资料/.test(nhText) && /地板/.test(nhText));
rep.check('无命中渲染 closest 列表', /Alpha paper/.test(nhText));
// P2：深挖档下无命中不得再喊"不要换词重试"。规则层在深挖档会跳过 stopRule 换成补库循环
// （guidance.js resultNotes: thorough && p.stopRule → continue），但渲染层两种档位都会跑，
// 曾无条件写那句话 —— 两段文字同时出现时模型收到"去补库"和"别重试"两条互相打架的指令。
const thNhText = JSON.stringify(renderSearch({}, Object.assign({ __session: 'X', __diligence: 'thorough' }, noHit)));
rep.check('深挖档无命中渲染补库循环、且不含任何"不要…重试"',
  /深挖模式/.test(thNhText) && /kb_fetch/.test(thNhText) && !/不要换词|不要反复改写同一句话重试/.test(thNhText),
  thNhText.slice(0, 200));
rep.check('默认档无命中给出可执行的措辞（不要反复改写同一句话）',
  /不要反复改写同一句话重试/.test(nhText), nhText.slice(0, 200));
// 同一类矛盾还有一处**活文案**（不是注释）：kb_scope 的 diligence_note 默认档曾写
// "一次提问最多 3 次检索，每次换实质策略；无命中就如实说明，不要换词穷举"——同句里一正一反。
const dNote = String((await byName.kb_scope.execute({ diligence: 'normal' }, { agent: { id: 'SP' } })).diligence_note || '');
rep.check('kb_scope 默认纪律文案不再出现"换词"矛盾',
  !/不要换词/.test(dNote) && /不要反复重试/.test(dNote), dNote);

// P3：来源块不得回吐绝对路径 —— 上一行已经印了"文件：<基名>"，绝对路径纯属重复，
// 还每篇来源泄露一次本机目录结构。
const withPath = { ok: true, depth: 'deep', mode_used: 'hybrid', ms: 100,
  results: [{ title: 'NoDOI paper', doi: null, file: 'x.pdf', path: 'D:/fake-lib/papers/x.pdf', score: 1.0, snippet: 's' }] };
const wpText = JSON.stringify(renderSearch({}, Object.assign({ __session: 'X' }, withPath)));
rep.check('来源不再重复打印绝对路径', !/fake-lib/.test(wpText) && /x\.pdf/.test(wpText), wpText.slice(0, 200));

// P4：软关闭时 wrapper 只回 {ok, kb_rag_disabled, scope, note}，没有 docs/chunks/vectors。
// 以前直接落到统计行渲染成"0 文档 / 0 块 / 0 向量"——把"已关闭"谎报成"空库"。
const dsStats = JSON.stringify(byName.kb_stats.output.render({},
  { ok: true, kb_rag_disabled: true, scope: 'kb', note: 'kb-rag 当前处于关闭状态' }));
rep.check('kb_stats 软关闭渲染「已关闭」而不是 0 文档',
  /已关闭/.test(dsStats) && !/0 文档/.test(dsStats), dsStats.slice(0, 160));
// 范围/严格提示：scopeWrapped 把 scope_note / strict_note 挂在响应上，渲染器必须真的把它们印出来。
// 曾经只印了 `strict` 这个布尔值（"· 严格模式"四个字），note 正文全被丢掉 —— 后果是 scope=both
// 时模型不知道还要去 web_search、scope=web 时不知道该以网络结果为准，两档范围静默失效。
const scopedText = JSON.stringify(renderSearch({}, Object.assign({}, normal, {
  scope: 'both', scope_note: '范围：知识库+全网。除本库内结果外，请再调用 web_search 检索开放网络。',
})));
rep.check('正常命中渲染 scope_note（scope=both 才可能去联网）', /请再调用 web_search/.test(scopedText),
  scopedText.slice(0, 140));
const strictText = JSON.stringify(renderSearch({}, Object.assign({}, normal, {
  strict: true, strict_note: '严格模式：答案仅允许基于本次检索返回的 evidence/results 内容。',
})));
rep.check('strict=true 渲染 strict_note 正文（不能只有"· 严格模式"标签）', /严格模式：答案仅允许/.test(strictText),
  strictText.slice(0, 140));
const nhScoped = JSON.stringify(renderSearch({}, Object.assign({ __session: 'X' }, noHit, {
  scope: 'web', scope_note: '范围：仅全网。本次仅给出库内命中供参考；请以 web_search 结果为准作答。',
})));
rep.check('无命中分支同样渲染 scope_note（这条路径提前 return，最易漏）', /请以 web_search 结果为准/.test(nhScoped),
  nhScoped.slice(0, 160));
const degraded = { ok: true, mode_used: 'keyword', ms: 100, embedding_error: 'ModuleNotFoundError: scipy',
  vectors_missing: 12, note: '向量索引缺失，降级为纯关键词；命中 5 块，返回 Top-2',
  results: [{ title: 'A', score: 1.1 }] };
const dText = JSON.stringify(renderSearch({}, Object.assign({ __session: 'X' }, degraded)));
rep.check('降级向量提示', /向量链路|纯关键词/.test(dText));
rep.check('note 透传且去掉重复尾巴', /降级为纯关键词/.test(dText) && !/命中 5 块，返回 Top-2/.test(dText));

const ingestRender = byName.kb_ingest.output.render({}, { ok: true, mode: 'ingest', ms: 10,
  totals: { added: 1, chunks: 9, vectors: 0 }, files: [], embedding_error: 'ModuleNotFoundError: scipy' });
rep.check('入库 0 向量给原因', /未建向量/.test(JSON.stringify(ingestRender)));
const statsRender = byName.kb_stats.output.render({}, { ok: true, docs: 3, chunks: 9, vectors: 9, ms: 5,
  embedding: 'BAAI/bge-small-zh-v1.5', embedding_error: null, vectors_missing: 0,
  health: { missing_vecs: 12, orphan_chunks: 0, docs_without_retrievable_chunks: 2, blind_sample: ['a.pdf'], ok: false },
  device: { requested: 'auto', torch: '2.0+cpu', cuda_available: false, embed_device: 'cpu' } });
const stText = JSON.stringify(statsRender);
rep.check('统计渲染嵌入/设备/索引不完整', /嵌入模型/.test(stText) && /计算设备/.test(stText) && /索引不完整/.test(stText));
rep.check('统计渲染整篇不可检索告警', /没有任何可检索分块/.test(stText));

// /kb 命令
rep.check('commands.register 调用 1 次', commands.length === 1, commands.length);
const kb = commands[0];
const inv = (id, raw) => ({ agent: { id }, rawInput: ' ' + raw });
const rStatus = await kb.handler(inv('S1', 'status'));
rep.check('status 返回状态卡', rStatus.kind === 'success' && /工具注册数：10/.test(rStatus.text));
const rBoth = await kb.handler(inv('S1', 'both'));
const rS1 = await kb.handler(inv('S1', 'status'));
const rS2 = await kb.handler(inv('S2', 'status'));
rep.check('会话隔离：S1=both 而 S2 仍是 kb',
  /范围 both/.test(rS1.text) && /范围 kb ·/.test(rS2.text),
  'S1=' + (rS1.text || '').split('\n')[2] + ' ｜ S2=' + (rS2.text || '').split('\n')[2]);
const rOff = await kb.handler(inv('S1', 'off'));
rep.check('软关闭文案', /软关闭/.test(rOff.text));
// P5：状态卡的"开关"必须区分四种档位。以前只判 enabled 布尔 → 四态塌成两标签，
// `off hard` 把工具全卸了仍写"关（软关闭）"。档位由 enabled + 实际注册数无损推出，不新增持久化字段。
const softStatus = (await kb.handler(inv('S1', 'status'))).text;
rep.check('软关闭：状态卡写"软关闭"且不写"硬关闭"',
  /软关闭/.test(softStatus) && !/硬关闭/.test(softStatus), softStatus.split('\n')[2]);
const offResp = await byName.kb_scope.execute({}, { agent: { id: 'S1' } });
rep.check('软关闭生效：调用返回 kb_rag_disabled', offResp && offResp.kb_rag_disabled === true);
const otherResp = await byName.kb_scope.execute({}, { agent: { id: 'S2' } });
rep.check('另一会话不受影响', otherResp && otherResp.ok === true && otherResp.kb_rag_disabled === undefined);
await kb.handler(inv('S1', 'on'));
rep.check('on 之后恢复（scope 保持 both）',
  (await byName.kb_scope.execute({}, { agent: { id: 'S1' } })).scope === 'both');

// 硬关闭 / 半关闭 / 重新注册
const rHard = await kb.handler(inv('S1', 'off hard'));
rep.check('硬关闭文案', /硬关闭/.test(rHard.text));
const hardStatus = (await kb.handler(inv('S1', 'status'))).text;
rep.check('硬关闭：状态卡写"硬关闭"而不是"软关闭"',
  /硬关闭/.test(hardStatus) && !/软关闭/.test(hardStatus), hardStatus.split('\n')[2]);
rep.check('硬关闭后工具数 0', namesNow().length === 0, namesNow().length);
await kb.handler(inv('S1', 'on'));
rep.check('on 后 10 个工具回来且无重名',
  namesNow().length === 10 && new Set(namesNow()).size === 10, namesNow().length);
const rHalf = await kb.handler(inv('S1', 'off search'));
rep.check('半关闭文案', /半关闭/.test(rHalf.text));
const halfStatus = (await kb.handler(inv('S1', 'status'))).text;
rep.check('半关闭：状态卡写"仅检索工具"而不是笼统的"开"',
  /仅检索工具/.test(halfStatus), halfStatus.split('\n')[2]);
rep.check('半关闭只撤检索工具',
  !namesNow().includes('kb_search') && namesNow().includes('kb_ingest'), namesNow().join(','));
await kb.handler(inv('S1', 'on'));
rep.check('半关闭后 on 能补齐 10 个（回归过）', namesNow().length === 10, namesNow().length);

// 深挖模式
const rTh = await kb.handler(inv('S3', 'thorough'));
rep.check('thorough 文案含补库循环', /深挖模式/.test(rTh.text) && /kb_fetch/.test(rTh.text));
rep.check('kb_scope 回报 diligence', (await byName.kb_scope.execute({}, { agent: { id: 'S3' } })).diligence === 'thorough');
const thText = JSON.stringify(renderSearch({}, Object.assign({ __session: 'X', __diligence: 'thorough' }, noHit)));
rep.check('深挖档把无命中转成补库指引', /深挖模式/.test(thText) && /kb_fetch/.test(thText));
rep.check('默认档不出现深挖指引', !/深挖模式/.test(nhText));

rep.finish();
