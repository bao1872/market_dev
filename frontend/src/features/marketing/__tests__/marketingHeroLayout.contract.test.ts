// 营销 Hero + Nav 布局契约测试（Full Alignment V1 + V1.2）
// 目的：通过源码静态扫描，保证 Hero/Nav 已按参考图 #1 / #2 对齐：
//   - V1.2：Hero 主视觉为 ProductDeviceStage 真实产品大屏（MacBook + iPhone），
//     六维 proof、诚实 status badges、#how-it-works 次级 CTA
//   - Nav 含 5 项菜单 + tagline + 绿色 CTA + 窄屏汉堡 + 真正可用的移动端面板
//   - 不引入实时行情（Hero/Nav 不 fetch）、不引新依赖、不暴露内部研究路由、不出现硬编码 hex
//
// 读源：__dirname 解析以定位 frontend/ 根目录。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const __dirname = dirname(fileURLToPath(import.meta.url))
const FRONTEND_ROOT = resolve(__dirname, '../../../../') // frontend/

function readSrc(relPath: string): string {
  return readFileSync(resolve(FRONTEND_ROOT, relPath), 'utf-8')
}

const heroSrc = readSrc('src/features/marketing/sections/Hero.tsx')
const navSrc = readSrc('src/features/marketing/sections/MarketingNav.tsx')
const deviceStageSrc = readSrc(
  'src/features/marketing/components/ProductDeviceStage.tsx',
)
const copySrc = readSrc('src/features/marketing/data/copy.ts')
const scssSrc = readSrc('src/features/marketing/marketing.module.scss')
const packageJson = readSrc('package.json')

test('A1. Hero 包含 ProductDeviceStage 真实产品大屏 + 六维 proof + status badges + #hero 锚点', () => {
  assert.ok(
    /import\s+ProductDeviceStage/.test(heroSrc),
    'Hero.tsx 必须 import ProductDeviceStage',
  )
  assert.ok(
    /<ProductDeviceStage\s*\/>/.test(heroSrc),
    'Hero.tsx 必须渲染 <ProductDeviceStage />',
  )
  assert.ok(
    /import\s+HeroScreener/.test(heroSrc) === false,
    'Hero.tsx 不得再引用假筛选表 HeroScreener',
  )
  assert.ok(/heroProof/.test(heroSrc), 'Hero.tsx 必须含六维 proof')
  assert.ok(
    /marketing-hero-status/.test(heroSrc),
    'Hero.tsx 必须带 status badges testid',
  )
  assert.ok(
    /id=["']hero["']/.test(heroSrc),
    'Hero.tsx 必须挂 #hero 锚点（对应 NAV.items[0].href）',
  )
})

test('A2. Hero 次级 CTA 指向 #how-it-works，且无 0:19 时长徽章', () => {
  assert.ok(
    /secondaryCta/.test(heroSrc),
    'Hero.tsx 必须渲染次级 CTA',
  )
  assert.ok(
    /HERO\.secondaryCta\.href/.test(heroSrc),
    'Hero 必须消费 HERO.secondaryCta.href',
  )
  assert.ok(
    /#how-it-works/.test(copySrc),
    'HERO.secondaryCta.href 必须指向 #how-it-works',
  )
  assert.ok(!/btnDuration/.test(heroSrc), 'Hero 不得再渲染 0:19 时长徽章')
  assert.ok(!/0:19/.test(heroSrc), 'Hero 不得含 0:19 假时长')
})

test('A3. 真实产品大屏用 MARKETING_MEDIA 三张真实截图（desktop + mobile + xueqiu），无假 Screener', () => {
  assert.ok(
    /MARKETING_MEDIA\.desktopProduct/.test(deviceStageSrc),
    'ProductDeviceStage 必须消费 MARKETING_MEDIA.desktopProduct',
  )
  assert.ok(
    /MARKETING_MEDIA\.mobileResearch/.test(deviceStageSrc),
    'ProductDeviceStage 必须消费 MARKETING_MEDIA.mobileResearch',
  )
  assert.ok(
    /macbookMock/.test(deviceStageSrc) && /iphoneMock/.test(deviceStageSrc),
    'ProductDeviceStage 必须包含 MacBook + iPhone 双层设备外壳',
  )
  assert.ok(
    /MARKETING_MEDIA\.xiaozXueqiu/.test(copySrc),
    'copy.ts DISCOVERY/XIAOZ 必须消费 MARKETING_MEDIA.xiaozXueqiu 真实雪球截图',
  )
  // A 股惯例涨红跌绿仍由 scss token 体系保证（screen 图来自真实产品，不再自造市场表）
})

test('A4. Nav 含 tagline + 5 项 + 绿色 CTA + 窄屏汉堡 + 真正可用的移动端面板', () => {
  assert.ok(/navTagline/.test(navSrc), 'Nav 必须渲染 tagline')
  assert.ok(/NAV\.items\.map/.test(navSrc), 'Nav 必须遍历 NAV.items')
  assert.ok(
    /NAV\.ctaHref/.test(navSrc) && /NAV\.ctaLabel/.test(navSrc),
    'Nav 必须消费 NAV.ctaHref + NAV.ctaLabel',
  )
  assert.ok(/navBurger/.test(navSrc), 'Nav 必须含窄屏汉堡按钮')
  assert.ok(/mobileNavPanel/.test(navSrc), 'Nav 必须含移动端面板')
  assert.ok(
    /mobileOpen && \(/.test(navSrc) || /mobileOpen \?/.test(navSrc),
    'Nav 必须按 mobileOpen 真实渲染移动端面板',
  )
  assert.ok(
    /setMobileOpen\(false\)/.test(navSrc),
    'Nav 点击锚点必须收起移动端面板',
  )
  assert.ok(/Escape/.test(navSrc), 'Nav 必须支持 Escape 关闭移动端面板')
})

test('A5. SCSS 已为 V1.2 新增真实产品大屏 / 回放视觉类（无硬编码 hex）', () => {
  const required = [
    'navTagline',
    'navBurger',
    'mobileNavPanel',
    'heroIntro',
    'heroProof',
    'statusBadges',
    'statusBadgeDot',
    'deviceStage',
    'macbookMock',
    'iphoneMock',
    'discoveryGrid',
    'discoveryFlow',
    'discoveryMedia',
    'workflowGrid',
    'marketCenter',
    'labTab',
    'labFunnelBar',
    'xiaozScreenshotFrame',
    'realReplayFrame',
    'realReplayProgressFill',
    'watchPhone',
    'finalCta',
    'fieldSearchInput',
  ]
  for (const cls of required) {
    assert.ok(
      new RegExp(`\\.${cls}\\b`).test(scssSrc),
      `marketing.module.scss 缺少 .${cls}`,
    )
  }
  // 禁止新增硬编码 hex（历史 #4f8ef7 已改为 token），新样式一律用 token/rgba
  const hexMatches = scssSrc.match(/#[0-9a-fA-F]{6}/g) ?? []
  const allowed = new Set([
    '#0A0F14',
    '#111A23',
    '#161F29',
    '#1A2433',
    '#263440',
    '#1D2832',
    '#F2F6F8',
    '#98A1B3',
    '#657281',
    '#00F6C2',
    '#39F5CF',
    '#00B28A',
    '#3882F6',
    '#F59E0B',
    '#8B5CF6',
    '#FF4D4F',
    '#22C55E',
  ])
  for (const hex of hexMatches) {
    assert.ok(
      allowed.has(hex.toUpperCase()) || allowed.has(hex),
      `SCSS 不得出现非 token 硬编码 hex: ${hex}`,
    )
  }
})

test('A6. 不引新依赖；Hero / Nav / 设备大屏不调用实时行情 API', () => {
  const deps = JSON.parse(packageJson).dependencies as Record<string, string>
  const allowed = new Set([
    '@tanstack/react-query',
    'axios',
    'clsx',
    'lightweight-charts',
    'react',
    'react-dom',
    'react-router-dom',
    'zustand',
  ])
  for (const dep of Object.keys(deps)) {
    assert.ok(allowed.has(dep), `不允许新增依赖: ${dep}`)
  }

  const combined = heroSrc + navSrc + deviceStageSrc
  assert.ok(!/\bfetch\s*\(/.test(combined), 'Hero / Nav / DeviceStage 不得调用 fetch')
  assert.ok(!/\baxios\s*\(/.test(combined), 'Hero / Nav / DeviceStage 不得调用 axios')
  assert.ok(
    !/\buseQuery\s*\(/.test(combined),
    'Hero / Nav / DeviceStage 不得调用 useQuery',
  )
})