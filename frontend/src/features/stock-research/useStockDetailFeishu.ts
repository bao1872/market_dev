// [useStockDetailFeishu] - 描述: StockDetailPage 飞书异步投递 hook
// 负责 POST 创建截图任务 → 1s 轮询至 success/failed 或 30s 超时 → interval/timeout 清理。
// 保持飞书默认 1d 截图契约，不改 Capture API。
import { useState, useRef, useCallback, useEffect } from 'react'
import { useMutation } from '@tanstack/react-query'
import { sendStockDetailFeishu, getStockDetailFeishuStatus } from '@/api/endpoints'
import type { StockDetailFeishuCreateResponse, StockDetailFeishuStatusResponse } from '@/api/endpoints'
import { useToast } from '@/store/toast'

export interface StockDetailFeishuParams {
  instrumentId: string | undefined
}

export interface StockDetailFeishu {
  feishuOpen: boolean
  setFeishuOpen: (open: boolean) => void
  feishuResult: StockDetailFeishuCreateResponse | null
  feishuStatus: StockDetailFeishuStatusResponse | null
  feishuPolling: boolean
  sendFeishuPending: boolean
  stopFeishuPolling: () => void
  handleSendFeishu: () => void
  handleOpenFeishu: () => void
  handleCloseFeishu: () => void
}

export function useStockDetailFeishu({ instrumentId }: StockDetailFeishuParams): StockDetailFeishu {
  const showToast = useToast((s) => s.show)

  const [feishuOpen, setFeishuOpen] = useState(false)
  const [feishuResult, setFeishuResult] = useState<StockDetailFeishuCreateResponse | null>(null)
  const [feishuStatus, setFeishuStatus] = useState<StockDetailFeishuStatusResponse | null>(null)
  const [feishuPolling, setFeishuPolling] = useState(false)
  const feishuPollIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const feishuPollTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const sendFeishuMutation = useMutation<
    StockDetailFeishuCreateResponse,
    Error,
    { instrId: string }
  >({
    mutationFn: ({ instrId }) => sendStockDetailFeishu(instrId),
  })

  // 清理轮询定时器（卸载 / 关闭模态框 / 轮询结束均需调用）
  const stopFeishuPolling = useCallback(() => {
    if (feishuPollIntervalRef.current) {
      clearInterval(feishuPollIntervalRef.current)
      feishuPollIntervalRef.current = null
    }
    if (feishuPollTimeoutRef.current) {
      clearTimeout(feishuPollTimeoutRef.current)
      feishuPollTimeoutRef.current = null
    }
    setFeishuPolling(false)
  }, [])

  // 组件卸载时清理轮询定时器，避免内存泄漏
  useEffect(() => {
    return () => stopFeishuPolling()
  }, [stopFeishuPolling])

  // POST 创建异步任务 → toast 提示入队 → 1s 轮询至 success/failed 或 30s 超时
  const handleSendFeishu = useCallback(() => {
    if (!instrumentId) return
    setFeishuResult(null)
    setFeishuStatus(null)
    stopFeishuPolling()
    sendFeishuMutation.mutate(
      { instrId: instrumentId },
      {
        onSuccess: (res) => {
          setFeishuResult(res)
          showToast('已进入发送队列', `test_run_id: ${res.test_run_id.slice(0, 8)}`)
          setFeishuPolling(true)
          // 30s 超时兜底：超时后停止轮询并提示用户去消息中心查看
          feishuPollTimeoutRef.current = setTimeout(() => {
            stopFeishuPolling()
            showToast('发送超时', '请到消息中心查看最终状态')
          }, 30000)
          // 每 1s 轮询状态，命中终态（success/failed）即停止
          feishuPollIntervalRef.current = setInterval(async () => {
            let status: StockDetailFeishuStatusResponse
            try {
              status = await getStockDetailFeishuStatus(res.test_run_id)
            } catch (e) {
              stopFeishuPolling()
              showToast('状态查询失败', e instanceof Error ? e.message : '请重试')
              return
            }
            setFeishuStatus(status)
            if (status.overall_status === 'success' || status.overall_status === 'failed') {
              stopFeishuPolling()
              if (status.overall_status === 'success') {
                const parts: string[] = []
                parts.push(
                  `卡片${status.card_status === 'success' ? '已送达' : status.card_status}`,
                )
                if (status.image_status !== 'not_created') {
                  parts.push(
                    `图片${status.image_status === 'success' ? '已送达' : status.image_status}`,
                  )
                }
                showToast('发送成功', parts.join(' · '))
              } else {
                showToast(
                  '发送失败',
                  `${status.failed_step ?? '未知步骤'} · ${status.error_code ?? ''} · ${status.error_message ?? ''}`.trim(),
                )
              }
            }
          }, 1000)
        },
        onError: () => showToast('发送失败', '请重试'),
      },
    )
  }, [instrumentId, sendFeishuMutation, stopFeishuPolling, showToast])

  const handleOpenFeishu = useCallback(() => {
    stopFeishuPolling()
    setFeishuResult(null)
    setFeishuStatus(null)
    setFeishuOpen(true)
  }, [stopFeishuPolling])

  const handleCloseFeishu = useCallback(() => {
    if (!sendFeishuMutation.isPending && !feishuPolling) {
      stopFeishuPolling()
      setFeishuOpen(false)
    }
  }, [sendFeishuMutation.isPending, feishuPolling, stopFeishuPolling])

  return {
    feishuOpen,
    setFeishuOpen,
    feishuResult,
    feishuStatus,
    feishuPolling,
    sendFeishuPending: sendFeishuMutation.isPending,
    stopFeishuPolling,
    handleSendFeishu,
    handleOpenFeishu,
    handleCloseFeishu,
  }
}
