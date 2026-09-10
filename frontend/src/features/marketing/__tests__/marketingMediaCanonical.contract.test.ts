// [V1.6.1] Canonical Media Contract — 单源资产路径锁定
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const __dirname = dirname(fileURLToPath(import.meta.url))
const ROOT = resolve(__dirname, '../../../../')

function read(rel: string): string {
  return readFileSync(resolve(ROOT, rel), 'utf-8')
}

const copySrc = read('src/features/marketing/data/copy.ts')
const packageJson = read('package.json')
const viteSrc = read('vite.config.ts')
const marketingDir = resolve(ROOT, 'public/marketing-media')
const files = readdirSync(marketingDir)

const buildScript = (JSON.parse(packageJson).scripts as Record<string, string>)[
  'build:marketing-site'
]

test('A. MARKETING_MEDIA runtime image URL 必须全部以 /marketing-assets/media/ 开头', () => {
  const refs = copySrc.match(/\/marketing-assets\/media\/[\w.\-]+/g) || []
  assert.ok(refs.length > 0, 'copy.ts 必须引用 /marketing-assets/media/ URL')
  for (const url of refs) {
    assert.ok(url.startsWith('/marketing-assets/media/'), url)
    assert.ok(!url.includes('/ref/') && !url.includes('/Users/') && !url.includes('Desktop'), url)
  }
})

test('B. panji-watch-research.webp 必须存在于 public/marketing-media/', () => {
  assert.ok(files.includes('panji-watch-research.webp'), '缺 panji-watch-research.webp')
})

test('C. build:marketing-site 必须 cp -R public/marketing-media/.', () => {
  assert.ok(buildScript.includes('cp -R public/marketing-media/.'), '缺 cp -R canonical')
})

test('D. build 脚本不得含 public/landing/assets/images/poster_img1.webp', () => {
  assert.ok(!buildScript.includes('public/landing/assets/images/poster_img1.webp'), '不得从 landing 偷文件')
})

test('E. Marketing runtime URL 不得引用 ref/ 或本地绝对路径', () => {
  assert.ok(!copySrc.includes('/ref/') && !copySrc.includes('/Users/'), '不得含 ref 或本地路径')
})

test('F. vite.config.ts 必须提供 marketingMediaDevAlias', () => {
  assert.ok(viteSrc.includes('marketingMediaDevAlias'), '缺 alias plugin')
  assert.ok(
    viteSrc.includes('/marketing-assets/media/') && viteSrc.includes('/marketing-media/'),
    '缺 rewrite 映射',
  )
})
