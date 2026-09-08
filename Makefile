# FitMind AI —— 常用命令入口
#
# 本机已验证的是独立版 docker-compose。若你装的是新版 Compose 插件，
# 用 `make COMPOSE="docker compose" up` 覆盖即可。端口由根目录 .env 配置。
COMPOSE ?= docker-compose
PY      := backend/.venv/bin/python
PIP     := backend/.venv/bin/pip
ALEMBIC := backend/.venv/bin/alembic

.DEFAULT_GOAL := help
.PHONY: help up down restart rebuild logs logs-worker ps seed db \
        test eval cov check gen migrate venv dev-api dev-worker dev-web clean

help:  ## 显示可用命令
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk -F':.*?## ' '{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# ── Docker（推荐的运行方式）─────────────────────────────────────────────

up:  ## 构建并启动全部服务（改了代码用这个）
	@test -f .env || { echo "缺少 .env，先执行：cp .env.example .env 并填写 LLM_* 三项"; exit 1; }
	$(COMPOSE) up -d --build
	@echo ""
	@echo "  已启动。访问地址以 .env 中 WEB_PORT / API_PORT 为准。"
	@echo ""
	@echo "  首次启动请执行 make seed 灌入演示数据"

down:  ## 停止服务，保留数据卷
	$(COMPOSE) down

restart:  ## 重启服务，不重建镜像
	$(COMPOSE) restart

rebuild:  ## 强制无缓存重建镜像
	$(COMPOSE) build --no-cache
	$(COMPOSE) up -d

logs:  ## 跟踪全部服务日志
	$(COMPOSE) logs -f

logs-worker:  ## 只看 worker 日志（排查待办/记忆抽取用）
	$(COMPOSE) logs -f worker

ps:  ## 查看容器状态
	$(COMPOSE) ps

seed:  ## 灌入演示数据（207 条食物库 + demo 账号）
	$(COMPOSE) exec server python scripts/seed_demo.py
	@echo "演示账号 demo@fitmind.cn / demo123456"

db:  ## 只启动 PostgreSQL（本机开发时用）
	$(COMPOSE) up -d postgres

# ── 本机开发 ────────────────────────────────────────────────────────────

venv:  ## 创建后端虚拟环境并安装依赖
	python3.12 -m venv backend/.venv
	$(PIP) install -r backend/requirements.txt

migrate:  ## 把本机开发库推到最新迁移版本
	cd backend && .venv/bin/alembic upgrade head

dev-api:  ## 本机启动 API（热重载）
	cd backend && .venv/bin/uvicorn app.main:app --reload

dev-worker:  ## 本机启动抽取 worker（不起它待办不会出现）
	cd backend && .venv/bin/python -m app.core.jobs.worker

dev-web:  ## 本机启动前端
	cd web && npm run dev

gen:  ## 从运行中的 API 重新生成前端类型（勿手改 api.gen.ts）
	cd web && npm run gen

# ── 测试与检查 ──────────────────────────────────────────────────────────

test:  ## 后端全量测试
	cd backend && .venv/bin/python -m pytest -q

eval:  ## 对运行中的服务执行 Agent 核心场景评测
	backend/.venv/bin/python evals/runner.py

cov:  ## 后端测试 + 覆盖率
	cd backend && .venv/bin/python -m pytest --cov=app --cov-report=term -q

check:  ## 提交前全量检查：后端覆盖率 + 前端类型检查与构建
	cd backend && .venv/bin/python -m pytest --cov=app --cov-report=term -q
	cd web && npx tsc --noEmit
	cd web && npm run build

clean:  ## 清理构建产物与缓存
	rm -rf web/.next web/tsconfig.tsbuildinfo
	find backend -type d -name __pycache__ -prune -exec rm -rf {} +
	find backend -type d -name .pytest_cache -prune -exec rm -rf {} +
