// 把 npm-package/lib/guidance.js **整份**生成到 plugin/host.js 的镜像块里。
//
// 为什么这么做：动态插件半边（plugin/host.js）不能 import npm 包的模块，只能内联；手抄必然漂移
// （这正是问题总账 #15 的病根）。所以不再手写"精简镜像"，而是把 guidance.js 的源码原样嵌成一个
// IIFE 并导出同名 API —— 两半共用**同一份实现**，规则数量、文案、节流逻辑都只有一处。
// `--check` 在验证阶段发现漂移（tests/run.py 的前置检查会跑它）。
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

const START = '    // >>> kb-guidance-mirror（由 tools/sync-host-guidance.mjs 从 npm-package/lib/guidance.js 生成，勿手改）';
const END = '    // <<< kb-guidance-mirror';

// 源码原样搬进来，只去掉 ESM 的 export 前缀（其余逐字一致，便于人眼比对）
const source = readFileSync(GUIDANCE, 'utf8')
  .replace(/^export\s+/gm, '')
  .replace(/\r\n/g, '\n')
  .trimEnd();

const api = ['guidedDescription', 'resultNotes', 'sessionNotes', 'makeThrottle', 'policyInventory',
  'disciplineText', 'RELEVANCE_FLOOR', 'MAX_SEARCH_CALLS_PER_QUESTION'];

const block = [
  START,
  '    // 两半共用同一实现：改提示只改 npm-package/lib/guidance.js，再跑 sync 脚本。',
  '    const KBG = (function () {',
  source.split('\n').map((l) => (l ? '    ' + l : l)).join('\n'),
  '    return { ' + api.join(', ') + ' };',
  '    })();',
  END,
].join('\n');

const src = readFileSync(HOST, 'utf8');
const startIdx = src.indexOf(START);
const endIdx = src.indexOf(END);
let next;
if (startIdx >= 0 && endIdx > startIdx) {
  next = src.slice(0, startIdx) + block + src.slice(endIdx + END.length);
} else {
  // 首次注入：放在 `return {` 那一行之前（块里是 const 声明，放进对象字面量会语法错误）
  const anchor = src.lastIndexOf('\nreturn {');
  if (anchor < 0) { console.error('!! host.js 里找不到 `return {`，无法注入'); process.exit(2); }
  const insertAt = anchor + 1;
  next = src.slice(0, insertAt) + block + '\n' + src.slice(insertAt);
}

if (process.argv.includes('--check')) {
  if (next === src) { console.log('OK  镜像块与 guidance.js 一致'); process.exit(0); }
  console.error('!! 镜像块已漂移：跑 node tools/sync-host-guidance.mjs 重新生成');
  process.exit(1);
}
writeFileSync(HOST, next, 'utf8');
console.log((next === src ? '无需改动' : '已更新') + '：plugin/host.js 的 kb-guidance-mirror 块（整份 guidance.js）');
