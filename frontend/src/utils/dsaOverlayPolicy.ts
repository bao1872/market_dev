// [DSA Overlay Policy] - 描述: DSA/BB overlay 周期策略、渲染决策、toggle 决策与提示文案
//   纯函数模块（无 JSX），便于 Node --experimental-strip-types 单元测试
//   供 StrategyChart 复用

/**
 * DSA overlay 是否允许在指定周期渲染
 *
 * [PR #32] - DSA VWAP 支持全周期（1d/15m/1h/1w/1mo），不再 1d-only。
 * 1d 是主结构锚，非 1d 是验证图层（用于核查该周期结构）。
 */
export function shouldAllowDsaOverlay(timeframe: string): boolean {
  return ['1d', '15m', '1h', '1w', '1mo'].includes(timeframe)
}

/**
 * BB overlay 是否允许在指定周期渲染
 *
 * [PR #33] - BB 全周期支持，1w/1mo 不再被前端 skip（修 PR #32 遗留 L1666）。
 */
export function shouldAllowBbOverlay(timeframe: string): boolean {
  return ['1d', '15m', '1h', '1w', '1mo'].includes(timeframe)
}

/**
 * 判断当前 timeframe 是否需要校验 DSA source mismatch
 *
 * [PR #32] - DSA 全周期渲染，全部需要校验 source mismatch。
 * 之前 PR #31 仅 1d 校验（DSA 不在 15m/1h 渲染），现已改为全周期支持。
 */
export function shouldCheckDsaMismatch(timeframe: string): boolean {
  return shouldAllowDsaOverlay(timeframe)
}

/**
 * DSA overlay 在指定周期的 title 提示文案
 *
 * - 1d: "DSA VWAP 日线结构锚。"（主趋势锚）
 * - 非 1d: "DSA VWAP 当前周期验证图层：用于核查该周期结构，不作为主趋势锚。"
 */
export function DSA_TITLE_HINT(timeframe: string): string {
  if (timeframe === '1d') {
    return 'DSA VWAP 日线结构锚。'
  }
  return 'DSA VWAP 当前周期验证图层：用于核查该周期结构，不作为主趋势锚。'
}

/**
 * DSA layer 渲染决策（替代 StrategyChart L1661 硬编码 skip）
 *
 * [PR #33] - 彻底移除 `if (layer.layer_id === 'dsa_vwap' && timeframe !== '1d') return` 硬编码。
 * 决策只受 layers.dsa / dsaSourceMismatch / 周期支持控制，不再按 timeframe 跳过。
 *
 * @param layerId      - 图层 id（'dsa_vwap'）
 * @param layers       - 当前图层开关状态（仅需 dsa 字段）
 * @param dsaSourceMismatch - DSA source 是否与 K 线 time 不对齐
 * @param timeframe    - 当前周期
 */
export function shouldRenderDsaLayer(
  layerId: string,
  layers: { dsa: boolean },
  dsaSourceMismatch: boolean,
  timeframe: string,
): boolean {
  if (layerId !== 'dsa_vwap') return false
  if (!layers.dsa) return false
  if (dsaSourceMismatch) return false
  return shouldAllowDsaOverlay(timeframe)
}

/**
 * BB layer 渲染决策（替代 StrategyChart L1666 硬编码 skip）
 *
 * [PR #33] - 彻底移除 `if (layer.layer_id === 'bb' && (timeframe === '1w' || timeframe === '1mo')) return` 硬编码。
 * 决策只受 layers.bb / 周期支持控制，1w/1mo 不再被 skip。
 *
 * @param layerId   - 图层 id（'bb'）
 * @param layers    - 当前图层开关状态（仅需 bb 字段）
 * @param timeframe - 当前周期
 */
export function shouldRenderBbLayer(
  layerId: string,
  layers: { bb: boolean },
  timeframe: string,
): boolean {
  if (layerId !== 'bb') return false
  if (!layers.bb) return false
  return shouldAllowBbOverlay(timeframe)
}

/**
 * DSA toggle 决策（替代 StrategyChart L2226 硬编码 disable）
 *
 * [PR #33] - 彻底移除 `if (groupId === 'dsa' && timeframe !== '1d') return` 硬编码。
 * 非 capture 模式下 DSA 全周期可切换；capture 模式仍锁定（advice.md v6 第 2 条）。
 *
 * @param groupId       - toggle group id（'dsa' / 'bb' / 'profile' / ...）
 * @param isCaptureMode - 是否处于飞书截图模式
 * @param captureLayers - 截图模式锁定的图层列表（FEISHU_CAPTURE_LAYERS）
 */
export function shouldToggleDsa(
  groupId: string,
  isCaptureMode: boolean,
  captureLayers: readonly string[],
): boolean {
  if (groupId !== 'dsa') return true
  if (isCaptureMode && captureLayers.includes('dsa')) return false
  return true
}

/**
 * DSA 纵轴范围候选决策（替代 StrategyChart L1503 硬编码 timeframe === '1d'）
 *
 * [PR #33] - 彻底移除 `if (layer.layer_id === 'dsa_vwap' && layers.dsa && timeframe === '1d')` 硬编码。
 * DSA 全周期参与 y-axis range，避免非 1d DSA 渲染后被轴范围挤掉。
 *
 * @param layerId   - 图层 id（'dsa_vwap'）
 * @param layers    - 当前图层开关状态（仅需 dsa 字段）
 * @param timeframe - 当前周期
 */
export function shouldIncludeDsaInPriceRange(
  layerId: string,
  layers: { dsa: boolean },
  timeframe: string,
): boolean {
  if (layerId !== 'dsa_vwap') return false
  if (!layers.dsa) return false
  return shouldAllowDsaOverlay(timeframe)
}
