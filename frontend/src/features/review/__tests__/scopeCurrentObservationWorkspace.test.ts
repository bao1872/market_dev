// [R3B] Current Observation Workspace ownership 静态契约测试（纯 tsx --test，无 DOM）。
//
// 覆盖（R3B spec FE-1/2/3/4/7/13/15/17）：
// - Current route 渲染 ScopeCurrentObservationWorkspace（FE-1）
// - Current route 不再渲染 ScopeCurrentSnapshotPanel（FE-2）
// - ScopeDetailWorkspace 不再用 parseCurrentSnapshot 作为 Current owner（FE-3）
// - Current workspace 直接消费 detail.observationGroups（FE-4）
// - 无前端第二份 canonical 中文 label map（FE-7）
// - 无 Analysis 泄漏（Position/Velocity/Acceleration/Capital Tilt/Migration）（FE-13/15）
// - 无 useState 创建的 Current sub-tab state（FE-17）

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const REVIEW_DIR = join(dirname(fileURLToPath(import.meta.url)), '..')
const read = (rel: string): string => readFileSync(join(REVIEW_DIR, rel), 'utf8')

const workspaceSrc = read('ScopeCurrentObservationWorkspace.tsx')
const detailSrc = read('ScopeDetailWorkspace.tsx')
const contractSrc = read('scopeObservationWorkspaceContract.ts')

// R3B-FE-1：Current route renders ScopeCurrentObservationWorkspace
test('R3B-FE-1: Current route renders ScopeCurrentObservationWorkspace', () => {
  assert.ok(
    detailSrc.includes('ScopeCurrentObservationWorkspace'),
    'ScopeDetailWorkspace must render ScopeCurrentObservationWorkspace',
  )
  assert.ok(
    detailSrc.includes("tab === 'current'"),
    'current tab branch must exist',
  )
})

// R3B-FE-2：Current route no longer renders ScopeCurrentSnapshotPanel
test('R3B-FE-2: Current route no longer renders ScopeCurrentSnapshotPanel', () => {
  assert.ok(
    !detailSrc.includes('ScopeCurrentSnapshotPanel'),
    'ScopeDetailWorkspace must not reference ScopeCurrentSnapshotPanel',
  )
})

// R3B-FE-3：ScopeDetailWorkspace no longer uses parseCurrentSnapshot as Current owner
test('R3B-FE-3: ScopeDetailWorkspace no longer imports/uses parseCurrentSnapshot as owner', () => {
  // 精确匹配：import 语句或 JSX 调用，而非注释中的说明性文字。
  const hasImport = /import\s*\{[^}]*\bparseCurrentSnapshot\b[^}]*\}\s*from/.test(detailSrc)
  const hasCall = /parseCurrentSnapshot\s*\(/.test(detailSrc)
  assert.ok(
    !hasImport && !hasCall,
    'parseCurrentSnapshot must not be imported or called as Current owner in ScopeDetailWorkspace',
  )
})

// R3B-FE-4：Current workspace consumes detail.observationGroups directly (passed as prop)
test('R3B-FE-4: Current workspace receives observationGroups + observation as props only', () => {
  assert.ok(
    workspaceSrc.includes('observationGroups') && workspaceSrc.includes('observation'),
    'workspace must receive observationGroups + observation props',
  )
  // 必须调用 adapter owner 构建模型，而非自行重解析
  assert.ok(
    workspaceSrc.includes('buildObservationWorkspaceModel'),
    'workspace must use contract adapter to build model',
  )
})

// R3B-FE-7：no frontend duplicate canonical Chinese label map for the 8 groups
test('R3B-FE-7: no frontend duplicate canonical Chinese label map', () => {
  // backend label 由 contract 透传；前端不得硬编码 8 组中文 label 映射。
  // 允许的 UI IA 区域标题（Price & Capital 等）不属于 canonical 8-group label map。
  const forbiddenPairs = [
    "price_capital: '价格与资金表现'",
    "trend_state: '趋势状态'",
    "momentum_squeeze_release: '动量与压缩释放'",
  ]
  for (const p of forbiddenPairs) {
    assert.ok(
      !contractSrc.includes(p),
      `frontend must not hardcode canonical label map (found: ${p})`,
    )
  }
})

// R3B-FE-13/15：no Analysis leakage in new Current owner
test('R3B-FE-13/15: no Analysis fields in Current owner', () => {
  // 这些属于 Analysis tabs（Dynamics/Internal/Leadership/Attribution），不应出现在 Current workspace。
  // 使用代码级 token（驼峰/下划线字段名），避免命中说明性注释。
  const analysisLeak = [
    'capitalTilt',
    'capital_tilt',
    'leadershipConcentration',
    'leadership_concentration',
    'acceleration',
    'migration',
  ]
  for (const token of analysisLeak) {
    assert.ok(
      !workspaceSrc.includes(token),
      `Current owner must not contain Analysis token: ${token}`,
    )
  }
  // 验证组件确实消费的是 observation（L1），而非 composition 派生
  assert.ok(
    workspaceSrc.includes('observationGroups') && workspaceSrc.includes('observation'),
    'Current owner must consume observationGroups + observation',
  )
})

// R3B-FE-17：no useState-created Current sub-tab state
test('R3B-FE-17: no useState sub-tab state in Current workspace', () => {
  // 精确匹配实际调用 useState(，而非注释中的描述。
  assert.ok(
    !/useState\s*\(/.test(workspaceSrc),
    'Current workspace must not create useState sub-tab state',
  )
  assert.ok(
    workspaceSrc.includes('anchorScroll'),
    'Current navigation must use anchor scroll, not state machine',
  )
})

// R3B §20：no second query hook (single detail owner)
test('R3B §20: no new query hook in Current workspace', () => {
  // 精确匹配实际 hook 调用，而非注释中提及的复用语义。
  assert.ok(
    !/use(Query|InfiniteQuery|ReviewScopeDetail)\s*\(/.test(workspaceSrc),
    'Current workspace must not create a second query hook',
  )
})

// R3B-V B：Current workspace 不引用 group.status（backend 无 group-level status）
test('R3B-V B: workspace does not reference group.status', () => {
  assert.ok(
    !/group\.status/.test(workspaceSrc),
    'Current workspace must not read invented group.status contract',
  )
})

// R3B-V C：GroupShell 不声称每个 present fact 是"已加载"状态标签。
// 精确匹配：GroupShell 用的 per-fact state span class 已被移除（observationFactState 不再出现）。
// Observation Context 区块的"已加载"是诚实标注 observation 字段存在，不在此约束内。
test('R3B-V C: GroupShell no per-fact "已加载" state label', () => {
  assert.ok(
    !workspaceSrc.includes('observationFactState'),
    'GroupShell must not render per-fact state label (observationFactState class removed)',
  )
  // GroupShell 渲染的 fact 项只含 fact key，不应含任何 status-like 文案
  assert.ok(
    !/observationFactState|className=\{styles\.observationFactState\}/.test(workspaceSrc),
    'GroupShell must not attach fact-state label nodes',
  )
})
