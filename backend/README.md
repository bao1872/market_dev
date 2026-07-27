# Trading Platform V1.1 Backend

多用户选股与盘中监控平台后端。

## 技术栈

- FastAPI + SQLAlchemy 2.0 (async) + Alembic
- PostgreSQL 16 (psycopg3 / asyncpg)
- Redis 7
- Pydantic v2 + pydantic-settings

## 快速开始

```bash
# 1. 复制环境变量模板并填写共享 PostgreSQL / Redis 连接（本地开发不启动 Docker 数据服务）
cp backend/.env.example backend/.env

# 2. 安装依赖（清华源）
cd backend
pip install -e . -i https://pypi.tuna.tsinghua.edu.cn/simple

# 3. 启动后端开发服务器（原生 Python 进程，不依赖 Docker）
make backend

# 4. 运行测试
make test
```

注意：本地开发不执行 `make up`，不启动本地 PostgreSQL / Redis 容器。

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
