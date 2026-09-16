// Auth / Access 领域 API owner
//
// 职责：当前登录用户的认证与自身访问上下文
//   - 认证：login / register / renew / refreshToken / changePassword
//   - 自身访问上下文：getMe(/v1/me) / getMyAccess(/v1/me/access) / getMyMembership(/v1/me/membership)
//
// 边界（S3-A）：管理员对其他用户/订阅/权限的管理属于 admin domain，不在本文件。
//
// 依赖方向：client ← auth ← endpoints(compatibility barrel) / useAuthApi。
// 本文件不得 import endpoints.ts（否则形成循环依赖）。
//
// 约定（与迁移前 endpoints.ts 逐字等价，仅物理位置变化）：
// - 公开端点（login/register/refresh）使用 publicApiClient，避免携带旧 token 或触发 401 refresh
// - 需认证端点（renew/change-password/me/me/access/me/membership）使用 apiClient
// - 字段命名 snake_case，与后端 JSON 对齐

import { apiClient, publicApiClient } from './client'

// ============================================================
// Auth 领域类型
// ============================================================

// [Auth] - 描述: AccessProfile 当前用户完整权限上下文（12 字段，对齐后端 AccessProfileResponse）
// 与 backend/app/schemas/access.py AccessProfileResponse 字段语义完全一致
// 唯一真源为 backend/app/services/access_control_service.get_access_context
// [Phase 5B-2 PRD60 PA-01] 新增 capabilities 字段（三类独立权限状态）
export interface CapabilityInfo {
  active: boolean
  expires_at: string | null
  watchlist_limit: number | null
}

export interface AccessProfile {
  user_id: string
  account_status: string
  roles: string[]
  is_admin: boolean
  is_member: boolean
  subscription_active: boolean
  plan_code: string | null
  plan_display_name: string | null
  expires_at: string | null
  features: string[]
  limits: Record<string, number>
  capabilities: Record<string, CapabilityInfo>
  // [权限模型 V2] 统一权限画像字段
  default_route?: string | null
  active_capability_keys?: string[]
  capability_source?: string
  diagnostics?: string[]
}

// [Auth] - 描述: 登录响应 - 含 4 个 token 字段 + 10 个 AccessProfile 字段（对齐后端 LoginResponse）
// 替代旧字段 membership_expired（语义等价：subscription_active = not membership_expired；字段名保留为 V1.6 API 兼容）
// next_route 由后端权威计算：admin→/admin/overview；member active→/overview；member expired→/subscription-expired
export interface LoginResponse {
  // token 字段（4 个）
  access_token: string
  refresh_token: string
  token_type: string
  expires_in: number
  // AccessProfile 字段（10 个）
  is_admin: boolean
  roles: string[]
  subscription_required: boolean
  subscription_active: boolean
  plan_code: string | null
  plan_display_name: string | null
  expires_at: string | null
  features: string[]
  limits: Record<string, number>
  capabilities: Record<string, CapabilityInfo>
  next_route: string
}

/** Token 刷新响应 */
export interface TokenResponse {
  access_token: string
  refresh_token: string
  token_type: string
  expires_in: number
}

/** 用户信息响应（含角色列表）——也被 admin 用户管理端点复用（endpoints.ts 从此处 import 以保持单一实现） */
export interface UserResponse {
  id: string
  email: string
  status: string
  timezone: string
  roles: string[]
  created_at: string
  updated_at: string
}

/** 会员状态响应 */
export interface MembershipResponse {
  status: string
  started_at: string
  expires_at: string
  remaining_days: number
  renewal_count: number
}

/** 注册成功响应（membership_* 字段为 V1.6 API 遗留命名，语义等价于 subscription_*） */
export interface RegisterSuccessResponse {
  access_token: string
  refresh_token: string
  token_type: string
  expires_in: number
  membership_started_at: string
  membership_expires_at: string
}

/** 续期成功响应（membership_status 为 V1.6 API 遗留命名，语义等价于 subscription_status） */
export interface RenewSuccessResponse {
  membership_status: string
  started_at: string
  old_expires_at: string | null
  new_expires_at: string
  remaining_days: number
}

// ============================================================
// 请求体类型
// ============================================================

/** 登录请求 */
export interface LoginRequest {
  email: string
  password: string
}

/** 注册请求 */
export interface RegisterRequest {
  email: string
  password: string
  invite_code: string
  timezone?: string
}

/** 续期请求 */
export interface RenewRequest {
  invite_code: string
}

// ============================================================
// ===== Auth 端点 =====
// ============================================================

// [Auth] - 描述: 用户登录 - 返回 token + AccessProfile 权限上下文 + next_route（公开接口）
// 前端不再判断 membership_expired，直接使用 next_route 跳转
export async function login(email: string, password: string): Promise<LoginResponse> {
  const { data } = await publicApiClient.post<LoginResponse>('/v1/auth/login', { email, password })
  return data
}

/** 邀请码注册 - 原子操作创建账户 + 开通 30 天会员（公开接口） */
export async function register(payload: RegisterRequest): Promise<RegisterSuccessResponse> {
  const { data } = await publicApiClient.post<RegisterSuccessResponse>('/v1/auth/register', payload)
  return data
}

/** 邀请码续期 - 未到期顺延 / 已到期从当天计算（需认证，保持 apiClient） */
export async function renew(inviteCode: string): Promise<RenewSuccessResponse> {
  const { data } = await apiClient.post<RenewSuccessResponse>('/v1/auth/renew', { invite_code: inviteCode })
  return data
}

/** 使用 refresh token 刷新，返回新的 access + refresh token（公开接口）
 * refresh_token 通过 JSON body 提交（非 query string），避免被 access log / referer 泄露
 */
export async function refreshToken(refreshToken: string): Promise<TokenResponse> {
  const { data } = await publicApiClient.post<TokenResponse>('/v1/auth/refresh', {
    refresh_token: refreshToken,
  })
  return data
}

/** 自助修改当前登录用户密码（需认证；成功返回 204，无响应体）
 *
 * 后端语义：校验 current_password 后写入 get_password_hash(new_password)。
 * 注意：当前架构无 token_version/会话吊销，已签发 token 在到期前仍然有效，
 * 因此调用方成功后必须清除本地登录态并要求重新登录。
 */
export async function changePassword(currentPassword: string, newPassword: string): Promise<void> {
  await apiClient.post('/v1/auth/change-password', {
    current_password: currentPassword,
    new_password: newPassword,
  })
}

/** 获取当前用户信息（含角色列表） */
export async function getMe(): Promise<UserResponse> {
  const { data } = await apiClient.get<UserResponse>('/v1/me')
  return data
}

// [Auth] - 描述: 获取当前用户完整权限上下文 AccessProfile（11 字段，对齐后端 AccessProfileResponse）
// 续期成功后调用此接口刷新前端 accessProfile，避免重新登录
export async function getMyAccess(): Promise<AccessProfile> {
  const { data } = await apiClient.get<AccessProfile>('/v1/me/access')
  return data
}

/** 获取当前用户订阅状态（/me/membership 为 V1.6 遗留路径名） */
export async function getMyMembership(): Promise<MembershipResponse> {
  const { data } = await apiClient.get<MembershipResponse>('/v1/me/membership')
  return data
}
