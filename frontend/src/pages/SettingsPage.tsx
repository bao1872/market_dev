// 通知与个人设置页（受保护路由）
// 对应原型：settings.html (V1.6.3)
//
// 用法：
// 1. 路由 /settings，受保护路由（经 ProtectedLayout 包裹）
// 2. 左栏 4 张卡片：会员状态 / 用户通知规则 / 方案消息订阅 / 我的通知渠道
// 3. 右栏 2 张卡片：飞书消息预览（3 个 tab 切换）/ 飞书配置步骤
// 4. 两个弹窗：飞书配置弹窗（新建/编辑）、续期弹窗
//
// 依赖 hooks：
// - useMyMembership：会员状态卡（到期时间、剩余天数、环形进度）
// - useRenew：续期弹窗提交
// - useNotificationChannels：我的通知渠道列表
// - useCreateNotificationChannel：飞书配置弹窗"验证并保存"
// - useTestNotificationChannel：飞书配置弹窗"发送测试消息"（编辑已有渠道时）
// - usePreviewNotification：预览卡"发送当前示例"按钮（校验后端可渲染消息结构）

import { useState, type CSSProperties } from 'react'
import {
  useMyMembership,
  useRenew,
  useNotificationChannels,
  useCreateNotificationChannel,
  useTestNotificationChannel,
  usePreviewNotification,
} from '@/hooks/useApi'
import { useToast } from '@/store/toast'
import type { NotificationChannel } from '@/api/endpoints'

// ===== 工具函数 =====

/** 将 ISO 日期字符串格式化为 YYYY-MM-DD */
function formatDate(iso: string | null | undefined): string {
  if (!iso) return '—'
  return iso.split('T')[0]
}

/** 邀请码清洁长度（仅字母数字，与原型 app.js 校验逻辑一致） */
function cleanLength(code: string): number {
  return code.replace(/[^A-Za-z0-9]/g, '').length
}

/** 计算续期后到期日（当前到期日 + 30 天），返回 YYYY-MM-DD */
function computeRenewPreview(expiresAt: string | null | undefined): string {
  if (!expiresAt) return '—'
  const base = new Date(expiresAt)
  if (Number.isNaN(base.getTime())) return '—'
  base.setDate(base.getDate() + 30)
  return base.toISOString().split('T')[0]
}

/** 格式化渠道验证时间（HH:mm 验证），未验证返回"未验证" */
function formatVerifyTime(iso: string | null | undefined): string {
  if (!iso) return '未验证'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '未验证'
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  return `${hh}:${mm} 验证`
}

// ===== 预览 Tab 配置 =====

type PreviewTab = 'selector' | 'monitor' | 'system'

const PREVIEW_TABS: { key: PreviewTab; label: string; messageType: string }[] = [
  { key: 'selector', label: '选股组合结果', messageType: 'selection_plan_completed' },
  { key: 'monitor', label: '监控组合触发', messageType: 'composite_event_confirmed' },
  { key: 'system', label: '系统异常', messageType: 'system_data_delay' },
]

// ===== 飞书消息预览子组件（静态内容，对齐原型三种消息 Schema）=====

/** 选股组合结果预览 */
function SelectorPreview() {
  return (
    <div className="feishu-card-preview">
      <div className="feishu-accent blue"></div>
      <div className="feishu-header">
        <div className="feishu-icon">QS</div>
        <div>
          <b>选股组合方案执行完成</b>
          <span>强势共振 · 2026-06-18</span>
        </div>
      </div>
      <div className="feishu-body">
        <div className="feishu-kv"><span>组合逻辑</span><b>DSA AND 突破强度</b></div>
        <div className="feishu-kv"><span>最终命中</span><b className="pos">8 只</b></div>
        <div className="feishu-divider"></div>
        <div className="feishu-stock">
          <b>1. 鼎阳科技 688112</b>
          <span>DSA 平均收益 1.10% · 偏移方差率 0.65%</span>
          <span>突破幅度 2.77% · 量比 1.58x</span>
        </div>
        <div className="feishu-stock">
          <b>2. 矩子科技 300802</b>
          <span>DSA 平均收益 1.24% · 偏移方差率 0.82%</span>
          <span>突破幅度 3.14% · 量比 1.92x</span>
        </div>
        <button className="feishu-link">查看完整组合结果 →</button>
      </div>
      <div className="feishu-footer">量策服务台 · 15:13</div>
    </div>
  )
}

/** 监控组合触发预览 */
function MonitorPreview() {
  return (
    <div className="feishu-card-preview">
      <div className="feishu-accent green"></div>
      <div className="feishu-header">
        <div className="feishu-icon">QS</div>
        <div>
          <b>监控组合事件已确认</b>
          <span>节点共振追踪 · 鼎阳科技 688112</span>
        </div>
      </div>
      <div className="feishu-body">
        <div className="feishu-kv"><span>当前价格</span><b>45.94</b></div>
        <div className="feishu-kv"><span>确认状态</span><b className="pos">3/3 全部满足</b></div>
        <div className="feishu-divider"></div>
        <div className="event-step done">
          <i>1</i>
          <div>
            <b>10:18 · Node 碰触 POC</b>
            <span>节点 46.02–46.18 · 位置 0.71</span>
          </div>
        </div>
        <div className="event-step done">
          <i>2</i>
          <div>
            <b>10:22 · ATR Rope 向上确认</b>
            <span>蓝带位置 0.76 · 偏离度 +2.14%</span>
          </div>
        </div>
        <div className="event-step done">
          <i>3</i>
          <div>
            <b>10:28 · Volume Delta 放量确认</b>
            <span>Z-score 2.31 · 主动买入 68%</span>
          </div>
        </div>
        <button className="feishu-link">打开个股策略详情 →</button>
      </div>
      <div className="feishu-footer">量策服务台 · 冷却 10 分钟</div>
    </div>
  )
}

/** 系统异常预览 */
function SystemPreview() {
  return (
    <div className="feishu-card-preview">
      <div className="feishu-accent orange"></div>
      <div className="feishu-header">
        <div className="feishu-icon">!</div>
        <div>
          <b>策略服务数据延迟</b>
          <span>系统服务通知</span>
        </div>
      </div>
      <div className="feishu-body">
        <div className="feishu-kv"><span>分钟行情延迟</span><b className="neg">128 秒</b></div>
        <div className="feishu-kv"><span>处理动作</span><b>实时推送已暂停</b></div>
        <div className="notice">数据恢复后系统会自动继续监控，并在站内保留完整运行记录。</div>
        <button className="feishu-link">查看系统状态 →</button>
      </div>
      <div className="feishu-footer">量策服务台 · 10:32</div>
    </div>
  )
}

// ===== 续期弹窗 =====

function RenewModal({
  expiresAt,
  onClose,
}: {
  expiresAt: string | null
  onClose: () => void
}) {
  const toast = useToast()
  const renew = useRenew()
  const [inviteCode, setInviteCode] = useState('')
  const [error, setError] = useState('')

  const oldDate = formatDate(expiresAt)
  const newDate = computeRenewPreview(expiresAt)
  const isValid = cleanLength(inviteCode) >= 8

  // 邀请码输入：大写化并清除错误
  const handleInviteChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    setInviteCode(e.target.value.toUpperCase())
    if (error) setError('')
  }

  // 确认续期：校验通过后调用 useRenew
  const handleSubmit = () => {
    if (!isValid) {
      setError('请输入有效的邀请码。')
      return
    }
    renew.mutate(inviteCode, {
      onSuccess: (data) => {
        toast.show('续期成功', `会员有效期已更新至 ${formatDate(data.new_expires_at)}`)
        onClose()
      },
      onError: (err: unknown) => {
        const axiosErr = err as { response?: { data?: { detail?: string } } }
        const message = axiosErr.response?.data?.detail ?? '邀请码无效或已使用'
        setError(message)
        toast.show('续期失败', message)
      },
    })
  }

  return (
    <div className="modal-backdrop open" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <div>
            <b>使用邀请码续期</b>
            <div className="card-sub">每个有效邀请码可增加30天会员</div>
          </div>
          <button className="icon-btn" onClick={onClose}>×</button>
        </div>
        <div className="modal-body">
          <div className="membership-result compact">
            <div>
              <span>当前到期时间</span>
              <b>{oldDate}</b>
            </div>
            <div>
              <span>续期后到期时间</span>
              <b className="pos">{newDate}</b>
            </div>
          </div>
          <div className="form-grid-gap">
            <div className="form-row full">
              <label className="form-label">邀请码</label>
              <div className="invite-input-wrap">
                <input
                  className="input invite-code-input"
                  value={inviteCode}
                  onChange={handleInviteChange}
                  placeholder="请输入邀请码"
                  required
                />
                {isValid && (
                  <span className="invite-validation good">✓ 邀请码有效 · +30天</span>
                )}
              </div>
              <div className="help">邀请码兑换后立即失效，不能再次使用。</div>
            </div>
            <div className="form-error">{error}</div>
          </div>
        </div>
        <div className="modal-foot">
          <button className="btn" onClick={onClose}>取消</button>
          <button
            className="btn primary"
            type="button"
            onClick={handleSubmit}
            disabled={renew.isPending}
          >
            {renew.isPending ? '续期中...' : '确认续期'}
          </button>
        </div>
      </div>
    </div>
  )
}

// ===== 飞书配置弹窗 =====

/** 飞书渠道表单状态 */
interface FeishuFormState {
  displayName: string
  webhookUrl: string
  secret: string
  selection: boolean
  monitor: boolean
  singleStrategy: boolean
  system: boolean
}

function FeishuModal({
  editingChannel,
  onClose,
}: {
  editingChannel: NotificationChannel | null
  onClose: () => void
}) {
  const toast = useToast()
  const createChannel = useCreateNotificationChannel()
  const testChannel = useTestNotificationChannel()

  const [form, setForm] = useState<FeishuFormState>({
    displayName: editingChannel?.display_name ?? '',
    webhookUrl: '',
    secret: '',
    selection: true,
    monitor: true,
    singleStrategy: false,
    system: false,
  })

  // 通用字段更新
  const handleField = <K extends keyof FeishuFormState>(key: K, value: FeishuFormState[K]) => {
    setForm((prev) => ({ ...prev, [key]: value }))
  }

  // 验证并保存：调用创建渠道接口
  const handleSave = () => {
    if (!form.displayName.trim()) {
      toast.show('保存失败', '请填写配置名称')
      return
    }
    if (!form.webhookUrl.trim()) {
      toast.show('保存失败', '请填写 Webhook URL')
      return
    }
    // 构造接收消息类型列表
    const messageTypes: string[] = []
    if (form.selection) messageTypes.push('selection_plan_completed')
    if (form.monitor) messageTypes.push('composite_event_confirmed')
    if (form.singleStrategy) messageTypes.push('single_strategy_event')
    if (form.system) messageTypes.push('system_service')

    createChannel.mutate(
      {
        adapter_type: 'feishu',
        display_name: form.displayName.trim(),
        target_config: {
          webhook_url: form.webhookUrl.trim(),
          message_types: messageTypes,
        },
        secret_ref: form.secret.trim() || undefined,
      },
      {
        onSuccess: () => {
          toast.show('保存成功', '飞书机器人配置已加密保存')
          onClose()
        },
        onError: (err: unknown) => {
          const axiosErr = err as { response?: { data?: { detail?: string } } }
          const message = axiosErr.response?.data?.detail ?? '保存失败，请检查 Webhook URL'
          toast.show('保存失败', message)
        },
      },
    )
  }

  // 发送测试消息：编辑已有渠道时调用测试接口
  const handleTest = () => {
    if (!editingChannel) {
      toast.show('提示', '请先保存配置后再发送测试消息')
      return
    }
    testChannel.mutate(editingChannel.id, {
      onSuccess: (data) => {
        if (data.delivery.success) {
          toast.show('测试成功', '测试消息已发送到飞书群')
        } else {
          toast.show('测试失败', data.delivery.error_message ?? '发送失败，请检查配置')
        }
      },
      onError: () => {
        toast.show('测试失败', '请稍后重试')
      },
    })
  }

  return (
    <div className="modal-backdrop open" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <div>
            <b>配置我的飞书群机器人</b>
            <div className="card-sub">所有字段均由当前用户填写</div>
          </div>
          <button className="icon-btn" onClick={onClose}>×</button>
        </div>
        <div className="modal-body">
          <div className="notice">平台只调用该机器人发送方案消息，不会获得飞书账号、联系人或群聊读取权限。</div>
          <div className="form-grid form-grid-gap">
            <div className="form-row full">
              <label className="form-label">配置名称</label>
              <input
                className="input"
                value={form.displayName}
                onChange={(e) => handleField('displayName', e.target.value)}
                placeholder="如：交易提醒群"
              />
            </div>
            <div className="form-row full">
              <label className="form-label">Webhook URL</label>
              <input
                className="input"
                type="password"
                value={form.webhookUrl}
                onChange={(e) => handleField('webhookUrl', e.target.value)}
                placeholder="粘贴 https://open.feishu.cn/open-apis/bot/v2/hook/..."
              />
              <div className="help">仅允许 HTTPS 飞书官方域名。</div>
            </div>
            <div className="form-row full">
              <label className="form-label">签名密钥（可选）</label>
              <input
                className="input"
                type="password"
                value={form.secret}
                onChange={(e) => handleField('secret', e.target.value)}
                placeholder="启用机器人签名校验时填写"
              />
            </div>
            <div className="form-row full">
              <label className="form-label">接收消息类型</label>
              <div className="strategy-check-grid">
                <label className="strategy-check">
                  <input
                    type="checkbox"
                    className="checkbox"
                    checked={form.selection}
                    onChange={(e) => handleField('selection', e.target.checked)}
                  />
                  选股组合结果
                </label>
                <label className="strategy-check">
                  <input
                    type="checkbox"
                    className="checkbox"
                    checked={form.monitor}
                    onChange={(e) => handleField('monitor', e.target.checked)}
                  />
                  监控组合事件
                </label>
                <label className="strategy-check">
                  <input
                    type="checkbox"
                    className="checkbox"
                    checked={form.singleStrategy}
                    onChange={(e) => handleField('singleStrategy', e.target.checked)}
                  />
                  单策略过程事件
                </label>
                <label className="strategy-check">
                  <input
                    type="checkbox"
                    className="checkbox"
                    checked={form.system}
                    onChange={(e) => handleField('system', e.target.checked)}
                  />
                  系统服务消息
                </label>
              </div>
            </div>
          </div>
        </div>
        <div className="modal-foot">
          <button className="btn" onClick={onClose}>取消</button>
          <button
            className="btn"
            type="button"
            onClick={handleTest}
            disabled={testChannel.isPending}
          >
            {testChannel.isPending ? '发送中...' : '发送测试消息'}
          </button>
          <button
            className="btn primary"
            type="button"
            onClick={handleSave}
            disabled={createChannel.isPending}
          >
            {createChannel.isPending ? '保存中...' : '验证并保存'}
          </button>
        </div>
      </div>
    </div>
  )
}

// ===== 主页面 =====

export default function SettingsPage() {
  const toast = useToast()
  const membershipQuery = useMyMembership()
  const channelsQuery = useNotificationChannels()
  const previewNotification = usePreviewNotification()

  const [showRenewModal, setShowRenewModal] = useState(false)
  const [showFeishuModal, setShowFeishuModal] = useState(false)
  const [editingChannel, setEditingChannel] = useState<NotificationChannel | null>(null)
  const [previewTab, setPreviewTab] = useState<PreviewTab>('selector')

  // 通知规则表单状态
  const [cooldown, setCooldown] = useState('10')
  const [quietStart, setQuietStart] = useState('22:30')
  const [quietEnd, setQuietEnd] = useState('08:30')
  const [pauseOnDelay, setPauseOnDelay] = useState(true)

  // 方案消息订阅状态
  const [subs, setSubs] = useState({
    selectionStrong: true,
    monitorNode: true,
    dsaSingle: false,
    nodeSingle: false,
  })

  const membership = membershipQuery.data
  const remainingDays = membership?.remaining_days ?? 0
  // 环形进度百分比：剩余天数 / 30 天，上限 100%
  const ringPct = Math.min((remainingDays / 30) * 100, 100)
  const ringStyle = { '--ring-pct': `${ringPct}%` } as CSSProperties

  const channels = channelsQuery.data?.items ?? []
  // 站内消息渠道（系统默认）
  const inAppChannels = channels.filter((c) => c.adapter_type === 'in_app')
  // 飞书渠道
  const feishuChannels = channels.filter((c) => c.adapter_type === 'feishu')

  // 打开新建飞书配置弹窗
  const handleOpenNewFeishu = () => {
    setEditingChannel(null)
    setShowFeishuModal(true)
  }

  // 打开编辑飞书配置弹窗
  const handleOpenEditFeishu = (channel: NotificationChannel) => {
    setEditingChannel(channel)
    setShowFeishuModal(true)
  }

  // 保存通知规则（当前为前端态，后续接入后端配置接口）
  const handleSaveRules = () => {
    toast.show('已保存', '个人设置已保存')
  }

  // 发送当前示例：调用预览接口校验后端可渲染消息结构
  const handleSendPreview = () => {
    const tab = PREVIEW_TABS.find((t) => t.key === previewTab)
    if (!tab) return
    previewNotification.mutate(
      {
        message_type: tab.messageType,
        context: {},
      },
      {
        onSuccess: () => {
          toast.show('已发送', '示例消息已发送到飞书')
        },
        onError: () => {
          toast.show('发送失败', '请稍后重试')
        },
      },
    )
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1 className="page-title">通知与个人设置</h1>
          <div className="page-desc">飞书机器人由用户自行配置；右侧可预览真实推送消息结构</div>
        </div>
        <div className="actions">
          <button className="btn primary" onClick={handleOpenNewFeishu}>＋ 配置我的飞书机器人</button>
        </div>
      </div>

      <div className="grid split-2">
        {/* ===== 左栏 ===== */}
        <section className="stack">
          {/* 会员状态卡 */}
          <div className="card membership-card">
            <div className="card-head">
              <div>
                <div className="card-title">会员状态</div>
                <div className="card-sub">注册即开放全部功能；邀请码可随时续期</div>
              </div>
              <span className="status-pill ok">有效会员</span>
            </div>
            <div className="card-body">
              <div className="membership-hero">
                <div>
                  <span>当前到期时间</span>
                  <b>{formatDate(membership?.expires_at)}</b>
                  <small>剩余 {remainingDays} 天</small>
                </div>
                <div className="membership-ring" style={ringStyle}>
                  <b>{remainingDays}</b>
                  <span>天</span>
                </div>
              </div>
              <div className="membership-access-row">
                <span><i>✓</i> 选股策略</span>
                <span><i>✓</i> 分钟监控</span>
                <span><i>✓</i> 个股指标</span>
                <span><i>✓</i> 飞书推送</span>
              </div>
              <div className="notice section-gap">会员仅控制账户有效期，不设置功能等级、自选股数量或方案数量限制。</div>
            </div>
            <div className="drawer-foot">
              <div className="help">未到期续期将从当前到期日顺延30天</div>
              <button className="btn primary" onClick={() => setShowRenewModal(true)}>使用邀请码续期</button>
            </div>
          </div>

          {/* 用户通知规则卡 */}
          <div className="card">
            <div className="card-head">
              <div>
                <div className="card-title">用户通知规则</div>
                <div className="card-sub">跨全部选股和监控组合方案生效</div>
              </div>
            </div>
            <div className="card-body">
              <div className="form-grid">
                <div className="form-row">
                  <label className="form-label">默认通知冷却</label>
                  <div className="input-group">
                    <input
                      className="input input-group-input"
                      value={cooldown}
                      onChange={(e) => setCooldown(e.target.value)}
                    />
                    <span className="btn input-group-suffix">分钟</span>
                  </div>
                </div>
                <div className="form-row">
                  <label className="form-label">时区</label>
                  <select className="select" defaultValue="Asia/Shanghai (UTC+8)">
                    <option>Asia/Shanghai (UTC+8)</option>
                  </select>
                </div>
                <div className="form-row">
                  <label className="form-label">静默开始</label>
                  <input
                    className="input"
                    type="time"
                    value={quietStart}
                    onChange={(e) => setQuietStart(e.target.value)}
                  />
                </div>
                <div className="form-row">
                  <label className="form-label">静默结束</label>
                  <input
                    className="input"
                    type="time"
                    value={quietEnd}
                    onChange={(e) => setQuietEnd(e.target.value)}
                  />
                </div>
              </div>
              <div className="toggle-row">
                <div>
                  <b>数据延迟时暂停通知</b>
                  <div className="help">分钟行情延迟超过 90 秒时不发送实时提醒。</div>
                </div>
                <button
                  className={`switch${pauseOnDelay ? ' on' : ''}`}
                  onClick={() => setPauseOnDelay((v) => !v)}
                  type="button"
                  aria-label="切换数据延迟时暂停通知"
                />
              </div>
            </div>
            <div className="drawer-foot">
              <button className="btn primary" onClick={handleSaveRules}>保存设置</button>
            </div>
          </div>

          {/* 方案消息订阅卡 */}
          <div className="card">
            <div className="card-head">
              <div>
                <div className="card-title">方案消息订阅</div>
                <div className="card-sub">按组合方案决定哪些消息进入第三方渠道</div>
              </div>
            </div>
            <div className="card-body">
              <div className="strategy-check-grid">
                <label className="strategy-check">
                  <input
                    type="checkbox"
                    className="checkbox"
                    checked={subs.selectionStrong}
                    onChange={(e) => setSubs((s) => ({ ...s, selectionStrong: e.target.checked }))}
                  />
                  选股组合「强势共振」
                </label>
                <label className="strategy-check">
                  <input
                    type="checkbox"
                    className="checkbox"
                    checked={subs.monitorNode}
                    onChange={(e) => setSubs((s) => ({ ...s, monitorNode: e.target.checked }))}
                  />
                  监控组合「节点共振追踪」
                </label>
                <label className="strategy-check">
                  <input
                    type="checkbox"
                    className="checkbox"
                    checked={subs.dsaSingle}
                    onChange={(e) => setSubs((s) => ({ ...s, dsaSingle: e.target.checked }))}
                  />
                  DSA 单策略过程消息
                </label>
                <label className="strategy-check">
                  <input
                    type="checkbox"
                    className="checkbox"
                    checked={subs.nodeSingle}
                    onChange={(e) => setSubs((s) => ({ ...s, nodeSingle: e.target.checked }))}
                  />
                  Node 单策略过程消息
                </label>
              </div>
            </div>
          </div>

          {/* 我的通知渠道卡 */}
          <div className="card">
            <div className="card-head">
              <div>
                <div className="card-title">我的通知渠道</div>
                <div className="card-sub">飞书凭据由当前用户本人维护</div>
              </div>
            </div>
            <div className="card-body stack">
              <div className="user-config-callout">
                <div className="callout-icon">飞</div>
                <div>
                  <b>用户自行填写飞书机器人信息</b>
                  <div className="help">填写 Webhook URL 和可选签名密钥；平台校验后加密保存，管理员无法查看明文。</div>
                </div>
              </div>
              {/* 站内消息渠道（系统默认，后端未返回时展示占位） */}
              {inAppChannels.length === 0 && (
                <div className="channel-card">
                  <div className="channel-logo">内</div>
                  <div className="channel-main">
                    <div className="channel-title">站内消息</div>
                    <div className="channel-meta">全部方案消息 · 系统默认</div>
                  </div>
                  <span className="status-pill ok">始终启用</span>
                </div>
              )}
              {inAppChannels.map((c) => (
                <div className="channel-card" key={c.id}>
                  <div className="channel-logo">内</div>
                  <div className="channel-main">
                    <div className="channel-title">{c.display_name || '站内消息'}</div>
                    <div className="channel-meta">全部方案消息 · 系统默认</div>
                  </div>
                  <span className="status-pill ok">始终启用</span>
                </div>
              ))}
              {/* 飞书渠道（后端未返回时展示占位） */}
              {feishuChannels.length === 0 && (
                <div className="channel-card">
                  <div className="channel-logo">飞</div>
                  <div className="channel-main">
                    <div className="channel-title">未配置飞书机器人</div>
                    <div className="channel-meta">点击右上角按钮添加配置</div>
                  </div>
                  <span className="status-pill off">未配置</span>
                </div>
              )}
              {feishuChannels.map((c) => (
                <div className="channel-card" key={c.id}>
                  <div className="channel-logo">飞</div>
                  <div className="channel-main">
                    <div className="channel-title">{c.display_name}</div>
                    <div className="channel-meta">{formatVerifyTime(c.last_verified_at)}</div>
                  </div>
                  <span className={`status-pill ${c.status === 'active' ? 'ok' : 'off'}`}>
                    {c.status === 'active' ? '可用' : '未验证'}
                  </span>
                  <button className="icon-btn" onClick={() => handleOpenEditFeishu(c)}>编辑</button>
                </div>
              ))}
            </div>
          </div>
        </section>

        {/* ===== 右栏 ===== */}
        <section className="stack">
          {/* 飞书消息预览卡 */}
          <div className="card">
            <div className="card-head">
              <div>
                <div className="card-title">飞书消息预览</div>
                <div className="card-sub">开发时按同一消息 Schema 渲染网页预览和飞书卡片</div>
              </div>
              <button
                className="btn small"
                onClick={handleSendPreview}
                disabled={previewNotification.isPending}
              >
                {previewNotification.isPending ? '发送中...' : '发送当前示例'}
              </button>
            </div>
            <div className="card-body">
              <div className="strategy-tabs-bar compact">
                {PREVIEW_TABS.map((t) => (
                  <button
                    key={t.key}
                    className={`strategy-tab${previewTab === t.key ? ' active' : ''}`}
                    onClick={() => setPreviewTab(t.key)}
                  >
                    {t.label}
                  </button>
                ))}
              </div>
              {previewTab === 'selector' && (
                <div className="strategy-panel active"><SelectorPreview /></div>
              )}
              {previewTab === 'monitor' && (
                <div className="strategy-panel active"><MonitorPreview /></div>
              )}
              {previewTab === 'system' && (
                <div className="strategy-panel active"><SystemPreview /></div>
              )}
            </div>
          </div>

          {/* 飞书配置步骤卡 */}
          <div className="card">
            <div className="card-head">
              <div>
                <div className="card-title">飞书配置步骤</div>
                <div className="card-sub">平台不要求用户提供飞书账号密码</div>
              </div>
            </div>
            <div className="card-body">
              <div className="steps">
                <div className="step-row">
                  <span className="step-num">1</span>
                  <div className="step-text">在飞书群设置中添加"自定义机器人"。</div>
                </div>
                <div className="step-row">
                  <span className="step-num">2</span>
                  <div className="step-text">复制 Webhook URL；启用签名校验时同时复制密钥。</div>
                </div>
                <div className="step-row">
                  <span className="step-num">3</span>
                  <div className="step-text">填写后发送测试消息，测试成功才允许启用。</div>
                </div>
                <div className="step-row">
                  <span className="step-num">4</span>
                  <div className="step-text">保存后只显示脱敏地址，敏感字段加密存储。</div>
                </div>
              </div>
            </div>
          </div>
        </section>
      </div>

      {/* 续期弹窗 */}
      {showRenewModal && (
        <RenewModal
          expiresAt={membership?.expires_at ?? null}
          onClose={() => setShowRenewModal(false)}
        />
      )}

      {/* 飞书配置弹窗 */}
      {showFeishuModal && (
        <FeishuModal
          editingChannel={editingChannel}
          onClose={() => setShowFeishuModal(false)}
        />
      )}
    </>
  )
}
