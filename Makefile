# V1.1 交易平台 - 开发命令
# 用法: make <target>

.PHONY: dev backend frontend tunnel tunnel-status tunnel-stop migrate migrate-new test lint check-fast up down docker-build docker-up docker-down worker

# 启动全栈开发环境：原生 Python / Node.js 进程，不依赖 Docker
# 前置条件：已配置 backend/.env 中的 DATABASE_URL 与 REDIS_URL
dev:
	$(MAKE) backend &
	$(MAKE) frontend &

# 启动后端开发服务器（原生 Python 进程，监听 0.0.0.0:8000）
backend:
	cd backend && uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# 启动前端开发服务器
frontend:
	cd frontend && npm run dev

# 启动本地开发 SSH 隧道（PostgreSQL 15432 / Redis 16379）
# 依赖：~/.ssh/config 中已配置 Host panji-prod（HostName 43.136.118.82）
tunnel:
	scripts/local/ssh-tunnel.sh start

# 检查 SSH 隧道状态
tunnel-status:
	scripts/local/ssh-tunnel.sh status

# 停止本地开发 SSH 隧道
tunnel-stop:
	scripts/local/ssh-tunnel.sh stop

# 执行数据库迁移到最新版本
migrate:
	cd backend && alembic upgrade head

# 创建新迁移（用法: make migrate-new MSG="add xxx table"）
migrate-new:
	cd backend && alembic revision --autogenerate -m "$(MSG)"

# 运行后端测试
test:
	cd backend && pytest

# 快速本地验证入口（Task 002）：复用现有 CI 已验证的纯单元测试分层。
# 设计原则：不改变任何业务语义 / 测试 / schema / runtime；不引入第二套测试分类；
# 复用 conftest 的 PURE_UNIT_TEST 机制（sentinel DB，禁止连正式 PG）；
# 排除 postgres（真实 PG 集成）与 external_data（外部数据源）；禁止部署；fail-closed。
#
# 等价于 CI 的 "Backend Unit Tests" job（已验证命令）：
#   PURE_UNIT_TEST=1 pytest -m "not postgres and not external_data"
# 另含一层 ruff 静态检查（T0）。速度优先，不放 mypy 全量（慢）；
# 需要类型检查时用 `make lint`（或 CI 的 mypy-new-files 增量）。
#
# 前置：backend 虚拟环境已激活（ruff/pytest 在 PATH）；
#       pure-unit 模式不连任何数据库，但部分单元测试会用到 Redis，
#       默认指向 redis://localhost:6379/15，可用 REDIS_URL 覆盖。
check-fast:
	@ROOT=$$(git rev-parse --show-toplevel) ; \
	  CHANGED=$$( { git -C "$$ROOT" diff --name-only origin/dev HEAD -- backend 2>/dev/null; git -C "$$ROOT" diff --name-only HEAD -- backend; git -C "$$ROOT" diff --name-only --cached -- backend; git -C "$$ROOT" ls-files --others --exclude-standard -- backend; } | sed 's|^backend/||' | grep '\.py$$' | sort -u ) ; \
	  cd "$$ROOT/backend" && \
	  echo "==[1/2] ruff on changed backend files (T0, mirrors CI ruff-new-files) ==" && \
	  if [ -n "$$CHANGED" ]; then echo "lint targets:"; echo "$$CHANGED" | sed 's/^/  /'; ruff check $$CHANGED; RUFF_RC=$$?; else echo "  (no changed backend python files; ruff skipped)"; RUFF_RC=0; fi ; \
	  echo "==[2/2] pytest pure-unit (no postgres, no external_data) ==" && \
	  PURE_UNIT_TEST=1 APP_ENV=test REDIS_URL=redis://localhost:6379/15 CAPTURE_STATIC_DIR=/tmp/panji-ci-captures \
	    pytest -m "not postgres and not external_data" --tb=short -q ; PY_RC=$$? ; \
	  echo "==================== check-fast summary ====================" ; \
	  echo "ruff: $$RUFF_RC   pytest: $$PY_RC" ; \
	  if [ $$RUFF_RC -ne 0 ] || [ $$PY_RC -ne 0 ]; then echo "CHECK-FAST: FAIL"; exit 1; fi ; \
	  echo "CHECK-FAST: PASS"

# 代码检查（ruff + mypy）
lint:
	cd backend && ruff check . && mypy app

# [废弃] 本地不再通过 Docker Compose 启动 PostgreSQL / Redis 服务。
# 本地开发直接连接已确认的共享 PostgreSQL 与 Redis 实例，并通过 REDIS_URL 中的逻辑 DB 隔离运行状态。
# 如需调试本地 Redis 容器（已废弃），可手动执行：docker-compose up -d redis
up:
	@echo "警告：本地开发已不使用 Docker Compose 启动服务。请直接运行 make backend / make frontend。"

# [废弃] 停止本地 PostgreSQL + Redis 容器
down:
	@echo "警告：本地开发已不使用 Docker Compose 启动服务。"

# ===== Docker 生产环境命令 =====

# 构建生产环境镜像（自动注入 GIT_SHA / BUILD_TIME / PYPROJECT_LOCK_HASH）
# [CHANGE-20260718-003] 启用 BuildKit（syntax directive 已在 Dockerfile 声明，此处显式设置环境变量
# 确保旧版 docker 也能识别）；PYPROJECT_LOCK_HASH 由 backend/pyproject.toml sha256 计算，
# 写入镜像 LABEL 供审计；依赖层缓存仍由 COPY pyproject.toml 触发失效。
docker-build:
	DOCKER_BUILDKIT=1 \
	GIT_SHA=$$(git rev-parse --short HEAD) \
	BUILD_TIME=$$(date -u +"%Y-%m-%dT%H:%M:%SZ") \
	PYPROJECT_LOCK_HASH=$$(sha256sum backend/pyproject.toml | cut -d' ' -f1) \
	docker compose -f docker-compose.prod.yml build

# 启动生产环境（后台）
docker-up:
	docker compose -f docker-compose.prod.yml up -d

# 停止生产环境
docker-down:
	docker compose -f docker-compose.prod.yml down

# 本地运行 Worker（需先激活虚拟环境）
worker:
	cd backend && source .venv/bin/activate && python -m app.worker
