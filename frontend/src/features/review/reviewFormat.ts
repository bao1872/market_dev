// [ReviewFormat] - 描述: canonical Scope 展示用纯格式化函数（Slice C 引入）
// 规则：
// - null/undefined → 显示占位符 "—"（绝不把 null 当作 0）
// - 只做展示格式化，不计算 phase / capital tilt / migration / score / ranking
// - 无 React / SCSS 依赖，可被 node --test 直接运行

export const NULL_DISPLAY = '—'

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

/** Dynamics Phase 展示标签（可在此做本地化映射；不计算 phase） */
export function formatPhaseLabel(phase: string | null | undefined): string {
  if (!phase) return NULL_DISPLAY
  return phase
}
