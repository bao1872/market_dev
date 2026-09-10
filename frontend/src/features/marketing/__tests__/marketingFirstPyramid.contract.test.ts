// Marketing V1.5.2 契约：第一金字塔 99 字段字典 + UNIFIED FOOTER。
// 目标：
//   - Marketing 非产品表格消费产品 SSOT 派生的 FIRST_PYRAMID_FIELD_DICTIONARY，
//     禁止另写第二套 99 字段。
//   - 公开 adapter 不得泄露内部技术词（fp_/VWAP/BOS/CHoCH/POC/VAH/VAL/SQZ/feature_snapshot/Run ID）。
//   - Drawer 完全静态（无 fetch / useMarketFilterSpecs / /api/v1/*），默认趋势、真字段、可搜索。
//   - Footer 合并为单一 footerUnified（无独立 boxed footerCommunity 大卡片），
//     小二维码 object-fit: contain、desktop <=128px、QQ 保持 922×922。
//
// 读源：__dirname 解析以定位 frontend/ 根目录；产品字典用相对路径 import（tsx 可直接解析）。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import {
  FP_FIELD_GROUPS,
  FIRST_PYRAMID_FIELD_DICTIONARY,
  type FirstPyramidDictionaryField,
} from '../../market-workspace/firstPyramidColumns'
import {
  MARKETING_FIRST_PYRAMID_FIELDS,
  MARKETING_FIRST_PYRAMID_BANNED_TOKENS,
} from '../data/firstPyramidPublicDictionary'

const __dirname = dirname(fileURLToPath(import.meta.url))
const FRONTEND_ROOT = resolve(__dirname, '../../../../') // frontend/

function readSrc(relPath: string): string {
  return readFileSync(resolve(FRONTEND_ROOT, relPath), 'utf-8')
}

const drawerSrc = readSrc('src/features/marketing/sections/FirstPyramidDrawerShell.tsx')
const adapterSrc = readSrc('src/features/marketing/data/firstPyramidPublicDictionary.ts')
const footerSrc = readSrc('src/features/marketing/sections/MarketingFooter.tsx')
const scssSrc = readSrc('src/features/marketing/marketing.module.scss')

// ===== 第一金字塔：共享字典派生 =====

test('V1.5.2-P1. 派生 FIRST_PYRAMID_FIELD_DICTIONARY 必须 99 字段、8 组、字段非空', () => {
  assert.equal(FIRST_PYRAMID_FIELD_DICTIONARY.length, 99)
  const groups = Object.keys(FP_FIELD_GROUPS)
  assert.equal(groups.length, 8)
  for (const field of FIRST_PYRAMID_FIELD_DICTIONARY) {
    assert.ok(field.key.length > 0, `key 非空: ${field.key}`)
    assert.ok(field.group && groups.includes(field.group), `group 非法: ${field.group}`)
    assert.ok(field.title.length > 0, `title 非空: ${field.key}`)
    assert.ok(field.helpText.length > 0, `helpText 非空: ${field.key}`)
  }
})

test('V1.5.2-P2. 8 组字段数锁定 7/18/8/21/13/9/10/13', () => {
  const groupCounts = Object.values(FP_FIELD_GROUPS).map((keys) => keys.length)
  assert.deepEqual(groupCounts, [7, 18, 8, 21, 13, 9, 10, 13])

  const byGroup = new Map<FirstPyramidDictionaryField['group'], number>()
  for (const field of FIRST_PYRAMID_FIELD_DICTIONARY) {
    byGroup.set(field.group, (byGroup.get(field.group) ?? 0) + 1)
  }
  for (const [group, keys] of Object.entries(FP_FIELD_GROUPS)) {
    assert.equal(
      byGroup.get(group as keyof typeof FP_FIELD_GROUPS),
      keys.length,
      `派生字典组「${group}」数量需与注册表一致`,
    )
  }
})

// ===== Marketing 消费共享字典 =====

test('V1.5.2-P3. Marketing 消费共享字典，不复写第二套 99 字段', () => {
  assert.match(
    adapterSrc,
    /import\s*\{[^}]*FIRST_PYRAMID_FIELD_DICTIONARY[^}]*\}\s*from\s*['"]\.\.\/\.\.\/market-workspace\/firstPyramidColumns['"]/,
    'adapter 必须 import 产品 SSOT 的 FIRST_PYRAMID_FIELD_DICTIONARY',
  )
  assert.match(
    drawerSrc,
    /import\s*\{[^}]*MARKETING_FIRST_PYRAMID_FIELDS[^}]*\}\s*from\s*['"]\.\.\/data\/firstPyramidPublicDictionary['"]/,
    'drawer 必须消费 marketing public dictionary',
  )
})

test('V1.5.2-P4. Drawer 完全静态 + 默认趋势 + 无 DATA PENDING', () => {
  for (const bad of [
    'DATA PENDING',
    'pendingNote',
    'fieldPending',
    'fetch(',
    'useMarketFilterSpecs',
    '/api/v1/',
  ]) {
    assert.ok(!drawerSrc.includes(bad), `drawer 不得含 ${bad}`)
  }
  assert.ok(drawerSrc.includes("useState('trend')"), 'drawer 默认 activeGroup 必须为 trend')
  assert.ok(drawerSrc.includes('fieldDictionary'), 'drawer 必须用两栏 fieldDictionary')
})

test('V1.5.2-P5. 公开 dictionary 禁止内部技术词（title + description）', () => {
  assert.equal(MARKETING_FIRST_PYRAMID_FIELDS.length, 99)
  for (const field of MARKETING_FIRST_PYRAMID_FIELDS) {
    const visible = `${field.title} ${field.description}`
    for (const token of MARKETING_FIRST_PYRAMID_BANNED_TOKENS) {
      assert.ok(!visible.includes(token), `${field.key} 公开文字泄露「${token}」: ${visible}`)
    }
  }
})

test('V1.5.2-P6. 关键定义锁定（量能 / 结构事件新鲜度 / 主要成交密集价）', () => {
  const byKey = new Map(MARKETING_FIRST_PYRAMID_FIELDS.map((f) => [f.key, f]))

  const ratio = byKey.get('fp_volume_ratio20')!
  assert.equal(ratio.title, '20日量比')
  assert.equal(ratio.description, '当前成交量 ÷ 过去20日平均成交量。')

  const percentile = byKey.get('fp_volume_percentile20')!
  assert.equal(percentile.title, '20日量分位')
  assert.match(percentile.description, /范围 0–1/)

  const zscore = byKey.get('fp_volume_zscore20')!
  assert.equal(zscore.title, '20日量Z分')
  assert.match(zscore.description, /偏离多少个标准差/)

  const freshness = byKey.get('fp_structure_event_freshness')!
  assert.equal(freshness.title, '结构事件新鲜度')
  assert.match(freshness.description, /越小越新/)

  const poc = byKey.get('fp_poc_price')!
  assert.equal(poc.title, '主要成交密集价')
  assert.match(poc.description, /不代表股东真实持仓成本/)
})

// ===== UNIFIED FOOTER =====

test('V1.5.2-F1. Footer 已合并为 footerUnified，无独立 boxed footerCommunity', () => {
  assert.ok(footerSrc.includes('styles.footerUnified'), 'Footer 必须有 footerUnified')
  assert.match(scssSrc, /\.footerUnified\s*\{/, 'SCSS 必须有 .footerUnified')

  // 不再有独立 boxed 大卡片（footerCommunityCopy/Title/Desc 为文字类名，允许保留）
  assert.ok(!/\n\.footerCommunity\s*\{/.test(scssSrc), 'SCSS 不得再有 .footerCommunity 大卡片规则')
  assert.ok(!/styles\.footerCommunity\b/.test(footerSrc), 'Footer 不得再用 boxed footerCommunity 布局')
  assert.ok(!footerSrc.includes('footerInner'), 'Footer 已弃用 footerInner')
  assert.ok(!scssSrc.includes('.footerInner'), 'SCSS 已移除 .footerInner')
  assert.ok(!footerSrc.includes('footerQrCard'), 'Footer 已弃用 footerQrCard')
  assert.ok(!scssSrc.includes('.footerQrCard'), 'SCSS 已移除 .footerQrCard')
  assert.ok(!footerSrc.includes('card.note'), '二维码下不得再显示 card.note')
})

test('V1.5.2-F2. QR object-fit contain、desktop <=128px、QQ 仍为 922×922', () => {
  assert.match(
    scssSrc,
    /\.footerQrImage\s+img\s*\{[^}]*object-fit:\s*contain/,
    '.footerQrImage img 必须 object-fit: contain',
  )

  const m = scssSrc.match(/\.footerQrImage\s*\{[^}]*width:\s*(\d+)px/)
  assert.ok(m, '.footerQrImage 需声明桌面宽度')
  assert.ok(Number(m[1]) <= 128, `desktop QR 显示宽度 ${m[1]}px 必须 <= 128`)

  const buf = readFileSync(
    resolve(FRONTEND_ROOT, 'public/marketing-media/community-qq-group-qr.png'),
  )
  assert.equal(buf.readUInt32BE(16), 922, 'canonical QQ QR 宽必须为 922')
  assert.equal(buf.readUInt32BE(20), 922, 'canonical QQ QR 高必须为 922')
})

test('V1.5.2-F3. 窄屏字典：分组折行 2 列，字段列表位于下方', () => {
  // 提取 max-width: v.$bp-narrow 媒体块（到下一个 section 注释为止），校验其内部覆盖规则
  const start = scssSrc.indexOf('@media (max-width: v.$bp-narrow')
  assert.ok(start !== -1, 'SCSS 必须存在 @media (max-width: v.$bp-narrow)')
  const endMarker = scssSrc.indexOf('@media (prefers-reduced-motion', start)
  const narrowBlock = scssSrc.slice(
    start,
    endMarker === -1 ? start + 3000 : endMarker,
  )
  assert.match(
    narrowBlock,
    /\.fieldDictionary\s*\{[^}]*grid-template-columns:\s*1fr/,
    '窄屏 fieldDictionary 必须为单列（字段列表位于下方）',
  )
  assert.match(
    narrowBlock,
    /\.fieldGroups\s*\{[^}]*flex-direction:\s*row/,
    '窄屏 fieldGroups 必须改为横向折行',
  )
})