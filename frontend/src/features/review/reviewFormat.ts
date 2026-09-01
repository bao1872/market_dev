// [ReviewFormat] - 描述: canonical Scope 展示用纯格式化函数（Slice C 引入）
// 规则：
// - null/undefined → 显示占位符 "—"（绝不把 null 当作 0）
// - 只做展示格式化，不计算 phase / capital tilt / migration / score / ranking
// - phase / readiness 展示 label 经 reviewCopy 中文化映射；canonical 值不变
// - 无 React / SCSS 依赖，可被 node --test 直接运行

import { PHASE_LABELS, READINESS_LABELS } from './reviewCopy'

export const NULL_DISPLAY = '—'

/**
 * [REVIEW-UX-EXPERIMENT-READINESS-01 Slice A] 无 scopeName 时的产品展示占位。
 * 空名称时绝不用内部 scopeKey / UUID 冒充正常产品名称；
 * scopeKey 仍保留为 URL、routing、API identity、React key 与 Raw Facts 身份。
 */
export const UNNAMED_SCOPE_LABEL = '未命名板块'

/** 百分比格式化：输入为比率（0.123 → "12.3%"）；null/NaN → "—" */
export function formatPercentNullable(
  value: number | null | undefined,
  digits = 1,
): string {
  if (value === null || value === undefined || Number.isNaN(value)) return NULL_DISPLAY
  return `${(value * 100).toFixed(digits)}%`
}

/** 普通数字格式化；null/NaN → "—" */
export function formatNumberNullable(
  value: number | null | undefined,
  digits = 2,
): string {
  if (value === null || value === undefined || Number.isNaN(value)) return NULL_DISPLAY
  return value.toFixed(digits)
}

/**
 * Position 格式化：canonical 0–100 historical percentile，直接展示原值，
 * 绝不乘 100（75 显示 "75"，不是 "7500%"）。
 */
export function formatPosition(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return NULL_DISPLAY
  return String(Math.round(value * 100) / 100)
}

/** Dynamics Phase 展示标签（REVIEW-UX-CN-01：经 reviewCopy 中文化映射；不计算 phase，canonical 值不变） */
export function formatPhaseLabel(phase: string | null | undefined): string {
  if (!phase) return NULL_DISPLAY
  return PHASE_LABELS[phase] ?? phase
}

/** Composition Readiness 展示标签（REVIEW-UX-CN-01：经 reviewCopy 中文化映射；canonical 值不变） */
export function formatReadiness(readiness: string | null | undefined): string {
  if (!readiness) return NULL_DISPLAY
  return READINESS_LABELS[readiness] ?? readiness
}

/**
 * 成员展示名：member_name 有真实值且不同于 member_id 时展示 name；
 * member_name 缺失/为空/与 member_id 相同 → 诚实展示 member_id（prompt §9）。
 */
export function memberName(m: { member_id: string | number; member_name?: string | null }): string {
  return m.member_name && String(m.member_name).trim() !== '' && String(m.member_name) !== String(m.member_id)
    ? String(m.member_name)
    : String(m.member_id)
}

/** 成员身份目录类型（后端 memberDirectory 值）。 */
export interface MemberDirectoryEntry {
  symbol: string
  name: string
}

export type MemberDirectory = Record<string, MemberDirectoryEntry>

/**
 * [REVIEW-PRODUCT-CLOSURE-01 Phase C] 成员展示唯一 owner。
 * 优先级：目录中 name+symbol 齐全 → "名称 · 代码"；仅 symbol → symbol；
 * 目录缺失该 id 时 → 短/内部 id（UUID 兜底）。
 * UUID 只出现在 title/技术 hover，不作为主展示。
 */
export function displayMember(
  memberId: string | number,
  directory: MemberDirectory | null | undefined,
): string {
  const id = String(memberId)
  const entry = directory?.[id]
  if (!entry) return id
  const name = entry.name?.trim()
  const symbol = entry.symbol?.trim()
  if (name && symbol) return `${name} · ${symbol}`
  if (symbol) return symbol
  if (name) return name
  return id
}

/**
 * 贡献占比格式化（R1 technical concentration top3/top5 contribution）。
 * 输入为 persisted {numerator, denominator} 分数；前端只做展示格式化，绝不重算比例。
 * denominator > 0 → 显示 "num / den  (pct%)"；
 * denominator == 0 或分子/分母为 null/缺失 → "—"（绝不伪造 0% 或 100%）。
 */
export function formatContributionFraction(
  frac: { numerator: number | null; denominator: number | null } | null | undefined,
): string {
  if (!frac) return NULL_DISPLAY
  const num = frac.numerator
  const den = frac.denominator
  if (num === null || num === undefined || den === null || den === undefined) return NULL_DISPLAY
  if (den === 0) return NULL_DISPLAY
  const pct = (num / den) * 100
  return `${num.toFixed(1)} / ${den.toFixed(1)}  (${pct.toFixed(1)}%)`
}

/** 无量纲倍数（1.50 → "1.50×"）。绝不 x100。 */
export function formatMultipleNullable(
  value: number | null | undefined,
  digits = 2,
): string {
  if (value === null || value === undefined || Number.isNaN(value)) return NULL_DISPLAY
  return `${value.toFixed(digits)}×`
}

/** 无量纲原始标量（BB position 1.12 / -0.15，BB width 0.0832）：原值展示，绝不 clamp / 绝不 x100 / 无单位后缀。 */
export function formatRawDimensionlessNullable(
  value: number | null | undefined,
  digits = 4,
): string {
  if (value === null || value === undefined || Number.isNaN(value)) return NULL_DISPLAY
  return value.toFixed(digits)
}

/** 历史百分位原值展示（72.5 → "72.5"）；绝不 x100（非 "7250%" 亦非 "72.5%"）。 */
export function formatPercentileNullable(
  value: number | null | undefined,
  digits = 1,
): string {
  if (value === null || value === undefined || Number.isNaN(value)) return NULL_DISPLAY
  return value.toFixed(digits)
}

/**
 * [REVIEW-UX-EXPERIMENT-READINESS-01 Slice A] 总成交额展示单位：百亿元。
 * 1 百亿元 = 10^10 元。
 *
 * A2 Unit Gate 证据链（证明 canonical amount 单位 = 人民币元）：
 * - bar 源 = pytdx：backend/app/services/bars_validator.py:16
 *   「df: pytdx 返回的 DataFrame，含 datetime/open/high/low/close/volume/amount 列」
 * - pytdx → DB 无换算：backend/app/core/pytdx_adapter.py:650-651（仅 vol→volume 改名，
 *   amount 原值传递）
 * - pytdx amount 单位 = 元：backend/app/services/auction_quote_provider.py:51
 *   「amount: float | None  # pytdx amount 字段（元）」（同处 vol = 手）
 * - Review 链无换算：backend/app/domain/review/member_fact.py:432
 *   `amount=_number(row.amount)`；backend/app/domain/review/scope_observation.py:653-657
 *   `total_amount = sum(...)`（仅改排序保证确定性，注释「Same formula」）
 * - 真实量级反证：402 成员 concept total_amount=312834079356
 *   （experiments/review_real_market_acceptance/canonical_compositions.jsonl）
 *   → 元 = 3128 亿元（≈7.8 亿元/只/日，符合 A 股量级）；
 *     若为千元则 = 312 万亿元，超全市场日成交额约 200 倍，物理不可能。
 */
export const AMOUNT_UNIT_LABEL = '百亿元'
export const RAW_YUAN_PER_BAI_YI_YUAN = 10_000_000_000

/**
 * 总成交额（百亿元）展示格式化：display = raw / 10^10。
 * - null / undefined / NaN → NULL_DISPLAY('—')，绝不落 0；
 * - amountAvailability === 'unavailable' 由 VM 层短路为 '—'，不进入本换算；
 * - 真实正值但按 digits 舍入后为 0 → 显示 "<0.01" 而非 "0.00"，
 *   避免把「有成交但极小」误显示为「零成交」。
 */
export function formatAmountInBaiYiYuan(
  value: number | null | undefined,
  digits = 2,
): string {
  if (value === null || value === undefined || Number.isNaN(value)) return NULL_DISPLAY
  const scaled = value / RAW_YUAN_PER_BAI_YI_YUAN
  const fixed = scaled.toFixed(digits)
  if (scaled > 0 && Number(fixed) === 0) {
    return `<${(1 / 10 ** digits).toFixed(digits)}`
  }
  return fixed
}

/** Z-score 原始展示（1.35 / -1.35）；无 "%"，无方向配色。 */
export function formatZScoreNullable(
  value: number | null | undefined,
  digits = 2,
): string {
  if (value === null || value === undefined || Number.isNaN(value)) return NULL_DISPLAY
  return value.toFixed(digits)
}
