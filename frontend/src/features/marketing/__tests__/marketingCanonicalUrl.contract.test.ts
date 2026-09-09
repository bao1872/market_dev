// Marketing Canonical URL 收口契约测试
//
// 目的：锁定「Preview → 正式 Public Site」架构收口，防止以下回归：
//   - 构建脚本仍硬编码 /marketing-preview/ 作为门户 base / artifact target；
//   - App.tsx 仍把 /marketing-preview 注册为第二份 MarketingPage 入口；
//   - 正式 Marketing Site HTML 仍带 noindex 或引用 /marketing-preview/；
//   - 部署器仍把 artifact 写到 /marketing-preview/ 或主 SPA index.html。
//
// 全部读取源码（确定性、无需构建产物），由 `npm run test:contract` 显式枚举执行。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const __dirname = dirname(fileURLToPath(import.meta.url))
const ROOT = resolve(__dirname, '../../../../../') // repo root (market_dev/)
const read = (rel: string) => readFileSync(resolve(ROOT, rel), 'utf8')

const pkg = JSON.parse(read('frontend/package.json'))
const appTsx = read('frontend/src/App.tsx')
const siteHtml = read('frontend/marketing-site/index.html')
const deployScript = read('scripts/ops/panji-marketing-site-deploy')

test('A. package.json 不得再含 build:marketing-preview', () => {
  assert.ok(
    !('build:marketing-preview' in (pkg.scripts ?? {})),
    'package.json 仍存在 build:marketing-preview，门户收口未完成',
  )
})

test('B. build:marketing-site 必须存在且 base 为 /marketing-assets/（非 /marketing-preview/）', () => {
  const script = pkg.scripts?.['build:marketing-site']
  assert.ok(typeof script === 'string', 'package.json 缺少 build:marketing-site')
  assert.ok(
    script.includes('--base /marketing-assets/'),
    'build:marketing-site 必须含 --base /marketing-assets/',
  )
  assert.ok(
    !script.includes('--base /marketing-preview/'),
    'build:marketing-site 不得再引用 /marketing-preview/ 作为 base',
  )
})

test('C. App.tsx 不得再注册 MarketingPage 作为 /marketing-preview 入口', () => {
  assert.ok(
    !appTsx.includes('MarketingPage'),
    'App.tsx 仍引用 MarketingPage；/marketing-preview 第二门户未移除',
  )
  assert.ok(
    appTsx.includes('LegacyMarketingPreviewRedirect'),
    'App.tsx 缺少 LegacyMarketingPreviewRedirect 兼容跳转',
  )
  assert.ok(
    appTsx.includes("window.location.replace('/')"),
    'LegacyMarketingPreviewRedirect 未触发浏览器跳转到根路径',
  )
})

test('D. Marketing Site HTML 为正式门户：无 noindex、无 /marketing-preview/、title 正确', () => {
  assert.ok(
    !siteHtml.includes('noindex,nofollow'),
    'Marketing Site HTML 仍含 noindex,nofollow（应为正式公开页）',
  )
  assert.ok(
    !siteHtml.includes('/marketing-preview/'),
    'Marketing Site HTML 仍引用 /marketing-preview/',
  )
  assert.ok(
    siteHtml.includes('<title>盘迹｜看一眼就知道怎么用</title>'),
    'Marketing Site title 应为「盘迹｜看一眼就知道怎么用」',
  )
})

test('E. Marketing Site entry 指向 site.tsx（非 preview.tsx）', () => {
  assert.ok(
    siteHtml.includes('../src/features/marketing/site.tsx'),
    'Marketing Site HTML 必须引用 ../src/features/marketing/site.tsx',
  )
})

test('F. 部署器用 build:marketing-site 构建，target 为 marketing-assets + portal/index.html（不再用 marketing-preview 作为构建目标）', () => {
  assert.ok(
    deployScript.includes('build:marketing-site'),
    '部署器未使用 build:marketing-site 作为构建命令',
  )
  assert.ok(
    !deployScript.includes('build:marketing-preview'),
    '部署器仍引用 build:marketing-preview',
  )
  assert.ok(
    deployScript.includes('marketing-assets'),
    '部署器未引用 marketing-assets 资源目录',
  )
  assert.ok(
    deployScript.includes('portal/index.html'),
    '部署器未把根门户 HTML 安装到 portal/index.html',
  )
})
