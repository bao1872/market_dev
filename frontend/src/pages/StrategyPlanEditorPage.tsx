// 选股方案编辑页（受保护路由）
// 对应原型：strategy-plan-editor.html (V1.6.3)
// 用法：创建或编辑选股组合方案，支持策略组合（AND/OR）、条件筛选、股票范围、排序与每日推送配置
// 路由：/strategy-plan-editor?mode=new 创建 / /strategy-plan-editor?planId=xxx 编辑
// 依赖 hooks：useSelectionPlans / useSelectionPlan / useCreateSelectionPlan / useUpdateSelectionPlan /
//            useValidateSelectionPlan / usePreviewSelectionPlan / useCloneSelectionPlan / useStrategies
import { useState, useEffect, useRef, useMemo, useCallback } from 'react'
import type { DragEvent } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import clsx from 'clsx'
import { useToast } from '@/store/toast'
import {
  useSelectionPlans,
  useSelectionPlan,
  useCreateSelectionPlan,
  useUpdateSelectionPlan,
  useValidateSelectionPlan,
  usePreviewSelectionPlan,
  useCloneSelectionPlan,
  useStrategies,
} from '@/hooks/useApi'
import type {
  SelectionPlanDetail,
  SelectionPlanMember,
  SelectionMemberCondition,
  SelectionPlanCreateRequest,
  SelectionPlanUpdateRequest,
  MemberSpec,
  ConditionSpec,
  Strategy,
  SelectionPlanPreviewResponse,
} from '@/api/endpoints'

// ===== 类型定义 =====

/** 条件表单状态（编辑态，value 统一为字符串便于输入） */
interface ConditionFormState {
  tempId: string
  metricKey: string
  operator: string
  value: string
  value2: string | null
}

/** 成员表单状态（编辑态） */
interface MemberFormState {
  tempId: string
  strategyDefinitionId: string
  strategyKey: string
  versionPolicy: string
  strategyVersion: string | null
  enabled: boolean
  params: Record<string, unknown>
  conditions: ConditionFormState[]
  expanded: boolean
}

/** 股票范围表单状态 */
interface UniverseFormState {
  market: string
  minListingDays: number
  excludeSt: boolean
  excludeSuspended: boolean
}

/** 排序规格表单状态 */
interface SortSpecFormState {
  field: string
  direction: string
  pageSize: number
}

/** 每日推送表单状态 */
interface NotificationFormState {
  enabled: boolean
  content: string
  maxDisplay: number
  channel: string
}

/** 整体表单状态 */
interface FormState {
  name: string
  description: string
  status: string
  operator: string
  missingMemberPolicy: string
  universe: UniverseFormState
  sortSpec: SortSpecFormState
  notification: NotificationFormState
  members: MemberFormState[]
}

// ===== 常量 =====

/** 默认表单状态（新建方案） */
const DEFAULT_FORM_STATE: FormState = {
  name: '',
  description: '',
  status: 'active',
  operator: 'AND',
  missingMemberPolicy: 'skip',
  universe: {
    market: 'all_a',
    minListingDays: 60,
    excludeSt: true,
    excludeSuspended: true,
  },
  sortSpec: {
    field: 'combo_match_count',
    direction: 'desc',
    pageSize: 50,
  },
  notification: {
    enabled: true,
    content: 'new_with_detail',
    maxDisplay: 20,
    channel: 'in_app_feishu',
  },
  members: [],
}

/** 条件运算符选项 */
const OPERATORS = ['>=', '<=', '>', '<', '==', '!=', 'between'] as const

/** 市场范围选项 */
const MARKET_OPTIONS = [
  { value: 'all_a', label: '全部 A 股（含北交所）' },
  { value: 'sh_sz', label: '沪深主板' },
  { value: 'gem', label: '创业板' },
  { value: 'star', label: '科创板' },
  { value: 'bj', label: '北交所' },
]

/** 排序字段选项 */
const SORT_FIELD_OPTIONS = [
  { value: 'combo_match_count', label: '组合匹配策略数' },
  { value: 'dsa_vwap_avg_return', label: 'DSA · VWAP 平均收益率' },
  { value: 'breakout_amplitude', label: '突破强度 · 突破幅度' },
]

/** 排序方向选项 */
const SORT_DIRECTION_OPTIONS = [
  { value: 'desc', label: '降序' },
  { value: 'asc', label: '升序' },
]

/** 页面默认条数选项 */
const PAGE_SIZE_OPTIONS = [50, 100, 200]

/** 推送内容选项 */
const NOTIFICATION_CONTENT_OPTIONS = [
  { value: 'new_with_detail', label: '新增组合结果 + 命中明细' },
  { value: 'all', label: '全部组合结果' },
]

/** 推送渠道选项 */
const NOTIFICATION_CHANNEL_OPTIONS = [
  { value: 'in_app_feishu', label: '站内消息 + 飞书「交易提醒群」' },
  { value: 'in_app', label: '仅站内消息' },
  { value: 'feishu', label: '仅飞书推送' },
]

/** 策略可用指标映射（按 strategy_key 前缀匹配） */
const STRATEGY_METRICS: Record<string, { key: string; label: string }[]> = {
  dsa: [
    { key: 'dir_duration', label: 'dir=1 持续时间' },
    { key: 'vwap_avg_return', label: 'VWAP 平均收益率' },
    { key: 'offset_mean', label: 'offset_mean / 偏移均值' },
    { key: 'offset_std', label: 'offset_std / 偏移标准差' },
    { key: 'offset_variance_rate', label: 'offset_variance_rate / 偏移方差率' },
    { key: 'offset_percentile', label: 'offset_percentile / 偏移百分位' },
  ],
  breakout: [
    { key: 'breakout_amplitude', label: '突破幅度' },
    { key: 'volume_ratio', label: '量比' },
    { key: 'position_risk', label: '位置风险' },
  ],
  default: [
    { key: 'rank_value', label: '排名值' },
    { key: 'match_score', label: '匹配分' },
  ],
}

/** 策略描述映射（按 strategy_key 前缀匹配） */
const STRATEGY_DESCRIPTIONS: Record<string, string> = {
  dsa: '方向稳定、收益速度、偏移强度、稳定性与短期位置。',
  breakout: '结构突破、量能确认、压力距离与位置风险联合过滤。',
}

// ===== 工具函数 =====

/** 生成临时 ID（用于前端列表 key） */
function genTempId(): string {
  return `tmp_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`
}

/** 根据 strategy_key 获取可用指标列表 */
function getStrategyMetrics(strategyKey: string): { key: string; label: string }[] {
  const k = strategyKey.toLowerCase()
  if (k.includes('dsa')) return STRATEGY_METRICS.dsa
  if (k.includes('breakout')) return STRATEGY_METRICS.breakout
  return STRATEGY_METRICS.default
}

/** 根据 strategy_key 获取策略描述 */
function getStrategyDescription(strategyKey: string): string {
  const k = strategyKey.toLowerCase()
  if (k.includes('dsa')) return STRATEGY_DESCRIPTIONS.dsa
  if (k.includes('breakout')) return STRATEGY_DESCRIPTIONS.breakout
  return '选股策略'
}

/** 根据 strategy_definition_id 从策略目录中查找展示名 */
function getStrategyDisplayName(
  strategyDefinitionId: string,
  strategyMap: Map<string, Strategy>,
): string {
  const s = strategyMap.get(strategyDefinitionId)
  return s?.display_name ?? `策略 ${strategyDefinitionId.slice(0, 8)}`
}

/** 根据 strategy_definition_id 从策略目录中查找 strategy_key */
function getStrategyKey(
  strategyDefinitionId: string,
  strategyMap: Map<string, Strategy>,
): string {
  return strategyMap.get(strategyDefinitionId)?.strategy_key ?? ''
}

/** 将 API 返回的成员条件转换为表单条件状态 */
function conditionToFormState(c: SelectionMemberCondition): ConditionFormState {
  return {
    tempId: genTempId(),
    metricKey: c.metric_key,
    operator: c.operator,
    value: c.value1 === null || c.value1 === undefined ? '' : String(c.value1),
    value2: c.value2 === null || c.value2 === undefined ? null : String(c.value2),
  }
}

/** 将 API 返回的成员转换为表单成员状态 */
function memberToFormState(
  m: SelectionPlanMember,
  strategyMap: Map<string, Strategy>,
): MemberFormState {
  return {
    tempId: genTempId(),
    strategyDefinitionId: m.strategy_definition_id,
    strategyKey: getStrategyKey(m.strategy_definition_id, strategyMap),
    versionPolicy: m.version_policy,
    strategyVersion: m.strategy_version_id,
    enabled: m.enabled,
    params: m.params,
    conditions: (m.conditions ?? []).map(conditionToFormState),
    expanded: true,
  }
}

/** 将 API 返回的方案详情转换为表单状态 */
function planDetailToFormState(
  plan: SelectionPlanDetail,
  strategyMap: Map<string, Strategy>,
): FormState {
  const rev = plan.current_revision_data
  const universe = (rev?.universe ?? {}) as Record<string, unknown>
  const sortSpec = (rev?.sort_spec ?? []) as Record<string, unknown>[]
  const notification = (rev?.notification_config ?? {}) as Record<string, unknown>
  const firstSort = sortSpec[0] ?? {}

  return {
    name: plan.name,
    description: plan.description ?? '',
    status: plan.status,
    operator: rev?.operator ?? 'AND',
    missingMemberPolicy: rev?.missing_member_policy ?? 'skip',
    universe: {
      market: String(universe.market ?? 'all_a'),
      minListingDays: Number(universe.min_listing_days ?? 60),
      excludeSt: Boolean(universe.exclude_st ?? true),
      excludeSuspended: Boolean(universe.exclude_suspended ?? true),
    },
    sortSpec: {
      field: String(firstSort.field ?? 'combo_match_count'),
      direction: String(firstSort.direction ?? 'desc'),
      pageSize: Number(notification.page_size ?? 50),
    },
    notification: {
      enabled: Boolean(notification.enabled ?? true),
      content: String(notification.content ?? 'new_with_detail'),
      maxDisplay: Number(notification.max_display ?? 20),
      channel: String(notification.channel ?? 'in_app_feishu'),
    },
    members: (rev?.members ?? []).map((m) => memberToFormState(m, strategyMap)),
  }
}

/** 将表单条件状态转换为 API 条件规格 */
function formConditionToSpec(c: ConditionFormState): ConditionSpec {
  return {
    metric_key: c.metricKey,
    operator: c.operator,
    value: c.value,
    value2: c.value2 ?? undefined,
  }
}

/** 将表单成员状态转换为 API 成员规格 */
function formMemberToSpec(m: MemberFormState): MemberSpec {
  return {
    strategy_key: m.strategyKey,
    version_policy: m.versionPolicy,
    strategy_version: m.strategyVersion ?? undefined,
    params: m.params,
    conditions: m.conditions.map(formConditionToSpec),
    enabled: m.enabled,
  }
}

/** 将表单状态转换为创建请求体 */
function formStateToCreatePayload(form: FormState): SelectionPlanCreateRequest {
  return {
    name: form.name,
    description: form.description || undefined,
    operator: form.operator,
    missing_member_policy: form.missingMemberPolicy,
    universe: {
      market: form.universe.market,
      min_listing_days: form.universe.minListingDays,
      exclude_st: form.universe.excludeSt,
      exclude_suspended: form.universe.excludeSuspended,
    },
    sort_spec: [
      {
        field: form.sortSpec.field,
        direction: form.sortSpec.direction,
      },
    ],
    notification: {
      enabled: form.notification.enabled,
      content: form.notification.content,
      max_display: form.notification.maxDisplay,
      channel: form.notification.channel,
      page_size: form.sortSpec.pageSize,
    },
    members: form.members.map(formMemberToSpec),
  }
}

/** 将表单状态转换为更新请求体 */
function formStateToUpdatePayload(form: FormState): SelectionPlanUpdateRequest {
  return formStateToCreatePayload(form)
}

// ===== 主组件 =====
export default function StrategyPlanEditorPage() {
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const toast = useToast.getState()

  // --- URL 参数解析 ---
  const mode = searchParams.get('mode') // 'new' 表示新建
  const planIdParam = searchParams.get('planId')
  const isNewMode = mode === 'new'

  // --- 数据查询 ---
  const plansQuery = useSelectionPlans()
  const plans = plansQuery.data?.items ?? []

  const strategiesQuery = useStrategies('selector')
  const strategies = strategiesQuery.data?.items ?? []

  // 策略目录映射：strategy_definition_id -> Strategy
  const strategyMap = useMemo(() => {
    const m = new Map<string, Strategy>()
    for (const s of strategies) {
      m.set(s.id, s)
    }
    return m
  }, [strategies])

  // 当前编辑的方案 ID：优先 URL 参数，否则取列表第一个
  const [currentPlanId, setCurrentPlanId] = useState<string>(planIdParam ?? '')
  const effectivePlanId = isNewMode ? '' : (currentPlanId || plans[0]?.id || '')
  const planDetailQuery = useSelectionPlan(effectivePlanId || undefined)
  const planDetail = planDetailQuery.data

  // --- 变更操作 ---
  const createMutation = useCreateSelectionPlan()
  const updateMutation = useUpdateSelectionPlan()
  const validateMutation = useValidateSelectionPlan()
  const previewMutation = usePreviewSelectionPlan()
  const cloneMutation = useCloneSelectionPlan()

  // --- 表单状态 ---
  const [form, setForm] = useState<FormState>(DEFAULT_FORM_STATE)
  const [dirty, setDirty] = useState(false)
  const [previewData, setPreviewData] = useState<SelectionPlanPreviewResponse | null>(null)
  const [showStrategyModal, setShowStrategyModal] = useState(false)
  const [pendingStrategyKeys, setPendingStrategyKeys] = useState<Set<string>>(new Set())

  // 防止重复初始化覆盖用户编辑
  const initializedForPlanId = useRef<string>('')

  // --- 初始化表单：当方案详情加载后填充表单 ---
  useEffect(() => {
    if (isNewMode) {
      // 新建模式：重置为默认状态
      if (initializedForPlanId.current !== '__new__') {
        setForm({ ...DEFAULT_FORM_STATE, members: [] })
        setDirty(false)
        setPreviewData(null)
        initializedForPlanId.current = '__new__'
      }
      return
    }
    if (!planDetail) return
    // 编辑模式：方案 ID 变化时重新初始化
    if (initializedForPlanId.current !== planDetail.id) {
      setForm(planDetailToFormState(planDetail, strategyMap))
      setDirty(false)
      setPreviewData(null)
      initializedForPlanId.current = planDetail.id
    }
  }, [planDetail, isNewMode, strategyMap])

  // --- beforeunload 脏数据保护 ---
  useEffect(() => {
    if (!dirty) return
    const handler = (e: BeforeUnloadEvent) => {
      e.preventDefault()
      e.returnValue = ''
    }
    window.addEventListener('beforeunload', handler)
    return () => window.removeEventListener('beforeunload', handler)
  }, [dirty])

  // ===== 表单更新回调 =====

  /** 通用字段更新（标记 dirty） */
  const updateField = useCallback(<K extends keyof FormState>(key: K, value: FormState[K]) => {
    setForm((prev) => ({ ...prev, [key]: value }))
    setDirty(true)
  }, [])

  /** 更新股票范围字段 */
  const updateUniverse = useCallback(<K extends keyof UniverseFormState>(key: K, value: UniverseFormState[K]) => {
    setForm((prev) => ({ ...prev, universe: { ...prev.universe, [key]: value } }))
    setDirty(true)
  }, [])

  /** 更新排序规格字段 */
  const updateSortSpec = useCallback(<K extends keyof SortSpecFormState>(key: K, value: SortSpecFormState[K]) => {
    setForm((prev) => ({ ...prev, sortSpec: { ...prev.sortSpec, [key]: value } }))
    setDirty(true)
  }, [])

  /** 更新推送配置字段 */
  const updateNotification = useCallback(
    <K extends keyof NotificationFormState>(key: K, value: NotificationFormState[K]) => {
      setForm((prev) => ({ ...prev, notification: { ...prev.notification, [key]: value } }))
      setDirty(true)
    },
    [],
  )

  /** 更新成员字段 */
  const updateMember = useCallback(
    (tempId: string, patch: Partial<MemberFormState>) => {
      setForm((prev) => ({
        ...prev,
        members: prev.members.map((m) => (m.tempId === tempId ? { ...m, ...patch } : m)),
      }))
      setDirty(true)
    },
    [],
  )

  /** 添加成员（从策略目录选择） */
  const addMembers = useCallback(
    (strategyIds: string[]) => {
      const newMembers: MemberFormState[] = strategyIds
        .filter((id) => !form.members.some((m) => m.strategyDefinitionId === id))
        .map((id) => {
          const strategyKey = getStrategyKey(id, strategyMap)
          return {
            tempId: genTempId(),
            strategyDefinitionId: id,
            strategyKey,
            versionPolicy: 'pinned',
            strategyVersion: null,
            enabled: true,
            params: {},
            conditions: [],
            expanded: true,
          }
        })
      if (newMembers.length === 0) return
      setForm((prev) => ({ ...prev, members: [...prev.members, ...newMembers] }))
      setDirty(true)
      toast.show('已加入选股策略', `新增 ${newMembers.length} 个策略到组合`)
    },
    [form.members, strategyMap, toast],
  )

  /** 移除成员 */
  const removeMember = useCallback(
    (tempId: string) => {
      setForm((prev) => ({
        ...prev,
        members: prev.members.filter((m) => m.tempId !== tempId),
      }))
      setDirty(true)
      toast.show('已从组合移除策略')
    },
    [toast],
  )

  /** 拖拽重排成员 */
  const reorderMembers = useCallback((fromIndex: number, toIndex: number) => {
    setForm((prev) => {
      const next = [...prev.members]
      const [moved] = next.splice(fromIndex, 1)
      next.splice(toIndex, 0, moved)
      return { ...prev, members: next }
    })
    setDirty(true)
  }, [])

  /** 添加条件到指定成员 */
  const addCondition = useCallback((memberTempId: string) => {
    setForm((prev) => ({
      ...prev,
      members: prev.members.map((m) =>
        m.tempId === memberTempId
          ? {
              ...m,
              conditions: [
                ...m.conditions,
                {
                  tempId: genTempId(),
                  metricKey: getStrategyMetrics(m.strategyKey)[0]?.key ?? '',
                  operator: '>=',
                  value: '',
                  value2: null,
                },
              ],
            }
          : m,
      ),
    }))
    setDirty(true)
  }, [])

  /** 移除条件 */
  const removeCondition = useCallback((memberTempId: string, conditionTempId: string) => {
    setForm((prev) => ({
      ...prev,
      members: prev.members.map((m) =>
        m.tempId === memberTempId
          ? { ...m, conditions: m.conditions.filter((c) => c.tempId !== conditionTempId) }
          : m,
      ),
    }))
    setDirty(true)
  }, [])

  /** 更新条件字段 */
  const updateCondition = useCallback(
    (
      memberTempId: string,
      conditionTempId: string,
      patch: Partial<ConditionFormState>,
    ) => {
      setForm((prev) => ({
        ...prev,
        members: prev.members.map((m) =>
          m.tempId === memberTempId
            ? {
                ...m,
                conditions: m.conditions.map((c) =>
                  c.tempId === conditionTempId ? { ...c, ...patch } : c,
                ),
              }
            : m,
        ),
      }))
      setDirty(true)
    },
    [],
  )

  // ===== 事件处理 =====

  /** 保存方案 */
  const handleSave = async () => {
    if (!form.name.trim()) {
      toast.show('保存失败', '请填写方案名称')
      return
    }
    if (form.members.length === 0) {
      toast.show('保存失败', '请至少添加一个选股策略')
      return
    }
    try {
      const payload = formStateToCreatePayload(form)
      if (isNewMode || !effectivePlanId) {
        // 新建
        const created = await createMutation.mutateAsync(payload)
        toast.show('已生成新方案版本', '旧版本保持不可变')
        setDirty(false)
        // 跳转到编辑模式
        navigate(`/strategy-plan-editor?planId=${created.id}`)
      } else {
        // 更新（创建新 revision）
        await updateMutation.mutateAsync({ planId: effectivePlanId, payload: formStateToUpdatePayload(form) })
        toast.show('已生成新方案版本', '旧版本保持不可变')
        setDirty(false)
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : '未知错误'
      toast.show('保存失败', msg)
    }
  }

  /** 另存为新方案（克隆当前方案） */
  const handleSaveAs = async () => {
    if (!effectivePlanId) {
      toast.show('另存为失败', '当前为新建方案，请直接保存')
      return
    }
    if (!form.name.trim()) {
      toast.show('另存为失败', '请填写方案名称')
      return
    }
    try {
      await cloneMutation.mutateAsync({
        planId: effectivePlanId,
        payload: { name: `${form.name} 副本`, description: form.description || undefined },
      })
      toast.show('已另存为新方案')
      setDirty(false)
    } catch (err) {
      const msg = err instanceof Error ? err.message : '未知错误'
      toast.show('另存为失败', msg)
    }
  }

  /** 刷新预览 */
  const handleRefreshPreview = async () => {
    if (!effectivePlanId) {
      toast.show('预览失败', '请先保存方案')
      return
    }
    try {
      const today = new Date().toISOString().slice(0, 10)
      const result = await previewMutation.mutateAsync({
        planId: effectivePlanId,
        payload: { trade_date: today },
      })
      setPreviewData(result)
      toast.show('组合预览已刷新')
    } catch (err) {
      const msg = err instanceof Error ? err.message : '未知错误'
      toast.show('预览失败', msg)
    }
  }

  /** 验证方案 */
  const handleValidate = async () => {
    if (!effectivePlanId) {
      toast.show('验证失败', '请先保存方案')
      return
    }
    try {
      const result = await validateMutation.mutateAsync(effectivePlanId)
      if (result.valid) {
        toast.show('验证通过', '方案配置符合规则')
      } else {
        const errs = result.errors.map((e) => JSON.stringify(e)).join('; ')
        toast.show('验证未通过', errs || '请检查方案配置')
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : '未知错误'
      toast.show('验证失败', msg)
    }
  }

  /** 添加策略模态框：确认加入 */
  const handleConfirmAddStrategies = () => {
    const selectedIds = strategies
      .filter((s) => pendingStrategyKeys.has(s.strategy_key))
      .map((s) => s.id)
    if (selectedIds.length === 0) {
      setShowStrategyModal(false)
      return
    }
    addMembers(selectedIds)
    setPendingStrategyKeys(new Set())
    setShowStrategyModal(false)
  }

  /** 取消编辑 */
  const handleCancel = () => {
    navigate('/screener')
  }

  /** 切换编辑的方案 */
  const handlePlanSwitch = (id: string) => {
    if (dirty) {
      if (!window.confirm('当前方案有未保存的修改，确认切换？')) return
    }
    setCurrentPlanId(id)
    navigate(`/strategy-plan-editor?planId=${id}`)
  }

  // ===== 拖拽处理 =====

  const dragIndex = useRef<number>(-1)

  const handleDragStart = (index: number) => (e: DragEvent<HTMLDivElement>) => {
    dragIndex.current = index
    e.dataTransfer.effectAllowed = 'move'
  }

  const handleDragOver = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault()
    e.dataTransfer.dropEffect = 'move'
  }

  const handleDrop = (index: number) => (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault()
    const from = dragIndex.current
    if (from < 0 || from === index) return
    reorderMembers(from, index)
    dragIndex.current = -1
  }

  // ===== 派生数据 =====

  /** 当前方案名标题 */
  const pageTitle = isNewMode
    ? '新建选股组合方案'
    : planDetail
      ? `编辑选股组合方案：${planDetail.name}`
      : '编辑选股组合方案'

  /** 预览统计：成员命中数 */
  const memberHitStats = previewData?.member_hit_stats ?? {}
  const previewTotal = previewData?.total ?? 0
  const previewSample = previewData?.sample ?? []

  /** 预览：占全市场比例（按 5000 只估算） */
  const marketPercent = previewTotal > 0 ? ((previewTotal / 5000) * 100).toFixed(2) : '0.00'

  /** 保存中状态 */
  const saving = createMutation.isPending || updateMutation.isPending

  // ===== 渲染 =====

  return (
    <div>
      {/* 页面头 */}
      <div className="page-head">
        <div>
          <div className="page-desc">
            <Link to="/screener" className="link-btn">
              选股策略
            </Link>{' '}
            / 组合方案编辑
          </div>
          <h1 className="page-title section-gap">{pageTitle}</h1>
          <div className="page-desc">
            一个方案可以组合多个选股策略；每个策略保留自己的参数与结果字段
          </div>
        </div>
        <div className="actions">
          {/* 方案切换下拉（编辑模式） */}
          {!isNewMode && plans.length > 0 && (
            <select
              className="select"
              value={effectivePlanId}
              onChange={(e) => handlePlanSwitch(e.target.value)}
            >
              {plans.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name} · {p.current_revision} 版
                </option>
              ))}
            </select>
          )}
          <button className="btn" onClick={handleCancel}>
            取消
          </button>
          {!isNewMode && (
            <button
              className="btn"
              onClick={handleSaveAs}
              disabled={cloneMutation.isPending}
            >
              {cloneMutation.isPending ? '另存中…' : '另存为'}
            </button>
          )}
          <button className="btn primary" onClick={handleSave} disabled={saving}>
            {saving ? '保存中…' : '保存方案'}
          </button>
        </div>
      </div>

      {/* 编辑器布局 */}
      <div className="editor-layout">
        {/* 左侧编辑区 */}
        <section className="card">
          {/* 基本信息 */}
          <div className="editor-section">
            <div className="editor-title">基本信息</div>
            <div className="editor-desc">方案名称用于结果页、推送记录和方案切换。</div>
            <div className="form-grid">
              <div className="form-row">
                <label className="form-label">方案名称</label>
                <input
                  className="input"
                  value={form.name}
                  onChange={(e) => updateField('name', e.target.value)}
                  placeholder="例如：强势共振"
                />
              </div>
              <div className="form-row">
                <label className="form-label">方案状态</label>
                <select
                  className="select"
                  value={form.status}
                  onChange={(e) => updateField('status', e.target.value)}
                >
                  <option value="active">启用</option>
                  <option value="paused">停用</option>
                </select>
              </div>
              <div className="form-row full">
                <label className="form-label">方案说明</label>
                <input
                  className="input"
                  value={form.description}
                  onChange={(e) => updateField('description', e.target.value)}
                  placeholder="简要描述方案目标"
                />
              </div>
            </div>
          </div>

          {/* 策略组合关系 */}
          <div className="editor-section">
            <div className="editor-title">策略组合关系</div>
            <div className="editor-desc">
              控制不同策略之间如何合并。单个策略内部的筛选条件仍按该策略自己的规则执行。
            </div>
            <div className="combo-mode-grid">
              <label
                className={clsx('combo-mode-card', form.operator === 'AND' && 'active')}
              >
                <input
                  type="radio"
                  name="selectorCombo"
                  checked={form.operator === 'AND'}
                  onChange={() => updateField('operator', 'AND')}
                />
                <b>全部策略满足（AND）</b>
                <span>股票必须同时满足方案中的所有策略。</span>
              </label>
              <label
                className={clsx('combo-mode-card', form.operator === 'OR' && 'active')}
              >
                <input
                  type="radio"
                  name="selectorCombo"
                  checked={form.operator === 'OR'}
                  onChange={() => updateField('operator', 'OR')}
                />
                <b>任一策略满足（OR）</b>
                <span>满足任一策略即可进入组合结果。</span>
              </label>
            </div>
          </div>

          {/* 组合中的选股策略 */}
          <div className="editor-section">
            <div className="section-head-inline">
              <div>
                <div className="editor-title">组合中的选股策略</div>
                <div className="editor-desc">
                  拖动排序用于结果解释与推送展示；策略计算仍由后台共享执行。
                </div>
              </div>
              <button
                className="btn small primary"
                onClick={() => {
                  setPendingStrategyKeys(new Set())
                  setShowStrategyModal(true)
                }}
              >
                ＋ 添加选股策略
              </button>
            </div>
            <div className="strategy-composer">
              {form.members.length === 0 ? (
                <div className="empty">
                  <h3>暂未添加选股策略</h3>
                  <p>点击右上角"添加选股策略"开始组合。</p>
                </div>
              ) : (
                form.members.map((m, idx) => {
                  const displayName = getStrategyDisplayName(m.strategyDefinitionId, strategyMap)
                  const description = getStrategyDescription(m.strategyKey)
                  const metrics = getStrategyMetrics(m.strategyKey)
                  return (
                    <div key={m.tempId}>
                      <article
                        className="strategy-composer-card"
                        draggable
                        onDragStart={handleDragStart(idx)}
                        onDragOver={handleDragOver}
                        onDrop={handleDrop(idx)}
                      >
                        <div className="composer-handle">⋮⋮</div>
                        <div className="composer-main">
                          <div className="composer-head">
                            <div>
                              <span className="strategy-type-pill">
                                SELECTOR {String(idx + 1).padStart(2, '0')}
                              </span>
                              <h3>
                                {displayName}{' '}
                                {m.strategyVersion && (
                                  <span className="tag info">{m.strategyVersion}</span>
                                )}
                              </h3>
                              <p>{description}</p>
                            </div>
                            <div className="actions">
                              <button
                                className="btn small"
                                onClick={() => updateMember(m.tempId, { expanded: !m.expanded })}
                              >
                                {m.expanded ? '收起参数' : '展开参数'}
                              </button>
                              <button
                                className="icon-btn composer-remove"
                                title="从组合移除"
                                onClick={() => removeMember(m.tempId)}
                              >
                                ×
                              </button>
                            </div>
                          </div>
                          {m.expanded && (
                            <div className="composer-config">
                              <div className="condition-table">
                                {m.conditions.length === 0 ? (
                                  <div className="condition-empty-hint">
                                    暂无条件，策略将按默认规则执行
                                  </div>
                                ) : (
                                  m.conditions.map((c, ci) => (
                                    <div className="condition-line" key={c.tempId}>
                                      <div className="condition-index">{ci + 1}</div>
                                      <select
                                        className="select"
                                        value={c.metricKey}
                                        onChange={(e) =>
                                          updateCondition(m.tempId, c.tempId, {
                                            metricKey: e.target.value,
                                          })
                                        }
                                      >
                                        {metrics.map((metric) => (
                                          <option key={metric.key} value={metric.key}>
                                            {metric.label}
                                          </option>
                                        ))}
                                      </select>
                                      <select
                                        className="select"
                                        value={c.operator}
                                        onChange={(e) =>
                                          updateCondition(m.tempId, c.tempId, {
                                            operator: e.target.value,
                                          })
                                        }
                                      >
                                        {OPERATORS.map((op) => (
                                          <option key={op} value={op}>
                                            {op}
                                          </option>
                                        ))}
                                      </select>
                                      <input
                                        className="input condition-value"
                                        value={c.value}
                                        onChange={(e) =>
                                          updateCondition(m.tempId, c.tempId, {
                                            value: e.target.value,
                                          })
                                        }
                                        placeholder="阈值"
                                      />
                                      <button
                                        className="icon-btn remove-condition"
                                        title="移除条件"
                                        onClick={() => removeCondition(m.tempId, c.tempId)}
                                      >
                                        ×
                                      </button>
                                    </div>
                                  ))
                                )}
                              </div>
                              <button
                                className="btn small section-gap"
                                onClick={() => addCondition(m.tempId)}
                              >
                                ＋ 添加{displayName}条件
                              </button>
                            </div>
                          )}
                        </div>
                      </article>
                      {/* 组合连接符（非最后一个成员） */}
                      {idx < form.members.length - 1 && (
                        <div className="combo-connector">
                          <span>{form.operator}</span>
                        </div>
                      )}
                    </div>
                  )
                })
              )}
            </div>
          </div>

          {/* 股票范围 */}
          <div className="editor-section">
            <div className="editor-title">股票范围</div>
            <div className="editor-desc">组合方案中的所有策略使用同一股票范围。</div>
            <div className="form-grid">
              <div className="form-row">
                <label className="form-label">市场范围</label>
                <select
                  className="select"
                  value={form.universe.market}
                  onChange={(e) => updateUniverse('market', e.target.value)}
                >
                  {MARKET_OPTIONS.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </select>
              </div>
              <div className="form-row">
                <label className="form-label">最低上市天数</label>
                <input
                  className="input"
                  type="number"
                  value={form.universe.minListingDays}
                  onChange={(e) => updateUniverse('minListingDays', Number(e.target.value))}
                />
              </div>
            </div>
            <div className="toggle-row">
              <div>
                <b>排除 ST / 退市整理</b>
              </div>
              <button
                type="button"
                className={clsx('switch', form.universe.excludeSt && 'on')}
                onClick={() => updateUniverse('excludeSt', !form.universe.excludeSt)}
                aria-label="切换排除 ST"
              />
            </div>
            <div className="toggle-row">
              <div>
                <b>排除停牌股票</b>
              </div>
              <button
                type="button"
                className={clsx('switch', form.universe.excludeSuspended && 'on')}
                onClick={() =>
                  updateUniverse('excludeSuspended', !form.universe.excludeSuspended)
                }
                aria-label="切换排除停牌"
              />
            </div>
          </div>

          {/* 组合结果排序与展示 */}
          <div className="editor-section">
            <div className="editor-title">组合结果排序与展示</div>
            <div className="form-grid">
              <div className="form-row">
                <label className="form-label">主排序字段</label>
                <select
                  className="select"
                  value={form.sortSpec.field}
                  onChange={(e) => updateSortSpec('field', e.target.value)}
                >
                  {SORT_FIELD_OPTIONS.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </select>
              </div>
              <div className="form-row">
                <label className="form-label">方向</label>
                <select
                  className="select"
                  value={form.sortSpec.direction}
                  onChange={(e) => updateSortSpec('direction', e.target.value)}
                >
                  {SORT_DIRECTION_OPTIONS.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </select>
              </div>
              <div className="form-row">
                <label className="form-label">页面默认条数</label>
                <select
                  className="select"
                  value={form.sortSpec.pageSize}
                  onChange={(e) => updateSortSpec('pageSize', Number(e.target.value))}
                >
                  {PAGE_SIZE_OPTIONS.map((n) => (
                    <option key={n} value={n}>
                      {n}
                    </option>
                  ))}
                </select>
              </div>
            </div>
          </div>

          {/* 每日推送 */}
          <div className="editor-section">
            <div className="editor-title">每日推送</div>
            <div className="editor-desc">
              推送发送最终组合结果，并可附带每只股票的策略命中明细。
            </div>
            <div className="toggle-row">
              <div>
                <b>启用该组合方案推送</b>
              </div>
              <button
                type="button"
                className={clsx('switch', form.notification.enabled && 'on')}
                onClick={() =>
                  updateNotification('enabled', !form.notification.enabled)
                }
                aria-label="切换启用推送"
              />
            </div>
            <div className="form-grid form-grid-gap">
              <div className="form-row">
                <label className="form-label">推送内容</label>
                <select
                  className="select"
                  value={form.notification.content}
                  onChange={(e) => updateNotification('content', e.target.value)}
                >
                  {NOTIFICATION_CONTENT_OPTIONS.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </select>
              </div>
              <div className="form-row">
                <label className="form-label">最多展示</label>
                <input
                  className="input"
                  type="number"
                  value={form.notification.maxDisplay}
                  onChange={(e) =>
                    updateNotification('maxDisplay', Number(e.target.value))
                  }
                />
              </div>
              <div className="form-row full">
                <label className="form-label">通知渠道</label>
                <select
                  className="select"
                  value={form.notification.channel}
                  onChange={(e) => updateNotification('channel', e.target.value)}
                >
                  {NOTIFICATION_CHANNEL_OPTIONS.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </select>
              </div>
            </div>
          </div>
        </section>

        {/* 右侧粘性预览区 */}
        <aside className="stack">
          <div className="card sticky-summary">
            <div className="card-head">
              <div>
                <div className="card-title">组合预览</div>
                <div className="card-sub">
                  使用 {new Date().toISOString().slice(0, 10)} 数据即时估算
                </div>
              </div>
              <button
                className="btn small"
                onClick={handleRefreshPreview}
                disabled={previewMutation.isPending || !effectivePlanId}
              >
                {previewMutation.isPending ? '刷新中…' : '刷新'}
              </button>
            </div>
            <div className="card-body">
              {/* 成员命中统计 */}
              {form.members.length === 0 ? (
                <div className="empty">
                  <p>请先添加选股策略</p>
                </div>
              ) : (
                <>
                  {form.members.map((m) => {
                    const displayName = getStrategyDisplayName(
                      m.strategyDefinitionId,
                      strategyMap,
                    )
                    const hit = memberHitStats[m.tempId] ?? memberHitStats[m.strategyDefinitionId] ?? 0
                    return (
                      <div className="summary-row" key={m.tempId}>
                        <span>{displayName}命中</span>
                        <b className="num">{hit}</b>
                      </div>
                    )
                  })}
                  <div className="summary-row">
                    <span>最终{form.operator === 'AND' ? '交集' : '并集'}</span>
                    <b className="num pos">{previewTotal}</b>
                  </div>
                  <div className="summary-row">
                    <span>占全市场</span>
                    <b className="num">{marketPercent}%</b>
                  </div>
                  <div className="summary-row">
                    <span>组合关系</span>
                    <span className="status-pill ok">{form.operator}</span>
                  </div>
                </>
              )}

              {/* 结果样例 */}
              {previewSample.length > 0 && (
                <>
                  <div className="card-title preview-sample-title">结果样例</div>
                  <div className="preview-list">
                    {previewSample.slice(0, 5).map((r) => {
                      const summary = r.summary as Record<string, unknown>
                      const name = String(summary.name ?? summary.instrument_name ?? r.instrument_id.slice(0, 8))
                      return (
                        <div className="preview-stock" key={r.id}>
                          <span>{name}</span>
                          <span>
                            {form.members
                              .filter(
                                (m) =>
                                  r.matched_member_ids.includes(m.tempId) ||
                                  r.matched_member_ids.includes(m.strategyDefinitionId),
                              )
                              .map((m) => {
                                const displayName = getStrategyDisplayName(
                                  m.strategyDefinitionId,
                                  strategyMap,
                                )
                                const short = displayName.slice(0, 4)
                                return (
                                  <i className="mini-check" key={m.tempId}>
                                    {short}
                                  </i>
                                )
                              })}
                          </span>
                        </div>
                      )
                    })}
                  </div>
                </>
              )}

              <div className="notice preview-notice">
                组合方案只保存策略引用、版本与用户参数，不复制底层计算结果。
              </div>

              {/* 验证按钮 */}
              <button
                className="btn small section-gap"
                onClick={handleValidate}
                disabled={validateMutation.isPending || !effectivePlanId}
              >
                {validateMutation.isPending ? '验证中…' : '验证方案配置'}
              </button>
            </div>
          </div>
        </aside>
      </div>

      {/* 添加选股策略模态框 */}
      {showStrategyModal && (
        <div className="modal-backdrop open">
          <div className="modal">
            <div className="modal-head">
              <div>
                <b>添加选股策略</b>
                <div className="card-sub">仅显示管理员已开放且与当前市场兼容的策略</div>
              </div>
              <button
                className="icon-btn"
                onClick={() => setShowStrategyModal(false)}
                title="关闭"
              >
                ×
              </button>
            </div>
            <div className="modal-body">
              {strategiesQuery.isLoading ? (
                <div className="empty">加载策略目录中…</div>
              ) : strategies.length === 0 ? (
                <div className="empty">
                  <p>暂无可用选股策略</p>
                </div>
              ) : (
                <div className="strategy-picker-list">
                  {strategies.map((s) => {
                    const inCombo = form.members.some(
                      (m) => m.strategyDefinitionId === s.id,
                    )
                    const checked = inCombo || pendingStrategyKeys.has(s.strategy_key)
                    return (
                      <label key={s.id} className="strategy-picker-item">
                        <input
                          type="checkbox"
                          checked={checked}
                          disabled={inCombo}
                          onChange={(e) => {
                            setPendingStrategyKeys((prev) => {
                              const next = new Set(prev)
                              if (e.target.checked) {
                                next.add(s.strategy_key)
                              } else {
                                next.delete(s.strategy_key)
                              }
                              return next
                            })
                          }}
                        />
                        <div>
                          <b>{s.display_name}</b>
                          <span>
                            {inCombo
                              ? `已在组合中 · ${s.strategy_key}`
                              : `可选 · ${s.strategy_key}`}
                          </span>
                        </div>
                      </label>
                    )
                  })}
                </div>
              )}
            </div>
            <div className="modal-foot">
              <button className="btn" onClick={() => setShowStrategyModal(false)}>
                取消
              </button>
              <button className="btn primary" onClick={handleConfirmAddStrategies}>
                加入组合
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
