// 管理后台统一错误解析工具（PRD §8.4.9 / R14 闭环）
// 消费后端 admin_errors.admin_error 产出的统一错误结构：
//   stable_error_code / error_code / reason / detail / message / severity /
//   retryable / resumable / recommended_action
// 前端统一从这里解析，避免每个页面各自拼 detail 字段判断。

export interface AdminApiErrorInfo {
  /** 统一权威错误码（新前端消费，PRD 规范 <domain>_<reason>） */
  stableErrorCode: string | null
  /** 兼容错误码（旧前端依赖的历史值，如 DUPLICATE_RUN） */
  legacyErrorCode: string | null
  /** 人类可读消息 */
  message: string
  severity: 'error' | 'warning' | 'info' | null
  /** 是否可重跑 */
  retryable: boolean
  /** 是否可恢复（断点继续） */
  resumable: boolean
  /** 建议动作 */
  recommendedAction: string | null
  /** 额外业务上下文字段（如 after_close_run_id） */
  extra: Record<string, unknown>
}

/** 解析后端错误为结构化信息。无法解析时返回 null（非管理统一错误）。 */
export function parseAdminApiError(error: unknown): AdminApiErrorInfo | null {
  if (!error || typeof error !== 'object') return null
  const err = error as {
    response?: { data?: unknown }
  }
  const data = err.response?.data
  if (!data || typeof data !== 'object') return null
  const d = data as Record<string, unknown>

  // 兼容旧格式：detail 可能是字符串
  let detailObj = d
  if (typeof d.detail === 'object' && d.detail !== null) {
    detailObj = d.detail as Record<string, unknown>
  }
  if (typeof d.detail === 'string') {
    return {
      stableErrorCode: null,
      legacyErrorCode: null,
      message: d.detail,
      severity: null,
      retryable: false,
      resumable: false,
      recommendedAction: null,
      extra: {},
    }
  }

  const stableErrorCode = (detailObj.stable_error_code ?? null) as string | null
  const legacyErrorCode = (detailObj.error_code ?? detailObj.reason ?? null) as string | null
  const message = (detailObj.message ?? detailObj.detail ?? null) as string | null
  if (!stableErrorCode && !legacyErrorCode && !message) return null

  return {
    stableErrorCode,
    legacyErrorCode,
    message: message ?? '操作失败，请稍后重试',
    severity: (detailObj.severity as AdminApiErrorInfo['severity']) ?? null,
    retryable: Boolean(detailObj.retryable),
    resumable: Boolean(detailObj.resumable),
    recommendedAction: (detailObj.recommended_action ?? null) as string | null,
    extra: { ...detailObj },
  }
}

/** 生成包含建议动作的完整提示文案（用于 Toast / 表单错误展示）。 */
export function formatAdminApiError(error: unknown): string {
  const info = parseAdminApiError(error)
  if (!info) {
    if (error instanceof Error) return error.message
    return '操作失败，请稍后重试'
  }
  let text = info.message
  if (info.recommendedAction) {
    text = `${text}（${info.recommendedAction}）`
  }
  return text
}
