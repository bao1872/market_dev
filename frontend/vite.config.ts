import {
  defineConfig,
  type Plugin,
} from 'vite'
import react from '@vitejs/plugin-react'
import { fileURLToPath, URL } from 'node:url'

// [V1.6.1] Marketing media dev alias：
//   /marketing-assets/media/* → /marketing-media/* → public/marketing-media/*
// Production: build:marketing-site 用 cp -R public/marketing-media/，URL 不变
// Dev: rewrite 让 vite dev server 直接 serve public/marketing-media/
const marketingMediaDevAlias: Plugin = {
  name: 'marketing-media-dev-alias',

  configureServer(server) {
    server.middlewares.use((req, _res, next) => {
      if (req.url?.startsWith('/marketing-assets/media/')) {
        req.url = req.url.replace(
          '/marketing-assets/media/',
          '/marketing-media/',
        )
      }
      next()
    })
  },
}

export default defineConfig({
  plugins: [react(), marketingMediaDevAlias],
  define: {
    'import.meta.env.VITE_GIT_SHA': JSON.stringify(process.env.GIT_SHA || 'dev'),
    'import.meta.env.VITE_BUILD_TIME': JSON.stringify(process.env.BUILD_TIME || new Date().toISOString()),
  },
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  css: {
    modules: {
      localsConvention: 'camelCaseOnly',
      generateScopedName: '[name]__[local]__[hash:base64:5]',
    },
    preprocessorOptions: {
      scss: {
        api: 'modern-compiler',
      },
    },
  },
  server: {
    host: '0.0.0.0',
    port: 8008,
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
