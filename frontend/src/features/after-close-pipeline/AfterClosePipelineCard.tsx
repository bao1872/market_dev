// 盘后流水线摘要卡 - 系统概览第二层入口卡
//
// 用法：
// 1. 在 AdminIndexPage 中嵌入，数据来自 useAdminSystemOverview().after_close_pipeline
// 2. 摘要展示：状态 pill / 业务日期 / 编排阶段 / watchlist_ready / Worker 心跳
// 3. 操作按钮：
//    - 更新今日日线并计算选股（POST /admin/after-close-runs，原 create）
//    - 从DSA重算（POST /admin/after-close-runs/{id}/force?restart_from=daily_ready，需覆盖率≥90%）
//    - 从失败步骤继续（POST /admin/after-close-runs/{id}/resume，仅失败状态显示）
//    - 强制执行（POST /admin/after-close-runs/{id}/force，二次确认）
// 4. 进入详情页链接 → /admin/after-close（8 步骤时间线 + 数据新鲜度 + 运行列表 + 事件抽屉）
//
// 依赖 hooks：
// - useCreateAfterCloseRun：创建盘后编排（POST /admin/after-close-runs）
// - useRetryAfterCloseRun：重试失败任务（POST /admin/after-close-runs/{id}/retry）
// - useResumeAfterCloseRun：从失败步骤继续（POST /admin/after-close-runs/{id}/resume）
// - useForceAfterCloseRun：强制重新执行（POST /admin/after-close-runs/{id}/force）
// - useAfterCloseRunStatus：轮询编排详情（worker/心跳/租约/检查点）

import { useState } from 'react'
import { Link } from 'react-router-dom'
import {
  useCreateAfterCloseRun,
  useRetryAfterCloseRun,
  useResumeAfterCloseRun,
  useForceAfterCloseRun,
  useAfterCloseRunStatus,
} from '@/hooks/useApi'
import { useToast } from '@/store/toast'
import { shanghaiBusinessDate, formatShanghaiTime } from '@/utils/datetime'
import { formatAdminApiError } from '@/utils/adminErrors'
import type { SystemOverview } from '@/api/endpoints'

// [AfterClosePipelineCard] - 状态 → pill 样式映射
function statusPillClass(status: string | undefined): string {
  if (!status) return 'off'
  if (status === 'PUBLISHED') return 'ok'
  const failedStates = ['BARS_FAILED', 'DSA_FAILED', 'STALE']
  if (failedStates.includes(status)) return 'error'
  const runningStates = ['BARS_RUNNING', 'DSA_RUNNING']
  if (runningStates.includes(status)) return 'warn'
  return 'off'
}

// [AfterClosePipelineCard] - 状态中文标签
function statusLabel(status: string | undefined): string {
  switch (status) {
    case 'NOT_STARTED': return '未开始'
    case 'BARS_RUNNING': return '行情更新中'
    case 'BARS_FAILED': return '行情失败'
    case 'WAITING_DSA': return '等待DSA'
    case 'DSA_QUEUED': return 'DSA排队'
    case 'DSA_RUNNING': return 'DSA计算中'
    case 'DSA_COMPLETED': return 'DSA完成'
    case 'DSA_FAILED': return 'DSA失败'
    case 'PUBLISHED': return '已发布'
    case 'STALE': return '过期'
    default: return '-'
  }
}

interface AfterClosePipelineCardProps {
  /** 从 SystemOverview.after_close_pipeline 获取的流水线数据 */
  pipeline: SystemOverview['after_close_pipeline'] | null
  /** 盘后编排任务 ID（用于重试/强制按钮，系统概览未提供时为 null） */
  jobRunId?: string | null
  /** 交易日期（创建按钮使用，默认取上海当前业务日期） */
  tradeDate?: string
  /** 是否加载中 */
  loading?: boolean
}

export function AfterClosePipelineCard({
  pipeline,
  jobRunId = null,
  tradeDate,
  loading = false,
}: AfterClosePipelineCardProps) {
  const toast = useToast.getState()
  const createMutation = useCreateAfterCloseRun()
  const retryMutation = useRetryAfterCloseRun()
  const resumeMutation = useResumeAfterCloseRun()
  const forceMutation = useForceAfterCloseRun()
  // [Phase7] - 轮询盘后编排详情（worker/心跳/租约/检查点/中断原因），10s 间隔
  // jobRunId 为 null 时不启用查询（与按钮 disabled 条件一致）
  const afterCloseDetail = useAfterCloseRunStatus(jobRunId).data

  const [confirmingForce, setConfirmingForce] = useState(false)

  const status = pipeline?.status

  // [Phase6] - 失败状态：BARS_FAILED/DSA_FAILED/STALE 时显示重试 + resume 按钮
  const isFailedState = status === 'BARS_FAILED' || status === 'DSA_FAILED' || status === 'STALE'
  const canRetry = !!jobRunId && isFailedState
  const canResume = !!jobRunId && isFailedState
  const canForce = !!jobRunId

  // [AfterClose] - 当天已有 queued/running 编排任务时禁用创建按钮（避免触发 409 DUPLICATE_RUN）
  const orchestratorJobStatus = afterCloseDetail?.status
  const hasActiveAfterCloseRun =
    orchestratorJobStatus === 'queued' || orchestratorJobStatus === 'running'

  const handleCreate = async () => {
    const date = tradeDate || shanghaiBusinessDate()
    try {
      const result = await createMutation.mutateAsync(date)
      toast.show('任务已加入队列', result.message)
    } catch (err: unknown) {
      // [R14] 统一经 formatAdminApiError 消费结构化错误（含 recommended_action）
      toast.show('创建失败', formatAdminApiError(err))
    }
  }

  const handleRetry = async () => {
    if (!jobRunId) return
    try {
      const result = await retryMutation.mutateAsync(jobRunId)
      toast.show('重试已启动', result.message)
    } catch (err: unknown) {
      // [R14] 统一消费结构化错误（run_not_found / not_retryable 等）
      toast.show('重试失败', formatAdminApiError(err))
    }
  }

  const handleResume = async () => {
    if (!jobRunId) return
    try {
      const result = await resumeMutation.mutateAsync(jobRunId)
      toast.show('已从断点恢复', result.message)
    } catch (err: unknown) {
      // [R14] 统一消费结构化错误（修复旧实现将后端对象声明为 string 导致 Toast 收到对象的问题）
      toast.show('恢复失败', formatAdminApiError(err))
    }
  }

  const handleForce = async () => {
    if (!jobRunId) return
    if (!confirmingForce) {
      setConfirmingForce(true)
      return
    }
    setConfirmingForce(false)
    try {
      const result = await forceMutation.mutateAsync({ runId: jobRunId })
      toast.show('强制执行已启动', result.message)
    } catch (err: unknown) {
      // [R14] 统一消费结构化错误（bad_request / coverage_insufficient 等）
      toast.show('强制执行失败', formatAdminApiError(err))
    }
  }

  // [CHANGE-20260728-008] 从 DSA 阶段重算（替代原 dsa-only 独立端点）
  // 调用 force?restart_from=daily_ready，跳过日线刷新，需覆盖率≥90%
  const handleForceFromDsa = async () => {
    if (!jobRunId) return
    try {
      const result = await forceMutation.mutateAsync({
        runId: jobRunId,
        restartFrom: 'daily_ready',
      })
      toast.show('DSA 重算已启动', result.message)
    } catch (err: unknown) {
      // [R14] 统一经 formatAdminApiError 消费结构化错误（coverage_insufficient 等）
      toast.show('DSA 重算失败', formatAdminApiError(err))
    }
  }

  return (
    <section className="card">
      <div className="card-head">
        <div>
          <div className="card-title">盘后流水线</div>
          <div className="card-sub">
            {loading ? '加载中…' : `状态: ${statusLabel(status)}`}
          </div>
        </div>
        <div className="actions after-close-actions">
          <button
            className="btn small primary"
            onClick={handleCreate}
            disabled={createMutation.isPending || hasActiveAfterCloseRun}
            title={
              hasActiveAfterCloseRun
                ? '当天已有盘后任务正在运行'
                : '更新今日日线并计算选股（完整流水线）'
            }
          >
            {createMutation.isPending ? '创建中…' : '更新日线并选股'}
          </button>
          <button
            className="btn small"
            onClick={handleForceFromDsa}
            disabled={forceMutation.isPending || !canForce}
            title="从 DSA 阶段重算（跳过日线刷新，需当日覆盖率≥90%）"
          >
            {forceMutation.isPending ? '重算中…' : '从DSA重算'}
          </button>
          {canResume && (
            <button
              className="btn small"
              onClick={handleResume}
              disabled={resumeMutation.isPending}
              title="从失败步骤继续（保留断点，不重复拉行情）"
            >
              {resumeMutation.isPending ? '恢复中…' : '从断点继续'}
            </button>
          )}
          {canRetry && (
            <button
              className="btn small"
              onClick={handleRetry}
              disabled={retryMutation.isPending}
              title="重试（从头执行，重置检查点）"
            >
              {retryMutation.isPending ? '重试中…' : '重试'}
            </button>
          )}
          {canForce && (
            <button
              className="btn small"
              onClick={handleForce}
              disabled={forceMutation.isPending}
              title="强制重新执行（任何状态都可触发，需二次确认）"
            >
              {forceMutation.isPending
                ? '执行中…'
                : confirmingForce
                  ? '确认强制执行？'
                  : '强制执行'}
            </button>
          )}
        </div>
      </div>
      <div className="card-body">
        {/* 摘要行：状态 pill + 编排阶段 + Worker 心跳 + 进入详情链接 */}
        <div className="toggle-row">
          <span>流水线状态</span>
          <b>
            <span className={`status-pill ${statusPillClass(status)}`}>
              {statusLabel(status)}
            </span>
          </b>
        </div>
        <div className="toggle-row">
          <span>编排阶段</span>
          <b className="num">
            {loading ? '-' : (pipeline?.orchestrator_status ?? '-')}
          </b>
        </div>
        <div className="toggle-row">
          <span>Worker 心跳</span>
          <b className="num">
            {loading
              ? '-'
              : afterCloseDetail?.heartbeat_at
                ? `${formatShanghaiTime(afterCloseDetail.heartbeat_at)}${
                    afterCloseDetail.heartbeat_stale ? '（超时）' : ''
                  }`
                : '无记录'}
          </b>
        </div>
        <div className="toggle-row">
          <span>最后成功步骤</span>
          <b className="num">
            {loading ? '-' : (afterCloseDetail?.last_completed_step ?? '-')}
          </b>
        </div>
        <div className="toggle-row">
          <span>行情更新至</span>
          <b className="num">
            {loading
              ? '-'
              : pipeline?.data_freshness?.bars?.latest_daily_trade_date ?? '-'}
          </b>
        </div>
        <div className="toggle-row">
          <span>选股发布至</span>
          <b className="num">
            {loading
              ? '-'
              : pipeline?.data_freshness?.strategy?.latest_published_trade_date ?? '-'}
          </b>
        </div>
        {/* WAITING_DSA 提示（保留，便于管理员快速发现阻塞原因） */}
        {pipeline?.waiting_dsa_reason && (
          <div className="pipeline-waiting-notice">
            <b>等待 DSA: {pipeline.waiting_dsa_reason}</b>
            {pipeline.waiting_dsa_suggestion && (
              <span>建议: {pipeline.waiting_dsa_suggestion}</span>
            )}
          </div>
        )}
        {/* 错误信息（失败状态时展示） */}
        {isFailedState && pipeline?.dsa_run?.error_message && (
          <div className="notice error" style={{ marginTop: '10px' }}>
            {pipeline.dsa_run.error_message}
          </div>
        )}
        <Link className="btn small card-body-action" to="/admin/after-close">
          查看流水线详情 →
        </Link>
      </div>
    </section>
  )
}
