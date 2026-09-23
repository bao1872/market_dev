// [PANJI-TDX-RELIABILITY-PARITY-03] 实时分钟行情不可用（后端 typed 503）前端契约。
//
// 独立模块（不依赖 apiClient / axios），便于契约测试与 UI 层直接引用。
//
// 后端对 **MANDATORY** base 周期（15m / 1h）的 live provider outage 返回：
//   HTTP 503 + detail = { code, timeframe, provider_family, message }
//
// Axios 原生 `error.message` 只会是 "Request failed with status code 503"，
// 不含后端友好文案；因此 chart-snapshot 在本地把它转成这个 typed error。

/** 用户可见文案（与后端 detail.message 一致，作为缺失时的兜底）。 */
export const LIVE_MARKET_DATA_UNAVAILABLE_MESSAGE = '实时分钟行情暂不可用，请稍后重试'

/** 机器可读错误码。 */
export const LIVE_MARKET_DATA_UNAVAILABLE_CODE = 'LIVE_MARKET_DATA_UNAVAILABLE'

export class LiveMarketDataUnavailableError extends Error {
  readonly code = LIVE_MARKET_DATA_UNAVAILABLE_CODE
  readonly status = 503
  readonly timeframe: string | null
  readonly providerFamily: string | null

  constructor(
    message: string = LIVE_MARKET_DATA_UNAVAILABLE_MESSAGE,
    timeframe: string | null = null,
    providerFamily: string | null = null,
  ) {
    super(message)
    this.name = 'LiveMarketDataUnavailableError'
    this.timeframe = timeframe
    this.providerFamily = providerFamily
  }
}

/** 后端 503 detail 的判别式（只对 chart-snapshot 生效）。 */
export function isLiveMarketDataUnavailableDetail(body: unknown): body is {
  code: string
  timeframe?: string
  provider_family?: string | null
  message?: string
} {
  if (typeof body !== 'object' || body === null) return false
  return (body as { code?: unknown }).code === LIVE_MARKET_DATA_UNAVAILABLE_CODE
}
