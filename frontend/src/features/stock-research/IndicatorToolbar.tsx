// [IndicatorToolbar] - 描述: 图表图层显示工具栏（PRD §6.2 — 单一真源 v2）
// 渲染 7 个图层开关（主图 4 + 副图 3），breakout 仅 selection 来源显示。
// 这是唯一的交互式图层工具栏；StrategyChart 内部 tv-strategy-legend 改为只读说明。
// 用户只能显隐，不得修改窗口、阈值等算法参数。
import {
  CHART_LAYER_MANIFEST,
  chartLayersForSource,
  type ChartLayerKey,
  type ChartLayerVisibility,
  type ResearchSource,
} from './stockResearchTypes'
import clsx from 'clsx'

export interface IndicatorToolbarProps {
  visibility: ChartLayerVisibility
  onToggle: (id: ChartLayerKey, visible: boolean) => void
  source: ResearchSource
}

export function IndicatorToolbar({ visibility, onToggle, source }: IndicatorToolbarProps) {
  const layers = chartLayersForSource(CHART_LAYER_MANIFEST, source)
  const mainLayers = layers.filter((e) => e.kind === 'main')
  const subLayers = layers.filter((e) => e.kind === 'sub')

  const renderToggle = (entry: (typeof CHART_LAYER_MANIFEST)[number]) => {
    const active = visibility[entry.id]
    const disabled = entry.enabled === false
    return (
      <label
        key={entry.id}
        className={clsx('indicator-toggle-item', !active && 'off', disabled && 'disabled')}
        title={
          disabled
            ? `${entry.name}尚未开放`
            : `${entry.name}（${entry.kind === 'main' ? '主图' : '副图'}）`
        }
      >
        <button
          type="button"
          className="indicator-toggle-btn"
          onClick={() => !disabled && onToggle(entry.id, !active)}
          aria-pressed={active}
          disabled={disabled}
        >
          <span className="indicator-toggle-name">{entry.name}</span>
          <i className={clsx('indicator-toggle-switch', active && 'on')} />
        </button>
      </label>
    )
  }

  return (
    <div className="indicator-toolbar">
      <div className="indicator-toolbar-group">
        <span className="indicator-toolbar-label">主图</span>
        {mainLayers.map(renderToggle)}
      </div>
      <div className="indicator-toolbar-group">
        <span className="indicator-toolbar-label">副图</span>
        {subLayers.map(renderToggle)}
      </div>
    </div>
  )
}

export default IndicatorToolbar
