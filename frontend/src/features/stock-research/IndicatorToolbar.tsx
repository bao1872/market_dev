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
    // enabled=false 时禁用开关（灰显不可点击），Phase 5 实现后改回 true
    const disabled = entry.enabled === false
    return (
      <label
        key={entry.id}
        className={clsx('indicator-toggle-item', !active && 'off', disabled && 'disabled')}
        title={
          disabled
            ? `${entry.name}（${entry.kind === 'main' ? '主图' : '副图'}）- 真实筹码共识区将在后续版本中实现`
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
