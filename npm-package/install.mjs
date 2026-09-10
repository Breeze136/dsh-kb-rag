#!/usr/bin/env node
// dsh-kb-rag — one-click installer entry (npm bin: dsh-kb-rag-install).
// Dispatches to scripts/install.sh (macOS/Linux) or scripts/install.ps1 (Windows).
// Users always pass bash-style flags (--profile / --mirror / --dry-run ...);
// on Windows they are translated to the PowerShell style automatically.
//
// 真·一键行为：
//  - 默认注入 --models（预下载模型；直连失败自动切 hf-mirror.com 镜像重试），
//    传 --no-models 可跳过（只装依赖与插件，首次检索时再下载）；
//  - 默认注入 --yes（不卡交互：缺 pnpm 直接自动装）。
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);

const WIN_MAP = {
  "--profile": "-Profile",
  "--mirror": "-Mirror",
  "--models": "-Models",
  "--with-docx": "-WithDocx",
  "--dry-run": "-DryRun",
  "--skip-pip": "-SkipPip",
  "--skip-node": "-SkipNode",
  "--skip-dsh": "-SkipDsh",
  "--no-models": "-NoModels",
  "--yes": "-Yes",
  "-y": "-Yes",
};

// 用户显式传参优先；否则按"真一键"缺省补齐
const wantsNoModels = args.includes("--no-models") || args.includes("-NoModels");
const wantsYes = args.includes("--yes") || args.includes("-y") || args.includes("-Yes");
const finalArgs = [...args];
if (!wantsNoModels && !finalArgs.includes("--models")) finalArgs.push("--models");
if (!wantsYes) finalArgs.push("--yes");

if (process.platform === "win32") {
  const psArgs = finalArgs.map((a) => WIN_MAP[a] || a);
  const r = spawnSync(
    "powershell",
    ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", join(here, "scripts", "install.ps1"), ...psArgs],
    { stdio: "inherit", shell: false },
  );
  process.exit(r.status == null ? 1 : r.status);
} else {
  const r = spawnSync("bash", [join(here, "scripts", "install.sh"), ...finalArgs], { stdio: "inherit", shell: false });
  process.exit(r.status == null ? 1 : r.status);
}
