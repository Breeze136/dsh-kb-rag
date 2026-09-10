#!/usr/bin/env bash
# dsh-kb-rag — one-click installer (macOS / Linux / Git Bash)
# 完成一条链：Python 依赖 → 引擎冒烟测试 → Node/pnpm → dsh 插件安装激活 → (默认)模型预下载。
# 用法：./scripts/install.sh [--profile <name>] [--mirror <pip镜像>] [--models] [--no-models] [--with-docx]
#                            [--dry-run] [--skip-pip] [--skip-node] [--skip-dsh] [-y]
# 环境变量：HF_ENDPOINT（模型镜像，如 https://hf-mirror.com）、KB_EMBED_MODEL、KB_RERANK_MODEL、PIP_INDEX_URL。
set -u

# ---------------------------------------------------------------- helpers

BOLD=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; CYAN=$'\033[36m'; RESET=$'\033[0m'
if [ ! -t 1 ]; then BOLD=""; DIM=""; GREEN=""; YELLOW=""; RED=""; CYAN=""; RESET=""; fi

ok()     { printf '%s[OK]%s %s\n'  "$GREEN"  "$RESET" "$*"; }
warn()   { printf '%s[!!]%s %s\n'  "$YELLOW" "$RESET" "$*"; }
fail()   { printf '%s[XX]%s %s\n'  "$RED"    "$RESET" "$*"; }
step()   { printf '\n%s== %s%s\n' "$BOLD$CYAN" "$*" "$RESET"; }
die()    { fail "$*"; exit 1; }

hf_cache_root() {
  if [ -n "${HF_HOME:-}" ]; then printf '%s' "$HF_HOME"
  elif [ -n "${HF_HUB_CACHE:-}" ]; then printf '%s' "$HF_HUB_CACHE"
  else printf '%s' "${XDG_CACHE_HOME:-$HOME/.cache}/huggingface"; fi
}
model_cached() {
  # HF cache dir is `hub/models--<org>--<name>` with model ID `/` -> `--`.
  local dir
  # HF 缓存目录把 model ID 的 '/' 换成 '--'（双连字符），例如
  # models--BAAI--bge-small-zh-v1.5 —— 用 tr '/' '-' 只会得到单连字符而恒不命中。
  dir="$(hf_cache_root)/hub/models--$(printf '%s' "$1" | sed 's|/|--|g')"
  [ -e "$dir" ]
}

DRY_RUN=0; SKIP_PIP=0; SKIP_NODE=0; SKIP_DSH=0; WITH_DOCX=0; WANT_MODELS=0; NO_MODELS=0; ASSUME_YES=0
PROFILE=""; MIRROR="${PIP_INDEX_URL:-}"
EMBED_MODEL="${KB_EMBED_MODEL:-BAAI/bge-small-zh-v1.5}"
RERANK_MODEL="${KB_RERANK_MODEL:-BAAI/bge-reranker-base}"

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
ENGINE="$REPO_DIR/kb_engine.py"

PY_PROBE_MODULES="fitz numpy faiss sentence_transformers torch"
DOCX_MODULE="docx"

usage() {
  sed -n '3,5p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

while [ $# -gt 0 ]; do
  case "$1" in
    --profile)   PROFILE="${2:-}"; shift 2 ;;
    --mirror)    MIRROR="${2:-}"; shift 2 ;;
    --models)    WANT_MODELS=1; shift ;;
    --no-models) NO_MODELS=1; shift ;;
    --with-docx) WITH_DOCX=1; shift ;;
    --dry-run)   DRY_RUN=1; shift ;;
    --skip-pip)  SKIP_PIP=1; shift ;;
    --skip-node) SKIP_NODE=1; shift ;;
    --skip-dsh)  SKIP_DSH=1; shift ;;
    -y|--yes)    ASSUME_YES=1; shift ;;
    -h|--help)   usage ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done

[ -f "$ENGINE" ] || die "kb_engine.py not found at: $ENGINE"

printf '%s' "$BOLD"
cat <<'BANNER'
  ┌─────────────────────────────────────────────┐
  │  dsh-kb-rag · one-click installer           │
  │  local literature knowledge-base RAG (DSH)  │
  └─────────────────────────────────────────────┘
BANNER
printf '%s' "$RESET"
[ "$DRY_RUN" -eq 1 ] && warn "dry-run: 只打印将执行的动作，不安装"

# ---------------------------------------------------------------- 内存检查
# torch + bge-reranker-base（约 1.1GB）实际运行建议 8GB 以上内存，装前先告知。
ram_gb() {
  case "$(uname -s)" in
    Darwin) sysctl -n hw.memsize 2>/dev/null | awk '{printf "%.1f", $1/1073741824}' ;;
    *) awk '/^MemTotal:/{printf "%.1f", $2/1048576; exit}' /proc/meminfo 2>/dev/null ;;
  esac
}
RAM_GB=$(ram_gb)
if [ -n "${RAM_GB:-}" ]; then
  ram_verdict=$(awk -v g="$RAM_GB" 'BEGIN{print (g>=8)?"ok":(g>=4)?"warn":"low"}')
  case "$ram_verdict" in
    ok)   ok "本机内存: $RAM_GB GB" ;;
    warn) warn "本机内存: $RAM_GB GB，低于建议的 8GB —— 安装可继续，但运行重排模型（约 1.1GB）时内存偏紧" ;;
    low)  warn "本机内存: $RAM_GB GB，远低于建议的 8GB —— 安装可继续，但运行 torch + 重排模型时很可能内存不足/极慢" ;;
  esac
else
  warn "无法读取本机内存大小（不影响安装）"
fi

# ---------------------------------------------------------------- python

step "1/5 定位 Python (>= 3.9)"
PYTHON=""
for cand in python3 python py; do
  if command -v "$cand" >/dev/null 2>&1; then
    ver=$("$cand" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "0.0")
    if [ "$ver" = "0.0" ]; then
      [ "$cand" != "python" ] && warn "$cand 存在但无法运行（可能是 Windows Store 存根），跳过"
      continue
    fi
    major=${ver%%.*}; minor=${ver#*.}
    if [ "$major" -ge 4 ] || { [ "$major" -eq 3 ] && [ "${minor:-0}" -ge 9 ]; } 2>/dev/null; then
      PYTHON="$cand"; ok "found: $cand ($ver)"; break
    else
      warn "$cand is $ver (< 3.9), skipped"
    fi
  fi
done
[ -n "$PYTHON" ] || die "未找到 Python >= 3.9。请安装后重跑：https://www.python.org/downloads/  （或 brew install python / apt install python3）"

# ---------------------------------------------------------------- python deps

step "2/5 检查 Python 依赖"
if [ "$SKIP_PIP" -eq 1 ]; then
  warn "--skip-pip：跳过依赖安装"
else
  probe_modules="$PY_PROBE_MODULES"
  [ "$WITH_DOCX" -eq 1 ] && probe_modules="$probe_modules $DOCX_MODULE"
  missing=$("$PYTHON" - "$probe_modules" <<'PYEOF'
import importlib.util, sys
mods = sys.argv[1].split()
pkgs = {"fitz": "PyMuPDF", "faiss": "faiss-cpu", "sentence_transformers": "sentence-transformers", "docx": "python-docx"}
print(" ".join(pkgs.get(m, m) for m in mods if importlib.util.find_spec(m) is None))
PYEOF
  )
  docx_state=$("$PYTHON" -c "import importlib.util; print('ok' if importlib.util.find_spec('docx') else 'missing')")
  if [ "$docx_state" = "missing" ] && [ "$WITH_DOCX" -eq 0 ]; then
    warn "python-docx 未装（可选，DOCX 原生解析；缺失时引擎自动回退 zip+regex）。加 --with-docx 一并安装"
  fi
  if [ -z "${missing// /}" ]; then
    ok "核心依赖齐全 (PyMuPDF / numpy / faiss / sentence-transformers / torch)"
  else
    warn "缺失: $missing"
    pip_extra="--disable-pip-version-check"
    [ -n "$MIRROR" ] && pip_extra="$pip_extra -i $MIRROR"
    echo "$DIM  $PYTHON -m pip install $pip_extra $missing$RESET"
    # shellcheck disable=SC2086 # 包名与参数按词拆分是有意的
    if [ "$DRY_RUN" -eq 1 ]; then
      warn "dry-run：未执行"
    elif "$PYTHON" -m pip install $pip_extra $missing; then
      ok "依赖安装完成"
    else
      warn "pip 全局安装失败，回退 --user 重试…"
      if "$PYTHON" -m pip install $pip_extra --user $missing; then
        ok "依赖已装到用户目录 (--user)"
      else
        die "pip 安装失败。可换镜像重试：./scripts/install.sh --mirror https://pypi.tuna.tsinghua.edu.cn/simple"
      fi
    fi
  fi
fi

# ---------------------------------------------------------------- engine smoke test

step "3/5 引擎冒烟测试 (kb_engine.py stats)"
if [ "$DRY_RUN" -eq 0 ]; then
  SMOKE_TMP=$(mktemp -d)
  out=$(printf '{"kb_root":"%s"}' "$SMOKE_TMP/kb" | "$PYTHON" "$ENGINE" stats 2>&1) || { fail "$out"; die "引擎冒烟测试失败"; }
  case "$out" in
    *'"ok": true'*|*'"ok":true'*)
      eng_ver=$(printf '%s' "$out" | "$PYTHON" -c "import sys, json; print(json.load(sys.stdin).get('engine', '?'))" 2>/dev/null)
      ok "engine v${eng_ver} 自检通过" ;;
    *) fail "$out"; die "引擎冒烟测试失败" ;;
  esac
  rm -rf "$SMOKE_TMP"
else
  warn "dry-run：跳过"
fi

# ---------------------------------------------------------------- node / pnpm

step "4/5 检查 Node.js / pnpm"
if [ "$SKIP_NODE" -eq 1 ]; then
  warn "--skip-node：跳过"
elif command -v node >/dev/null 2>&1; then
  ok "node $(node --version)"
  if command -v pnpm >/dev/null 2>&1; then
    ok "pnpm $(pnpm --version)"
  elif command -v npm >/dev/null 2>&1; then
    warn "pnpm 未找到（DSH 官方插件流程需要 pnpm）"
    if [ "$DRY_RUN" -eq 1 ]; then
      warn "dry-run：将执行 npm install -g pnpm"
    else
      if [ "$ASSUME_YES" -eq 1 ]; then
        npm install -g pnpm && ok "pnpm $(pnpm --version) 已安装" || {
          warn "npm 全局安装失败，尝试 corepack 启用…"
          corepack enable pnpm 2>/dev/null
          command -v pnpm >/dev/null 2>&1 && ok "pnpm 已经 corepack 启用" || warn "pnpm 安装失败，请手动：npm install -g pnpm（或 corepack enable pnpm）"
        }
      else
        printf '  现在自动安装 pnpm？[y/N] '
        read -r ans
        case "$ans" in y|Y|yes|YES) npm install -g pnpm && ok "pnpm $(pnpm --version) 已安装" || warn "pnpm 安装失败，请手动：npm install -g pnpm" ;; *) warn "跳过（可稍后手动：npm install -g pnpm）" ;; esac
      fi
    fi
  else
    warn "npm 未找到，无法自动装 pnpm；请手动：npm install -g pnpm"
  fi
else
  warn "Node.js 未找到（dsh 插件安装步骤需要；可 --skip-node 跳过本步）"
fi

# ---------------------------------------------------------------- dsh plugin add

step "5/5 安装并激活 DSH 插件 (dsh plugin add)"
# profile 自动检测：未指定时扫 $DSH_HOME/profiles（默认 ~/.dsh/profiles）——
# 唯一即用；多个时交互选择（非交互必须显式指定）；一个都没有则用 web（DSH 首次使用会自动创建）。
# 注意：`dsh plugin` 的 --profile 是必填项，不带 --profile 调用必然失败，
# 因此这里必须解析出一个非空 profile，或明确报错退出。
if [ -z "$PROFILE" ] && command -v dsh >/dev/null 2>&1; then
  PROFILES_DIR="${DSH_HOME:-$HOME/.dsh}/profiles"
  CANDIDATES=""
  if [ -d "$PROFILES_DIR" ]; then
    # 真实 profile 含 cordis.yml / package.json（DSH 部署目录）；node_modules 等杂物排除
    CANDIDATES=$(cd "$PROFILES_DIR" 2>/dev/null && ls -1 2>/dev/null | while read -r d; do
      [ -d "$d" ] && { [ -f "$d/cordis.yml" ] || [ -f "$d/package.json" ]; } && echo "$d"
    done)
  fi
  CAND_N=$(printf '%s' "$CANDIDATES" | grep -c . || true)
  if [ "$CAND_N" -eq 1 ]; then
    PROFILE=$(printf '%s' "$CANDIDATES" | head -1)
    ok "profile 自动检测: $PROFILE（$PROFILES_DIR 下唯一）"
  elif [ "$CAND_N" -gt 1 ]; then
    if [ "$ASSUME_YES" -eq 0 ] && [ "$DRY_RUN" -eq 0 ]; then
      warn "检测到多个 profile: $(printf '%s' "$CANDIDATES" | tr '\n' ' ')"
      printf '  用哪个？输入名称后回车: '
      read -r ANS
      if [ -n "$ANS" ] && printf '%s\n' "$CANDIDATES" | grep -Fqx -- "$ANS"; then
        PROFILE="$ANS"
      else
        die "未选择有效 profile，已中止（dsh plugin 的 --profile 为必填项）"
      fi
    else
      die "检测到多个 profile（$(printf '%s' "$CANDIDATES" | tr '\n' ' ')）但当前是非交互模式：请带 --profile <名称> 重跑"
    fi
  else
    PROFILE="web"
    warn "未检测到已有 profile，使用默认 web（DSH 首次使用会自动创建该 profile）"
  fi
fi
if [ "$SKIP_DSH" -eq 1 ]; then
  warn "--skip-dsh：跳过"
elif ! command -v dsh >/dev/null 2>&1; then
  warn "dsh CLI 未找到。插件市场路线：安装 dsh-plugin-registry 后在设置面板一键安装；或先安装 DSH 再重跑本脚本"
else
  echo "$DIM  dsh plugin --profile $PROFILE add dsh-kb-rag$RESET"
  if [ "$DRY_RUN" -eq 1 ]; then
    warn "dry-run：未执行"
  elif dsh plugin --profile "$PROFILE" add dsh-kb-rag; then
    ok "插件已安装并自动激活（dsh.bundle 声明）"
  else
    die "dsh 插件安装失败——请检查上方输出（网络 / pnpm / profile 名）"
  fi
fi

# ---------------------------------------------------------------- model cache status + optional pre-download

EMBED_CACHED=0; RERANK_CACHED=0
model_cached "$EMBED_MODEL" && EMBED_CACHED=1
model_cached "$RERANK_MODEL" && RERANK_CACHED=1

if [ "$EMBED_CACHED" -eq 1 ] && [ "$RERANK_CACHED" -eq 1 ]; then
  step "模型已在本机缓存，无需下载"
  ok "embed: $EMBED_MODEL 已缓存"
  ok "rerank: $RERANK_MODEL 已缓存"
elif [ "$WANT_MODELS" -eq 1 ] && [ "$NO_MODELS" -eq 0 ]; then
  step "预下载模型 ($EMBED_MODEL / $RERANK_MODEL)"
  [ "$EMBED_CACHED" -eq 1 ] && ok "embed 已缓存，跳过" || warn "embed 未缓存，将下载（约 95MB）"
  [ "$RERANK_CACHED" -eq 1 ] && ok "rerank 已缓存，跳过" || warn "rerank 未缓存，将下载（约 1.1GB）"
  echo "$DIM  慢网可能数分钟；Ctrl+C 可跳过（不影响安装，首次检索时自动重试）$RESET"
  if [ "$DRY_RUN" -eq 1 ]; then
    warn "dry-run：跳过"
  else
    if [ -n "${HF_ENDPOINT:-}" ]; then
      echo "$DIM  HF_ENDPOINT=$HF_ENDPOINT$RESET"
    else
      echo "$DIM  未设 HF_ENDPOINT；直连下载失败时会自动切换镜像 hf-mirror.com 重试$RESET"
    fi
    download_models() {
      "$PYTHON" - "$EMBED_MODEL" "$RERANK_MODEL" <<'PYEOF'
import os, sys
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "60")
try:
    from sentence_transformers import SentenceTransformer, CrossEncoder
    SentenceTransformer(sys.argv[1])
    print("[ok] embed model ready")
    CrossEncoder(sys.argv[2])
    print("[ok] reranker ready")
except Exception as e:
    print(f"[fail] {type(e).__name__}: {e}")
    sys.exit(1)
PYEOF
    }
    if download_models; then
      ok "模型已就绪"
    elif [ -z "${HF_ENDPOINT:-}" ]; then
      warn "模型下载失败，自动切换镜像 HF_ENDPOINT=https://hf-mirror.com 重试…"
      export HF_ENDPOINT=https://hf-mirror.com
      if download_models; then ok "模型已就绪（hf-mirror.com）"
      else warn "模型下载失败——不影响安装；首次检索时会自动重试"; fi
    else
      warn "模型下载失败——不影响安装；首次检索时会自动重试"
    fi
  fi
else
  step "模型状态"
  if [ "$NO_MODELS" -eq 1 ]; then
    warn "--no-models：跳过模型下载（首次检索时自动下载约 1.2GB，直连失败会自动切 hf-mirror.com 镜像）"
  else
    warn "模型未全部缓存（首次检索时自动下载约 1.2GB，直连失败会自动切 hf-mirror.com 镜像）。想现在下载可加 --models"
  fi
fi

# ---------------------------------------------------------------- summary

printf '\n%s' "$BOLD"
echo "──────────────────────────────────────────────"
printf '安装完成%s\n' "$RESET"
[ "$DRY_RUN" -eq 0 ] || printf '%s(dry-run 演练，未做任何变更)%s\n' "$YELLOW" "$RESET"
cat <<NEXT
下一步：
  1. 重启 DSH，打开一个新会话 —— 9 个 kb_* 工具自动注册
  2. 首次检索会弹出查询范围选择（封闭库 / 库+全网 / 仅全网）
  3. 入库：对话里说“把 <文献目录> 入库”（kb_ingest）或“同步 Zotero”（kb_zotero）
  4. 提问：“石墨烯是怎么用化学气相沉积合成的？”（kb_rag，自动带 DOI 引用）

受限网络提示：pip 加速   ./scripts/install.sh --mirror https://pypi.tuna.tsinghua.edu.cn/simple
              模型镜像   下载失败时自动切换 hf-mirror.com 重试；也可手动 export HF_ENDPOINT=https://hf-mirror.com
NEXT
exit 0
