// [USER-FIX-3 / A2] - 描述: /market 批次元数据（published-runs）与行情主数据的依赖解耦 owner
//
// 背景（真实 dev runtime 已复现）：
//   普通 member（capabilities = {self_selection, market_data}，无 research_replay）在 /market：
//     GET /v1/market/stocks?scope=market      → 200（rows 已就绪）
//     GET /v1/strategies/.../published-runs   → 403「需要权限: research_replay」
//   而 MarketWorkspacePage 当时把 runsQuery.isLoading / runsQuery.isError 并入了表格的
//   loading/error，导致一个**与普通用户无关的辅助诊断查询**遮断了已经成功返回的行情主数据
//   （表格只剩「运行批次加载失败」错误行，0 行数据）。
//
// 冻结的数据依赖关系：
//   PRIMARY（主数据）
//     /v1/market/stocks → rows / total / table loading / table error
//   OPTIONAL ADMIN DIAGNOSTIC（仅 admin 消费）
//     /v1/strategies/{key}/published-runs → batch meta only
//
// 由此推出三条硬约束，全部在本模块以纯函数形式固定，并由 __tests__/marketBatchMetaGate.test.ts
// 行为化验证（不是字符串 grep）：
//   1. 非 admin 不得发起 published-runs 请求；
//   2. 辅助查询的 loading/error 不得影响行情表；
//   3. 非 admin 不得消费（可能来自管理员会话的）published-runs 旧缓存。
//
// 权限模型不在此处修正：普通用户没有 research_replay 是合法状态，
// 禁止通过给用户补权限 / 放宽后端 guard 来绕过本问题。

/** 批次元数据门控输入（全部来自 auth store 的权威状态） */
export interface BatchMetaGateInput {
  /** accessStatus === 'ready'（capability 已解析，route guard 的同一真源） */
  accessReady: boolean
  /** 真实 is_admin（非视图层 role 切换） */
  isAdmin: boolean
  /** 页面消费的 DSA 策略 key */
  strategyKey: string
}

/**
 * 解析应传给 `usePublishedRuns` 的 strategyKey。
 *
 * 返回 `undefined` 时，hook 内 `enabled: !!strategyKey` 成立为 false ⇒ 不发起任何请求。
 * 做不到「条件调用 Hook」，因此统一用「传 undefined」这一既有合同实现门控，
 * 不新增第二套 enabled 开关。
 *
 * 判据刻意选 `accessReady && isAdmin`：
 *   - published-runs 在页面上的唯一消费方是顶部「批次信息」面板，该面板本身 `仅 admin 可见`
 *     （见 MarketWorkspacePage 的 `isAdmin && batchMeta`）；
 *   - 因此不给「有 research_replay 的普通用户」加载页面根本不会展示的数据；
 *   - `accessReady` 前置可保证 capability 未解析完成前不偷跑任何请求。
 */
export function resolveBatchMetaStrategyKey(input: BatchMetaGateInput): string | undefined {
  if (!input.accessReady) return undefined
  if (!input.isAdmin) return undefined
  return input.strategyKey
}

/** 批次元数据条目选择输入 */
export interface BatchMetaItemsInput<T> {
  /** 门控结果（= resolveBatchMetaStrategyKey(...) !== undefined） */
  shouldLoadBatchMeta: boolean
  /** usePublishedRuns 的 data（可能来自上一账号会话的 Query 缓存） */
  data: { items?: T[] } | undefined
}

/**
 * 选择可用于渲染的批次元数据条目。
 *
 * 门控关闭时**无条件返回空数组**：Query 缓存是跨账号复用的，
 * 若只做到「disabled query」但仍消费 `runsQuery.data`，
 * 就会出现「管理员登录过 → logout → 普通用户登录 → 仍显示管理员批次信息」的泄漏。
 */
export function selectBatchMetaItems<T>(input: BatchMetaItemsInput<T>): T[] {
  if (!input.shouldLoadBatchMeta) return []
  return input.data?.items ?? []
}

/** 行情表状态输入（[S2-A] 主表只接收主数据源，根本不接收 batchMeta* 输入） */
export interface MarketTableStateInput {
  /** 主数据源 loading（/v1/market/stocks） */
  marketStocksLoading: boolean
  /** 主数据源 error 文案（null = 无错误） */
  marketStocksError: string | null
}

/** 行情表状态（只由主数据源决定） */
export interface MarketTableState {
  loading: boolean
  error: string | null
}

/**
 * 解析行情表的 loading / error。
 *
 * [S2-A] 输入现在只含主数据源字段；辅助查询（published-runs）状态根本不是本函数的入参，
 * 因此不可能再被并入行情表的 loading/error。这是「主表根本没有这个输入」的解耦，
 * 而非「我知道 auxiliary error，但选择忽略」。
 */
export function resolveMarketTableState(input: MarketTableStateInput): MarketTableState {
  return {
    loading: input.marketStocksLoading,
    error: input.marketStocksError,
  }
}

/**
 * 把 /v1/market/stocks 的异常映射为用户可见文案。
 *
 * 主数据失败必须显式失败：不得因为「辅助错误不阻塞表格」而把主数据错误也吞掉。
 * 保留既有 20260730-012 合同：422 展示后端 detail，500 展示 request_id。
 */
export function describeMarketStocksError(err: unknown): string {
  const e = err as
    | {
        response?: {
          status?: number
          data?: { detail?: string }
          headers?: { get?: (k: string) => string | null }
        }
      }
    | undefined
  const status = e?.response?.status
  if (status === 422) {
    return `筛选/排序参数无效：${e?.response?.data?.detail ?? '未知错误'}`
  }
  if (status === 500) {
    const reqId = e?.response?.headers?.get?.('x-request-id')
    return `服务器错误${reqId ? `（request_id=${reqId}）` : ''}`
  }
  return `行情列表加载失败：${status ?? '网络错误'}`
}
