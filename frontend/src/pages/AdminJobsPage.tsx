// 任务与事件页（受保护路由，admin only）
// 对应原型：admin/jobs.html (V1.6.3)
//
// 用法：
// 1. 路由 /admin/jobs，受保护路由（经 ProtectedLayout + AdminRoute 包裹）
// 2. KPI 4 列：运行中数 / 失败数 / 今日全局事件数 / 今日用户消息数
// 3. 定时任务运行记录表：使用 StrategyDataTable，列含运行ID/任务名/业务日期/计划时间/开始/结束/处理/状态/耗时/详情
// 4. split-even 布局：最近全局事件列表（含快照按钮）+ 失败投递列表（含重试按钮）
// 5. 任务详情抽屉 jobDrawer：任务名/业务日期/计划时间/开始/结束/耗时/处理进度/错误信息/元数据
// 6. 事件快照抽屉 eventDrawer：JSON pre 展示事件结构 + 分发统计
// 7. 手动重跑弹窗 rerunModal：任务类型/交易日/运行时覆盖JSON
//
// 依赖 hooks：
// - useSchedulerJobRuns：获取定时任务运行记录（SchedulerJobRun）
// - useStrategyEvents：获取事件列表（查询 watchlist_monitor 策略事件）
// - useStrategyEventDetail：获取事件详情（含 snapshot 快照，抽屉打开时按需加载）
// - useMessages：获取用户消息总数（KPI 4，当前用户维度）
// - useNotificationChannels：获取通知渠道（筛选失败状态作为失败投递列表）
// - useTriggerStrategyRun：手动重跑 DSA 策略
// - useToast：操作反馈

import { useState, useMemo, useCallback } from 'react'
import { useQueries } from '@tanstack/react-query'
import {
  useSchedulerJobRuns,
  useStrategyEvents,
  useStrategyEventDetail,
  useMessages,
  useMessageDeliveries,
  useRetryMessageDelivery,
  useTriggerStrategyRun,
} from '@/hooks/useApi'
import { STRATEGY_KEYS } from '@/constants/strategyKeys'
import * as api from '@/api/endpoints'
import type { SchedulerJobRunItem, Instrument, MessageDelivery } from '@/api/endpoints'
import { useToast } from '@/store/toast'
import { StrategyDataTable } from '@/components/StrategyDataTable'
import type { DataTableColumn } from '@/components/StrategyDataTable'

// ===== 类型定义 =====

/** 定时任务运行记录表行类型（带索引签名以满足 StrategyDataTable 的 Row extends Record<string, unknown>） */
interface JobRunRow {
  id: string
  job_name: string
  business_date: string | null
  scheduled_at: string
  started_at: string
  finished_at: string
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

// ===== 工具函数 =====

/** 格式化 ISO 时间为 HH:MM:SS，无效返回 '-' */
function formatTime(iso: string | null | undefined): string {
  if (!iso) return '-'
  try {
    return new Date(iso).toLocaleTimeString('zh-CN', {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    })
  } catch {
    return '-'
  }
}

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
  if (s === 'success' || s === 'succeeded' || s === 'completed') return 'ok'
  if (s === 'failed' || s === 'error') return 'bad'
  if (s === 'running' || s === 'retrying' || s === 'partial' || s === 'partial_success') return 'warn'
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
  if (s === 'pending') return '等待中'
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

// ===== 主页面 =====

export default function AdminJobsPage() {
  const toast = useToast.getState()

  // ===== 状态：抽屉/弹窗开关 =====
  const [jobDrawerOpen, setJobDrawerOpen] = useState(false)
  const [eventDrawerOpen, setEventDrawerOpen] = useState(false)
  const [rerunModalOpen, setRerunModalOpen] = useState(false)
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null)
  const [selectedEventId, setSelectedEventId] = useState<string | null>(null)

  // ===== 状态：手动重跑表单 =====
  const [rerunTaskType, setRerunTaskType] = useState<string>(STRATEGY_KEYS.DSA_SELECTOR)
  const [rerunTradeDate, setRerunTradeDate] = useState('')
  const [rerunOverrides, setRerunOverrides] = useState('')

  // ===== 查询：定时任务运行记录（SchedulerJobRun）=====
  const schedulerJobRunsQuery = useSchedulerJobRuns({ limit: 20 })

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
      return {
        id: run.id,
        job_name: run.job_name,
        business_date: run.business_date,
        scheduled_at: formatTime(run.scheduled_at),
        started_at: formatTime(run.started_at),
        finished_at: formatTime(run.finished_at),
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

  // ===== KPI 计算 =====
  const kpiRunning = useMemo(
    () => jobRunRows.filter((r) => r.status_pill_class === 'warn').length,
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

  // ===== 事件列表 instrument 查询（批量查询构建 symbol 映射）=====
  const eventInstrumentIds = useMemo(
    () => [...new Set(events.map((e) => e.instrument_id))],
    [events],
  )
  const instrumentQueries = useQueries({
    queries: eventInstrumentIds.map((id) => ({
      queryKey: ['instruments', id],
      queryFn: () => api.getInstrumentById(id),
      staleTime: 5 * 60 * 1000,
    })),
  })
  const instrumentMap = useMemo(() => {
    const m = new Map<string, Instrument>()
    instrumentQueries.forEach((q, i) => {
      if (q.data) m.set(eventInstrumentIds[i], q.data)
    })
    return m
  }, [instrumentQueries, eventInstrumentIds])

  // ===== 选中运行详情（抽屉展示用）=====
  const selectedRun = useMemo(
    () => jobRunRows.find((r) => r.id === selectedRunId)?.raw ?? null,
    [jobRunRows, selectedRunId],
  )

  // ===== 事件详情（抽屉展示用）=====
  const eventDetail = eventDetailQuery.data

  // ===== 事件处理 =====

  /** 打开任务详情抽屉 */
  const handleOpenJobDrawer = useCallback((runId: string) => {
    setSelectedRunId(runId)
    setJobDrawerOpen(true)
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
      setRerunOverrides('')
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

  /** 导出日志（当前无后端导出接口，显示 toast 提示） */
  const handleExportLogs = useCallback(() => {
    toast.show('导出日志', '日志导出功能开发中')
  }, [toast])

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
        render: (row) => row.job_name,
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
          <button className="btn" onClick={handleExportLogs}>
            导出日志
          </button>
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
        {/* KPI 3：今日全局事件数 */}
        <div className="card kpi-card">
          <div className="kpi-label">今日全局事件</div>
          <div className="kpi-value">{eventsQuery.isLoading ? '-' : kpiEvents}</div>
        </div>
        {/* KPI 4：今日用户消息数 */}
        <div className="card kpi-card">
          <div className="kpi-label">今日用户消息</div>
          <div className="kpi-value">{messagesQuery.isLoading ? '-' : kpiMessages}</div>
        </div>
      </div>

      {/* 定时任务运行记录表 */}
      <section className="card">
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
                      {formatTime(event.event_time)}
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
                        <b className="num">{formatTime(selectedRun.scheduled_at)}</b>
                      </div>
                      <div className="toggle-row">
                        <span>开始时间</span>
                        <b className="num">{formatTime(selectedRun.started_at)}</b>
                      </div>
                      <div className="toggle-row">
                        <span>结束时间</span>
                        <b className="num">{formatTime(selectedRun.finished_at)}</b>
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
                    {formatTime(eventDetail.event_time)} · 逻辑实体：
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
                {/* 运行时覆盖（审计） */}
                <div className="form-row full">
                  <label className="form-label">运行时覆盖（审计）</label>
                  <textarea
                    className="json-textarea"
                    placeholder="可选 JSON，仅用于本次运行"
                    value={rerunOverrides}
                    onChange={(e) => setRerunOverrides(e.target.value)}
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
