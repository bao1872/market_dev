// 盘后流水线详情页（受保护路由，admin only）
//
// 用法：
// 1. 路由 /admin/after-close，受保护路由（经 ProtectedLayout + AdminRoute 包裹）
// 2. 数据源：useAfterClosePipelineLatest（GET /admin/after-close/pipeline/latest）
//    - running 状态 10s 轮询，非 running 60s 轮询，页面不可见暂停（hook 内实现）
// 3. 页面结构（5 个区块）：
//    - 顶部状态卡：trade_date / market_session / overall_status / watchlist_ready / watchlist_reason
//    - 步骤时间线（垂直，每步显示 status/started_at/finished_at/duration/counts/error）
//    - 数据新鲜度卡：行情 + 选股（复用 .data-freshness-grid 样式）
//    - 编排状态详情：当前阶段/Worker/心跳/租约/检查点/中断原因（来自 after_close_run 摘要）
//    - 最近 20 次运行列表（after_close_orchestrator + snapshot_run 混合）
// 4. 事件日志抽屉：点击"查看事件"按钮打开，展示最近 100 条事件（来自 pipeline.events）
// 5. 操作按钮：触发当日 after_close 编排（POST /admin/after-close/pipeline/run，幂等）
//
// 依赖 hooks：
// - useAfterClosePipelineLatest：查询最近交易日聚合状态
// - useAfterClosePipelineRuns：查询最近 20 次运行
// - useCreateAfterClosePipelineRun：触发编排（admin，幂等）

import { useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import {
  useAfterClosePipelineLatest,
  useAfterClosePipelineByDate,
  useAfterClosePipelineRuns,
  useCreateAfterClosePipelineRun,
  useCancelAfterCloseRun,
  useReconcileAfterCloseRun,
  useRestartAfterCloseRun,
  useForceRestartAfterCloseRun,
} from '@/hooks/useApi'
import { useToast } from '@/store/toast'
import { shanghaiBusinessDate, formatShanghaiTime } from '@/utils/datetime'
import { formatAdminApiError } from '@/utils/adminErrors'
import type { PipelineStep, PipelineRunItem } from '@/api/endpoints'
import {
  stepLabel,
  overallStatusLabel,
  overallStatusPillClass,
  marketSessionLabel,
  stepStatusLabel,
  stepStatusClass,
  runItemStatusPillClass,
  runItemKindLabel,
  formatDurationSeconds,
  getStepKeys,
} from './adminAfterClosePipelineHelpers'

// ===== [Phase8A] 步骤时间线组件（以 API steps 为主，不硬编码步骤数量）=====
function PipelineTimeline({ steps }: { steps: PipelineStep[] }) {
  // [Phase8A] 以 API 返回的 steps 为主；API 未返回时用 DEFAULT_STEP_ORDER 兜底
  const stepKeys = getStepKeys(steps)

  return (
    <div className="pipeline-timeline">
      {stepKeys.map((stepKey, idx) => {
        // 从 API steps 中查找对应数据
        const step = steps.find((s) => s.step === stepKey)
        const status = step?.status ?? 'pending'
        const cls = stepStatusClass(status)
        return (
          <div key={stepKey} className={`pipeline-timeline-item ${cls}`}>
            <div className="pipeline-timeline-index">{idx + 1}</div>
            <div className="pipeline-timeline-main">
              <div className="pipeline-timeline-head">
                <b>{stepLabel(stepKey)}</b>
                <span className={`status-pill ${cls === 'done' ? 'ok' : cls === 'active' ? 'warn' : cls === 'error' ? 'error' : 'off'}`}>
                  {stepStatusLabel(status)}
                </span>
              </div>
              {step && (
                <div className="pipeline-timeline-meta">
                  {step.started_at && (
                    <span>开始: {formatShanghaiTime(step.started_at)}</span>
                  )}
                  {step.finished_at && (
                    <span>结束: {formatShanghaiTime(step.finished_at)}</span>
                  )}
                  {/* [TIMELINE-FIX] 透传 status + warnings，running→"进行中"，invalid_order→"未知"，负数→"未知" */}
                  {(step.duration_seconds != null || status === 'running' || step.warnings?.length) && (
                    <span
                      title={
                        step.warnings && step.warnings.length > 0
                          ? `诊断: ${step.warnings.join(', ')}（事件跨重试/时区偏差，未用 max(0,x) 掩盖）`
                          : undefined
                      }
                      className={step.warnings?.length ? 'timeline-meta-warn' : ''}
                    >
                      耗时: {formatDurationSeconds(step.duration_seconds, status, step.warnings)}
                    </span>
                  )}
                  <span>
                    进度: {step.processed == null ? '未知' : step.processed.toLocaleString()} /{' '}
                    {step.total == null ? '未知' : step.total.toLocaleString()}
                  </span>
                  <span>最近进度: {step.last_progress_at ? formatShanghaiTime(step.last_progress_at) : '未知'}</span>
                  <span>尝试: {step.attempt ?? '未知'}{step.retry_count != null ? ` · 重试 ${step.retry_count}` : ''}</span>
                  {step.optional && <span>可选步骤</span>}
                  {Object.keys(step.counts).length > 0 && (
                    <span>
                      其他计数:{' '}
                      {Object.entries(step.counts)
                        .map(([k, v]) => `${k}=${String(v)}`)
                        .join(', ')}
                    </span>
                  )}
                </div>
              )}
              {step?.error_message && (
                <div className="pipeline-timeline-error">{step.error_message}</div>
              )}
            </div>
          </div>
        )
      })}
    </div>
  )
}

// ===== 主页面 =====
export default function AdminAfterClosePipelinePage() {
  const toast = useToast.getState()
  // [OPS-06] 从 AdminJobsPage 跳转而来的历史任务：URL 携带 ?tradeDate=YYYY-MM-DD，
  // 直接定位到被点击的 run，而不是默认显示最新交易日。
  const [searchParams] = useSearchParams()
  const initialTradeDate = searchParams.get('tradeDate') ?? ''
  // selectedDate: '' 表示"最新交易日"，否则按指定日期查询（允许选择历史日期如 2026-07-15）
  const [selectedDate, setSelectedDate] = useState(initialTradeDate)
  const todayStr = shanghaiBusinessDate()

  // selectedDate === '' 时用 latest（自动定位最近交易日），否则按指定日期查询
  const pipelineQuery = useAfterClosePipelineLatest(selectedDate === '')
  const byDateQuery = useAfterClosePipelineByDate(
    selectedDate !== '' ? selectedDate : null,
    selectedDate !== '',
  )
  const runsQuery = useAfterClosePipelineRuns(20)
  const createMutation = useCreateAfterClosePipelineRun()
  const cancelMutation = useCancelAfterCloseRun()
  const reconcileMutation = useReconcileAfterCloseRun()
  const restartMutation = useRestartAfterCloseRun()
  const forceRestartMutation = useForceRestartAfterCloseRun()

  const [eventDrawerOpen, setEventDrawerOpen] = useState(false)
  const [confirmAction, setConfirmAction] = useState<'cancel' | 'force' | null>(null)

  // 统一取 pipeline 数据（latest 或 by-date）
  const pipeline = selectedDate === '' ? pipelineQuery.data : byDateQuery.data
  const isLoading = selectedDate === '' ? pipelineQuery.isLoading : byDateQuery.isLoading
  const runs = runsQuery.data?.items ?? []

  const overallStatus = pipeline?.overall_status
  const tradeDate = pipeline?.trade_date ?? (selectedDate !== '' ? selectedDate : todayStr)
  const afterCloseRun = pipeline?.after_close_run
  const featureRun = pipeline?.feature_snapshot_run
  const events = pipeline?.events ?? []

  // 触发当日 after_close 编排（幂等）
  const handleCreateRun = async () => {
    try {
      const result = await createMutation.mutateAsync({ trade_date: tradeDate })
      if (result.is_new) {
        toast.show('任务已创建', `已加入队列，job_run_id=${result.job_run_id.slice(0, 8)}`)
      } else {
        toast.show(
          '任务已存在',
          `当天已有 ${result.status} 任务，job_run_id=${result.job_run_id.slice(0, 8)}`,
        )
      }
    } catch (err: unknown) {
      // [R14] 统一经 parseAdminApiError/formatAdminApiError 消费结构化错误（含 recommended_action）
      toast.show('创建失败', formatAdminApiError(err))
    }
  }

  const runAction = async (
    title: string,
    action: () => Promise<{ message?: string }>,
  ) => {
    try {
      const result = await action()
      toast.show(title, result.message)
    } catch (err: unknown) {
      // [R14] 统一经 formatAdminApiError 消费结构化错误（cancel/reconcile/restart/force 等）
      toast.show(`${title}失败`, formatAdminApiError(err))
    }
  }

  const handleCancelRun = () => afterCloseRun && runAction(
    '终止请求已提交',
    () => cancelMutation.mutateAsync({ runId: afterCloseRun.job_run_id, reason: '管理员从诊断页终止' }),
  ).finally(() => setConfirmAction(null))

  const handleReconcileRun = () => afterCloseRun && runAction(
    '对账完成',
    () => reconcileMutation.mutateAsync({ runId: afterCloseRun.job_run_id, reason: '管理员从诊断页对账' }),
  )

  const handleRestartRun = () => afterCloseRun && runAction(
    '已从检查点续跑',
    () => restartMutation.mutateAsync(afterCloseRun.job_run_id),
  )

  const handleForceRestartRun = () => afterCloseRun && runAction(
    '完整强制重跑已排队',
    () => forceRestartMutation.mutateAsync({ runId: afterCloseRun.job_run_id }),
  ).finally(() => setConfirmAction(null))

  const canRestart = afterCloseRun != null &&
    (afterCloseRun.status === 'interrupted' || afterCloseRun.status === 'failed')
  const isActiveRun = afterCloseRun?.status === 'queued' || afterCloseRun?.status === 'running'
  const anyActionPending = cancelMutation.isPending || reconcileMutation.isPending ||
    restartMutation.isPending || forceRestartMutation.isPending

  return (
    <>
      {/* 页头 */}
      <div className="page-head">
        <div>
          <h1 className="page-title">盘后流水线详情</h1>
          <div className="page-desc">
            交易日 {tradeDate} · 步骤时间线 · 数据新鲜度 · 最近运行
          </div>
        </div>
        <div className="actions">
          <Link className="btn small" to="/admin/overview">
            ← 返回概览
          </Link>
          {/* 交易日选择器：默认"最新"（latest），可切换到指定日期（含历史如 2026-07-15）*/}
          <div className="date-selector-row">
            <label className="date-selector-label">交易日</label>
            <select
              className="date-selector-select"
              value={selectedDate}
              onChange={(e) => setSelectedDate(e.target.value)}
              title="选择交易日查看历史任务（默认最新）"
            >
              <option value="">最新</option>
              {/* 最近 7 个自然日（含今日），供管理员快速选择历史日期 */}
              {Array.from({ length: 7 }, (_, i) => {
                const d = new Date()
                d.setDate(d.getDate() - i)
                const y = d.getFullYear()
                const m = String(d.getMonth() + 1).padStart(2, '0')
                const day = String(d.getDate()).padStart(2, '0')
                return `${y}-${m}-${day}`
              })
                .filter((d) => d !== todayStr)
                .map((d) => (
                  <option key={d} value={d}>{d}</option>
                ))}
            </select>
          </div>
          <button
            className="btn small danger"
            onClick={() => setConfirmAction('cancel')}
            disabled={!isActiveRun || anyActionPending}
            title={isActiveRun ? '请求 Worker 协作式终止；不会删除已有结果' : '仅运行中或排队中的任务可终止'}
          >
            终止任务
          </button>
          <button
            className="btn small"
            onClick={handleReconcileRun}
            disabled={!afterCloseRun || anyActionPending}
            title="核验运行状态与事件并修正持久化状态；不会启动计算"
          >
            {reconcileMutation.isPending ? '对账中…' : '对账状态'}
          </button>
          <button
            className="btn small warning"
            onClick={handleRestartRun}
            disabled={!canRestart || anyActionPending}
            title={`保留成功检查点，从 ${afterCloseRun?.restart_from ?? afterCloseRun?.last_completed_step ?? '失败位置'} 继续`}
          >
            {restartMutation.isPending ? '续跑中…' : '从此处续跑'}
          </button>
          <button
            className="btn small danger"
            onClick={() => setConfirmAction('force')}
            disabled={!afterCloseRun || anyActionPending}
            title="忽略已有检查点，从首步完整重新排队"
          >
            完整强制重跑
          </button>
          <button
            className="btn small primary"
            onClick={handleCreateRun}
            disabled={createMutation.isPending || overallStatus === 'running'}
            title="幂等创建当日盘后编排"
          >
            {createMutation.isPending ? '创建中…' : '创建当日编排'}
          </button>
          <button
            className="btn small"
            onClick={() => setEventDrawerOpen(true)}
            disabled={events.length === 0}
            title="查看最近 100 条事件日志"
          >
            查看事件（{events.length}）
          </button>
        </div>
      </div>

      {/* ===== 顶部状态卡 ===== */}
      <div className="grid section-gap">
        <section className="card">
          <div className="card-head">
            <div>
              <div className="card-title">流水线状态</div>
              <div className="card-sub">交易日 {tradeDate}</div>
            </div>
          </div>
          <div className="card-body">
            <div className="toggle-row">
              <span>整体状态</span>
              <b>
                <span className={`status-pill ${overallStatusPillClass(overallStatus)}`}>
                  {isLoading ? '-' : overallStatusLabel(overallStatus)}
                </span>
              </b>
            </div>
            <div className="toggle-row">
              <span>市场时段</span>
              <b className="num">
                {isLoading ? '-' : marketSessionLabel(pipeline?.market_session)}
              </b>
            </div>
            <div className="toggle-row">
              <span>自选可用</span>
              <b>
                <span
                  className={`status-pill ${pipeline?.watchlist_ready ? 'ok' : 'off'}`}
                >
                  {isLoading ? '-' : pipeline?.watchlist_ready ? '是' : '否'}
                </span>
              </b>
            </div>
            <div className="toggle-row">
              <span>不可用原因</span>
              <b className="num">
                {isLoading ? '-' : (pipeline?.watchlist_reason ?? '-')}
              </b>
            </div>
            <div className="toggle-row">
              <span>已有完整回补</span>
              <b className="num">
                {isLoading ? '-' : pipeline?.has_backfill_full ? '是' : '否'}
              </b>
            </div>
          </div>
        </section>
      </div>

      {/* ===== 步骤时间线 ===== */}
      <div className="grid section-gap">
        <section className="card">
          <div className="card-head">
            <div>
              <div className="card-title">步骤时间线</div>
              <div className="card-sub">
                {/* [CHANGE-20260831-ADMIN-TIMELINE] 7 步 current canonical 序列（含复盘/历史阶段） */}
                refreshing_daily → syncing_boards → checking_coverage →
                computing_features → computing_review → computing_history → watchlist_ready
              </div>
            </div>
          </div>
          <div className="card-body">
            {isLoading ? (
              <div className="notice">加载中…</div>
            ) : pipeline ? (
              <PipelineTimeline steps={pipeline.steps} />
            ) : (
              <div className="notice">暂无数据</div>
            )}
          </div>
        </section>
      </div>

      {/* ===== 数据新鲜度 + 编排状态详情 两列 ===== */}
      <div className="grid split-2 section-gap">
        {/* 数据新鲜度卡（复用 .data-freshness-grid 样式）*/}
        <section className="card">
          <div className="card-head">
            <div>
              <div className="card-title">数据新鲜度</div>
              <div className="card-sub">行情 + 选股策略</div>
            </div>
          </div>
          <div className="card-body">
            {pipeline?.data_freshness ? (
              <div className="data-freshness-grid">
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
                  {pipeline.data_freshness.bars.is_behind_latest_trade_date && (
                    <div className="data-freshness-warn">行情落后最近交易日</div>
                  )}
                </div>
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
                    <b className="num">
                      {pipeline.data_freshness.strategy.failed_count ?? '-'}
                    </b>
                  </div>
                  <div className="toggle-row">
                    <span>发布时间</span>
                    <b className="num">
                      {pipeline.data_freshness.strategy.published_at
                        ? formatShanghaiTime(pipeline.data_freshness.strategy.published_at)
                        : '-'}
                    </b>
                  </div>
                </div>
              </div>
            ) : (
              <div className="notice">暂无数据</div>
            )}
          </div>
        </section>

        {/* 编排状态详情卡（after_close_run 摘要 + feature_snapshot_run 摘要）*/}
        <section className="card">
          <div className="card-head">
            <div>
              <div className="card-title">编排状态详情</div>
              <div className="card-sub">after_close_orchestrator + feature_snapshot_run</div>
            </div>
          </div>
          <div className="card-body">
            {afterCloseRun ? (
              <>
                <div className="detail-title">after_close_orchestrator</div>
                <div className="toggle-row">
                  <span>job_run_id</span>
                  <b className="num">{afterCloseRun.job_run_id.slice(0, 8)}</b>
                </div>
                <div className="toggle-row">
                  <span>状态</span>
                  <b>
                    <span className={`status-pill ${runItemStatusPillClass(afterCloseRun.status)}`}>
                      {afterCloseRun.status}
                    </span>
                  </b>
                </div>
                <div className="toggle-row">
                  <span>编排阶段</span>
                  <b className="num">{afterCloseRun.orchestrator_status ?? '-'}</b>
                </div>
                <div className="toggle-row">
                  <span>开始时间</span>
                  <b className="num">
                    {afterCloseRun.started_at
                      ? formatShanghaiTime(afterCloseRun.started_at)
                      : '-'}
                  </b>
                </div>
                <div className="toggle-row">
                  <span>结束时间</span>
                  <b className="num">
                    {afterCloseRun.finished_at
                      ? formatShanghaiTime(afterCloseRun.finished_at)
                      : '-'}
                  </b>
                </div>
                <div className="toggle-row">
                  <span>Worker</span>
                  <b className="num">{afterCloseRun.worker_instance_id ?? '-'}</b>
                </div>
                <div className="toggle-row">
                  <span>最后心跳</span>
                  <b className="num">
                    {afterCloseRun.heartbeat_at
                      ? formatShanghaiTime(afterCloseRun.heartbeat_at)
                      : '-'}
                  </b>
                </div>
                <div className="toggle-row">
                  <span>租约到期</span>
                  <b className="num">
                    {afterCloseRun.lease_expires_at
                      ? formatShanghaiTime(afterCloseRun.lease_expires_at)
                      : '-'}
                  </b>
                </div>
                <div className="toggle-row">
                  <span>最后成功步骤</span>
                  <b className="num">{afterCloseRun.last_completed_step ?? '未知'}</b>
                </div>
                <div className="toggle-row"><span>处理进度</span><b className="num">{pipeline?.diagnostics?.processed ?? '未知'} / {pipeline?.diagnostics?.total ?? '未知'}</b></div>
                <div className="toggle-row"><span>最近进度</span><b className="num">{pipeline?.diagnostics?.last_progress_at ? formatShanghaiTime(pipeline.diagnostics.last_progress_at) : '未知'}</b></div>
                <div className="toggle-row"><span>心跳年龄</span><b className="num">{pipeline?.diagnostics?.heartbeat_age_seconds == null ? '未知' : formatDurationSeconds(pipeline.diagnostics.heartbeat_age_seconds)}</b></div>
                <div className="toggle-row"><span>租约剩余</span><b className="num">{pipeline?.diagnostics?.lease_remaining_seconds == null ? '未知' : formatDurationSeconds(pipeline.diagnostics.lease_remaining_seconds)}</b></div>
                <div className="toggle-row"><span>已用时</span><b className="num">{pipeline?.diagnostics?.elapsed_seconds == null ? '未知' : formatDurationSeconds(pipeline.diagnostics.elapsed_seconds)}</b></div>
                <div className="toggle-row"><span>重试次数</span><b className="num">{pipeline?.diagnostics?.retry_count ?? '未知'}</b></div>
                <div className="toggle-row"><span>发布状态</span><b className="num">{pipeline?.diagnostics?.partial_success ? '部分成功（核心结果已发布）' : pipeline?.diagnostics?.publication_status ?? '未知'}</b></div>
                {afterCloseRun.error_message && (
                  <div className="notice error" style={{ marginTop: '10px' }}>
                    {afterCloseRun.error_message}
                  </div>
                )}
              </>
            ) : (
              <div className="notice">今日尚无 after_close 编排任务</div>
            )}

            {featureRun && (
              <>
                <div className="detail-title" style={{ marginTop: '16px' }}>
                  feature_snapshot_run
                </div>
                <div className="toggle-row">
                  <span>run_id</span>
                  <b className="num">{featureRun.run_id.slice(0, 8)}</b>
                </div>
                <div className="toggle-row">
                  <span>类型</span>
                  <b className="num">{featureRun.run_type}</b>
                </div>
                <div className="toggle-row">
                  <span>状态</span>
                  <b>
                    <span className={`status-pill ${runItemStatusPillClass(featureRun.status)}`}>
                      {featureRun.status}
                    </span>
                  </b>
                </div>
                <div className="toggle-row">
                  <span>范围</span>
                  <b className="num">{featureRun.scope}</b>
                </div>
                <div className="toggle-row">
                  <span>快照数</span>
                  <b className="num">
                    {featureRun.snapshot_count ?? '-'} / {featureRun.expected_count ?? '-'}
                  </b>
                </div>
                <div className="toggle-row">
                  <span>失败数</span>
                  <b className="num">{featureRun.failed_count ?? '-'}</b>
                </div>
                <div className="toggle-row">
                  <span>发布时间</span>
                  <b className="num">
                    {featureRun.published_at
                      ? formatShanghaiTime(featureRun.published_at)
                      : '-'}
                  </b>
                </div>
              </>
            )}
          </div>
        </section>
      </div>

      {/* ===== 最近 20 次运行列表 ===== */}
      <div className="grid section-gap">
        <section className="card">
          <div className="card-head">
            <div>
              <div className="card-title">最近运行</div>
              <div className="card-sub">after_close_orchestrator + snapshot_run 混合列表</div>
            </div>
          </div>
          <div className="card-body">
            {runsQuery.isLoading ? (
              <div className="notice">加载中…</div>
            ) : runs.length === 0 ? (
              <div className="notice">暂无运行记录</div>
            ) : (
              <div className="table-shell">
                <div className="table-scroll">
                  <table className="data-table">
                  <thead>
                    <tr>
                      <th>类型</th>
                      <th>交易日</th>
                      <th>状态</th>
                      <th>编排阶段</th>
                      <th>快照数</th>
                      <th>失败</th>
                      <th>开始</th>
                      <th>结束</th>
                      <th>ID</th>
                    </tr>
                  </thead>
                  <tbody>
                    {runs.map((item: PipelineRunItem, idx: number) => (
                      <tr key={`${item.kind}-${item.job_run_id ?? item.run_id}-${idx}`}>
                        <td>{runItemKindLabel(item.kind)}</td>
                        <td className="num">{item.trade_date ?? '-'}</td>
                        <td>
                          <span className={`status-pill ${runItemStatusPillClass(item.status)}`}>
                            {item.status}
                          </span>
                        </td>
                        <td className="num">{item.orchestrator_status ?? '-'}</td>
                        <td className="num">{item.snapshot_count ?? '-'}</td>
                        <td className="num">{item.failed_count ?? '-'}</td>
                        <td className="num">
                          {item.started_at ? formatShanghaiTime(item.started_at) : '-'}
                        </td>
                        <td className="num">
                          {item.finished_at ? formatShanghaiTime(item.finished_at) : '-'}
                        </td>
                        <td className="num">
                          {(item.job_run_id ?? item.run_id ?? '-').slice(0, 8)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                  </table>
                </div>
              </div>
            )}
          </div>
        </section>
      </div>

      {confirmAction && (
        <div className="modal-backdrop" role="presentation" onClick={() => setConfirmAction(null)}>
          <section
            className="modal-card"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="after-close-confirm-title"
            onClick={(event) => event.stopPropagation()}
          >
            <h2 id="after-close-confirm-title">
              {confirmAction === 'cancel' ? '确认终止当前任务？' : '确认完整强制重跑？'}
            </h2>
            <p>
              {confirmAction === 'cancel'
                ? '系统将请求 Worker 协作式停止；已产生的数据不会删除。'
                : '系统将忽略已有成功检查点，从第一步完整重新排队。已有发布结果不会在前端删除。'}
            </p>
            <div className="after-close-actions">
              <button className="btn" onClick={() => setConfirmAction(null)} autoFocus>返回</button>
              <button
                className="btn danger"
                onClick={confirmAction === 'cancel' ? handleCancelRun : handleForceRestartRun}
                disabled={anyActionPending}
              >
                {anyActionPending ? '提交中…' : confirmAction === 'cancel' ? '确认终止' : '确认完整重跑'}
              </button>
            </div>
          </section>
        </div>
      )}

      {/* ===== 事件日志抽屉（100 events max，来自 pipeline.events）===== */}
      {eventDrawerOpen && (
        <div className="drawer-backdrop open" onClick={() => setEventDrawerOpen(false)}>
          <aside className="drawer" onClick={(e) => e.stopPropagation()}>
            <div className="drawer-head">
              <div>
                <b>事件日志 · 最近 {events.length} 条</b>
                <div className="card-sub">交易日 {tradeDate}</div>
              </div>
              <button className="icon-btn" onClick={() => setEventDrawerOpen(false)}>
                ×
              </button>
            </div>
            <div className="drawer-body">
              {events.length === 0 ? (
                <div className="notice">暂无事件</div>
              ) : (
                <div className="job-event-timeline">
                  {events.map((event) => (
                    <div key={event.id} className={`job-event-item ${event.level}`}>
                      <span className={`job-event-level ${event.level}`}>
                        {event.level === 'error' ? 'ERROR' : event.level === 'warn' ? 'WARN' : 'INFO'}
                      </span>
                      <div className="job-event-main">
                        <div className="job-event-step">{event.step}</div>
                        <div className="job-event-message">{event.message}</div>
                        <div className="job-event-time">
                          {formatShanghaiTime(event.created_at)}
                        </div>
                        {event.payload && Object.keys(event.payload).length > 0 && (
                          <pre className="job-event-payload">
                            {JSON.stringify(event.payload, null, 2)}
                          </pre>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
            <div className="drawer-foot">
              <button className="btn" onClick={() => setEventDrawerOpen(false)}>
                关闭
              </button>
            </div>
          </aside>
        </div>
      )}
    </>
  )
}
