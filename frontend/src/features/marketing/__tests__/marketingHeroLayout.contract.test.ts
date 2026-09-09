// 营销 Hero + Nav 布局契约测试（Slice A）
// 目的：通过源码静态扫描，保证 Hero/Nav 已按参考图 #1 / #2 对齐：
//   - Hero 含 HeroScreener 组件、1000+ stat、status badges、0:19 时长徽章
//   - Nav 含 5 项菜单 + tagline + 绿色 CTA + 窄屏汉堡
//   - 不引入实时行情、不引新依赖、不暴露内部研究路由
//
// 读源：与 marketingCanonicalUrl.contract.test.ts 同款源码读法；
//       __dirname 解析以定位 frontend/src/features/marketing 与 sc根目录。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const __dirname = dirname(fileURLToPath(import.meta.url))
const FRONTEND_ROOT = resolve(__dirname, '../../../../') // frontend/

function readSrc(relPath: string): string {
  return readFileSync(
    resolve(FRONTEND_ROOT, relPath),
    'utf-8',
  )
}

const heroSrc = readSrc(
  'src/features/marketing/sections/Hero.tsx',
)
const navSrc = readSrc(
  'src/features/marketing/sections/MarketingNav.tsx',
)
const heroScreenerSrc = readSrc(
  'src/features/marketing/components/HeroScreener.tsx',
)
const heroScreenerDataSrc = readSrc(
  'src/features/marketing/data/heroScreener.ts',
)
const scssSrc = readSrc(
  'src/features/marketing/marketing.module.scss',
)
const packageJson = readSrc('package.json')

test('A1. Hero 包含 HeroScreener 组件 + stat + status badges', () => {
  assert.ok(
    /import\s+HeroScreener/.test(heroSrc),
    'Hero.tsx 必须 import HeroScreener',
  )
  assert.ok(
    /<HeroScreener\s*\/>/.test(heroSrc),
    'Hero.tsx 必须渲染 <HeroScreener />',
  )
  assert.ok(
    /marketing-hero-stat/.test(heroSrc),
    'Hero.tsx 必须带 stat testid',
  )
  assert.ok(
    /marketing-hero-status/.test(heroSrc),
    'Hero.tsx 必须带 status badges testid',
  )
  assert.ok(
    /id=["']hero["']/.test(heroSrc),
    'Hero.tsx 必须挂 #hero 锚点（对应 NAV.items[0].href）',
  )
})

test('A2. Hero 次级 CTA 含 0:19 时长徽章', () => {
  assert.ok(
    /btnDuration/.test(heroSrc),
    'Hero.tsx 必须渲染时长徽章',
  )
  assert.ok(
    /secondaryCta\.duration/.test(heroSrc),
    'Hero.tsx 必须消费 HERO.secondaryCta.duration',
  )
})

test('A3. HeroScreener 用确定性 mock + "演示数据" 标注', () => {
  assert.ok(
    /HERO_SCREENER_ROWS/.test(heroScreenerSrc),
    'HeroScreener 必须消费 HERO_SCREENER_ROWS',
  )
  assert.ok(
    /演示数据/.test(heroScreenerDataSrc) ||
      /非实时行情/.test(heroScreenerDataSrc),
    'heroScreener.ts 必须明确标注"演示数据/非实时行情"',
  )
  // A 股惯例：涨红跌绿
  assert.ok(
    /color-up/.test(scssSrc) || /heroScreenerUp/.test(scssSrc),
    'Screener 必须使用 A 股惯例（涨红 / 跌绿）',
  )
})

test('A4. Nav 含 tagline + 5 项 + 绿色 CTA + 窄屏汉堡', () => {
  assert.ok(
    /navTagline/.test(navSrc),
    'Nav 必须渲染 tagline',
  )
  assert.ok(
    /NAV\.items\.map/.test(navSrc),
    'Nav 必须遍历 NAV.items',
  )
  assert.ok(
    /NAV\.ctaHref/.test(navSrc) && /NAV\.ctaLabel/.test(navSrc),
    'Nav 必须消费 NAV.ctaHref + NAV.ctaLabel',
  )
  assert.ok(
    /navBurger/.test(navSrc),
    'Nav 必须含窄屏汉堡按钮',
  )
})

test('A5. SCSS 已为 Slice A 新增所有视觉类', () => {
  const required = [
    'navTagline',
    'navBurger',
    'heroGrid',
    'heroStat',
    'btnDuration',
    'statusBadges',
    'statusBadgeDot',
    'heroScreener',
    'heroScreenerTable',
    'heroScreenerUp',
    'heroScreenerDown',
  ]
  for (const cls of required) {
    assert.ok(
      new RegExp(`\\.${cls}\\b`).test(scssSrc),
      `marketing.module.scss 缺少 .${cls}`,
    )
  }
})

test('A6. 不引新依赖；不出现实时行情相关 API 字段', () => {
  // 列出 package.json dependencies，不允许新增 react-query 之外的网络客户端
  const deps = JSON.parse(packageJson).dependencies as Record<
    string,
    string
  >
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
    assert.ok(
      allowed.has(dep),
      `不允许新增依赖: ${dep}`,
    )
  }

  // Hero / HeroScreener / 数据文件均不应出现 fetch / axios / api 客户端调用
  const combined =
    heroSrc + navSrc + heroScreenerSrc + heroScreenerDataSrc
  assert.ok(
    !/\bfetch\s*\(/.test(combined),
    'Hero / Nav / Screener 不得调用 fetch',
  )
  assert.ok(
    !/\baxios\s*\(/.test(combined),
    'Hero / Nav / Screener 不得调用 axios',
  )
  assert.ok(
    !/\buseQuery\s*\(/.test(combined),
    'Hero / Nav / Screener 不得调用 useQuery',
  )
})