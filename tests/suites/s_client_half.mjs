// 客户端半边：npm 包（页面 bundle 协议）与动态插件（函数体 + React 闭包符号）跑**同一组断言**。
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { loadClientBundle, makeReporter, ReactStub, REPO, preparePkg } from './_nodehelper.mjs';

const rep = makeReporter();
preparePkg(process.cwd());

// ── 两种加载方式 ─────────────────────────────────────────────────────────────
const handoff = loadClientBundle(join(REPO, 'npm-package', 'lib', 'client.js'));
rep.check('bundle 通过 __ModuleLoader__.load 注册工厂', !!handoff && typeof handoff.factory === 'function');
rep.check('id 等于包名 dsh-kb-rag', handoff && handoff.id === 'dsh-kb-rag', handoff && handoff.id);
const npmHalf = handoff.factory((id) => {
  if (id === 'react') return ReactStub;
  throw new Error('unexpected require: ' + id);
});

// 动态半边：沙箱用 new Function('React', …, clientCode) 求值，这里照做
const dynHalf = new Function('React', readFileSync(join(REPO, 'plugin', 'client.js'), 'utf8'))(ReactStub);
rep.check('动态半边返回插件对象（name/inject/apply）',
  !!dynHalf && dynHalf.name === 'kb-rag-sources' && typeof dynHalf.apply === 'function');

// ── 同一组断言跑两半 ─────────────────────────────────────────────────────────
function battery(label, mod) {
  rep.check(label + '：导出 apply 与 inject', typeof mod.apply === 'function' && Array.isArray(mod.inject),
    JSON.stringify(mod.inject));
  rep.check(label + '：inject 声明 slots', Array.isArray(mod.inject) && mod.inject.includes('slots'));

  const registered = [];
  let slotsAsked = 0;
  const slots = {
    inject(target, cb) { rep.check(label + '：slots.inject(' + target + ')', typeof cb === 'function'); cb(); },
    register(spec, comp) { registered.push({ spec, comp }); },
  };
  mod.apply({ get(n) { if (n === 'slots') { slotsAsked += 1; return slots; } return undefined; } });
  rep.check(label + '：ctx.get("slots") 被调用', slotsAsked === 1);

  const toolviews = registered.filter((r) => r.spec.name === 'tool.call.toolview');
  const headers = registered.filter((r) => r.spec.name === 'conversation.session.header.actions');
  rep.check(label + '：注册 kb_search / kb_rag 两个工具卡片',
    toolviews.length === 2 && toolviews.map((r) => r.spec.key).sort().join(',') === 'kb_rag,kb_search',
    toolviews.map((r) => r.spec.key).join(','));
  rep.check(label + '：注册会话栏指示条 kb-rag-chip',
    headers.length === 1 && headers[0].spec.id === 'kb-rag-chip');

  const card = toolviews.find((r) => r.spec.key === 'kb_search').comp;
  const structured = JSON.stringify(card({ block: { type: 'tool-result', isError: false, meta: {
    verdict: '相关', no_hit: false, max_score: 0.98, sources: [
      { idx: 1, title: 'Alpha paper', doi: '10.5555/12345678', authors: 'Author A; Author B', year: 2021, section: 'Results' },
      { idx: 2, title: 'Beta paper', doi: null, year: 2019, section: 'Methods' }] } } }));
  rep.check(label + '：结构化 meta 渲染标题与 DOI 链接',
    /Alpha paper/.test(structured) && /doi\.org\/10\.5555\/12345678/.test(structured));
  rep.check(label + '：无 DOI 的来源回退成纯文本',
    /Beta paper/.test(structured) && !/doi\.org\/null/.test(structured));
  rep.check(label + '：结构化路径渲染作者/年份/章节', /Author A/.test(structured) && /§Results/.test(structured));

  const fallback = JSON.stringify(card({ block: { type: 'tool-result', isError: false, content: [
    { type: 'text', text: '**知识库来源 Top-2**\n\n1. [Gamma paper](https://doi.org/10.5555/aaa)\n' }] } }));
  rep.check(label + '：无 meta 时退回解析 markdown 链接（宿主格式是 "1. [标题](链接)"）', /Gamma paper/.test(fallback),
    fallback.slice(0, 120));

  const noHit = JSON.stringify(card({ block: { type: 'tool-result', isError: false, meta: {
    verdict: '无关', no_hit: true, max_score: 0.03, sources: [], closest: [{ title: 'Nearest one', year: 2020 }] } } }));
  rep.check(label + '：无命中给出理由与最接近的一篇', /库内无相关资料/.test(noHit) && /Nearest one/.test(noHit));

  const weak = JSON.stringify(card({ block: { type: 'tool-result', isError: false, meta: {
    verdict: '弱相关', no_hit: false, max_score: 0.4, sources: [{ idx: 1, title: 'Weak one', doi: null }] } } }));
  rep.check(label + '：弱相关给出升级 depth 的提示', /相关性偏弱/.test(weak));

  rep.check(label + '：运行中显示检索中 + query',
    /检索中/.test(JSON.stringify(card({ block: { type: 'tool-call', arguments: JSON.stringify({ query: 'graphene' }) } }))));
  rep.check(label + '：出错显示失败文案', /失败/.test(JSON.stringify(card({ block: { type: 'tool-result', isError: true } }))));
  rep.check(label + '：空 props / 未知块类型不崩',
    card({}) !== null && card({}) !== undefined && card({ block: { type: 'reasoning' } }) !== undefined);

  const chip = JSON.stringify(headers[0].comp({}));
  rep.check(label + '：指示条 title 列出 /kb 命令', /\/kb thorough/.test(chip));
}

battery('npm', npmHalf);
battery('动态', dynHalf);

// 两半的实现要一致（同一份渲染逻辑，只有模块形态/引号风格不同）
const npmSrc = readFileSync(join(REPO, 'npm-package', 'lib', 'client.js'), 'utf8');
const dynSrc = readFileSync(join(REPO, 'plugin', 'client.js'), 'utf8');
for (const frag of [
  '([^\\]]{2,240})',
  '库内无相关资料',
  '相关性偏弱：建议升级 depth=deep 再查一次',
  '库内最接近：',
  'conversation.session.header.actions',
  'kb-rag-chip',
  'tool.call.toolview',
]) {
  rep.check('两半都含同一实现片段：' + frag.slice(0, 40), npmSrc.includes(frag) && dynSrc.includes(frag));
}
rep.check('两半都声明 inject slots', /inject\s*=\s*\["slots"\]/.test(npmSrc) && /inject: \['slots'\]/.test(dynSrc));

// 用户可见文案逐条对齐：抽出两半的中文字符串字面量做集合比较（注释里的中文不算）
function cjkLiterals(src) {
  const out = new Set();
  const re = /(['"])((?:[^'"\\\n]|\\.)*?)\1/g;
  let m = null;
  while ((m = re.exec(src)) !== null) {
    const text = m[2];
    if (/[\u4e00-\u9fff]/.test(text)) out.add(text);
  }
  return out;
}
const npmMsgs = cjkLiterals(npmSrc);
const dynMsgs = cjkLiterals(dynSrc);
const missing = [...npmMsgs].filter((t) => !dynMsgs.has(t));
const extra = [...dynMsgs].filter((t) => !npmMsgs.has(t));
rep.check('npm 的每条中文文案都在动态半边里（' + npmMsgs.size + ' 条）', missing.length === 0,
  missing.length > 0 ? '缺：' + missing.slice(0, 3).join(' / ') : '');
rep.check('动态半边只多出「服务不可用」类兜底文案', extra.length === 0
  || extra.every((t) => /不可用|未注册/.test(t)), extra.join(' / '));

rep.finish();
