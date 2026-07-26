// V2.1 邀请码能力配置管理页（PRD §6 + §8.1）
//
// 路由：/admin/invite-codes
// 权限：admin（受 AdminRoute 保护）
//
// 功能：
// 1. 邀请码创建表单（InviteCapabilityForm）
// 2. 邀请码列表（capabilities + duration_months + status + 创建/兑换信息）
// 3. 撤销按钮（仅 available 状态可撤销）
// 4. 状态筛选 + 分页
//
// 与 V1 AdminUsersPage 中的邀请码管理并存，V2.1 使用 capabilities + duration_months。

import { useState, useMemo, useCallback } from 'react'
import {
  useInviteCodesV2,
  useCreateInviteCodesV2,
  useRevokeInviteCodeV2,
} from '@/hooks/useApi'
import {
  InviteCapabilityForm,
} from '@/features/invite-capability/InviteCapabilityForm'
import {
  formatCapabilitySummary,
  formatInviteCodeStatus,
} from '@/features/invite-capability/inviteCapabilityValidation'
import type {
  InviteCodeV2CreateRequest,
  InviteCodeV2ListItem,
  InviteCodeV2Response,
} from '@/api/endpoints'

type StatusFilter = 'available' | 'redeemed' | 'revoked' | 'all'

const STATUS_OPTIONS: { value: StatusFilter; label: string }[] = [
  { value: 'all', label: '全部' },
  { value: 'available', label: '未使用' },
  { value: 'redeemed', label: '已兑换' },
  { value: 'revoked', label: '已撤销' },
]

const PAGE_SIZE = 20

export default function AdminInviteCapabilityPage() {
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all')
  const [offset, setOffset] = useState(0)
  const [showForm, setShowForm] = useState(false)
  const [toastMsg, setToastMsg] = useState<string | null>(null)

  const queryParams = useMemo(() => {
    const params: { limit: number; offset: number; status?: 'available' | 'redeemed' | 'revoked' } = {
      limit: PAGE_SIZE,
      offset,
    }
    if (statusFilter !== 'all') params.status = statusFilter
    return params
  }, [statusFilter, offset])

  const listQuery = useInviteCodesV2(queryParams)
  const createMutation = useCreateInviteCodesV2()
  const revokeMutation = useRevokeInviteCodeV2()

  const handleCreate = useCallback(
    async (request: InviteCodeV2CreateRequest): Promise<InviteCodeV2Response[]> => {
      const codes = await createMutation.mutateAsync(request)
      setToastMsg(`成功生成 ${codes.length} 个邀请码`)
      return codes
    },
    [createMutation],
  )

  const handleRevoke = useCallback(
    async (item: InviteCodeV2ListItem) => {
      if (!window.confirm(`确认撤销邀请码？撤销后不可恢复。\n\n权限：${formatCapabilitySummary(item.capabilities, item.duration_months)}`)) {
        return
      }
      try {
        await revokeMutation.mutateAsync(item.id)
        setToastMsg('邀请码已撤销')
      } catch (e) {
        const msg = e instanceof Error ? e.message : '撤销失败'
        setToastMsg(`撤销失败：${msg}`)
      }
    },
    [revokeMutation],
  )

  const items = listQuery.data?.items ?? []
  const total = listQuery.data?.total ?? 0
  const hasPrev = offset > 0
  const hasNext = offset + PAGE_SIZE < total

  return (
    <>
      <div className="page-head">
        <div>
          <h1 className="page-title">邀请码管理（V2.1）</h1>
          <div className="page-desc">
            基于能力配置的邀请码：勾选能力 + 自选额度 + 授权月数，兑换后按能力创建独立 grant
          </div>
        </div>
        <div className="actions">
          <button
            className="btn primary"
            onClick={() => setShowForm((v) => !v)}
            disabled={createMutation.isPending}
          >
            {showForm ? '收起表单' : '＋ 生成邀请码'}
          </button>
        </div>
      </div>

      {toastMsg && (
        <div className="notice" onClick={() => setToastMsg(null)}>
          {toastMsg}
        </div>
      )}

      {/* 创建表单 */}
      {showForm && (
        <div className="card">
          <InviteCapabilityForm
            onSubmit={handleCreate}
            onCancel={() => setShowForm(false)}
          />
        </div>
      )}

      {/* 列表筛选 */}
      <div className="filter-bar">
        <div className="filter-tabs">
          {STATUS_OPTIONS.map((opt) => (
            <button
              key={opt.value}
              className={`filter-tab ${statusFilter === opt.value ? 'active' : ''}`}
              onClick={() => {
                setStatusFilter(opt.value)
                setOffset(0)
              }}
            >
              {opt.label}
            </button>
          ))}
        </div>
      </div>

      {/* 列表 */}
      <div className="card">
        {listQuery.isLoading && <div className="loading-placeholder">加载中...</div>}
        {listQuery.error && (
          <div className="notice notice-error">
            加载失败：{listQuery.error.message}
          </div>
        )}
        {!listQuery.isLoading && items.length === 0 && (
          <div className="empty-state">暂无邀请码</div>
        )}
        {items.length > 0 && (
          <table className="data-table">
            <thead>
              <tr>
                <th>权限摘要</th>
                <th>授权月数</th>
                <th>状态</th>
                <th>创建时间</th>
                <th>兑换信息</th>
                <th>备注</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.id}>
                  <td>
                    <div className="capability-summary">
                      {formatCapabilitySummary(item.capabilities, item.duration_months)}
                    </div>
                  </td>
                  <td>{item.duration_months}个月</td>
                  <td>
                    <span className={`pill pill-${item.status}`}>
                      {formatInviteCodeStatus(item.status)}
                    </span>
                  </td>
                  <td>{formatDateTime(item.created_at)}</td>
                  <td>
                    {item.redeemed_at ? (
                      <span>
                        {formatDateTime(item.redeemed_at)}
                        {item.redeemed_by_user_id && (
                          <div className="muted">
                            用户：{item.redeemed_by_user_id.slice(0, 8)}...
                          </div>
                        )}
                      </span>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                  <td>{item.note || <span className="muted">—</span>}</td>
                  <td>
                    {item.status === 'available' && (
                      <button
                        className="btn small"
                        onClick={() => handleRevoke(item)}
                        disabled={revokeMutation.isPending}
                      >
                        撤销
                      </button>
                    )}
                    {(item.status === 'redeemed' || item.status === 'revoked') && (
                      <span className="muted">—</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        {/* 分页 */}
        {total > PAGE_SIZE && (
          <div className="pagination">
            <button
              className="btn small"
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
              disabled={!hasPrev}
            >
              上一页
            </button>
            <span className="pagination-info">
              {offset + 1} - {Math.min(offset + PAGE_SIZE, total)} / {total}
            </span>
            <button
              className="btn small"
              onClick={() => setOffset(offset + PAGE_SIZE)}
              disabled={!hasNext}
            >
              下一页
            </button>
          </div>
        )}
      </div>
    </>
  )
}

/** ISO 时间字符串 → YYYY-MM-DD HH:mm 短格式 */
function formatDateTime(iso: string): string {
  try {
    const d = new Date(iso)
    if (Number.isNaN(d.getTime())) return iso
    const y = d.getFullYear()
    const m = String(d.getMonth() + 1).padStart(2, '0')
    const day = String(d.getDate()).padStart(2, '0')
    const hh = String(d.getHours()).padStart(2, '0')
    const mm = String(d.getMinutes()).padStart(2, '0')
    return `${y}-${m}-${day} ${hh}:${mm}`
  } catch {
    return iso
  }
}
