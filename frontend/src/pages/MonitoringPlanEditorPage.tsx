// 监控方案编辑页（受保护路由）
// 对应原型：monitoring-plan-editor.html (V1.6.3)
//
// 用法：路由 /monitoring-plan-editor?planId=<uuid>
//   - 携带 planId：加载并编辑已有监控方案
//   - 不携带 planId：新建监控方案
//
// 与选股方案编辑器的区别：
//   - 触发模式（独立通知 / ALL 全部确认 / ANY 任一触发）
//   - 成员角色（TRIGGER 触发 / CONFIRM 确认）
//   - 时间窗口（组合确认窗口）+ 冷却 + 失效策略
//   - 事件顺序（ordered）约束

import { useEffect, useMemo, useState } from 'react'
import { useSearchParams, useNavigate } from 'react-router-dom'
import clsx from 'clsx'
import {
  useMonitoringPlan,
  useCreateMonitoringPlan,
  useUpdateMonitoringPlan,
  useValidateMonitoringPlan,
  useStrategies,
} from '@/hooks/useApi'
import { useToast } from '@/store/toast'
import type {
  MonitoringPlanMember,
  MonitoringPlanMemberRequest,
  MonitoringPlanCreateRequest,
} from '@/api/endpoints'

// ============================================================
// 常量定义
// ============================================================

// 组合触发模式
type ComboMode = 'independent' | 'all' | 'any'

const COMBO_MODE_OPTIONS: { value: ComboMode; title: string; desc: string }[] = [
  { value: 'independent', title: '各策略独立通知', desc: '每个策略事件分别触发，不形成组合事件。' },
  { value: 'all', title: '全部策略确认（ALL）', desc: '指定时间窗口内所有策略都满足后触发一次组合事件。' },
  { value: 'any', title: '任一策略触发（ANY）', desc: '任一策略满足即形成方案事件。' },
]

// 应用范围选项
const SCOPE_OPTIONS = ['全部自选股', '重点追踪分组']

// 事件顺序选项
const ORDER_OPTIONS: { value: boolean; label: string }[] = [
  { value: false, label: '不限制顺序' },
  { value: true, label: '按策略顺序依次确认' },
]

// 组合状态失效策略
const INVALIDATION_OPTIONS = ['窗口结束自动失效', '任一策略反向事件时失效']

// 通知渠道选项
const CHANNEL_OPTIONS = ['站内消息 + 飞书「交易提醒群」']

// 单策略过程事件策略
const PROCESS_EVENT_OPTIONS = ['仅站内记录，不推送飞书', '同时推送']

// 策略参数表单字段配置
interface ParamFieldConfig {
  label: string
  options: string[]
  paramKey: string
}

// 策略参数表单整体配置
interface StrategyParamConfig {
  triggerEvent?: ParamFieldConfig
  confirmCondition?: ParamFieldConfig
  nodeFilter?: ParamFieldConfig
  blueBandPosition?: ParamFieldConfig
  deltaDirection?: ParamFieldConfig
}

// 已知监控策略的参数配置（按 display_name 关键词匹配，对齐原型 V1.6.3）
const KNOWN_STRATEGY_PARAMS: { match: string; role: 'TRIGGER' | 'CONFIRM'; config: StrategyParamConfig }[] = [
  {
    match: 'node cluster',
    role: 'TRIGGER',
    config: {
      triggerEvent: {
        label: '触发事件',
        options: ['碰触任意节点', '仅碰触 POC', '碰触上方节点', '碰触下方节点'],
        paramKey: 'trigger_event',
      },
      nodeFilter: {
        label: '节点过滤',
        options: ['不限制多空量', '多头量 ≥ 空头量'],
        paramKey: 'node_filter',
      },
    },
  },
  {
    match: 'atr rope',
    role: 'CONFIRM',
    config: {
      confirmCondition: {
        label: '确认条件',
        options: ['趋势方向 = 向上', '价格回到蓝带上方'],
        paramKey: 'confirm_condition',
      },
      blueBandPosition: {
        label: '蓝带位置',
        options: ['≥ 0.60', '≥ 0.50'],
        paramKey: 'blue_band_position',
      },
    },
  },
  {
    match: 'volume delta',
    role: 'CONFIRM',
    config: {
      confirmCondition: {
        label: '确认条件',
        options: ['成交量 Z-score ≥ 2.0', '主动买入占比 ≥ 65%'],
        paramKey: 'confirm_condition',
      },
      deltaDirection: {
        label: 'Delta 方向',
        options: ['净流入', '不限制'],
        paramKey: 'delta_direction',
      },
    },
  },
]

// 默认策略版本号（用于展示，实际应从策略版本获取）
const DEFAULT_VERSION = 'v1.0.0'

// ============================================================
// 类型定义
// ============================================================

// 编辑器中的策略成员表单状态
interface MemberForm {
  localId: string
  strategyDefinitionId: string
  strategyKey: string
  displayName: string
  version: string
  role: 'TRIGGER' | 'CONFIRM'
  eventType: string
  params: Record<string, string>
  expanded: boolean
}

// 编辑器整体表单状态
interface PlanForm {
  name: string
  description: string
  scope: string
  mode: ComboMode
  confirmationWindowMinutes: number
  ordered: boolean
  cooldownMinutes: number
  invalidationPolicy: string
  feishuPushEnabled: boolean
  notificationChannel: string
  processEventPolicy: string
  members: MemberForm[]
}

// 默认表单（新建方案）
function createDefaultForm(): PlanForm {
  return {
    name: '',
    description: '',
    scope: SCOPE_OPTIONS[0],
    mode: 'all',
    confirmationWindowMinutes: 15,
    ordered: false,
    cooldownMinutes: 10,
    invalidationPolicy: INVALIDATION_OPTIONS[0],
    feishuPushEnabled: true,
    notificationChannel: CHANNEL_OPTIONS[0],
    processEventPolicy: PROCESS_EVENT_OPTIONS[0],
    members: [],
  }
}

// ============================================================
// 辅助函数
// ============================================================

// 生成本地唯一 ID
let localIdSeq = 0
function genLocalId(): string {
  localIdSeq += 1
  return `local-${Date.now()}-${localIdSeq}`
}

// 根据策略 display_name 匹配已知参数配置
function matchStrategyConfig(
  displayName: string,
): StrategyParamConfig & { role: 'TRIGGER' | 'CONFIRM' } {
  const lower = displayName.toLowerCase()
  for (const item of KNOWN_STRATEGY_PARAMS) {
    if (lower.includes(item.match)) {
      return { ...item.config, role: item.role }
    }
  }
  return { role: 'CONFIRM' }
}

// 从 API 成员转换为表单成员
function memberToForm(
  member: MonitoringPlanMember,
  displayName: string,
  strategyKey: string,
): MemberForm {
  const cfg = matchStrategyConfig(displayName)
  const params: Record<string, string> = {}
  const fields = [cfg.triggerEvent, cfg.confirmCondition, cfg.nodeFilter, cfg.blueBandPosition, cfg.deltaDirection]
  for (const field of fields) {
    if (!field) continue
    const saved = member.params?.[field.paramKey]
    params[field.paramKey] = typeof saved === 'string' && saved ? saved : field.options[0]
  }
  return {
    localId: genLocalId(),
    strategyDefinitionId: member.strategy_definition_id,
    strategyKey,
    displayName,
    version: DEFAULT_VERSION,
    role: member.role === 'TRIGGER' ? 'TRIGGER' : 'CONFIRM',
    eventType: member.event_type || 'trigger',
    params,
    expanded: true,
  }
}

// 从可选策略构造新成员表单
function strategyToMember(
  strategyKey: string,
  displayName: string,
  strategyDefinitionId: string,
): MemberForm {
  const cfg = matchStrategyConfig(displayName)
  const params: Record<string, string> = {}
  const fields = [cfg.triggerEvent, cfg.confirmCondition, cfg.nodeFilter, cfg.blueBandPosition, cfg.deltaDirection]
  for (const field of fields) {
    if (!field) continue
    params[field.paramKey] = field.options[0]
  }
  return {
    localId: genLocalId(),
    strategyDefinitionId,
    strategyKey,
    displayName,
    version: DEFAULT_VERSION,
    role: cfg.role,
    eventType: cfg.role === 'TRIGGER' ? 'trigger' : 'confirm',
    params,
    expanded: true,
  }
}

// 表单成员转 API 请求成员
function formToMemberRequest(m: MemberForm): MonitoringPlanMemberRequest {
  return {
    strategy_definition_id: m.strategyDefinitionId,
    version_policy: 'pinned',
    event_type: m.eventType,
    role: m.role,
    position: 0,
    required: true,
    enabled: true,
    params: m.params,
    conditions: [],
  }
}

// 深比较两个表单是否相同（用于脏数据检测）
function isFormEqual(a: PlanForm, b: PlanForm): boolean {
  return JSON.stringify(a) === JSON.stringify(b)
}

// ============================================================
// 主组件
// ============================================================

export default function MonitoringPlanEditorPage() {
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const showToast = useToast((s) => s.show)

  const planId = searchParams.get('planId') || undefined
  const isNew = !planId

  // 数据查询：方案详情（编辑模式）
  const planQuery = useMonitoringPlan(planId)
  // 数据查询：可选策略列表（用于添加策略弹窗）
  const strategiesQuery = useStrategies()

  // 变更操作
  const createMutation = useCreateMonitoringPlan()
  const updateMutation = useUpdateMonitoringPlan()
  const validateMutation = useValidateMonitoringPlan()

  // 表单状态
  const [form, setForm] = useState<PlanForm>(createDefaultForm)
  // 初始快照（用于脏数据检测）
  const [initialSnapshot, setInitialSnapshot] = useState<PlanForm>(createDefaultForm)
  // 是否已从 API 初始化（编辑模式）
  const [initialized, setInitialized] = useState(false)
  // 添加策略弹窗是否打开
  const [pickerOpen, setPickerOpen] = useState(false)
  // 保存中
  const [saving, setSaving] = useState(false)

  // 编辑模式：从 API 数据初始化表单
  useEffect(() => {
    if (isNew || initialized) return
    const plan = planQuery.data
    if (!plan) return
    const revision = plan.current_revision_detail
    // 构造成员表单：从 members 映射，尝试匹配策略 display_name
    const members: MemberForm[] = (revision?.members || []).map((m) => {
      const matched = strategiesQuery.data?.items.find((s) => s.id === m.strategy_definition_id)
      const displayName = matched?.display_name || `策略 ${m.strategy_definition_id.slice(-6)}`
      const strategyKey = matched?.strategy_key || m.strategy_definition_id
      return memberToForm(m, displayName, strategyKey)
    })
    const next: PlanForm = {
      name: plan.name || '',
      description: plan.description || '',
      scope: SCOPE_OPTIONS[0],
      mode: (revision?.mode as ComboMode) || 'all',
      confirmationWindowMinutes: Math.round((revision?.confirmation_window_seconds || 900) / 60),
      ordered: revision?.ordered || false,
      cooldownMinutes: Math.round((revision?.cooldown_seconds || 600) / 60),
      invalidationPolicy: INVALIDATION_OPTIONS[0],
      feishuPushEnabled: (revision?.notification_config?.feishu_enabled as boolean) ?? true,
      notificationChannel: CHANNEL_OPTIONS[0],
      processEventPolicy:
        revision?.process_event_policy === 'push' ? PROCESS_EVENT_OPTIONS[1] : PROCESS_EVENT_OPTIONS[0],
      members,
    }
    setForm(next)
    setInitialSnapshot(next)
    setInitialized(true)
  }, [isNew, initialized, planQuery.data, strategiesQuery.data])

  // 脏数据检测
  const isDirty = useMemo(() => !isFormEqual(form, initialSnapshot), [form, initialSnapshot])

  // beforeunload 脏数据保护
  useEffect(() => {
    if (!isDirty) return
    const handler = (e: BeforeUnloadEvent) => {
      e.preventDefault()
      e.returnValue = ''
    }
    window.addEventListener('beforeunload', handler)
    return () => window.removeEventListener('beforeunload', handler)
  }, [isDirty])

  // 表单更新辅助
  function updateForm(patch: Partial<PlanForm>) {
    setForm((prev) => ({ ...prev, ...patch }))
  }

  // 切换组合触发模式
  function handleModeChange(mode: ComboMode) {
    updateForm({ mode })
  }

  // 切换某成员参数区展开/收起
  function toggleMemberExpand(localId: string) {
    setForm((prev) => ({
      ...prev,
      members: prev.members.map((m) => (m.localId === localId ? { ...m, expanded: !m.expanded } : m)),
    }))
  }

  // 移除成员
  function removeMember(localId: string) {
    setForm((prev) => ({
      ...prev,
      members: prev.members.filter((m) => m.localId !== localId),
    }))
    showToast('已移除策略', '该策略已从组合中移除')
  }

  // 更新成员参数
  function updateMemberParam(localId: string, paramKey: string, value: string) {
    setForm((prev) => ({
      ...prev,
      members: prev.members.map((m) =>
        m.localId === localId ? { ...m, params: { ...m.params, [paramKey]: value } } : m,
      ),
    }))
  }

  // 添加策略到组合
  function addMember(strategyDefinitionId: string, strategyKey: string, displayName: string) {
    const newMember = strategyToMember(strategyKey, displayName, strategyDefinitionId)
    setForm((prev) => ({ ...prev, members: [...prev.members, newMember] }))
    showToast('已加入组合', `${displayName} 已加入监控组合`)
    setPickerOpen(false)
  }

  // 构造保存请求体
  function buildPayload(): MonitoringPlanCreateRequest {
    return {
      name: form.name || '未命名监控方案',
      description: form.description || undefined,
      mode: form.mode,
      confirmation_window_seconds: form.confirmationWindowMinutes * 60,
      ordered: form.ordered,
      cooldown_seconds: form.cooldownMinutes * 60,
      process_event_policy: form.processEventPolicy === PROCESS_EVENT_OPTIONS[1] ? 'push' : 'silent',
      notification_config: {
        feishu_enabled: form.feishuPushEnabled,
        channel: form.notificationChannel,
      },
      members: form.members.map(formToMemberRequest),
    }
  }

  // 保存方案
  async function handleSave() {
    if (!form.name.trim()) {
      showToast('保存失败', '请填写方案名称')
      return
    }
    if (form.members.length === 0) {
      showToast('保存失败', '请至少添加一个监控策略')
      return
    }
    setSaving(true)
    try {
      const payload = buildPayload()
      if (isNew) {
        await createMutation.mutateAsync(payload)
        showToast('监控组合方案已保存', '新方案已创建')
      } else if (planId) {
        // 更新前先验证方案合法性
        try {
          const validation = await validateMutation.mutateAsync(planId)
          if (!validation.valid) {
            showToast('验证未通过', validation.errors.join('；') || '请检查方案配置')
            setSaving(false)
            return
          }
        } catch {
          // 验证接口失败时不阻塞保存，继续执行更新
        }
        await updateMutation.mutateAsync({ planId, payload })
        showToast('监控组合方案已保存', '方案已更新为新版本')
      }
      // 保存成功后更新快照，清除脏标记
      setInitialSnapshot(form)
      navigate('/watchlist')
    } catch (err) {
      const msg = err instanceof Error ? err.message : '未知错误'
      showToast('保存失败', msg)
    } finally {
      setSaving(false)
    }
  }

  // 另存为新方案
  async function handleSaveAs() {
    if (!form.name.trim()) {
      showToast('另存失败', '请填写方案名称')
      return
    }
    setSaving(true)
    try {
      const payload = buildPayload()
      await createMutation.mutateAsync(payload)
      showToast('已另存为新监控方案', '新方案已创建')
      setInitialSnapshot(form)
      navigate('/watchlist')
    } catch (err) {
      const msg = err instanceof Error ? err.message : '未知错误'
      showToast('另存失败', msg)
    } finally {
      setSaving(false)
    }
  }

  // 取消编辑
  function handleCancel() {
    navigate('/watchlist')
  }

  // 加载态
  const loading = !isNew && planQuery.isLoading
  // 可选策略列表（标记已在组合中的）
  const availableStrategies = useMemo(() => {
    const usedIds = new Set(form.members.map((m) => m.strategyDefinitionId))
    return (strategiesQuery.data?.items || []).map((s) => ({
      id: s.id,
      strategyKey: s.strategy_key,
      displayName: s.display_name,
      kind: s.kind,
      inCombo: usedIds.has(s.id),
    }))
  }, [strategiesQuery.data, form.members])

  // 页面标题
  const pageTitle = isNew
    ? '新建监控组合方案'
    : `编辑监控组合方案：${form.name || planQuery.data?.name || ''}`

  return (
    <>
      <div className="page-head">
        <div>
          <div className="page-desc">
            <a
              href="#/watchlist"
              onClick={(e) => {
                e.preventDefault()
                navigate('/watchlist')
              }}
            >
              我的自选
            </a>{' '}
            / 监控组合方案编辑
          </div>
          <h1 className="page-title">{pageTitle}</h1>
          <div className="page-desc">将多个监控策略组合为一个服务方案，并定义独立触发或组合确认逻辑</div>
        </div>
        <div className="actions">
          <button className="btn" onClick={handleCancel}>取消</button>
          <button className="btn" onClick={handleSaveAs} disabled={saving}>另存为</button>
          <button className="btn primary" onClick={handleSave} disabled={saving}>
            {saving ? '保存中…' : '保存方案'}
          </button>
        </div>
      </div>

      {loading ? (
        <div className="card">
          <div className="card-body">
            <div className="page-desc">正在加载方案数据…</div>
          </div>
        </div>
      ) : (
        <div className="editor-layout">
          <section className="card">
            {/* 基本信息 */}
            <div className="editor-section">
              <div className="editor-title">基本信息</div>
              <div className="form-grid">
                <div className="form-row">
                  <label className="form-label">方案名称</label>
                  <input
                    className="input"
                    value={form.name}
                    onChange={(e) => updateForm({ name: e.target.value })}
                    placeholder="输入方案名称"
                  />
                </div>
                <div className="form-row">
                  <label className="form-label">应用范围</label>
                  <select className="select" value={form.scope} onChange={(e) => updateForm({ scope: e.target.value })}>
                    {SCOPE_OPTIONS.map((opt) => (<option key={opt} value={opt}>{opt}</option>))}
                  </select>
                </div>
                <div className="form-row full">
                  <label className="form-label">方案说明</label>
                  <input
                    className="input"
                    value={form.description}
                    onChange={(e) => updateForm({ description: e.target.value })}
                    placeholder="描述方案的触发与确认逻辑"
                  />
                </div>
              </div>
            </div>

            {/* 组合触发模式 */}
            <div className="editor-section">
              <div className="editor-title">组合触发模式</div>
              <div className="editor-desc">决定多个实时策略事件如何形成最终通知。</div>
              <div className="combo-mode-grid three">
                {COMBO_MODE_OPTIONS.map((opt) => (
                  <label key={opt.value} className={clsx('combo-mode-card', form.mode === opt.value && 'active')}>
                    <input
                      type="radio"
                      name="monitorCombo"
                      checked={form.mode === opt.value}
                      onChange={() => handleModeChange(opt.value)}
                    />
                    <b>{opt.title}</b>
                    <span>{opt.desc}</span>
                  </label>
                ))}
              </div>
              <div className="form-grid">
                <div className="form-row">
                  <label className="form-label">组合确认窗口</label>
                  <div className="input-group">
                    <input
                      className="input input-group-input"
                      type="number"
                      min={1}
                      value={form.confirmationWindowMinutes}
                      onChange={(e) => updateForm({ confirmationWindowMinutes: Math.max(1, Number(e.target.value) || 1) })}
                    />
                    <span className="btn input-group-suffix">分钟</span>
                  </div>
                </div>
                <div className="form-row">
                  <label className="form-label">事件顺序</label>
                  <select className="select" value={form.ordered ? 'ordered' : 'free'} onChange={(e) => updateForm({ ordered: e.target.value === 'ordered' })}>
                    {ORDER_OPTIONS.map((opt) => (<option key={String(opt.value)} value={opt.value ? 'ordered' : 'free'}>{opt.label}</option>))}
                  </select>
                </div>
              </div>
            </div>

            {/* 组合中的监控策略 */}
            <div className="editor-section">
              <div className="section-head-inline">
                <div>
                  <div className="editor-title">组合中的监控策略</div>
                  <div className="editor-desc">每个策略定义一个可判定的事件条件；事件由共享监控引擎计算。</div>
                </div>
                <button className="btn small primary" onClick={() => setPickerOpen(true)}>＋ 添加监控策略</button>
              </div>
              <div className="strategy-composer">
                {form.members.length === 0 ? (
                  <div className="notice">暂无监控策略，点击右上角「添加监控策略」开始组合。</div>
                ) : (
                  form.members.map((m, idx) => (
                    <div key={m.localId}>
                      <article className="strategy-composer-card">
                        <div className="composer-handle">{idx + 1}</div>
                        <div className="composer-main">
                          <div className="composer-head">
                            <div>
                              <span className="strategy-type-pill">{m.role}</span>
                              <h3>{m.displayName} <span className="tag good">{m.version}</span></h3>
                              <p>{m.role === 'TRIGGER' ? '触发事件由该策略生成，进入组合确认窗口。' : '在确认窗口内提供确认信号。'}</p>
                            </div>
                            <div className="composer-head-actions">
                              <button className="btn small" onClick={() => toggleMemberExpand(m.localId)} title={m.expanded ? '收起参数' : '展开参数'}>
                                {m.expanded ? '收起' : '展开'}
                              </button>
                              <button className="icon-btn composer-remove" onClick={() => removeMember(m.localId)} title="移除策略">×</button>
                            </div>
                          </div>
                          {m.expanded && (
                            <div className="composer-config">
                              <MemberParamForm member={m} onParamChange={(paramKey, value) => updateMemberParam(m.localId, paramKey, value)} />
                            </div>
                          )}
                        </div>
                      </article>
                      {idx < form.members.length - 1 && (
                        <div className="combo-connector">
                          <span>{idx === 0 ? `THEN / WITHIN ${form.confirmationWindowMinutes}m` : 'AND'}</span>
                        </div>
                      )}
                    </div>
                  ))
                )}
              </div>
            </div>

            {/* 去重、冷却与失效 */}
            <div className="editor-section">
              <div className="editor-title">去重、冷却与失效</div>
              <div className="form-grid">
                <div className="form-row">
                  <label className="form-label">通知冷却</label>
                  <div className="input-group">
                    <input
                      className="input input-group-input"
                      type="number"
                      min={1}
                      value={form.cooldownMinutes}
                      onChange={(e) => updateForm({ cooldownMinutes: Math.max(1, Number(e.target.value) || 1) })}
                    />
                    <span className="btn input-group-suffix">分钟</span>
                  </div>
                </div>
                <div className="form-row">
                  <label className="form-label">组合状态失效</label>
                  <select className="select" value={form.invalidationPolicy} onChange={(e) => updateForm({ invalidationPolicy: e.target.value })}>
                    {INVALIDATION_OPTIONS.map((opt) => (<option key={opt} value={opt}>{opt}</option>))}
                  </select>
                </div>
              </div>
            </div>

            {/* 通知内容 */}
            <div className="editor-section">
              <div className="editor-title">通知内容</div>
              <div className="toggle-row">
                <div>
                  <b>启用飞书组合事件推送</b>
                  <div className="help">消息同时展示各策略的确认时间和指标值。</div>
                </div>
                <span
                  className={clsx('switch', form.feishuPushEnabled && 'on')}
                  onClick={() => updateForm({ feishuPushEnabled: !form.feishuPushEnabled })}
                  role="button"
                  tabIndex={0}
                />
              </div>
              <div className="form-grid">
                <div className="form-row">
                  <label className="form-label">通知渠道</label>
                  <select className="select" value={form.notificationChannel} onChange={(e) => updateForm({ notificationChannel: e.target.value })}>
                    {CHANNEL_OPTIONS.map((opt) => (<option key={opt} value={opt}>{opt}</option>))}
                  </select>
                </div>
                <div className="form-row">
                  <label className="form-label">单策略过程事件</label>
                  <select className="select" value={form.processEventPolicy} onChange={(e) => updateForm({ processEventPolicy: e.target.value })}>
                    {PROCESS_EVENT_OPTIONS.map((opt) => (<option key={opt} value={opt}>{opt}</option>))}
                  </select>
                </div>
              </div>
            </div>
          </section>
          <aside className="stack">
            <div className="card sticky-summary">
              <div className="card-head">
                <div>
                  <div className="card-title">方案运行预览</div>
                  <div className="card-sub">当前自选股 18 只</div>
                </div>
                <button className="btn small" onClick={() => showToast('运行预览已刷新', '已重新加载最新候选与确认数据')}>刷新</button>
              </div>
              <div className="card-body">
                <div className="summary-row"><span>Node 触发候选</span><b className="num">6</b></div>
                <div className="summary-row"><span>ATR 已确认</span><b className="num">4</b></div>
                <div className="summary-row"><span>Volume 已确认</span><b className="num">3</b></div>
                <div className="summary-row"><span>最终组合事件</span><b className="num pos">2</b></div>
                <div className="summary-row"><span>确认窗口</span><b className="num">{form.confirmationWindowMinutes}m</b></div>
                <div className="card-title preview-section-title">最近组合事件</div>
                <div className="combo-event-mini">
                  <b>鼎阳科技 · 10:28</b>
                  <span>Node 10:18 → ATR 10:22 → Volume 10:28</span>
                  <i>全部确认 · 已推送</i>
                </div>
                <div className="notice preview-notice">过程事件和最终组合事件分别保存，便于回溯每个策略的贡献。</div>
              </div>
            </div>
          </aside>
        </div>
      )}

      {pickerOpen && (
        <div className="modal-backdrop open" onClick={() => setPickerOpen(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-head">
              <div>
                <b>添加监控策略</b>
                <div className="card-sub">新策略接入后自动出现在可选列表</div>
              </div>
              <button className="icon-btn" onClick={() => setPickerOpen(false)}>×</button>
            </div>
            <div className="modal-body">
              {availableStrategies.length === 0 ? (
                <div className="page-desc">暂无可用策略，请稍后再试。</div>
              ) : (
                <div className="strategy-picker-list">
                  {availableStrategies.map((s) => (
                    <label key={s.id} className="strategy-picker-item">
                      <input type="checkbox" checked={s.inCombo} disabled={s.inCombo} />
                      <div>
                        <b>{s.displayName}</b>
                        <span>{s.inCombo ? '已在组合中' : `可用 · ${s.kind || '监控'}`}</span>
                      </div>
                      {!s.inCombo && (
                        <button
                          className="btn small primary"
                          onClick={(e) => {
                            e.preventDefault()
                            addMember(s.id, s.strategyKey, s.displayName)
                          }}
                        >
                          加入
                        </button>
                      )}
                    </label>
                  ))}
                </div>
              )}
            </div>
            <div className="modal-foot">
              <button className="btn" onClick={() => setPickerOpen(false)}>取消</button>
            </div>
          </div>
        </div>
      )}
    </>
  )
}

// ============================================================
// 成员参数表单子组件
// ============================================================

interface MemberParamFormProps {
  member: MemberForm
  onParamChange: (paramKey: string, value: string) => void
}

// 根据成员的 displayName 匹配已知参数配置，渲染对应的下拉选择字段
function MemberParamForm({ member, onParamChange }: MemberParamFormProps) {
  const cfg = matchStrategyConfig(member.displayName)
  const fields: { config: ParamFieldConfig }[] = []
  if (cfg.triggerEvent) fields.push({ config: cfg.triggerEvent })
  if (cfg.confirmCondition) fields.push({ config: cfg.confirmCondition })
  if (cfg.nodeFilter) fields.push({ config: cfg.nodeFilter })
  if (cfg.blueBandPosition) fields.push({ config: cfg.blueBandPosition })
  if (cfg.deltaDirection) fields.push({ config: cfg.deltaDirection })

  if (fields.length === 0) {
    return (
      <div className="form-grid">
        <div className="form-row full">
          <div className="help">该策略暂无可配置参数。</div>
        </div>
      </div>
    )
  }

  return (
    <div className="form-grid">
      {fields.map(({ config }) => (
        <div className="form-row" key={config.paramKey}>
          <label className="form-label">{config.label}</label>
          <select
            className="select"
            value={member.params[config.paramKey] || config.options[0]}
            onChange={(e) => onParamChange(config.paramKey, e.target.value)}
          >
            {config.options.map((opt) => (<option key={opt} value={opt}>{opt}</option>))}
          </select>
        </div>
      ))}
    </div>
  )
}
