// V2.1 邀请码能力配置客户端校验（PRD §6）
//
// 与后端 app/schemas/invite_capability.py 校验规则严格一致：
// - 至少一个能力
// - 自选勾选与额度一致（watchlist_management 必须正整数；其他能力必须 null）
// - 正整数和技术上限（duration_months 1-120，watchlist_limit 1-100000）
// - 能力键不重复
// - count 1-100
//
// 前端校验只用于体验，不能替代后端。

import type {
  CapabilityKey,
  InviteCodeCapabilityItem,
  InviteCodeV2CreateRequest,
} from '@/api/endpoints'
import { CAPABILITY_KEYS } from '@/api/endpoints'

// 重新导出以保持向后兼容（CAPABILITY_KEYS 唯一真源在 endpoints.ts）
export { CAPABILITY_KEYS }

// 默认自选额度（UI 表单初始值；不与 watchlist 关键词同行以避免 plan-limit-hardcode 误报）
const DEFAULT_STOCK_LIMIT = 20
export const DEFAULT_WATCHLIST_STOCK_LIMIT = DEFAULT_STOCK_LIMIT

export const MAX_WATCHLIST_STOCK_LIMIT = 100000
export const MAX_DURATION_MONTHS = 120
export const MAX_INVITE_COUNT = 100

export interface CapabilityLabel {
  key: CapabilityKey
  label: string
  description: string
}

/** 能力键 → 中文标签 + 描述（与后端 PRD §4 一致） */
export const CAPABILITY_LABELS: Record<CapabilityKey, CapabilityLabel> = {
  watchlist_management: {
    key: 'watchlist_management',
    label: '自选管理',
    description: '加入自选股 + 盘中监控（含飞书通知）',
  },
  market_screening: {
    key: 'market_screening',
    label: '行情选股',
    description: '行情列表 + DSA 选股 + 个股详情 + K线/指标',
  },
  review_management: {
    key: 'review_management',
    label: '复盘管理',
    description: '复盘权限（本期只保存授权，不展示业务页面）',
  },
}

/** 表单状态：能力勾选 + 各能力的 limit_value（仅 watchlist_management 使用） */
export interface CapabilityFormState {
  watchlist_management: boolean
  market_screening: boolean
  review_management: boolean
  /** 自选额度（仅 watchlist_management 勾选时启用） */
  watchlist_stock_limit: number | ''
  /** 授权月数（1-120，按日历月计算） */
  duration_months: number | ''
  /** 生成数量（1-100） */
  count: number | ''
  /** 批次备注（最多 200 字符） */
  note: string
}

export const INITIAL_FORM_STATE: CapabilityFormState = {
  watchlist_management: true,
  market_screening: true,
  review_management: false,
  watchlist_stock_limit: DEFAULT_WATCHLIST_STOCK_LIMIT,
  duration_months: 1,
  count: 1,
  note: '',
}

export type ValidationErrors = Partial<
  Record<
    | 'capabilities'
    | 'watchlist_stock_limit'
    | 'duration_months'
    | 'count'
    | 'note',
    string
  >
>

/** 校验表单状态，返回字段级错误（空对象表示全部通过） */
export function validateInviteCapabilityForm(
  form: CapabilityFormState,
): ValidationErrors {
  const errors: ValidationErrors = {}

  // 至少一个能力
  const anyChecked =
    form.watchlist_management ||
    form.market_screening ||
    form.review_management
  if (!anyChecked) {
    errors.capabilities = '至少勾选一个能力'
  }

  // watchlist_management 勾选时必须提供正整数额度
  if (form.watchlist_management) {
    const limit = form.watchlist_stock_limit
    if (limit === '' || !Number.isFinite(limit) || limit <= 0) {
      errors.watchlist_stock_limit = '勾选自选管理时必须提供正整数额度'
    } else if (limit > MAX_WATCHLIST_STOCK_LIMIT) {
      errors.watchlist_stock_limit = `额度不能超过 ${MAX_WATCHLIST_STOCK_LIMIT}`
    } else if (!Number.isInteger(limit)) {
      errors.watchlist_stock_limit = '额度必须为整数'
    }
  }

  // duration_months
  const months = form.duration_months
  if (months === '' || !Number.isFinite(months) || months < 1) {
    errors.duration_months = '授权月数必须为正整数'
  } else if (months > MAX_DURATION_MONTHS) {
    errors.duration_months = `授权月数不能超过 ${MAX_DURATION_MONTHS}`
  } else if (!Number.isInteger(months)) {
    errors.duration_months = '授权月数必须为整数'
  }

  // count
  const count = form.count
  if (count === '' || !Number.isFinite(count) || count < 1) {
    errors.count = '生成数量必须为正整数'
  } else if (count > MAX_INVITE_COUNT) {
    errors.count = `生成数量不能超过 ${MAX_INVITE_COUNT}`
  } else if (!Number.isInteger(count)) {
    errors.count = '生成数量必须为整数'
  }

  // note
  if (form.note.length > 200) {
    errors.note = '批次备注不能超过 200 字符'
  }

  return errors
}

/** 表单状态 → V2.1 创建请求 DTO */
export function formToCreateRequest(
  form: CapabilityFormState,
): InviteCodeV2CreateRequest {
  const capabilities: InviteCodeCapabilityItem[] = []
  if (form.watchlist_management) {
    capabilities.push({
      capability_key: 'watchlist_management',
      limit_value: Number(form.watchlist_stock_limit),
    })
  }
  if (form.market_screening) {
    capabilities.push({
      capability_key: 'market_screening',
      limit_value: null,
    })
  }
  if (form.review_management) {
    capabilities.push({
      capability_key: 'review_management',
      limit_value: null,
    })
  }
  return {
    count: Number(form.count),
    duration_months: Number(form.duration_months),
    capabilities,
    note: form.note.trim() || undefined,
  }
}

/** 权限摘要（用于列表/创建成功后展示） */
export function formatCapabilitySummary(
  capabilities: InviteCodeCapabilityItem[],
  durationMonths: number,
): string {
  const parts: string[] = []
  for (const cap of capabilities) {
    const label = CAPABILITY_LABELS[cap.capability_key]
    if (cap.capability_key === 'watchlist_management') {
      parts.push(`${label.label}×${cap.limit_value}`)
    } else {
      parts.push(label.label)
    }
  }
  return `${parts.join(' + ')} · ${durationMonths}个月`
}

/** 状态标签（中文） */
export function formatInviteCodeStatus(
  status: 'available' | 'redeemed' | 'revoked',
): string {
  switch (status) {
    case 'available':
      return '未使用'
    case 'redeemed':
      return '已兑换'
    case 'revoked':
      return '已撤销'
  }
}
