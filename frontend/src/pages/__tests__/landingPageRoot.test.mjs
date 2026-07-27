// [LandingPage] - 描述: 根路径 / 跳转契约与门户可访问性测试
// 用法：node --test src/pages/__tests__/landingPageRoot.test.mjs
//
// [Phase 5B-1] 覆盖：
// 1. LandingPage 源码禁止出现 window.location.replace('/')（自跳转当前 URL = 无限刷新）
// 2. LandingPage 源码必须包含 /portal/index.html 跳转目标
// 3. LandingPage 源码必须包含 import.meta.env.DEV 守卫（生产不跳转）
// 4. 生产兜底必须显示可点击入口，禁止无限刷新
// 5. portal/index.html 静态文件存在于 frontend/public/portal/
// 6. App.tsx 路由配置 / 仍指向 LandingPage（公开路由）
//
// 修复历史：
//   - Phase 5B-0：发现 / 无限刷新，但仅作为"本地开发限制"绕过，未修复
//   - Phase 5B-1：明确为代码 Bug，dev 模式跳转 /portal/index.html，prod 渲染稳定兜底
//
// 注：使用 .mjs 扩展名以支持 Node 20.10 原生运行（无 --experimental-strip-types）

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync, existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
const LANDING_PAGE_PATH = join(__dirname, '..', 'LandingPage', 'LandingPage.tsx')
const APP_TSX_PATH = join(__dirname, '..', '..', 'App.tsx')
const PORTAL_HTML_PATH = join(__dirname, '..', '..', '..', 'public', 'portal', 'index.html')

function readSource(p) {
  return readFileSync(p, 'utf-8')
}

// ===== 1. 禁止自跳转到当前 URL =====
test('LandingPage 禁止 window.location.replace("/") 自跳转（无限刷新根因）', () => {
  const src = readSource(LANDING_PAGE_PATH)
  // 仅检查实际代码行（排除注释行），避免误报历史修复说明
  const codeLines = src
    .split('\n')
    .filter((line) => !line.trim().startsWith('//'))
    .join('\n')
  assert.ok(
    !codeLines.includes("window.location.replace('/')") &&
      !codeLines.includes('window.location.replace("/")'),
    'LandingPage 实际代码不得使用 window.location.replace("/") 自跳转（会触发无限刷新）',
  )
})

// ===== 2. 必须跳转到 /portal/index.html =====
test('LandingPage 开发环境必须跳转到 /portal/index.html', () => {
  const src = readSource(LANDING_PAGE_PATH)
  assert.ok(
    src.includes('/portal/index.html'),
    'LandingPage 必须包含 /portal/index.html 跳转目标',
  )
})

// ===== 3. 必须使用 import.meta.env.DEV 守卫 =====
test('LandingPage 必须使用 import.meta.env.DEV 守卫跳转（生产不跳转）', () => {
  const src = readSource(LANDING_PAGE_PATH)
  assert.ok(
    src.includes('import.meta.env.DEV'),
    'LandingPage 必须使用 import.meta.env.DEV 守卫跳转逻辑（生产环境不跳转）',
  )
})

// ===== 4. 生产兜底必须显示可点击入口 =====
test('LandingPage 生产兜底必须渲染可点击入口（不得仅显示空白）', () => {
  const src = readSource(LANDING_PAGE_PATH)
  // 必须包含至少一个 <a href 链接作为兜底入口
  assert.ok(
    src.includes('<a') && src.includes('href'),
    'LandingPage 生产兜底必须渲染至少一个 <a href> 入口链接',
  )
  // 必须包含 portal 链接（生产兜底页应能进入门户）
  assert.ok(
    src.includes('href={PORTAL_PATH}') || src.includes('href="/portal/index.html"'),
    'LandingPage 生产兜底必须包含 portal 入口链接',
  )
})

// ===== 5. portal 静态文件存在 =====
test('frontend/public/portal/index.html 静态门户文件存在', () => {
  assert.ok(
    existsSync(PORTAL_HTML_PATH),
    'frontend/public/portal/index.html 必须存在（Vite 静态服务 /portal/ 入口）',
  )
})

// ===== 6. portal/index.html 关键资源可访问性 =====
test('portal/index.html 包含 base href 和品牌标记（关键资源可访问）', () => {
  const html = readSource(PORTAL_HTML_PATH)
  // base href 确保相对资源路径正确
  assert.ok(html.includes('<base href="/portal/">'), 'portal/index.html 必须含 <base href="/portal/">')
  // 品牌标记
  assert.ok(html.includes('盘迹'), 'portal/index.html 必须含品牌标记 "盘迹"')
})

// ===== 7. App.tsx 路由配置 / 仍指向 LandingPage（公开路由） =====
test('App.tsx 公开路由 / 仍配置为 LandingPage（lazy 加载）', () => {
  const src = readSource(APP_TSX_PATH)
  assert.ok(
    src.includes("path: '/'") && src.includes('LandingPage'),
    'App.tsx 必须保留 / → LandingPage 公开路由配置',
  )
  // 确保没有把 / 改成 Navigate 重定向（保留 LandingPage 组件作为公开入口）
  assert.ok(
    src.includes("import('./pages/LandingPage')"),
    'App.tsx 必须保留 LandingPage 的 lazy 加载',
  )
})
