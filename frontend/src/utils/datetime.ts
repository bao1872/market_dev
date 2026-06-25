// 统一时区工具：所有时间显示固定 Asia/Shanghai
//
// 职责：
// - 提供上海时区的时间/日期格式化函数
// - 提供上海业务日期（A股交易日判断用）
//
// 使用约定：
// - 后端返回的 ISO 字符串（带 tz）直接传入 formatShanghaiTime 即可
// - 不要在业务代码中使用 toLocaleString() / toLocaleTimeString() / toLocaleDateString() 而不指定 timeZone

/** 完整日期时间（yyyy/MM/dd HH:mm:ss，上海时区） */
export function formatShanghaiTime(value: string | Date | null | undefined): string {
  if (!value) return '-'
  try {
    return new Date(value).toLocaleString('zh-CN', {
      timeZone: 'Asia/Shanghai',
      hour12: false,
    })
  } catch {
    return '-'
  }
}

/** 仅日期（yyyy/MM/dd，上海时区） */
export function formatShanghaiDate(value: string | Date | null | undefined): string {
  if (!value) return '-'
  try {
    return new Date(value).toLocaleDateString('zh-CN', {
      timeZone: 'Asia/Shanghai',
    })
  } catch {
    return '-'
  }
}

/** 仅时间（HH:mm，上海时区） */
export function formatShanghaiTimeShort(value: string | Date | null | undefined): string {
  if (!value) return '-'
  try {
    return new Date(value).toLocaleTimeString('zh-CN', {
      timeZone: 'Asia/Shanghai',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    })
  } catch {
    return '-'
  }
}

/** 当前上海业务日期（yyyy-mm-dd，用于 A 股交易日判断） */
export function shanghaiBusinessDate(): string {
  // 使用 Intl.DateTimeFormat 获取上海时区当前日期
  const fmt = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  })
  const parts = fmt.formatToParts(new Date())
  const y = parts.find((p) => p.type === 'year')?.value ?? ''
  const m = parts.find((p) => p.type === 'month')?.value ?? ''
  const d = parts.find((p) => p.type === 'day')?.value ?? ''
  return `${y}-${m}-${d}`
}
