// [IndicatorToolbar] - 描述: 指标显示工具栏（PRD §6.2）
// 渲染 5 个指标图层的显隐开关，按主图/副图分组。
// 用户只能显隐，不得修改窗口、阈值等算法参数。
import { INDICATOR_LAYER_MANIFEST, type IndicatorVisibility } from './stockResearchTypes'
import clsx from 'clsx'

export interface IndicatorToolbarProps {
  visibility: IndicatorVisibility
  onToggle: (id: string, visible: boolean) => void
}

export function IndicatorToolbar({ visibility, onToggle }: IndicatorToolbarProps) {
  const mainLayers = INDICATOR_LAYER_MANIFEST.filter((e) => e.kind === 'main')
  const subLayers = INDICATOR_LAYER_MANIFEST.filter((e) => e.kind === 'sub')

  const renderToggle = (entry: (typeof INDICATOR_LAYER_MANIFEST)[number]) => {
    const active = visibility[entry.id] ?? entry.defaultVisible
    return (
      <label
        key={entry.id}
        className={clsx('indicator-toggle-item', !active && 'off')}
        title={`${entry.name}（${entry.kind === 'main' ? '主图' : '副图'}）`}
      >
        <button
          type="button"
          className="indicator-toggle-btn"
          onClick={() => onToggle(entry.id, !active)}
          aria-pressed={active}
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
