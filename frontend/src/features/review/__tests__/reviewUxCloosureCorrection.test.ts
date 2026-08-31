// [reviewUxCloosureCorrection.test] - 描述: REVIEW-UX-CLOSURE-02 CORRECTION-01
// 纯函数几何测试 + 源码契约测试（source contract，非 runtime proof）。
//
// 说明：本文件的 source-grep 断言只验证“源码层面用户可见文案/结构已按合同修正”，
// 不等同于浏览器真实渲染验收（后者在部署后由人工/真实浏览器完成）。
// 纯几何 computeTooltipPosition 为可单测的确定性函数。
import assert from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import {
  computeTooltipPosition,
  type Rect,
} from '../tooltipGeometry'

const __dirname = dirname(fileURLToPath(import.meta.url))
const FEATURE_DIR = join(__dirname, '..')

function readSource(relPath: string): string {
  return readFileSync(join(FEATURE_DIR, relPath), 'utf8')
}

const anchor: Rect = { top: 100, left: 100, right: 200, bottom: 120, width: 100, height: 20 }

// ---------------------------------------------------------------------------
// 纯几何测试：computeTooltipPosition
// ---------------------------------------------------------------------------
test('geom: normal — 锚点下方、左对齐，视口充足', () => {
  const tooltip: Rect = { top: 0, left: 0, right: 0, bottom: 0, width: 200, height: 80 }
  const res = computeTooltipPosition(anchor, tooltip, { width: 1920, height: 1080 }, 8)
  assert.equal(res.top, anchor.bottom + 8) // 128
  assert.equal(res.left, anchor.left) // 100
})

test('geom: right flip — 右侧空间不足时向左展开并 clamps 到 margin', () => {
  // tooltip 宽 200，anchor.right=200，视口宽 250 → 200+200=400 > 242 → 向左
  // left = max(margin, anchor.right - tooltipW) = max(8, 0) = 8
  const tooltip: Rect = { top: 0, left: 0, right: 0, bottom: 0, width: 200, height: 80 }
  const res = computeTooltipPosition(anchor, tooltip, { width: 250, height: 1080 }, 8)
  assert.equal(res.left, 8)
})

test('geom: bottom flip — 底部空间不足时向上偏移显示在锚点上方', () => {
  // 视口高 160，anchor.bottom=120，tooltip 高 80 → 120+8+80=208 > 152 → 向上
  const tooltip: Rect = { top: 0, left: 0, right: 0, bottom: 0, width: 120, height: 80 }
  const res = computeTooltipPosition(anchor, tooltip, { width: 1920, height: 160 }, 8)
  assert.equal(res.top, anchor.top - tooltip.height - 8) // 100-80-8 = 12
})

test('geom: top clamp — tooltip 比视口还高时顶部 clamp 到 margin', () => {
  const tooltip: Rect = { top: 0, left: 0, right: 0, bottom: 0, width: 120, height: 500 }
  const res = computeTooltipPosition(anchor, tooltip, { width: 1920, height: 160 }, 8)
  // 先向上：100-500-8=-408 < 8 → clamp 到 8
  assert.equal(res.top, 8)
})

test('geom: oversized tooltip — 纯几何只做 left/top clamp，不缩小 tooltip 尺寸', () => {
  // 职责边界：computeTooltipPosition 只负责定位（left/top clamp），
  // 不负责改变 tooltip 尺寸；tooltip 实际尺寸由 CSS/render owner（max-width/max-height）保证。
  const tooltip: Rect = { top: 0, left: 0, right: 0, bottom: 0, width: 900, height: 800 }
  const res = computeTooltipPosition(anchor, tooltip, { width: 600, height: 400 }, 8)
  assert.equal(res.left, 8) // 右侧不足 → 向左 → clamp 到 margin
  assert.equal(res.top, 8) // 顶部不足 → clamp 到 margin
  // 纯几何函数不得声称可以缩小 tooltip：left/top 已 clamp，但 900x800 的尺寸原样透传（由 CSS 约束）
  assert.ok(res.left + tooltip.width > 600, '纯几何不缩小 tooltip 宽度（超视口部分由 CSS 截断，非定位函数职责）')
})

test('source: rendered tooltip 受 viewport 约束（max-width / max-height 由 CSS/inline 保证）', () => {
  const src = readSource('ReviewTerm.tsx')
  // P1-3：tooltip 实际最大尺寸不超过 viewport，由 render owner 保证；定位仍用真实 getBoundingClientRect。
  assert.ok(/maxWidth:\s*'min\(340px, calc\(100vw - 16px\)\)'/.test(src), 'tooltip 缺少 viewport 约束的 max-width')
  assert.ok(/maxHeight:\s*'calc\(100vh - 16px\)'/.test(src), 'tooltip 缺少 viewport 约束的 max-height')
  assert.ok(/overflowY:\s*'auto'/.test(src), 'tooltip 缺少 overflow-y:auto')
  assert.ok(/whiteSpace:\s*'normal'/.test(src), 'tooltip 缺少 white-space:normal')
  // 不得重新引入固定高度猜测
  assert.ok(!/const tooltipH = \d+/.test(src), '仍存在固定高度猜测')
})

// ---------------------------------------------------------------------------
// 源码契约测试（source contract）：用户可见文案/结构已按合同修正
// ---------------------------------------------------------------------------
test('source: 普通页面不常驻显示 canonical group_key（<code> 已移除）', () => {
  const src = readSource('ScopeCurrentObservationWorkspace.tsx')
  // 不得存在将 group_key 作为可见 <code> 显示的写法
  assert.ok(!/className=\{styles\.observationGroupKey\}/.test(src), 'observationGroupKey <code> 仍存在于普通页面')
  // 但 id 锚点仍可保留（非可见）
  assert.ok(/id=\{`obs-group-\$\{group\.group_key\}`\}/.test(src) || /id=\{`obs-group-\$\{g\.group_key\}`\}/.test(src))
})

test('source: 普通 UI 不显示 backend path（observation.structure.current_state / canonical producer）', () => {
  const src = readSource('ScopeCurrentObservationWorkspace.tsx')
  assert.ok(!/来自 observation\.structure\.current_state（已加载）/.test(src), '仍直接显示 backend path 文案')
  assert.ok(!/canonical producer 未产出 chip 事实/.test(src), '仍直接显示 canonical producer 文案')
  // 技术字段仅允许出现在 tooltip help 内
  assert.ok(src.includes('当前技术状态已加载'))
  assert.ok(src.includes('本期暂无筹码数据'))
})

test('source: 表格 Technical cell 使用中文简称，不再使用 cryptic 单字母', () => {
  const src = readSource('ScopeExplorerTable.tsx')
  assert.ok(/label: '强度集中度'/.test(src), '缺少 强度集中度 中文简称')
  assert.ok(/label: '前5强度占比'/.test(src), '缺少 前5强度占比 中文简称')
  assert.ok(/label: '最高-中位强度差'/.test(src), '缺少 最高-中位强度差 中文简称')
  assert.ok(/label: '技术强度最高成员'/.test(src), '缺少 技术强度最高成员 中文简称')
  // 不得再出现 cryptic 前缀
  assert.ok(!/HHI \$/.test(src), '仍存在 HHI 单字母')
  assert.ok(!/Top5 \$/.test(src), '仍存在 Top5 单字母')
  assert.ok(!/Gap \$/.test(src), '仍存在 Gap 单字母')
  assert.ok(!/\bL \$/.test(src), '仍存在 L 单字母')
})

test('source: TradingView attribution 是真实 <a> 链接', () => {
  const src = readSource('ScopeDynamicsPanel.tsx')
  assert.ok(/href="https:\/\/www\.tradingview\.com\/"/.test(src), '缺少 TradingView 真实链接')
  assert.ok(/<a[\s\S]*TradingView Lightweight Charts[\s\S]*<\/a>/.test(src), 'attribution 不是真实 <a> 元素')
  assert.ok(/attributionLogo:\s*false/.test(src), 'attributionLogo 未保持 false')
})

test('source: P0-1 普通 UI 不显示 historical_dynamics（改为中文 unavailable 文案）', () => {
  const dyn = readSource('ScopeDynamicsPanel.tsx')
  // 旧的可视 unavailable 文案已移除（不再把 backend canonical 名称作为普通用户文本）
  assert.ok(!/该层当前不可用（无 historical_dynamics）/.test(dyn), '旧 historical_dynamics 可视文案仍存在')
  // 主视觉已改为中文
  assert.ok(/本期暂无等权涨跌动态数据/.test(dyn), '缺少中文 unavailable 文案')
  // canonical 技术 identity 仅允许出现在注释/tooltip，不应作为主视觉文本
})

test('source: P0-2 普通错误标题不显示 backend canonical 名称（Current Observation）', () => {
  const cur = readSource('ScopeCurrentObservationWorkspace.tsx')
  assert.ok(!/Current Observation 合同无效/.test(cur), '普通错误标题仍含 backend canonical 名称')
  assert.ok(/当日事实数据格式异常/.test(cur), '缺少中文错误主标题')
  // err.message 仅作为技术细节，不作为主视觉文本（已放入 panelErrorDetail）
  assert.ok(/技术细节：\{err\.message\}/.test(cur), 'err.message 未降级为技术细节')
})

test('source: ReviewTerm 使用 useRef 而非 useState 作为 DOM ref', () => {
  const src = readSource('ReviewTerm.tsx')
  assert.ok(/const anchorRef = useRef<HTMLElement \| null>\(null\)/.test(src), 'anchor 未使用 useRef')
  assert.ok(!/useState<HTMLElement \| null>\(null\)/.test(src), '仍使用 useState 作为 DOM ref')
  assert.ok(!/\[open, labelRef\]/.test(src), 'effect dependency 仍包含 ref state tuple')
})

test('source: tooltip 碰撞使用真实尺寸，不猜测固定高度（无 const tooltipH = 120）', () => {
  const src = readSource('ReviewTerm.tsx')
  assert.ok(!/const tooltipH = \d+/.test(src), '仍存在固定高度猜测')
  assert.ok(/getBoundingClientRect\(\)/.test(src), '未读取 tooltip 真实尺寸')
  assert.ok(/computeTooltipPosition\(/.test(src), '未调用真实尺寸碰撞定位')
})
