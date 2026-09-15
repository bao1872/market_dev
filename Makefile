# V1.1 交易平台 - 开发命令
# 用法: make <target>

.PHONY: dev backend frontend tunnel tunnel-status tunnel-stop migrate-new check test-pure-full

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

# 创建新迁移（用法: make migrate-new MSG="add xxx table"）
# 仅生成 revision 文件，不执行任何 DB 变更。
# 真实 migration 执行只走远程受控流程（scripts/ops/panji-test-deploy / panji-verify）。
migrate-new:
	cd backend && alembic revision --autogenerate -m "$(MSG)"

# 日常提交前验证入口
#   = T0 final changed-files (t0_gate.py) + T1 modified-scope unit (t1_gate.py)
#
# 默认：提交前 worktree 检查
#   make check
#     T0 --base HEAD --worktree
#     T1 --base HEAD --worktree
# 精确 committed range：
#   make check BASE=<sha> HEAD=<sha>
#     T0 --base BASE --head HEAD
#     T1 --base BASE --head HEAD
# 显式 targeted selector（不 eval，逗号分隔）：
#   make check BACKEND_T1="tests/test_foo.py::test_case,tests/test_bar.py"
#   make check FRONTEND_T1="src/features/foo/__tests__/foo.test.ts"
#
# 前置：backend 虚拟环境已激活（ruff/pytest 在 PATH）；
#       pure-unit 模式不连任何数据库，但部分单元测试会用到 Redis，
#       默认指向 redis://localhost:6379/15，可用 REDIS_URL 覆盖。
# 全量纯单元测试（6000+）不是日常路径，见 test-pure-full（T6）。
check:
	@ROOT=$$(git rev-parse --show-toplevel); \
	 BASE_ARG="$(BASE)"; HEAD_ARG="$(HEAD)"; \
	 if [ -z "$$BASE_ARG" ]; then \
	   T0_ARGS="--base HEAD --worktree"; \
	   T1_ARGS="--base HEAD --worktree"; \
	 else \
	   T0_ARGS="--base $$BASE_ARG"; T1_ARGS="--base $$BASE_ARG"; \
	   if [ -n "$$HEAD_ARG" ]; then T0_ARGS="$$T0_ARGS --head $$HEAD_ARG"; T1_ARGS="$$T1_ARGS --head $$HEAD_ARG"; fi; \
	 fi; \
	 if [ -n "$(BACKEND_T1)" ]; then T1_ARGS="$$T1_ARGS --backend-tests $(BACKEND_T1)"; fi; \
	 if [ -n "$(FRONTEND_T1)" ]; then T1_ARGS="$$T1_ARGS --frontend-tests $(FRONTEND_T1)"; fi; \
	 echo "==[1/2] T0 final changed-files (t0_gate.py) =="; \
	 python3 "$$ROOT/scripts/quality/t0_gate.py" $$T0_ARGS || exit 1; \
	 echo "==[2/2] T1 modified-scope unit (t1_gate.py) =="; \
	 python3 "$$ROOT/scripts/quality/t1_gate.py" $$T1_ARGS

# T6 Full PURE_UNIT（显式入口，非日常）
# 6000+ 纯单元测试；与 make check 的 modified-scope 分层不同。
test-pure-full:
	cd backend && PURE_UNIT_TEST=1 APP_ENV=test REDIS_URL=redis://localhost:6379/15 CAPTURE_STATIC_DIR=/tmp/panji-ci-captures \
	  pytest -m "not postgres and not external_data" --tb=short -q
