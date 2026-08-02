// [Capabilities] - 描述: capability 机器值 ↔ 中文展示标签的唯一真源
// 背景（PRD60 PA-01 / CHANGE-20260802-002）：
//   竞价（/auction/*）与复盘（/review）同属一项权益，机器值统一为 research_replay，
//   面向管理员与用户的中文展示统一为「复盘与竞价」。
//   不存在独立的 auction capability，本文件不得新增 auction 项。
// 使用约束：
//   任何组件需要展示 capability 中文名时必须从此处读取，禁止再散落硬编码字符串。
// 本文件为纯 TS（无 React 依赖），可被 node --test 直接运行，便于契约测试。

/** 全部 capability 机器值（与 backend app/models/user_capability.py ALL_CAPABILITIES 对齐） */
export const CAPABILITY_KEYS = ['self_selection', 'market_data', 'research_replay'] as const

export type CapabilityKey = (typeof CAPABILITY_KEYS)[number]

/** capability 机器值 → 中文展示标签（唯一真源） */
export const CAPABILITY_LABELS: Record<CapabilityKey, string> = {
  self_selection: '自选管理',
  market_data: '行情数据',
  // 复盘与竞价共用同一权益，展示层合并表述
  research_replay: '复盘与竞价',
}

/** capability 机器值 → 覆盖范围补充说明（管理端勾选项副标题） */
export const CAPABILITY_DESCRIPTIONS: Record<CapabilityKey, string> = {
  self_selection: 'self_selection · 行情列表/自选/盘中监控',
  market_data: 'market_data · 行情列表/个股详情',
  research_replay: 'research_replay · 复盘工作台/竞价分析',
}

/** 复盘与竞价共用的 capability 机器值（前端守卫与导航过滤统一引用） */
export const REPLAY_AND_AUCTION_CAPABILITY: CapabilityKey = 'research_replay'

/** 判断字符串是否为已知 capability 机器值 */
export function isCapabilityKey(value: string): value is CapabilityKey {
  return (CAPABILITY_KEYS as readonly string[]).includes(value)
}

/**
 * 取 capability 的中文标签；未知机器值原样返回，避免静默吞掉后端新增值。
 */
export function capabilityLabel(capability: string): string {
  return isCapabilityKey(capability) ? CAPABILITY_LABELS[capability] : capability
}

/** 用户 capability 状态（与 backend AccessContext.capabilities 对齐的最小子集） */
export interface CapabilityStateLike {
  active?: boolean
}

/**
 * 判断用户是否具备某 capability（admin 豁免）。
 * 复盘与竞价共用 research_replay，因此两者的可见性由本函数统一决定，
 * 不允许在组件内再实现第二套权限判断。
 */
export function hasCapability(
  capabilities: Record<string, CapabilityStateLike | undefined> | undefined | null,
  capability: string,
  isAdmin: boolean,
): boolean {
  if (isAdmin) return true
  return capabilities?.[capability]?.active === true
}

/** 单条 capability 授权（与 backend CapabilityGrant / InviteCode.capabilities JSONB 对齐） */
export interface CapabilityGrantLike {
  capability: string
  months?: number
  watchlist_limit?: number | null
}

/**
 * 将 capability 授权列表格式化为展示文案，例如：
 *   「自选管理 · 行情数据 · 复盘与竞价」
 * 规则：
 * - 按 CAPABILITY_KEYS 固定顺序输出，避免后端顺序变化导致展示抖动
 * - 无对应权限的标签不显示
 * - 列表为空 / null / undefined 时返回空字符串，由调用方决定占位文案
 */
export function formatCapabilityGrants(
  grants: readonly CapabilityGrantLike[] | null | undefined,
): string {
  if (!grants || grants.length === 0) return ''
  const present = new Set(grants.map((g) => g.capability))
  const ordered = CAPABILITY_KEYS.filter((key) => present.has(key)).map(
    (key) => CAPABILITY_LABELS[key],
  )
  // 保留后端可能返回的未知机器值（原样展示），排在已知项之后
  const unknown = grants
    .map((g) => g.capability)
    .filter((c) => !isCapabilityKey(c))
  return [...ordered, ...unknown].join(' · ')
}
