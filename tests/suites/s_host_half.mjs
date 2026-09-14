// 动态插件半边（plugin/host.js）：以函数体方式加载，验证与静态半边对齐后的
// 工具注册 / 描述注入 / 结果注入 / /kb 三档关闭 / 会话隔离 / 深挖模式 / 可选服务缺失兜底，
// 以及最关键的一条回归：**两半边的 10 个工具定义逐字段一致**（描述、参数、输出 schema、呈现器）。
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { makeCtx, makeReporter, preparePkg, hostHarness, loadHostPlugin } from './_nodehelper.mjs';

const rep = makeReporter();
const harness = await hostHarness();
console.log('  ·  harness：' + harness.mode);
if (harness.mode === 'stub' && harness.tried.length > 0) console.log('  ·  ' + harness.tried.join(' ｜ '));

// ── 动态半边 ─────────────────────────────────────────────────────────────────
const dy = makeCtx();
const plugin = await loadHostPlugin(harness);
plugin.apply(dy.ctx);
const byName = Object.fromEntries(dy.registered.map((t) => [t.name, t]));
const namesNow = () => dy.registered.map((t) => t.name);

rep.check('插件名 kb-rag', plugin.name === 'kb-rag');
rep.check('inject 含 subprocess（issue #1：未声明会静默丢工具）',
  Array.isArray(plugin.inject) && plugin.inject.includes('subprocess'), JSON.stringify(plugin.inject));
rep.check('inject 不含可选服务 commands / userQuestions（未注册会让插件永远 park）',
  !plugin.inject.includes('commands') && !plugin.inject.includes('userQuestions'), JSON.stringify(plugin.inject));
rep.check('注册 10 个工具', dy.registered.length === 10, namesNow().join(' '));
rep.check('参数已按 DSL 编译成 JSON Schema（证明走的是 harness.defineTool）',
  byName.kb_search && byName.kb_search.parameters && byName.kb_search.parameters.type === 'object',
  JSON.stringify(byName.kb_search && byName.kb_search.parameters && byName.kb_search.parameters.type));

// ── 描述层（镜像块注入）─────────────────────────────────────────────────────
for (const n of ['kb_search', 'kb_rag']) {
  const d = byName[n].description || '';
  rep.check(n + ' 描述含调用纪律', d.includes('调用纪律'), d.length + ' 字符');
  rep.check(n + ' 描述含深挖指引（描述注册时定死，两档都要交代）', /thorough/.test(d));
}
rep.check('kb_stats 未被注入纪律', !(byName.kb_stats.description || '').includes('调用纪律'));

// ── 结果层（withNotes 注入）────────────────────────────────────────────────
const renderSearch = byName.kb_search.output.render;
const normal = { ok: true, depth: 'quick', mode_used: 'hybrid', ms: 320, results: [{ title: 'T', score: 1.2 }] };
rep.check('正常命中不追加提示', !/库内无相关资料|可转达给用户/.test(JSON.stringify(renderSearch({}, normal))));
const noHit = { ok: true, depth: 'deep', mode_used: 'hybrid', reranker: 'x', ms: 2400, results: [],
  no_hit: true, verdict: '无关', max_score: 0.03, floor: 0.1, floor_weak: 0.35,
  closest: [{ title: 'Alpha paper', year: 2020, section: 'Results' }] };
const nhText = JSON.stringify(renderSearch({}, Object.assign({ __session: 'X' }, noHit)));
rep.check('无命中渲染理由 + closest 列表', /库内无相关资料/.test(nhText) && /Alpha paper/.test(nhText));
rep.check('tellUser 规则折成「可转达给用户」', /可转达给用户/.test(nhText));
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
  vectors_missing: 12, results: [{ title: 'A', score: 1.1 }] };
rep.check('向量降级提示', /向量链路|纯关键词/.test(JSON.stringify(renderSearch({}, Object.assign({ __session: 'X' }, degraded)))));
const disabled = { ok: true, kb_rag_disabled: true, scope: 'kb', note: 'kb-rag 当前处于关闭状态' };
rep.check('软关闭载荷渲染出「已关闭」提示',
  /关闭/.test(JSON.stringify(renderSearch({}, Object.assign({ __session: 'X' }, disabled)))));
const thText = JSON.stringify(renderSearch({}, Object.assign({ __session: 'X', __diligence: 'thorough' }, noHit)));
rep.check('深挖档把无命中转成补库指引', /深挖模式/.test(thText) && /kb_fetch/.test(thText));
rep.check('默认档不出现深挖指引', !/深挖模式/.test(nhText));

// P2：深挖档下"无命中"的渲染不得再喊"不要换词重试"。规则层在深挖档会跳过 stopRule 换成补库循环
// （resultNotes: thorough && p.stopRule → continue），但渲染层两种档位都会跑，曾无条件写那句话 ——
// 两段文字同时出现时模型收到"去补库"和"别重试"两条互相打架的指令。
rep.check('深挖档无命中渲染补库循环、且不含任何"不要…重试"',
  /深挖模式/.test(thText) && /kb_fetch/.test(thText) && !/不要换词|不要反复改写同一句话重试/.test(thText),
  thText.slice(0, 200));
rep.check('默认档无命中给出可执行的措辞（不要反复改写同一句话）',
  /不要反复改写同一句话重试/.test(nhText), nhText.slice(0, 200));
// 同一类矛盾还有一处**活文案**（不是注释）：kb_scope 的 diligence_note 默认档曾写
// "一次提问最多 3 次检索，每次换实质策略；无命中就如实说明，不要换词穷举"——同句里一正一反。
const dNote = String((await byName.kb_scope.execute({ diligence: 'normal' }, { agent: { id: 'SP' } })).diligence_note || '');
rep.check('kb_scope 默认纪律文案不再出现"换词"矛盾',
  !/不要换词/.test(dNote) && /不要反复重试/.test(dNote), dNote);

// P3：来源块不得回吐绝对路径 —— 上一行已经印了"文件：<基名>"，绝对路径纯属重复，还每篇来源
// 泄露一次本机目录结构。
const withPath = { ok: true, depth: 'deep', mode_used: 'hybrid', ms: 100,
  results: [{ title: 'NoDOI paper', doi: null, file: 'x.pdf', path: 'D:/fake-lib/papers/x.pdf', score: 1.0, snippet: 's' }] };
const wpText = JSON.stringify(renderSearch({}, Object.assign({ __session: 'X' }, withPath)));
rep.check('来源不再重复打印绝对路径', !/fake-lib/.test(wpText) && /x\.pdf/.test(wpText), wpText.slice(0, 200));

// P4：软关闭时 wrapper 只回 {ok, kb_rag_disabled, scope, note}，没有 docs/chunks/vectors。
// 以前直接落到统计行渲染成"0 文档 / 0 块 / 0 向量"——把"已关闭"谎报成"空库"，是最误导的一种输出。
const dsStats = JSON.stringify(byName.kb_stats.output.render({}, Object.assign({ __session: 'X' }, disabled)));
rep.check('kb_stats 软关闭渲染「已关闭」而不是 0 文档',
  /已关闭/.test(dsStats) && !/0 文档/.test(dsStats), dsStats.slice(0, 160));

// ── /kb 命令：三档关闭 + 会话隔离 + 深挖 ────────────────────────────────────
rep.check('commands.register 调用 1 次', dy.commands.length === 1, dy.commands.length);
const kb = dy.commands[0];
const inv = (id, raw) => ({ agent: { id }, rawInput: ' ' + raw });
const rStatus = await kb.handler(inv('S1', 'status'));
rep.check('status 返回状态卡', rStatus.kind === 'success' && /工具注册数：10/.test(rStatus.text));
await kb.handler(inv('S1', 'both'));
rep.check('会话隔离：S1=both 而 S2 仍是 kb',
  /范围 both/.test((await kb.handler(inv('S1', 'status'))).text) && /范围 kb ·/.test((await kb.handler(inv('S2', 'status'))).text));
const rOff = await kb.handler(inv('S1', 'off'));
rep.check('软关闭文案', /软关闭/.test(rOff.text));
// P5：状态卡的"开关"必须把四种档位分开报。以前只判 enabled 布尔 → 四态塌成两标签，
// `off hard` 把工具全卸了仍写"关（软关闭）"。档位由 enabled + 实际注册数无损推出，不新增持久化字段。
const softStatus = (await kb.handler(inv('S1', 'status'))).text;
rep.check('软关闭：状态卡写"软关闭"且不写"硬关闭"',
  /软关闭/.test(softStatus) && !/硬关闭/.test(softStatus), softStatus.split('\n')[2]);
const offResp = await byName.kb_scope.execute({}, { agent: { id: 'S1' } });
rep.check('软关闭生效：调用返回 kb_rag_disabled', offResp && offResp.kb_rag_disabled === true, JSON.stringify(offResp));
rep.check('软关闭不拉引擎（scope 随会话带出）', offResp && offResp.scope === 'both', JSON.stringify(offResp && offResp.scope));
const otherResp = await byName.kb_scope.execute({}, { agent: { id: 'S2' } });
rep.check('另一会话不受影响', otherResp && otherResp.ok === true && otherResp.kb_rag_disabled === undefined);
await kb.handler(inv('S1', 'on'));
rep.check('on 之后恢复（scope 保持 both）',
  (await byName.kb_scope.execute({}, { agent: { id: 'S1' } })).scope === 'both');

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

// 反复重注册不得层层套娃（动态半边是原地替换 execute，容易越包越深）
const chain = (fn) => { let n = 0; while (typeof (fn && fn.__kbInner) === 'function') { n += 1; fn = fn.__kbInner } return n };
await kb.handler(inv('S1', 'off hard'));
await kb.handler(inv('S1', 'on'));
await kb.handler(inv('S1', 'off hard'));
await kb.handler(inv('S1', 'on'));
rep.check('反复 /kb on 不套娃（包装层数恒为 1）', chain(dy.registered.find((t) => t.name === 'kb_scope').execute) === 1,
  String(chain(dy.registered.find((t) => t.name === 'kb_scope').execute)));
rep.check('反复重注册后工具仍可用',
  (await byName.kb_scope.execute({}, { agent: { id: 'S9' } })).ok === true);

const rTh = await kb.handler(inv('S3', 'thorough'));
rep.check('thorough 文案含补库循环', /深挖模式/.test(rTh.text) && /kb_fetch/.test(rTh.text));
rep.check('kb_scope 回报 diligence',
  (await byName.kb_scope.execute({}, { agent: { id: 'S3' } })).diligence === 'thorough');
rep.check('kb_scope diligence 按会话隔离',
  (await byName.kb_scope.execute({}, { agent: { id: 'S2' } })).diligence === 'normal');
const scoped = await byName.kb_scope.execute({ scope: 'web', depth: 'quick', strict: true, save: true }, { agent: { id: 'S4' } });
rep.check('kb_scope 支持 scope/depth/strict/save', scoped.scope === 'web' && scoped.depth === 'quick'
  && scoped.strict === true && scoped.saved === true, JSON.stringify(scoped).slice(0, 160));
rep.check('kb_scope 回报会话键', scoped.session === 'S4', String(scoped.session));
const rPolicy = await kb.handler(inv('S1', 'policy'));
rep.check('policy 列出提示规则', /search-discipline/.test(rPolicy.text) && /提示规则/.test(rPolicy.text));
rep.check('kb_fetch 支持 ingest=true（深挖补库少一次往返）',
  byName.kb_fetch.parameters.properties.ingest !== undefined);

// ── 可选服务缺失的兜底：没有 commands 也要照常给 10 个工具 ────────────────
const dy2 = makeCtx({ commands: false });
const plugin2 = await loadHostPlugin(harness);
let threw = null;
try { plugin2.apply(dy2.ctx); } catch (e) { threw = e; }
rep.check('commands 服务缺失时不抛异常', threw === null, threw ? String(threw.message) : '');
rep.check('commands 缺失仍注册 10 个工具', dy2.registered.length === 10, dy2.registered.length);

// ── 半边对齐：两半的 10 个工具定义逐字段一致 ──────────────────────────────
const { dir } = preparePkg(join(process.cwd(), 'hosthalf'));
const staticMod = await import(pathToFileURL(join(dir, 'lib', 'index.js')).href);
const st = makeCtx();
staticMod.apply(st.ctx);
const stByName = Object.fromEntries(st.registered.map((t) => [t.name, t]));
const norm = (s) => String(s || '').replace(/\s+/g, ' ').trim();
rep.check('两半注册的工具名集合一致',
  JSON.stringify(namesNow().sort()) === JSON.stringify(Object.keys(stByName).sort()),
  namesNow().sort().join(','));
for (const name of Object.keys(stByName).sort()) {
  const a = stByName[name];
  const b = byName[name];
  if (b === undefined) { rep.check(name + ' 在动态半边存在', false); continue; }
  const diff = [];
  if (norm(a.description) !== norm(b.description)) diff.push('描述');
  if (JSON.stringify(a.parameters) !== JSON.stringify(b.parameters)) diff.push('参数');
  if (JSON.stringify(a.output.schema) !== JSON.stringify(b.output.schema)) diff.push('输出 schema');
  if (a.timeoutMs !== b.timeoutMs) diff.push('timeoutMs');
  if ((typeof a.presentCall) !== (typeof b.presentCall)) diff.push('presentCall');
  if ((typeof a.output.presentationMeta) !== (typeof b.output.presentationMeta)) diff.push('presentationMeta');
  rep.check(name + ' 两半定义一致', diff.length === 0, diff.join('/'));
}

// 镜像块本体：host.js 里的 KBG 必须来自 guidance.js（guards.py 另有逐字节 --check）
const hostSrc = rep.readText(join('plugin', 'host.js'));
rep.check('镜像块标记存在', hostSrc.includes('kb-guidance-mirror（由 tools/sync-host-guidance.mjs'));
rep.check('镜像块导出完整 API（与 guidance.js 同 8 项）',
  /return \{ guidedDescription, resultNotes, sessionNotes, makeThrottle, policyInventory, disciplineText, RELEVANCE_FLOOR, MAX_SEARCH_CALLS_PER_QUESTION \};/.test(hostSrc));
rep.check('host.js 不直接读 ctx.commands / ctx.userQuestions 属性（必须走 ctx.get 可选查询）',
  !/ctx\.userQuestions|ctx\.commands/.test(hostSrc));

rep.finish();
