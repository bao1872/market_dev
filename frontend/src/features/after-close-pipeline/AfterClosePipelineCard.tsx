// 盘后流水线状态卡片 - 7 阶段进度 + WAITING_DSA 提示 + 操作按钮
//
// 用法：
// 1. 在 AdminIndexPage 中嵌入，数据来自 useAdminSystemOverview().after_close_pipeline
// 2. 显示 7 阶段进度条（NOT_STARTED → BARS_RUNNING → WAITING_DSA → DSA_QUEUED → DSA_RUNNING → DSA_COMPLETED → PUBLISHED）
// 3. WAITING_DSA 状态时展示细分原因 + 人类可读建议
// 4. 操作按钮（[Phase6] 拆分为 4 个独立入口）：
//    - 更新今日日线并计算选股（POST /admin/after-close-runs，原 create）
//    - 仅重算今日选股（POST /admin/after-close-runs/dsa-only，要求覆盖率 ≥ 90%）
//    - 从失败步骤继续（POST /admin/after-close-runs/{id}/resume，仅失败状态显示）
//    - 强制执行（POST /admin/after-close-runs/{id}/force，二次确认）
//
// 依赖 hooks：
// - useCreateAfterCloseRun：创建盘后编排（POST /admin/after-close-runs）
// - useDsaOnlyRun：仅重算今日 DSA（POST /admin/after-close-runs/dsa-only）
// - useRetryAfterCloseRun：重试失败任务（POST /admin/after-close-runs/{id}/retry）
// - useResumeAfterCloseRun：从失败步骤继续（POST /admin/after-close-runs/{id}/resume）
// - useForceAfterCloseRun：强制重新执行（POST /admin/after-close-runs/{id}/force）

import { useState } from 'react'
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
  const dsaOnlyMutation = useDsaOnlyRun()
  const retryMutation = useRetryAfterCloseRun()
  const resumeMutation = useResumeAfterCloseRun()
  const forceMutation = useForceAfterCloseRun()
  // [Phase7] - 轮询盘后编排详情（worker/心跳/租约/检查点/中断原因），10s 间隔
  // jobRunId 为 null 时不启用查询（与按钮 disabled 条件一致）
  const afterCloseDetail = useAfterCloseRunStatus(jobRunId).data

  const [confirmingForce, setConfirmingForce] = useState(false)

  const status = pipeline?.status
  const { index: currentIndex, state: currentState } = statusToStage(status)

  // WAITING_DSA 提示
  const waitingReason = pipeline?.waiting_dsa_reason
  const waitingSuggestion = pipeline?.waiting_dsa_suggestion

  // [Phase6] - 失败状态：BARS_FAILED/DSA_FAILED/STALE 时显示重试 + resume 按钮
  const isFailedState = status === 'BARS_FAILED' || status === 'DSA_FAILED' || status === 'STALE'
  const canRetry = !!jobRunId && isFailedState
  const canResume = !!jobRunId && isFailedState
  const canForce = !!jobRunId

  // 创建盘后编排（更新今日日线并计算选股）
  const handleCreate = async () => {
    const date = tradeDate || shanghaiBusinessDate()
    try {
      const result = await createMutation.mutateAsync(date)
      toast.show('盘后编排已创建', result.message)
    } catch {
      toast.show('创建失败', '请稍后重试或检查权限')
    }
  }

  // [Phase6] 仅重算今日 DSA（要求当日日线覆盖率 ≥ 90%）
  const handleDsaOnly = async () => {
    const date = tradeDate || shanghaiBusinessDate()
    try {
      const result = await dsaOnlyMutation.mutateAsync(date)
      toast.show('DSA 重算已创建', result.message)
    } catch (err: unknown) {
      // [Phase6] - 409 时展示覆盖率不足原因（detail 是 dict 含 reason/message）
      const axiosErr = err as { response?: { status?: number; data?: { detail?: unknown } } }
      const respStatus = axiosErr.response?.status
      const detail = axiosErr.response?.data?.detail
      if (respStatus === 409 && detail && typeof detail === 'object' && detail !== null) {
        const detailDict = detail as { message?: string; reason?: string }
        const message = detailDict.message ?? '当日日线覆盖率不足'
        toast.show('DSA 重算失败', message)
      } else if (respStatus === 409 && typeof detail === 'string') {
        toast.show('DSA 重算失败', detail)
      } else {
        toast.show('DSA 重算失败', '请稍后重试或检查权限')
      }
    }
  }

  // 重试失败任务（从头执行，与 resume 区别：retry 重置 last_completed_step）
  const handleRetry = async () => {
    if (!jobRunId) return
    try {
      const result = await retryMutation.mutateAsync(jobRunId)
      toast.show('重试已启动', result.message)
    } catch {
      toast.show('重试失败', '请稍后重试')
    }
  }

  // [Phase6] 从失败步骤继续（保留断点检查点，不重复拉行情）
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

  // 强制重新执行（带二次确认）
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
        <div className="actions after-close-actions">
          {/* [Phase6] - 4 个独立入口：更新日线+选股 / 仅重算选股 / 从断点继续 / 强制执行 */}
          <button
            className="btn small primary"
            onClick={handleCreate}
            disabled={createMutation.isPending}
            title="更新今日日线并计算选股（完整流水线）"
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

        {/* [Phase7] 编排状态详情：当前阶段/心跳/租约/最后成功步骤/中断原因
            数据来源：useAfterCloseRunStatus(jobRunId) 轮询 GET /admin/after-close-runs/{id}
            展示条件：jobRunId 存在且查询返回数据
            心跳超时（heartbeat_stale=true）红色高亮，中断原因红色文字 */}
        {afterCloseDetail && (
          <div className="orchestrator-detail">
            <div className="detail-title">编排状态详情</div>
            <div className="detail-grid">
              <div className="toggle-row">
                <span>当前阶段</span>
                <b className="num">{afterCloseDetail.orchestrator_status ?? '-'}</b>
              </div>
              <div className="toggle-row">
                <span>Worker</span>
                <b className="num">{afterCloseDetail.worker_instance_id ?? '-'}</b>
              </div>
              <div className={`toggle-row${afterCloseDetail.heartbeat_stale ? ' stale' : ''}`}>
                <span>最后心跳</span>
                <b className="num">
                  {afterCloseDetail.heartbeat_at
                    ? formatShanghaiTime(afterCloseDetail.heartbeat_at)
                    : '-'}
                  {afterCloseDetail.heartbeat_stale && '（超时）'}
                </b>
              </div>
              <div className="toggle-row">
                <span>租约到期</span>
                <b className="num">
                  {afterCloseDetail.lease_expires_at
                    ? formatShanghaiTime(afterCloseDetail.lease_expires_at)
                    : '-'}
                </b>
              </div>
              <div className="toggle-row">
                <span>最后成功步骤</span>
                <b className="num">{afterCloseDetail.last_completed_step ?? '-'}</b>
              </div>
              {afterCloseDetail.interrupt_reason && (
                <div className="toggle-row interrupt-reason">
                  <span>中断原因</span>
                  <b className="num">{afterCloseDetail.interrupt_reason}</b>
                </div>
              )}
            </div>
            {/* [AfterClose] - 非交易日等跳过原因提示（黄色警告） */}
            {afterCloseDetail.skip_reason === 'NON_TRADING_DAY' && (
              <div className="notice warn" style={{ marginTop: '10px' }}>
                因非交易日跳过，未执行行情更新和选股
              </div>
            )}
          </div>
        )}

        {/* [Phase 9] 数据新鲜度：行情数据 + 选股策略两个独立区块 */}
        {pipeline?.data_freshness && (
          <div className="data-freshness-grid">
            {/* 行情数据区块（6 项，is_behind=true 红色高亮）*/}
            <div
              className={`data-freshness-block${
                pipeline.data_freshness.bars.is_behind_latest_trade_date ? ' behind' : ''
              }`}
            >
              <div className="data-freshness-title">行情数据</div>
              <div className="toggle-row">
                <span>最新日线交易日</span>
                <b className="num">
                  {pipeline.data_freshness.bars.latest_daily_trade_date ?? '-'}
                </b>
              </div>
              <div className="toggle-row">
                <span>日线覆盖率</span>
                <b className="num">
                  {pipeline.data_freshness.bars.daily_coverage != null
                    ? `${(pipeline.data_freshness.bars.daily_coverage * 100).toFixed(1)}%`
                    : '-'}
                </b>
              </div>
              <div className="toggle-row">
                <span>最新 15m Bar</span>
                <b className="num">
                  {pipeline.data_freshness.bars.latest_15m_bar_time
                    ? formatShanghaiTime(pipeline.data_freshness.bars.latest_15m_bar_time)
                    : '-'}
                </b>
              </div>
              <div className="toggle-row">
                <span>最新 60m Bar</span>
                <b className="num">
                  {pipeline.data_freshness.bars.latest_60m_bar_time
                    ? formatShanghaiTime(pipeline.data_freshness.bars.latest_60m_bar_time)
                    : '-'}
                </b>
              </div>
              <div className="toggle-row">
                <span>最后成功任务</span>
                <b className="num">
                  {pipeline.data_freshness.bars.last_success_job_id
                    ? pipeline.data_freshness.bars.last_success_job_id.slice(0, 8)
                    : '-'}
                </b>
              </div>
              {pipeline.data_freshness.bars.is_behind_latest_trade_date && (
                <div className="data-freshness-warn">行情落后最近交易日</div>
              )}
            </div>

            {/* 选股策略区块（7 项）*/}
            <div className="data-freshness-block">
              <div className="data-freshness-title">选股策略</div>
              <div className="toggle-row">
                <span>最新计算交易日</span>
                <b className="num">
                  {pipeline.data_freshness.strategy.latest_compute_trade_date ?? '-'}
                </b>
              </div>
              <div className="toggle-row">
                <span>最新发布交易日</span>
                <b className="num">
                  {pipeline.data_freshness.strategy.latest_published_trade_date ?? '-'}
                </b>
              </div>
              <div className="toggle-row">
                <span>运行状态</span>
                <b className="num">{pipeline.data_freshness.strategy.status ?? '-'}</b>
              </div>
              <div className="toggle-row">
                <span>标的总数</span>
                <b className="num">
                  {pipeline.data_freshness.strategy.total_instruments ?? '-'}
                </b>
              </div>
              <div className="toggle-row">
                <span>失败数</span>
                <b className="num">{pipeline.data_freshness.strategy.failed_count ?? '-'}</b>
              </div>
              <div className="toggle-row">
                <span>发布时间</span>
                <b className="num">
                  {pipeline.data_freshness.strategy.published_at
                    ? formatShanghaiTime(pipeline.data_freshness.strategy.published_at)
                    : '-'}
                </b>
              </div>
              <div className="toggle-row">
                <span>运行 ID</span>
                <b className="num">
                  {pipeline.data_freshness.strategy.strategy_run_id
                    ? pipeline.data_freshness.strategy.strategy_run_id.slice(0, 8)
                    : '-'}
                </b>
              </div>
            </div>
          </div>
        )}
      </div>
    </section>
  )
}
