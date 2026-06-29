// [内测申请] - 描述: 站内内测申请问卷模态框，受控组件，open 由父组件管理
// 校验规则：微信号/手机号至少填一个、盯盘数量必填正整数、理由必选、选"其他"补充说明必填、隐私同意必选
// 提交行为：禁用按钮防重复点击；成功显示成功页（编号+时间）；失败保留已填内容；429/422 区分提示
import { useEffect, useMemo, useState } from 'react'
import type { AxiosError } from 'axios'
import {
  submitBetaApplication,
  type BetaApplicationResponse,
  type BetaReasonCode,
} from '@/api/beta-application'
import styles from './BetaApplicationModal.module.scss'

/** 理由选项常量（与后端 reason_code 枚举对齐） */
const REASON_OPTIONS: { value: BetaReasonCode; label: string }[] = [
  { value: 'busy', label: '工作太忙没时间看盘' },
  { value: 'too_many', label: '股票太多看不过来' },
  { value: 'forget', label: '关注时间久了容易忘记' },
  { value: 'quant', label: '想体验量化工具' },
  { value: 'other', label: '其他' },
]

/** 表单字段状态 */
interface FormState {
  wechat: string
  phone: string
  watchStockCount: string
  reasonCode: BetaReasonCode | ''
  reasonOther: string
  privacyAgreed: boolean
}

const INITIAL_FORM: FormState = {
  wechat: '',
  phone: '',
  watchStockCount: '',
  reasonCode: '',
  reasonOther: '',
  privacyAgreed: false,
}

/** 字段级错误信息（key 为字段名，空对象表示无错误） */
type FieldErrors = Partial<Record<keyof FormState | '_form', string>>

export interface BetaApplicationModalProps {
  open: boolean
  onClose: () => void
}

/** 内测申请问卷模态：表单 → 提交 → 成功页 */
export default function BetaApplicationModal({ open, onClose }: BetaApplicationModalProps) {
  const [form, setForm] = useState<FormState>(INITIAL_FORM)
  const [errors, setErrors] = useState<FieldErrors>({})
  const [submitting, setSubmitting] = useState(false)
  const [success, setSuccess] = useState<BetaApplicationResponse | null>(null)

  // 打开时重置内部状态（防止上次填写内容残留）
  useEffect(() => {
    if (open) {
      setForm(INITIAL_FORM)
      setErrors({})
      setSubmitting(false)
      setSuccess(null)
    }
  }, [open])

  // ESC 关闭模态（提交中或成功页时禁用 ESC 防误触）
  useEffect(() => {
    if (!open) return
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape' && !submitting) onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose, submitting])

  // 选"其他"时显示补充说明输入框
  const showReasonOther = form.reasonCode === 'other'

  // 局部更新某个字段并清除该字段错误
  function updateField<K extends keyof FormState>(key: K, value: FormState[K]) {
    setForm((prev) => ({ ...prev, [key]: value }))
    setErrors((prev) => {
      if (!prev[key]) return prev
      const next = { ...prev }
      delete next[key]
      return next
    })
  }

  /** 校验表单，返回是否通过；错误写入 errors 状态 */
  function validate(): boolean {
    const next: FieldErrors = {}
    if (!form.wechat.trim() && !form.phone.trim()) {
      next._form = '请至少填写一种联系方式（微信号或手机号）'
    }
    const count = Number(form.watchStockCount)
    if (!form.watchStockCount || !Number.isInteger(count) || count <= 0) {
      next.watchStockCount = '请填写正整数（盯盘股票数量）'
    }
    if (!form.reasonCode) {
      next.reasonCode = '请选择使用理由'
    }
    if (showReasonOther && !form.reasonOther.trim()) {
      next.reasonOther = '请填写补充说明'
    }
    if (!form.privacyAgreed) {
      next.privacyAgreed = '请勾选隐私同意'
    }
    setErrors(next)
    return Object.keys(next).length === 0
  }

  /** 提交：通过校验后调用 API，按状态码分支处理 */
  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (submitting) return
    if (!validate()) return
    setSubmitting(true)
    setErrors({})
    try {
      const res = await submitBetaApplication({
        wechat: form.wechat.trim() || undefined,
        phone: form.phone.trim() || undefined,
        watch_stock_count: Number(form.watchStockCount),
        reason_code: form.reasonCode as BetaReasonCode,
        reason_other: showReasonOther ? form.reasonOther.trim() : undefined,
        privacy_agreed: form.privacyAgreed,
      })
      setSuccess(res)
    } catch (err: unknown) {
      const axiosErr = err as AxiosError<{ detail?: unknown }>
      const status = axiosErr.response?.status
      if (status === 429) {
        setErrors({ _form: '提交过于频繁，请稍后再试' })
      } else if (status === 422) {
        // 后端返回 FastAPI 校验错误数组：[{loc: [...], msg: ...}]，提取首条作为提示
        const detail = axiosErr.response?.data?.detail
        if (Array.isArray(detail) && detail.length > 0) {
          const first = detail[0] as { loc?: unknown[]; msg?: string }
          const field = Array.isArray(first.loc) ? first.loc[first.loc.length - 1] : '字段'
          setErrors({ _form: `${field}：${first.msg ?? '校验失败'}` })
        } else {
          setErrors({ _form: '提交内容校验失败，请检查后重试' })
        }
      } else {
        setErrors({ _form: '提交失败，请稍后重试' })
      }
    } finally {
      setSubmitting(false)
    }
  }

  // 提交按钮禁用条件：提交中或表单未填写关键字段
  const canSubmit = useMemo(() => {
    if (submitting) return false
    if (!form.watchStockCount || !form.reasonCode || !form.privacyAgreed) return false
    if (!form.wechat.trim() && !form.phone.trim()) return false
    if (showReasonOther && !form.reasonOther.trim()) return false
    return true
  }, [submitting, form])

  if (!open) return null

  return (
    <div
      className={`${styles.modal} ${styles.open}`}
      onClick={() => !submitting && onClose()}
      role="dialog"
      aria-modal="true"
      aria-label="内测申请"
    >
      <div className={styles.modalCard} onClick={(e) => e.stopPropagation()}>
        {success ? (
          <SuccessView result={success} onClose={onClose} />
        ) : (
          <>
            <div className={styles.modalHead}>
              <h3>申请内测</h3>
              <button
                className={styles.close}
                onClick={() => !submitting && onClose()}
                aria-label="关闭"
                disabled={submitting}
              >
                ×
              </button>
            </div>
            <form className={styles.form} onSubmit={handleSubmit} noValidate>
              <p className={styles.intro}>
                填写下面的信息，我们会尽快与你联系。微信号和手机号至少填一个。
              </p>

              {errors._form && <div className={styles.formError}>{errors._form}</div>}

              <div className={styles.row}>
                <label className={styles.field}>
                  <span className={styles.label}>微信号</span>
                  <input
                    type="text"
                    className={styles.input}
                    value={form.wechat}
                    onChange={(e) => updateField('wechat', e.target.value)}
                    placeholder="选填"
                    autoComplete="off"
                    disabled={submitting}
                  />
                </label>
                <label className={styles.field}>
                  <span className={styles.label}>手机号</span>
                  <input
                    type="tel"
                    className={styles.input}
                    value={form.phone}
                    onChange={(e) => updateField('phone', e.target.value)}
                    placeholder="选填"
                    autoComplete="off"
                    disabled={submitting}
                  />
                </label>
              </div>

              <label className={styles.field}>
                <span className={styles.label}>
                  盯盘股票数量 <em className={styles.required}>*</em>
                </span>
                <input
                  type="number"
                  className={styles.input}
                  value={form.watchStockCount}
                  onChange={(e) => updateField('watchStockCount', e.target.value)}
                  placeholder="如 10"
                  min={1}
                  step={1}
                  inputMode="numeric"
                  disabled={submitting}
                />
                {errors.watchStockCount && (
                  <span className={styles.fieldError}>{errors.watchStockCount}</span>
                )}
              </label>

              <label className={styles.field}>
                <span className={styles.label}>
                  使用理由 <em className={styles.required}>*</em>
                </span>
                <select
                  className={styles.select}
                  value={form.reasonCode}
                  onChange={(e) =>
                    updateField('reasonCode', e.target.value as BetaReasonCode | '')
                  }
                  disabled={submitting}
                >
                  <option value="">请选择</option>
                  {REASON_OPTIONS.map((opt) => (
                    <option key={opt.value} value={opt.value}>
                      {opt.label}
                    </option>
                  ))}
                </select>
                {errors.reasonCode && (
                  <span className={styles.fieldError}>{errors.reasonCode}</span>
                )}
              </label>

              {showReasonOther && (
                <label className={styles.field}>
                  <span className={styles.label}>
                    补充说明 <em className={styles.required}>*</em>
                  </span>
                  <textarea
                    className={styles.textarea}
                    value={form.reasonOther}
                    onChange={(e) => updateField('reasonOther', e.target.value)}
                    placeholder="请简要描述你的使用场景"
                    rows={3}
                    disabled={submitting}
                  />
                  {errors.reasonOther && (
                    <span className={styles.fieldError}>{errors.reasonOther}</span>
                  )}
                </label>
              )}

              <label className={styles.checkboxRow}>
                <input
                  type="checkbox"
                  className={styles.checkbox}
                  checked={form.privacyAgreed}
                  onChange={(e) => updateField('privacyAgreed', e.target.checked)}
                  disabled={submitting}
                />
                <span className={styles.checkboxLabel}>
                  我已阅读并同意收集上述信息用于内测申请审核与联系
                </span>
              </label>
              {errors.privacyAgreed && (
                <span className={styles.fieldError}>{errors.privacyAgreed}</span>
              )}

              <button
                type="submit"
                className={`${styles.btn} ${styles.btnPrimary} ${styles.btnWide}`}
                disabled={!canSubmit}
              >
                {submitting ? '提交中…' : '提交申请'}
              </button>
            </form>
          </>
        )}
      </div>
    </div>
  )
}

/** 成功页面：展示申请编号 + 提交时间，关闭按钮 */
function SuccessView({
  result,
  onClose,
}: {
  result: BetaApplicationResponse
  onClose: () => void
}) {
  // 提交时间格式化：后端返回 ISO 字符串，转本地时间展示
  const submittedText = useMemo(() => {
    try {
      const d = new Date(result.submitted_at)
      return d.toLocaleString('zh-CN', { hour12: false })
    } catch {
      return result.submitted_at
    }
  }, [result.submitted_at])

  return (
    <div className={styles.success}>
      <div className={styles.successIcon} aria-hidden="true">✓</div>
      <h3>申请已提交</h3>
      <p className={styles.successText}>我们会尽快与你联系，请保持联系方式畅通。</p>
      <div className={styles.successMeta}>
        <div>
          <span>申请编号</span>
          <strong>{result.id}</strong>
        </div>
        <div>
          <span>提交时间</span>
          <strong>{submittedText}</strong>
        </div>
        <div>
          <span>状态</span>
          <strong>{result.status}</strong>
        </div>
      </div>
      <button
        type="button"
        className={`${styles.btn} ${styles.btnPrimary} ${styles.btnWide}`}
        onClick={onClose}
      >
        关闭
      </button>
    </div>
  )
}
