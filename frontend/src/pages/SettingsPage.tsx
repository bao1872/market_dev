// 通知与个人设置页（受保护路由）
// 对应原型：settings.html (V1.6.3)
//
// 用法：
// 1. 路由 /settings，受保护路由（经 ProtectedLayout 包裹）
// 2. 左栏 3 张卡片：会员状态 / 用户通知规则 / 我的通知渠道
// 3. 右栏 1 张卡片：飞书配置步骤
// 4. 两个弹窗：飞书配置弹窗（新建/编辑）、续期弹窗
// 5. 最近事件实测弹窗：展示最近事件摘要与诊断结果
//
// 依赖 hooks：
// - useMyMembership：会员状态卡（到期时间、剩余天数、环形进度）
// - useRenew：续期弹窗提交
// - useNotificationChannels：我的通知渠道列表
// - useCreateNotificationChannel：飞书配置弹窗"验证并保存"
// - useTestNotificationChannel：飞书配置弹窗"发送测试消息"（编辑已有渠道时）
// - useTestNotificationChannelLatestEvent：通知渠道卡"发送最近事件实测"

import { useState, useEffect, type CSSProperties } from 'react'
import {
  useMyMembership,
  useRenew,
  useNotificationChannels,
  useCreateNotificationChannel,
  useUpdateNotificationChannel,
  useDeleteNotificationChannel,
  useTestNotificationChannel,
  useTestNotificationChannelLatestEvent,
} from '@/hooks/useApi'
import { useToast } from '@/store/toast'
import type { NotificationChannel, ChannelLatestEventTestResponse } from '@/api/endpoints'

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
  appId: string
  appSecret: string
  receiveId: string
  receiveIdType: string
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
  const updateChannel = useUpdateNotificationChannel()
  const testChannel = useTestNotificationChannel()

  const emptyForm: FeishuFormState = {
    displayName: '',
    appId: '',
    appSecret: '',
    receiveId: '',
    receiveIdType: 'user_id',
  }

  const [form, setForm] = useState<FeishuFormState>(emptyForm)
  const [hasMaskedSecret, setHasMaskedSecret] = useState(false)

  // 编辑已有渠道时回填表单；新建时重置
  useEffect(() => {
    if (editingChannel) {
      const cfg = editingChannel.target_config || {}
      const rawSecret = String(cfg.app_secret ?? '')
      // 脱敏值（****xxxx）不在输入框显示，清空后用 placeholder 提示
      const isMasked = rawSecret.startsWith('****')
      setHasMaskedSecret(isMasked)
      setForm({
        displayName: editingChannel.display_name ?? '',
        appId: String(cfg.app_id ?? ''),
        appSecret: isMasked ? '' : rawSecret,
        receiveId: String(cfg.receive_id ?? ''),
        receiveIdType: String(cfg.receive_id_type ?? 'user_id'),
      })
    } else {
      setHasMaskedSecret(false)
      setForm(emptyForm)
    }
  }, [editingChannel])

  // 通用字段更新
  const handleField = <K extends keyof FeishuFormState>(key: K, value: FeishuFormState[K]) => {
    setForm((prev) => ({ ...prev, [key]: value }))
  }

  // 验证并保存：只支持平台应用通知模式
  const handleSave = () => {
    if (!form.displayName.trim()) {
      toast.show('保存失败', '请填写配置名称')
      return
    }
    if (!form.appId.trim()) {
      toast.show('保存失败', '请填写飞书 App ID')
      return
    }
    if (!form.appSecret.trim() && !hasMaskedSecret) {
      toast.show('保存失败', '请填写飞书 App Secret')
      return
    }
    if (!form.receiveId.trim()) {
      toast.show('保存失败', '请填写接收者 ID')
      return
    }

    const targetConfig: Record<string, unknown> = {
      app_id: form.appId.trim(),
      receive_id: form.receiveId.trim(),
      receive_id_type: form.receiveIdType,
    }
    // 仅在用户输入了新 secret 时才发送（编辑时脱敏值已被清除）
    if (form.appSecret.trim()) {
      targetConfig.app_secret = form.appSecret.trim()
    }

    if (editingChannel) {
      // 编辑模式：调用 UPDATE
      updateChannel.mutate(
        {
          channelId: editingChannel.id,
          data: {
            display_name: form.displayName.trim(),
            target_config: targetConfig,
          },
        },
        {
          onSuccess: () => {
            toast.show('保存成功', '飞书应用通知配置已更新')
            onClose()
          },
          onError: (err: unknown) => {
            const axiosErr = err as { response?: { data?: { detail?: string } } }
            const message = axiosErr.response?.data?.detail ?? '保存失败，请检查配置'
            toast.show('保存失败', message)
          },
        },
      )
    } else {
      // 新建模式：调用 CREATE
      createChannel.mutate(
        {
          adapter_type: 'feishu_platform_app',
          display_name: form.displayName.trim(),
          target_config: targetConfig,
        },
        {
          onSuccess: () => {
            toast.show('保存成功', '飞书应用通知配置已保存')
            onClose()
          },
          onError: (err: unknown) => {
            const axiosErr = err as { response?: { data?: { detail?: string } } }
            const message = axiosErr.response?.data?.detail ?? '保存失败，请检查配置'
            toast.show('保存失败', message)
          },
        },
      )
    }
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
          toast.show('测试成功', '测试消息已发送到飞书')
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
            <b>配置飞书通知</b>
            <div className="card-sub">填写飞书平台应用凭证</div>
          </div>
          <button className="icon-btn" onClick={onClose}>×</button>
        </div>
        <div className="modal-body">
          <div className="notice">平台只调用该渠道发送策略消息，不会获得飞书账号、联系人或群聊读取权限。</div>
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
              <label className="form-label">飞书 App ID</label>
              <input
                className="input"
                type="text"
                value={form.appId}
                onChange={(e) => handleField('appId', e.target.value)}
                placeholder="如 cli_a6b37d1d077b900e"
              />
              <div className="help">在飞书开放平台「凭证与基础信息」中获取。</div>
            </div>
            <div className="form-row full">
              <label className="form-label">飞书 App Secret</label>
              <input
                  className="input"
                  type="password"
                  value={form.appSecret}
                  onChange={(e) => handleField('appSecret', e.target.value)}
                  placeholder={hasMaskedSecret ? '已保存，重新输入可修改' : '飞书应用凭证密钥'}
                />
              <div className="help">加密存储，仅用于获取 tenant_access_token。</div>
            </div>
            <div className="form-row full">
              <label className="form-label">接收者 ID</label>
              <input
                className="input"
                type="text"
                value={form.receiveId}
                onChange={(e) => handleField('receiveId', e.target.value)}
                placeholder="如 bg332537"
              />
            </div>
            <div className="form-row full">
              <label className="form-label">接收者类型</label>
              <select
                className="select"
                value={form.receiveIdType}
                onChange={(e) => handleField('receiveIdType', e.target.value)}
              >
                <option value="user_id">User ID</option>
                <option value="open_id">Open ID</option>
              </select>
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
            disabled={createChannel.isPending || updateChannel.isPending}
          >
            {createChannel.isPending || updateChannel.isPending ? '保存中...' : '验证并保存'}
          </button>
        </div>
      </div>
    </div>
  )
}

// ===== 最近事件实测弹窗 =====

function LatestEventTestModal({
  channel,
  result,
  error,
  onClose,
}: {
  channel: NotificationChannel
  result: ChannelLatestEventTestResponse | null
  error: string | null
  onClose: () => void
}) {
  const delivery = result?.delivery
  const diagnostics = result?.diagnostics ?? {}
  const eventSummary =
    (diagnostics.event_summary as string | undefined) ||
    (diagnostics.summary as string | undefined) ||
    (delivery?.success ? '最近事件已投递' : '暂无事件摘要')

  return (
    <div className="modal-backdrop open" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <div>
            <b>最近事件实测</b>
            <div className="card-sub">{channel.display_name}</div>
          </div>
          <button className="icon-btn" onClick={onClose}>×</button>
        </div>
        <div className="modal-body">
          {error && <div className="notice error">{error}</div>}
          {!error && !delivery && <div className="notice">正在获取最近事件与诊断结果…</div>}
          {delivery && (
            <>
              <div className={delivery.success ? 'notice' : 'notice error'}>
                投递结果：{delivery.success ? '成功' : '失败'}
                {delivery.error_message ? ` · ${delivery.error_message}` : ''}
              </div>
              <div className="card section-gap">
                <div className="card-head">
                  <div className="card-title">最近事件摘要</div>
                </div>
                <div className="card-body">
                  <p>{eventSummary}</p>
                </div>
              </div>
              <div className="card section-gap">
                <div className="card-head">
                  <div className="card-title">诊断详情</div>
                </div>
                <div className="card-body">
                  <pre className="json-snapshot">{JSON.stringify(diagnostics, null, 2)}</pre>
                </div>
              </div>
            </>
          )}
        </div>
        <div className="modal-foot">
          <button className="btn" onClick={onClose}>关闭</button>
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
  const deleteChannel = useDeleteNotificationChannel()
  const latestEventTest = useTestNotificationChannelLatestEvent()

  const [showRenewModal, setShowRenewModal] = useState(false)
  const [showFeishuModal, setShowFeishuModal] = useState(false)
  const [editingChannel, setEditingChannel] = useState<NotificationChannel | null>(null)
  const [latestEventChannel, setLatestEventChannel] = useState<NotificationChannel | null>(null)
  const [latestEventResult, setLatestEventResult] = useState<ChannelLatestEventTestResponse | null>(null)
  const [latestEventError, setLatestEventError] = useState<string | null>(null)

  // 通知规则表单状态
  const [cooldown, setCooldown] = useState('10')
  const [quietStart, setQuietStart] = useState('22:30')
  const [quietEnd, setQuietEnd] = useState('08:30')
  const [pauseOnDelay, setPauseOnDelay] = useState(true)

  const membership = membershipQuery.data
  const remainingDays = membership?.remaining_days ?? 0
  // 环形进度百分比：剩余天数 / 30 天，上限 100%
  const ringPct = Math.min((remainingDays / 30) * 100, 100)
  const ringStyle = { '--ring-pct': `${ringPct}%` } as CSSProperties

  const channels = channelsQuery.data?.items ?? []
  // 站内消息渠道（系统默认）
  const inAppChannels = channels.filter((c) => c.adapter_type === 'in_app' || c.adapter_type === 'mock')
  // 飞书渠道
  const feishuChannels = channels.filter((c) => c.adapter_type === 'feishu_platform_app')

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

  // 发送最近事件实测
  const handleTestLatestEvent = (channel: NotificationChannel) => {
    setLatestEventChannel(channel)
    setLatestEventResult(null)
    setLatestEventError(null)
    latestEventTest.mutate(channel.id, {
      onSuccess: (data) => {
        setLatestEventResult(data)
      },
      onError: (err: unknown) => {
        const axiosErr = err as { response?: { data?: { detail?: string } } }
        setLatestEventError(axiosErr.response?.data?.detail ?? '实测请求失败，请稍后重试')
      },
    })
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1 className="page-title">通知与个人设置</h1>
          <div className="page-desc">飞书应用通知由用户自行配置；右侧可预览真实推送消息结构</div>
        </div>
        <div className="actions">
          {feishuChannels.length === 0 && (
            <button className="btn primary" onClick={handleOpenNewFeishu}>＋ 配置飞书通知</button>
          )}
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
              <div className="notice section-gap">会员仅控制账户有效期，不设置功能等级、自选股数量或策略数量限制。</div>
            </div>
            <div className="drawer-foot">
              <div className="help">未到期续期将从当前到期日顺延30天</div>
              <button className="btn primary" onClick={() => setShowRenewModal(true)}>使用邀请码续期</button>
            </div>
          </div>

          {/* 用户通知规则卡 */}
          <div className="card" style={{ opacity: 0.5, pointerEvents: 'none' }}>
            <div className="card-head">
              <div>
                <div className="card-title">用户通知规则 <span className="status-pill off">尚未接入后端</span></div>
                <div className="card-sub">跨选股策略与自选股监控生效</div>
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
              <button className="btn primary" disabled>保存设置</button>
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
                  <b>用户自行填写飞书应用通知配置</b>
                  <div className="help">填写飞书 App ID / App Secret / 接收者 ID；平台校验后加密保存，管理员无法查看明文。</div>
                </div>
              </div>
              {/* 站内消息渠道（系统默认，后端未返回时展示占位） */}
              {inAppChannels.length === 0 && (
                <div className="channel-card">
                  <div className="channel-logo">内</div>
                  <div className="channel-main">
                    <div className="channel-title">站内消息</div>
                    <div className="channel-meta">全部策略消息 · 系统默认</div>
                  </div>
                  <span className="status-pill ok">始终启用</span>
                </div>
              )}
              {inAppChannels.map((c) => (
                <div className="channel-card" key={c.id}>
                  <div className="channel-logo">内</div>
                  <div className="channel-main">
                    <div className="channel-title">{c.display_name || '站内消息'}</div>
                    <div className="channel-meta">全部策略消息 · 系统默认</div>
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
                    <div className="channel-title">
                      {c.display_name}
                      <span className="channel-type-badge">应用通知</span>
                    </div>
                    <div className="channel-meta">{formatVerifyTime(c.last_verified_at)}</div>
                  </div>
                  <span className={`status-pill ${c.status === 'active' ? 'ok' : 'off'}`}>
                    {c.status === 'active' ? '可用' : '未验证'}
                  </span>
                  <button className="icon-btn" onClick={() => handleOpenEditFeishu(c)}>编辑</button>
                  <button
                    className="btn small"
                    onClick={() => handleTestLatestEvent(c)}
                    disabled={latestEventTest.isPending}
                  >
                    {latestEventTest.isPending && latestEventChannel?.id === c.id ? '实测中...' : '发送最近事件实测'}
                  </button>
                  <button
                    className="btn small danger"
                    onClick={() => {
                      if (confirm('确定要删除此飞书通知渠道吗？')) {
                        deleteChannel.mutate(c.id, {
                          onSuccess: () => {
                            toast.show('已删除', '飞书通知渠道已删除')
                          },
                        })
                      }
                    }}
                  >
                    删除
                  </button>
                </div>
              ))}
            </div>
          </div>
        </section>

        {/* ===== 右栏 ===== */}
        <section className="stack">
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
                  <div className="step-text">在飞书开放平台创建企业自建应用，并开启机器人能力。</div>
                </div>
                <div className="step-row">
                  <span className="step-num">2</span>
                  <div className="step-text">在「凭证与基础信息」中复制 App ID 和 App Secret。</div>
                </div>
                <div className="step-row">
                  <span className="step-num">3</span>
                  <div className="step-text">在「权限管理」中授予发送消息相关权限（im:chat:readonly、im:message:send_as_bot）。</div>
                </div>
                <div className="step-row">
                  <span className="step-num">4</span>
                  <div className="step-text">填写接收者 User ID，发送测试消息，测试成功才允许启用。</div>
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

      {/* 最近事件实测弹窗 */}
      {latestEventChannel && (
        <LatestEventTestModal
          channel={latestEventChannel}
          result={latestEventResult}
          error={latestEventError}
          onClose={() => {
            setLatestEventChannel(null)
            setLatestEventResult(null)
            setLatestEventError(null)
          }}
        />
      )}
    </>
  )
}
