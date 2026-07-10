// 登录/注册页（公开路由）
// 对应原型：login.html（V1.6.3）
// 用法：路由 /login 渲染此页面，支持登录与邀请码注册双 tab
// [Auth] - 描述: 登录成功后直接使用后端返回的 next_route 跳转（替代旧会员到期判断逻辑）
// 注册成功后展示成功页，点击"进入服务台"复用注册返回的 token 完成登录
import { useState, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuthStore } from '@/store/auth'
import type { AuthUser } from '@/store/auth'
import { useToast } from '@/store/toast'
import { useLogin, useRegister } from '@/hooks/useApi'
import { getMe, getMyAccess } from '@/api/endpoints'
import type { LoginResponse, RegisterSuccessResponse } from '@/api/endpoints'
import BrandLogo from '@/components/BrandLogo'

// 从 axios 错误中提取可读消息（FastAPI 错误体通常在 response.data.detail）
function getErrorMessage(error: unknown): string {
  if (error && typeof error === 'object' && 'response' in error) {
    const response = (error as { response?: { data?: { detail?: string } | string } }).response
    if (response?.data) {
      if (typeof response.data === 'string') return response.data
      if (response.data.detail) return response.data.detail
    }
  }
  if (error instanceof Error) return error.message
  return '操作失败，请稍后重试'
}

// ISO 日期字符串截取 YYYY-MM-DD（后端返回的 started_at/expires_at 为 ISO 格式）
function formatDate(isoStr: string): string {
  return isoStr.slice(0, 10)
}

export default function LoginPage() {
  const navigate = useNavigate()
  const loginMutation = useLogin()
  const registerMutation = useRegister()

  // tab 状态：login | register
  const [activeTab, setActiveTab] = useState<'login' | 'register'>('login')
  // 注册成功后展示成功页（保存注册响应以显示会员信息）
  const [registerResult, setRegisterResult] = useState<RegisterSuccessResponse | null>(null)
  // 登录流程进行中（含 getMe 拉取用户信息阶段）
  const [authenticating, setAuthenticating] = useState(false)
  // 登录提交阶段：防止表单快速双击或回车触发重复 login 请求
  const [isSubmitting, setIsSubmitting] = useState(false)
  const submittingRef = useRef(false)

  // 登录表单状态
  const [loginAccount, setLoginAccount] = useState('')
  const [loginPassword, setLoginPassword] = useState('')
  const [keepLogin, setKeepLogin] = useState(true)

  // 注册表单状态
  const [regEmail, setRegEmail] = useState('')
  const [regPassword, setRegPassword] = useState('')
  const [regPassword2, setRegPassword2] = useState('')
  const [regInvite, setRegInvite] = useState('')
  const [regTerms, setRegTerms] = useState(false)
  const [registerError, setRegisterError] = useState('')

  // 邀请码实时校验：长度 >= 8 视为格式有效（实际校验由后端完成）
  const inviteValid = regInvite.length >= 8

  // [Auth] - 描述: 登录成功后的统一处理 - 直接使用 login 响应的 AccessProfile 字段构造 AuthUser
  // 不再调用 getMe 二次请求（login 响应已含 is_admin/roles/subscription_active 等权限上下文）
  // 跳转目标由后端权威计算的 next_route 决定（admin→/admin/overview；member active→/market；expired→/subscription-expired）
  async function handleLoginSuccess(data: LoginResponse, keepLogin: boolean) {
    setAuthenticating(true)
    try {
      // 先 login 写入 token + storage（根据 keepLogin 选 local/session），user 暂为 null
      // 让 axios 拦截器能从 storage 读取 token
      useAuthStore.getState().login(data.access_token, null, data.refresh_token, keepLogin)
      // [Auth] - 描述: 直接使用 login 响应的 AccessProfile 字段构造 AuthUser（避免再调 getMe）
      const user: AuthUser = {
        id: '', // login 响应不含 user_id，由后续 getMe 或 /me/access 补全；路由守卫仅依赖 is_admin/subscription_active
        name: '',
        email: '',
        is_admin: data.is_admin,
        roles: data.roles,
        subscription_active: data.subscription_active,
        plan_code: data.plan_code,
        plan_display_name: data.plan_display_name,
        expires_at: data.expires_at,
        features: data.features,
        limits: data.limits,
      }
      // 异步补全 user.id/email/name（不阻塞跳转，路由守卫不依赖这些字段）
      getMe()
        .then((me) => {
          useAuthStore.getState().setUser({
            ...user,
            id: me.id,
            email: me.email,
            name: me.email,
          })
        })
        .catch(() => {
          // getMe 失败不阻塞跳转，权限上下文已由 login 响应提供
        })
      useAuthStore.getState().setUser(user)
      useToast.getState().show('登录成功', '已进入盘迹')
      // [Auth] - 描述: 使用后端返回的 next_route 跳转（权威路由分发，前端不再本地判断会员到期状态）
      navigate(data.next_route)
    } catch (err) {
      // 登录流程异常：logout 清除 token + store 状态，避免残留无效登录态
      useAuthStore.getState().logout()
      useToast.getState().show('登录失败', getErrorMessage(err))
    } finally {
      setAuthenticating(false)
    }
  }

  // 登录提交（防重复：submittingRef 阻止并发，isSubmitting 控制按钮状态与文案）
  async function handleLoginSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (submittingRef.current || isSubmitting) return
    if (!loginAccount.trim() || !loginPassword.trim()) {
      useToast.getState().show('请填写完整', '邮箱与密码不能为空')
      return
    }
    submittingRef.current = true
    setIsSubmitting(true)
    try {
      const data = await loginMutation.mutateAsync({
        email: loginAccount.trim(),
        password: loginPassword,
      })
      await handleLoginSuccess(data, keepLogin)
    } catch (err) {
      useToast.getState().show('登录失败', getErrorMessage(err))
    } finally {
      submittingRef.current = false
      setIsSubmitting(false)
    }
  }

  // 注册提交
  function handleRegisterSubmit(e: React.FormEvent) {
    e.preventDefault()
    setRegisterError('')
    // 表单校验
    if (!regEmail.trim()) return setRegisterError('请填写邮箱')
    if (regPassword.length < 8) return setRegisterError('密码至少 8 位')
    if (regPassword !== regPassword2) return setRegisterError('两次输入的密码不一致')
    if (regInvite.length < 8) return setRegisterError('邀请码至少 8 位')
    if (!regTerms) return setRegisterError('请阅读并同意服务协议')

    registerMutation.mutate(
      {
        email: regEmail.trim(),
        password: regPassword,
        invite_code: regInvite,
      },
      {
        onSuccess: (data) => {
          setRegisterResult(data)
          useToast.getState().show('注册成功', '会员已开通，有效期 30 天')
        },
        onError: (err) => setRegisterError(getErrorMessage(err)),
      },
    )
  }

  // [Auth] - 描述: 注册成功页"进入服务台"按钮 - 复用注册返回的 token 走 login 流程拉取完整 AccessProfile
  // 新注册用户会员刚开通，subscription_active 必为 true，next_route 必为 /overview
  // 注册流程默认保持登录（keepLogin=true），与登录页 keepLogin 复选框无关
  async function handleEnterService() {
    if (!registerResult) return
    // 注册响应不含 AccessProfile 字段，需先 login 写 token 后再调 /me/access 获取权限上下文
    setAuthenticating(true)
    try {
      useAuthStore.getState().login(
        registerResult.access_token,
        null,
        registerResult.refresh_token,
        true,
      )
      // [Auth] - 描述: 注册成功后必须拉取 /me/access 获取完整 AccessProfile 构造 AuthUser
      // 注册响应只有 token + 会员时间，无 is_admin/roles/subscription_active 等权限字段
      const access = await getMyAccess()
      const user: AuthUser = {
        id: access.user_id,
        name: '',
        email: '',
        is_admin: access.is_admin,
        roles: access.roles,
        subscription_active: access.subscription_active,
        plan_code: access.plan_code,
        plan_display_name: access.plan_display_name,
        expires_at: access.expires_at,
        features: access.features,
        limits: access.limits,
      }
      // 异步补全 email/name（不阻塞跳转）
      getMe()
        .then((me) => {
          useAuthStore.getState().setUser({
            ...user,
            name: me.email,
            email: me.email,
          })
        })
        .catch(() => {
          // getMe 失败不阻塞跳转
        })
      useAuthStore.getState().setUser(user)
      useToast.getState().show('登录成功', '已进入盘迹')
      // [Auth] - 描述: 注册后默认跳转 /market（新注册订阅已激活，无 next_route 字段）
      navigate('/market')
    } catch (err) {
      useAuthStore.getState().logout()
      useToast.getState().show('获取权限信息失败', getErrorMessage(err))
    } finally {
      setAuthenticating(false)
    }
  }

  // 邀请码输入：实时大写化
  function handleInviteChange(e: React.ChangeEvent<HTMLInputElement>) {
    setRegInvite(e.target.value.toUpperCase())
  }

  return (
    <div className="login-page auth-page">
      {/* 左侧视觉区 */}
      <section className="login-visual">
        <div className="login-grid"></div>
        <div className="login-copy">
          <BrandLogo variant="sidebar" />
          <h1 className="login-title">
            把看盘经验，变成
            <br />
            可计算、可追踪的服务
          </h1>
          <p className="login-lead">
            注册后用邀请码激活套餐，按所选套餐解锁选股、监控、个股指标与消息推送功能。
          </p>
          <div className="login-feature">
            <span className="tag info">邀请码激活套餐</span>
            <span className="tag good">按套餐解锁功能</span>
            <span className="tag warn">到期前可续期</span>
          </div>
          <div className="auth-flow-mini">
            <div>
              <i>1</i>
              <span>填写账户资料</span>
            </div>
            <b></b>
            <div>
              <i>2</i>
              <span>验证邀请码</span>
            </div>
            <b></b>
            <div>
              <i>3</i>
              <span>进入服务台</span>
            </div>
          </div>
        </div>
      </section>

      {/* 右侧表单区 */}
      <section className="login-form-wrap">
        <div className="login-card auth-card">
          {/* 双 tab 切换 */}
          <div className="auth-tabs" role="tablist">
            <button
              className={`auth-tab ${activeTab === 'login' ? 'active' : ''}`}
              type="button"
              onClick={() => setActiveTab('login')}
            >
              登录
            </button>
            <button
              className={`auth-tab ${activeTab === 'register' ? 'active' : ''}`}
              type="button"
              onClick={() => setActiveTab('register')}
            >
              注册会员
            </button>
          </div>

          {/* 登录面板 */}
          <div className={`auth-panel ${activeTab === 'login' ? 'active' : ''}`}>
            <h2>欢迎回来</h2>
            <p className="page-desc">登录你的策略服务账户</p>
            <form onSubmit={handleLoginSubmit} noValidate>
              <div className="form-row">
                <label className="form-label">邮箱</label>
                <input
                  className="input"
                  type="email"
                  value={loginAccount}
                  onChange={(e) => setLoginAccount(e.target.value)}
                  placeholder="邮箱"
                />
              </div>
              <div className="form-row">
                <label className="form-label">密码</label>
                <input
                  className="input"
                  type="password"
                  value={loginPassword}
                  onChange={(e) => setLoginPassword(e.target.value)}
                  placeholder="密码"
                />
              </div>
              <div className="toggle-row">
                <label>
                  <input
                    type="checkbox"
                    className="checkbox"
                    checked={keepLogin}
                    onChange={(e) => setKeepLogin(e.target.checked)}
                  />
                  保持登录
                </label>
                <a className="forgot-link">忘记密码</a>
              </div>
              <button
                className="btn primary auth-submit"
                type="submit"
                disabled={isSubmitting || authenticating}
              >
                {isSubmitting || authenticating ? '登录中...' : '登录服务台'}
              </button>
            </form>
            <div className="auth-switch-copy">
              还没有账户？
              <button type="button" className="link-btn" onClick={() => setActiveTab('register')}>
                使用邀请码注册
              </button>
            </div>
          </div>

          {/* 注册面板 */}
          <div className={`auth-panel ${activeTab === 'register' ? 'active' : ''}`}>
            {registerResult ? (
              // 注册成功页
              <div className="register-success">
                <div className="success-orb">✓</div>
                <h2>注册成功，套餐已激活</h2>
                <p>你的账户已按所选套餐解锁对应功能权限。</p>
                <div className="membership-result">
                  <div>
                    <span>套餐状态</span>
                    <b className="pos">有效</b>
                  </div>
                  <div>
                    <span>生效时间</span>
                    <b>{formatDate(registerResult.membership_started_at)}</b>
                  </div>
                  <div>
                    <span>到期时间</span>
                    <b>{formatDate(registerResult.membership_expires_at)}</b>
                  </div>
                  <div>
                    <span>功能权限</span>
                    <b>按套餐开放</b>
                  </div>
                </div>
                <div className="notice">
                  到期前可在"通知与设置 → 会员状态"中再次输入邀请码续期。
                </div>
                <button
                  className="btn primary auth-submit"
                  type="button"
                  onClick={handleEnterService}
                  disabled={authenticating}
                >
                  {authenticating ? '进入中...' : '进入盘迹'}
                </button>
              </div>
            ) : (
              // 注册表单
              <div>
                <h2>注册会员</h2>
                <p className="page-desc">邀请码验证通过后自动获得30天会员</p>
                <form onSubmit={handleRegisterSubmit} noValidate>
                  <div className="form-grid auth-form-grid">
                    <div className="form-row">
                      <label className="form-label">邮箱</label>
                      <input
                        className="input"
                        type="email"
                        value={regEmail}
                        onChange={(e) => setRegEmail(e.target.value)}
                        placeholder="邮箱"
                      />
                    </div>
                    <div className="form-row">
                      <label className="form-label">密码</label>
                      <input
                        className="input"
                        type="password"
                        value={regPassword}
                        onChange={(e) => setRegPassword(e.target.value)}
                        placeholder="至少 8 位"
                      />
                    </div>
                    <div className="form-row">
                      <label className="form-label">确认密码</label>
                      <input
                        className="input"
                        type="password"
                        value={regPassword2}
                        onChange={(e) => setRegPassword2(e.target.value)}
                        placeholder="再次输入密码"
                      />
                    </div>
                    <div className="form-row full">
                      <label className="form-label">
                        邀请码 <span className="required-mark">必填</span>
                      </label>
                      <div className="invite-input-wrap">
                        <input
                          className="input invite-code-input"
                          value={regInvite}
                          onChange={handleInviteChange}
                          placeholder="输入邀请码"
                          autoComplete="off"
                        />
                        {inviteValid && (
                          <span className="invite-validation good">
                            ✓ 邀请码有效 · 激活30天
                          </span>
                        )}
                      </div>
                      <div className="help">
                        邀请码只能兑换一次，可用于新用户注册或现有会员续期。
                      </div>
                    </div>
                  </div>
                  <label className="terms-row">
                    <input
                      type="checkbox"
                      className="checkbox"
                      checked={regTerms}
                      onChange={(e) => setRegTerms(e.target.checked)}
                    />
                    我已阅读并同意服务协议与隐私政策
                  </label>
                  <div className="form-error">{registerError}</div>
                  <button
                    className="btn primary auth-submit"
                    type="submit"
                    disabled={registerMutation.isPending}
                  >
                    {registerMutation.isPending ? '注册中...' : '验证邀请码并注册'}
                  </button>
                </form>
                <div className="auth-switch-copy">
                  已有账户？
                  <button type="button" className="link-btn" onClick={() => setActiveTab('login')}>
                    直接登录
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>
      </section>
    </div>
  )
}
