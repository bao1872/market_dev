# V1.1 交易平台 - 开发命令
# 用法: make <target>

.PHONY: dev backend frontend migrate migrate-new test lint up down docker-build docker-up docker-down worker

# 启动全栈开发环境：docker-compose + 后端 + 前端（后台）
dev:
	$(MAKE) up
	$(MAKE) backend &
	$(MAKE) frontend &

# 启动后端开发服务器
backend:
	cd backend && uvicorn app.main:app --reload --port 8000

# 启动前端开发服务器
frontend:
	cd frontend && npm run dev

# 执行数据库迁移到最新版本
migrate:
	cd backend && alembic upgrade head

# 创建新迁移（用法: make migrate-new MSG="add xxx table"）
migrate-new:
	cd backend && alembic revision --autogenerate -m "$(MSG)"

# 运行后端测试
test:
	cd backend && pytest

# 代码检查（ruff + mypy）
lint:
	cd backend && ruff check . && mypy app

# 启动 PostgreSQL + Redis
up:
	docker-compose up -d

# 停止 PostgreSQL + Redis
down:
	docker-compose down

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
