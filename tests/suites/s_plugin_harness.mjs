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
rep.check('硬关闭后工具数 0', namesNow().length === 0, namesNow().length);
await kb.handler(inv('S1', 'on'));
rep.check('on 后 10 个工具回来且无重名',
  namesNow().length === 10 && new Set(namesNow()).size === 10, namesNow().length);
const rHalf = await kb.handler(inv('S1', 'off search'));
rep.check('半关闭文案', /半关闭/.test(rHalf.text));
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
