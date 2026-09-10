// [V1.6.2] Canonical Media Contract — direct MARKETING_MEDIA iteration
// 锁：所有 marketing runtime URL 必须走 canonical public/marketing-media/
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const __dirname = dirname(fileURLToPath(import.meta.url))
// __dirname = frontend/src/features/marketing/__tests__/
// ROOT     = market_dev/ (repo root, 5 levels up)
const ROOT = resolve(__dirname, '../../../../../')
const FE = resolve(ROOT, 'frontend')

function readRepo(rel: string): string {
  return readFileSync(resolve(ROOT, rel), 'utf-8')
}
function readFe(rel: string): string {
  return readFileSync(resolve(FE, rel), 'utf-8')
}

const copySrc = readFe('src/features/marketing/data/copy.ts')
const packageJson = readFe('package.json')
const viteSrc = readFe('vite.config.ts')
const deploySrc = readRepo('scripts/ops/panji-marketing-site-deploy')
const marketingDir = resolve(FE, 'public/marketing-media')
const mediaFiles = readdirSync(marketingDir)

const buildScript = (JSON.parse(packageJson).scripts as Record<string, string>)[
  'build:marketing-site'
]

test('A. MARKETING_MEDIA 所有 runtime URL 必须以 /marketing-assets/media/ 开头，且不含 ref 或本地绝对路径', async () => {
  const mod = await import('../data/copy')
  const MEDIA = mod.MARKETING_MEDIA as Record<string, string>
  for (const [key, url] of Object.entries(MEDIA)) {
    assert.ok(
      url.startsWith('/marketing-assets/media/'),
      `MARKETING_MEDIA.${key}: ${url} 必须以 /marketing-assets/media/ 开头`,
    )
    assert.ok(
      !/ref\/|file:\/\/|\/Users\/|Desktop\//.test(url),
      `MARKETING_MEDIA.${key}: 禁止 runtime 路径 ${url}`,
    )
  }
})

test('B. panji-watch-research.webp 必须存在于 public/marketing-media/', () => {
  assert.ok(
    mediaFiles.includes('panji-watch-research.webp'),
    '缺 panji-watch-research.webp（Git recover 自 origin/dev）',
  )
})

test('C. build:marketing-site 必须 cp -R public/marketing-media/. dist-marketing-site/media/', () => {
  assert.ok(
    buildScript.includes('cp -R public/marketing-media/.'),
    'build:marketing-site 必须统一 cp -R canonical 目录',
  )
})

test('D. build 脚本不得含 public/landing/assets/images/poster_img1.webp', () => {
  assert.ok(
    !buildScript.includes('public/landing/assets/images/poster_img1.webp'),
    'build 脚本不得再从 landing 目录偷 poster 文件',
  )
})

test('E. copy.ts 不得含 ref/ 或本地绝对路径', () => {
  assert.ok(!copySrc.includes('/ref/') && !copySrc.includes('../ref/'), 'copy.ts 不得含 ref 引用')
  assert.ok(!copySrc.includes('/Users/') && !copySrc.includes('Desktop/'), 'copy.ts 不得含开发机绝对路径')
})

test('F. vite.config.ts 必须提供 marketingMediaDevAlias（/marketing-assets/media/ → /marketing-media/）', () => {
  assert.ok(viteSrc.includes('marketingMediaDevAlias'), '缺 alias plugin 注册')
  assert.ok(
    viteSrc.includes('/marketing-assets/media/') && viteSrc.includes('/marketing-media/'),
    '缺 rewrite 映射',
  )
})

test('G. deploy 脚本 verify loop 必须含 panji-watch-research.webp', () => {
  assert.ok(
    deploySrc.includes('panji-watch-research.webp'),
    'deploy 脚本 verify loop 必须包含新 canonical poster',
  )
  assert.ok(
    !deploySrc.includes('poster_img1.webp'),
    'deploy 脚本不得再含旧 poster_img1.webp',
  )
})
