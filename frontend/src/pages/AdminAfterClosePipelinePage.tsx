// 盘后流水线详情页（受保护路由，admin only）
//
// 用法：
// 1. 路由 /admin/after-close，受保护路由（经 ProtectedLayout + AdminRoute 包裹）
// 2. 数据源：useAfterClosePipelineLatest（GET /admin/after-close/pipeline/latest）
//    - running 状态 10s 轮询，非 running 60s 轮询，页面不可见暂停（hook 内实现）
// 3. 页面结构（5 个区块）：
//    - 顶部状态卡：trade_date / market_session / overall_status / watchlist_ready / watchlist_reason
//    - 8 步骤时间线（垂直，每步显示 status/started_at/finished_at/duration/counts/error）
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
import { Link } from 'react-router-dom'
import {
  useAfterClosePipelineLatest,
  useAfterClosePipelineRuns,
  useCreateAfterClosePipelineRun,
} from '@/hooks/useApi'
import { useToast } from '@/store/toast'
import { shanghaiBusinessDate, formatShanghaiTime } from '@/utils/datetime'
import type { PipelineStep, PipelineRunItem } from '@/api/endpoints'

// ===== 8 步骤定义（与后端 _PIPELINE_STEPS 严格对齐）=====
// [AfterClosePipelinePage] - 描述: 8 个展示步骤（前 7 个来自 after_close_orchestrator 状态机，最后一个是 watchlist gate）
const PIPELINE_STEPS: { key: string; label: string }[] = [
  { key: 'refreshing_daily', label: '刷新日线' },
  { key: 'checking_coverage', label: '检查覆盖率' },
  { key: 'creating_dsa', label: '创建DSA任务' },
  { key: 'waiting_dsa_worker', label: '等待DSA计算' },
  { key: 'quality_gate', label: '质量门禁' },
  { key: 'feature_snapshot', label: '特征快照' },
  { key: 'publishing', label: '发布结果' },
  { key: 'watchlist_ready', label: '自选可用' },
]

// ===== overall_status → 中文标签 + pill 样式 =====
function overallStatusLabel(status: string | undefined): string {
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

function overallStatusPillClass(status: string | undefined): string {
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
function marketSessionLabel(session: string | undefined): string {
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
function stepStatusLabel(status: string): string {
  switch (status) {
    case 'pending': return '待执行'
    case 'running': return '执行中'
    case 'completed': return '已完成'
    case 'failed': return '失败'
    case 'skipped': return '跳过'
    default: return '-'
  }
}

function stepStatusClass(status: string): string {
  switch (status) {
    case 'completed': return 'done'
    case 'running': return 'active'
    case 'failed': return 'error'
    case 'skipped': return 'skipped'
    default: return ''
  }
}

// ===== 运行列表项状态 → 中文标签 + pill 样式 =====
function runItemStatusPillClass(status: string): string {
  switch (status) {
    case 'succeeded': return 'ok'
    case 'running':
    case 'queued': return 'warn'
    case 'failed':
    case 'interrupted': return 'error'
    default: return 'off'
  }
}

function runItemKindLabel(kind: string): string {
  switch (kind) {
    case 'after_close_orchestrator': return '盘后编排'
    case 'snapshot_run': return '特征快照'
    default: return kind
  }
}

// ===== 格式化耗时（秒 → "Xm Ys"）=====
function formatDurationSeconds(seconds: number | null | undefined): string {
  if (seconds == null) return '-'
  if (seconds < 60) return `${seconds.toFixed(1)}s`
  const m = Math.floor(seconds / 60)
  const s = Math.round(seconds % 60)
  return `${m}m ${s}s`
}

// ===== 8 步骤时间线组件 =====
function PipelineTimeline({ steps }: { steps: PipelineStep[] }) {
  // 构建步骤索引映射，处理 watchlist_ready 不在 steps 中的情况
  const stepMap = new Map<string, PipelineStep>(steps.map((s) => [s.step, s]))

  return (
    <div className="pipeline-timeline">
      {PIPELINE_STEPS.map((stage, idx) => {
        const step = stepMap.get(stage.key)
        const status = step?.status ?? 'pending'
        const cls = stepStatusClass(status)
        return (
          <div key={stage.key} className={`pipeline-timeline-item ${cls}`}>
            <div className="pipeline-timeline-index">{idx + 1}</div>
            <div className="pipeline-timeline-main">
              <div className="pipeline-timeline-head">
                <b>{stage.label}</b>
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
                  {step.duration_seconds != null && (
                    <span>耗时: {formatDurationSeconds(step.duration_seconds)}</span>
                  )}
                  {Object.keys(step.counts).length > 0 && (
                    <span>
                      计数:{' '}
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

// ===== feature_snapshot 进度 + 速度/ETA =====
function FeatureSnapshotProgress({
  progress,
  startedAt,
}: {
  progress: Record<string, unknown>
  startedAt: string | null
}) {
  const processed = Number(progress['processed'] ?? 0)
  const total = Number(progress['total'] ?? 0)
  const snapshotCount = Number(progress['snapshot_count'] ?? 0)
  const failedCount = Number(progress['failed_count'] ?? 0)
  const updatedAt =
    typeof progress['updated_at'] === 'string' ? (progress['updated_at'] as string) : null

  const percent = total > 0 ? (processed / total) * 100 : 0

  // 速度/ETA：以整体 run 开始时间为基准估算（仅展示参考值）
  let speedPerSec: number | null = null
  let etaSeconds: number | null = null
  if (startedAt && processed > 0) {
    const startMs = new Date(startedAt).getTime()
    const elapsed = (Date.now() - startMs) / 1000
    if (elapsed > 0) {
      speedPerSec = processed / elapsed
      const remain = total - processed
      if (remain > 0 && speedPerSec > 0) {
        etaSeconds = remain / speedPerSec
      }
    }
  }

  return (
    <div className="feature-snapshot-progress" style={{ marginTop: '10px' }}>
      <div className="detail-title">特征快照进度</div>
      <div className="toggle-row">
        <span>处理进度</span>
        <b className="num">
          {processed} / {total}（{percent.toFixed(1)}%）
        </b>
      </div>
      <div className="toggle-row">
        <span>快照成功 / 失败</span>
        <b className="num">
          {snapshotCount} / {failedCount}
        </b>
      </div>
      {updatedAt && (
        <div className="toggle-row">
          <span>进度更新时间</span>
          <b className="num">{formatShanghaiTime(updatedAt)}</b>
        </div>
      )}
      {speedPerSec != null && (
        <div className="toggle-row">
          <span>估算速度</span>
          <b className="num">{speedPerSec.toFixed(2)} 股/秒</b>
        </div>
      )}
      {etaSeconds != null && (
        <div className="toggle-row">
          <span>预计剩余</span>
          <b className="num">{formatDurationSeconds(etaSeconds)}</b>
        </div>
      )}
    </div>
  )
}

// ===== 主页面 =====
export default function AdminAfterClosePipelinePage() {
  const toast = useToast.getState()
  const pipelineQuery = useAfterClosePipelineLatest()
  const runsQuery = useAfterClosePipelineRuns(20)
  const createMutation = useCreateAfterClosePipelineRun()

  const [eventDrawerOpen, setEventDrawerOpen] = useState(false)

  const pipeline = pipelineQuery.data
  const isLoading = pipelineQuery.isLoading
  const runs = runsQuery.data?.items ?? []

  const overallStatus = pipeline?.overall_status
  const tradeDate = pipeline?.trade_date ?? shanghaiBusinessDate()
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
      const axiosErr = err as { response?: { status?: number; data?: { detail?: string } } }
      const message = axiosErr.response?.data?.detail ?? '请稍后重试'
      toast.show('创建失败', message)
    }
  }

  return (
    <>
      {/* 页头 */}
      <div className="page-head">
        <div>
          <h1 className="page-title">盘后流水线详情</h1>
          <div className="page-desc">
            交易日 {tradeDate} · 5 阶段时间线 · 数据新鲜度 · 最近运行
          </div>
        </div>
        <div className="actions">
          <Link className="btn small" to="/admin/overview">
            ← 返回概览
          </Link>
          <button
            className="btn small primary"
            onClick={handleCreateRun}
            disabled={createMutation.isPending || overallStatus === 'running'}
            title={
              overallStatus === 'running'
                ? '当前任务运行中，请等待完成'
                : '触发当日 after_close 编排（幂等，已有任务时返回 existing）'
            }
          >
            {createMutation.isPending ? '创建中…' : '触发当日编排'}
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

      {/* ===== feature_snapshot 疑似停滞告警 ===== */}
      {pipeline?.feature_snapshot_stalled && (
        <div className="grid section-gap">
          <div className="notice error">
            特征快照阶段疑似停滞：心跳正常，但进度超过 5 分钟未推进。请检查 after_close worker 进程是否存活。
          </div>
        </div>
      )}

      {/* ===== 5 阶段时间线 ===== */}
      <div className="grid section-gap">
        <section className="card">
          <div className="card-head">
            <div>
              <div className="card-title">8 步骤时间线</div>
              <div className="card-sub">
                refreshing_daily → checking_coverage → creating_dsa → waiting_dsa_worker →
                quality_gate → feature_snapshot → publishing → watchlist_ready
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
                  <b className="num">{afterCloseRun.last_completed_step ?? '-'}</b>
                </div>
                {afterCloseRun.error_message && (
                  <div className="notice error" style={{ marginTop: '10px' }}>
                    {afterCloseRun.error_message}
                  </div>
                )}
                {afterCloseRun.feature_snapshot_stalled && (
                  <div className="notice error" style={{ marginTop: '10px' }}>
                    特征快照阶段疑似停滞（心跳正常，进度长时间未推进）
                  </div>
                )}
                {afterCloseRun.feature_snapshot_progress && (
                  <FeatureSnapshotProgress
                    progress={afterCloseRun.feature_snapshot_progress}
                    startedAt={afterCloseRun.started_at}
                  />
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
              <div className="table-wrap">
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
            )}
          </div>
        </section>
      </div>

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
