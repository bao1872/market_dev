// [CP-18] - 描述: 最小 Playwright E2E 配置（PRD V2.0 Phase 4.1 §8 验证门禁）
//
// 设计原则：
// 1. 轻量目标 E2E — 只覆盖 6 个关键场景，不做全量回归
// 2. Mock API — 通过 page.route() 拦截所有 /api/** 与 /capture/** 请求，返回 fixture
// 3. 不生成大量截图 — 仅失败时保留单张证据（screenshot: only-on-failure）
// 4. 单浏览器 — 仅 chromium（CI 资源约束）
// 5. 不依赖生产数据 — 所有数据来自 e2e/fixtures/*.json
//
// 用法：
//   本地：npm run test:e2e
//   CI：见 .github/workflows/ci.yml frontend-e2e job
//
// 资源约束：
//   - workers=1 串行执行（避免内存峰值）
//   - 超时 30s/测试，全局 180s
//   - 失败重试 0 次（CI 看到真实状态，不掩盖 flake）

import { defineConfig, devices } from '@playwright/test'
import { fileURLToPath } from 'node:url'
import { dirname } from 'node:path'

const PORT = Number(process.env.PLAYWRIGHT_PREVIEW_PORT ?? 4173)
const BASE_URL = `http://127.0.0.1:${PORT}`

// ES module 兼容：__dirname 在 ESM 中不可用，需通过 import.meta.url 推导
const __dirname = dirname(fileURLToPath(import.meta.url))

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  reporter: process.env.CI ? [['github'], ['list']] : 'list',
  timeout: 30_000,
  globalTimeout: 180_000,
  expect: { timeout: 5_000 },

  use: {
    baseURL: BASE_URL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'off',
    actionTimeout: 10_000,
    navigationTimeout: 15_000,
  },

  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],

  webServer: {
    command: `npm run build && npm run preview -- --port ${PORT} --strictPort`,
    url: BASE_URL,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    cwd: __dirname,
  },
})
