// [reasonCodeMessages] - 描述: 状态观察面板 reasonCode → 用户文案映射（纯函数，可测试）
// 测试 9: 前端 reasonCode 文案覆盖
// 每种 reasonCode 必须返回明确的标题和可选的数据日期 meta，禁止统一显示"暂无可用状态数据"。

export interface ReasonCodeMessage {
  title: string
  meta?: string
}

export function getReasonCodeMessage(
  reasonCode: string | null,
  runTradeDate: string | null,
): ReasonCodeMessage | null {
  switch (reasonCode) {
    case 'no_published_full_run':
      return { title: '尚未有盘后快照发布，状态数据将在下一个交易日盘后生成' }
    case 'snapshot_missing':
      return {
        title: '该股票暂无快照数据',
        meta: runTradeDate ? `最新 run 日期：${runTradeDate}` : undefined,
      }
    case 'snapshot_run_not_linked':
      return {
        title: '快照数据存在但尚未关联发布批次',
        meta: runTradeDate ? `run 日期：${runTradeDate}（待修复归属）` : undefined,
      }
    case 'legacy_snapshot_ambiguous':
      return {
        title: '快照数据归属不明确',
        meta: runTradeDate ? `run 日期：${runTradeDate}` : undefined,
      }
    case null:
      return { title: '暂无可用状态数据' }
    default:
      return null
  }
}
