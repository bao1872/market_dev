// 盘后流水线状态卡片 - 7 阶段进度 + WAITING_DSA 提示 + 操作按钮
//
// 用法：
// 1. 在 AdminIndexPage 中嵌入，数据来自 useAdminSystemOverview().after_close_pipeline
// 2. 显示 7 阶段进度条（NOT_STARTED → BARS_RUNNING → WAITING_DSA → DSA_QUEUED → DSA_RUNNING → DSA_COMPLETED → PUBLISHED）
// 3. WAITING_DSA 状态时展示细分原因 + 人类可读建议
// 4. 操作按钮：创建（始终可用）/ 重试（需 jobRunId + 失败状态）/ 强制（需 jobRunId）
//
// 依赖 hooks：
// - useCreateAfterCloseRun：创建盘后编排（POST /admin/after-close-runs）
// - useRetryAfterCloseRun：重试失败任务（POST /admin/after-close-runs/{id}/retry）
// - useForceAfterCloseRun：强制重新执行（POST /admin/after-close-runs/{id}/force）

import { useState } from 'react'
import {
  useCreateAfterCloseRun,
  useRetryAfterCloseRun,
  useForceAfterCloseRun,
} from '@/hooks/useApi'
import { useToast } from '@/store/toast'
import { shanghaiBusinessDate, formatShanghaiTime } from '@/utils/datetime'
import type { SystemOverview } from '@/api/endpoints'

// [AfterClosePipelineCard] - 7 阶段定义（顺序即流水线推进顺序）
const PIPELINE_STAGES = [
  { key: 'NOT_STARTED', label: '未开始' },
  { key: 'BARS_RUNNING', label: '行情更新' },
  { key: 'WAITING_DSA', label: '等待DSA' },
  { key: 'DSA_QUEUED', label: 'DSA排队' },
  { key: 'DSA_RUNNING', label: 'DSA计算' },
  { key: 'DSA_COMPLETED', label: 'DSA完成' },
  { key: 'PUBLISHED', label: '已发布' },
] as const

// [AfterClosePipelineCard] - 状态 → 阶段索引 + 状态样式映射
function statusToStage(status: string | undefined): {
  index: number
  state: 'done' | 'active' | 'error'
} {
  if (!status) return { index: 0, state: 'active' }
  const failedStates = ['BARS_FAILED', 'DSA_FAILED', 'STALE']
  if (failedStates.includes(status)) {
    // BARS_FAILED → 阶段 1（行情更新失败）
    // DSA_FAILED → 阶段 4（DSA 计算失败）
    // STALE → 阶段 0（整体过期）
    if (status === 'BARS_FAILED') return { index: 1, state: 'error' }
    if (status === 'DSA_FAILED') return { index: 4, state: 'error' }
    return { index: 0, state: 'error' }
  }
  const idx = PIPELINE_STAGES.findIndex((s) => s.key === status)
  if (idx < 0) return { index: 0, state: 'active' }
  // PUBLISHED 是终态，所有阶段标记为 done
  if (status === 'PUBLISHED') return { index: PIPELINE_STAGES.length - 1, state: 'done' }
  return { index: idx, state: 'active' }
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
  const forceMutation = useForceAfterCloseRun()

  const [confirmingForce, setConfirmingForce] = useState(false)

  const status = pipeline?.status
  const { index: currentIndex, state: currentState } = statusToStage(status)

  // WAITING_DSA 提示
  const waitingReason = pipeline?.waiting_dsa_reason
  const waitingSuggestion = pipeline?.waiting_dsa_suggestion

  // 操作按钮可用性
  const canRetry = !!jobRunId && (status === 'BARS_FAILED' || status === 'DSA_FAILED' || status === 'STALE')
  const canForce = !!jobRunId

  // 创建盘后编排
  const handleCreate = async () => {
    const date = tradeDate || shanghaiBusinessDate()
    try {
      const result = await createMutation.mutateAsync(date)
      toast.show('盘后编排已创建', result.message)
    } catch {
      toast.show('创建失败', '请稍后重试或检查权限')
    }
  }

  // 重试失败任务
  const handleRetry = async () => {
    if (!jobRunId) return
    try {
      const result = await retryMutation.mutateAsync(jobRunId)
      toast.show('重试已启动', result.message)
    } catch {
      toast.show('重试失败', '请稍后重试')
    }
  }

  // 强制重新执行
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
            {loading ? '加载中…' : `状态: ${status ?? '-'}`}
          </div>
        </div>
        <div className="actions">
          <button
            className="btn small primary"
            onClick={handleCreate}
            disabled={createMutation.isPending}
          >
            {createMutation.isPending ? '创建中…' : '创建盘后编排'}
          </button>
          {canRetry && (
            <button
              className="btn small"
              onClick={handleRetry}
              disabled={retryMutation.isPending}
            >
              {retryMutation.isPending ? '重试中…' : '重试'}
            </button>
          )}
          {canForce && (
            <button
              className="btn small"
              onClick={handleForce}
              disabled={forceMutation.isPending}
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
        {/* 7 阶段进度条 */}
        <div className="pipeline-steps">
          {PIPELINE_STAGES.map((stage, i) => {
            let cls = ''
            if (currentState === 'error' && i === currentIndex) {
              cls = 'error'
            } else if (i < currentIndex || (currentState === 'done' && i === currentIndex)) {
              cls = 'done'
            } else if (i === currentIndex) {
              cls = 'active'
            }
            return (
              <div key={stage.key} className={`pipeline-step ${cls}`}>
                <b>{stage.label}</b>
                {stage.key === status && <span>{stage.key === status ? '●' : ''}</span>}
              </div>
            )
          })}
        </div>

        {/* bars_job / dsa_run 摘要 */}
        {pipeline?.bars_job && (
          <div className="toggle-row">
            <span>bars_scheduler</span>
            <b className="num">
              {pipeline.bars_job.status ?? '-'}
              {pipeline.bars_job.finished_at
                ? ` · ${formatShanghaiTime(pipeline.bars_job.finished_at)}`
                : ''}
            </b>
          </div>
        )}
        {pipeline?.dsa_run && (
          <div className="toggle-row">
            <span>DSA 状态</span>
            <b className="num">
              {pipeline.dsa_run.status ?? '-'}
              {pipeline.dsa_run.error_message ? ` · ${pipeline.dsa_run.error_message}` : ''}
            </b>
          </div>
        )}

        {/* WAITING_DSA 提示 */}
        {waitingReason && (
          <div className="pipeline-waiting-notice">
            <b>等待 DSA: {waitingReason}</b>
            {waitingSuggestion && <span>建议: {waitingSuggestion}</span>}
          </div>
        )}

        {/* 错误信息 */}
        {currentState === 'error' && pipeline?.dsa_run?.error_message && (
          <div className="notice error" style={{ marginTop: '10px' }}>
            {pipeline.dsa_run.error_message}
          </div>
        )}
      </div>
    </section>
  )
}
