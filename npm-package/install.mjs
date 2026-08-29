#!/usr/bin/env node
// dsh-kb-rag — one-click installer entry (npm bin: dsh-kb-rag-install).
// Dispatches to scripts/install.sh (macOS/Linux) or scripts/install.ps1 (Windows).
// Users always pass bash-style flags (--profile / --mirror / --models / --dry-run ...);
// on Windows they are translated to the PowerShell style automatically.
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
  "--yes": "-Yes",
  "-y": "-Yes",
};

if (process.platform === "win32") {
  const psArgs = args.map((a) => WIN_MAP[a] || a);
  const r = spawnSync(
    "powershell",
    ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", join(here, "scripts", "install.ps1"), ...psArgs],
    { stdio: "inherit", shell: false },
  );
  process.exit(r.status == null ? 1 : r.status);
} else {
  const r = spawnSync("bash", [join(here, "scripts", "install.sh"), ...args], { stdio: "inherit", shell: false });
  process.exit(r.status == null ? 1 : r.status);
}
