# dsh-kb-rag — one-click installer (Windows PowerShell 5.1+)
# 完成一条链：Python 依赖 → 引擎冒烟测试 → Node/pnpm → dsh 插件安装激活 → (可选)模型预下载。
# 用法：powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install.ps1 [-Profile <name>] [-Mirror <url>] [-Models] [-WithDocx] [-DryRun] [-SkipPip] [-SkipNode] [-SkipDsh] [-Yes]
# 环境变量：HF_ENDPOINT（模型镜像，如 https://hf-mirror.com）、KB_EMBED_MODEL、KB_RERANK_MODEL、PIP_INDEX_URL。
[CmdletBinding()]
param(
  [string]$Profile = "",
  [string]$Mirror = "",
  [switch]$Models,
  [switch]$WithDocx,
  [switch]$DryRun,
  [switch]$SkipPip,
  [switch]$SkipNode,
  [switch]$SkipDsh,
  [switch]$Yes
)

$ErrorActionPreference = "Continue"
$PyProbeModules = @("fitz", "numpy", "faiss", "sentence_transformers", "torch")
$PkgOf = @{ fitz = "PyMuPDF"; faiss = "faiss-cpu"; sentence_transformers = "sentence-transformers"; docx = "python-docx" }
if (-not $Mirror -and $env:PIP_INDEX_URL) { $Mirror = $env:PIP_INDEX_URL }
$EmbedModel = if ($env:KB_EMBED_MODEL) { $env:KB_EMBED_MODEL } else { "BAAI/bge-small-zh-v1.5" }
$RerankModel = if ($env:KB_RERANK_MODEL) { $env:KB_RERANK_MODEL } else { "BAAI/bge-reranker-base" }

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoDir = Split-Path -Parent $ScriptDir
$Engine = Join-Path $RepoDir "kb_engine.py"

function Write-Step($msg)  { Write-Host ""; Write-Host ("== " + $msg) -ForegroundColor Cyan }
function Write-Ok($msg)    { Write-Host ("[OK] " + $msg) -ForegroundColor Green }
function Write-Warn2($msg) { Write-Host ("[!!] " + $msg) -ForegroundColor Yellow }
function Write-Fail($msg)  { Write-Host ("[XX] " + $msg) -ForegroundColor Red }

function Get-HfCacheRoot() {
  if ($env:HF_HOME) { return $env:HF_HOME }
  if ($env:HF_HUB_CACHE) { return $env:HF_HUB_CACHE }
  return Join-Path $env:USERPROFILE ".cache\huggingface"
}

function Test-ModelCached($model) {
  # HF cache dir is `hub/models--<org>--<name>` with model ID `/` -> `--`.
  $dir = Join-Path (Get-HfCacheRoot) ("hub\models--" + ($model -replace '/', '--'))
  return (Test-Path $dir)
}

function Test-PythonVersion($exe) {
  try {
    $v = & $exe -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
    if (-not $v) { return $null }
    $parts = "$v".Trim() -split '\.'
    if ($parts.Count -lt 2) { return $null }
    $major = [int]$parts[0]; $minor = [int]$parts[1]
    if ($major -gt 3 -or ($major -eq 3 -and $minor -ge 9)) { return "$major.$minor" }
    return $null
  } catch { return $null }
}

Write-Host "  dsh-kb-rag - one-click installer (local literature knowledge-base RAG)" -ForegroundColor White
if ($DryRun) { Write-Warn2 "dry-run: 只打印将执行的动作，不安装" }
if (-not (Test-Path $Engine)) { Write-Fail "kb_engine.py not found at: $Engine"; exit 1 }

# ---------------------------------------------------------------- 1/5 python

Write-Step "1/5 定位 Python (>= 3.9)"
$Py = $null
foreach ($cand in @("python", "py", "python3")) {
  $cmd = Get-Command $cand -ErrorAction SilentlyContinue
  if (-not $cmd) { continue }
  $exe = if ($cand -eq "py") { "py" } else { $cmd.Source }
  $ver = Test-PythonVersion $exe
  if ($ver) { $Py = $exe; Write-Ok "found: $cand ($ver)"; break }
  else { Write-Warn2 "$cand version < 3.9, skipped" }
}
if (-not $Py) { Write-Fail "未找到 Python >= 3.9。请安装后重跑：https://www.python.org/downloads/"; exit 1 }

# ---------------------------------------------------------------- 2/5 python deps

Write-Step "2/5 检查 Python 依赖"
if ($SkipPip) {
  Write-Warn2 "-SkipPip：跳过依赖安装"
} else {
  $probeModules = $PyProbeModules
  if ($WithDocx) { $probeModules = $PyProbeModules + "docx" }
  $modList = ($probeModules -join " ")
  $missingRaw = & $Py -c @"
import importlib.util, sys
pkgs = {'fitz': 'PyMuPDF', 'faiss': 'faiss-cpu', 'sentence_transformers': 'sentence-transformers', 'docx': 'python-docx'}
print(' '.join(pkgs.get(m, m) for m in sys.argv[1].split() if importlib.util.find_spec(m) is None))
"@ $modList 2>$null
  $missing = @("$missingRaw".Trim() -split '\s+' | Where-Object { $_ })
  if (-not $WithDocx) {
    $docxOk = & $Py -c "import importlib.util; print('ok' if importlib.util.find_spec('docx') else 'missing')" 2>$null
    if ("$docxOk".Trim() -eq "missing") {
      Write-Warn2 "python-docx 未装（可选，DOCX 原生解析；缺失时引擎自动回退 zip+regex）。加 -WithDocx 一并安装"
    }
  }
  if ($missing.Count -eq 0) {
    Write-Ok "核心依赖齐全 (PyMuPDF / numpy / faiss / sentence-transformers / torch)"
  } else {
    Write-Warn2 ("缺失: " + ($missing -join " "))
    $pipArgs = @("-m", "pip", "install", "--disable-pip-version-check") + $missing
    if ($Mirror) { $pipArgs += @("-i", $Mirror) }
    Write-Host ("    $Py " + ($pipArgs -join " ")) -ForegroundColor DarkGray
    if ($DryRun) {
      Write-Warn2 "dry-run：未执行"
    } else {
      & $Py @pipArgs
      if ($LASTEXITCODE -eq 0) {
        Write-Ok "依赖安装完成"
      } else {
        Write-Warn2 "pip 全局安装失败，回退 --user 重试…"
        & $Py (@($pipArgs[0..3]) + @("--user") + $missing)
        if ($LASTEXITCODE -eq 0) { Write-Ok "依赖已装到用户目录 (--user)" }
        else { Write-Fail "pip 安装失败。可换镜像重试：.\scripts\install.ps1 -Mirror https://pypi.tuna.tsinghua.edu.cn/simple"; exit 1 }
      }
    }
  }
}

# ---------------------------------------------------------------- 3/5 engine smoke test

Write-Step "3/5 引擎冒烟测试 (kb_engine.py stats)"
if ($DryRun) {
  Write-Warn2 "dry-run：跳过"
} else {
  $smokeDir = Join-Path ([System.IO.Path]::GetTempPath()) ("kbrag-smoke-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
  $req = '{"kb_root":"' + ($smokeDir -replace '\\', '/') + '"}'
  $out = $req | & $Py $Engine stats 2>&1 | Out-String
  if ("$out" -match '"ok"\s*:\s*true') {
    $ver = if ("$out" -match '"engine"\s*:\s*"([^"]+)"') { $Matches[1] } else { "?" }
    Write-Ok "engine v$ver 自检通过"
  } else {
    Write-Fail $out
    Write-Fail "引擎冒烟测试失败"; exit 1
  }
  if (Test-Path $smokeDir) { Remove-Item -Recurse -Force $smokeDir -ErrorAction SilentlyContinue }
}

# ---------------------------------------------------------------- 4/5 node / pnpm

Write-Step "4/5 检查 Node.js / pnpm"
if ($SkipNode) {
  Write-Warn2 "-SkipNode：跳过"
} elseif (Get-Command node -ErrorAction SilentlyContinue) {
  Write-Ok ("node " + (node --version))
  if (Get-Command pnpm -ErrorAction SilentlyContinue) {
    Write-Ok ("pnpm " + (pnpm --version))
  } elseif (Get-Command npm -ErrorAction SilentlyContinue) {
    Write-Warn2 "pnpm 未找到（DSH 官方插件流程需要 pnpm）"
    $doInstall = $Yes.IsPresent
    if (-not $doInstall) {
      $ans = Read-Host "  现在自动安装 pnpm？[y/N]"
      $doInstall = ($ans -match '^(y|Y|yes|YES)$')
    }
    if ($doInstall) {
      if ($DryRun) { Write-Warn2 "dry-run：将执行 npm install -g pnpm" }
      else {
        npm install -g pnpm
        if ($LASTEXITCODE -eq 0 -and (Get-Command pnpm -ErrorAction SilentlyContinue)) { Write-Ok ("pnpm " + (pnpm --version) + " 已安装") }
        else { Write-Warn2 "pnpm 安装失败，请手动：npm install -g pnpm" }
      }
    } else {
      Write-Warn2 "跳过（可稍后手动：npm install -g pnpm）"
    }
  } else {
    Write-Warn2 "npm 未找到，无法自动装 pnpm；请手动：npm install -g pnpm"
  }
} else {
  Write-Warn2 "Node.js 未找到（dsh 插件安装步骤需要；可 -SkipNode 跳过本步）"
}

# ---------------------------------------------------------------- 5/5 dsh plugin add

Write-Step "5/5 安装并激活 DSH 插件 (dsh plugin add)"
if ($SkipDsh) {
  Write-Warn2 "-SkipDsh：跳过"
} elseif (-not (Get-Command dsh -ErrorAction SilentlyContinue)) {
  Write-Warn2 "dsh CLI 未找到。插件市场路线：安装 dsh-plugin-registry 后在设置面板一键安装；或先安装 DSH 再重跑本脚本"
} else {
  if ($Profile) {
    Write-Host ("    dsh plugin --profile $Profile add dsh-kb-rag") -ForegroundColor DarkGray
    if ($DryRun) { Write-Warn2 "dry-run：未执行" }
    else {
      dsh plugin --profile $Profile add dsh-kb-rag
      if ($LASTEXITCODE -eq 0) { Write-Ok "插件已安装并自动激活（dsh.bundle 声明）" }
      else { Write-Warn2 "dsh 安装失败——请检查上方输出（网络 / pnpm / profile 名）" }
    }
  } else {
    Write-Warn2 "未指定 -Profile，先尝试默认部署目录"
    Write-Host "    dsh plugin add dsh-kb-rag" -ForegroundColor DarkGray
    if ($DryRun) { Write-Warn2 "dry-run：未执行" }
    else {
      dsh plugin add dsh-kb-rag
      if ($LASTEXITCODE -eq 0) { Write-Ok "插件已安装并自动激活（dsh.bundle 声明）" }
      else { Write-Warn2 "默认部署失败。请带 profile 重跑：.\scripts\install.ps1 -Profile <name>" }
    }
  }
}

# ---------------------------------------------------------------- model cache status + optional pre-download

$embedCached = Test-ModelCached $EmbedModel
$rerankCached = Test-ModelCached $RerankModel

if ($embedCached -and $rerankCached) {
  Write-Step "模型已在本机缓存，无需下载"
  Write-Ok ("embed: $EmbedModel 已缓存")
  Write-Ok ("rerank: $RerankModel 已缓存")
} elseif ($Models) {
  Write-Step ("预下载模型 ($EmbedModel / $RerankModel)")
  if (-not $embedCached) { Write-Warn2 "embed 未缓存，将下载（约 95MB）" } else { Write-Ok "embed 已缓存，跳过" }
  if (-not $rerankCached) { Write-Warn2 "rerank 未缓存，将下载（约 1.1GB）" } else { Write-Ok "rerank 已缓存，跳过" }
  Write-Host "    慢网可能数分钟；按 Ctrl+C 可跳过（不影响安装，首次检索时自动重试）。" -ForegroundColor DarkGray
  if ($DryRun) {
    Write-Warn2 "dry-run：跳过"
  } else {
    if ($env:HF_ENDPOINT) { Write-Host ("    HF_ENDPOINT=$($env:HF_ENDPOINT)") -ForegroundColor DarkGray }
    if (-not $env:HF_ENDPOINT) { Write-Warn2 "未设 HF_ENDPOINT；国内网络建议先 set `$env:HF_ENDPOINT='https://hf-mirror.com'" }
    & $Py -c @"
import os, sys
os.environ.setdefault('HF_HUB_DOWNLOAD_TIMEOUT', '60')
try:
    from sentence_transformers import SentenceTransformer, CrossEncoder
    SentenceTransformer(sys.argv[1]); print('[ok] embed model ready')
    CrossEncoder(sys.argv[2]); print('[ok] reranker ready')
except Exception as e:
    print('[fail] %s: %s' % (type(e).__name__, e)); sys.exit(1)
"@ $EmbedModel $RerankModel
    if ($LASTEXITCODE -eq 0) { Write-Ok "模型已就绪" }
    else { Write-Warn2 "模型下载失败——不影响安装；首次检索时会自动重试（受限网络先 `$env:HF_ENDPOINT='https://hf-mirror.com'）" }
  }
} else {
  Write-Step "模型状态"
  Write-Warn2 "模型未全部缓存（首次检索时自动下载约 1.2GB）。想现在下载可加 --models；国内网络先 set `$env:HF_ENDPOINT='https://hf-mirror.com'"
}

# ---------------------------------------------------------------- summary

Write-Host ""
Write-Host "──────────────────────────────────────────────"
if ($DryRun) { Write-Host "安装完成 (dry-run 演练，未做任何变更)" -ForegroundColor Yellow }
else { Write-Host "安装完成" -ForegroundColor White }
Write-Host @"
下一步：
  1. 重启 DSH，打开一个新会话 —— 8 个 kb_* 工具自动注册
  2. 首次检索会弹出查询范围选择（封闭库 / 库+全网 / 仅全网）
  3. 入库：对话里说"把 <文献目录> 入库"（kb_ingest）或"同步 Zotero"（kb_zotero）
  4. 提问："BiFeO3 畴壁导电机制是什么？"（kb_rag，自动带 DOI 引用）

受限网络提示：pip 加速   .\scripts\install.ps1 -Mirror https://pypi.tuna.tsinghua.edu.cn/simple
              模型镜像   `$env:HF_ENDPOINT = 'https://hf-mirror.com'
"@
exit 0
