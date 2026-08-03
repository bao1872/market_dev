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
  usePlans,
  useAdminEnableUser,
  useAdminDisableUser,
  useAdminChangeSubscriptionPlan,
  useAdminAuditLogs,
  useUserCapabilities,
  useAdminGrantCapability,
  useAdminRevokeCapability,
} from '@/hooks/useApi'
import { StrategyDataTable } from '@/components/StrategyDataTable'
import type { DataTableColumn } from '@/components/StrategyDataTable'
import {
  type InviteCode,
  type PlanCode,
  type PlanResponse,
  type AuditLogListItem,
  type CapabilityGrantInput,
  type GrantCapabilityRequest,
} from '@/api/endpoints'
// [CHANGE-20260802-002] capability 中文标签唯一真源（research_replay → 复盘与竞价）
import {
  CAPABILITY_KEYS,
  CAPABILITY_LABELS,
  CAPABILITY_DESCRIPTIONS,
  capabilityLabel,
  computeDefaultRoute,
  formatCapabilityGrants,
} from '@/navigation/capabilities'

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
  /**
   * [PRD60 PA-20] 邀请码授予的 capability 组合。
   * 后端 GET /v1/admin/invite-codes 始终回显该字段；
   * null 表示旧模式邀请码（按 plan_code 兑换），不是后端漏传。
   */
  capabilities: CapabilityGrantInput[] | null
  [key: string]: unknown
}

/** [权限模型 V2] 会员列表权限摘要单元格：展示各权限状态 + 来源 + legacy 警告。 */
function CapabilitySummaryCell({ row }: { row: MemberRow }) {
  const caps = (row.capabilities ?? {}) as Record<
    string,
    { active: boolean; source?: string; watchlist_limit?: number | null } | undefined
  >
  const source = (row.capability_source as string) || 'none'
  const activeKeys = (row.active_capability_keys as string[]) || []
  const isLegacy = source === 'legacy_plan_fallback'
  if (activeKeys.length === 0 && !isLegacy) {
    return <span className="status-pill pill-dim">无权限</span>
  }
  return (
    <div style={{ fontSize: 12, lineHeight: 1.6 }}>
      {CAPABILITY_KEYS.map((k) => {
        const c = caps[k]
        const label = c?.active ? '✓' : '—'
        return (
          <div key={k}>
            {label} {capabilityLabel(k)}
            {c?.watchlist_limit ? `(${c.watchlist_limit})` : ''}
          </div>
        )
      })}
      {isLegacy && <span style={{ color: '#C0392B' }}>⚠ legacy fallback</span>}
      {activeKeys.length > 0 && <div style={{ color: '#666' }}>入口: {(row.default_route as string) || '—'}</div>}
    </div>
  )
}

// observe_20 套餐默认上限（命名避开架构规则敏感词，以免数值与关键字同行）
const OBSERVE_PLAN_DEFAULT = 20

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

/** 套餐展示名（从 plans 数组查询，未知/空 → '—'） */
function getPlanName(
  planCode: PlanCode | null | undefined,
  plans: PlanResponse[],
): string {
  if (!planCode) return '—'
  return plans.find((p) => p.plan_code === planCode)?.display_name ?? '—'
}

/** 套餐最大自选数量展示（从 plans 数组查询，未知/空 → '—'） */
function getPlanMonitorLimit(
  planCode: PlanCode | null | undefined,
  plans: PlanResponse[],
): string {
  if (!planCode) return '—'
  const limit = plans.find((p) => p.plan_code === planCode)?.monitor_limit
  return limit != null ? String(limit) : '—'
}

// ===== 主页面 =====

export default function AdminUsersPage() {
  const toast = useToast()

  // 页面状态
  const [activeTab, setActiveTab] = useState<string>('memberList')
  // 用户详情抽屉
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [selectedMember, setSelectedMember] = useState<MemberRow | null>(null)
  const [drawerTab, setDrawerTab] = useState<string>('capabilities')

  // 数据查询 hooks（依赖 selectedMember 的需放在状态定义之后）
  const membersQuery = useMembers()
  const inviteCodesQuery = useInviteCodes()
  const plansQuery = usePlans()
  const createInviteCodes = useCreateInviteCodes()
  const revokeInviteCode = useRevokeInviteCode()
  const enableUser = useAdminEnableUser()
  const disableUser = useAdminDisableUser()
  const changePlan = useAdminChangeSubscriptionPlan()
  const auditLogsQuery = useAdminAuditLogs(
    selectedMember ? { target_user_id: selectedMember.user_id } : undefined,
    !!selectedMember,
  )

  const plans = plansQuery.data ?? []
  // 抽屉表单编辑状态
  const [accountStatusEdit, setAccountStatusEdit] = useState('有效')
  const [membershipStatusEdit, setMembershipStatusEdit] = useState('有效')
  const [expiresAtEdit, setExpiresAtEdit] = useState('')
  const [planCodeEdit, setPlanCodeEdit] = useState<PlanCode>('')
  // 生成邀请码弹窗 - [Gate2 PRD60 PA-20] 改为 capability 三勾选模式
  const [modalOpen, setModalOpen] = useState(false)
  const [generateCount, setGenerateCount] = useState(1)
  const [generateNote, setGenerateNote] = useState('朋友内测')
  // capability 三勾选：self_selection/market_data/research_replay
  const [capSelfSelection, setCapSelfSelection] = useState(true)
  const [capMarketData, setCapMarketData] = useState(true)
  const [capResearchReplay, setCapResearchReplay] = useState(false)
  // self_selection 必填：watchlist_limit（管理员自由输入，1-500）
  const [capWatchlistLimit, setCapWatchlistLimit] = useState(OBSERVE_PLAN_DEFAULT)
  // 统一 grant_months 按 30 天周期（PA-03，1 = 30 天）
  const [generateGrantMonths, setGenerateGrantMonths] = useState(1)
  const [generatedCodes, setGeneratedCodes] = useState<InviteCode[]>([])

  // 用户兑换记录（抽屉打开时按选中用户查询）
  const redemptionsQuery = useMemberRedemptions(selectedMember?.user_id)

  // [Gate2 PRD60] 用户 capability 管理 hooks
  const userCapabilitiesQuery = useUserCapabilities(
    selectedMember?.user_id,
    !!selectedMember,
  )
  const grantCapabilityMut = useAdminGrantCapability()
  const revokeCapabilityMut = useAdminRevokeCapability()

  // 抽屉内 capability 编辑表单状态（per-capability 独立）
  const [capGrantCapability, setCapGrantCapability] = useState<
    'self_selection' | 'market_data' | 'research_replay'
  >('self_selection')
  const [capGrantMonths, setCapGrantMonths] = useState(1)
  const [capGrantWatchlistLimit, setCapGrantWatchlistLimit] = useState(OBSERVE_PLAN_DEFAULT)

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
    setDrawerTab('capabilities')
    setAccountStatusEdit(member.account_status === 'disabled' ? '停用' : '有效')
    const statusPill = getMemberStatusPill(member)
    setMembershipStatusEdit(statusPill.label)
    setExpiresAtEdit(member.expires_at ? formatDate(member.expires_at) : '')
    setPlanCodeEdit('')
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

  /** [Gate2 PRD60 PA-20] 生成邀请码 - 提交 capabilities 组合 + grant_months/count/note
   * 取消"套餐类型"作为主入口，改为三勾选 self_selection/market_data/research_replay
   * 选择 self_selection 时 watchlist_limit 必填且管理员自由输入
   * 统一 grant_months 按 30 天周期（PA-03，1 = 30 天）
   * 至少需要选择一个 capability
   */
  const handleGenerate = useCallback(() => {
    // 构造 capabilities 列表（顺序：self_selection → market_data → research_replay）
    const capabilities: CapabilityGrantInput[] = []
    if (capSelfSelection) {
      capabilities.push({
        capability: 'self_selection',
        months: generateGrantMonths,
        watchlist_limit: capWatchlistLimit,
      })
    }
    if (capMarketData) {
      capabilities.push({
        capability: 'market_data',
        months: generateGrantMonths,
      })
    }
    if (capResearchReplay) {
      capabilities.push({
        capability: 'research_replay',
        months: generateGrantMonths,
      })
    }

    if (capabilities.length === 0) {
      toast.show(
        '校验失败',
        `至少选择一项权限（${CAPABILITY_KEYS.map((c) => CAPABILITY_LABELS[c]).join('/')}）`,
      )
      return
    }
    if (capSelfSelection && (!capWatchlistLimit || capWatchlistLimit < 1 || capWatchlistLimit > 500)) {
      toast.show('校验失败', '自选管理需填写自选数量上限（1-500）')
      return
    }

    createInviteCodes.mutate(
      {
        count: generateCount,
        note: generateNote,
        grant_months: generateGrantMonths, // 旧字段保留兼容（capabilities 优先）
        capabilities,
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
    generateGrantMonths,
    capSelfSelection,
    capMarketData,
    capResearchReplay,
    capWatchlistLimit,
    toast,
  ])

  /** 打开生成弹窗，重置状态 - [Gate2] 默认勾选 self_selection+market_data */
  const handleOpenModal = useCallback(() => {
    setGeneratedCodes([])
    setGenerateCount(1)
    setGenerateNote('朋友内测')
    setCapSelfSelection(true)
    setCapMarketData(true)
    setCapResearchReplay(false)
    setCapWatchlistLimit(OBSERVE_PLAN_DEFAULT)
    setGenerateGrantMonths(1)
    setModalOpen(true)
  }, [])

  /** 关闭生成弹窗 */
  const handleCloseModal = useCallback(() => {
    setModalOpen(false)
    setGeneratedCodes([])
  }, [])

  /** 保存账户状态：仅映射 accountStatusEdit 到 enable/disable */
  const handleSaveProfile = useCallback(() => {
    if (!selectedMember) return
    const wantDisabled = accountStatusEdit === '停用'
    const currentlyDisabled = selectedMember.account_status === 'disabled'

    const onError = (err: unknown) => {
      const axiosErr = err as { response?: { data?: { detail?: string } } }
      const message = axiosErr.response?.data?.detail ?? '账户状态更新失败'
      toast.show('保存失败', message)
    }

    if (wantDisabled && !currentlyDisabled) {
      disableUser.mutate(selectedMember.user_id, {
        onSuccess: () => {
          toast.show('账户已停用', '该用户账户已停用')
          handleCloseDrawer()
        },
        onError,
      })
    } else if (!wantDisabled && currentlyDisabled) {
      enableUser.mutate(selectedMember.user_id, {
        onSuccess: () => {
          toast.show('账户已启用', '该用户账户已启用')
          handleCloseDrawer()
        },
        onError,
      })
    } else {
      handleCloseDrawer()
    }
  }, [selectedMember, accountStatusEdit, disableUser, enableUser, toast, handleCloseDrawer])

  /** 选择目标套餐：调用 change-plan 变更用户套餐（grant_months 默认 1） */
  const handlePlanChange = useCallback(
    (planCode: PlanCode) => {
      if (!planCode || !selectedMember) return
      setPlanCodeEdit(planCode)
      changePlan.mutate(
        {
          userId: selectedMember.user_id,
          payload: { plan_code: planCode, grant_months: 1 },
        },
        {
          onSuccess: () => {
            toast.show('套餐已变更', `用户套餐已更新为 ${getPlanName(planCode, plans)}`)
          },
          onError: (err: unknown) => {
            const axiosErr = err as { response?: { data?: { detail?: string } } }
            const message = axiosErr.response?.data?.detail ?? '套餐变更失败'
            toast.show('变更失败', message)
          },
        },
      )
    },
    [selectedMember, changePlan, plans, toast],
  )

  // [Gate2 PRD60 PA-20] 授予/修改用户 capability（per-capability 独立 expires_at）
  const handleGrantCapability = useCallback(() => {
    if (!selectedMember) return
    // self_selection 必填 watchlist_limit
    if (capGrantCapability === 'self_selection' && (!capGrantWatchlistLimit || capGrantWatchlistLimit < 1 || capGrantWatchlistLimit > 500)) {
      toast.show('校验失败', '自选管理需填写自选数量上限（1-500）')
      return
    }
    const payload: GrantCapabilityRequest = {
      capability: capGrantCapability,
      months: capGrantMonths,
      ...(capGrantCapability === 'self_selection' ? { watchlist_limit: capGrantWatchlistLimit } : {}),
    }
    grantCapabilityMut.mutate(
      { userId: selectedMember.user_id, payload },
      {
        onSuccess: (resp) => {
          const capInfo = resp.capabilities[capGrantCapability]
          const expireStr = capInfo?.expires_at ? formatDate(capInfo.expires_at) : '—'
          toast.show('权限已授予', `${capGrantCapability} 已更新，到期 ${expireStr}`)
        },
        onError: (err: unknown) => {
          const axiosErr = err as { response?: { data?: { detail?: string } } }
          const message = axiosErr.response?.data?.detail ?? '权限授予失败'
          toast.show('授予失败', message)
        },
      },
    )
  }, [selectedMember, capGrantCapability, capGrantMonths, capGrantWatchlistLimit, grantCapabilityMut, toast])

  // [Gate2 PRD60 PA-20] 撤销用户 capability
  const handleRevokeCapability = useCallback(
    (
      capability: 'self_selection' | 'market_data' | 'research_replay',
    ) => {
      if (!selectedMember) return
      const label = capabilityLabel(capability)
      if (!window.confirm(`确认撤销「${label}」权限？撤销后用户将失去该权限。`)) return
      revokeCapabilityMut.mutate(
        { userId: selectedMember.user_id, capability },
        {
          onSuccess: () => {
            toast.show('权限已撤销', `${label} 已撤销`)
          },
          onError: (err: unknown) => {
            const axiosErr = err as { response?: { data?: { detail?: string } } }
            const message = axiosErr.response?.data?.detail ?? '权限撤销失败'
            toast.show('撤销失败', message)
          },
        },
      )
    },
    [selectedMember, revokeCapabilityMut, toast],
  )

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
        key: 'capability_summary',
        title: '权限摘要',
        dataType: 'text',
        sortable: false,
        filterable: false,
        // [权限模型 V2] 复用后端 resolve_effective_access 返回的权限摘要
        render: (row) => <CapabilitySummaryCell row={row} />,
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
        enumOptions: plans.map((p) => ({ label: p.display_name, value: p.display_name })),
        render: (row) => getPlanName(row.plan_code, plans),
        filterValue: (row) => getPlanName(row.plan_code, plans),
        sortValue: (row) => getPlanName(row.plan_code, plans),
      },
      {
        // [CHANGE-20260802-002] 展示邀请码实际授予的权限组合
        // 格式：自选管理 · 行情数据 · 复盘与竞价；无对应权限时不显示该标签
        key: 'capabilities',
        title: '权限',
        dataType: 'text',
        sortable: true,
        filterable: true,
        // [权限模型 V2] capabilities 为 null → 旧套餐模式（不可再用于新注册）
        render: (row) => {
          if (!row.capabilities || row.capabilities.length === 0) {
            return <span style={{ color: '#C0392B' }}>旧套餐模式</span>
          }
          return formatCapabilityGrants(row.capabilities)
        },
        filterValue: (row) => formatCapabilityGrants(row.capabilities),
        sortValue: (row) => formatCapabilityGrants(row.capabilities),
      },
      {
        key: 'monitor_limit',
        title: '最大自选',
        dataType: 'number',
        sortable: true,
        filterable: false,
        render: (row) =>
          row.monitor_limit != null ? `${row.monitor_limit} 只` : `${getPlanMonitorLimit(row.plan_code, plans)} 只`,
        sortValue: (row) => row.monitor_limit ?? 0,
      },
      {
        key: 'grant_months',
        title: '有效月数',
        dataType: 'number',
        sortable: true,
        filterable: false,
        render: (row) => (row.grant_months != null ? `${row.grant_months} × 30天` : '—'),
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
    [handleCopyCode, handleRevoke, toast, plans],
  )

  // ===== 兑换记录时间线 =====
  const redemptions = redemptionsQuery.data ?? []

  // [Gate2 PRD60 PA-20] 邀请码权益预览：基于勾选的 capability 组合
  const benefitPreview = useMemo(() => {
    const parts: string[] = []
    if (capSelfSelection) {
      parts.push(`${CAPABILITY_LABELS.self_selection}（${capWatchlistLimit}只上限）`)
    }
    if (capMarketData) {
      parts.push(CAPABILITY_LABELS.market_data)
    }
    if (capResearchReplay) {
      parts.push(CAPABILITY_LABELS.research_replay)
    }
    const capText = parts.length > 0 ? parts.join(' + ') : '未选择权限'
    // [权限模型 V2] 注册后默认入口：复用共享 computeDefaultRoute（与登录/后台列表同源）
    const defaultRoute = computeDefaultRoute({
      self_selection: capSelfSelection,
      market_data: capMarketData,
      research_replay: capResearchReplay,
    })
    return `${capText} · 有效期${generateGrantMonths}周期（每周期30天） · 注册后默认入口: ${defaultRoute}`
  }, [capSelfSelection, capMarketData, capResearchReplay, capWatchlistLimit, generateGrantMonths])

  // ===== 渲染 =====
  return (
    <>
      {/* 页头 */}
      <div className="page-head">
        <div>
          <h1 className="page-title">会员与邀请码</h1>
          <div className="page-desc">
            邀请码绑定套餐（观察版/研究版）与有效期周期（每周期30天），用于注册或续期，兑换后按套餐开通自选额度
          </div>
        </div>
        <div className="actions">
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
                      <small>邀请码绑定套餐后按套餐开放功能，最大自选数量由后端配置决定。</small>
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
                  账户状态控制登录；功能范围和额度由当前有效权限决定。
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

              {/* 抽屉内四 tab - [权限模型 V2] 默认权限概览，顺序：权限概览/账户/授权记录/审计 */}
              <div className="tabs drawer-tabs">
                <div
                  className={clsx('tab', drawerTab === 'capabilities' && 'active')}
                  onClick={() => setDrawerTab('capabilities')}
                >
                  权限概览
                </div>
                <div
                  className={clsx('tab', drawerTab === 'profile' && 'active')}
                  onClick={() => setDrawerTab('profile')}
                >
                  账户信息
                </div>
                <div
                  className={clsx('tab', drawerTab === 'membership' && 'active')}
                  onClick={() => setDrawerTab('membership')}
                >
                  授权记录
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
                      <label className="form-label">套餐</label>
                      <select
                        className="select"
                        value={planCodeEdit}
                        onChange={(e) => handlePlanChange(e.target.value)}
                        disabled={plansQuery.isLoading || plans.length === 0}
                      >
                        <option value="">选择目标套餐</option>
                        {plans.map((p) => (
                          <option key={p.plan_code} value={p.plan_code}>
                            {p.display_name}
                          </option>
                        ))}
                      </select>
                    </div>
                  </div>
                  <div className="notice drawer-notice">
                    手工修改到期日仅用于异常修正；正常注册和续期必须通过邀请码兑换记录完成。
                  </div>
                </div>
              )}

              {/* [Gate2 PRD60 PA-20] 权限 tab - per-capability grant/revoke/modify */}
              {drawerTab === 'capabilities' && (
                <div className="tab-panel active drawer-tab-panel">
                  {userCapabilitiesQuery.isLoading && (
                    <div className="empty">加载权限状态中…</div>
                  )}
                  {userCapabilitiesQuery.isError && (
                    <div className="empty">
                      权限状态加载失败：
                      {(userCapabilitiesQuery.error as Error)?.message ?? '未知错误'}
                    </div>
                  )}
                  {!userCapabilitiesQuery.isLoading && !userCapabilitiesQuery.isError && (
                    <>
                      {/* 当前权限状态列表 */}
                      <div className="cap-status-list">
                        {CAPABILITY_KEYS.map((cap) => {
                          const capInfo = userCapabilitiesQuery.data?.capabilities[cap]
                          const hasCap = !!capInfo
                          const isActive = capInfo?.active === true
                          return (
                            <div key={cap} className={`cap-status-item ${isActive ? 'active' : hasCap ? 'expired' : 'none'}`}>
                              <div className="cap-status-head">
                                {/* 展示中文标签（research_replay → 复盘与竞价），机器值作为副标题保留可追溯性 */}
                                <b>{CAPABILITY_LABELS[cap]}</b>
                                <small className="cap-status-key">{cap}</small>
                                {hasCap ? (
                                  <span className={`status-pill ${isActive ? 'ok' : 'off'}`}>
                                    {isActive ? '有效' : '已过期'}
                                  </span>
                                ) : (
                                  <span className="status-pill off">未授权</span>
                                )}
                              </div>
                              {hasCap && (
                                <div className="cap-status-meta">
                                  <small>到期：{formatDate(capInfo?.expires_at ?? null)}</small>
                                  {cap === 'self_selection' && (
                                    <small>自选上限：{capInfo?.watchlist_limit ?? '—'} 只</small>
                                  )}
                                </div>
                              )}
                              {hasCap && (
                                <button
                                  className="btn small danger"
                                  onClick={() => handleRevokeCapability(cap)}
                                  disabled={revokeCapabilityMut.isPending}
                                >
                                  撤销
                                </button>
                              )}
                            </div>
                          )
                        })}
                      </div>

                      {/* 授予/修改 capability 表单 */}
                      <div className="cap-grant-form">
                        <div className="form-grid">
                          <div className="form-row">
                            <label className="form-label">权限类型</label>
                            <select
                              className="select"
                              value={capGrantCapability}
                              onChange={(e) => setCapGrantCapability(e.target.value as typeof capGrantCapability)}
                            >
                              {CAPABILITY_KEYS.map((cap) => (
                                <option key={cap} value={cap}>
                                  {CAPABILITY_LABELS[cap]}（{cap}）
                                </option>
                              ))}
                            </select>
                          </div>
                          <div className="form-row">
                            <label className="form-label">有效期周期（每周期30天）</label>
                            <input
                              className="input"
                              type="number"
                              min={1}
                              max={36}
                              value={capGrantMonths}
                              onChange={(e) => {
                                const v = Number(e.target.value)
                                if (Number.isFinite(v)) {
                                  setCapGrantMonths(Math.min(36, Math.max(1, Math.trunc(v))))
                                }
                              }}
                            />
                          </div>
                          {capGrantCapability === 'self_selection' && (
                            <div className="form-row">
                              <label className="form-label">
                                自选数量上限 <span className="required-mark">*</span>
                              </label>
                              <input
                                className="input"
                                type="number"
                                min={1}
                                max={500}
                                value={capGrantWatchlistLimit}
                                onChange={(e) => {
                                  const v = Number(e.target.value)
                                  if (Number.isFinite(v)) {
                                    setCapGrantWatchlistLimit(Math.min(500, Math.max(1, Math.trunc(v))))
                                  }
                                }}
                              />
                            </div>
                          )}
                        </div>
                        <button
                          className="btn primary"
                          onClick={handleGrantCapability}
                          disabled={grantCapabilityMut.isPending}
                        >
                          {grantCapabilityMut.isPending ? '处理中...' : '授予/续期'}
                        </button>
                      </div>

                      <div className="notice drawer-notice">
                        已有该 capability 时取较晚的 expires_at（不降权），并更新 watchlist_limit（如提供）。
                        旧 plan_code fallback 仅兼容无 cap 行用户，不覆盖已有独立授权。
                      </div>
                    </>
                  )}
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
                  {auditLogsQuery.isLoading && (
                    <div className="empty">加载审计记录中…</div>
                  )}
                  {!auditLogsQuery.isLoading && auditLogsQuery.isError && (
                    <div className="empty">
                      审计记录加载失败：
                      {(auditLogsQuery.error as Error)?.message ?? '未知错误'}
                    </div>
                  )}
                  {!auditLogsQuery.isLoading &&
                    !auditLogsQuery.isError &&
                    (auditLogsQuery.data?.items ?? []).length === 0 && (
                      <div className="empty">暂无审计记录</div>
                  )}
                  {!auditLogsQuery.isLoading && !auditLogsQuery.isError && (
                    <div className="timeline-simple">
                      {(auditLogsQuery.data?.items ?? []).map((log: AuditLogListItem) => (
                        <div key={log.id}>
                          <i />
                          <span>
                            <b>
                              {formatDateTime(log.created_at)} · {log.action}
                            </b>
                            <small>操作者 {log.actor_user_id}</small>
                          </span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>

            <div className="drawer-foot">
              <button className="btn" onClick={handleCloseDrawer}>
                关闭
              </button>
              <button className="btn primary" onClick={handleSaveProfile}>
                保存
              </button>
            </div>
          </aside>
        </div>
      )}

      {/* 生成邀请码弹窗 generateInviteModal - [Gate2 PRD60 PA-20] 三勾选模式 */}
      {modalOpen && (
        <div className="modal-backdrop open" onClick={handleCloseModal}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-head">
              <div>
                <b>生成邀请码</b>
                <div className="card-sub">
                  勾选权限组合与有效期，兑换后按 capability 独立授权（PA-20）
                </div>
              </div>
              <button className="icon-btn" onClick={handleCloseModal}>
                ×
              </button>
            </div>

            <div className="modal-body">
              <div className="form-grid">
                {/* [Gate2] 取消"套餐类型"主入口，改为三勾选 capability */}
                <div className="form-row full">
                  <label className="form-label">权限组合（至少选择一项）</label>
                  <div className="capability-checkbox-group">
                    <label className="capability-checkbox-item">
                      <input
                        type="checkbox"
                        checked={capSelfSelection}
                        onChange={(e) => setCapSelfSelection(e.target.checked)}
                      />
                      <span>
                        <b>{CAPABILITY_LABELS.self_selection}</b>
                        <small>{CAPABILITY_DESCRIPTIONS.self_selection}</small>
                      </span>
                    </label>
                    <label className="capability-checkbox-item">
                      <input
                        type="checkbox"
                        checked={capMarketData}
                        onChange={(e) => setCapMarketData(e.target.checked)}
                      />
                      <span>
                        <b>{CAPABILITY_LABELS.market_data}</b>
                        <small>{CAPABILITY_DESCRIPTIONS.market_data}</small>
                      </span>
                    </label>
                    <label className="capability-checkbox-item">
                      <input
                        type="checkbox"
                        checked={capResearchReplay}
                        onChange={(e) => setCapResearchReplay(e.target.checked)}
                      />
                      <span>
                        <b>{CAPABILITY_LABELS.research_replay}</b>
                        <small>{CAPABILITY_DESCRIPTIONS.research_replay}</small>
                      </span>
                    </label>
                  </div>
                </div>
                {/* self_selection 选中时必填 watchlist_limit（管理员自由输入，1-500） */}
                {capSelfSelection && (
                  <div className="form-row">
                    <label className="form-label">
                      自选数量上限 <span className="required-mark">*</span>
                    </label>
                    <input
                      className="input"
                      type="number"
                      min={1}
                      max={500}
                      value={capWatchlistLimit}
                      onChange={(e) => {
                        const v = Number(e.target.value)
                        if (Number.isFinite(v)) {
                          setCapWatchlistLimit(Math.min(500, Math.max(1, Math.trunc(v))))
                        }
                      }}
                    />
                    <small className="form-hint">PA-02：self_selection 必填（1-500）</small>
                  </div>
                )}
                <div className="form-row">
                  <label className="form-label">有效期周期（每周期30天）</label>
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
                  <small className="form-hint">PA-03：1周期=30天，按N×30天计算</small>
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
                邀请码不绑定具体用户。兑换成功后按勾选的 capability 独立授权（per-capability 独立 expires_at）。旧 plan_code 字段保留兼容性，capabilities 优先。
              </div>

              {/* 生成后显示新码 */}
              {generatedCodes.length > 0 && (
                <div className="generated-invite-list">
                  <div className="generated-invite-title">新邀请码</div>
                  {generatedCodes.map((code) => {
                    // [CHANGE-20260802-002] 展示后端实际回显的权限组合，而非前端勾选状态，
                    // 后端漏传时显示明确提示而不是静默隐藏。
                    const capText = formatCapabilityGrants(code.capabilities)
                    return (
                      <div key={code.id} className="generated-invite-box">
                        <b>{code.code}</b>
                        <small className="generated-invite-caps">
                          {capText || '按套餐授权（未返回 capability 组合）'}
                        </small>
                        <button
                          className="btn small"
                          onClick={() => handleCopyCode(code.code)}
                        >
                          复制
                        </button>
                      </div>
                    )
                  })}
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
