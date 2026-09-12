// node suite 公共助手（下划线开头 → 不被 run.py 当作用例收集）。
//
// 职责：
//   1) 在沙箱里准备一份"可加载的插件包"：把 npm-package/lib/*.js 复制过去，并让
//      `import "@deepseek-ai/dsh-tools"` 能解析。优先用真实 DSH 安装（能发现 API 漂移），
//      找不到就写一个最小 stub（defineTool = 原样返回），保证测试不依赖本机是否装了 DSH。
//   2) 提供 stub ctx（tools/commands/effect/get/timeout）与 report()，把断言按约定格式输出。
import { cpSync, existsSync, mkdirSync, readFileSync, symlinkSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

export const REPO = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');

const DSH_TOOLS_CANDIDATES = [
  process.env.DSH_TOOLS_PATH,
  join(process.env.APPDATA || '', 'npm/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-tools'),
  join(process.env.APPDATA || '', 'dsh-desktop/harness/profiles/node_modules/@deepseek-ai/dsh-tools'),
].filter(Boolean);

/** 在 sandbox 里准备插件包目录，返回 { dir, toolsMode }。 */
export function preparePkg(sandbox) {
  const pkgDir = join(sandbox, 'pkg');
  const libDir = join(pkgDir, 'lib');
  mkdirSync(libDir, { recursive: true });
  for (const f of ['index.js', 'guidance.js', 'client.js']) {
    const src = join(REPO, 'npm-package', 'lib', f);
    if (existsSync(src)) cpSync(src, join(libDir, f));
  }
  writeFileSync(join(pkgDir, 'package.json'), JSON.stringify({ name: 'pkgtest', private: true, type: 'module' }), 'utf8');

  const scopeDir = join(pkgDir, 'node_modules', '@deepseek-ai');
  mkdirSync(scopeDir, { recursive: true });
  const link = join(scopeDir, 'dsh-tools');
  const real = DSH_TOOLS_CANDIDATES.find((p) => existsSync(p));
  if (real) {
    if (!existsSync(link)) {
      try { symlinkSync(real, link, 'junction'); } catch { /* 失败则落回 stub */ }
    }
    if (existsSync(link)) return { dir: pkgDir, toolsMode: 'real:' + real };
  }
  // stub：defineTool 原样返回（本套件只验证插件自身的注册/渲染/命令逻辑）
  const stub = join(link);
  mkdirSync(stub, { recursive: true });
  writeFileSync(join(stub, 'package.json'),
    JSON.stringify({ name: '@deepseek-ai/dsh-tools', version: '0.0.0-stub', type: 'module', main: 'index.js' }), 'utf8');
  writeFileSync(join(stub, 'index.js'), 'export function defineTool(spec) { return spec; }\n', 'utf8');
  return { dir: pkgDir, toolsMode: 'stub' };
}

/** stub ctx：记录注册的工具/命令，disposer 真正从注册表移除（重新注册要能恢复）。 */
export function makeCtx() {
  const registered = [];
  const commands = [];
  const services = {
    subprocess: { resolveExecutable: async () => 'python', spawn() { throw new Error('stub: no engine'); } },
    sandboxPolicy: {},
  };
  const ctx = {
    get: (n) => services[n],
    timeout: (ms) => new Promise((r) => setTimeout(r, Math.min(ms, 5))),
    tools: { register(t) { registered.push(t); return () => { const i = registered.indexOf(t); if (i >= 0) registered.splice(i, 1); }; } },
    commands: { register(c) { commands.push(c); return () => {}; } },
    effect(fn) { const d = fn(); return typeof d === 'function' ? d : () => {}; },
    on() {},
    logger: console,
  };
  return { ctx, registered, commands };
}

/** 收集断言并按约定输出结果行。 */
export function makeReporter() {
  const checks = [];
  return {
    check(label, ok, detail = '') {
      checks.push({ label: String(label), ok: Boolean(ok), detail: String(detail).slice(0, 300) });
      console.log((ok ? '  PASS  ' : '  FAIL  ') + label + (detail ? ' | ' + String(detail).slice(0, 180) : ''));
    },
    skip(reason) {
      console.log('__SUITE_RESULT__ ' + JSON.stringify({ skip: String(reason) }));
      process.exit(0);
    },
    finish() {
      console.log('__SUITE_RESULT__ ' + JSON.stringify({ checks }));
      process.exit(checks.every((c) => c.ok) ? 0 : 1);
    },
    readText(rel) { return readFileSync(join(REPO, rel), 'utf8'); },
  };
}

/** 客户端 bundle 的加载协议：执行脚本时只在页面全局注册工厂。 */
export function loadClientBundle(path) {
  const src = readFileSync(path, 'utf8');
  let handoff = null;
  const fakeWindow = { __ModuleLoader__: { load: (spec) => { handoff = spec; } } };
  new Function('window', src)(fakeWindow);
  return handoff;
}

/** 极简 React 桩：函数组件会被真正调用（等于一次迷你渲染）。 */
export const ReactStub = {
  createElement(type, props, ...children) {
    const p = Object.assign({}, props || {});
    if (children.length > 0) p.children = children.length === 1 ? children[0] : children;
    if (typeof type === 'function') return type(p);
    return { type, props: p, children };
  },
};
