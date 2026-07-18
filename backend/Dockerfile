# syntax=docker/dockerfile:1.4
# V1.1 交易平台 - 后端 Dockerfile
# 多阶段构建：builder 安装依赖到虚拟环境，runtime 仅复制必要产物
# 主要依赖：fastapi, uvicorn, sqlalchemy, asyncpg, psycopg3, redis, pytdx, pandas, numpy
# 用法: docker build -t trading-backend ./backend
#
# [CHANGE-20260718-003 构建性能] 版本 ARG 移到依赖层之后 + BuildKit pip cache mount
# + 基础镜像 digest 固定 + 移除 PIP_NO_CACHE_DIR（改用 cache mount 持久化下载缓存）。
# 依赖层仅由 pyproject.toml 变化触发；版本 ARG 仅让源码/build 与最终 metadata 层失效。

# ===== Stage 1: builder - 安装 Python 依赖 =====
# 基础镜像 digest 固定（CHANGE-20260718-003）；维护时显式 --pull 更新 digest
FROM python:3.11-slim@sha256:e031123e3d85762b141ad1cbc56452ba69c6e722ebf2f042cc0dc86c47c0d8b3 AS builder

WORKDIR /app

# 配置清华 PyPI 镜像源，加速国内依赖下载
ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
# 注：PIP_NO_CACHE_DIR 已移除——改用 BuildKit cache mount 持久化 pip 下载缓存（见下方 --mount）。

# 依赖锁标识（pyproject.toml 的 sha256，由 Makefile/deploy 传入）；仅作 LABEL 审计，
# 依赖层缓存仍由 COPY pyproject.toml 触发。未来引入 uv.lock/poetry.lock 后可扩展为 deps 基础镜像 key。
ARG PYPROJECT_LOCK_HASH=unspecified

# 创建虚拟环境，便于整体复制到 runtime 阶段
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# 复制依赖声明并先安装锁定依赖；BuildKit cache mount 持久化 pip 下载缓存，
# 使 pyproject.toml 变化时的重装只需下载增量包（依赖层仅由 pyproject.toml 触发失效）。
COPY pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install .

# 复制应用源码（代码变化只影响本层及之后）
COPY app ./app

# ===== Stage 2: runtime - 精简运行时镜像 =====
# 基础镜像 digest 固定（CHANGE-20260718-003）
FROM python:3.11-slim@sha256:e031123e3d85762b141ad1cbc56452ba69c6e722ebf2f042cc0dc86c47c0d8b3

# 安装 Node.js 运行时（pywencai 的 get_token() 需要 node 执行 hexin-v.bundle.js 计算反爬 token）
# 必须在版本 ARG 之前（CHANGE-20260718-003）：ARG 值变化会使后续所有层缓存失效，
# apt-get 层不依赖版本，放在 ARG 前可跨版本复用。
RUN apt-get update && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 从 builder 复制已安装好依赖的虚拟环境（BuildKit 按 venv 内容哈希缓存，
# pip install 层不变则本层 CACHED，不受源码或版本变化影响）
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# 复制不常变动的配置/迁移/脚本（放在 app 源码之前，减少缓存失效范围）
COPY alembic.ini ./
COPY alembic ./alembic
COPY scripts ./scripts

# 复制应用源码（最常变动，放在版本 ARG 之前：版本变化不应使源码 COPY 层失效）
COPY app ./app

# 版本 ARG 在所有依赖/源码层之后声明（CHANGE-20260718-003）：
# 仅 ENV/LABEL 等 metadata 层受版本变化影响，apt-get/venv/源码 COPY 全部可跨版本缓存。
ARG GIT_SHA=unknown
ARG BUILD_TIME=unknown
# 依赖锁标识在 runtime 阶段重新声明（ARG 不跨阶段），写入 label 供审计
ARG PYPROJECT_LOCK_HASH=unspecified

# 将构建版本写入运行时环境变量，/version 端点读取
ENV GIT_SHA=${GIT_SHA}
ENV BUILD_TIME=${BUILD_TIME}

# 依赖锁标识写入镜像 label，便于审计/排障
LABEL panji.dep-lock-hash=${PYPROJECT_LOCK_HASH} \
      panji.git-sha=${GIT_SHA} \
      panji.build-time=${BUILD_TIME}

EXPOSE 8000

# 启动 uvicorn ASGI 服务
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
