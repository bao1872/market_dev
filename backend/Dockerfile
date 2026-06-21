# V1.1 交易平台 - 后端 Dockerfile
# 多阶段构建：builder 安装依赖到虚拟环境，runtime 仅复制必要产物
# 主要依赖：fastapi, uvicorn, sqlalchemy, asyncpg, psycopg3, redis, pytdx, pandas, numpy
# 用法: docker build -t trading-backend ./backend

# ===== Stage 1: builder - 安装 Python 依赖 =====
FROM python:3.11-slim AS builder

WORKDIR /app

# 配置清华 PyPI 镜像源，加速国内依赖下载
ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ENV PIP_NO_CACHE_DIR=1

# 创建虚拟环境，便于整体复制到 runtime 阶段
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# 复制依赖声明与应用源码（setuptools 需解析 app 包结构才能安装）
COPY pyproject.toml ./
COPY app ./app

# 安装项目及全部依赖到虚拟环境
RUN pip install .

# ===== Stage 2: runtime - 精简运行时镜像 =====
FROM python:3.11-slim

WORKDIR /app

# 从 builder 复制已安装好依赖的虚拟环境
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# 复制应用源码
COPY app ./app

EXPOSE 8000

# 启动 uvicorn ASGI 服务
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
