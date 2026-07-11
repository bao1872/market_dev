// [buildUserEventExplanation] - 描述: 用户事件解释纯函数（无 React 依赖，可被 node --test 直接运行）
// 从 StrategyEventDetail 中提取白名单字段，构建用户可读的事件解释。
// 不直接整段输出任意 payload；不暴露内部字段名、算法参数、JSON。
// 校验 event.instrument_id 与当前 instrumentId 一致，不一致时标记 instrumentMismatch=true。
import type { StrategyEventDetail } from '../../api/endpoints.ts'
import { getEventLabel } from '../../constants/userFacingLabels.ts'

export interface UserEventExplanationInput {
  eventDetail?: StrategyEventDetail | null
  /** 当前查看的 instrumentId，用于校验 event 是否属于当前股票 */
  currentInstrumentId?: string | null
}

export interface UserEventExplanation {
  /** 是否有事件 */
  hasEvent: boolean
  /** event.instrument_id 与 currentInstrumentId 不一致时为 true（调用方应隐藏价格等敏感信息） */
  instrumentMismatch: boolean
  /** 事件时间（原始 ISO，调用方负责格式化） */
  eventTime: string | null
  /** 内部事件类型（如 bb_upper_touch） */
  eventType: string | null
  /** 通俗文案（如"价格触及近期波动上沿"） */
  eventLabel: string | null
  /** 关联价格（从 payload 白名单字段提取，已格式化为字符串） */
  price: string | null
  /** 关键证据（从 payload.text_content / summary 提取，人类可读） */
  evidence: string[]
}

// payload 价格字段白名单（按优先级排序）
const PRICE_FACT_KEYS = ['current_price', 'price', '现价']
const PRICE_DIRECT_KEYS = ['current_price', 'price', 'last_price', 'close_price']

/**
 * 从 payload 中提取关联价格（三级回退：facts 数组 → 顶层字段 → 纯文本正则）。
 * 只消费白名单字段，不输出任意 payload。
 */
function extractPrice(payload: Record<string, unknown>): string | null {
  // 1. facts 数组按 key 白名单匹配
  const facts = payload.facts as Array<Record<string, unknown>> | undefined
  if (Array.isArray(facts)) {
    for (const f of facts) {
      const k = String(f.key ?? '').toLowerCase()
      if (PRICE_FACT_KEYS.includes(k)) {
        const v = f.value
        if (v !== undefined && v !== null && v !== '') return String(v)
      }
    }
  }
  // 2. 顶层结构化字段白名单
  for (const k of PRICE_DIRECT_KEYS) {
    const v = payload[k]
    if (v !== undefined && v !== null && v !== '') return String(v)
  }
  // 3. 纯文本正则（text_content 中"现价：xxx"格式）
  const text = payload.text_content as string | undefined
  if (text) {
    const m = text.match(/现价[：:]\s*([\d.]+)/)
    if (m) return m[1]
  }
  return null
}

/**
 * 从 payload 中提取关键证据（仅 text_content / summary，人类可读描述）。
 * 不输出任意 payload 字段。
 */
function extractEvidence(payload: Record<string, unknown>): string[] {
  const evidence: string[] = []
  const text = payload.text_content as string | undefined
  if (text) evidence.push(text)
  const summary = payload.summary as string | undefined
  if (summary && summary !== text) evidence.push(summary)
  return evidence
}

/**
 * 构建用户事件解释。纯函数：无副作用，相同输入相同输出。
 * 调用方根据 hasEvent / instrumentMismatch 决定渲染策略。
 */
export function buildUserEventExplanation(
  input: UserEventExplanationInput,
): UserEventExplanation {
  const { eventDetail, currentInstrumentId } = input

  if (!eventDetail) {
    return {
      hasEvent: false,
      instrumentMismatch: false,
      eventTime: null,
      eventType: null,
      eventLabel: null,
      price: null,
      evidence: [],
    }
  }

  const instrumentMismatch =
    !!currentInstrumentId &&
    !!eventDetail.instrument_id &&
    currentInstrumentId !== eventDetail.instrument_id

  return {
    hasEvent: true,
    instrumentMismatch,
    eventTime: eventDetail.event_time,
    eventType: eventDetail.event_type,
    eventLabel: getEventLabel(eventDetail.event_type),
    price: extractPrice(eventDetail.payload),
    evidence: extractEvidence(eventDetail.payload),
  }
}
