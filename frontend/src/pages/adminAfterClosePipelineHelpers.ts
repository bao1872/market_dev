// [Phase8A] 盘后流水线纯函数 helpers（从 AdminAfterClosePipelinePage.tsx 提取）
// 用法：被 AdminAfterClosePipelinePage.tsx 导入；被 __tests__/adminAfterClosePipeline.test.ts 测试
//
// 设计说明：
// - 纯函数 + 常量，无 React 依赖，可被 node --experimental-strip-types 直接导入测试
// - stepLabel/getStepKeys 以 API 返回为主，不硬编码步骤数量
// - legacy 四状态（creating_dsa/waiting_dsa_worker/quality_gate/feature_snapshot）
//   不出现在 STEP_LABELS / DEFAULT_STEP_ORDER 中，后端映射为 computing_features 后前端只展示 1 步

import type { PipelineStep } from '@/api/endpoints'

// ===== 步骤标签映射 =====
export const STEP_LABELS: Record<string, string> = {
  refreshing_daily: '刷新日线',
  syncing_boards: '同步板块',
  checking_coverage: '检查覆盖率',
  computing_features: '统一特征计算',
  publishing: '发布结果',
  watchlist_ready: '自选可用',
}

// 默认步骤顺序（API 未返回 steps 或步骤缺失时的兜底）
export const DEFAULT_STEP_ORDER: string[] = [
  'refreshing_daily',
  'syncing_boards',
  'checking_coverage',
  'computing_features',
  'publishing',
  'watchlist_ready',
]

// legacy 四状态（已收敛为 computing_features，不应出现在新 API steps 中）
export const LEGACY_STEP_KEYS: string[] = [
  'creating_dsa',
  'waiting_dsa_worker',
  'quality_gate',
  'feature_snapshot',
]

export function stepLabel(key: string): string {
  return STEP_LABELS[key] || key
}

// ===== overall_status → 中文标签 =====
export function overallStatusLabel(status: string | undefined): string {
  switch (status) {
    case 'not_started': return '未开始'
    case 'running': return '运行中'
    case 'succeeded': return '成功'
    case 'failed': return '失败'
    case 'blocked': return '阻塞'
    case 'skipped': return '跳过（非交易日）'
    default: return '-'
  }
}

export function overallStatusPillClass(status: string | undefined): string {
  switch (status) {
    case 'succeeded': return 'ok'
    case 'running': return 'warn'
    case 'failed':
    case 'blocked': return 'error'
    case 'skipped':
    case 'not_started':
    default: return 'off'
  }
}

// ===== market_session → 中文标签 =====
export function marketSessionLabel(session: string | undefined): string {
  switch (session) {
    case 'NON_TRADING_DAY': return '非交易日'
    case 'PRE_OPEN': return '盘前'
    case 'MORNING_SESSION': return '上午盘'
    case 'LUNCH_BREAK': return '午间休市'
    case 'AFTERNOON_SESSION': return '下午盘'
    case 'MARKET_CLOSED': return '已收盘'
    default: return '-'
  }
}

// ===== 步骤状态 → 中文标签 + 样式 =====
export function stepStatusLabel(status: string): string {
  switch (status) {
    case 'pending': return '待执行'
    case 'running': return '执行中'
    case 'completed': return '已完成'
    case 'failed': return '失败'
    case 'skipped': return '跳过'
    default: return '-'
  }
}

export function stepStatusClass(status: string): string {
  switch (status) {
    case 'completed': return 'done'
    case 'running': return 'active'
    case 'failed': return 'error'
    case 'skipped': return 'skipped'
    default: return ''
  }
}

// ===== 运行列表项状态 → pill 样式 =====
export function runItemStatusPillClass(status: string): string {
  switch (status) {
    case 'succeeded': return 'ok'
    case 'running':
    case 'queued': return 'warn'
    case 'failed':
    case 'interrupted': return 'error'
    default: return 'off'
  }
}

export function runItemKindLabel(kind: string): string {
  switch (kind) {
    case 'after_close_orchestrator': return '盘后编排'
    case 'snapshot_run': return '特征快照'
    default: return kind
  }
}

// ===== 格式化耗时（秒 → "Xm Ys"）=====
export function formatDurationSeconds(seconds: number | null | undefined): string {
  if (seconds == null) return '-'
  if (seconds < 60) return `${seconds.toFixed(1)}s`
  const m = Math.floor(seconds / 60)
  const s = Math.round(seconds % 60)
  return `${m}m ${s}s`
}

// ===== [Phase8A] 从 API steps 提取步骤 key 列表（以 API 顺序为主）=====
export function getStepKeys(steps: PipelineStep[]): string[] {
  return steps.length > 0 ? steps.map((s) => s.step) : DEFAULT_STEP_ORDER
}

// ===== [Phase8A] 轮询间隔常量 + helper =====
export const PIPELINE_POLL_RUNNING = 10_000 // running 状态 10s
export const PIPELINE_POLL_IDLE = 60_000 // 非 running 状态 60s

/**
 * 根据 overall_status 返回轮询间隔（毫秒）。
 * running → 10s，其余（含 undefined/not_started/succeeded/failed/blocked/skipped）→ 60s。
 */
export function getPipelinePollInterval(status: string | undefined): number {
  return status === 'running' ? PIPELINE_POLL_RUNNING : PIPELINE_POLL_IDLE
}
