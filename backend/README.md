# Trading Platform V1.1 Backend

多用户选股与盘中监控平台后端。

## 技术栈

- FastAPI + SQLAlchemy 2.0 (async) + Alembic
- PostgreSQL 16 (psycopg3 / asyncpg)
- Redis 7
- Pydantic v2 + pydantic-settings

## 本地 Python 环境（canonical venv）

Canonical backend 虚拟环境：

    backend/.venv

Python 要求：

    Python >= 3.11

从仓库根目录激活：

    source backend/.venv/bin/activate

从 backend/ 目录激活：

    source .venv/bin/activate

验证：

    which python
    python --version

解释器必须解析到：

    <repo>/backend/.venv/bin/python

明确区分：

- `.venv` = Python 虚拟环境（运行时依赖隔离）
- `.env` = 配置/环境变量文件（backend/.env / backend/.env.example）
- 二者**无关**，不要混淆
- 不要在仓库内再创建其他本地环境，例如：
  - repo/.venv
  - backend/venv
  - backend/env
  - .venv2

首次搭建（在 backend/ 下）：

    cd backend
    python3 -m venv .venv
    source .venv/bin/activate
    python -m pip install -e .

注意：不要硬编码 Python 3.12；仓库契约为 >= 3.11。

## 快速开始

```bash
# 1. 复制环境变量模板并填写共享 PostgreSQL / Redis 连接（本地开发不启动 Docker 数据服务）
cp backend/.env.example backend/.env

# 2. 创建并激活 canonical 虚拟环境，安装依赖（清华源）
cd backend
python3 -m venv .venv          # 首次创建；之后仅需 source .venv/bin/activate
source .venv/bin/activate
pip install -e . -i https://pypi.tuna.tsinghua.edu.cn/simple
cd ..                         # 返回 repo root；后续 make 命令依赖根目录 Makefile

# 3. 启动 SSH 隧道（PostgreSQL -> 127.0.0.1:15432，Redis -> 127.0.0.1:16379）
# 前提：~/.ssh/config 已配置 Host panji-prod（HostName 43.136.118.82）
make tunnel

# 4. 启动后端开发服务器（原生 Python 进程，不依赖 Docker）
make backend

# 5. 运行测试
make check
```

注意：
- 本地开发不执行 `make up`，不启动本地 PostgreSQL / Redis 容器。
- 本地开发 Redis 必须使用独立逻辑 DB（如 `/15`），`backend/app/config.py` 会在启动时校验 DB 0 并拒绝启动。
- 本地 development 环境启动时跳过共享数据库维护写入（策略种子、日历刷新、僵尸任务恢复）。

## 目录结构

```
backend/
├── app/
│   ├── main.py          # FastAPI 入口
│   ├── config.py        # Pydantic Settings
│   ├── db.py            # 异步 SQLAlchemy engine + session
│   ├── api/             # API 路由
│   ├── core/            # 安全、依赖注入
│   ├── models/          # ORM 模型
│   └── schemas/         # Pydantic schemas
├── alembic/             # 数据库迁移
│   ├── env.py
│   └── versions/        # 迁移文件
├── tests/               # 测试
├── pyproject.toml
├── alembic.ini
└── .env.example
```

## 配置

复制 `.env.example` 为 `.env`，按需修改。仅启动级配置放环境变量；业务密钥进入加密配置中心。
