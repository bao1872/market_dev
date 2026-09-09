// [门户槽位] - 描述: 产品官网唯一部署槽位 SSOT 契约测试
// 用法：node --experimental-strip-types --test scripts/contract-tests/public-site-slot.contract.test.ts
//
// 背景：旧「使用说明」Help Center（frontend/public/portal）曾与产品官网共用同一物理槽位
//   `${LIVE_ROOT}/frontend/dist/portal/index.html`，导致「谁最后部署谁赢」：
//   - 全量前端 build（vite 拷贝 public/**）把旧帮助中心首页落进 dist/portal/index.html；
//   - Marketing 轻部署把产品官网 install 到 dist/portal/index.html；
//   - nginx `location = /` 精确读到 /portal/index.html ⇒ 全量 build 一执行旧帮助中心复活。
//
//   本轮已「彻底退役旧 Portal + 拆掉共享部署槽位」：
//   1. frontend/public/portal/** 从仓库删除（全量 build 不再产出 dist/portal/*）；
//   2. 产品官网唯一部署槽位改为 dist/site/index.html，只由 Marketing 轻部署写入；
//   3. nginx 根槽位指向 /site/index.html，/portal/ 一律 301 到 /。
//
// 覆盖（锁死不变量，SSOT 回归）：
// 1. frontend/public/portal 已彻底删除（index.html / SOURCE.md 均不存在）
// 2. nginx location = / 精确分流到 /site/index.html（不得再引用任何 /portal 路径）
// 3. nginx 存在 /portal/ 退役 tombstone，且无 /portal/index.html 精确配置残留
// 4. 全量前端 build 源（public/**）不再含有会被拷到根槽位的旧帮助中心首页
// 5. /api、Capture(SPA fallback) 合同未被删除
//
// 注：Marketing 轻部署把 root HTML 写入 site/index.html 的断言由
//     src/features/marketing/__tests__/marketingCanonicalUrl.contract.test.ts（test F）锁死，
//     本文件专注 nginx 根槽位 + 全量 build 无法再生旧帮助中心这两个 SSOT 不变量。

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync, existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)
// 旧「使用说明」portal 源码目录（已删除）：frontend/public/portal
const PORTAL_DIR = join(__dirname, '..', '..', 'public', 'portal')
// frontend 静态源根：frontend/public
const PUBLIC_DIR = join(__dirname, '..', '..', 'public')
// nginx.conf：frontend/nginx.conf
const NGINX_CONF = join(__dirname, '..', '..', 'nginx.conf')
// 全量部署脚本：scripts/deploy/panji-deploy.sh
const PANJI_DEPLOY = join(__dirname, '..', '..', '..', 'scripts', 'deploy', 'panji-deploy.sh')
// Marketing 轻部署脚本：scripts/ops/panji-marketing-site-deploy
const MARKETING_DEPLOY = join(__dirname, '..', '..', '..', 'scripts', 'ops', 'panji-marketing-site-deploy')

test('1. 旧使用说明 portal 源码已彻底删除', () => {
  assert.ok(!existsSync(PORTAL_DIR),
    'frontend/public/portal 应已从仓库删除（旧使用说明 Help Center 彻底退役）')
  assert.ok(!existsSync(join(PORTAL_DIR, 'index.html')),
    'portal/index.html（旧使用说明首页）不应存在')
  assert.ok(!existsSync(join(PORTAL_DIR, 'SOURCE.md')),
    'portal/SOURCE.md 不应存在')
  assert.ok(!existsSync(join(PORTAL_DIR, 'assets')),
    'portal/assets 不应存在')
  assert.ok(!existsSync(join(PORTAL_DIR, 'content')),
    'portal/content 不应存在')
})

test('2. nginx 根槽位精确分流到 /site/index.html（不再引用 /portal）', () => {
  const conf = readFileSync(NGINX_CONF, 'utf-8')
  const rootLocation = conf.match(/location\s*=\s*\/\s*\{([\s\S]*?)\n\s*\}/)
  assert.ok(rootLocation, '缺少 location = / 精确分流')
  assert.match(
    rootLocation[1],
    /try_files\s+\/site\/index\.html\s+=404;/,
    '根路径必须精确分流到 /site/index.html（产品官网唯一部署槽位）',
  )
  // 根 location 内不得再引用任何 /portal 路径
  assert.ok(
    !/\/portal\//.test(rootLocation[1]) && !/\/portal\/index\.html/.test(rootLocation[1]),
    '根 location 不得再引用 /portal 路径（旧帮助中心已退役）',
  )
})

test('3. /portal 已配置退役 tombstone，且无 /portal/index.html 精确配置残留', () => {
  const conf = readFileSync(NGINX_CONF, 'utf-8')
  // /portal/ 一律 301 到根路径（防止运行时残留 dist/portal/* 经 SPA fallback 被访问）
  assert.ok(
    /location\s*\^\~\s*\/portal\//.test(conf),
    '缺少 /portal/ 退役 tombstone（location ^~ /portal/ 应 301 到根路径）',
  )
  assert.ok(
    !/location\s*=\s*\/portal\/index\.html/.test(conf),
    '不得残留 location = /portal/index.html 精确配置',
  )
  assert.ok(
    !/location\s*[~^=]*\s*\/portal\/pages/.test(conf),
    '不得残留 /portal/pages 静态配置',
  )
})

test('4. 全量前端 build 源（public/**）不再含有旧帮助中心首页', () => {
  // vite build 会把 public/** 拷到 dist/；确认源码里不存在会被拷到 nginx 根槽位的旧帮助中心首页
  const indexPath = join(PUBLIC_DIR, 'index.html')
  if (existsSync(indexPath)) {
    const html = readFileSync(indexPath, 'utf-8')
    assert.ok(
      !/使用说明首页|盘迹使用说明中心|快速开始|帮助中心/.test(html),
      'frontend/public/index.html 不得是旧帮助中心首页',
    )
  }
  // portal 目录不存在 ⇒ 全量 build 不会产生 dist/portal/*
  assert.ok(!existsSync(PORTAL_DIR),
    'frontend/public/portal 不存在 ⇒ 全量 build（vite 拷贝 public/**）无法再生旧帮助中心')
  // 全量 build 的产物路径必须 ≠ 产品官网根槽位 site/index.html：
  // 产品官网根槽位只由 Marketing 轻部署写入，全量 build 无法覆盖。
  // 断言 public 下不存在 site/index.html（避免全量 build 误写根槽位的另一来源）。
  assert.ok(!existsSync(join(PUBLIC_DIR, 'site', 'index.html')),
    'frontend/public/site/index.html 不应存在（产品官网根槽位只允许 Marketing 轻部署写入）')
})

test('5. /api、Capture(SPA fallback) 合同未被删除', () => {
  const conf = readFileSync(NGINX_CONF, 'utf-8')
  // /api/ 通用代理
  assert.ok(/location\s*\/api\/\s*\{/.test(conf), '/api/ 代理被删除')
  assert.ok(conf.includes('rewrite ^/api/(.*) /$1 break'), '/api 只剥离一次合同被删除')
  assert.ok(!conf.includes('/api/api/v1'), '不得恢复双 /api 前缀合同')
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

test('6. 全量部署不得删除营销轻部署写入的根槽位（SSOT 保护）', () => {
  // 全量部署（panji-deploy.sh sync_frontend_runtime）用 `rsync --delete` 同步 frontend/dist。
  // 产品官网根槽位 site/index.html 与 marketing-assets/ 只由 Marketing 轻部署写入，全量 build 不产出，
  // 若不 --exclude，--delete 会把它们清掉，破坏「任意顺序部署后 / 都必须是产品官网」。
  const deploy = readFileSync(PANJI_DEPLOY, 'utf-8')
  // 找到 frontend dist 同步的 rsync --delete 调用
  const syncBlock = deploy.match(/rsync\s+-a\s+--delete\s+[\s\S]*?frontend\/dist\/["']?\s+["']?\$\{LIVE_ROOT\}\/frontend\/dist\//)
  assert.ok(syncBlock, '缺少 frontend dist 的 rsync --delete 同步调用')
  // 该 rsync 必须排除 site/ 与 marketing-assets/，避免 --delete 清掉营销产物
  assert.ok(syncBlock[0].includes("--exclude='site/'"),
    'frontend dist rsync 必须 --exclude=\'site/\'（保护产品官网根槽位不被全量部署删除）')
  assert.ok(syncBlock[0].includes("--exclude='marketing-assets/'"),
    'frontend dist rsync 必须 --exclude=\'marketing-assets/\'（保护营销资源不被全量部署删除）')
})

test('7. nginx 对 /portal 精确与前缀均 301 到根（彻底 tombstone）', () => {
  const conf = readFileSync(NGINX_CONF, 'utf-8')
  assert.match(conf, /location\s*=\s*\/portal\s*\{[^}]*return\s+301\s+\/;/, '缺少 location = /portal 精确 301')
  assert.match(conf, /location\s*\^\~\s*\/portal\/\s*\{[^}]*return\s+301\s+\/;/, '缺少 location ^~ /portal/ 前缀 301')
})

test('8. 部署器支持 CUTOVER 预置 --prepare-only（锁 site 槽位、不要求根已切换、不碰旧 portal）', () => {
  const deploy = readFileSync(MARKETING_DEPLOY, 'utf-8')
  // 参数：必须有 --prepare-only 分支
  assert.ok(/--prepare-only\)\s*PREPARE_ONLY=true/.test(deploy),
    '部署器必须支持 --prepare-only 选项分支，置 PREPARE_ONLY=true')
  // 部署：prepare-only 仍 install 产品官网到 site/index.html（产品官网唯一槽位）
  assert.ok(deploy.includes("CURRENT_ROOT_HTML=\"${LIVE_ROOT}/frontend/dist/site/index.html\"")
      || deploy.includes('dist/site/index.html'),
    '部署器必须把产品官网根 HTML 安装到 site/index.html')
  // prepare-only 不得回写任何 /portal/index.html / portal 槽位
  assert.ok(!/install[^\n]*portal\/index\.html/.test(deploy),
    '部署器不得再把任何 HTML install 到 portal/index.html 槽位')
  // prepare-only 必须以磁盘文件为准确认 site 就绪（不要求根 / 已切换、不依赖旧 nginx 路由）
  assert.ok(/\bPREPARE_ONLY\b.{0,400}site\/index\.html/.test(deploy)
      || /\bPREPARE_ONLY\b[\s\S]{0,400}"\$\{CURRENT_ROOT_HTML\}"/.test(deploy),
    'prepare-only 必须校验 site/index.html 就绪')
})