# syntax=docker/dockerfile:1.4
# V1.1 交易平台 - 前端 Dockerfile
# 多阶段构建：builder 编译 React 应用，runtime 用 nginx 托管静态资源
# 用法: docker build -t trading-frontend ./frontend
#
# [CHANGE-20260718-003 构建性能] 版本 ARG 移到依赖层之后 + BuildKit npm cache mount
# + 基础镜像 digest 固定。依赖层仅由 package.json/package-lock.json 变化触发。

# ===== Stage 1: builder - 编译前端应用 =====
# 基础镜像 digest 固定（CHANGE-20260718-003）；维护时显式 --pull 更新 digest
FROM node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293 AS builder

WORKDIR /app

# 配置清华 npm 镜像源，加速国内依赖下载
RUN npm config set registry https://registry.npmmirror.com

# 先复制依赖声明文件，利用 Docker 层缓存
COPY package.json package-lock.json ./

# 安装依赖（CI 模式，严格按 lockfile 安装）；BuildKit cache mount 持久化 npm 下载缓存，
# 使 lockfile 变化时的重装只需下载增量包（依赖层仅由 package*.json 触发失效）。
RUN --mount=type=cache,target=/root/.npm \
    npm ci

# 复制源码（代码变化只影响本层及之后）
COPY . .

# 版本信息通过 ARG 传入，Vite define 读取（CHANGE-20260718-003：版本 ARG 移到 npm ci 之后，
# 仅让源码/build 层失效，不影响 npm ci 依赖层缓存）
ARG GIT_SHA=dev
ARG BUILD_TIME=unknown
ARG VITE_GIT_SHA=dev
ARG VITE_BUILD_TIME=unknown
ENV GIT_SHA=${GIT_SHA}
ENV BUILD_TIME=${BUILD_TIME}
ENV VITE_GIT_SHA=${VITE_GIT_SHA}
ENV VITE_BUILD_TIME=${VITE_BUILD_TIME}
RUN npm run build

# ===== Stage 2: runtime - nginx 托管静态资源 =====
# 基础镜像 digest 固定（CHANGE-20260718-003）
FROM nginx:alpine@sha256:54f2a904c251d5a34adf545a72d32515a15e08418dae0266e23be2e18c66fefa

# 复制自定义 nginx 配置（API 反向代理 + SPA 路由回退）
COPY nginx.conf /etc/nginx/conf.d/default.conf

# 复制构建产物到 nginx 静态资源目录
COPY --from=builder /app/dist /usr/share