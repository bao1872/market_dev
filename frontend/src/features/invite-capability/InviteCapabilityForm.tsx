// V2.1 邀请码能力配置表单（PRD §6）
//
// 表单职责：
// 1. 三个能力 checkbox（watchlist_management / market_screening / review_management）
// 2. 自选额度输入（仅 watchlist_management 勾选时启用）
// 3. 授权月数输入（duration_months，1-120）
// 4. 生成数量（count，1-100）
// 5. 批次备注（note，最多 200 字符）
// 6. 客户端即时校验（validateInviteCapabilityForm）
// 7. 提交时调用 onSubmit(formToCreateRequest(form))
// 8. 后端错误通过 serverError 展示
//
// 不提供编辑已创建权限配置的能力（PRD G2）。

import { useState, useCallback, useMemo } from 'react'
import {
  CAPABILITY_KEYS,
  CAPABILITY_LABELS,
  INITIAL_FORM_STATE,
  MAX_DURATION_MONTHS,
  MAX_INVITE_COUNT,
  MAX_WATCHLIST_STOCK_LIMIT,
  formToCreateRequest,
  validateInviteCapabilityForm,
  type CapabilityFormState,
} from './inviteCapabilityValidation'
import type { InviteCodeV2CreateRequest, InviteCodeV2Response } from '@/api/endpoints'

export interface InviteCapabilityFormProps {
  /** 提交回调，返回后端错误字符串（成功时返回 null） */
  onSubmit: (request: InviteCodeV2CreateRequest) => Promise<InviteCodeV2Response[]>
  /** 创建成功后回调（父组件用于显示新码） */
  onCreated?: (codes: InviteCodeV2Response[]) => void
  /** 取消回调 */
  onCancel?: () => void
}

export function InviteCapabilityForm({
  onSubmit,
  onCreated,
  onCancel,
}: InviteCapabilityFormProps) {
  const [form, setForm] = useState<CapabilityFormState>(INITIAL_FORM_STATE)
  const [serverError, setServerError] = useState<string | null>(null)
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [createdCodes, setCreatedCodes] = useState<InviteCodeV2Response[]>([])

  const errors = useMemo(() => validateInviteCapabilityForm(form), [form])

  const updateField = useCallback(
    <K extends keyof CapabilityFormState>(
      key: K,
      value: CapabilityFormState[K],
    ) => {
      setForm((prev) => ({ ...prev, [key]: value }))
      setServerError(null)
      setCreatedCodes([])
    },
    [],
  )

  const handleToggleCapability = useCallback(
    (key: (typeof CAPABILITY_KEYS)[number]) => {
      setForm((prev) => {
        const next = { ...prev, [key]: !prev[key] }
        // 取消勾选 watchlist_management 时清空额度（避免残留值）
        if (key === 'watchlist_management' && prev.watchlist_management) {
          next.watchlist_stock_limit = ''
        }
        // 重新勾选 watchlist_management 时恢复默认值
        if (key === 'watchlist_management' && !prev.watchlist_management) {
          next.watchlist_stock_limit = 20
        }
        return next
      })
      setServerError(null)
      setCreatedCodes([])
    },
    [],
  )

  const handleSubmit = useCallback(async () => {
    // 客户端校验失败时不提交
    const validationErrors = validateInviteCapabilityForm(form)
    if (Object.keys(validationErrors).length > 0) {
      return
    }
    setIsSubmitting(true)
    setServerError(null)
    try {
      const request = formToCreateRequest(form)
      const codes = await onSubmit(request)
      setCreatedCodes(codes)
      onCreated?.(codes)
    } catch (e) {
      const msg = extractErrorMessage(e)
      setServerError(msg)
    } finally {
      setIsSubmitting(false)
    }
  }, [form, onSubmit, onCreated])

  const canSubmit =
    Object.keys(errors).length === 0 && !isSubmitting

  return (
    <div className="invite-capability-form">
      {/* 能力勾选 */}
      <div className="form-section">
        <div className="form-section-title">能力配置</div>
        <div className="capability-checkbox-list">
          {CAPABILITY_KEYS.map((key) => {
            const label = CAPABILITY_LABELS[key]
            const checked = form[key]
            return (
              <label
                key={key}
                className={`capability-checkbox ${checked ? 'checked' : ''}`}
              >
                <input
                  type="checkbox"
                  checked={checked}
                  onChange={() => handleToggleCapability(key)}
                />
                <div className="capability-checkbox-content">
                  <div className="capability-checkbox-label">{label.label}</div>
                  <div className="capability-checkbox-desc">
                    {label.description}
                  </div>
                </div>
              </label>
            )
          })}
        </div>
        {errors.capabilities && (
          <div className="form-error">{errors.capabilities}</div>
        )}
      </div>

      {/* 自选额度（仅 watchlist_management 勾选时启用） */}
      <div className="form-row">
        <label className="form-label">
          自选额度（只）
          {!form.watchlist_management && (
            <span className="form-hint">（未勾选自选管理，不可填写）</span>
          )}
        </label>
        <input
          className="input"
          type="number"
          min={1}
          max={MAX_WATCHLIST_STOCK_LIMIT}
          step={1}
          value={form.watchlist_stock_limit}
          disabled={!form.watchlist_management}
          onChange={(e) => {
            const v = e.target.value === '' ? '' : Number(e.target.value)
            updateField('watchlist_stock_limit', v as number | '')
          }}
        />
        {errors.watchlist_stock_limit && (
          <div className="form-error">{errors.watchlist_stock_limit}</div>
        )}
      </div>

      {/* 授权月数 */}
      <div className="form-row">
        <label className="form-label">授权月数（按日历月计算）</label>
        <input
          className="input"
          type="number"
          min={1}
          max={MAX_DURATION_MONTHS}
          step={1}
          value={form.duration_months}
          onChange={(e) => {
            const v = e.target.value === '' ? '' : Number(e.target.value)
            updateField('duration_months', v as number | '')
          }}
        />
        {errors.duration_months && (
          <div className="form-error">{errors.duration_months}</div>
        )}
      </div>

      {/* 生成数量 */}
      <div className="form-row">
        <label className="form-label">生成数量</label>
        <input
          className="input"
          type="number"
          min={1}
          max={MAX_INVITE_COUNT}
          step={1}
          value={form.count}
          onChange={(e) => {
            const v = e.target.value === '' ? '' : Number(e.target.value)
            updateField('count', v as number | '')
          }}
        />
        {errors.count && <div className="form-error">{errors.count}</div>}
      </div>

      {/* 批次备注 */}
      <div className="form-row">
        <label className="form-label">批次备注（可选）</label>
        <input
          className="input"
          type="text"
          maxLength={200}
          placeholder="例如：6月线下交流会"
          value={form.note}
          onChange={(e) => updateField('note', e.target.value)}
        />
        {errors.note && <div className="form-error">{errors.note}</div>}
      </div>

      {/* 后端错误展示 */}
      {serverError && (
        <div className="notice notice-error">{serverError}</div>
      )}

      {/* 创建后展示新码 */}
      {createdCodes.length > 0 && (
        <div className="generated-invite-list">
          <div className="generated-invite-title">新邀请码（仅本次显示，后续不可获取）</div>
          {createdCodes.map((c) => (
            <div key={c.id} className="generated-invite-box">
              <b>{c.code}</b>
              <button
                className="btn small"
                onClick={() => navigator.clipboard?.writeText(c.code)}
              >
                复制
              </button>
            </div>
          ))}
        </div>
      )}

      {/* 提交按钮 */}
      <div className="form-actions">
        {onCancel && (
          <button className="btn" onClick={onCancel} disabled={isSubmitting}>
            取消
          </button>
        )}
        <button
          className="btn primary"
          onClick={handleSubmit}
          disabled={!canSubmit}
        >
          {isSubmitting ? '生成中...' : '生成邀请码'}
        </button>
      </div>
    </div>
  )
}

/** 从 axios 错误对象中提取后端 detail 字符串 */
function extractErrorMessage(e: unknown): string {
  if (typeof e === 'object' && e !== null) {
    const any = e as { response?: { data?: { detail?: unknown } }; message?: string }
    const detail = any.response?.data?.detail
    if (typeof detail === 'string') return detail
    if (detail && typeof detail === 'object') return JSON.stringify(detail)
    if (any.message) return any.message
  }
  return '生成邀请码失败'
}
