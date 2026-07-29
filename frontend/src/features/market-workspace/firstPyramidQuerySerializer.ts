/**
 * [CHANGE-20260729-004 P0-1] 第一金字塔筛选/排序 URL 序列化纯函数
 *
 * 职责：
 *   - 将 StrategyDataTable 的 fp_ 列筛选（DataTableFilter[]）序列化为 fp_filter 字符串
 *   - 将 fp_ 列排序（{ key, direction }）序列化为 fp_sort 字符串
 *   - 仅处理 key 以 'fp_' 开头的筛选/排序，其他列由原有 metric_filters/sort 处理
 *
 * 协议（与后端 market_stocks_service._parse_fp_filter 对齐）：
 *   fp_filter=key1:op1:val1[;val2];key2:op2:val2
 *   - 多条件用 `;` 分隔
 *   - between 用 `key:between:val1;val2`（val1 和 val2 用 `;` 分隔）
 *   - empty/not_empty 无 value：`key:empty:`（值部分可省略）
 *   fp_sort=key:direction
 *
 * 不变量：
 *   - 仅处理 fp_ 前缀的 filter/sort；其他列原样返回空
 *   - null/空值不编码
 *   - 不校验白名单（由后端 _parse_fp_filter/_parse_fp_sort 校验，非法值返回 422）
 */

import type { DataTableFilter } from '@/components/StrategyDataTable'

/** 判断 filter/sort key 是否为第一金字塔字段 */
export function isFpKey(key: string | null | undefined): boolean {
  return !!key && key.startsWith('fp_')
}

/**
 * 序列化 fp 筛选条件为 fp_filter 字符串。
 *
 * @param filters StrategyDataTable 的所有列筛选
 * @returns fp_filter 字符串（无 fp 筛选时返回 undefined）
 */
export function serializeFpFilters(
  filters: DataTableFilter[] | null | undefined,
): string | undefined {
  if (!filters || filters.length === 0) return undefined

  const parts: string[] = []
  for (const f of filters) {
    if (!isFpKey(f.key)) continue

    if (f.operator === 'between') {
      if (f.value === undefined || f.value2 === undefined) continue
      parts.push(`${f.key}:between:${f.value};${f.value2}`)
    } else if (f.operator === 'empty' || f.operator === 'not_empty') {
      parts.push(`${f.operator === 'empty' ? f.key : f.key}:${f.operator}:`)
    } else {
      if (f.value === undefined || f.value === '') continue
      parts.push(`${f.key}:${f.operator}:${f.value}`)
    }
  }

  return parts.length > 0 ? parts.join(';') : undefined
}

/**
 * 序列化 fp 排序为 fp_sort 字符串。
 *
 * @param sort StrategyDataTable 的当前排序（{ key, direction } | null）
 * @returns fp_sort 字符串（非 fp 字段或 null 时返回 undefined）
 */
export function serializeFpSort(
  sort: { key: string; direction: 'asc' | 'desc' } | null | undefined,
): string | undefined {
  if (!sort || !isFpKey(sort.key)) return undefined
  return `${sort.key}:${sort.direction}`
}
