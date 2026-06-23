import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { fileURLToPath, URL } from 'node:url'

// Vite 配置：React 插件 + 路径别名 @/ -> src/ + SCSS Modules + /api 代理到后端 8000
// 前端开发服务器监听 0.0.0.0:8008（腾讯云外部可访问）
// /api 代理：去掉 /api 前缀后转发到后端 8000（后端路由无 /api 前缀，bars 路由自带 /api/v1）
export default defineConfig({
  plugins: [react()],
  define: {
    'import.meta.env.VITE_GIT_SHA': JSON.stringify(process.env.GIT_SHA || 'dev'),
    'import.meta.env.VITE_BUILD_TIME': JSON.stringify(new Date().toISOString()),
  },
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  css: {
    modules: {
      // SCSS Modules：*.module.scss 自动启用 CSS Modules，类名转为 camelCase
      localsConvention: 'camelCaseOnly',
      generateScopedName: '[name]__[local]__[hash:base64:5]',
    },
    preprocessorOptions: {
      scss: {
        // 使用 Sass modern API，避免 legacy JS API 在 Dart Sass 2.0 被移除
        api: 'modern-compiler',
      },
    },
  },
  server: {
    host: '0.0.0.0',
    port: 8008,
    // 允许从任何来源访问工作区文件（腾讯云外部访问需要）
    fs: {
      strict: false,
    },
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
  // 生产预览服务器（npm run preview）：使用 dist/ 静态文件 + API 代理
  // 适合外部访问，无 dev server 的模块编译/HMR 问题
  preview: {
    host: '0.0.0.0',
    port: 8008,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
})
