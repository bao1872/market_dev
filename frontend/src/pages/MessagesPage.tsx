// 消息中心页（受保护路由）
// 对应原型：messages.html (V1.6.3)
// 用法：统一管理组合方案消息、单策略过程事件与系统消息，支持按类型/时间筛选与标记已读
// 依赖 hooks：useMessages / useMarkMessageRead
// 路由：/messages
import { useState, useMemo, useCallback } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useToast } from '@/store/toast'
import { useMessages, useMarkMessageRead } from '@/hooks/useApi'
import type { NotificationMessage } from '@/api/endpoints'
import { StrategyDataTable } from '@/components/StrategyDataTable'
import type { DataTableColumn } from '@/components/StrategyDataTable'

// ===== 类型定义 =====

/** 消息筛选维度：全部 / 未读 / 选股组合 / 监控组合 / 过程事件 / 系统 */
type MessageFilter = 'all' | 'unread' | 'selection' | 'monitoring' | 'process' | 'system'

/** 时间范围：最近 7 天 / 最近 30 天 */
type TimeRange = '7d' | '30d'

/** 投递状态 pill 样式 */
type DeliveryPill = 'ok' | 'off' | 'warn'

/** 消息行类型（从 NotificationMessage 派生，带索引签名以满足 StrategyDataTable 约束） */
interface MessageRow {
  id: string
  message_type: string
  type_label: string
  type_tag: 'good' | 'info' | 'warn'
  plan_name: string
  title: string
  subtitle: string
  time_text: string
  created_at: string
  delivery_label: string
  delivery_pill: DeliveryPill
  unread: boolean
  navigate_target: string
  [key: string]: unknown
}

// ===== 常量 =====

/** 消息类型 → 中文标签 + tag 样式 */
const TYPE_META: Record<string, { label: string; tag: 'good' | 'info' | 'warn' }> = {
  monitoring_composite: { label: '监控组合', tag: 'good' },
  selection_composite: { label: '选股组合', tag: 'good' },
  process_event: { label: '过程事件', tag: 'info' },
  system: { label: '系统', tag: 'warn' },
}

/** 筛选项配置（对应原型 segmented 按钮） */
const FILTER_OPTIONS: Array<{ value: MessageFilter; label: string }> = [
  { value: 'all', label: '全部' },
  { value: 'unread', label: '未读' },
  { value: 'selection', label: '选股组合' },
  { value: 'monitoring', label: '监控组合' },
  { value: 'process', label: '过程事件' },
  { value: 'system', label: '系统' },
]

/** 时间范围配置（对应原型 select 下拉） */
const TIME_RANGE_OPTIONS: Array<{ value: TimeRange; label: string }> = [
  { value: '7d', label: '最近 7 天' },
  { value: '30d', label: '最近 30 天' },
]

// ===== 工具函数 =====

/** 从消息 body 中按候选 key 列表取第一个非空字符串值 */
function pickBodyStr(body: Record<string, unknown>, keys: string[]): string {
  for (const k of keys) {
    const v = body[k]
    if (v !== undefined && v !== null && v !== '') return String(v)
  }
  return ''
}

/** 格式化消息时间：今日显示 HH:MM，昨日显示"昨日 HH:MM"，更早显示 MM-DD HH:MM */
function formatMessageTime(isoString: string): string {
  try {
    const date = new Date(isoString)
    const now = new Date()
    const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate())
    const yesterdayStart = new Date(todayStart.getTime() - 86400000)
    const msgDate = new Date(date.getFullYear(), date.getMonth(), date.getDate())

    const hh = String(date.getHours()).padStart(2, '0')
    const mm = String(date.getMinutes()).padStart(2, '0')

    if (msgDate.getTime() === todayStart.getTime()) {
      return `${hh}:${mm}`
    }
    if (msgDate.getTime() === yesterdayStart.getTime()) {
      return `昨日 ${hh}:${mm}`
    }
    const M = String(date.getMonth() + 1).padStart(2, '0')
    const D = String(date.getDate()).padStart(2, '0')
    return `${M}-${D} ${hh}:${mm}`
  } catch {
    return '-'
  }
}

/** 解析投递状态：优先从 body.channels 数组判断，其次从 delivery_status 字段映射 */
function parseDelivery(body: Record<string, unknown>): { label: string; pill: DeliveryPill } {
  const status = pickBodyStr(body, ['delivery_status', 'delivery_state', 'status'])
  const channels = body.channels as Array<Record<string, unknown>> | undefined

  // 优先根据渠道投递结果判断
  if (Array.isArray(channels) && channels.length > 0) {
    const successCount = channels.filter(
      (c) => c.success === true || c.status === 'success',
    ).length
    if (successCount >= 2) return { label: '双渠道成功', pill: 'ok' }
    if (successCount === 1) {
      const feishuSuccess = channels.some(
        (c) =>
          (c.adapter_type === 'feishu' || c.channel === 'feishu') &&
          (c.success === true || c.status === 'success'),
      )
      return { label: feishuSuccess ? '飞书成功' : '仅站内', pill: feishuSuccess ? 'ok' : 'off' }
    }
    return { label: '仅站内', pill: 'off' }
  }

  // 根据 status 字符串映射
  switch (status) {
    case 'feishu_success':
      return { label: '飞书成功', pill: 'ok' }
    case 'dual_success':
    case 'both_success':
      return { label: '双渠道成功', pill: 'ok' }
    case 'in_app_only':
    case 'in_app':
      return { label: '仅站内', pill: 'off' }
    case 'completed':
    case 'done':
      return { label: '已完成', pill: 'ok' }
    case 'failed':
      return { label: '投递失败', pill: 'warn' }
    default:
      return status ? { label: status, pill: 'off' } : { label: '仅站内', pill: 'off' }
  }
}

// ===== 主页面 =====
export default function MessagesPage() {
  const navigate = useNavigate()
  const toast = useToast.getState()
  const [activeFilter, setActiveFilter] = useState<MessageFilter>('all')
  const [timeRange, setTimeRange] = useState<TimeRange>('7d')
  const [isMarkingAll, setIsMarkingAll] = useState(false)

  // 获取消息列表：未读筛选走 API（unread_only），类型筛选走客户端
  const messagesQuery = useMessages({
    unread_only: activeFilter === 'unread',
    limit: 100,
  })
  const markReadMutation = useMarkMessageRead()

  const allMessages: NotificationMessage[] = messagesQuery.data?.items ?? []

  // 各筛选分类的计数（基于当前已加载的全部消息）
  const filterCounts = useMemo<Record<MessageFilter, number>>(() => {
    const counts: Record<MessageFilter, number> = {
      all: allMessages.length,
      unread: 0,
      selection: 0,
      monitoring: 0,
      process: 0,
      system: 0,
    }
    for (const m of allMessages) {
      if (!m.read_at) counts.unread++
      if (m.message_type === 'selection_composite') counts.selection++
      else if (m.message_type === 'monitoring_composite') counts.monitoring++
      else if (m.message_type === 'process_event') counts.process++
      else if (m.message_type === 'system') counts.system++
    }
    return counts
  }, [allMessages])

  // 时间范围 + 类型过滤（unread 已通过 API 参数过滤）
  const filteredMessages = useMemo(() => {
    const now = Date.now()
    const cutoff = now - (timeRange === '7d' ? 7 : 30) * 86400000

    return allMessages.filter((m) => {
      // 时间范围过滤
      const msgTime = new Date(m.created_at).getTime()
      if (Number.isNaN(msgTime) || msgTime < cutoff) return false

      // 类型过滤
      if (activeFilter === 'all' || activeFilter === 'unread') return true
      if (activeFilter === 'selection') return m.message_type === 'selection_composite'
      if (activeFilter === 'monitoring') return m.message_type === 'monitoring_composite'
      if (activeFilter === 'process') return m.message_type === 'process_event'
      if (activeFilter === 'system') return m.message_type === 'system'
      return true
    })
  }, [allMessages, timeRange, activeFilter])

  // 转换为表格行
  const rows: MessageRow[] = useMemo(() => {
    return filteredMessages.map((m) => {
      const body = m.body || {}
      const typeMeta = TYPE_META[m.message_type] ?? {
        label: m.message_type,
        tag: 'info' as const,
      }
      const delivery = parseDelivery(body)

      // 根据消息类型决定查看跳转目标
      let navigateTarget = '/watchlist'
      if (m.message_type === 'selection_composite') {
        navigateTarget = '/screener'
      } else if (m.message_type === 'system') {
        navigateTarget = '/settings'
      } else if (m.message_type === 'monitoring_composite') {
        const symbol = pickBodyStr(body, [
          'symbol',
          'instrument_symbol',
          'stock_symbol',
        ])
        navigateTarget = symbol ? `/stock/${symbol}` : '/watchlist'
      }

      return {
        id: m.id,
        message_type: m.message_type,
        type_label: typeMeta.label,
        type_tag: typeMeta.tag,
        plan_name: pickBodyStr(body, [
          'plan_name',
          'strategy_name',
          'plan',
          'source_name',
        ]),
        title: pickBodyStr(body, ['title', 'main_title', 'subject', 'summary']),
        subtitle: pickBodyStr(body, [
          'subtitle',
          'sub_title',
          'detail',
          'description',
          'timeline',
        ]),
        time_text: formatMessageTime(m.created_at),
        created_at: m.created_at,
        delivery_label: delivery.label,
        delivery_pill: delivery.pill,
        unread: !m.read_at,
        navigate_target: navigateTarget,
      }
    })
  }, [filteredMessages])

  // 标记单条已读
  const handleMarkRead = useCallback(
    async (messageId: string) => {
      try {
        await markReadMutation.mutateAsync(messageId)
      } catch {
        toast.show('标记失败', '请稍后重试')
      }
    },
    [markReadMutation, toast],
  )

  // 全部标记已读：并发标记所有未读消息
  const handleMarkAllRead = useCallback(async () => {
    const unreadMessages = allMessages.filter((m) => !m.read_at)
    if (unreadMessages.length === 0) {
      toast.show('无需操作', '没有未读消息')
      return
    }
    setIsMarkingAll(true)
    try {
      await Promise.all(
        unreadMessages.map((m) => markReadMutation.mutateAsync(m.id)),
      )
      toast.show('全部标记已读', `已标记 ${unreadMessages.length} 条消息`)
    } catch {
      toast.show('标记失败', '部分消息标记失败，请稍后重试')
    } finally {
      setIsMarkingAll(false)
    }
  }, [allMessages, markReadMutation, toast])

  // 点击查看：标记已读 + 跳转
  const handleView = useCallback(
    (row: MessageRow) => {
      if (row.unread) {
        handleMarkRead(row.id)
      }
      navigate(row.navigate_target)
    },
    [handleMarkRead, navigate],
  )

  // 列定义
  const columns: DataTableColumn<MessageRow>[] = useMemo(
    () => [
      {
        key: 'type',
        title: '类型',
        dataType: 'text',
        sortable: false,
        filterable: false,
        render: (row) => (
          <div className="message-type-cell">
            {row.unread && (
              <button
                className="message-unread-dot"
                title="点击标记已读"
                onClick={(e) => {
                  e.stopPropagation()
                  handleMarkRead(row.id)
                }}
              />
            )}
            <span className={`tag ${row.type_tag}`}>{row.type_label}</span>
          </div>
        ),
      },
      {
        key: 'plan_name',
        title: '方案 / 策略',
        dataType: 'text',
        sortable: true,
        filterable: true,
        render: (row) => row.plan_name || '-',
      },
      {
        key: 'content',
        title: '消息内容',
        dataType: 'text',
        sortable: false,
        filterable: true,
        filterValue: (row) => `${row.title} ${row.subtitle}`,
        render: (row) => (
          <div>
            <div className="symbol">{row.title || '-'}</div>
            {row.subtitle && <div className="symbol-sub">{row.subtitle}</div>}
          </div>
        ),
      },
      {
        key: 'time_text',
        title: '时间',
        dataType: 'datetime',
        sortable: true,
        filterable: false,
        sortValue: (row) => new Date(row.created_at).getTime(),
        render: (row) => <span className="num">{row.time_text}</span>,
      },
      {
        key: 'delivery',
        title: '投递状态',
        dataType: 'text',
        sortable: false,
        filterable: false,
        render: (row) => (
          <span className={`status-pill ${row.delivery_pill}`}>
            {row.delivery_label}
          </span>
        ),
      },
      {
        key: 'action',
        title: '',
        dataType: 'text',
        sortable: false,
        filterable: false,
        isAction: true,
        render: (row) => (
          <button
            className="btn small"
            onClick={(e) => {
              e.stopPropagation()
              handleView(row)
            }}
          >
            查看
          </button>
        ),
      },
    ],
    [handleMarkRead, handleView],
  )

  // 渲染
  return (
    <>
      {/* 页头 */}
      <div className="page-head">
        <div>
          <h1 className="page-title">消息中心</h1>
          <div className="page-desc">
            组合方案消息、单策略过程事件与系统消息统一管理，渠道投递状态可追溯
          </div>
        </div>
        <div className="actions">
          <button
            className="btn"
            onClick={handleMarkAllRead}
            disabled={isMarkingAll || markReadMutation.isPending}
          >
            全部已读
          </button>
          <Link className="btn primary" to="/settings">
            通知设置与示例
          </Link>
        </div>
      </div>

      {/* 工具栏：分段按钮 + 时间范围 */}
      <div className="toolbar">
        <div className="segmented">
          {FILTER_OPTIONS.map((opt) => (
            <button
              key={opt.value}
              className={`segment${activeFilter === opt.value ? ' active' : ''}`}
              onClick={() => setActiveFilter(opt.value)}
            >
              {opt.label} {filterCounts[opt.value]}
            </button>
          ))}
        </div>
        <div className="toolbar-spacer"></div>
        <select
          className="select"
          value={timeRange}
          onChange={(e) => setTimeRange(e.target.value as TimeRange)}
        >
          {TIME_RANGE_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
      </div>

      {/* 消息表 */}
      <div className="card">
        <StrategyDataTable
          tableId="messages-list"
          columns={columns}
          rows={rows}
          rowKey={(row) => row.id}
          loading={messagesQuery.isLoading}
          error={messagesQuery.isError ? '消息加载失败' : null}
          searchable={false}
          emptyText="没有符合条件的消息"
        />
      </div>
    </>
  )
}
