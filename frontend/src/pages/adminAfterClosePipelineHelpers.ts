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
  // [CHANGE-20260831-ADMIN-TIMELINE] legacy 兼容标签：publishing 不再是 current canonical 步骤，
  // 此标签仅当 API 显式返回 publishing（历史 legacy run 的真实事件）时用于展示。
  publishing: '发布结果',
  // [SLICE-01-CORRECTION-02] 新增历史状态推进阶段（First Pyramid History 自动生产 + exact-T readiness）
  computing_history: '历史状态推进',
  // [CHANGE-20260801-REVIEW-CLOSURE] 新增复盘计算与发布阶段
  computing_review: '复盘计算发布',
  watchlist_ready: '自选可用',
}

// 默认步骤顺序（API 未返回 steps 或步骤缺失时的兜底）
// [CHANGE-20260831-ADMIN-TIMELINE] 7 步 current canonical 序列：
//   computing_features → computing_review → computing_history → watchlist_ready
// 与后端 after_close_orchestrator._CHECKPOINT_ORDER（features → review → history）保持一致。
// publishing 已从 current canonical DAG 移除：不再作为默认步骤出现；
//   STEP_LABELS.publishing 仅保留给历史 legacy run 的兼容展示（API 显式返回时才渲染）。
export const DEFAULT_STEP_ORDER: string[] = [
  'refreshing_daily',
  'syncing_boards',
  'checking_coverage',
  'computing_features',
  'computing_review',
  'computing_history',
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
    // [AC-TERMINAL-01 2026-08-04] 终态如实显示，不再回落 '-'
    case 'partial_success': return '部分成功'
    case 'cancelled': return '已取消'
    case 'interrupted': return '已中断'
    default: return '-'
  }
}

export function overallStatusPillClass(status: string | undefined): string {
  switch (status) {
    case 'succeeded': return 'ok'
    case 'running': return 'warn'
    // 部分成功：核心已发布但有降级，用 warn 而非 error
    case 'partial_success': return 'warn'
    case 'failed':
    case 'blocked': return 'error'
    // 取消/中断不是失败，用中性样式
    case 'cancelled':
    case 'interrupted': return 'off'
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
    case 'completed':
    case 'succeeded': return '已完成'
    case 'failed': return '失败'
    case 'skipped': return '跳过'
    case 'skipped_unavailable': return '不可用，已跳过'
    case 'cancelled': return '已终止'
    // [AC-TERMINAL-01 2026-08-04] 步骤级终态显式呈现，不再回落"未知"
    case 'timed_out': return '超时'
    case 'unavailable': return '不可用'
    case 'interrupted': return '已中断'
    default: return '未知'
  }
}

export function stepStatusClass(status: string): string {
  switch (status) {
    case 'completed':
    case 'succeeded': return 'done'
    case 'running': return 'active'
    case 'failed':
    // 超时属于异常终态，与 failed 同级提示
    case 'timed_out': return 'error'
    case 'skipped':
    case 'skipped_unavailable':
    case 'unavailable':
    case 'interrupted':
    case 'cancelled': return 'skipped'
    default: return ''
  }
}

// ===== 运行列表项状态 → pill 样式 =====
export function runItemStatusPillClass(status: string): string {
  switch (status) {
    case 'succeeded': return 'ok'
    case 'running':
    case 'queued':
    // [AC-TERMINAL-01 2026-08-04] 部分成功：核心已发布但有降级
    case 'partial_success': return 'warn'
    case 'failed':
    case 'interrupted': return 'error'
    // 取消是管理员主动行为，不标红
    case 'cancelled': return 'off'
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
// [TIMELINE-FIX]
// - duration=null 且 stepStatus="running" → 显示"进行中"
// - duration=null 且 stepStatus!="running" + warnings 含 invalid_order → 显示"未知"（不用 max 掩盖）
// - duration<0（不应出现，仅防御性）→ 显示"未知"
export function formatDurationSeconds(
  seconds: number | null | undefined,
  stepStatus?: string | null,
  warnings?: string[] | null,
): string {
  // 进行中：running 状态且没有 duration
  if ((seconds == null || seconds <= 0) && stepStatus === 'running') {
    return '进行中'
  }
  // 顺序异常或非正耗时：显示"未知"而非 max(0,x)/负数
  if (warnings && warnings.includes('invalid_order_or_zero_duration')) {
    return '未知'
  }
  if (seconds == null) return '-'
  if (seconds <= 0) return '未知'
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
