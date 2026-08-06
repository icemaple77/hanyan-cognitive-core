#!/usr/bin/env bash
# HCC (Hanyan Cognitive Core) 一键安装脚本
#
# 用法：
#   ./install.sh                  基础安装（venv + dev extras，DB 优先走 Docker）
#   ./install.sh --full           额外安装 rerank + pdf 两个可选 extras
#   ./install.sh --with-rerank    额外安装交叉编码器重排（llama-cpp-python，需要本地编译）
#   ./install.sh --with-pdf       额外安装 PDF 索引支持（pdf-inspector）
#   ./install.sh --skip-db        跳过 PostgreSQL 安装/初始化（已有现成数据库时用）
#   ./install.sh --skip-redis     跳过 Redis 安装（默认 HCC_REDIS_ENABLED=false，本来就不装也能跑）
#   ./install.sh --skip-ollama    跳过 Ollama 检测/安装提示
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
  C_RESET='\033[0m'; C_GREEN='\033[0;32m'; C_YELLOW='\033[0;33m'; C_RED='\033[0;31m'; C_BLUE='\033[0;34m'
else
  C_RESET=''; C_GREEN=''; C_YELLOW=''; C_RED=''; C_BLUE=''
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
  if [[ "$default" == "Y" ]]; then
    read -r -p "$prompt [Y/n] " reply
    reply="${reply:-Y}"
  else
    read -r -p "$prompt [y/N] " reply
    reply="${reply:-N}"
  fi
  [[ "$reply" =~ ^[Yy] ]]
}

# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------
WITH_RERANK=0
WITH_PDF=0
SKIP_DB=0
SKIP_REDIS=0
SKIP_OLLAMA=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --full)         WITH_RERANK=1; WITH_PDF=1 ;;
    --with-rerank)  WITH_RERANK=1 ;;
    --with-pdf)     WITH_PDF=1 ;;
    --skip-db)      SKIP_DB=1 ;;
    --skip-redis)   SKIP_REDIS=1 ;;
    --skip-ollama)  SKIP_OLLAMA=1 ;;
    -h|--help)
      sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) die "未知参数：$1（用 --help 查看用法）" ;;
  esac
  shift
done

OS_NAME="$(uname -s)"
DB_MODE=""   # docker | native | skipped

echo ""
info "======================================================"
info "  HCC — Hanyan Cognitive Core 一键安装"
info "  跨 Agent 统一记忆层 · 混合检索 · 梦境巩固 · 情绪引擎"
info "======================================================"
echo ""

# ---------------------------------------------------------------------------
# 1. Python 3.11+
# ---------------------------------------------------------------------------
info "[1/6] 检查 Python 3.11+ ..."

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

if [[ -z "$PYTHON_BIN" ]]; then
  warn "未找到 Python 3.11+。"
  if [[ "$OS_NAME" == "Darwin" ]] && command -v brew >/dev/null 2>&1; then
    if ask_yes "是否用 Homebrew 安装 python@3.12？"; then
      brew install python@3.12 || die "python@3.12 安装失败，请手动安装 Python 3.11+ 后重跑本脚本。"
      PYTHON_BIN="python3.12"
    else
      die "请先安装 Python 3.11+ 后重跑本脚本。"
    fi
  elif command -v apt-get >/dev/null 2>&1; then
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
ok "使用 ${PYTHON_BIN}（$(${PYTHON_BIN} -c 'import sys; print(sys.version.split()[0])')）"

# ---------------------------------------------------------------------------
# 2. PostgreSQL 17 + pgvector（可选跳过）
# ---------------------------------------------------------------------------
if [[ "$SKIP_DB" -eq 1 ]]; then
  info "[2/6] 已指定 --skip-db，跳过 PostgreSQL 安装/初始化。"
  DB_MODE="skipped"
else
  info "[2/6] 检查 PostgreSQL 17 + pgvector ..."

  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    if ask_yes "检测到 Docker，是否用 docker compose 一键启动 PostgreSQL(pgvector) + Redis 容器？（最省事，推荐）"; then
      docker compose up -d db redis || die "docker compose 启动失败，请检查 Docker 是否正常运行。"
      DB_MODE="docker"
      ok "PostgreSQL(pgvector) + Redis 容器已启动（db: localhost:5433, redis: localhost:6381）。"
    fi
  fi

  if [[ -z "$DB_MODE" ]]; then
    DB_MODE="native"
    if command -v psql >/dev/null 2>&1; then
      ok "检测到本机已安装 psql，跳过安装步骤。"
    else
      warn "未检测到 PostgreSQL 客户端。"
      if [[ "$OS_NAME" == "Darwin" ]]; then
        command -v brew >/dev/null 2>&1 || die "未检测到 Homebrew（https://brew.sh），请先安装后重跑本脚本，或改用 Docker。"
        if ask_yes "是否用 Homebrew 安装 postgresql@17 + pgvector？"; then
          brew install postgresql@17 pgvector || die "PostgreSQL/pgvector 安装失败。"
          brew services start postgresql@17 || warn "postgresql@17 服务启动失败，请手动执行 brew services start postgresql@17"
        else
          die "请自行安装 PostgreSQL 17 + pgvector 后重跑本脚本（或用 --skip-db 跳过）。"
        fi
      elif command -v apt-get >/dev/null 2>&1; then
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
    info "初始化本机 hcc 角色 / 数据库（端口 5432）..."
    if [[ "$OS_NAME" == "Darwin" ]]; then
      PSQL_CMD=(psql -h localhost -p 5432 -U "$(whoami)" -d postgres)
    else
      PSQL_CMD=(sudo -u postgres psql -h localhost -p 5432)
    fi
    {
      "${PSQL_CMD[@]}" -tc "SELECT 1 FROM pg_roles WHERE rolname='hcc'" | grep -q 1 \
        || "${PSQL_CMD[@]}" -c "CREATE ROLE hcc LOGIN PASSWORD 'hcc';"
      "${PSQL_CMD[@]}" -tc "SELECT 1 FROM pg_database WHERE datname='hcc'" | grep -q 1 \
        || "${PSQL_CMD[@]}" -c "CREATE DATABASE hcc OWNER hcc;"
      "${PSQL_CMD[@]}" -d hcc -c "CREATE EXTENSION IF NOT EXISTS vector;"
    } && ok "本机 hcc 角色/数据库/pgvector 扩展已就绪。" \
      || warn "自动创建角色/数据库失败（权限或连接问题），请手动创建后再运行 --skip-db 安装，或检查 .env 中 HCC_DATABASE_URL。"
  fi
fi

# ---------------------------------------------------------------------------
# 3. Redis（可选，未启用时事件总线自动退化为进程内广播）
# ---------------------------------------------------------------------------
if [[ "$SKIP_REDIS" -eq 1 || "$DB_MODE" == "docker" ]]; then
  info "[3/6] 跳过独立 Redis 安装（--skip-redis 或已随 docker compose 一并启动）。"
elif [[ "$SKIP_DB" -eq 1 ]]; then
  info "[3/6] 跳过 Redis（随 --skip-db 一起跳过，按需自行安装）。"
else
  info "[3/6] Redis（可选 —— 默认 HCC_REDIS_ENABLED=false，不装也能跑）..."
  if command -v redis-cli >/dev/null 2>&1; then
    ok "检测到本机已安装 Redis，跳过。"
  elif ask_yes "是否安装本机 Redis？" N; then
    if [[ "$OS_NAME" == "Darwin" ]] && command -v brew >/dev/null 2>&1; then
      brew install redis && brew services start redis
    elif command -v apt-get >/dev/null 2>&1; then
      sudo apt-get update && sudo apt-get install -y redis-server && sudo systemctl enable --now redis-server
    else
      warn "无法自动安装，请参考 https://redis.io/docs/getting-started/ 手动安装。"
    fi
  else
    info "跳过 Redis 安装（HCC_REDIS_ENABLED=false 时不影响启动）。"
  fi
fi

# ---------------------------------------------------------------------------
# 4. Ollama（可选，本地 embedding / 本地降噪需要）
# ---------------------------------------------------------------------------
if [[ "$SKIP_OLLAMA" -eq 1 ]]; then
  info "[4/6] 已指定 --skip-ollama，跳过。"
else
  info "[4/6] Ollama（可选 —— 本地 embedding / 本地降噪模型需要，不装则用 HCC_EMBEDDING_PROVIDER=hash 兜底）..."
  if command -v ollama >/dev/null 2>&1; then
    ok "检测到本机已安装 Ollama。"
    if ask_yes "是否拉取推荐模型（qwen3-embedding:0.6b 用于中文 embedding，qwen3.5:4b 用于本地降噪）？" N; then
      ollama pull qwen3-embedding:0.6b || warn "qwen3-embedding:0.6b 拉取失败，可稍后手动 ollama pull。"
      ollama pull qwen3.5:4b || warn "qwen3.5:4b 拉取失败，可稍后手动 ollama pull。"
    fi
  else
    warn "未检测到 Ollama。"
    if [[ "$OS_NAME" == "Darwin" ]] && command -v brew >/dev/null 2>&1; then
      if ask_yes "是否用 Homebrew 安装 Ollama？" N; then
        brew install ollama && brew services start ollama
      fi
    else
      info "可参考 https://ollama.com/download 手动安装；不装也能跑，.env 中把 HCC_EMBEDDING_PROVIDER 设为 hash 即可零依赖启动。"
    fi
  fi
fi

# ---------------------------------------------------------------------------
# 5. Python venv + 依赖安装
# ---------------------------------------------------------------------------
info "[5/6] 创建 venv 并安装依赖 ..."

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

# ---------------------------------------------------------------------------
# 6. .env 配置 + 数据库初始化
# ---------------------------------------------------------------------------
info "[6/6] 生成配置 / 初始化数据库表 ..."

if [[ -f .env ]]; then
  ok ".env 已存在，跳过生成（如需重置请先手动删除 .env 再重跑本脚本）。"
else
  [[ -f .env.example ]] || die "找不到 .env.example，无法生成配置。"
  cp .env.example .env
  if [[ "$DB_MODE" == "native" ]]; then
    # .env.example 默认端口 5433 对应 docker-compose 映射；本机原生安装用标准 5432
    sed_inplace 's#localhost:5433/hcc#localhost:5432/hcc#' .env
  fi
  ok "已生成 .env"
  warn "请检查 .env 中的 HCC_DATABASE_URL / HCC_VAULT_ROOT / HCC_QMD_DIR 等字段，按需修改后再启动。"
fi

if [[ "$SKIP_DB" -eq 1 ]]; then
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
