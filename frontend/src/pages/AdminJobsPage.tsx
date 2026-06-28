// 任务与事件页（受保护路由，admin only）
// 对应原型：admin/jobs.html (V1.6.3)
//
// 用法：
// 1. 路由 /admin/jobs，受保护路由（经 ProtectedLayout + AdminRoute 包裹）
// 2. KPI 4 列：运行中数（status==='running'）/ 失败数 / 全部事件数 / 全部消息数
// 3. 定时任务运行记录表：使用 StrategyDataTable，列含运行ID/任务名/业务日期/计划时间/开始/结束/处理/状态/耗时/详情
// 4. split-even 布局：最近全局事件列表（含快照按钮）+ 失败投递列表（含重试按钮）
// 5. 任务详情抽屉 jobDrawer：任务名/业务日期/计划时间/开始/结束/耗时/处理进度/错误信息/元数据
// 6. 事件快照抽屉 eventDrawer：JSON pre 展示事件结构 + 分发统计
// 7. 手动重跑弹窗 rerunModal：任务类型/交易日（已移除运行时覆盖 JSON 输入）
//
// 依赖 hooks：
// - useSchedulerJobRuns：获取定时任务运行记录（SchedulerJobRun）
// - useAdminStrategyRuns：获取策略运行记录（StrategyRun，admin 路径）
// - useStrategyEvents：获取事件列表（查询 watchlist_monitor 策略事件）
// - useStrategyEventDetail：获取事件详情（含 snapshot 快照，抽屉打开时按需加载）
// - useMessages：获取用户消息总数（KPI 4，当前用户维度）
// - useNotificationChannels：获取通知渠道（筛选失败状态作为失败投递列表）
// - useTriggerStrategyRun：手动重跑 DSA 策略
// - useToast：操作反馈

import { useState, useMemo, useCallback } from 'react'
import {
  useSchedulerJobRuns,
  useAdminStrategyRuns,
  useStrategyEvents,
  useStrategyEventDetail,
  useMessages,
  useMessageDeliveries,
  useRetryMessageDelivery,
  useTriggerStrategyRun,
  useBatchInstruments,
} from '@/hooks/useApi'
import { STRATEGY_KEYS } from '@/constants/strategyKeys'
import type { SchedulerJobRunItem, Instrument, MessageDelivery, StrategyRun } from '@/api/endpoints'
import { useToast } from '@/store/toast'
import { StrategyDataTable } from '@/components/StrategyDataTable'
import type { DataTableColumn } from '@/components/StrategyDataTable'
import { formatShanghaiTime } from '@/utils/datetime'
import { JobRunEventTimeline } from '@/features/admin-job-events/JobRunEventTimeline'

// ===== 任务名中文映射 =====
// [AdminJobsPage] - 描述: job_name 原始 key → 中文展示名映射表
const JOB_NAME_CN_MAP: Record<string, string> = {
  bars_scheduler: '盘后行情更新',
  strategy_scheduler: '盘后选股兜底调度',
  monitor_scheduler: '盘中自选股监控',
  calendar_scheduler: '交易日历更新',
  strategy_batch: 'DSA 批量计算',
  outbox: '事件消息分发',
  delivery: '第三方通知投递',
  after_close_orchestrator: '盘后编排',
}

// ===== 类型定义 =====

/** 定时任务运行记录表行类型（带索引签名以满足 StrategyDataTable 的 Row extends Record<string, unknown>） */
interface JobRunRow {
  id: string
  job_name: string
  business_date: string | null
  scheduled_at: string
  started_at: string
  finished_at: string
  heartbeat_at: string
  lease_expires_at: string
  current_run_id: string
  last_bar_time: string
  processed: string
  progress: number | null
  status: string
  status_pill_class: string
  status_text: string
  duration: string
  error_code: string | null
  error_message: string | null
  metadata_json: string | null
  raw: SchedulerJobRunItem
  [key: string]: unknown
}

/** 策略运行记录表行类型 */
interface StrategyRunRow {
  id: string
  trade_date: string | null
  run_type: string
  status: string
  status_pill_class: string
  status_text: string
  progress: number | null
  progress_text: string
  succeeded_count: number | null
  failed_count: number | null
  skipped_count: number | null
  started_at: string
  finished_at: string
  published_at: string
  raw: StrategyRun
  [key: string]: unknown
}

// ===== 工具函数 =====
// 时间格式化统一使用 formatShanghaiTime（@/utils/datetime），确保 Asia/Shanghai 时区。

/** 计算耗时（秒），返回可读字符串如 "8.1s" / "2m41s" / "1h5m" */
function formatDuration(startedAt: string | null, finishedAt: string | null): string {
  if (!startedAt) return '-'
  const start = new Date(startedAt).getTime()
  // 未完成时用当前时间作为结束，展示已运行耗时
  const end = finishedAt ? new Date(finishedAt).getTime() : Date.now()
  if (Number.isNaN(start) || Number.isNaN(end)) return '-'
  const diff = Math.max(0, end - start)
  const seconds = Math.floor(diff / 1000)
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  const remainSeconds = seconds % 60
  if (minutes < 60) return `${minutes}m${remainSeconds}s`
  const hours = Math.floor(minutes / 60)
  const remainMinutes = minutes % 60
  return `${hours}h${remainMinutes}m`
}

/** 状态映射到 status-pill 的 class（ok/warn/bad/off） */
function statusToPillClass(status: string): string {
  const s = status.toLowerCase()
  if (s === 'success' || s === 'succeeded' || s === 'completed' || s === 'published') return 'ok'
  if (s === 'failed' || s === 'error') return 'bad'
  if (s === 'running' || s === 'retrying' || s === 'partial' || s === 'partial_success' || s === 'interrupted') return 'warn'
  return 'off'
}

/** 状态映射到中文文本 */
function statusToText(status: string): string {
  const s = status.toLowerCase()
  if (s === 'success' || s === 'succeeded' || s === 'completed') return '成功'
  if (s === 'failed' || s === 'error') return '失败'
  if (s === 'running') return '运行中'
  if (s === 'retrying') return '重试中'
  if (s === 'partial' || s === 'partial_success') return '部分完成'
  if (s === 'pending' || s === 'queued') return '等待中'
  if (s === 'interrupted') return '已中断'
  if (s === 'published') return '已发布'
  return status
}

/** 从 payload 中按候选 key 列表取第一个非空值 */
function pickPayload(payload: Record<string, unknown>, keys: string[]): unknown {
  for (const k of keys) {
    const v = payload[k]
    if (v !== undefined && v !== null && v !== '') return v
  }
  return undefined
}

/** 解析 metadata_json，提取 current_run_id（strategy_run_id）和 last_bar_time */
function parseJobMetadata(metadataJson: string | null): {
  current_run_id: string
  last_bar_time: string
} {
  if (!metadataJson) return { current_run_id: '', last_bar_time: '' }
  try {
    const obj = JSON.parse(metadataJson) as Record<string, unknown>
    const runId = obj.strategy_run_id ?? obj.current_run_id
    const lastBar = obj.last_bar_time
    return {
      current_run_id: runId != null ? String(runId) : '',
      last_bar_time: lastBar != null ? String(lastBar) : '',
    }
  } catch {
    return { current_run_id: '', last_bar_time: '' }
  }
}

// ===== 主页面 =====

export default function AdminJobsPage() {
  const toast = useToast.getState()

  // ===== 状态：Tab / 抽屉/弹窗开关 =====
  const [activeTab, setActiveTab] = useState<'scheduler' | 'strategy'>('scheduler')
  const [jobDrawerOpen, setJobDrawerOpen] = useState(false)
  const [strategyDrawerOpen, setStrategyDrawerOpen] = useState(false)
  const [eventDrawerOpen, setEventDrawerOpen] = useState(false)
  const [rerunModalOpen, setRerunModalOpen] = useState(false)
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null)
  const [selectedStrategyRunId, setSelectedStrategyRunId] = useState<string | null>(null)
  const [selectedEventId, setSelectedEventId] = useState<string | null>(null)

  // ===== 状态：手动重跑表单 =====
  const [rerunTaskType, setRerunTaskType] = useState<string>(STRATEGY_KEYS.DSA_SELECTOR)
  const [rerunTradeDate, setRerunTradeDate] = useState('')

  // ===== 查询：定时任务运行记录（SchedulerJobRun）=====
  const schedulerJobRunsQuery = useSchedulerJobRuns({ limit: 20 })

  // ===== 查询：策略运行记录（DSA_SELECTOR，admin 路径）=====
  const strategyRunsQuery = useAdminStrategyRuns(STRATEGY_KEYS.DSA_SELECTOR, { limit: 20 })

  // ===== 查询：事件列表（watchlist_monitor 事件）=====
  const eventsQuery = useStrategyEvents(STRATEGY_KEYS.WATCHLIST_MONITOR, { limit: 10 })

  // ===== 查询：用户消息总数（KPI 4，当前用户维度）=====
  const messagesQuery = useMessages({ limit: 1 })

  // ===== 查询：失败消息投递记录（真实数据源）=====
  const deliveriesQuery = useMessageDeliveries({ status: 'failed', limit: 20 })

  // ===== 查询：事件详情（抽屉打开时按需加载）=====
  const eventDetailQuery = useStrategyEventDetail(selectedEventId ?? undefined)

  // ===== 变更：触发策略运行（手动重跑 / 仅重跑失败项）=====
  const triggerRun = useTriggerStrategyRun()

  // ===== 变更：立即重试消息投递（admin）=====
  const retryDelivery = useRetryMessageDelivery()

  // ===== 派生数据：转换为 JobRunRow =====
  const jobRunRows: JobRunRow[] = useMemo(() => {
    const runs = schedulerJobRunsQuery.data?.items ?? []
    return runs.map((run) => {
      const succeeded = run.succeeded_count ?? 0
      const failed = run.failed_count ?? 0
      const total = run.total_count
      const processed = total != null ? `${succeeded + failed}/${total}` : '-'
      const meta = parseJobMetadata(run.metadata_json)
      return {
        id: run.id,
        job_name: run.job_name,
        business_date: run.business_date,
        scheduled_at: formatShanghaiTime(run.scheduled_at),
        started_at: formatShanghaiTime(run.started_at),
        finished_at: formatShanghaiTime(run.finished_at),
        heartbeat_at: formatShanghaiTime(run.heartbeat_at),
        lease_expires_at: formatShanghaiTime(run.lease_expires_at),
        current_run_id: meta.current_run_id,
        last_bar_time: meta.last_bar_time ? formatShanghaiTime(meta.last_bar_time) : '',
        processed,
        progress: run.progress,
        status: run.status,
        status_pill_class: statusToPillClass(run.status),
        status_text: statusToText(run.status),
        duration: formatDuration(run.started_at, run.finished_at),
        error_code: run.error_code,
        error_message: run.error_message,
        metadata_json: run.metadata_json,
        raw: run,
      }
    })
  }, [schedulerJobRunsQuery.data])

  // ===== 派生数据：转换为 StrategyRunRow =====
  const strategyRunRows: StrategyRunRow[] = useMemo(() => {
    const runs = strategyRunsQuery.data?.items ?? []
    return runs.map((run) => {
      const succeeded = run.succeeded_count ?? 0
      const failed = run.failed_count ?? 0
      const skipped = run.skipped_count ?? 0
      const total = run.total_instruments
      const done = succeeded + failed + skipped
      const progress = total != null && total > 0 ? done / total : null
      return {
        id: run.id,
        trade_date: run.trade_date,
        run_type: run.run_type,
        status: run.status,
        status_pill_class: statusToPillClass(run.status),
        status_text: statusToText(run.status),
        progress,
        progress_text: progress != null ? `${(progress * 100).toFixed(0)}%` : '-',
        succeeded_count: run.succeeded_count,
        failed_count: run.failed_count,
        skipped_count: run.skipped_count,
        started_at: formatShanghaiTime(run.started_at),
        finished_at: formatShanghaiTime(run.finished_at),
        published_at: formatShanghaiTime(run.published_at),
        raw: run,
      }
    })
  }, [strategyRunsQuery.data])

  // ===== KPI 计算 =====
  // [AdminJobsPage] - 描述: 运行中任务仅统计 status==='running'，不含 interrupted/warn 等其他状态
  const kpiRunning = useMemo(
    () => jobRunRows.filter((r) => r.status === 'running').length,
    [jobRunRows],
  )
  const kpiFailed = useMemo(
    () => jobRunRows.filter((r) => r.status_pill_class === 'bad').length,
    [jobRunRows],
  )
  const kpiEvents = eventsQuery.data?.total ?? 0
  const kpiMessages = messagesQuery.data?.total ?? 0

  // ===== 事件列表 =====
  const events = eventsQuery.data?.items ?? []

  // ===== 失败投递列表：直接查询 message_deliveries 表的 failed 记录 =====
  const failedDeliveries = useMemo<MessageDelivery[]>(() => {
    return deliveriesQuery.data ?? []
  }, [deliveriesQuery.data])

  // [AdminJobsPage] - 描述: 事件 instrument 批量查询，单次 POST /instruments/batch 替代 N+1 循环
  const eventInstrumentIds = useMemo(
    () => [...new Set(events.map((e) => e.instrument_id))],
    [events],
  )
  const batchInstrumentsQuery = useBatchInstruments(eventInstrumentIds)
  const instrumentMap = useMemo(() => {
    const m = new Map<string, Instrument>()
    batchInstrumentsQuery.data?.items.forEach((inst) => {
      m.set(inst.id, inst)
    })
    return m
  }, [batchInstrumentsQuery.data])

  // ===== 选中运行详情（抽屉展示用）=====
  const selectedRun = useMemo(
    () => jobRunRows.find((r) => r.id === selectedRunId)?.raw ?? null,
    [jobRunRows, selectedRunId],
  )

  // ===== 选中策略运行详情（抽屉展示用）=====
  const selectedStrategyRun = useMemo(
    () => strategyRunRows.find((r) => r.id === selectedStrategyRunId)?.raw ?? null,
    [strategyRunRows, selectedStrategyRunId],
  )

  // ===== 事件详情（抽屉展示用）=====
  const eventDetail = eventDetailQuery.data

  // ===== 事件处理 =====

  /** 打开任务详情抽屉 */
  const handleOpenJobDrawer = useCallback((runId: string) => {
    setSelectedRunId(runId)
    setJobDrawerOpen(true)
  }, [])

  /** 打开策略运行详情抽屉 */
  const handleOpenStrategyDrawer = useCallback((runId: string) => {
    setSelectedStrategyRunId(runId)
    setStrategyDrawerOpen(true)
  }, [])

  /** 打开事件快照抽屉 */
  const handleOpenEventDrawer = useCallback((eventId: string) => {
    setSelectedEventId(eventId)
    setEventDrawerOpen(true)
  }, [])

  /** 手动重跑：调用 useTriggerStrategyRun 创建重跑任务 */
  const handleRerun = useCallback(async () => {
    if (!rerunTradeDate) {
      toast.show('重跑失败', '请选择交易日')
      return
    }
    try {
      await triggerRun.mutateAsync({
        strategyKey: rerunTaskType,
        payload: {
          trade_date: rerunTradeDate,
          run_type: 'manual',
        },
      })
      toast.show('重跑任务已创建', `${rerunTaskType} · ${rerunTradeDate}`)
      setRerunModalOpen(false)
    } catch {
      toast.show('重跑失败', '请稍后重试')
    }
  }, [rerunTaskType, rerunTradeDate, triggerRun, toast])

  /** 重试失败投递：调用 POST /admin/message-deliveries/{id}/retry */
  const handleRetryDelivery = useCallback(
    async (deliveryId: string) => {
      try {
        await retryDelivery.mutateAsync(deliveryId)
        toast.show('重试成功', '投递记录已重新尝试')
      } catch {
        toast.show('重试失败', '请稍后重试')
      }
    },
    [retryDelivery, toast],
  )

  // ===== 列定义 =====

  const jobRunColumns: DataTableColumn<JobRunRow>[] = useMemo(
    () => [
      {
        key: 'id',
        title: '运行 ID',
        dataType: 'text',
        sortable: true,
        filterable: true,
        sortValue: (row) => row.id,
        render: (row) => <span className="num">{row.id}</span>,
      },
      {
        key: 'job_name',
        title: '任务名',
        dataType: 'text',
        sortable: true,
        filterable: true,
        helpText:
          '盘中监控按上午盘/下午盘记录；盘后行情在 16:00 启动；DSA 由行情覆盖率达标后触发，18:30 有兜底调度',
        render: (row) => (
          <div>
            <div className="job-name-cn">{JOB_NAME_CN_MAP[row.job_name] ?? row.job_name}</div>
            <div className="job-name-raw">{row.job_name}</div>
          </div>
        ),
      },
      {
        key: 'business_date',
        title: '业务日期',
        dataType: 'text',
        sortable: true,
        filterable: true,
        sortValue: (row) => row.business_date ?? '',
        filterValue: (row) => row.business_date ?? '',
        render: (row) => <span className="num">{row.business_date ?? '-'}</span>,
      },
      {
        key: 'scheduled_at',
        title: '计划时间',
        dataType: 'datetime',
        sortable: true,
        filterable: true,
        sortValue: (row) => row.scheduled_at,
        render: (row) => <span className="num">{row.scheduled_at}</span>,
      },
      {
        key: 'started_at',
        title: '开始',
        dataType: 'datetime',
        sortable: true,
        filterable: true,
        sortValue: (row) => row.started_at,
        render: (row) => <span className="num">{row.started_at}</span>,
      },
      {
        key: 'finished_at',
        title: '结束',
        dataType: 'datetime',
        sortable: true,
        filterable: true,
        sortValue: (row) => row.finished_at,
        render: (row) => <span className="num">{row.finished_at}</span>,
      },
      {
        key: 'heartbeat_at',
        title: '心跳',
        dataType: 'datetime',
        sortable: true,
        filterable: false,
        sortValue: (row) => row.heartbeat_at,
        render: (row) => <span className="num">{row.heartbeat_at}</span>,
      },
      {
        key: 'processed',
        title: '处理',
        dataType: 'text',
        sortable: false,
        filterable: false,
        render: (row) => <span className="num">{row.processed}</span>,
      },
      {
        key: 'status',
        title: '状态',
        dataType: 'enum',
        sortable: true,
        filterable: true,
        enumOptions: [
          { label: '成功', value: 'succeeded' },
          { label: '失败', value: 'failed' },
          { label: '运行中', value: 'running' },
          { label: '已中断', value: 'interrupted' },
        ],
        sortValue: (row) => row.status_text,
        filterValue: (row) => row.status_text,
        render: (row) => (
          <span className={`status-pill ${row.status_pill_class}`}>{row.status_text}</span>
        ),
      },
      {
        key: 'duration',
        title: '耗时',
        dataType: 'text',
        sortable: true,
        filterable: true,
        sortValue: (row) => row.duration,
        render: (row) => <span className="num">{row.duration}</span>,
      },
      {
        key: 'action',
        title: '',
        dataType: 'text',
        sortable: false,
        filterable: false,
        isAction: true,
        render: (row) => (
          <button className="btn small" onClick={() => handleOpenJobDrawer(row.id)}>
            详情
          </button>
        ),
      },
    ],
    [handleOpenJobDrawer],
  )

  const strategyRunColumns: DataTableColumn<StrategyRunRow>[] = useMemo(
    () => [
      {
        key: 'trade_date',
        title: '交易日',
        dataType: 'text',
        sortable: true,
        filterable: true,
        sortValue: (row) => row.trade_date ?? '',
        filterValue: (row) => row.trade_date ?? '',
        render: (row) => <span className="num">{row.trade_date ?? '-'}</span>,
      },
      {
        key: 'id',
        title: '运行 ID',
        dataType: 'text',
        sortable: true,
        filterable: true,
        sortValue: (row) => row.id,
        render: (row) => <span className="num">{row.id}</span>,
      },
      {
        key: 'run_type',
        title: '触发方式',
        dataType: 'text',
        sortable: true,
        filterable: true,
        render: (row) => row.run_type,
      },
      {
        key: 'status',
        title: '状态',
        dataType: 'enum',
        sortable: true,
        filterable: true,
        enumOptions: [
          { label: '成功', value: 'succeeded' },
          { label: '失败', value: 'failed' },
          { label: '运行中', value: 'running' },
          { label: '已中断', value: 'interrupted' },
          { label: '已发布', value: 'published' },
        ],
        sortValue: (row) => row.status_text,
        filterValue: (row) => row.status_text,
        render: (row) => (
          <span className={`status-pill ${row.status_pill_class}`}>{row.status_text}</span>
        ),
      },
      {
        key: 'progress',
        title: '处理进度',
        dataType: 'text',
        sortable: true,
        filterable: false,
        sortValue: (row) => row.progress ?? 0,
        render: (row) => <span className="num">{row.progress_text}</span>,
      },
      {
        key: 'counts',
        title: '成功 / 失败 / 跳过',
        dataType: 'text',
        sortable: false,
        filterable: false,
        render: (row) => (
          <span className="num">
            {row.succeeded_count ?? 0} / {row.failed_count ?? 0} / {row.skipped_count ?? 0}
          </span>
        ),
      },
      {
        key: 'started_at',
        title: '最后心跳',
        dataType: 'datetime',
        sortable: true,
        filterable: false,
        render: (row) => <span className="num">{row.started_at}</span>,
      },
      {
        key: 'finished_at',
        title: '完成时间',
        dataType: 'datetime',
        sortable: true,
        filterable: false,
        render: (row) => <span className="num">{row.finished_at}</span>,
      },
      {
        key: 'published_at',
        title: '发布时间',
        dataType: 'datetime',
        sortable: true,
        filterable: false,
        render: (row) => <span className="num">{row.published_at}</span>,
      },
      {
        key: 'action',
        title: '',
        dataType: 'text',
        sortable: false,
        filterable: false,
        isAction: true,
        render: (row) => (
          <button className="btn small" onClick={() => handleOpenStrategyDrawer(row.id)}>
            详情
          </button>
        ),
      },
    ],
    [handleOpenStrategyDrawer],
  )

  // ===== 渲染 =====
  return (
    <>
      {/* 页头 */}
      <div className="page-head">
        <div>
          <h1 className="page-title">任务与事件</h1>
          <div className="page-desc">
            任务运行、全局碰触事件、用户分发和第三方投递均具备独立状态与幂等键
          </div>
        </div>
        <div className="actions">
          <button className="btn primary" onClick={() => setRerunModalOpen(true)}>
            手动重跑
          </button>
        </div>
      </div>

      {/* KPI 4 列 */}
      <div className="grid kpi">
        {/* KPI 1：运行中任务数 */}
        <div className="card kpi-card">
          <div className="kpi-label">运行中任务</div>
          <div className="kpi-value">
            {schedulerJobRunsQuery.isLoading ? '-' : kpiRunning}
          </div>
        </div>
        {/* KPI 2：失败任务数 */}
        <div className="card kpi-card">
          <div className="kpi-label">失败任务</div>
          <div className="kpi-value neg">
            {schedulerJobRunsQuery.isLoading ? '-' : kpiFailed}
          </div>
          <div className="kpi-foot">最近 24 小时</div>
        </div>
        {/* KPI 3：全部全局事件数（events API 按 event_time 范围查询，非今日维度） */}
        <div className="card kpi-card">
          <div className="kpi-label">全部事件</div>
          <div className="kpi-value">{eventsQuery.isLoading ? '-' : kpiEvents}</div>
        </div>
        {/* KPI 4：全部用户消息数（messages API 无日期过滤） */}
        <div className="card kpi-card">
          <div className="kpi-label">全部消息</div>
          <div className="kpi-value">{messagesQuery.isLoading ? '-' : kpiMessages}</div>
        </div>
      </div>

      {/* Tab 切换 */}
      <div className="jobs-tabs" role="tablist">
        <button
          className={`jobs-tab ${activeTab === 'scheduler' ? 'active' : ''}`}
          onClick={() => setActiveTab('scheduler')}
          type="button"
          role="tab"
          aria-selected={activeTab === 'scheduler'}
        >
          定时任务
        </button>
        <button
          className={`jobs-tab ${activeTab === 'strategy' ? 'active' : ''}`}
          onClick={() => setActiveTab('strategy')}
          type="button"
          role="tab"
          aria-selected={activeTab === 'strategy'}
        >
          策略计算
        </button>
      </div>

      {/* 定时任务运行记录表 */}
      {activeTab === 'scheduler' && (
        <section className="card section-gap">
          <div className="card-head">
            <div>
              <div className="card-title">定时任务运行记录</div>
              <div className="card-sub">SchedulerJobRun 记录 bars / strategy / calendar / monitor 各调度任务</div>
            </div>
            <div className="chip-row">
              <span className="chip green">成功</span>
              <span className="chip orange">运行中</span>
              <span className="chip red">失败</span>
            </div>
          </div>
          <StrategyDataTable
            tableId="admin-jobs-runs"
            columns={jobRunColumns}
            rows={jobRunRows}
            rowKey={(row) => row.id}
            loading={schedulerJobRunsQuery.isLoading}
            error={schedulerJobRunsQuery.isError ? '定时任务运行记录加载失败' : null}
            searchable={false}
            emptyText="暂无定时任务运行记录"
          />
        </section>
      )}

      {/* 策略计算运行记录表 */}
      {activeTab === 'strategy' && (
        <section className="card section-gap">
          <div className="card-head">
            <div>
              <div className="card-title">策略计算运行记录</div>
              <div className="card-sub">趋势选股运行历史（admin）</div>
            </div>
            <div className="chip-row">
              <span className="chip green">成功</span>
              <span className="chip orange">运行中</span>
              <span className="chip red">失败</span>
            </div>
          </div>
          <StrategyDataTable
            tableId="admin-strategy-runs"
            columns={strategyRunColumns}
            rows={strategyRunRows}
            rowKey={(row) => row.id}
            loading={strategyRunsQuery.isLoading}
            error={strategyRunsQuery.isError ? '策略运行记录加载失败' : null}
            searchable={false}
            emptyText="暂无策略计算运行记录"
          />
        </section>
      )}

      {/* split-even：最近全局事件 + 失败投递 */}
      <div className="grid split-even section-gap">
        {/* 最近全局事件列表 */}
        <section className="card">
          <div className="card-head">
            <div>
              <div className="card-title">最近全局事件</div>
              <div className="card-sub">事件快照与用户通知解耦</div>
            </div>
          </div>
          <div className="list">
            {eventsQuery.isLoading && <div className="notice">加载中…</div>}
            {!eventsQuery.isLoading && events.length === 0 && (
              <div className="notice">暂无全局事件</div>
            )}
            {events.map((event) => {
              const inst = instrumentMap.get(event.instrument_id)
              const symbol = inst?.symbol ?? event.instrument_id.slice(0, 8)
              const payload = event.payload
              const nodeLow = pickPayload(payload, ['node_low', 'low', 'node.low'])
              const nodeHigh = pickPayload(payload, ['node_high', 'high', 'node.high'])
              const isPoc = pickPayload(payload, ['is_poc', 'poc'])
              const distributeCount = pickPayload(payload, [
                'distribute_count',
                'user_count',
                'distributed_to',
              ])
              return (
                <div className="list-item" key={event.id}>
                  <div className="list-icon">N</div>
                  <div className="list-main">
                    <div className="list-title">
                      {symbol} · {event.event_type} · {event.logical_entity_id ?? '-'}
                    </div>
                    <div className="list-meta">
                      {formatShanghaiTime(event.event_time)}
                      {nodeLow !== undefined && nodeHigh !== undefined
                        ? ` · 节点 ${nodeLow}–${nodeHigh}`
                        : ''}
                      {isPoc ? ' · POC' : ''}
                      {distributeCount !== undefined ? ` · 分发给 ${distributeCount} 个用户` : ''}
                    </div>
                  </div>
                  <button
                    className="btn small"
                    onClick={() => handleOpenEventDrawer(event.id)}
                  >
                    快照
                  </button>
                </div>
              )
            })}
          </div>
        </section>

        {/* 失败投递列表 */}
        <section className="card">
          <div className="card-head">
            <div>
              <div className="card-title">失败投递</div>
              <div className="card-sub">第三方渠道失败不影响站内消息</div>
            </div>
          </div>
          <div className="list">
            {deliveriesQuery.isLoading && <div className="notice">加载中…</div>}
            {!deliveriesQuery.isLoading && failedDeliveries.length === 0 && (
              <div className="notice">暂无失败投递</div>
            )}
            {failedDeliveries.map((delivery) => (
              <div className="list-item" key={delivery.id}>
                <div className="list-icon danger">!</div>
                <div className="list-main">
                  <div className="list-title">
                    {delivery.primary_instrument?.name || delivery.primary_instrument?.symbol || delivery.message_summary || '未知消息'} · {delivery.last_error_code ?? '未知错误'}
                  </div>
                  <div className="list-meta">
                    {delivery.display_name} · {delivery.adapter_type} · 已尝试 {delivery.attempt_count} 次
                  </div>
                </div>
                <button
                  className="btn small"
                  onClick={() => handleRetryDelivery(delivery.id)}
                  disabled={retryDelivery.isPending}
                >
                  重试
                </button>
              </div>
            ))}
          </div>
        </section>
      </div>

      {/* 任务详情抽屉 jobDrawer */}
      {jobDrawerOpen && (
        <div className="drawer-backdrop open" onClick={() => setJobDrawerOpen(false)}>
          <aside className="drawer" onClick={(e) => e.stopPropagation()}>
            <div className="drawer-head">
              <div>
                <b>任务详情 · {selectedRun?.id ?? '-'}</b>
                <div className="card-sub">
                  {selectedRun?.job_name ?? '-'} · {selectedRun?.business_date ?? '-'}
                </div>
              </div>
              <button className="icon-btn" onClick={() => setJobDrawerOpen(false)}>
                ×
              </button>
            </div>
            <div className="drawer-body">
              {selectedRun && (
                <>
                  <div className={`notice ${selectedRun.status === 'failed' ? 'error' : selectedRun.status === 'running' ? 'warn' : ''}`}>
                    任务状态：{statusToText(selectedRun.status)}
                    {selectedRun.error_code ? ` · ${selectedRun.error_code}` : ''}
                  </div>
                  <div className="card section-gap">
                    <div className="card-body">
                      <div className="toggle-row">
                        <span>任务名</span>
                        <b className="num">{selectedRun.job_name}</b>
                      </div>
                      <div className="toggle-row">
                        <span>业务日期</span>
                        <b className="num">{selectedRun.business_date ?? '-'}</b>
                      </div>
                      <div className="toggle-row">
                        <span>计划时间</span>
                        <b className="num">{formatShanghaiTime(selectedRun.scheduled_at)}</b>
                      </div>
                      <div className="toggle-row">
                        <span>开始时间</span>
                        <b className="num">{formatShanghaiTime(selectedRun.started_at)}</b>
                      </div>
                      <div className="toggle-row">
                        <span>结束时间</span>
                        <b className="num">{formatShanghaiTime(selectedRun.finished_at)}</b>
                      </div>
                      <div className="toggle-row">
                        <span>最后心跳</span>
                        <b className="num">{formatShanghaiTime(selectedRun.heartbeat_at)}</b>
                      </div>
                      <div className="toggle-row">
                        <span>租约到期</span>
                        <b className="num">{formatShanghaiTime(selectedRun.lease_expires_at)}</b>
                      </div>
                      <div className="toggle-row">
                        <span>当前子任务 ID</span>
                        <b className="num">
                          {parseJobMetadata(selectedRun.metadata_json).current_run_id || '-'}
                        </b>
                      </div>
                      <div className="toggle-row">
                        <span>最新处理 Bar</span>
                        <b className="num">
                          {(() => {
                            const lb = parseJobMetadata(selectedRun.metadata_json).last_bar_time
                            return lb ? formatShanghaiTime(lb) : '-'
                          })()}
                        </b>
                      </div>
                      <div className="toggle-row">
                        <span>耗时</span>
                        <b className="num">
                          {formatDuration(selectedRun.started_at, selectedRun.finished_at)}
                        </b>
                      </div>
                      <div className="toggle-row">
                        <span>处理进度</span>
                        <b className="num">
                          {selectedRun.succeeded_count ?? 0} 成功 / {selectedRun.failed_count ?? 0} 失败
                          {selectedRun.total_count != null ? ` / ${selectedRun.total_count} 总计` : ''}
                          {selectedRun.progress != null ? ` (${(selectedRun.progress * 100).toFixed(0)}%)` : ''}
                        </b>
                      </div>
                    </div>
                  </div>
                  {/* 错误信息（仅失败时展示） */}
                  {selectedRun.error_message && (
                    <div className="card section-gap">
                      <div className="card-head">
                        <div className="card-title">错误信息</div>
                      </div>
                      <div className="card-body">
                        <pre className="json-snapshot">{selectedRun.error_message}</pre>
                      </div>
                    </div>
                  )}
                  {/* 元数据 JSON（仅当有内容时展示） */}
                  {selectedRun.metadata_json && (
                    <div className="card section-gap">
                      <div className="card-head">
                        <div className="card-title">元数据</div>
                      </div>
                      <div className="card-body">
                        <pre className="json-snapshot">{selectedRun.metadata_json}</pre>
                      </div>
                    </div>
                  )}
                  {/* 事件时间线（按需加载，展示任务执行步骤事件） */}
                  <div className="card section-gap">
                    <div className="card-head">
                      <div className="card-title">事件时间线</div>
                    </div>
                    <div className="card-body">
                      <JobRunEventTimeline runId={selectedRun.id} />
                    </div>
                  </div>
                </>
              )}
            </div>
            <div className="drawer-foot">
              <button className="btn" onClick={() => setJobDrawerOpen(false)}>
                关闭
              </button>
            </div>
          </aside>
        </div>
      )}

      {/* 策略运行详情抽屉 strategyDrawer */}
      {strategyDrawerOpen && (
        <div className="drawer-backdrop open" onClick={() => setStrategyDrawerOpen(false)}>
          <aside className="drawer" onClick={(e) => e.stopPropagation()}>
            <div className="drawer-head">
              <div>
                <b>策略运行详情 · {selectedStrategyRun?.id ?? '-'}</b>
                <div className="card-sub">
                  {selectedStrategyRun?.trade_date ?? '-'} · {selectedStrategyRun?.run_type ?? '-'}
                </div>
              </div>
              <button className="icon-btn" onClick={() => setStrategyDrawerOpen(false)}>
                ×
              </button>
            </div>
            <div className="drawer-body">
              {selectedStrategyRun && (
                <>
                  <div className={`notice ${selectedStrategyRun.status === 'failed' ? 'error' : selectedStrategyRun.status === 'running' ? 'warn' : ''}`}>
                    运行状态：{statusToText(selectedStrategyRun.status)}
                  </div>
                  <div className="card section-gap">
                    <div className="card-body">
                      <div className="toggle-row">
                        <span>运行 ID</span>
                        <b className="num">{selectedStrategyRun.id}</b>
                      </div>
                      <div className="toggle-row">
                        <span>交易日</span>
                        <b className="num">{selectedStrategyRun.trade_date ?? '-'}</b>
                      </div>
                      <div className="toggle-row">
                        <span>触发方式</span>
                        <b className="num">{selectedStrategyRun.run_type}</b>
                      </div>
                      <div className="toggle-row">
                        <span>开始时间</span>
                        <b className="num">{formatShanghaiTime(selectedStrategyRun.started_at)}</b>
                      </div>
                      <div className="toggle-row">
                        <span>完成时间</span>
                        <b className="num">{formatShanghaiTime(selectedStrategyRun.finished_at)}</b>
                      </div>
                      <div className="toggle-row">
                        <span>发布时间</span>
                        <b className="num">{formatShanghaiTime(selectedStrategyRun.published_at)}</b>
                      </div>
                      <div className="toggle-row">
                        <span>处理进度</span>
                        <b className="num">
                          {selectedStrategyRun.succeeded_count ?? 0} 成功 / {selectedStrategyRun.failed_count ?? 0} 失败 / {selectedStrategyRun.skipped_count ?? 0} 跳过
                          {selectedStrategyRun.total_instruments != null ? ` / ${selectedStrategyRun.total_instruments} 总计` : ''}
                        </b>
                      </div>
                    </div>
                  </div>
                </>
              )}
            </div>
            <div className="drawer-foot">
              <button className="btn" onClick={() => setStrategyDrawerOpen(false)}>
                关闭
              </button>
            </div>
          </aside>
        </div>
      )}

      {/* 事件快照抽屉 eventDrawer */}
      {eventDrawerOpen && (
        <div className="drawer-backdrop open" onClick={() => setEventDrawerOpen(false)}>
          <aside className="drawer" onClick={(e) => e.stopPropagation()}>
            <div className="drawer-head">
              <div>
                <b>事件快照</b>
                <div className="card-sub">{selectedEventId ?? '-'}</div>
              </div>
              <button className="icon-btn" onClick={() => setEventDrawerOpen(false)}>
                ×
              </button>
            </div>
            <div className="drawer-body">
              {eventDetailQuery.isLoading && <div className="notice">加载中…</div>}
              {eventDetailQuery.isError && (
                <div className="notice error">事件详情加载失败</div>
              )}
              {eventDetail && (
                <>
                  {/* JSON pre 展示事件结构（symbol/strategy_version/bar_time/bar/logical_node_id/node/is_poc/long_volume/short_volume） */}
                  <pre className="json-snapshot">
                    {JSON.stringify(eventDetail.snapshot, null, 2)}
                  </pre>
                  {/* 分发统计 */}
                  <div className="notice section-gap">
                    事件类型：{eventDetail.event_type} · 事件时间：
                    {formatShanghaiTime(eventDetail.event_time)} · 逻辑实体：
                    {eventDetail.logical_entity_id ?? '-'}
                  </div>
                </>
              )}
            </div>
          </aside>
        </div>
      )}

      {/* 手动重跑弹窗 rerunModal */}
      {rerunModalOpen && (
        <div className="modal-backdrop open" onClick={() => setRerunModalOpen(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-head">
              <b>手动重跑</b>
              <button className="icon-btn" onClick={() => setRerunModalOpen(false)}>
                ×
              </button>
            </div>
            <div className="modal-body">
              <div className="form-grid">
                {/* 任务类型 */}
                <div className="form-row">
                  <label className="form-label">任务类型</label>
                  <select
                    className="select"
                    value={rerunTaskType}
                    onChange={(e) => setRerunTaskType(e.target.value)}
                  >
                    <option value={STRATEGY_KEYS.DSA_SELECTOR}>DSA 指定日期</option>
                  </select>
                </div>
                {/* 交易日 */}
                <div className="form-row">
                  <label className="form-label">交易日</label>
                  <input
                    className="input"
                    type="date"
                    value={rerunTradeDate}
                    onChange={(e) => setRerunTradeDate(e.target.value)}
                  />
                </div>
              </div>
            </div>
            <div className="modal-foot">
              <button className="btn" onClick={() => setRerunModalOpen(false)}>
                取消
              </button>
              <button
                className="btn primary"
                onClick={handleRerun}
                disabled={triggerRun.isPending}
              >
                {triggerRun.isPending ? '创建中...' : '创建任务'}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  )
}
