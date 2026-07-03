// [Auth] - 描述: 订阅到期续期拦截页（公开路由）
// 对应原型：membership-expired.html (V1.6.3)
//
// 用法：
// 1. 在路由配置中注册为公开路由 /subscription-expired（canonical，不经过 ProtectedLayout）
// 2. [Auth] - 描述: 当后端 next_route='/subscription-expired' 时（subscription_active=false）跳转到此页引导续期
// 3. 跳转时可通过 route state 传递 { email, expiresAt } 以展示原账户信息：
//    navigate('/subscription-expired', { state: { email, expiresAt } })
//
// 交互：
// - 邀请码输入时自动大写化，清洁长度（仅字母数字）≥8 时显示"✓ 邀请码格式正确"
// - 点击"兑换并恢复全部功能"调用 useRenew hook，成功后隐藏表单并显示内联成功提示
// - [Auth] - 描述: 续期成功后调用 /me/access 刷新 AuthUser.subscription_active，再跳转 /overview
// - 成功提示中"进入服务台"按钮跳转 /overview；"返回登录"链接跳转 /login
import { useState } from 'react'
import { useNavigate, useLocation } from 'react-router-dom'
import { useRenew } from '@/hooks/useApi'
import { useAuthStore } from '@/store/auth'
import type { AuthUser } from '@/store/auth'
import { useToast } from '@/store/toast'
import { getMyAccess } from '@/api/endpoints'
import type { RenewSuccessResponse } from '@/api/endpoints'
import BrandLogo from '@/components/BrandLogo'

// 续期页路由 state 类型（跳转方可选传入）
interface SubscriptionExpiredRouteState {
  email?: string
  expiresAt?: string
}

// 将 ISO 日期字符串格式化为 YYYY-MM-DD
function formatDate(iso: string | null | undefined): string {
  if (!iso) return '—'
  return iso.split('T')[0]
}

// 邀请码清洁长度（仅字母数字，与原型 app.js 校验逻辑一致）
function cleanLength(code: string): number {
  return code.replace(/[^A-Za-z0-9]/g, '').length
}

export default function SubscriptionExpiredPage() {
  const navigate = useNavigate()
  const location = useLocation()
  const renew = useRenew()
  const user = useAuthStore((s) => s.user)
  const toast = useToast()

  // 从 route state 读取跳转时传入的账户信息（可选）
  const routeState = location.state as SubscriptionExpiredRouteState | null

  const [inviteCode, setInviteCode] = useState('')
  const [error, setError] = useState('')
  const [success, setSuccess] = useState<RenewSuccessResponse | null>(null)

  // 邀请码实时校验：输入时大写化，长度≥8 显示"✓ 邀请码格式正确"
  const handleInviteChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    setInviteCode(e.target.value.toUpperCase())
    if (error) setError('')
  }

  // [Auth] - 描述: 续期提交 - 校验通过后调用 useRenew，成功后刷新 AccessProfile 并跳转
  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (cleanLength(inviteCode) < 8) {
      setError('请输入有效的邀请码。')
      return
    }
    setError('')
    renew.mutate(inviteCode, {
      onSuccess: (data) => {
        setSuccess(data)
        toast.show('续期成功', `会员有效期已更新至 ${formatDate(data.new_expires_at)}`)
        // [Auth] - 描述: 续期成功后异步刷新 AuthUser.subscription_active 等字段
        // 避免用户进入 /overview 时被 SubscriberRoute 拦回 /subscription-expired
        getMyAccess()
          .then((access) => {
            const currentUser = useAuthStore.getState().user
            if (!currentUser) return
            const updated: AuthUser = {
              ...currentUser,
              is_admin: access.is_admin,
              roles: access.roles,
              subscription_active: access.subscription_active,
              plan_code: access.plan_code,
              plan_display_name: access.plan_display_name,
              expires_at: access.expires_at,
              features: access.features,
              limits: access.limits,
            }
            useAuthStore.getState().setUser(updated)
          })
          .catch(() => {
            // 刷新失败不阻塞 UI，用户可手动刷新或重新登录
          })
      },
      onError: (err: unknown) => {
        // 尝试从 axios 错误响应中提取后端 detail 信息
        const axiosErr = err as { response?: { data?: { detail?: string } } }
        const message = axiosErr.response?.data?.detail ?? '邀请码无效或已使用'
        setError(message)
        toast.show('续期失败', message)
      },
    })
  }

  // [Auth] - 描述: 进入服务台 - 续期成功后跳转 /overview（SubscriberRoute 校验已刷新的 subscription_active）
  const handleEnter = () => {
    navigate('/overview')
  }

  // 返回登录
  const handleBackLogin = (e: React.MouseEvent) => {
    e.preventDefault()
    navigate('/login')
  }

  // [Auth] - 描述: 账户邮箱优先取 auth store，其次 route state（不再硬编码演示邮箱）
  const email = user?.email ?? routeState?.email ?? ''
  // 原到期时间取 route state（未传入时显示 —）
  const oldExpiresAt = routeState?.expiresAt ?? null
  // 邀请码格式是否有效
  const isValid = cleanLength(inviteCode) >= 8

  return (
    <div className="renew-gate-page">
      <div className="renew-gate-shell">
        {/* 品牌标识 */}
        <div className="renew-gate-brand">
          <BrandLogo variant="sidebar" />
          <div>
            <b>盘迹</b>
            <span>MEMBERSHIP RENEWAL</span>
          </div>
        </div>

        <div className="renew-gate-card">
          {/* 续期图标 */}
          <div className="renew-icon">⌛</div>

          {/* 到期标签 */}
          <span className="tag warn">会员已到期</span>

          <h1>续期后继续使用全部功能</h1>
          <p>
            你的账户仍然保留，选股方案、自选股、监控记录和通知配置不会被删除。输入新的邀请码即可恢复全部功能。
          </p>

          {/* 四宫格信息 */}
          <div className="membership-result compact">
            <div>
              <span>账户</span>
              <b>{email}</b>
            </div>
            <div>
              <span>原到期时间</span>
              <b>{formatDate(oldExpiresAt)}</b>
            </div>
            <div>
              <span>续期规则</span>
              <b>每个邀请码 +30 天</b>
            </div>
            <div>
              <span>数据状态</span>
              <b className="pos">完整保留</b>
            </div>
          </div>

          {/* 续期表单（成功后隐藏） */}
          {!success && (
            <form className="renew-form" onSubmit={handleSubmit}>
              <div className="form-row full">
                <label className="form-label">续期邀请码</label>
                <div className="invite-input-wrap">
                  <input
                    className="input invite-code-input"
                    value={inviteCode}
                    onChange={handleInviteChange}
                    placeholder="请输入邀请码"
                    required
                  />
                  {isValid && (
                    <span className="invite-validation good">✓ 邀请码格式正确</span>
                  )}
                </div>
              </div>
              <div className="form-error">{error}</div>
              <button
                className="btn primary auth-submit"
                type="submit"
                disabled={renew.isPending}
              >
                {renew.isPending ? '兑换中...' : '兑换并恢复全部功能'}
              </button>
            </form>
          )}

          {/* 续期成功内联提示（表单隐藏后显示） */}
          {success && (
            <div className="renew-success-inline show">
              <div className="success-orb small">✓</div>
              <div>
                <b>续期成功</b>
                <span>
                  会员有效期已更新至 {formatDate(success.new_expires_at)}，全部功能已恢复。
                </span>
              </div>
              <button className="btn primary" type="button" onClick={handleEnter}>
                进入服务台
              </button>
            </div>
          )}

          {/* 返回登录 */}
          <a className="back-login" href="/login" onClick={handleBackLogin}>
            ← 返回登录
          </a>
        </div>

        {/* 底部规则说明 */}
        <div className="renew-rule-note">
          邀请码为一次性兑换码；已过期账户从兑换当天起计算30天，未到期账户从原到期日顺延30天。
        </div>
      </div>
    </div>
  )
}
