// 把 npm-package/lib/guidance.js 里的提示文本**生成**到 plugin/host.js 的镜像块里。
//
// 为什么要有这个脚本：动态插件半边（plugin/host.js）不能 import npm 包的模块，
// 只能内联文本；手抄必然漂移（这正是问题总账 #15 的病根）。这里让 guidance.js 当唯一
// 事实来源，host.js 里的块由本脚本生成，`--check` 可在验证时发现漂移。
//
// 用法：
//   node tools/sync-host-guidance.mjs          # 写入/更新镜像块
//   node tools/sync-host-guidance.mjs --check  # 只检查是否与源一致（不一致退出码 1）
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, '..');
const GUIDANCE = resolve(root, 'npm-package/lib/guidance.js');
const HOST = resolve(root, 'plugin/host.js');

const START = '    // >>> kb-guidance-mirror（由 tools/sync-host-guidance.mjs 生成，勿手改）';
const END = '    // <<< kb-guidance-mirror';

const g = await import('file://' + GUIDANCE.replace(/\\/g, '/'));
const texts = {
  discipline: g.SEARCH_DISCIPLINE,
  thorough: g.THOROUGH_LOOP,
  defaults: g.DEFAULTS_NOTE,
  floor: g.RELEVANCE_FLOOR.rerank,
  maxCalls: g.MAX_SEARCH_CALLS_PER_QUESTION,
};

const block = [
  START,
  '    // 源：npm-package/lib/guidance.js（唯一事实来源）。改提示请改那边，再跑 sync 脚本。',
  '    const KB_GUIDANCE = ' + JSON.stringify(texts, null, 2).split('\n').join('\n    ') + '',
  '    // 检索纪律（描述层）：默认档给调用上限，深挖档给补库循环。',
  '    function kbDisciplineText(diligence) {',
  "      const pointer = '注：用户明确要求彻底查找时（\\'仔细找/慢慢来/别省时间/把相关文献都找齐\\'），'",
  "        + '先 kb_scope({ diligence: \"thorough\" })（或用 /kb thorough）——该模式下调用上限解除，'",
  "        + '改为「反复检索 → kb_fetch(ingest=true) 补库 → 引文关联 → 增量入库 → 再查」的循环。'",
  "      return diligence === 'thorough'",
  '        ? (KB_GUIDANCE.thorough + \'\\n\' + KB_GUIDANCE.defaults)',
  '        : (KB_GUIDANCE.discipline + \'\\n\' + KB_GUIDANCE.defaults + \'\\n\' + pointer)',
  '    }',
  '    // 结果层：无命中 / 弱相关 / 向量降级三种情形（深挖档把"叫停"换成"继续补库"）。',
  '    function kbResultNotes(value, diligence) {',
  '      const out = []',
  "      const list = (value && (value.results || value.evidence)) || []",
  '      const maxScore = list.length > 0',
  "        ? Math.max.apply(null, list.map(function (r) { return Number(r && r.score) || 0 })) : null",
  '      const thorough = diligence === \'thorough\'',
  '      const noHit = Boolean(value && (value.no_hit === true || value.verdict === \'无关\'',
  '        || (list.length === 0 && value.ok === true)))',
  '      if (noHit) {',
  '        out.push(thorough',
  "          ? '深挖模式：本轮没命中，**不要收尾**。① 换术语或放宽 filters 再检索；② 从已命中结果的 citations 取 DOI，用 kb_fetch({ identifiers: [doi], ingest: true }) 补库后重查；③ 用 related 列表横向扩展。'",
  "          : ('⚠ 库内无相关资料' + (maxScore !== null ? '（最高分 ' + maxScore.toFixed(2) + '，低于阈值 ' + KB_GUIDANCE.floor + '）' : '') + '：不要再换词重试；如实说明库内没有，并按 scope 转 web_search。'))",
  '      } else if (value && value.verdict === \'弱相关\' && !thorough) {',
  "        out.push('提示：本次结果相关性偏弱（最高分 ' + (maxScore === null ? '?' : maxScore.toFixed(2)) + '）。最多再升一次 depth=deep；仍弱就按「库内无资料」处理。')",
  '      }',
  '      if (value && (value.mode_used === \'keyword\' || value.embedding_error || value.vectors_missing > 0)) {',
  "        out.push('注意：向量链路当前不可用' + (value.embedding_error ? '（' + String(value.embedding_error).slice(0, 120) + '）' : '')",
  "          + '，本次检索已退化为纯关键词。请在回答中说明，并提示用户重跑 kb_ingest 可补齐缺失向量。')",
  '      }',
  '      if (value && value.kb_rag_disabled === true) {',
  "        out.push('kb-rag 当前处于关闭状态，本次调用未检索。请直接使用 web_search，或提示用户用 /kb on 开启。')",
  '      }',
  '      return out',
  '    }',
  END,
].join('\n');

const src = readFileSync(HOST, 'utf8');
const startIdx = src.indexOf(START);
const endIdx = src.indexOf(END);
let next;
if (startIdx >= 0 && endIdx > startIdx) {
  next = src.slice(0, startIdx) + block + src.slice(endIdx + END.length);
} else {
  // 首次注入：放在 `return {` 这一行**之前**（块里是 const/function 声明，
  // 放进对象字面量里会直接语法错误）
  const anchor = src.lastIndexOf('\nreturn {');
  if (anchor < 0) { console.error('!! host.js 里找不到 `return {`，无法注入'); process.exit(2); }
  const insertAt = anchor + 1;      // 指向 `return {` 行首
  next = src.slice(0, insertAt) + block + '\n' + src.slice(insertAt);
}

const args = process.argv.slice(2);
if (args.includes('--check')) {
  if (next === src) { console.log('OK  镜像块与 guidance.js 一致'); process.exit(0); }
  console.error('!! 镜像块已漂移：跑 node tools/sync-host-guidance.mjs 重新生成');
  process.exit(1);
}
writeFileSync(HOST, next, 'utf8');
console.log((next === src ? '无需改动' : '已更新') + '：plugin/host.js 的 kb-guidance-mirror 块');
