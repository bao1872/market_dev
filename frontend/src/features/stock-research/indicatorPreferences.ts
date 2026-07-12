// [IndicatorPreferences] - 描述: 图表图层显示偏好持久化（PRD §6.2 — 单一真源 v2）
// 使用 per-source+strategy 的 localStorage key，替代旧的全局 panji:indicator-visibility:v1
// 和 StrategyChart 内部的 detail-chart-strategy-groups-v3 key。
// 用户只能显隐图层，不得修改窗口、阈值等算法参数。
import {
  defaultChartLayerVisibility,
  type ChartLayerVisibility,
  type ResearchSource,
} from './stockResearchTypes.ts'

const PREF_VERSION = 2
const PREF_KEY_PREFIX = `panji:chart-layer-visibility:v${PREF_VERSION}`

// 旧 key（全局，4 键 IndicatorVisibility）
const LEGACY_TOOLBAR_KEY = 'panji:indicator-visibility:v1'
// 旧 key（per-source+strategy，12 键 LayerVisibility）
function legacyChartKey(source: string, strategyKey: string): string {
  return `detail-chart-strategy-groups-v3:${source}:${strategyKey}`
}

interface StoredPreference {
  version: number
  visibility: ChartLayerVisibility
}

function prefKey(source: ResearchSource, strategyKey: string): string {
  return `${PREF_KEY_PREFIX}:${source}:${strategyKey}`
}

// 从旧 12 键 LayerVisibility 提取 7 键 ChartLayerVisibility
// 分组图层用 OR（任一子图层开启则组开关为开）；单图层用 in 检查尊重显式 false
function migrateFromLegacyLayers(
  saved: Record<string, boolean>,
  defaults: ChartLayerVisibility,
): ChartLayerVisibility {
  const has = (k: string) => k in saved
  return {
    trend: has('dsa') || has('selection') ? !!(saved.dsa || saved.selection) : defaults.trend,
    node: has('node') || has('profile') || has('poc') ? !!(saved.node || saved.profile || saved.poc) : defaults.node,
    boll: has('bb') ? !!saved.bb : defaults.boll,
    volume: has('volume') ? !!saved.volume : defaults.volume,
    macd: has('macd') ? !!saved.macd : defaults.macd,
    sqzmom: has('sqzmom') ? !!saved.sqzmom : defaults.sqzmom,
    breakout: has('breakout') ? !!saved.breakout : defaults.breakout,
  }
}

// 从旧 4 键 IndicatorVisibility 提取 7 键 ChartLayerVisibility
function migrateFromLegacyToolbar(
  saved: Record<string, boolean>,
  defaults: ChartLayerVisibility,
): ChartLayerVisibility {
  return {
    trend: saved.price_structure ?? defaults.trend,
    node: defaults.node,
    boll: saved.boll ?? defaults.boll,
    volume: saved.volume ?? defaults.volume,
    macd: saved.macd ?? defaults.macd,
    sqzmom: defaults.sqzmom,
    breakout: defaults.breakout,
  }
}

// 从 localStorage 加载偏好；新 key 优先，旧 key 迁移一次后只写新 key
export function loadChartLayerVisibility(
  source: ResearchSource,
  strategyKey: string,
): ChartLayerVisibility {
  const defaults = defaultChartLayerVisibility(source)
  const key = prefKey(source, strategyKey)
  try {
    // 1. 优先读取新 key
    const raw = localStorage.getItem(key)
    if (raw) {
      const parsed = JSON.parse(raw) as StoredPreference
      if (parsed.version === PREF_VERSION && parsed.visibility) {
        return { ...defaults, ...parsed.visibility }
      }
    }
    // 2. 尝试从旧 chart key 迁移（12 键 → 7 键）
    const legacyRaw = localStorage.getItem(legacyChartKey(source, strategyKey))
    if (legacyRaw) {
      const saved = JSON.parse(legacyRaw) as Record<string, boolean>
      if (saved && typeof saved === 'object') {
        return migrateFromLegacyLayers(saved, defaults)
      }
    }
    // 3. 尝试从旧 toolbar key 迁移（4 键 → 7 键）
    const legacyToolbarRaw = localStorage.getItem(LEGACY_TOOLBAR_KEY)
    if (legacyToolbarRaw) {
      const parsed = JSON.parse(legacyToolbarRaw) as { visibility?: Record<string, boolean> }
      if (parsed?.visibility) {
        return migrateFromLegacyToolbar(parsed.visibility, defaults)
      }
    }
  } catch {
    // ignore parse / quota errors
  }
  return defaults
}

// 保存偏好到新 key（带版本号）
export function saveChartLayerVisibility(
  source: ResearchSource,
  strategyKey: string,
  visibility: ChartLayerVisibility,
): void {
  try {
    const payload: StoredPreference = { version: PREF_VERSION, visibility }
    localStorage.setItem(prefKey(source, strategyKey), JSON.stringify(payload))
  } catch {
    // ignore quota / serialization errors
  }
}
