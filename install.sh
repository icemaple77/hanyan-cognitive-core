#!/usr/bin/env bash
# HCC (Hanyan Cognitive Core) 一键安装脚本
#
# 用法：
#   ./install.sh                  交互式安装（检测环境 → 逐项确认，默认值直接回车）
#   ./install.sh -y | --yes       非交互模式，全部使用默认值/已识别环境，不问不停
#   ./install.sh --full           额外安装 rerank + pdf 两个可选 extras
#   ./install.sh --with-rerank    额外安装交叉编码器重排（llama-cpp-python，需要本地编译）
#   ./install.sh --with-pdf       额外安装 PDF 索引支持（pdf-inspector）
#   ./install.sh --no-docker      纯本地模式：不使用 Docker，直接用 brew/apt 装 PostgreSQL 17 + pgvector
#   ./install.sh --skip-db        跳过 PostgreSQL 安装/初始化（已有现成数据库时用）
#   ./install.sh --skip-redis     跳过 Redis 安装
#   ./install.sh --with-redis     强制安装本机 Redis（默认不装也能跑，HCC_REDIS_ENABLED=false）
#   ./install.sh --skip-ollama    跳过 Ollama 检测/安装提示
#   ./install.sh --db-host=HOST   数据库主机（默认 localhost）
#   ./install.sh --db-port=PORT  数据库端口（默认 native 5432 / docker 5433）
#   ./install.sh --db-name=NAME  数据库名（默认 hcc）
#   ./install.sh --db-user=USER  数据库用户（默认 hcc）
#   ./install.sh --embedding-model=MODEL   本地 embedding 模型（默认 qwen3-embedding:0.6b）
#   ./install.sh -h | --help      查看帮助
#
# 幂等：重复运行安全 —— 已存在的 venv / .env / 数据库表都会被跳过或做 IF NOT EXISTS 处理。
set -uo pipefail

# ---------------------------------------------------------------------------
# 基础工具函数
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ -t 1 ]; then
  C_RESET='\033[0m'; C_GREEN='\033[0;32m'; C_YELLOW='\033[0;33m'; C_RED='\033[0;31m'; C_BLUE='\033[0;34m'; C_BOLD='\033[1m'
else
  C_RESET=''; C_GREEN=''; C_YELLOW=''; C_RED=''; C_BLUE=''; C_BOLD=''
fi

info()  { printf "${C_BLUE}[HCC]${C_RESET} %s\n" "$1"; }
ok()    { printf "${C_GREEN}[OK]${C_RESET} %s\n" "$1"; }
warn()  { printf "${C_YELLOW}[警告]${C_RESET} %s\n" "$1"; }
die()   { printf "${C_RED}[错误]${C_RESET} %s\n" "$1" >&2; exit 1; }

# BSD sed（macOS）与 GNU sed（Linux）的 -i 参数不兼容，统一封装一下
sed_inplace() {
  if [[ "$(uname -s)" == "Darwin" ]]; then
    sed -i '' "$1" "$2"
  else
    sed -i "$1" "$2"
  fi
}

ask_yes() {
  # ask_yes "提示语" [默认 Y|N]
  local prompt="$1" default="${2:-Y}" reply
  if [[ "$NON_INTERACTIVE" -eq 1 ]]; then
    [[ "$default" == "Y" ]]
    return
  fi
  if [[ "$default" == "Y" ]]; then
    read -r -p "$prompt [Y/n] " reply
    reply="${reply:-Y}"
  else
    read -r -p "$prompt [y/N] " reply
    reply="${reply:-N}"
  fi
  [[ "$reply" =~ ^[Yy] ]]
}

ask_value() {
  # ask_value "提示语" "默认值" -> 通过 stdout 返回结果
  local prompt="$1" default="$2" reply
  if [[ "$NON_INTERACTIVE" -eq 1 ]]; then
    printf '%s' "$default"
    return
  fi
  read -r -p "$prompt [默认: $default] " reply
  printf '%s' "${reply:-$default}"
}

# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------
NON_INTERACTIVE=0
WITH_RERANK=0
WITH_PDF=0
NO_DOCKER=0
SKIP_DB=0
SKIP_REDIS=0
WITH_REDIS=0
SKIP_OLLAMA=0
DB_HOST_OVERRIDE=""
DB_PORT_OVERRIDE=""
DB_NAME_OVERRIDE=""
DB_USER_OVERRIDE=""
EMBEDDING_MODEL_OVERRIDE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    -y|--yes)        NON_INTERACTIVE=1 ;;
    --full)          WITH_RERANK=1; WITH_PDF=1 ;;
    --with-rerank)   WITH_RERANK=1 ;;
    --with-pdf)      WITH_PDF=1 ;;
    --no-docker)     NO_DOCKER=1 ;;
    --skip-db)       SKIP_DB=1 ;;
    --skip-redis)    SKIP_REDIS=1 ;;
    --with-redis)    WITH_REDIS=1 ;;
    --skip-ollama)   SKIP_OLLAMA=1 ;;
    --db-host=*)     DB_HOST_OVERRIDE="${1#*=}" ;;
    --db-port=*)     DB_PORT_OVERRIDE="${1#*=}" ;;
    --db-name=*)     DB_NAME_OVERRIDE="${1#*=}" ;;
    --db-user=*)     DB_USER_OVERRIDE="${1#*=}" ;;
    --embedding-model=*) EMBEDDING_MODEL_OVERRIDE="${1#*=}" ;;
    -h|--help)
      sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) die "未知参数：$1（用 --help 查看用法）" ;;
  esac
  shift
done

if [[ "$SKIP_REDIS" -eq 1 && "$WITH_REDIS" -eq 1 ]]; then
  die "--skip-redis 和 --with-redis 不能同时指定。"
fi

OS_NAME="$(uname -s)"

echo ""
info "======================================================"
info "  HCC — Hanyan Cognitive Core 一键安装"
info "  跨 Agent 统一记忆层 · 混合检索 · 梦境巩固 · 情绪引擎"
info "======================================================"
echo ""

# ---------------------------------------------------------------------------
# 阶段 1：检测环境
# ---------------------------------------------------------------------------
info "[检测] 正在识别本机环境 ..."

PYTHON_BIN=""
for cand in python3.13 python3.12 python3.11 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    ver="$("$cand" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    major="${ver%%.*}"; minor="${ver##*.}"
    if [[ "$major" -eq 3 && "$minor" -ge 11 ]]; then
      PYTHON_BIN="$cand"
      break
    fi
  fi
done
[[ -n "$PYTHON_BIN" ]] && ok "  Python: ${PYTHON_BIN}（$(${PYTHON_BIN} -c 'import sys; print(sys.version.split()[0])')）" \
                        || warn "  Python 3.11+: 未检测到"

HAS_DOCKER=0
if [[ "$NO_DOCKER" -eq 0 ]] && command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  HAS_DOCKER=1
  ok "  Docker: 已检测到（docker compose 可用）"
elif [[ "$NO_DOCKER" -eq 1 ]]; then
  info "  Docker: 已指定 --no-docker，跳过检测，走纯本地安装"
else
  info "  Docker: 未检测到（将走本机原生安装路径）"
fi

HAS_PSQL=0
command -v psql >/dev/null 2>&1 && HAS_PSQL=1 && ok "  psql: 已检测到" || info "  psql: 未检测到"

HAS_REDIS=0
command -v redis-cli >/dev/null 2>&1 && HAS_REDIS=1 && ok "  redis-cli: 已检测到" || info "  redis-cli: 未检测到"

HAS_OLLAMA=0
command -v ollama >/dev/null 2>&1 && HAS_OLLAMA=1 && ok "  ollama: 已检测到" || info "  ollama: 未检测到"

BREW_OK=0
[[ "$OS_NAME" == "Darwin" ]] && command -v brew >/dev/null 2>&1 && BREW_OK=1
APT_OK=0
command -v apt-get >/dev/null 2>&1 && APT_OK=1

echo ""

# ---------------------------------------------------------------------------
# 阶段 2：交互式收集配置（默认值直接回车即可）
# ---------------------------------------------------------------------------
if [[ "$NON_INTERACTIVE" -eq 1 ]]; then
  info "[配置] 已指定 -y/--yes，使用默认值 + 已识别环境，跳过逐项确认。"
else
  info "[配置] 接下来会问几个问题，直接回车用默认值即可。"
fi
echo ""

# --- Python 缺失时的处理 ---
if [[ -z "$PYTHON_BIN" ]]; then
  warn "未找到 Python 3.11+。"
  if [[ "$BREW_OK" -eq 1 ]]; then
    if ask_yes "是否用 Homebrew 安装 python@3.12？"; then
      brew install python@3.12 || die "python@3.12 安装失败，请手动安装 Python 3.11+ 后重跑本脚本。"
      PYTHON_BIN="python3.12"
    else
      die "请先安装 Python 3.11+ 后重跑本脚本。"
    fi
  elif [[ "$APT_OK" -eq 1 ]]; then
    if ask_yes "是否用 apt 安装 python3.12（需要 sudo）？"; then
      sudo apt-get update && sudo apt-get install -y python3.12 python3.12-venv || die "python3.12 安装失败。"
      PYTHON_BIN="python3.12"
    else
      die "请先安装 Python 3.11+ 后重跑本脚本。"
    fi
  else
    die "请先安装 Python 3.11+（https://www.python.org/downloads/）后重跑本脚本。"
  fi
fi

# --- DB 模式选择 ---
DB_MODE=""   # docker | native | skipped
if [[ "$SKIP_DB" -eq 1 ]]; then
  DB_MODE="skipped"
elif [[ "$HAS_DOCKER" -eq 1 ]]; then
  if ask_yes "检测到 Docker，是否用 docker compose 一键启动 PostgreSQL(pgvector) + Redis 容器？（最省事，推荐）"; then
    DB_MODE="docker"
  else
    DB_MODE="native"
  fi
else
  DB_MODE="native"
fi

DB_HOST="${DB_HOST_OVERRIDE:-localhost}"
DB_NAME="${DB_NAME_OVERRIDE:-hcc}"
DB_USER="${DB_USER_OVERRIDE:-hcc}"
if [[ "$DB_MODE" == "docker" ]]; then
  DB_PORT="${DB_PORT_OVERRIDE:-5433}"
elif [[ "$DB_MODE" == "native" ]]; then
  DB_PORT="${DB_PORT_OVERRIDE:-5432}"
else
  DB_PORT="${DB_PORT_OVERRIDE:-5432}"
fi

if [[ "$DB_MODE" == "native" && -z "$DB_HOST_OVERRIDE$DB_PORT_OVERRIDE$DB_NAME_OVERRIDE$DB_USER_OVERRIDE" ]]; then
  DB_HOST="$(ask_value "数据库主机？" "$DB_HOST")"
  DB_PORT="$(ask_value "数据库端口？" "$DB_PORT")"
  DB_NAME="$(ask_value "数据库名？" "$DB_NAME")"
  DB_USER="$(ask_value "数据库用户？" "$DB_USER")"
fi

# --- Redis ---
REDIS_MODE="skip"  # skip | docker | native
if [[ "$SKIP_REDIS" -eq 1 || "$DB_MODE" == "docker" ]]; then
  [[ "$DB_MODE" == "docker" ]] && REDIS_MODE="docker" || REDIS_MODE="skip"
elif [[ "$SKIP_DB" -eq 1 && "$WITH_REDIS" -eq 0 ]]; then
  REDIS_MODE="skip"
elif [[ "$HAS_REDIS" -eq 1 ]]; then
  REDIS_MODE="skip"   # 已装，无需再装
elif [[ "$WITH_REDIS" -eq 1 ]]; then
  REDIS_MODE="native"
elif ask_yes "是否安装本机 Redis？（可选，默认 HCC_REDIS_ENABLED=false 不装也能跑）" N; then
  REDIS_MODE="native"
fi

# --- Ollama ---
OLLAMA_MODE="skip"  # skip | install | pull-only
EMBEDDING_MODEL="${EMBEDDING_MODEL_OVERRIDE:-qwen3-embedding:0.6b}"
PULL_OLLAMA_MODELS=0
if [[ "$SKIP_OLLAMA" -eq 1 ]]; then
  OLLAMA_MODE="skip"
elif [[ "$HAS_OLLAMA" -eq 1 ]]; then
  OLLAMA_MODE="skip"
  if ask_yes "检测到 Ollama，是否拉取推荐模型（${EMBEDDING_MODEL} 用于中文 embedding，qwen3.5:4b 用于本地降噪）？" N; then
    PULL_OLLAMA_MODELS=1
  fi
elif [[ "$BREW_OK" -eq 1 ]]; then
  if ask_yes "未检测到 Ollama，是否用 Homebrew 安装？（本地 embedding / 本地降噪需要，不装则用 hash 兜底）" N; then
    OLLAMA_MODE="install"
    PULL_OLLAMA_MODELS=1
  fi
fi

echo ""

# ---------------------------------------------------------------------------
# 阶段 3：汇总确认
# ---------------------------------------------------------------------------
info "======================================================"
info "  安装方案汇总"
info "======================================================"
echo -e "  ${C_BOLD}Python${C_RESET}          ${PYTHON_BIN:-待安装}"
if [[ "$NO_DOCKER" -eq 1 ]]; then
  echo -e "  ${C_BOLD}模式${C_RESET}            纯本地（--no-docker，不依赖 Docker）"
fi
case "$DB_MODE" in
  docker)  echo -e "  ${C_BOLD}数据库${C_RESET}          Docker compose（PostgreSQL 17 + pgvector + Redis）端口 ${DB_PORT}" ;;
  native)  echo -e "  ${C_BOLD}数据库${C_RESET}          本机原生安装 —— ${DB_USER}@${DB_HOST}:${DB_PORT}/${DB_NAME}" ;;
  skipped) echo -e "  ${C_BOLD}数据库${C_RESET}          跳过（--skip-db，需已有可用实例）" ;;
esac
case "$REDIS_MODE" in
  docker) echo -e "  ${C_BOLD}Redis${C_RESET}           随 Docker compose 一起启动" ;;
  native) echo -e "  ${C_BOLD}Redis${C_RESET}           安装本机 Redis" ;;
  skip)   echo -e "  ${C_BOLD}Redis${C_RESET}           不安装（HCC_REDIS_ENABLED=false 不影响启动）" ;;
esac
case "$OLLAMA_MODE" in
  install) echo -e "  ${C_BOLD}Ollama${C_RESET}          安装 + 拉取 ${EMBEDDING_MODEL} / qwen3.5:4b" ;;
  skip)
    if [[ "$PULL_OLLAMA_MODELS" -eq 1 ]]; then
      echo -e "  ${C_BOLD}Ollama${C_RESET}          已安装，拉取 ${EMBEDDING_MODEL} / qwen3.5:4b"
    else
      echo -e "  ${C_BOLD}Ollama${C_RESET}          跳过（embedding 走 hash 兜底）"
    fi
    ;;
esac
[[ "$WITH_RERANK" -eq 1 ]] && echo -e "  ${C_BOLD}Extras${C_RESET}          + rerank"
[[ "$WITH_PDF" -eq 1 ]]    && echo -e "  ${C_BOLD}Extras${C_RESET}          + pdf"
echo ""

if ! ask_yes "确认按以上方案开始安装？"; then
  die "已取消安装。"
fi
echo ""

# ---------------------------------------------------------------------------
# 阶段 4：执行安装
# ---------------------------------------------------------------------------

# --- 1. 数据库 ---
if [[ "$DB_MODE" == "docker" ]]; then
  info "[1/5] 启动 Docker 容器（PostgreSQL + Redis）..."
  docker compose up -d db redis || die "docker compose 启动失败，请检查 Docker 是否正常运行。"
  ok "PostgreSQL(pgvector) + Redis 容器已启动（db: localhost:${DB_PORT}, redis: localhost:6381）。"
elif [[ "$DB_MODE" == "native" ]]; then
  info "[1/5] 检查 / 安装 PostgreSQL 17 + pgvector ..."
  if [[ "$HAS_PSQL" -eq 1 ]]; then
    ok "检测到本机已安装 psql，跳过安装步骤。"
  else
    warn "未检测到 PostgreSQL 客户端。"
    if [[ "$BREW_OK" -eq 1 ]]; then
      if ask_yes "是否用 Homebrew 安装 postgresql@17 + pgvector？"; then
        brew install postgresql@17 pgvector || die "PostgreSQL/pgvector 安装失败。"
        brew services start postgresql@17 || warn "postgresql@17 服务启动失败，请手动执行 brew services start postgresql@17"
      else
        die "请自行安装 PostgreSQL 17 + pgvector 后重跑本脚本（或用 --skip-db 跳过）。"
      fi
    elif [[ "$APT_OK" -eq 1 ]]; then
      if ask_yes "是否用 apt 安装 postgresql-17 + pgvector（需要 sudo，部分发行版需先添加 apt.postgresql.org 源）？"; then
        sudo apt-get update
        sudo apt-get install -y postgresql-17 postgresql-17-pgvector \
          || warn "标准 apt 源可能没有 postgresql-17-pgvector 包，请参考 https://github.com/pgvector/pgvector#installation 手动编译安装 pgvector 扩展。"
        sudo systemctl enable --now postgresql || warn "postgresql 服务启动失败，请手动执行 systemctl start postgresql"
      else
        die "请自行安装 PostgreSQL 17 + pgvector 后重跑本脚本（或用 --skip-db 跳过）。"
      fi
    else
      die "无法识别的操作系统/包管理器，请手动安装 PostgreSQL 17 + pgvector 后重跑本脚本（或用 --skip-db 跳过）。"
    fi
  fi

  # 创建本机角色/数据库（幂等：IF NOT EXISTS 语义），失败只警告不中断安装
  info "初始化本机 ${DB_USER} 角色 / ${DB_NAME} 数据库（${DB_HOST}:${DB_PORT}）..."
  if [[ "$OS_NAME" == "Darwin" ]]; then
    PSQL_CMD=(psql -h "$DB_HOST" -p "$DB_PORT" -U "$(whoami)" -d postgres)
  else
    PSQL_CMD=(sudo -u postgres psql -h "$DB_HOST" -p "$DB_PORT")
  fi
  {
    "${PSQL_CMD[@]}" -tc "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'" | grep -q 1 \
      || "${PSQL_CMD[@]}" -c "CREATE ROLE ${DB_USER} LOGIN PASSWORD '${DB_USER}';"
    "${PSQL_CMD[@]}" -tc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1 \
      || "${PSQL_CMD[@]}" -c "CREATE DATABASE ${DB_NAME} OWNER ${DB_USER};"
    "${PSQL_CMD[@]}" -d "$DB_NAME" -c "CREATE EXTENSION IF NOT EXISTS vector;"
  } && ok "本机 ${DB_USER} 角色/${DB_NAME} 数据库/pgvector 扩展已就绪。" \
    || warn "自动创建角色/数据库失败（权限或连接问题），请手动创建后再运行 --skip-db 安装，或检查 .env 中 HCC_DATABASE_URL。"
else
  info "[1/5] 已跳过数据库安装（--skip-db）。"
fi

# --- 2. Redis ---
if [[ "$REDIS_MODE" == "native" ]]; then
  info "[2/5] 安装本机 Redis ..."
  if [[ "$BREW_OK" -eq 1 ]]; then
    brew install redis && brew services start redis
  elif [[ "$APT_OK" -eq 1 ]]; then
    sudo apt-get update && sudo apt-get install -y redis-server && sudo systemctl enable --now redis-server
  else
    warn "无法自动安装，请参考 https://redis.io/docs/getting-started/ 手动安装。"
  fi
else
  info "[2/5] 跳过独立 Redis 安装（$( [[ "$REDIS_MODE" == "docker" ]] && echo "已随 docker compose 启动" || echo "默认不装也能跑" ))。"
fi

# --- 3. Ollama ---
if [[ "$OLLAMA_MODE" == "install" ]]; then
  info "[3/5] 安装 Ollama ..."
  brew install ollama && brew services start ollama
elif [[ "$SKIP_OLLAMA" -eq 1 ]]; then
  info "[3/5] 已指定 --skip-ollama，跳过。"
else
  info "[3/5] Ollama 已就绪或跳过。"
fi
if [[ "$PULL_OLLAMA_MODELS" -eq 1 ]] && command -v ollama >/dev/null 2>&1; then
  ollama pull "$EMBEDDING_MODEL" || warn "${EMBEDDING_MODEL} 拉取失败，可稍后手动 ollama pull。"
  ollama pull qwen3.5:4b || warn "qwen3.5:4b 拉取失败，可稍后手动 ollama pull。"
elif [[ "$HAS_OLLAMA" -eq 0 && "$OLLAMA_MODE" != "install" && "$SKIP_OLLAMA" -eq 0 ]]; then
  info "可参考 https://ollama.com/download 手动安装；不装也能跑，.env 中把 HCC_EMBEDDING_PROVIDER 设为 hash 即可零依赖启动。"
fi

# --- 4. Python venv + 依赖安装 ---
info "[4/5] 创建 venv 并安装依赖 ..."

if [[ -d .venv ]]; then
  ok ".venv 已存在，跳过创建。"
else
  "$PYTHON_BIN" -m venv .venv || die "venv 创建失败。"
  ok ".venv 创建完成。"
fi

# shellcheck disable=SC1091
source .venv/bin/activate

EXTRAS="dev"
[[ "$WITH_RERANK" -eq 1 ]] && EXTRAS="${EXTRAS},rerank"
[[ "$WITH_PDF" -eq 1 ]] && EXTRAS="${EXTRAS},pdf"

info "安装 extras：[$EXTRAS]（用 --full / --with-rerank / --with-pdf 追加可选依赖）..."
env -u PYTHONPATH pip install --upgrade pip >/dev/null
env -u PYTHONPATH pip install -e ".[${EXTRAS}]" || die "pip install 失败，请检查上方报错（rerank/pdf extras 需要本地编译环境）。"
ok "依赖安装完成。"

deactivate

# --- 5. .env 配置 + 数据库初始化 ---
info "[5/5] 生成配置 / 初始化数据库表 ..."

if [[ -f .env ]]; then
  ok ".env 已存在，跳过生成（如需重置请先手动删除 .env 再重跑本脚本）。"
else
  [[ -f .env.example ]] || die "找不到 .env.example，无法生成配置。"
  cp .env.example .env
  if [[ "$DB_MODE" != "skipped" ]]; then
    sed_inplace "s#localhost:5433/hcc#${DB_HOST}:${DB_PORT}/${DB_NAME}#" .env
    sed_inplace "s#hcc:hcc@#${DB_USER}:${DB_USER}@#" .env
  fi
  if [[ -n "$EMBEDDING_MODEL_OVERRIDE" ]]; then
    grep -q '^HCC_EMBEDDING_MODEL=' .env \
      && sed_inplace "s#^HCC_EMBEDDING_MODEL=.*#HCC_EMBEDDING_MODEL=${EMBEDDING_MODEL}#" .env \
      || echo "HCC_EMBEDDING_MODEL=${EMBEDDING_MODEL}" >> .env
  fi
  ok "已生成 .env"
  warn "请检查 .env 中的 HCC_DATABASE_URL / HCC_VAULT_ROOT / HCC_QMD_DIR 等字段，按需修改后再启动。"
fi

if [[ "$DB_MODE" == "skipped" ]]; then
  info "已跳过数据库初始化（--skip-db），启动前请自行确认 .env 中 HCC_DATABASE_URL 可连接。"
else
  info "初始化数据库表结构（幂等，可重复执行）..."
  env -u PYTHONPATH .venv/bin/python -c "
import asyncio
from dotenv import load_dotenv
load_dotenv()
from sqlalchemy import text
from gateway.core.database import engine, Base

async def main():
    async with engine.begin() as conn:
        await conn.execute(text('CREATE EXTENSION IF NOT EXISTS vector'))
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()

asyncio.run(main())
" && ok "数据库表已就绪。" \
  || warn "数据库初始化失败，请确认 .env 中 HCC_DATABASE_URL 指向的 PostgreSQL 实例可连接（用户/密码/端口是否匹配），修好后可重新运行本脚本。"
fi

# ---------------------------------------------------------------------------
# 完成
# ---------------------------------------------------------------------------
echo ""
ok "======================================================"
ok "  安装完成！"
ok "======================================================"
echo ""
echo "启动 API 网关："
echo "  source .venv/bin/activate"
echo "  uvicorn gateway.main:app --host 0.0.0.0 --port 8000"
echo "  （或：make dev）"
echo ""
echo "启动 MCP stdio server（供 Claude Code 等 MCP 客户端接入）："
echo "  source .venv/bin/activate"
echo "  python mcp/server.py"
echo ""
echo "健康检查："
echo "  curl http://localhost:8000/api/v1/health"
echo ""
echo "OpenClaw 插件接入见 hcc-openclaw-plugin/README.md"
echo ""
