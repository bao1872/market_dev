// 内测申请管理页（受保护路由，admin only）
// Task 4 - 管理员后台"内测申请"页面
//
// 用法：
// 1. 路由 /admin/beta-applications，受保护路由（经 ProtectedLayout + AdminRoute 包裹）
// 2. KPI 卡片：累计 / 今日 / 近 7 天 / 近 30 天 / 平均盯盘数
// 3. 筛选栏：status / reason_code / watch_stock_range / date_from / date_to + keyword 搜索
// 4. 列表（StrategyDataTable 服务端分页）：联系方式 / 盯盘数 / 理由 / 状态 / 飞书状态 / 提交时间 / 操作
// 5. 详情弹窗：完整字段 + 状态修改 + 重发飞书 + CSV 导出
//
// 依赖 hooks：
// - useAdminBetaApplications：列表查询（分页+筛选+搜索）
// - useAdminBetaApplicationStats：统计卡数据
// - useAdminBetaApplicationDetail：详情查询
// - useUpdateAdminBetaApplication：状态修改
// - useRetryAdminBetaApplicationFeishu：重发飞书

import { useState, useMemo, useCallback } from 'react'
import { useToast } from '@/store/toast'
import {
  useAdminBetaApplications,
  useAdminBetaApplicationStats,
  useAdminBetaApplicationDetail,
  useUpdateAdminBetaApplication,
  useRetryAdminBetaApplicationFeishu,
} from '@/hooks/useApi'
import { StrategyDataTable } from '@/components/StrategyDataTable'
import type { DataTableColumn } from '@/components/StrategyDataTable'
import {
  type BetaApplicationStatus,
  type BetaApplicationReasonCode,
  type WatchStockRange,
  buildBetaApplicationExportUrl,
} from '@/api/endpoints'
import { apiClient } from '@/api/client'

// ===== 类型定义（带索引签名以满足 StrategyDataTable 的 Row extends Record<string, unknown>）=====

interface BetaApplicationRow {
  id: string
  wechat: string | null
  phone: string | null
  watch_stock_count: number
  reason_code: BetaApplicationReasonCode
  reason_other: string | null
  status: BetaApplicationStatus
  source: string | null
  admin_note: string | null
  handled_by: string | null
  handled_at: string | null
  submitted_at: string
  updated_at: string
  feishu_delivery_status: string | null
  [key: string]: unknown
}

// ===== 常量映射 =====

const STATUS_LABELS: Record<BetaApplicationStatus, string> = {
  new: '新申请',
  contacted: '已联系',
  approved: '已通过',
  rejected: '已拒绝',
  converted: '已转化',
}

const STATUS_PILLS: Record<BetaApplicationStatus, string> = {
  new: 'warn',
  contacted: 'info',
  approved: 'ok',
  rejected: 'off',
  converted: 'ok',
}

const REASON_LABELS: Record<BetaApplicationReasonCode, string> = {
  busy: '工作繁忙',
  too_many: '股票太多',
  forget: '容易遗忘',
  quant: '量化研究',
  other: '其他',
}

const FEISHU_STATUS_LABELS: Record<string, string> = {
  pending: '待投递',
  success: '已投递',
  failed: '投递失败',
}

const FEISHU_STATUS_PILLS: Record<string, string> = {
  pending: 'warn',
  success: 'ok',
  failed: 'off',
}

// ===== 工具函数 =====

function formatDateTime(iso: string | null): string {
  if (!iso) return '—'
  try {
    const d = new Date(iso)
    const yyyy = d.getFullYear()
    const mm = String(d.getMonth() + 1).padStart(2, '0')
    const dd = String(d.getDate()).padStart(2, '0')
    const hh = String(d.getHours()).padStart(2, '0')
    const mi = String(d.getMinutes()).padStart(2, '0')
    return `${yyyy}-${mm}-${dd} ${hh}:${mi}`
  } catch {
    return iso
  }
}

// ===== 主组件 =====

export default function AdminBetaApplicationsPage() {
  const toast = useToast()

  // 筛选状态
  const [filterStatus, setFilterStatus] = useState<BetaApplicationStatus | ''>('')
  const [filterReason, setFilterReason] = useState<BetaApplicationReasonCode | ''>('')
  const [filterRange, setFilterRange] = useState<WatchStockRange | ''>('')
  const [filterDateFrom, setFilterDateFrom] = useState('')
  const [filterDateTo, setFilterDateTo] = useState('')
  const [filterKeyword, setFilterKeyword] = useState('')
  const [appliedKeyword, setAppliedKeyword] = useState('')

  // 分页状态
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)

  // 详情弹窗
  const [detailId, setDetailId] = useState<string | null>(null)
  const [editStatus, setEditStatus] = useState<BetaApplicationStatus>('new')
  const [editNote, setEditNote] = useState('')

  // 构造查询参数
  const queryParams = useMemo(() => {
    const params: Record<string, string | number | undefined> = {
      limit: pageSize,
      offset: (page - 1) * pageSize,
    }
    if (filterStatus) params.status = filterStatus
    if (filterReason) params.reason_code = filterReason
    if (filterRange) params.watch_stock_range = filterRange
    if (filterDateFrom) params.date_from = filterDateFrom
    if (filterDateTo) params.date_to = filterDateTo
    if (appliedKeyword) params.keyword = appliedKeyword
    return params
  }, [page, pageSize, filterStatus, filterReason, filterRange, filterDateFrom, filterDateTo, appliedKeyword])

  // 查询 hooks
  const listQuery = useAdminBetaApplications(queryParams)
  const statsQuery = useAdminBetaApplicationStats()
  const detailQuery = useAdminBetaApplicationDetail(detailId ?? undefined)
  const updateMutation = useUpdateAdminBetaApplication()
  const retryMutation = useRetryAdminBetaApplicationFeishu()

  const stats = statsQuery.data
  const rows = (listQuery.data?.items ?? []) as BetaApplicationRow[]

  // ===== 事件处理 =====

  const handleSearch = useCallback(() => {
    setAppliedKeyword(filterKeyword)
    setPage(1)
  }, [filterKeyword])

  const handleResetFilters = useCallback(() => {
    setFilterStatus('')
    setFilterReason('')
    setFilterRange('')
    setFilterDateFrom('')
    setFilterDateTo('')
    setFilterKeyword('')
    setAppliedKeyword('')
    setPage(1)
  }, [])

  const handleExportCsv = useCallback(() => {
    const url = buildBetaApplicationExportUrl({
      status: filterStatus || undefined,
      reason_code: filterReason || undefined,
      watch_stock_range: filterRange || undefined,
      date_from: filterDateFrom || undefined,
      date_to: filterDateTo || undefined,
      keyword: appliedKeyword || undefined,
    })
    // 通过 axios 请求 CSV（带认证 header），触发浏览器下载
    apiClient
      .get(url, { responseType: 'blob' })
      .then((res) => {
        const blob = new Blob([res.data], { type: 'text/csv;charset=utf-8;' })
        const link = document.createElement('a')
        const objectUrl = URL.createObjectURL(blob)
        link.href = objectUrl
        // 从 Content-Disposition 提取 filename，回退到默认名
        const cd = res.headers['content-disposition'] || ''
        const match = /filename="?([^";]+)"?/.exec(cd)
        link.download = match ? match[1] : `beta_applications_${new Date().toISOString().slice(0, 10)}.csv`
        document.body.appendChild(link)
        link.click()
        document.body.removeChild(link)
        URL.revokeObjectURL(objectUrl)
        toast.show('导出成功', 'CSV 文件已开始下载')
      })
      .catch(() => {
        toast.show('导出失败', '请稍后重试')
      })
  }, [filterStatus, filterReason, filterRange, filterDateFrom, filterDateTo, appliedKeyword, toast])

  const handleOpenDetail = useCallback((row: BetaApplicationRow) => {
    setDetailId(row.id)
    setEditStatus(row.status)
    setEditNote(row.admin_note ?? '')
  }, [])

  const handleCloseDetail = useCallback(() => {
    setDetailId(null)
    setEditStatus('new')
    setEditNote('')
  }, [])

  const handleSaveStatus = useCallback(() => {
    if (!detailId) return
    updateMutation.mutate(
      { appId: detailId, payload: { status: editStatus, admin_note: editNote } },
      {
        onSuccess: () => toast.show('保存成功', '申请状态已更新'),
        onError: (err: unknown) => {
          const msg = err instanceof Error ? err.message : '未知错误'
          toast.show('保存失败', msg)
        },
      },
    )
  }, [detailId, editStatus, editNote, updateMutation, toast])

  const handleRetryFeishu = useCallback(() => {
    if (!detailId) return
    retryMutation.mutate(detailId, {
      onSuccess: () => toast.show('已重发', '飞书通知已重新入队'),
      onError: (err: unknown) => {
        const msg = err instanceof Error ? err.message : '未知错误'
        toast.show('重发失败', msg)
      },
    })
  }, [detailId, retryMutation, toast])

  // ===== 表格列定义 =====
  const columns: DataTableColumn<BetaApplicationRow>[] = useMemo(
    () => [
      {
        key: 'contact',
        title: '联系方式',
        dataType: 'text',
        sortable: false,
        filterable: false,
        render: (row) => (
          <div>
            {row.wechat && <div className="symbol">微信: {row.wechat}</div>}
            {row.phone && <div className="symbol-sub">手机: {row.phone}</div>}
            {!row.wechat && !row.phone && <span>—</span>}
          </div>
        ),
      },
      {
        key: 'watch_stock_count',
        title: '盯盘数',
        dataType: 'number',
        sortable: true,
        filterable: false,
        render: (row) => `${row.watch_stock_count} 只`,
        sortValue: (row) => row.watch_stock_count,
      },
      {
        key: 'reason_code',
        title: '理由',
        dataType: 'enum',
        sortable: true,
        filterable: true,
        enumOptions: [
          { label: '工作繁忙', value: '工作繁忙' },
          { label: '股票太多', value: '股票太多' },
          { label: '容易遗忘', value: '容易遗忘' },
          { label: '量化研究', value: '量化研究' },
          { label: '其他', value: '其他' },
        ],
        render: (row) => REASON_LABELS[row.reason_code] ?? row.reason_code,
        filterValue: (row) => REASON_LABELS[row.reason_code] ?? row.reason_code,
        sortValue: (row) => REASON_LABELS[row.reason_code] ?? row.reason_code,
      },
      {
        key: 'status',
        title: '状态',
        dataType: 'enum',
        sortable: true,
        filterable: true,
        enumOptions: [
          { label: '新申请', value: '新申请' },
          { label: '已联系', value: '已联系' },
          { label: '已通过', value: '已通过' },
          { label: '已拒绝', value: '已拒绝' },
          { label: '已转化', value: '已转化' },
        ],
        render: (row) => (
          <span className={`status-pill ${STATUS_PILLS[row.status] ?? 'off'}`}>
            {STATUS_LABELS[row.status] ?? row.status}
          </span>
        ),
        filterValue: (row) => STATUS_LABELS[row.status] ?? row.status,
        sortValue: (row) => STATUS_LABELS[row.status] ?? row.status,
      },
      {
        key: 'feishu_delivery_status',
        title: '飞书',
        dataType: 'enum',
        sortable: false,
        filterable: false,
        render: (row) => {
          const s = row.feishu_delivery_status
          if (!s) return '—'
          return (
            <span className={`status-pill ${FEISHU_STATUS_PILLS[s] ?? 'off'}`}>
              {FEISHU_STATUS_LABELS[s] ?? s}
            </span>
          )
        },
      },
      {
        key: 'submitted_at',
        title: '提交时间',
        dataType: 'datetime',
        sortable: true,
        filterable: false,
        render: (row) => formatDateTime(row.submitted_at),
        sortValue: (row) => row.submitted_at,
      },
      {
        key: 'actions',
        title: '',
        dataType: 'text',
        sortable: false,
        filterable: false,
        isAction: true,
        render: (row) => (
          <button className="btn small" onClick={() => handleOpenDetail(row)}>
            详情
          </button>
        ),
      },
    ],
    [handleOpenDetail],
  )

  // ===== 渲染 =====
  return (
    <>
      {/* 页头 */}
      <div className="page-head">
        <div>
          <h1 className="page-title">内测申请</h1>
          <div className="page-desc">
            管理用户提交的内测申请，可联系、审核、转化，并支持重发飞书通知与 CSV 导出
          </div>
        </div>
        <div className="actions">
          <button className="btn" onClick={handleExportCsv}>
            导出 CSV
          </button>
        </div>
      </div>

      {/* KPI 卡片 */}
      <div className="grid kpi membership-kpis">
        <div className="card kpi-card">
          <div className="kpi-label">累计申请</div>
          <div className="kpi-value">{stats?.total ?? '—'}</div>
          <div className="kpi-foot">全部历史</div>
        </div>
        <div className="card kpi-card">
          <div className="kpi-label">今日新增</div>
          <div className="kpi-value">{stats?.today ?? '—'}</div>
          <div className="kpi-foot">按提交时间统计</div>
        </div>
        <div className="card kpi-card">
          <div className="kpi-label">近 7 天</div>
          <div className="kpi-value">{stats?.last_7_days ?? '—'}</div>
          <div className="kpi-foot">滚动 7 日窗口</div>
        </div>
        <div className="card kpi-card">
          <div className="kpi-label">近 30 天</div>
          <div className="kpi-value">{stats?.last_30_days ?? '—'}</div>
          <div className="kpi-foot">滚动 30 日窗口</div>
        </div>
        <div className="card kpi-card">
          <div className="kpi-label">平均盯盘数</div>
          <div className="kpi-value">{stats?.avg_watch_stock_count ?? '—'}</div>
          <div className="kpi-foot">所有申请均值</div>
        </div>
      </div>

      {/* 状态分布概览 */}
      {stats && (
        <div className="card" style={{ marginBottom: 16, padding: 16 }}>
          <div style={{ fontWeight: 600, marginBottom: 8 }}>状态分布</div>
          <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
            {(['new', 'contacted', 'approved', 'rejected', 'converted'] as BetaApplicationStatus[]).map((s) => (
              <div key={s} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <span className={`status-pill ${STATUS_PILLS[s]}`}>{STATUS_LABELS[s]}</span>
                <span style={{ fontWeight: 600 }}>{stats.by_status[s] ?? 0}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* 筛选栏 */}
      <div className="card" style={{ marginBottom: 16, padding: 16 }}>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: 12 }}>
          <div>
            <label style={{ display: 'block', fontSize: 12, color: '#888', marginBottom: 4 }}>状态</label>
            <select
              className="input"
              value={filterStatus}
              onChange={(e) => {
                setFilterStatus(e.target.value as BetaApplicationStatus | '')
                setPage(1)
              }}
            >
              <option value="">全部</option>
              {(['new', 'contacted', 'approved', 'rejected', 'converted'] as BetaApplicationStatus[]).map((s) => (
                <option key={s} value={s}>
                  {STATUS_LABELS[s]}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label style={{ display: 'block', fontSize: 12, color: '#888', marginBottom: 4 }}>理由</label>
            <select
              className="input"
              value={filterReason}
              onChange={(e) => {
                setFilterReason(e.target.value as BetaApplicationReasonCode | '')
                setPage(1)
              }}
            >
              <option value="">全部</option>
              {(['busy', 'too_many', 'forget', 'quant', 'other'] as BetaApplicationReasonCode[]).map((r) => (
                <option key={r} value={r}>
                  {REASON_LABELS[r]}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label style={{ display: 'block', fontSize: 12, color: '#888', marginBottom: 4 }}>盯盘区间</label>
            <select
              className="input"
              value={filterRange}
              onChange={(e) => {
                setFilterRange(e.target.value as WatchStockRange | '')
                setPage(1)
              }}
            >
              <option value="">全部</option>
              <option value="1-10">1-10</option>
              <option value="11-20">11-20</option>
              <option value="21-50">21-50</option>
              <option value="50+">50+</option>
            </select>
          </div>
          <div>
            <label style={{ display: 'block', fontSize: 12, color: '#888', marginBottom: 4 }}>起始日期</label>
            <input
              type="date"
              className="input"
              value={filterDateFrom}
              onChange={(e) => {
                setFilterDateFrom(e.target.value)
                setPage(1)
              }}
            />
          </div>
          <div>
            <label style={{ display: 'block', fontSize: 12, color: '#888', marginBottom: 4 }}>截止日期</label>
            <input
              type="date"
              className="input"
              value={filterDateTo}
              onChange={(e) => {
                setFilterDateTo(e.target.value)
                setPage(1)
              }}
            />
          </div>
          <div>
            <label style={{ display: 'block', fontSize: 12, color: '#888', marginBottom: 4 }}>搜索（微信/手机）</label>
            <div style={{ display: 'flex', gap: 4 }}>
              <input
                type="text"
                className="input"
                placeholder="输入微信号或手机号"
                value={filterKeyword}
                onChange={(e) => setFilterKeyword(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') handleSearch()
                }}
              />
              <button className="btn small" onClick={handleSearch}>
                搜索
              </button>
            </div>
          </div>
        </div>
        {(filterStatus || filterReason || filterRange || filterDateFrom || filterDateTo || appliedKeyword) && (
          <div style={{ marginTop: 12 }}>
            <button className="btn small" onClick={handleResetFilters}>
              重置筛选
            </button>
          </div>
        )}
      </div>

      {/* 列表 */}
      <div className="card">
        <StrategyDataTable
          tableId="admin-beta-applications"
          columns={columns}
          rows={rows}
          total={listQuery.data?.total ?? 0}
          serverSide={false}
          loading={listQuery.isLoading}
          error={listQuery.error ? '加载失败' : null}
          stale={listQuery.isFetching && !listQuery.isLoading}
          rowKey={(row) => row.id}
          searchable={false}
          emptyText="暂无内测申请"
        />
      </div>

      {/* 分页 */}
      {(listQuery.data?.total ?? 0) > pageSize && (
        <div style={{ display: 'flex', justifyContent: 'center', gap: 12, marginTop: 16, alignItems: 'center' }}>
          <button
            className="btn small"
            disabled={page <= 1}
            onClick={() => setPage((p) => Math.max(1, p - 1))}
          >
            上一页
          </button>
          <span>
            第 {page} 页 / 共 {Math.ceil((listQuery.data?.total ?? 0) / pageSize)} 页（共 {listQuery.data?.total ?? 0} 条）
          </span>
          <button
            className="btn small"
            disabled={page * pageSize >= (listQuery.data?.total ?? 0)}
            onClick={() => setPage((p) => p + 1)}
          >
            下一页
          </button>
          <select
            className="input"
            style={{ width: 80 }}
            value={pageSize}
            onChange={(e) => {
              setPageSize(Number(e.target.value))
              setPage(1)
            }}
          >
            <option value={20}>20/页</option>
            <option value={50}>50/页</option>
            <option value={100}>100/页</option>
          </select>
        </div>
      )}

      {/* 详情弹窗 */}
      {detailId && detailQuery.data && (
        <div
          className="modal-overlay open"
          onClick={(e) => {
            if (e.target === e.currentTarget) handleCloseDetail()
          }}
        >
          <div className="modal" style={{ maxWidth: 640, width: '90%' }}>
            <div className="modal-head">
              <h3>内测申请详情</h3>
              <button className="icon-btn" onClick={handleCloseDetail} aria-label="关闭">
                ×
              </button>
            </div>
            <div className="modal-body" style={{ maxHeight: '70vh', overflowY: 'auto' }}>
              {/* 基本信息 */}
              <div style={{ marginBottom: 16 }}>
                <div style={{ fontWeight: 600, marginBottom: 8 }}>基本信息</div>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8, fontSize: 14 }}>
                  <div>
                    <span style={{ color: '#888' }}>申请编号：</span>
                    <span style={{ fontFamily: 'monospace' }}>{detailQuery.data.id.slice(0, 8)}…</span>
                  </div>
                  <div>
                    <span style={{ color: '#888' }}>提交时间：</span>
                    <span>{formatDateTime(detailQuery.data.submitted_at)}</span>
                  </div>
                  <div>
                    <span style={{ color: '#888' }}>微信号：</span>
                    <span>{detailQuery.data.wechat ?? '—'}</span>
                  </div>
                  <div>
                    <span style={{ color: '#888' }}>手机号：</span>
                    <span>{detailQuery.data.phone ?? '—'}</span>
                  </div>
                  <div>
                    <span style={{ color: '#888' }}>盯盘数：</span>
                    <span>{detailQuery.data.watch_stock_count} 只</span>
                  </div>
                  <div>
                    <span style={{ color: '#888' }}>理由：</span>
                    <span>{REASON_LABELS[detailQuery.data.reason_code] ?? detailQuery.data.reason_code}</span>
                  </div>
                  <div>
                    <span style={{ color: '#888' }}>来源：</span>
                    <span>{detailQuery.data.source ?? '—'}</span>
                  </div>
                  <div>
                    <span style={{ color: '#888' }}>处理人：</span>
                    <span style={{ fontFamily: 'monospace' }}>
                      {detailQuery.data.handled_by ? detailQuery.data.handled_by.slice(0, 8) + '…' : '—'}
                    </span>
                  </div>
                  <div>
                    <span style={{ color: '#888' }}>处理时间：</span>
                    <span>{formatDateTime(detailQuery.data.handled_at)}</span>
                  </div>
                </div>
                {detailQuery.data.reason_other && (
                  <div style={{ marginTop: 8, fontSize: 14 }}>
                    <span style={{ color: '#888' }}>补充说明：</span>
                    <span>{detailQuery.data.reason_other}</span>
                  </div>
                )}
              </div>

              {/* 飞书投递信息 */}
              <div style={{ marginBottom: 16 }}>
                <div style={{ fontWeight: 600, marginBottom: 8 }}>飞书投递</div>
                <div style={{ display: 'flex', gap: 16, fontSize: 14, alignItems: 'center' }}>
                  <div>
                    <span style={{ color: '#888' }}>状态：</span>
                    {detailQuery.data.feishu_delivery_status ? (
                      <span className={`status-pill ${FEISHU_STATUS_PILLS[detailQuery.data.feishu_delivery_status] ?? 'off'}`}>
                        {FEISHU_STATUS_LABELS[detailQuery.data.feishu_delivery_status] ?? detailQuery.data.feishu_delivery_status}
                      </span>
                    ) : (
                      '—'
                    )}
                  </div>
                  <div>
                    <span style={{ color: '#888' }}>投递时间：</span>
                    <span>{formatDateTime(detailQuery.data.feishu_delivered_at)}</span>
                  </div>
                </div>
                {detailQuery.data.feishu_last_error && (
                  <div style={{ marginTop: 8, fontSize: 13, color: '#d33' }}>
                    错误：{detailQuery.data.feishu_last_error}
                  </div>
                )}
                <div style={{ marginTop: 8 }}>
                  <button
                    className="btn small"
                    onClick={handleRetryFeishu}
                    disabled={retryMutation.isPending}
                  >
                    {retryMutation.isPending ? '重发中…' : '重发飞书'}
                  </button>
                </div>
              </div>

              {/* 状态修改 */}
              <div style={{ marginBottom: 16 }}>
                <div style={{ fontWeight: 600, marginBottom: 8 }}>状态管理</div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                  <div>
                    <label style={{ display: 'block', fontSize: 12, color: '#888', marginBottom: 4 }}>状态</label>
                    <select
                      className="input"
                      value={editStatus}
                      onChange={(e) => setEditStatus(e.target.value as BetaApplicationStatus)}
                      style={{ width: '100%' }}
                    >
                      {(['new', 'contacted', 'approved', 'rejected', 'converted'] as BetaApplicationStatus[]).map((s) => (
                        <option key={s} value={s}>
                          {STATUS_LABELS[s]}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label style={{ display: 'block', fontSize: 12, color: '#888', marginBottom: 4 }}>管理员备注</label>
                    <textarea
                      className="input"
                      rows={3}
                      value={editNote}
                      onChange={(e) => setEditNote(e.target.value)}
                      placeholder="可填写联系记录、审核意见等"
                      style={{ width: '100%', resize: 'vertical' }}
                    />
                  </div>
                  <div>
                    <button
                      className="btn primary"
                      onClick={handleSaveStatus}
                      disabled={updateMutation.isPending}
                    >
                      {updateMutation.isPending ? '保存中…' : '保存状态'}
                    </button>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      )}
    </>
  )
}
