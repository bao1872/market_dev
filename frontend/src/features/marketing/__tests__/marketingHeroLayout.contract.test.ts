// 营销 Hero + Nav 布局契约测试（Full Alignment V1）
// 目的：通过源码静态扫描，保证 Hero/Nav 已按参考图 #1 / #2 对齐：
//   - Hero 含 HeroScreener 组件、六维 proof、诚实 status badges、#how-it-works 次级 CTA
//   - Nav 含 5 项菜单 + tagline + 绿色 CTA + 窄屏汉堡 + 真正可用的移动端面板
//   - 不引入实时行情、不引新依赖、不暴露内部研究路由、不出现硬编码 hex
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
const heroScreenerSrc = readSrc(
  'src/features/marketing/components/HeroScreener.tsx',
)
const heroScreenerDataSrc = readSrc(
  'src/features/marketing/data/heroScreener.ts',
)
const copySrc = readSrc('src/features/marketing/data/copy.ts')
const scssSrc = readSrc('src/features/marketing/marketing.module.scss')
const packageJson = readSrc('package.json')

test('A1. Hero 包含 HeroScreener 组件 + 六维 proof + status badges + #hero 锚点', () => {
  assert.ok(/import\s+HeroScreener/.test(heroSrc), 'Hero.tsx 必须 import HeroScreener')
  assert.ok(/<HeroScreener\s*\/>/.test(heroSrc), 'Hero.tsx 必须渲染 <HeroScreener />')
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
  // #how-it-works 字面量在 copy.ts（HERO.secondaryCta.href），Hero 通过 HERO.secondaryCta.href 消费
  assert.ok(
    /HERO\.secondaryCta\.href/.test(heroSrc),
    'Hero 必须消费 HERO.secondaryCta.href',
  )
  assert.ok(
    /#how-it-works/.test(copySrc),
    'HERO.secondaryCta.href 必须指向 #how-it-works',
  )
  // 不得出现 Slice A 的 0:19 假 affordance
  assert.ok(
    !/btnDuration/.test(heroSrc),
    'Hero 不得再渲染 0:19 时长徽章',
  )
  assert.ok(
    !/0:19/.test(heroSrc),
    'Hero 不得含 0:19 假时长',
  )
})

test('A3. HeroScreener 用确定性 mock + 盘迹式列 + "演示数据" 标注 + A股惯例', () => {
  assert.ok(
    /HERO_SCREENER_ROWS/.test(heroScreenerSrc),
    'HeroScreener 必须消费 HERO_SCREENER_ROWS',
  )
  assert.ok(
    /演示数据/.test(heroScreenerDataSrc) || /非实时行情/.test(heroScreenerDataSrc),
    'heroScreener.ts 必须明确标注"演示数据/非实时行情"',
  )
  // 盘迹式列：趋势/结构/动量/量能/筹码/最近变化（区别于普通行情表）
  for (const col of ['趋势', '结构', '动量', '量能', '筹码', '最近变化']) {
    assert.ok(
      heroScreenerSrc.includes(col),
      `HeroScreener 必须含盘迹式列: ${col}`,
    )
  }
  // A 股惯例：涨红跌绿
  assert.ok(
    /color-up/.test(scssSrc) || /heroScreenerUp/.test(scssSrc),
    'Screener 必须使用 A 股惯例（涨红 / 跌绿）',
  )
})

test('A4. Nav 含 tagline + 5 项 + 绿色 CTA + 窄屏汉堡 + 真正可用的移动端面板', () => {
  assert.ok(/navTagline/.test(navSrc), 'Nav 必须渲染 tagline')
  assert.ok(/NAV\.items\.map/.test(navSrc), 'Nav 必须遍历 NAV.items')
  assert.ok(
    /NAV\.ctaHref/.test(navSrc) && /NAV\.ctaLabel/.test(navSrc),
    'Nav 必须消费 NAV.ctaHref + NAV.ctaLabel',
  )
  assert.ok(/navBurger/.test(navSrc), 'Nav 必须含窄屏汉堡按钮')
  // 移动端面板必须真正渲染（fixed 展开、点击锚点收起）
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

test('A5. SCSS 已为 Full Alignment V1 新增所有视觉类（无硬编码 hex）', () => {
  const required = [
    'navTagline',
    'navBurger',
    'mobileNavPanel',
    'heroGrid',
    'heroProof',
    'statusBadges',
    'statusBadgeDot',
    'heroScreener',
    'heroScreenerTable',
    'heroScreenerUp',
    'heroScreenerDown',
    'discoveryGrid',
    'discoveryFlow',
    'workflowGrid',
    'structureTimeline',
    'marketCenter',
    'labTab',
    'labFunnelBar',
    'xiaozCard',
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
  // 禁止新增硬编码 hex（历史 #4f8ef7 已改为 token）
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

test('A6. 不引新依赖；不出现实时行情相关 API 字段', () => {
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

  const combined = heroSrc + navSrc + heroScreenerSrc + heroScreenerDataSrc
  assert.ok(!/\bfetch\s*\(/.test(combined), 'Hero / Nav / Screener 不得调用 fetch')
  assert.ok(!/\baxios\s*\(/.test(combined), 'Hero / Nav / Screener 不得调用 axios')
  assert.ok(
    !/\buseQuery\s*\(/.test(combined),
    'Hero / Nav / Screener 不得调用 useQuery',
  )
})
