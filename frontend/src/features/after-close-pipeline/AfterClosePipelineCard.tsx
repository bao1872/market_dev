// 盘后流水线摘要卡 - 系统概览第二层入口卡
//
// 用法：
// 1. 在 AdminIndexPage 中嵌入，数据来自 useAdminSystemOverview().after_close_pipeline
// 2. 摘要展示：状态 pill / 业务日期 / 编排阶段 / watchlist_ready / Worker 心跳
// 3. 操作按钮（4 个独立入口）：
//    - 更新今日日线并计算选股（POST /admin/after-close-runs，原 create）
//    - 仅重算今日选股（POST /admin/after-close-runs/dsa-only，要求覆盖率 ≥ 90%）
//    - 从失败步骤继续（POST /admin/after-close-runs/{id}/resume，仅失败状态显示）
//    - 强制执行（POST /admin/after-close-runs/{id}/force，二次确认）
// 4. 进入详情页链接 → /admin/after-close（8 步骤时间线 + 数据新鲜度 + 运行列表 + 事件抽屉）
//
// 依赖 hooks：
// - useCreateAfterCloseRun：创建盘后编排（POST /admin/after-close-runs）
// - useDsaOnlyRun：仅重算今日 DSA（POST /admin/after-close-runs/dsa-only）
// - useRetryAfterCloseRun：重试失败任务（POST /admin/after-close-runs/{id}/retry）
// - useResumeAfterCloseRun：从失败步骤继续（POST /admin/after-close-runs/{id}/resume）
// - useForceAfterCloseRun：强制重新执行（POST /admin/after-close-runs/{id}/force）
// - useAfterCloseRunStatus：轮询编排详情（worker/心跳/租约/检查点）

import { useState } from 'react'
import { Link } from 'react-router-dom'
import {
  useCreateAfterCloseRun,
  useDsaOnlyRun,
  useRetryAfterCloseRun,
  useResumeAfterCloseRun,
  useForceAfterCloseRun,
  useAfterCloseRunStatus,
} from '@/hooks/useApi'
import { useToast } from '@/store/toast'
import { shanghaiBusinessDate, formatShanghaiTime } from '@/utils/datetime'
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

// [AfterClose] - 创建盘后编排 409 detail → 人类可读消息（透明化真实失败原因）
// 后端 409 来源：NON_TRADING_DAY（非交易日）/ DUPLICATE_RUN（同日已有 queued/running 任务）/
// DATA_COVERAGE_INSUFFICIENT（覆盖率不足，dsa-only 复用此 formatter）
function formatAfterCloseCreate409Message(detail: unknown): string {
  if (typeof detail === 'string') return detail
  if (detail && typeof detail === 'object') {
    const d = detail as {
      error_code?: string
      reason?: string
      message?: string
      orchestrator_status?: string
      started_at?: string
    }
    if (d.error_code === 'NON_TRADING_DAY' || d.reason === 'NON_TRADING_DAY') {
      return d.message ?? '非交易日无需执行'
    }
    if (d.error_code === 'DUPLICATE_RUN') {
      const stage = d.orchestrator_status ? `（当前阶段: ${d.orchestrator_status}）` : ''
      const start = d.started_at ? `，开始于 ${formatShanghaiTime(d.started_at)}` : ''
      return `当天已有盘后任务正在运行${stage}${start}`
    }
    if (d.reason === 'DATA_COVERAGE_INSUFFICIENT') {
      return d.message ?? '行情覆盖率不足，暂无法执行'
    }
    return d.message ?? '创建失败，请稍后重试'
  }
  return '创建失败，请稍后重试'
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
  const dsaOnlyMutation = useDsaOnlyRun()
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
      const axiosErr = err as {
        response?: { status?: number; data?: { detail?: unknown } }
      }
      const respStatus = axiosErr.response?.status
      const detail = axiosErr.response?.data?.detail
      if (respStatus === 409) {
        toast.show('创建失败', formatAfterCloseCreate409Message(detail))
      } else {
        toast.show('创建失败', '请稍后重试')
      }
    }
  }

  const handleDsaOnly = async () => {
    const date = tradeDate || shanghaiBusinessDate()
    try {
      const result = await dsaOnlyMutation.mutateAsync(date)
      toast.show('DSA 重算已创建', result.message)
    } catch (err: unknown) {
      const axiosErr = err as { response?: { status?: number; data?: { detail?: unknown } } }
      const respStatus = axiosErr.response?.status
      const detail = axiosErr.response?.data?.detail
      if (respStatus === 409) {
        toast.show('DSA 重算失败', formatAfterCloseCreate409Message(detail))
      } else {
        toast.show('DSA 重算失败', '请稍后重试')
      }
    }
  }

  const handleRetry = async () => {
    if (!jobRunId) return
    try {
      const result = await retryMutation.mutateAsync(jobRunId)
      toast.show('重试已启动', result.message)
    } catch {
      toast.show('重试失败', '请稍后重试')
    }
  }

  const handleResume = async () => {
    if (!jobRunId) return
    try {
      const result = await resumeMutation.mutateAsync(jobRunId)
      toast.show('已从断点恢复', result.message)
    } catch (err: unknown) {
      const axiosErr = err as { response?: { status?: number; data?: { detail?: string } } }
      const message = axiosErr.response?.data?.detail ?? '请稍后重试'
      toast.show('恢复失败', message)
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
      const result = await forceMutation.mutateAsync(jobRunId)
      toast.show('强制执行已启动', result.message)
    } catch {
      toast.show('强制执行失败', '请稍后重试')
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
            onClick={handleDsaOnly}
            disabled={dsaOnlyMutation.isPending}
            title="仅重算今日 DSA（要求当日日线覆盖率 ≥ 90%）"
          >
            {dsaOnlyMutation.isPending ? '重算中…' : '仅重算选股'}
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
