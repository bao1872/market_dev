// [内测申请] - 描述: 站内内测申请公开 API（无需登录），对应后端 POST /public/beta-applications
// apiClient baseURL=/api，Vite 代理去掉 /api 前缀转发到后端 8000，后端路由为 /public/beta-applications
import { apiClient } from './client'

/** 使用理由枚举（与后端 reason_code 对齐） */
export type BetaReasonCode = 'busy' | 'too_many' | 'forget' | 'quant' | 'other'

/** 提交内测申请请求体 */
export interface BetaApplicationRequest {
  wechat?: string
  phone?: string
  watch_stock_count: number
  reason_code: BetaReasonCode
  reason_other?: string
  privacy_agreed: boolean
}

/** 提交内测申请响应体（201 新申请 / 200 重复均返回此结构） */
export interface BetaApplicationResponse {
  id: string
  status: string
  submitted_at: string
}

/**
 * 提交内测申请（公开接口，无需登录）
 * 状态码：201 新申请 / 200 重复 / 429 限流 / 422 校验失败
 * 失败时抛出 AxiosError，调用方按 error.response.status 区分处理
 */
export async function submitBetaApplication(
  payload: BetaApplicationRequest,
): Promise<BetaApplicationResponse> {
  const { data } = await apiClient.post<BetaApplicationResponse>(
    '/public/beta-applications',
    payload,
  )
  return data
}
