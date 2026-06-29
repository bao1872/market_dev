# V1.1 交易平台 - 后端 Dockerfile
# 多阶段构建：builder 安装依赖到虚拟环境，runtime 仅复制必要产物
# 主要依赖：fastapi, uvicorn, sqlalchemy, asyncpg, psycopg3, redis, pytdx, pandas, numpy
# 用法: docker build -t trading-backend ./backend

# ===== Stage 1: builder - 安装 Python 依赖 =====
FROM python:3.11-slim AS builder

# 构建版本注入：GIT_SHA / BUILD_TIME（docker compose build 时传入，缺失则回退 unknown）
ARG GIT_SHA=unknown
ARG BUILD_TIME=unknown

WORKDIR /app

# 配置清华 PyPI 镜像源，加速国内依赖下载
ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ENV PIP_NO_CACHE_DIR=1

# 创建虚拟环境，便于整体复制到 runtime 阶段
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# 复制依赖声明并先安装锁定依赖，使代码变化不使 pip 缓存失效
COPY pyproject.toml ./
RUN pip install .

# 复制应用源码（代码变化只影响本层及之后）
COPY app ./app

# ===== Stage 2: runtime - 精简运行时镜像 =====
FROM python:3.11-slim

# 从 builder 继承构建版本参数
ARG GIT_SHA=unknown
ARG BUILD_TIME=unknown

WORKDIR /app

# 将构建版本写入运行时环境变量，/version 端点读取
ENV GIT_SHA=${GIT_SHA}
ENV BUILD_TIME=${BUILD_TIME}

# 从 builder 复制已安装好依赖的虚拟环境
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# 复制应用源码
COPY app ./app

# [迁移] - 复制 Alembic 配置与迁移脚本，runtime 阶段需执行 alembic upgrade head
COPY alembic.ini ./
COPY alembic ./alembic

EXPOSE 8000

# 启动 uvicorn ASGI 服务
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
