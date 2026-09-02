#!/usr/bin/env bash
#
# FitMind AI 一键启动。
#
#   ./start.sh              构建并启动全部服务，等健康检查通过后灌入演示数据
#   ./start.sh --no-build   跳过镜像构建（只是重启，没改代码时更快）
#   ./start.sh --no-seed    不灌演示数据
#
# 脚本本身幂等：重复执行不会重复建账号或重复插数据。

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

readonly HEALTH_TIMEOUT_S=120
DO_BUILD=1
DO_SEED=1

# ── 颜色。非 TTY（CI、管道）时全部退化为空串，避免转义码污染日志 ────────
if [[ -t 1 ]]; then
  R=$'\033[31m'; G=$'\033[32m'; Y=$'\033[33m'; B=$'\033[36m'; D=$'\033[2m'; N=$'\033[0m'
else
  R=''; G=''; Y=''; B=''; D=''; N=''
fi

info() { printf '%s==>%s %s\n' "$B" "$N" "$1"; }
ok()   { printf '%s  ✓%s %s\n' "$G" "$N" "$1"; }
warn() { printf '%s  !%s %s\n' "$Y" "$N" "$1"; }
die()  { printf '%s  ✗%s %s\n' "$R" "$N" "$1" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-build) DO_BUILD=0 ;;
    --no-seed)  DO_SEED=0 ;;
    -h|--help)  sed -n '3,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)          die "未知参数：$1（用 --help 查看用法）" ;;
  esac
  shift
done

# ── 1. Compose 命令 ────────────────────────────────────────────────────
# 新版是 docker 的子命令，旧版是独立可执行文件，两种都得支持。
if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  die "找不到 Docker Compose。请安装 Docker Desktop 或 Compose 插件。"
fi

command -v docker >/dev/null 2>&1 || die "找不到 docker 命令。"
docker info >/dev/null 2>&1 || die "Docker 守护进程没在运行，请先启动 Docker Desktop。"
ok "Docker 就绪（${COMPOSE[*]}）"

# ── 2. 配置文件 ────────────────────────────────────────────────────────
if [[ ! -f .env ]]; then
  cp .env.example .env
  printf '\n'
  warn "已从 .env.example 生成 .env"
  printf '\n    请编辑 .env 填写这三项后重新运行本脚本：\n\n'
  printf '      LLM_BASE_URL   OpenAI 兼容网关地址\n'
  printf '      LLM_API_KEY    网关 API Key\n'
  printf '      LLM_MODEL      对话模型名\n\n'
  exit 1
fi

# 用 grep 取值而不是 source .env：值里的空格、#、引号会让 source 出意外行为。
env_value() {
  sed -n "s/^$1=//p" .env | tail -1 | tr -d '\r' | sed 's/^["'\'']//; s/["'\'']$//'
}

env_value_or_default() {
  local value
  value="$(env_value "$1")"
  printf '%s' "${value:-$2}"
}

missing=()
for key in LLM_BASE_URL LLM_API_KEY LLM_MODEL; do
  [[ -n "$(env_value "$key")" ]] || missing+=("$key")
done

if (( ${#missing[@]} > 0 )); then
  warn ".env 里这几项是空的：${missing[*]}"
  warn "对话会走 L4 规则兜底（不调模型），其余功能正常。"
else
  ok ".env 已配置模型服务"
fi

if [[ "$(env_value JWT_SECRET)" == 请替换* ]]; then
  warn "JWT_SECRET 还是占位值。本地体验可用，正式环境请换：openssl rand -hex 32"
fi

# ── 3. 端口占用 ────────────────────────────────────────────────────────
# 只警告，不终止。
#
# 曾经想过"识别监听进程是不是 Docker，不是就报错退出"——那是错的。Docker Desktop
# 的端口转发在不同平台由不同进程持有（本机实测是 ssh 隧道，不是 com.docker），
# 按进程名做白名单会把正常环境判成冲突，直接堵死启动。
#
# 端口到底能不能用，让 compose 说话：它冲突时会明确报 "port is already allocated"。
# 这里只在自己的容器都没跑、端口却被占着时提个醒，帮助定位。
WEB_PORT="$(env_value_or_default WEB_PORT 3000)"
API_PORT="$(env_value_or_default API_PORT 8000)"
POSTGRES_PORT="$(env_value_or_default POSTGRES_PORT 5433)"
NEXT_PUBLIC_API_BASE="$(env_value_or_default NEXT_PUBLIC_API_BASE "http://localhost:${API_PORT}/api/v1")"

for port in "$WEB_PORT" "$API_PORT" "$POSTGRES_PORT"; do
  [[ "$port" =~ ^[0-9]+$ ]] && (( port >= 1024 && port <= 65535 )) \
    || die "端口配置无效：$port（WEB_PORT、API_PORT、POSTGRES_PORT 必须是 1024–65535 的整数）"
done

if command -v lsof >/dev/null 2>&1 && [[ -z "$("${COMPOSE[@]}" ps -q 2>/dev/null)" ]]; then
  for port in "$WEB_PORT" "$API_PORT" "$POSTGRES_PORT"; do
    if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
      warn "端口 $port 已被占用，若下一步启动失败请先排查：lsof -nP -iTCP:$port -sTCP:LISTEN"
    fi
  done
fi

# ── 4. 启动 ────────────────────────────────────────────────────────────
# 必须带 --build：三个服务都把源码打进镜像，web 还把 NEXT_PUBLIC_API_BASE
# 作为构建参数烧进产物。不重建的话代码改动不会生效。
if (( DO_BUILD )); then
  info "构建并启动服务（首次构建需要下载依赖，可能几分钟）"
  "${COMPOSE[@]}" up -d --build
else
  info "启动服务（跳过构建）"
  "${COMPOSE[@]}" up -d
fi

# ── 5. 等健康检查 ──────────────────────────────────────────────────────
# server 容器启动命令里带 alembic upgrade head，迁移跑完才会开始监听，
# 所以这里等的既是进程起来也是迁移完成。
info "等待 API 就绪（最多 ${HEALTH_TIMEOUT_S}s，含数据库迁移）"
deadline=$(( SECONDS + HEALTH_TIMEOUT_S ))
until curl -fsS http://localhost:8000/health >/dev/null 2>&1; do
  if (( SECONDS >= deadline )); then
    printf '\n'
    printf '%s  ✗%s API 在 %ss 内没有就绪。最近日志：\n\n' "$R" "$N" "$HEALTH_TIMEOUT_S" >&2
    "${COMPOSE[@]}" logs --tail 40 server >&2
    exit 1
  fi
  printf '%s.%s' "$D" "$N"
  sleep 2
done
printf '\n'
ok "API 就绪"

if curl -fsS -o /dev/null "http://localhost:${WEB_PORT}" 2>/dev/null; then
  ok "Web 就绪"
else
  warn "Web 还在启动，稍等几秒刷新即可"
fi

# ── 6. 演示数据 ────────────────────────────────────────────────────────
if (( DO_SEED )); then
  info "灌入演示数据（幂等，重复执行安全）"
  "${COMPOSE[@]}" exec -T server python scripts/seed_demo.py
fi

# ── 7. 汇总 ────────────────────────────────────────────────────────────
cat <<EOF

  ${G}启动完成${N}

    Web        ${B}http://localhost:${WEB_PORT}${N}
    API 文档   ${B}http://localhost:${API_PORT}/docs${N}
    PostgreSQL localhost:${POSTGRES_PORT}
    浏览器 API ${D}${NEXT_PUBLIC_API_BASE}${N}

    演示账号   demo@fitmind.cn / demo123456

  ${D}待办与长期记忆由 worker 异步抽取，聊完等 2-5 秒再刷新右侧面板。
  查看日志：${COMPOSE[*]} logs -f worker
  停止服务：${COMPOSE[*]} down${N}

EOF
