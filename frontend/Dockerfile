# V1.1 交易平台 - 前端 Dockerfile
# 多阶段构建：builder 编译 React 应用，runtime 用 nginx 托管静态资源
# 用法: docker build -t trading-frontend ./frontend

# ===== Stage 1: builder - 编译前端应用 =====
FROM node:20-alpine AS builder

WORKDIR /app

# 配置清华 npm 镜像源，加速国内依赖下载
RUN npm config set registry https://registry.npmmirror.com

# 先复制依赖声明文件，利用 Docker 层缓存
COPY package.json package-lock.json ./

# 安装依赖（CI 模式，严格按 lockfile 安装）
RUN npm ci

# 复制源码并构建生产包
COPY . .
RUN npm run build

# ===== Stage 2: runtime - nginx 托管静态资源 =====
FROM nginx:alpine

# 复制自定义 nginx 配置（API 反向代理 + SPA 路由回退）
COPY nginx.conf /etc/nginx/conf.d/default.conf

# 复制构建产物到 nginx 静态资源目录
COPY --from=builder /app/dist /usr/share/nginx/html

EXPOSE 80
