// 会员与邀请码管理页（受保护路由，admin only）
// 对应原型：admin/users.html (V1.6.3)
//
// 用法：
// 1. 路由 /admin/users，受保护路由（经 ProtectedLayout + AdminRoute 包裹）
// 2. KPI 卡片：有效会员 / 7天内到期 / 未使用邀请码 / 本月兑换（注册·续期）
// 3. 三 tab：会员账户（StrategyDataTable）/ 邀请码管理（StrategyDataTable）/ 规则说明
// 4. 用户详情抽屉 userDrawer：账户 / 会员记录 / 审计 三 tab
// 5. 生成邀请码弹窗 generateInviteModal：数量选择 + 权益 + 备注 + 生成后显示新码
//
// 依赖 hooks：
// - useMembers：获取会员账户列表
// - useMemberRedemptions：获取用户兑换记录（抽屉会员记录 tab）
// - useInviteCodes：获取邀请码列表
// - useCreateInviteCodes：生成邀请码
// - useRevokeInviteCode：作废邀请码

import { useState, useMemo, useCallback } from 'react'
import clsx from 'clsx'
import { useToast } from '@/store/toast'
import {
  useMembers,
  useMemberRedemptions,
  useInviteCodes,
  useCreateInviteCodes,
  useRevokeInviteCode,
} from '@/hooks/useApi'
import { StrategyDataTable } from '@/components/StrategyDataTable'
import type { DataTableColumn } from '@/components/StrategyDataTable'
import {
  type InviteCode,
  type PlanCode,
  PLAN_CONTRACTS_PREVIEW,
} from '@/api/endpoints'

// ===== 类型定义（带索引签名以满足 StrategyDataTable 的 Row extends Record<string, unknown>）=====

/** 会员行类型（从 MemberListItem 派生） */
interface MemberRow {
  user_id: string
  email: string
  account_status: string
  membership_status: string | null
  started_at: string | null
  expires_at: string | null
  remaining_days: number | null
  renewal_count: number
  created_at: string
  [key: string]: unknown
}

/** 邀请码行类型（从 InviteCodeListItem 派生，含套餐快照字段） */
interface InviteCodeRow {
  id: string
  status: string
  grant_days: number
  plan_code: PlanCode | null
  monitor_limit: number | null
  grant_months: number | null
  note: string | null
  created_by: string
  created_at: string
  used_by: string | null
  used_at: string | null
  usage_type: string | null
  [key: string]: unknown
}

// ===== 工具函数 =====

/** 会员状态 pill 映射：根据账户状态 + 会员状态 + 剩余天数判断 */
function getMemberStatusPill(member: MemberRow): { label: string; pill: string } {
  // 账户停用优先显示
  if (member.account_status === 'disabled') {
    return { label: '停用', pill: 'off' }
  }
  // 会员状态为空（未开通会员）
  if (!member.membership_status) {
    return { label: '未开通', pill: 'off' }
  }
  const days = member.remaining_days
  if (days === null) {
    return { label: '未知', pill: 'off' }
  }
  if (days < 0) {
    return { label: '已到期', pill: 'off' }
  }
  if (days <= 7) {
    return { label: '即将到期', pill: 'warn' }
  }
  return { label: '有效', pill: 'ok' }
}

/** 邀请码状态 pill 映射 */
function getInviteStatusPill(status: string): { label: string; pill: string } {
  switch (status) {
    case 'unused':
      return { label: '未使用', pill: 'ok' }
    case 'used':
      return { label: '已使用', pill: 'off' }
    case 'revoked':
      return { label: '已作废', pill: 'off' }
    default:
      return { label: status, pill: 'off' }
  }
}

/** 兑换用途 tag 映射：register -> 注册(info) / renew -> 续期(good) */
function getUsageTypeTag(usageType: string | null): { label: string; tag: string } | null {
  if (!usageType) return null
  switch (usageType) {
    case 'register':
      return { label: '注册', tag: 'info' }
    case 'renew':
      return { label: '续期', tag: 'good' }
    default:
      return { label: usageType, tag: 'info' }
  }
}

/** 格式化日期为 YYYY-MM-DD，无效时返回 '—' */
function formatDate(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

/** 格式化日期时间为 YYYY-MM-DD HH:MM，无效时返回 '—' */
function formatDateTime(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  const h = String(d.getHours()).padStart(2, '0')
  const min = String(d.getMinutes()).padStart(2, '0')
  return `${y}-${m}-${day} ${h}:${min}`
}

/** 从邮箱提取用户名部分（@ 前的部分） */
function getEmailUsername(email: string): string {
  return email.split('@')[0] || email
}

/** 套餐展示名（observe_20 → 观察版，未知/空 → '—'） */
function getPlanName(planCode: PlanCode | null | undefined): string {
  if (!planCode) return '—'
  return PLAN_CONTRACTS_PREVIEW[planCode]?.name ?? '—'
}

/** 套餐最大自选数量展示（observe_20 → 20，未知/空 → '—'） */
function getPlanMonitorLimit(planCode: PlanCode | null | undefined): string {
  if (!planCode) return '—'
  const limit = PLAN_CONTRACTS_PREVIEW[planCode]?.monitorLimit
  return limit != null ? String(limit) : '—'
}

// ===== 主页面 =====

export default function AdminUsersPage() {
  const toast = useToast()

  // 数据查询 hooks
  const membersQuery = useMembers()
  const inviteCodesQuery = useInviteCodes()
  const createInviteCodes = useCreateInviteCodes()
  const revokeInviteCode = useRevokeInviteCode()

  // 页面状态
  const [activeTab, setActiveTab] = useState<string>('memberList')
  // 用户详情抽屉
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [selectedMember, setSelectedMember] = useState<MemberRow | null>(null)
  const [drawerTab, setDrawerTab] = useState<string>('profile')
  // 抽屉表单编辑状态
  const [accountStatusEdit, setAccountStatusEdit] = useState('有效')
  const [membershipStatusEdit, setMembershipStatusEdit] = useState('有效')
  const [expiresAtEdit, setExpiresAtEdit] = useState('')
  const [roleEdit, setRoleEdit] = useState('普通会员')
  // 生成邀请码弹窗
  const [modalOpen, setModalOpen] = useState(false)
  const [generateCount, setGenerateCount] = useState(1)
  const [generateNote, setGenerateNote] = useState('朋友内测')
  const [generatePlanCode, setGeneratePlanCode] = useState<PlanCode>('observe_20')
  const [generateGrantMonths, setGenerateGrantMonths] = useState(1)
  const [generatedCodes, setGeneratedCodes] = useState<InviteCode[]>([])

  // 用户兑换记录（抽屉打开时按选中用户查询）
  const redemptionsQuery = useMemberRedemptions(selectedMember?.user_id)

  // ===== 派生数据 =====
  const members = (membersQuery.data?.items ?? []) as MemberRow[]
  const inviteCodes = (inviteCodesQuery.data?.items ?? []) as InviteCodeRow[]

  // KPI 计算
  const kpis = useMemo(() => {
    // 有效会员：账户有效 + 会员有效 + 剩余天数 > 0
    const activeMembers = members.filter(
      (m) =>
        m.account_status === 'active' &&
        m.membership_status === 'active' &&
        (m.remaining_days ?? -1) > 0,
    ).length

    // 7天内到期：剩余天数 0-7（含 0）
    const expiringSoon = members.filter((m) => {
      const days = m.remaining_days ?? -1
      return days >= 0 && days <= 7
    }).length

    // 未使用邀请码
    const unusedCodes = inviteCodes.filter((c) => c.status === 'unused').length

    // 本月兑换：used_at 在当月，按 usage_type 区分注册/续期
    const now = new Date()
    const thisMonthCodes = inviteCodes.filter((c) => {
      if (!c.used_at) return false
      const d = new Date(c.used_at)
      return (
        d.getFullYear() === now.getFullYear() &&
        d.getMonth() === now.getMonth()
      )
    })
    const registerCount = thisMonthCodes.filter(
      (c) => c.usage_type === 'register',
    ).length
    const renewCount = thisMonthCodes.filter(
      (c) => c.usage_type === 'renew',
    ).length

    return {
      activeMembers,
      expiringSoon,
      unusedCodes,
      monthlyRedeem: thisMonthCodes.length,
      registerCount,
      renewCount,
    }
  }, [members, inviteCodes])

  // ===== 事件处理 =====

  /** 打开用户详情抽屉，初始化表单状态 */
  const handleOpenDrawer = useCallback((member: MemberRow) => {
    setSelectedMember(member)
    setDrawerTab('profile')
    setAccountStatusEdit(member.account_status === 'disabled' ? '停用' : '有效')
    const statusPill = getMemberStatusPill(member)
    setMembershipStatusEdit(statusPill.label)
    setExpiresAtEdit(member.expires_at ? formatDate(member.expires_at) : '')
    setRoleEdit('普通会员')
    setDrawerOpen(true)
  }, [])

  /** 关闭抽屉 */
  const handleCloseDrawer = useCallback(() => {
    setDrawerOpen(false)
    setSelectedMember(null)
  }, [])

  /** 复制邀请码到剪贴板 */
  const handleCopyCode = useCallback(
    (code: string) => {
      if (!code) {
        toast.show('提示', '邀请码明文仅在生成时返回，请从生成记录中复制')
        return
      }
      try {
        if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
          navigator.clipboard
            .writeText(code)
            .then(() => toast.show('已复制', `邀请码 ${code} 已复制到剪贴板`))
            .catch(() => toast.show('复制失败', '请手动复制邀请码'))
        } else {
          const textarea = document.createElement('textarea')
          textarea.value = code
          textarea.style.position = 'fixed'
          textarea.style.opacity = '0'
          document.body.appendChild(textarea)
          textarea.select()
          const ok = document.execCommand('copy')
          document.body.removeChild(textarea)
          if (ok) {
            toast.show('已复制', `邀请码 ${code} 已复制到剪贴板`)
          } else {
            toast.show('复制失败', '请手动复制邀请码')
          }
        }
      } catch {
        toast.show('复制失败', '请手动复制邀请码')
      }
    },
    [toast],
  )

  /** 作废邀请码 */
  const handleRevoke = useCallback(
    (inviteCodeId: string) => {
      revokeInviteCode.mutate(inviteCodeId, {
        onSuccess: () => {
          toast.show('邀请码已作废', '该邀请码已标记为已作废状态')
        },
        onError: (err: unknown) => {
          const axiosErr = err as { response?: { data?: { detail?: string } } }
          const message = axiosErr.response?.data?.detail ?? '邀请码作废失败'
          toast.show('作废失败', message)
        },
      })
    },
    [revokeInviteCode, toast],
  )

  /** 生成邀请码 - 提交 plan_code/grant_months/count/note（monitor_limit 由后端按 plan_code 计算） */
  const handleGenerate = useCallback(() => {
    createInviteCodes.mutate(
      {
        count: generateCount,
        note: generateNote,
        plan_code: generatePlanCode,
        grant_months: generateGrantMonths,
      },
      {
        onSuccess: (codes) => {
          setGeneratedCodes(codes)
          toast.show('邀请码已生成', `共生成 ${codes.length} 个邀请码`)
        },
        onError: (err: unknown) => {
          const axiosErr = err as { response?: { data?: { detail?: string } } }
          const message = axiosErr.response?.data?.detail ?? '邀请码生成失败'
          toast.show('生成失败', message)
        },
      },
    )
  }, [
    createInviteCodes,
    generateCount,
    generateNote,
    generatePlanCode,
    generateGrantMonths,
    toast,
  ])

  /** 打开生成弹窗，重置状态 */
  const handleOpenModal = useCallback(() => {
    setGeneratedCodes([])
    setGenerateCount(1)
    setGenerateNote('朋友内测')
    setGeneratePlanCode('observe_20')
    setGenerateGrantMonths(1)
    setModalOpen(true)
  }, [])

  /** 关闭生成弹窗 */
  const handleCloseModal = useCallback(() => {
    setModalOpen(false)
    setGeneratedCodes([])
  }, [])

  /** 重置登录（当前无后端接口，显示 toast 提示） */
  const handleResetLogin = useCallback(() => {
    toast.show('密码重置邮件已发送', `已向 ${selectedMember?.email ?? ''} 发送重置邮件`)
  }, [toast, selectedMember])

  /** 只读排障视角（当前无后端接口，显示 toast 提示） */
  const handleReadOnlyDebug = useCallback(() => {
    toast.show('已进入只读排障视角', '当前管理员以只读模式查看该用户数据')
  }, [toast])

  /** 保存会员资料（当前无后端接口，显示 toast 提示） */
  const handleSaveProfile = useCallback(() => {
    toast.show('会员资料已保存', '账户状态、会员状态、到期时间、角色已更新')
    handleCloseDrawer()
  }, [toast, handleCloseDrawer])

  /** 导出会员（当前无后端接口，显示 toast 提示） */
  const handleExportMembers = useCallback(() => {
    toast.show('导出会员', '会员列表导出功能开发中')
  }, [toast])

  // ===== 会员表列定义 =====
  const memberColumns: DataTableColumn<MemberRow>[] = useMemo(
    () => [
      {
        key: 'email',
        title: '用户',
        dataType: 'text',
        sortable: true,
        filterable: true,
        render: (row) => (
          <div>
            <div className="symbol">{getEmailUsername(row.email)}</div>
            <div className="symbol-sub">{row.email}</div>
          </div>
        ),
        filterValue: (row) => row.email,
        sortValue: (row) => row.email,
      },
      {
        key: 'membership_status',
        title: '会员状态',
        dataType: 'enum',
        sortable: true,
        filterable: true,
        enumOptions: [
          { label: '有效', value: '有效' },
          { label: '即将到期', value: '即将到期' },
          { label: '已到期', value: '已到期' },
          { label: '停用', value: '停用' },
          { label: '未开通', value: '未开通' },
        ],
        render: (row) => {
          const { label, pill } = getMemberStatusPill(row)
          return <span className={`status-pill ${pill}`}>{label}</span>
        },
        filterValue: (row) => getMemberStatusPill(row).label,
        sortValue: (row) => getMemberStatusPill(row).label,
      },
      {
        key: 'expires_at',
        title: '到期时间',
        dataType: 'datetime',
        sortable: true,
        filterable: true,
        render: (row) => formatDate(row.expires_at),
        filterValue: (row) => formatDate(row.expires_at),
        sortValue: (row) => row.expires_at ?? '',
      },
      {
        key: 'remaining_days',
        title: '剩余天数',
        dataType: 'number',
        sortable: true,
        filterable: true,
        render: (row) => {
          const days = row.remaining_days
          if (days === null) return '—'
          if (days < 0) return <span className="neg">{days} 天</span>
          return <span className="pos">{days} 天</span>
        },
        filterValue: (row) => String(row.remaining_days ?? ''),
        sortValue: (row) => row.remaining_days ?? 0,
      },
      {
        key: 'last_invite_code',
        title: '最近邀请码',
        dataType: 'text',
        sortable: false,
        filterable: false,
        // API 列表不返回明文邀请码，仅生成时可见
        render: () => '—',
      },
      {
        key: 'renewal_count',
        title: '累计续期',
        dataType: 'number',
        sortable: true,
        filterable: true,
        render: (row) => `${row.renewal_count} 次`,
        filterValue: (row) => String(row.renewal_count),
        sortValue: (row) => row.renewal_count,
      },
      {
        key: 'last_login',
        title: '最后登录',
        dataType: 'text',
        sortable: false,
        filterable: false,
        // API 列表不返回最后登录时间
        render: () => '—',
      },
      {
        key: 'actions',
        title: '',
        dataType: 'text',
        sortable: false,
        filterable: false,
        isAction: true,
        render: (row) => (
          <button className="btn small" onClick={() => handleOpenDrawer(row)}>
            管理
          </button>
        ),
      },
    ],
    [handleOpenDrawer],
  )

  // ===== 邀请码表列定义 =====
  const inviteColumns: DataTableColumn<InviteCodeRow>[] = useMemo(
    () => [
      {
        key: 'code',
        title: '邀请码',
        dataType: 'text',
        sortable: false,
        filterable: false,
        // API 列表不返回明文邀请码，仅生成时可见
        render: () => <b>—</b>,
      },
      {
        key: 'status',
        title: '状态',
        dataType: 'enum',
        sortable: true,
        filterable: true,
        enumOptions: [
          { label: '未使用', value: '未使用' },
          { label: '已使用', value: '已使用' },
          { label: '已作废', value: '已作废' },
        ],
        render: (row) => {
          const { label, pill } = getInviteStatusPill(row.status)
          return <span className={`status-pill ${pill}`}>{label}</span>
        },
        filterValue: (row) => getInviteStatusPill(row.status).label,
        sortValue: (row) => getInviteStatusPill(row.status).label,
      },
      {
        key: 'plan_code',
        title: '套餐',
        dataType: 'enum',
        sortable: true,
        filterable: true,
        enumOptions: [
          { label: '观察版', value: '观察版' },
          { label: '研究版', value: '研究版' },
        ],
        render: (row) => getPlanName(row.plan_code),
        filterValue: (row) => getPlanName(row.plan_code),
        sortValue: (row) => getPlanName(row.plan_code),
      },
      {
        key: 'monitor_limit',
        title: '最大自选',
        dataType: 'number',
        sortable: true,
        filterable: false,
        render: (row) =>
          row.monitor_limit != null ? `${row.monitor_limit} 只` : getPlanMonitorLimit(row.plan_code),
        sortValue: (row) => row.monitor_limit ?? 0,
      },
      {
        key: 'grant_months',
        title: '有效月数',
        dataType: 'number',
        sortable: true,
        filterable: false,
        render: (row) => (row.grant_months != null ? `${row.grant_months} 个月` : '—'),
        sortValue: (row) => row.grant_months ?? 0,
      },
      {
        key: 'usage_type',
        title: '兑换用途',
        dataType: 'enum',
        sortable: true,
        filterable: true,
        enumOptions: [
          { label: '注册', value: '注册' },
          { label: '续期', value: '续期' },
        ],
        render: (row) => {
          const tag = getUsageTypeTag(row.usage_type)
          if (!tag) return '—'
          return <span className={`tag ${tag.tag}`}>{tag.label}</span>
        },
        filterValue: (row) => getUsageTypeTag(row.usage_type)?.label ?? '',
        sortValue: (row) => getUsageTypeTag(row.usage_type)?.label ?? '',
      },
      {
        key: 'used_by',
        title: '使用者',
        dataType: 'text',
        sortable: false,
        filterable: true,
        render: (row) => row.used_by ?? '—',
        filterValue: (row) => row.used_by ?? '',
      },
      {
        key: 'used_at',
        title: '使用时间',
        dataType: 'datetime',
        sortable: true,
        filterable: true,
        render: (row) => formatDateTime(row.used_at),
        filterValue: (row) => formatDateTime(row.used_at),
        sortValue: (row) => row.used_at ?? '',
      },
      {
        key: 'note',
        title: '备注',
        dataType: 'text',
        sortable: false,
        filterable: true,
        render: (row) => row.note ?? '—',
        filterValue: (row) => row.note ?? '',
      },
      {
        key: 'actions',
        title: '',
        dataType: 'text',
        sortable: false,
        filterable: false,
        isAction: true,
        render: (row) => {
          const status = getInviteStatusPill(row.status)
          if (status.label === '未使用') {
            return (
              <>
                <button className="btn small" onClick={() => handleCopyCode('')}>
                  复制
                </button>
                <button
                  className="btn small danger"
                  onClick={() => handleRevoke(row.id)}
                >
                  作废
                </button>
              </>
            )
          }
          if (status.label === '已使用') {
            return (
              <button
                className="btn small"
                onClick={() => toast.show('已打开兑换记录', '兑换记录详情功能开发中')}
              >
                记录
              </button>
            )
          }
          return null
        },
      },
    ],
    [handleCopyCode, handleRevoke, toast],
  )

  // ===== 兑换记录时间线 =====
  const redemptions = redemptionsQuery.data ?? []

  // 生成邀请码弹窗权益预览（如"研究版 · 最多50只自选股 · 有效期6个月"）
  const benefitPreview = useMemo(() => {
    const contract = PLAN_CONTRACTS_PREVIEW[generatePlanCode]
    return `${contract.name} · 最多${contract.monitorLimit}只自选股 · 有效期${generateGrantMonths}个月`
  }, [generatePlanCode, generateGrantMonths])

  // ===== 渲染 =====
  return (
    <>
      {/* 页头 */}
      <div className="page-head">
        <div>
          <h1 className="page-title">会员与邀请码</h1>
          <div className="page-desc">
            邀请码绑定套餐（观察版/研究版）与有效期月数，用于注册或续期，兑换后按套餐开通自选额度
          </div>
        </div>
        <div className="actions">
          <button className="btn" onClick={handleExportMembers}>
            导出会员
          </button>
          <button className="btn primary" onClick={handleOpenModal}>
            ＋ 生成邀请码
          </button>
        </div>
      </div>

      {/* KPI 卡片 */}
      <div className="grid kpi membership-kpis">
        <div className="card kpi-card">
          <div className="kpi-label">有效会员</div>
          <div className="kpi-value">{kpis.activeMembers}</div>
          <div className="kpi-foot">
            <span className="kpi-delta up">+{kpis.registerCount}</span> 本月新增
          </div>
        </div>
        <div className="card kpi-card">
          <div className="kpi-label">7天内到期</div>
          <div className="kpi-value">{kpis.expiringSoon}</div>
          <div className="kpi-foot">可提醒用户准备续期码</div>
        </div>
        <div className="card kpi-card">
          <div className="kpi-label">未使用邀请码</div>
          <div className="kpi-value">{kpis.unusedCodes}</div>
          <div className="kpi-foot">一次性兑换码</div>
        </div>
        <div className="card kpi-card">
          <div className="kpi-label">本月兑换</div>
          <div className="kpi-value">{kpis.monthlyRedeem}</div>
          <div className="kpi-foot">
            注册 {kpis.registerCount} · 续期 {kpis.renewCount}
          </div>
        </div>
      </div>

      {/* 三 tab */}
      <div className="tabs admin-member-tabs">
        <div
          className={clsx('tab', activeTab === 'memberList' && 'active')}
          onClick={() => setActiveTab('memberList')}
        >
          会员账户
        </div>
        <div
          className={clsx('tab', activeTab === 'inviteList' && 'active')}
          onClick={() => setActiveTab('inviteList')}
        >
          邀请码管理
        </div>
        <div
          className={clsx('tab', activeTab === 'rulePanel' && 'active')}
          onClick={() => setActiveTab('rulePanel')}
        >
          规则说明
        </div>
      </div>

      {/* 会员账户 tab */}
      {activeTab === 'memberList' && (
        <div className="tab-panel active">
          <div className="card">
            <StrategyDataTable
              tableId="admin-members"
              columns={memberColumns}
              rows={members}
              rowKey={(row) => row.user_id}
              loading={membersQuery.isLoading}
              error={
                membersQuery.isError
                  ? (membersQuery.error as Error)?.message ?? '加载失败'
                  : null
              }
              emptyText="暂无会员账户"
            />
          </div>
        </div>
      )}

      {/* 邀请码管理 tab */}
      {activeTab === 'inviteList' && (
        <div className="tab-panel active">
          <div className="card">
            <StrategyDataTable
              tableId="admin-invite-codes"
              columns={inviteColumns}
              rows={inviteCodes}
              rowKey={(row) => row.id}
              loading={inviteCodesQuery.isLoading}
              error={
                inviteCodesQuery.isError
                  ? (inviteCodesQuery.error as Error)?.message ?? '加载失败'
                  : null
              }
              emptyText="暂无邀请码"
            />
          </div>
        </div>
      )}

      {/* 规则说明 tab */}
      {activeTab === 'rulePanel' && (
        <div className="tab-panel active">
          <div className="grid split-even">
            {/* 前期会员规则 */}
            <div className="card">
              <div className="card-head">
                <div>
                  <div className="card-title">前期会员规则</div>
                  <div className="card-sub">作为后端实现的默认业务约束</div>
                </div>
              </div>
              <div className="card-body">
                <div className="rule-list">
                  <div>
                    <i>1</i>
                    <span>
                      <b>套餐制会员</b>
                      <small>邀请码绑定观察版（20 只自选）或研究版（50 只自选），会员有效时按套餐开放功能。</small>
                    </span>
                  </div>
                  <div>
                    <i>2</i>
                    <span>
                      <b>注册必须使用邀请码</b>
                      <small>邀请码验证成功后创建账户并按套餐激活会员有效期。</small>
                    </span>
                  </div>
                  <div>
                    <i>3</i>
                    <span>
                      <b>邀请码一次性兑换</b>
                      <small>同一邀请码只能用于一次注册或一次续期，避免多人共享。</small>
                    </span>
                  </div>
                  <div>
                    <i>4</i>
                    <span>
                      <b>续期按邀请码套餐月数</b>
                      <small>未到期从原到期日顺延；已到期从兑换当天重新计算。</small>
                    </span>
                  </div>
                </div>
              </div>
            </div>

            {/* 到期后的处理 */}
            <div className="card">
              <div className="card-head">
                <div>
                  <div className="card-title">到期后的处理</div>
                  <div className="card-sub">不删除用户数据，不制造恢复风险</div>
                </div>
              </div>
              <div className="card-body">
                <div className="notice warn">
                  会员到期后暂停进入业务页面，但账户、方案、自选股、运行记录与通知配置全部保留。登录后直接进入续期页。
                </div>
                <div className="summary-row">
                  <span>账户登录</span>
                  <b>允许</b>
                </div>
                <div className="summary-row">
                  <span>业务功能</span>
                  <b>续期前暂停</b>
                </div>
                <div className="summary-row">
                  <span>数据保留</span>
                  <b className="pos">完整保留</b>
                </div>
                <div className="summary-row">
                  <span>管理员停用</span>
                  <b>独立于会员到期</b>
                </div>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* 用户详情抽屉 userDrawer */}
      {drawerOpen && selectedMember && (
        <div className="drawer-backdrop open" onClick={handleCloseDrawer}>
          <aside className="drawer" onClick={(e) => e.stopPropagation()}>
            <div className="drawer-head">
              <div>
                <b>会员详情 · {getEmailUsername(selectedMember.email)}</b>
                <div className="card-sub">
                  会员状态控制有效期，自选股额度由套餐决定
                </div>
              </div>
              <button className="icon-btn" onClick={handleCloseDrawer}>
                ×
              </button>
            </div>

            <div className="drawer-body">
              {/* 抽屉内 KPI */}
              <div className="grid split-even">
                <div className="card kpi-card">
                  <div className="kpi-label">会员剩余</div>
                  <div className="kpi-value">
                    {selectedMember.remaining_days ?? 0} 天
                  </div>
                </div>
                <div className="card kpi-card">
                  <div className="kpi-label">累计兑换</div>
                  <div className="kpi-value">{selectedMember.renewal_count} 次</div>
                </div>
              </div>

              {/* 抽屉内三 tab */}
              <div className="tabs drawer-tabs">
                <div
                  className={clsx('tab', drawerTab === 'profile' && 'active')}
                  onClick={() => setDrawerTab('profile')}
                >
                  账户
                </div>
                <div
                  className={clsx('tab', drawerTab === 'membership' && 'active')}
                  onClick={() => setDrawerTab('membership')}
                >
                  会员记录
                </div>
                <div
                  className={clsx('tab', drawerTab === 'audit' && 'active')}
                  onClick={() => setDrawerTab('audit')}
                >
                  审计
                </div>
              </div>

              {/* 账户 tab */}
              {drawerTab === 'profile' && (
                <div className="tab-panel active drawer-tab-panel">
                  <div className="form-grid">
                    <div className="form-row">
                      <label className="form-label">账户状态</label>
                      <select
                        className="select"
                        value={accountStatusEdit}
                        onChange={(e) => setAccountStatusEdit(e.target.value)}
                      >
                        <option>有效</option>
                        <option>停用</option>
                      </select>
                    </div>
                    <div className="form-row">
                      <label className="form-label">会员状态</label>
                      <select
                        className="select"
                        value={membershipStatusEdit}
                        onChange={(e) => setMembershipStatusEdit(e.target.value)}
                      >
                        <option>有效</option>
                        <option>已到期</option>
                      </select>
                    </div>
                    <div className="form-row">
                      <label className="form-label">会员到期时间</label>
                      <input
                        className="input"
                        type="date"
                        value={expiresAtEdit}
                        onChange={(e) => setExpiresAtEdit(e.target.value)}
                      />
                    </div>
                    <div className="form-row">
                      <label className="form-label">角色</label>
                      <select
                        className="select"
                        value={roleEdit}
                        onChange={(e) => setRoleEdit(e.target.value)}
                      >
                        <option>普通会员</option>
                        <option>管理员</option>
                      </select>
                    </div>
                  </div>
                  <div className="notice drawer-notice">
                    手工修改到期日仅用于异常修正；正常注册和续期必须通过邀请码兑换记录完成。
                  </div>
                </div>
              )}

              {/* 会员记录 tab */}
              {drawerTab === 'membership' && (
                <div className="tab-panel active drawer-tab-panel">
                  {redemptionsQuery.isLoading && (
                    <div className="empty">加载兑换记录中…</div>
                  )}
                  {!redemptionsQuery.isLoading && redemptions.length === 0 && (
                    <div className="empty">暂无兑换记录</div>
                  )}
                  {redemptions.length > 0 && (
                    <div className="timeline-simple">
                      {redemptions.map((r) => {
                        const tag = getUsageTypeTag(r.usage_type)
                        const isRegister = r.usage_type === 'register'
                        return (
                          <div key={r.id}>
                            <i className={isRegister ? '' : 'good'} />
                            <span>
                              <b>
                                {formatDate(r.redeemed_at)} · 邀请码
                                {tag?.label ?? r.usage_type}
                              </b>
                              <small>
                                新到期日 {formatDate(r.new_expires_at)}
                              </small>
                            </span>
                          </div>
                        )
                      })}
                    </div>
                  )}
                </div>
              )}

              {/* 审计 tab */}
              {drawerTab === 'audit' && (
                <div className="tab-panel active drawer-tab-panel">
                  <div className="empty">最近管理员审计记录</div>
                </div>
              )}
            </div>

            <div className="drawer-foot">
              <button className="btn" onClick={handleResetLogin}>
                重置登录
              </button>
              <button className="btn" onClick={handleReadOnlyDebug}>
                只读排障
              </button>
              <button className="btn primary" onClick={handleSaveProfile}>
                保存
              </button>
            </div>
          </aside>
        </div>
      )}

      {/* 生成邀请码弹窗 generateInviteModal */}
      {modalOpen && (
        <div className="modal-backdrop open" onClick={handleCloseModal}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-head">
              <div>
                <b>生成邀请码</b>
                <div className="card-sub">
                  选择套餐与有效期，兑换后按套餐开通自选额度与会员月数
                </div>
              </div>
              <button className="icon-btn" onClick={handleCloseModal}>
                ×
              </button>
            </div>

            <div className="modal-body">
              <div className="form-grid">
                <div className="form-row">
                  <label className="form-label">套餐类型</label>
                  <select
                    className="select"
                    value={generatePlanCode}
                    onChange={(e) => setGeneratePlanCode(e.target.value as PlanCode)}
                  >
                    <option value="observe_20">观察版（最多20只自选股）</option>
                    <option value="research_50">研究版（最多50只自选股）</option>
                  </select>
                </div>
                <div className="form-row">
                  <label className="form-label">有效期月数</label>
                  <input
                    className="input"
                    type="number"
                    min={1}
                    max={36}
                    value={generateGrantMonths}
                    onChange={(e) => {
                      const v = Number(e.target.value)
                      if (Number.isFinite(v)) {
                        setGenerateGrantMonths(Math.min(36, Math.max(1, Math.trunc(v))))
                      }
                    }}
                  />
                </div>
                <div className="form-row">
                  <label className="form-label">生成数量</label>
                  <select
                    className="select"
                    value={generateCount}
                    onChange={(e) => setGenerateCount(Number(e.target.value))}
                  >
                    <option value={1}>1</option>
                    <option value={5}>5</option>
                    <option value={10}>10</option>
                    <option value={20}>20</option>
                  </select>
                </div>
                <div className="form-row full">
                  <label className="form-label">批次备注</label>
                  <input
                    className="input"
                    value={generateNote}
                    placeholder="例如：6月线下交流会"
                    onChange={(e) => setGenerateNote(e.target.value)}
                  />
                </div>
              </div>

              <div className="notice modal-notice benefit-preview">
                权益预览：<b>{benefitPreview}</b>
              </div>

              <div className="notice modal-notice">
                邀请码不绑定具体用户。兑换成功后立即标记为已使用，并记录注册/续期用途、使用者与时间。最大自选股数量由后端按套餐计算。
              </div>

              {/* 生成后显示新码 */}
              {generatedCodes.length > 0 && (
                <div className="generated-invite-list">
                  <div className="generated-invite-title">新邀请码</div>
                  {generatedCodes.map((code) => (
                    <div key={code.id} className="generated-invite-box">
                      <b>{code.code}</b>
                      <button
                        className="btn small"
                        onClick={() => handleCopyCode(code.code)}
                      >
                        复制
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </div>

            <div className="modal-foot">
              <button className="btn" onClick={handleCloseModal}>
                取消
              </button>
              <button
                className="btn primary"
                onClick={handleGenerate}
                disabled={createInviteCodes.isPending}
              >
                {createInviteCodes.isPending ? '生成中...' : '生成邀请码'}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  )
}
