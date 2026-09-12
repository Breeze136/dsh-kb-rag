// npm 包的客户端半边：按页面协议（window.__ModuleLoader__.load）加载 bundle，验证插槽注册与卡片渲染。
import { join } from 'node:path';
import { loadClientBundle, makeReporter, ReactStub, REPO, preparePkg } from './_nodehelper.mjs';

const rep = makeReporter();
preparePkg(process.cwd());

const handoff = loadClientBundle(join(REPO, 'npm-package', 'lib', 'client.js'));
rep.check('bundle 通过 __ModuleLoader__.load 注册工厂', !!handoff && typeof handoff.factory === 'function');
rep.check('id 等于包名 dsh-kb-rag', handoff && handoff.id === 'dsh-kb-rag', handoff && handoff.id);

const mod = handoff.factory((id) => {
  if (id === 'react') return ReactStub;
  throw new Error('unexpected require: ' + id);
});
rep.check('导出 apply 与 inject', typeof mod.apply === 'function' && Array.isArray(mod.inject),
  JSON.stringify(mod.inject));

const registered = [];
let slotsAsked = 0;
const slots = {
  inject(target, cb) { rep.check('slots.inject(' + target + ')', typeof cb === 'function'); cb(); },
  register(spec, comp) { registered.push({ spec, comp }); },
};
mod.apply({ get(n) { if (n === 'slots') { slotsAsked += 1; return slots; } return undefined; } });
rep.check('ctx.get("slots") 被调用', slotsAsked === 1);
const toolviews = registered.filter((r) => r.spec.name === 'tool.call.toolview');
const headers = registered.filter((r) => r.spec.name === 'conversation.session.header.actions');
rep.check('注册 kb_search / kb_rag 两个工具卡片',
  toolviews.length === 2 && toolviews.map((r) => r.spec.key).sort().join(',') === 'kb_rag,kb_search',
  toolviews.map((r) => r.spec.key).join(','));
rep.check('注册会话栏指示条', headers.length === 1 && headers[0].spec.id === 'kb-rag-chip');

const card = toolviews.find((r) => r.spec.key === 'kb_search').comp;
const structured = JSON.stringify(card({ block: { type: 'tool-result', isError: false, meta: {
  verdict: '相关', no_hit: false, max_score: 0.98, sources: [
    { idx: 1, title: 'Alpha paper', doi: '10.5555/12345678', year: 2021, section: 'Results' },
    { idx: 2, title: 'Beta paper', doi: null, year: 2019, section: 'Methods' }] } } }));
rep.check('结构化 meta 路径渲染标题与 DOI 链接',
  /Alpha paper/.test(structured) && /doi\.org\/10\.5555\/12345678/.test(structured));
rep.check('无 DOI 的来源回退成纯文本', /Beta paper/.test(structured) && !/doi\.org\/null/.test(structured));

const fallback = JSON.stringify(card({ block: { type: 'tool-result', isError: false, content: [
  { type: 'text', text: '**知识库来源 Top-2**\n\n1. [Gamma paper](https://doi.org/10.5555/aaa)\n' }] } }));
rep.check('无 meta 时退回解析 markdown 链接（宿主格式是 "1. [标题](链接)"）', /Gamma paper/.test(fallback),
  fallback.slice(0, 120));

const noHit = JSON.stringify(card({ block: { type: 'tool-result', isError: false, meta: {
  verdict: '无关', no_hit: true, max_score: 0.03, sources: [], closest: [{ title: 'Nearest one', year: 2020 }] } } }));
rep.check('无命中卡片给出理由与最接近的一篇', /库内无相关资料/.test(noHit) && /Nearest one/.test(noHit));

rep.check('运行中显示检索中 + query',
  /检索中/.test(JSON.stringify(card({ block: { type: 'tool-call', arguments: JSON.stringify({ query: 'graphene' }) } }))));
rep.check('出错显示失败文案', /失败/.test(JSON.stringify(card({ block: { type: 'tool-result', isError: true } }))));
rep.check('空 props 不崩', card({}) !== null && card({}) !== undefined);

rep.finish();
