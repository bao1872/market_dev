# 代码库模块 Map

核验状态：已基于本地原生启动核验（第一阶段）；Phase 5A 补充 after-close readiness 权威入口
最后核验日期：2026-07-27
核验提交：72dcd6c074212c0935090ce86acc7e48ba619dcb（Phase 4）；Phase 5A 修复见 `docs/changes/2026/CHANGE-20260727-002-after-close-daily-readiness.md`
事实所有权：仓库目录、模块职责、依赖边界和公共入口

## 1. 顶层目录

| 目录 | 职责 | 主要入口 | 不应承担 |
|---|---|---|---|
| 前端 `frontend/` | React + Vite 单页应用；行情、自选、策略、监控、管理后台 UI | `frontend/src/main.tsx`；`frontend/vite.config.ts` | 后端权威业务计算 |
| 后端 `backend/` | FastAPI + SQLAlchemy 异步服务；业务 API、Worker、Scheduler、数据同步 | `backend/app/main.py:app`；`backend/app/worker.py` | 前端展示状态 |
| 任务/脚本 `backend/app/services/`、`backend/scripts/` | 盘后编排、Worker 实现、一次性脚本 | `backend/app/services/after_close_orchestrator.py`；`backend/scripts/*.py` | 重复领域算法 |
| 测试 `backend/tests/`、前端 contract tests | 后端 pytest 集成测试、前端 Node.js contract tests | `backend/tests/`；`frontend/scripts/contract-tests/` | 生产运行状态 |
| `docs/` | PRD、Maps、Changes、Runbooks | - | 代码事实源 |
| `rules/` | 代码规则与约束 | - | 产品需求 |
| `.github/workflows/` | GitHub Actions CI / 部署 workflow | `ci.yml`、`deploy-production.yml` | 不应包含应用 secret 明文 |
| `scripts/deploy/` | 生产环境自动部署脚本 | `scripts/deploy/panji-deploy.sh` | 不应在本地开发流程中被调用 |

## 2. 模块责任表

| 模块 | 路径 | 职责 | 调用方 | 依赖 | 状态 |
|---|---|---|---|---|---|
| 配置 | `backend/app/config.py` | 统一启动级配置加载、硬校验、脱敏日志；development 缺失 DATABASE_URL/REDIS_URL 或 Redis DB 0 时 fail-closed | 全局 `get_settings()` | 环境变量、`CONFIG_FILE`、`config.local.py` / `config.test.py`、`.env` | 已核验 |
| 数据访问 | `backend/app/db.py` | 异步 SQLAlchemy engine、session factory、FastAPI `get_db` | Service / API / Worker | `DATABASE_URL` | 已核验 |
| Redis Client | `redis.asyncio.from_url(settings.redis_url)` | 队列、锁、缓存 | Worker / Service | `REDIS_URL` | 已核验 |
| SSH 隧道 | `scripts/local/ssh-tunnel.sh` | 本地开发连接远程 PostgreSQL / Redis 的可重复隧道 | 开发者手动调用 | `~/.ssh/config` Host 别名 | 已核验 |
| 生产部署脚本 | `scripts/deploy/panji-deploy.sh` | 接收精确 SHA，验证 origin/main，按变更范围 Live Mount / 重建镜像部署，记录 previous/last-good，失败回滚 | GitHub Actions / 管理员手动 SSH 调用 | git、docker compose、flock、rsync、curl、npm/node | 代码已准备 / 未启用 |
| 生产部署 workflow | `.github/workflows/deploy-production.yml` | CI 通过后通过 SSH 调用远程部署脚本；支持手动 workflow_dispatch | GitHub Actions | secrets.PANJI_PROD_HOST/USER/SSH_KEY | 代码已准备 / 未启用 |
| 指标计算 | `backend/app/services/`、`backend/app/strategy_assets/algorithms/` | 纯计算与业务编排 | Worker / Service / API | 行情数据、数据库 | 未深入核验 |
| 发布 | `backend/app/services/after_close_pipeline_service.py` | 正式 run 切换 | Orchestrator | DB | 未核验 |
| 权限 | `backend/app/core/`、JWT / 依赖注入 | 后端授权 | API | 用户数据 | 未深入核验 |

## 3. 依赖方向

已确认：

- API / Worker / Service 统一通过 `get_settings()` 读取配置；
- 数据库访问统一通过 `backend/app/db.py` 的 `AsyncSessionLocal` / `async_engine`；
- Redis 统一通过 `settings.redis_url` 构造客户端，未发现硬编码 `redis://.../0` 的业务代码；
- 前端通过 Vite proxy `/api` → `http://localhost:8000` 访问后端；
- 后端 `app.main:app` 不直接依赖 Worker 进程，Worker 通过 `python -m app.worker` 独立启动。

待核验：

- 是否存在循环依赖；
- 是否存在越层访问；
- 是否存在万能 utils；
- 是否存在重复配置入口；
- 是否存在重复业务逻辑。

## 4. 公共入口

| 能力 | 权威入口 | 违规旁路 |
|---|---|---|
| 配置 | `backend/app/config.py:get_settings()` | 直接 `os.environ.get` 读取启动级配置（运行时配置除外） |
| DB Session | `backend/app/db.py:AsyncSessionLocal`、`get_db()` | 直接新建 engine |
| Redis Client | `redis.asyncio.from_url(get_settings().redis_url)` | 硬编码 Redis URL 或 DB |
| after-close readiness | `backend/app/services/after_close_orchestrator.py:execute_after_close_run` 中 `checking_coverage` 步骤（仅日线覆盖率 >= 0.9；Phase 5A 移除 15m 阻塞） | 在 after-close 链路中重复实现 15m 覆盖率检查或绕过 `execute_after_close_run` 自行检查 readiness；15m intraday 工具 `BarsCoverageService.compute_intraday_coverage` 保留供其他链路使用 |
| 生产部署脚本 | `scripts/deploy/panji-deploy.sh` | 服务器本地其他入口或手动复制 |
| 生产部署 workflow | `.github/workflows/deploy-production.yml` | 其他分支或未经 CI 的触发器 |
| 时间转换 | `backend/app/core/time.py`（待核验） | 未核验 |
| 股票标识 | `backend/app/models/instrument.py`（待核验） | 未核验 |
| 正式结果读取 | `backend/app/services/after_close_pipeline_service.py`（待核验） | 未核验 |
