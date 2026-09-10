#!/usr/bin/env node
// dsh-kb-rag-install — 裸 npx 入口（解决 `npx dsh-kb-rag-install` 的 E404 坑）。
//
// 本包只做一件事：定位依赖 dsh-kb-rag 里的 install.mjs 并以相同参数转发。
// 安装逻辑（依赖检查 → 引擎冒烟 → pnpm → dsh plugin add → 模型预下载 + 自动镜像）
// 全部在 dsh-kb-rag 包内维护，这里零逻辑、零重复。
import { spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));

function resolveInstaller() {
  // 常规解析（依赖装在 node_modules 里）
  try {
    const require = createRequire(import.meta.url);
    const pkgRoot = dirname(require.resolve("dsh-kb-rag/package.json"));
    const candidate = join(pkgRoot, "install.mjs");
    if (existsSync(candidate)) return candidate;
  } catch { /* fall through */ }
  // 兜底：相对本包定位 node_modules（require.resolve 被 exports/条件导出挡住时才走到这里）
  for (const cand of [
    join(here, "..", "dsh-kb-rag", "install.mjs"),                  // 平铺：node_modules/<本包>/../dsh-kb-rag
    join(here, "node_modules", "dsh-kb-rag", "install.mjs"),        // 嵌套：<本包>/node_modules/dsh-kb-rag
  ]) {
    if (existsSync(cand)) return cand;
  }
  return null;
}

const installer = resolveInstaller();
if (!installer) {
  console.error("[dsh-kb-rag-install] 内部错误：找不到 dsh-kb-rag 依赖（安装器本体）。");
  console.error("请重试：npx --yes dsh-kb-rag-install，或用完整命令：");
  console.error('  npx --yes --package dsh-kb-rag -c "dsh-kb-rag-install"');
  process.exit(1);
}

const r = spawnSync(process.execPath, [installer, ...process.argv.slice(2)], { stdio: "inherit" });
process.exit(r.status == null ? 1 : r.status);
