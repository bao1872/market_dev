// [ChartTime] - 描述: 图表时间键规范化和时间轴刻度工具
//   纯函数模块（无 JSX），便于 Node --experimental-strip-types 单元测试
//   供 StrategyChart 和 DSA source alignment contract test 复用

import type { BarData } from '@/components/StrategyChart'
// 注：BarData 仅类型使用，import type 在编译时擦除，避免循环依赖

/**
 * 规范化图表时间键：消除 +08:00 / naive ISO 差异，仅保留 date + HH:MM
 *
 * - 日线/周线/月线用日期（YYYY-MM-DD）
 * - 15m/1h 用日期+分钟（YYYY-MM-DD HH:MM）
 * - 采用正则解析，不依赖 Date 解析做业务时间匹配（避免时区/格式差异错位）
 *
 * 用于 DSA source_bar_times 与 K线 displayTimes 交集比较，决定是否暂停 DSA 渲染。
 *
 * 修复根因：
 *   - 后端 15m/1h trade_time 之前返回 naive datetime，前端 new Date(...) 在非亚洲时区
 *     浏览器中当作本地时间，导致显示错误时间（如 03:00）
 *   - 后端修复后返回 aware datetime(+08:00)，前端正确解析为 UTC 时刻
 *   - normalizeChartTime 仅提取 date + HH:MM 前缀，忽略时区后缀和秒数，
 *     使 K线（aware）与 source_bar_times（naive）产生相同 canonical key
 */
export function normalizeChartTime(raw: unknown, tf: string): string | null {
  const value = String(raw ?? '').trim()
  const match = value.match(/^(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2}))?/)
  if (!match) return null

  if (tf === '15m' || tf === '1h') {
    return match[2] ? `${match[1]} ${match[2]}` : null
  }

  return match[1]
}

/**
 * 时间轴刻度：返回 N 个等间距的 { idx, label }，label 按 Asia/Shanghai 格式化
 *
 * - 15m/1h 显示 "MM-DD HH:MM"（北京交易时间）
 * - 1d 显示 "MM-DD"
 * - 1w/1mo 显示 "YYYY-MM"
 *
 * 关键不变量：
 *   - 15m aware 时间 "2026-07-06T15:00:00+08:00" 应显示 "07-06 15:00"，
 *     不应显示 "07-07 03:00"（naive datetime 在非亚洲时区浏览器中的错误显示）
 */
export function timeTicks(
  data: BarData[],
  count: number,
  tf: string,
): { idx: number; label: string }[] {
  const out: { idx: number; label: string }[] = []
  const mdFmt = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    month: '2-digit',
    day: '2-digit',
  })
  const timeFmt = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
  const ymFmt = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
  })

  for (let i = 0; i < count; i++) {
    const idx = Math.round((data.length - 1) * i / (count - 1))
    const d = new Date(data[idx].time)
    let label: string
    if (tf === '15m' || tf === '1h') {
      label = `${mdFmt.format(d).replace(/\//g, '-')} ${timeFmt.format(d)}`
    } else if (tf === '1d') {
      label = mdFmt.format(d).replace(/\//g, '-')
    } else {
      label = ymFmt.format(d)
    }
    out.push({ idx, label })
  }
  return out
}
