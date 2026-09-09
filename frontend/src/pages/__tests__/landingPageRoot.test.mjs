// [LandingPage] - 描述: 根路径 / 跳转契约与门户可访问性测试
// 用法：node --test src/pages/__tests__/landingPageRoot.test.mjs
//
// [Phase 5B-1 / CHANGE-20260909] 覆盖：
// 1. LandingPage 源码禁止出现 window.location.replace('/')（自跳转当前 URL = 无限刷新）
// 2. LandingPage dev 模式跳向产品官网入口 /marketing-site/（不再跳已退役的 /portal/index.html）
// 3. LandingPage 源码必须包含 import.meta.env.DEV 守卫（生产不跳转）
// 4. 生产兜底必须显示可点击入口，禁止无限刷新
// 5. 旧「使用说明」portal/index.html 静态文件已彻底删除（frontend/public/portal 不存在）
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
const PORTAL_DIR = join(__dirname, '..', '..', '..', 'public', 'portal')

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

// ===== 2. dev 模式跳向产品官网入口 =====
test('LandingPage 开发环境跳向产品官网入口 /marketing-site/', () => {
  const src = readSource(LANDING_PAGE_PATH)
  assert.ok(
    src.includes('/marketing-site/'),
    'LandingPage 必须有 /marketing-site/ 产品官网入口',
  )
  assert.ok(
    !src.includes('/portal/index.html'),
    'LandingPage 不得再跳转已退役的 /portal/index.html（旧使用说明）',
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
  // 必须包含产品官网入口链接（生产兜底页应能进入产品官网）
  assert.ok(
    src.includes('href={PUBLIC_SITE_PATH}'),
    'LandingPage 生产兜底必须包含产品官网入口 href={PUBLIC_SITE_PATH}',
  )
})

// ===== 5. 旧使用说明 portal 静态文件已删除 =====
test('frontend/public/portal/index.html 静态门户文件已彻底删除', () => {
  assert.ok(
    !existsSync(PORTAL_HTML_PATH),
    'frontend/public/portal/index.html 应已删除（旧「使用说明」portal 彻底退役）',
  )
})

// ===== 6. 旧使用说明 portal 目录不存在 =====
test('frontend/public/portal 目录已不存在（帮助中心不再可访问）', () => {
  assert.ok(
    !existsSync(PORTAL_DIR),
    'frontend/public/portal 目录应已删除（旧帮助中心彻底退役，全量 build 无法再生）',
  )
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
