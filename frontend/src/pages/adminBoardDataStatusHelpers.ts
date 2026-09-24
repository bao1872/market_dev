// [BOARD-LOCAL-OWNERSHIP-01] 板块/概念「本地手动同步」状态的纯展示 helper。
//
// 设计说明：
// - 纯函数 + 常量，无 React 依赖，可被 node --experimental-strip-types 直接导入测试。
// - 语义边界（与后端一致）：**无**过期/超时/必须今日更新/X 天未更新判定；
//   最后成功时间只作信息展示，旧的 last_success_at 不使数据"失效"。
// - 最近一次尝试失败但库中仍有有效数据 → 数据仍可用，仅提示"仍使用上一次成功数据"。

import type { AdminBoardSyncStatusResponse } from '@/api/admin'

export const BOARD_SYNC_HELP_TEXT = '在本地仓库执行：scripts/ops/panji-board-sync'
export const BOARD_SYNC_UPDATE_MODE = '本地手动同步'
export const BOARD_SYNC_SOURCE = '问财'
export const BOARD_SYNC_STALE_REUSE_NOTICE =
  '最近一次同步尝试失败，当前仍使用上一次成功数据'

export interface BoardSyncRecentAttemptView {
  isFailed: boolean
  statusText: string
  timeText: string
  errorCode: string | null
}

export interface BoardSyncDisplayModel {
  updateMode: string
  source: string
  available: boolean
  lastSuccessText: string
  boardCount: number
  industryCount: number
  conceptCount: number
  membershipCount: number
  stockCount: number
  recentAttempt: BoardSyncRecentAttemptView | null
  dataNotice: string | null
  helpText: string
}

/** 把 ISO 时间格式化为本地可读串；无效/缺失返回占位。 */
export function formatBoardSyncTime(iso: string | null | undefined): string {
  if (!iso) return '暂无'
  const dt = new Date(iso)
  if (Number.isNaN(dt.getTime())) return iso
  return dt.toLocaleString('zh-CN', { hour12: false })
}

function attemptStatusText(status: string | undefined): string {
  if (status === 'succeeded') return '成功'
  if (status === 'failed') return '失败'
  if (status === 'skipped') return '已跳过'
  return status ? status : '未知'
}

/** 把后端 board-sync 状态读模型转为纯展示模型。 */
export function buildBoardSyncDisplay(
  status: AdminBoardSyncStatusResponse,
): BoardSyncDisplayModel {
  const attempt = status.recent_attempt
  const recentAttempt: BoardSyncRecentAttemptView | null = attempt
    ? {
        isFailed: attempt.status === 'failed',
        statusText: attemptStatusText(attempt.status),
        timeText: formatBoardSyncTime(attempt.completed_at as string | undefined),
        errorCode: (attempt.error_code as string | null | undefined) ?? null,
      }
    : null

  // 数据可用性只由 DB 快照决定；最近一次尝试失败不改判可用性。
  const dataNotice =
    status.available && recentAttempt?.isFailed
      ? BOARD_SYNC_STALE_REUSE_NOTICE
      : null

  return {
    updateMode: BOARD_SYNC_UPDATE_MODE,
    source: BOARD_SYNC_SOURCE,
    available: status.available,
    lastSuccessText: formatBoardSyncTime(status.last_success_at),
    boardCount: status.board_count,
    industryCount: status.industry_count,
    conceptCount: status.concept_count,
    membershipCount: status.membership_count,
    stockCount: status.stock_count,
    recentAttempt,
    dataNotice,
    helpText: BOARD_SYNC_HELP_TEXT,
  }
}
