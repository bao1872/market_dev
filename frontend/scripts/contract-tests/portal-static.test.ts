// [门户] - 描述: 公开静态门户静态合同测试（PRD 盘迹公开门户替换 V1.1 §12.1）
// 用法：node --experimental-strip-types --test scripts/contract-tests/portal-static.test.ts
// 覆盖：
// 1. 11 个 HTML 文件存在（index.html + 10 个 pages/*.html）
// 2. 必需资源存在（css/js/data/content/图片/SOURCE.md）
// 3. zip 来源与 SHA256 记录存在于 SOURCE.md
// 4. 无 href="#"、空 href、javascript:void(0) 死链接
// 5. 无 /replay 与不存在的 /alerts 业务链接
// 6. 无 panji-logo.svg 运行引用
// 7. 因子目录合同：total=240 / verified_current_core=24 / extended_catalog=216 / categories=15 / ID 唯一
// 8. JS 与 JSON 因子数据一致
// 9. 新因子六项输入存在
// 10. 摘要包含已选因子、输出选项与六项输入
// 11. Nginx 根路径精确分流存在（location = / → /portal/index.html）
// 12. /api、Capture(SPA fallback) 合同未被删除

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync, existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
// portal 目录：frontend/public/portal
const PORTAL_DIR = join(__dirname, '..', '..', 'public', 'portal')
// nginx.conf：frontend/nginx.conf
const NGINX_CONF = join(__dirname, '..', '..', 'nginx.conf')

function readText(rel: string): string {
  return readFileSync(join(PORTAL_DIR, rel), 'utf-8')
}

function assertExists(rel: string): void {
  assert.ok(existsSync(join(PORTAL_DIR, rel)), `缺少必需文件：${rel}`)
}

// 11 个 HTML 文件清单（PRD §5.1）
const HTML_FILES = [
  'index.html',
  'pages/quick-start.html',
  'pages/market.html',
  'pages/watchlist.html',
  'pages/stock-detail.html',
  'pages/alerts.html',
  'pages/messages.html',
  'pages/data.html',
  'pages/faq.html',
  'pages/boundaries.html',
  'pages/customization.html',
]

test('1. 11 个 HTML 文件全部存在', () => {
  assert.equal(HTML_FILES.length, 11, '应为 11 个 HTML')
  for (const f of HTML_FILES) {
    assertExists(f)
  }
})

test('2. 必需资源存在', () => {
  const required = [
    'assets/css/site.css',
    'assets/js/site.js',
    'assets/data/factors.public.js',
    'assets/data/factors.public.json',
    'assets/images/logo_symbol_128.png',
    'assets/images/wechat-qr-placeholder.svg',
    'content/site.json',
    'SOURCE.md',
  ]
  for (const f of required) {
    assertExists(f)
  }
})

test('3. SOURCE.md 记录 zip 来源与 SHA256', () => {
  const src = readText('SOURCE.md')
  assert.ok(src.includes('盘迹门户_完整版_Logo与需求摘要修正版.zip'), 'SOURCE.md 缺少 zip 名称')
  assert.ok(
    src.includes('4e89469330e0d5050a87a37305e7571bc67c401156c155bbebf0e1654192f523'),
    'SOURCE.md 缺少 SHA256',
  )
  assert.ok(src.includes('0.5.1'), 'SOURCE.md 缺少附件版本')
})

test('4. 无死链接（href="#" / 空 href / javascript:void(0)）', () => {
  const dead = [/href="#"/, /href=''/, /href=""/, /javascript:void\(0\)/]
  for (const f of HTML_FILES) {
    const html = readText(f)
    for (const re of dead) {
      assert.ok(!re.test(html), `${f} 存在死链接：${re}`)
    }
  }
})

test('5. 无 /replay 与不存在的 /alerts 业务链接', () => {
  for (const f of HTML_FILES) {
    const html = readText(f)
    assert.ok(!/href="\/replay"/.test(html), `${f} 存在 /replay 公开入口`)
    assert.ok(!/href="\/alerts"/.test(html), `${f} 存在伪 /alerts 业务链接`)
  }
})

test('6. 无 panji-logo.svg 运行引用', () => {
  for (const f of HTML_FILES) {
    const html = readText(f)
    assert.ok(!/panji-logo\.svg/.test(html), `${f} 仍引用 panji-logo.svg`)
  }
  // 附件 panji-logo.svg 不应被导入到 portal 目录
  assert.ok(!existsSync(join(PORTAL_DIR, 'assets/images/panji-logo.svg')), 'panji-logo.svg 不应被导入')
})

test('7. 因子目录合同（240/24/216/15，ID 唯一）', () => {
  const json = JSON.parse(readText('assets/data/factors.public.json'))
  assert.equal(json.version, 'public-catalog-v1', 'version 应为 public-catalog-v1')
  assert.equal(json.generated_at, '2026-07-22', 'generated_at 应为 2026-07-22')
  assert.equal(json.total, 240, 'total 应为 240')
  // PRD 称 current_core，实际字段为 verified_current_core
  assert.equal(json.verified_current_core, 24, 'verified_current_core 应为 24')
  assert.equal(json.extended_catalog, 216, 'extended_catalog 应为 216')
  assert.ok(Array.isArray(json.categories), 'categories 应为数组')
  assert.equal(json.categories.length, 15, 'categories 应为 15')

  const factors = json.factors
  assert.ok(Array.isArray(factors), 'factors 应为数组')
  assert.equal(factors.length, 240, 'factors 数量应为 240')
  const ids = factors.map((f: { id: string }) => f.id)
  const unique = new Set(ids)
  assert.equal(unique.size, 240, `因子 ID 应唯一，实际唯一 ${unique.size}`)

  // status 分布：24 current_core + 216 extended_catalog
  const coreCount = factors.filter((f: { status: string }) => f.status === 'current_core').length
  const extCount = factors.filter((f: { status: string }) => f.status === 'extended_catalog').length
  assert.equal(coreCount, 24, `status=current_core 应为 24，实际 ${coreCount}`)
  assert.equal(extCount, 216, `status=extended_catalog 应为 216，实际 ${extCount}`)
})

test('8. JS 与 JSON 因子数据一致', () => {
  const json = JSON.parse(readText('assets/data/factors.public.json'))
  const jsRaw = readText('assets/data/factors.public.js')
  // JS 形如：window.PANJI_FACTOR_CATALOG = { ... };
  assert.ok(jsRaw.includes('PANJI_FACTOR_CATALOG'), 'JS 应挂载到 window.PANJI_FACTOR_CATALOG')
  const start = jsRaw.indexOf('{')
  const end = jsRaw.lastIndexOf('}')
  assert.ok(start > -1 && end > start, 'JS 中应能提取 JSON 对象')
  const jsObj = JSON.parse(jsRaw.slice(start, end + 1))
  assert.equal(jsObj.version, json.version, 'JS/JSON version 不一致')
  assert.equal(jsObj.total, json.total, 'JS/JSON total 不一致')
  assert.equal(jsObj.verified_current_core, json.verified_current_core, 'JS/JSON verified_current_core 不一致')
  assert.equal(jsObj.extended_catalog, json.extended_catalog, 'JS/JSON extended_catalog 不一致')
  assert.equal(jsObj.categories.length, json.categories.length, 'JS/JSON categories 数量不一致')
  assert.equal(jsObj.factors.length, json.factors.length, 'JS/JSON factors 数量不一致')
  // ID 集合一致
  const jsonIds = new Set(json.factors.map((f: { id: string }) => f.id))
  const jsIds = new Set(jsObj.factors.map((f: { id: string }) => f.id))
  assert.deepEqual([...jsIds].sort(), [...jsonIds].sort(), 'JS/JSON 因子 ID 集合不一致')
})

test('9. 新因子六项输入存在', () => {
  const html = readText('pages/customization.html')
  const six = ['newObserve', 'newOutput', 'newLogic', 'newData', 'newExample', 'newScope']
  for (const id of six) {
    assert.ok(html.includes(`id="${id}"`), `customization.html 缺少六项输入字段：${id}`)
  }
})

test('10. 摘要包含已选因子、输出选项与六项输入', () => {
  const html = readText('pages/customization.html')
  // 已选因子容器
  assert.ok(html.includes('id="selectedList"'), '摘要缺少已选因子容器 selectedList')
  // 输出选项容器
  assert.ok(html.includes('id="outputOptions"'), '摘要缺少输出选项容器 outputOptions')
  // 摘要生成函数
  assert.ok(html.includes('function buildSummary'), '缺少 buildSummary 函数')
  // 复制按钮 + Clipboard fallback
  assert.ok(html.includes('id="copySummary"'), '缺少 copySummary 复制按钮')
  assert.ok(html.includes('navigator.clipboard'), '缺少 navigator.clipboard 调用')
  assert.ok(html.includes('execCommand'), '缺少 execCommand fallback')
  // 六项空值显示「未填写」
  assert.ok(html.includes("'未填写'") || html.includes("'未填写'"), '六项空值应显示「未填写」')
})

test('11. Nginx 根路径精确分流存在', () => {
  const conf = readFileSync(NGINX_CONF, 'utf-8')
  // 根路径精确匹配，返回 /portal/index.html
  assert.ok(conf.includes('location = /'), '缺少 location = / 精确分流')
  assert.ok(conf.includes('try_files /portal/index.html =404'), '根路径未分流到 /portal/index.html')
  // 门户首页与说明页禁缓存
  assert.ok(conf.includes('location = /portal/index.html'), '缺少 /portal/index.html 规则')
  assert.ok(conf.includes('location ~ ^/portal/pages/.*\\.html$'), '缺少 /portal/pages/*.html 规则')
  assert.ok(conf.includes('location /portal/'), '缺少 /portal/ 静态资源规则')
  assert.ok(conf.includes('no-store, no-cache, must-revalidate'), '门户 HTML 应禁缓存')
})

test('12. /api、Capture(SPA fallback) 合同未被删除', () => {
  const conf = readFileSync(NGINX_CONF, 'utf-8')
  // /api/v1/health 精确代理
  assert.ok(/location\s*=\s*\/api\/v1\/health/.test(conf), '/api/v1/health 精确代理被删除')
  // /api/ 通用代理
  assert.ok(/location\s*\/api\/\s*\{/.test(conf), '/api/ 代理被删除')
  assert.ok(conf.includes('proxy_pass http://$backend_url'), '/api/ proxy_pass 被删除')
  // WebSocket headers
  assert.ok(conf.includes('Upgrade $http_upgrade'), 'WebSocket Upgrade header 被删除')
  assert.ok(conf.includes('Connection "upgrade"'), 'WebSocket Connection header 被删除')
  // /index.html no-cache
  assert.ok(/location\s*=\s*\/index\.html/.test(conf), '/index.html no-cache 被删除')
  // /assets/ immutable
  assert.ok(/location\s*\/assets\/\s*\{/.test(conf), '/assets/ immutable 被删除')
  assert.ok(conf.includes('immutable'), '/assets/ immutable 缓存被删除')
  // SPA fallback（Capture /capture/stock/:symbol 等业务路由由 SPA fallback 处理）
  assert.ok(/location\s*\/\s*\{/.test(conf), 'SPA fallback location / 被删除')
  assert.ok(conf.includes('try_files $uri $uri/ /index.html'), 'SPA fallback try_files 被删除')
  // resolver 保留
  assert.ok(conf.includes('resolver 127.0.0.11'), 'resolver 被删除')
})
