// [IndicatorPreferences] - 描述: 指标图层显示偏好持久化（PRD §6.2）
// 首版使用带版本号的 localStorage；未来若有服务端偏好能力，可替换实现。
// 用户只能显隐指标，不得修改窗口、阈值等算法参数。
import { defaultIndicatorVisibility, type IndicatorVisibility } from './stockResearchTypes.ts'

const PREF_VERSION = 1
const PREF_KEY = `panji:indicator-visibility:v${PREF_VERSION}`

interface StoredPreference {
  version: number
  visibility: IndicatorVisibility
}

// 从 localStorage 加载偏好，与 manifest 默认值合并；版本不匹配时重置为默认值
export function loadIndicatorVisibility(): IndicatorVisibility {
  const defaults = defaultIndicatorVisibility()
  try {
    const raw = localStorage.getItem(PREF_KEY)
    if (!raw) return defaults
    const parsed = JSON.parse(raw) as StoredPreference
    if (parsed.version !== PREF_VERSION) return defaults
    if (!parsed.visibility || typeof parsed.visibility !== 'object') return defaults
    // 合并：manifest 默认值为基底，存储的用户偏好覆盖
    return { ...defaults, ...parsed.visibility }
  } catch {
    return defaults
  }
}

// 保存偏好到 localStorage（带版本号）
export function saveIndicatorVisibility(visibility: IndicatorVisibility): void {
  try {
    const payload: StoredPreference = { version: PREF_VERSION, visibility }
    localStorage.setItem(PREF_KEY, JSON.stringify(payload))
  } catch {
    // ignore quota / serialization errors
  }
}
