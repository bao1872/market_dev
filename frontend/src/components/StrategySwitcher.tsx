// StrategySwitcher：策略切换组件（V1.5.1）
// 对应原型 app.js initStrategySwitchers()
// 状态机：idle → loading_strategy_schema → loading_result → ready/empty/partial/error/unavailable
// 切换时：更新标签 → 请求 Schema → 构建列/图层 → 请求结果 → 恢复状态 → 更新 URL
import { useState, useEffect, useCallback, type ReactNode } from 'react'
import clsx from 'clsx'
import type { PageState } from '@/api/types'

// 策略选项定义
export interface StrategyOption {
  id: string
  name: string
  description: string
  version?: string
  kind?: 'selection' | 'monitor' | 'selection_plan' | 'monitor_plan'
}

// 策略面板内容
export interface StrategyPanel {
  id: string
  state: PageState
  content: ReactNode
}

interface StrategySwitcherProps {
  // 策略组标识（用于 sessionStorage 持久化）
  group: string
  options: StrategyOption[]
  panels: Record<string, StrategyPanel>
  // 切换回调
  onChange?: (strategyId: string) => void
  // 是否从 URL hash 恢复
  hashRestore?: boolean
}

export function StrategySwitcher({
  group,
  options,
  panels,
  onChange,
  hashRestore = true,
}: StrategySwitcherProps) {
  const [activeId, setActiveId] = useState<string>('')

  // 初始化：从 sessionStorage 或 URL 恢复，否则用第一个
  useEffect(() => {
    const key = `strategy-tab:${window.location.pathname}:${group}`
    let saved: string | null = null
    try {
      saved = sessionStorage.getItem(key)
    } catch {
      // ignore
    }

    // URL hash 优先
    if (hashRestore) {
      const hash = window.location.hash.replace('#', '')
      if (hash && panels[hash]) {
        setActiveId(hash)
        return
      }
    }

    // sessionStorage 次之
    if (saved && panels[saved]) {
      setActiveId(saved)
      return
    }

    // 默认第一个
    if (options.length > 0) {
      setActiveId(options[0].id)
    }
  }, [group, options, panels, hashRestore])

  // 切换策略
  const activate = useCallback(
    (id: string, persist = true) => {
      setActiveId(id)
      if (persist) {
        try {
          sessionStorage.setItem(
            `strategy-tab:${window.location.pathname}:${group}`,
            id,
          )
        } catch {
          // ignore
        }
      }
      // 更新 URL hash
      if (hashRestore) {
        const url = new URL(window.location.href)
        url.hash = id
        window.history.replaceState({}, '', url)
      }
      onChange?.(id)
    },
    [group, onChange, hashRestore],
  )

  if (options.length === 0) {
    return null
  }

  const activePanel = panels[activeId]

  return (
    <div>
      {/* 策略切换标签 */}
      <div className="strategy-switcher" data-strategy-group={group}>
        {options.map((option) => (
          <button
            key={option.id}
            className={clsx('strategy-option', activeId === option.id && 'active')}
            aria-selected={activeId === option.id}
            onClick={() => activate(option.id)}
          >
            <b>
              <i className="strategy-dot"></i>
              {option.name}
            </b>
            <span>{option.description}</span>
          </button>
        ))}
      </div>

      {/* 策略面板内容 */}
      {activePanel && (
        <div className={clsx('strategy-panel', 'active')} id={activePanel.id}>
          {activePanel.state === 'loading' && (
            <div className="empty">加载策略 Schema 中…</div>
          )}
          {activePanel.state === 'error' && (
            <div className="notice error">策略加载失败，请重试</div>
          )}
          {activePanel.state === 'strategy_unavailable' && (
            <div className="notice warn">策略维护中或未开放</div>
          )}
          {activePanel.state === 'permission_denied' && (
            <div className="notice">当前套餐无权限使用此策略</div>
          )}
          {(activePanel.state === 'ready' ||
            activePanel.state === 'empty' ||
            activePanel.state === 'partial' ||
            activePanel.state === 'stale') &&
            activePanel.content}
        </div>
      )}
    </div>
  )
}

// ===== PlanSwitcher：方案切换组件（V1.3 组合方案）=====
// 对应原型 .plan-switch-bar 结构
export interface PlanOption {
  id: string
  name: string
  revision?: string
  composition?: string[] // 成员策略 token
}

interface PlanSwitcherProps {
  plans: PlanOption[]
  activePlanId: string
  onChange: (planId: string) => void
  // 方案组合模式（ALL/ANY）
  comboMode?: 'ALL' | 'ANY'
  onComboModeChange?: (mode: 'ALL' | 'ANY') => void
}

export function PlanSwitcher({
  plans,
  activePlanId,
  onChange,
  comboMode,
  onComboModeChange,
}: PlanSwitcherProps) {
  const activePlan = plans.find((p) => p.id === activePlanId)

  return (
    <div className="plan-switch-bar">
      <div>
        <span style={{ fontSize: '11px', color: 'var(--muted)' }}>方案</span>
        <select
          className="select"
          value={activePlanId}
          onChange={(e) => onChange(e.target.value)}
        >
          {plans.map((plan) => (
            <option key={plan.id} value={plan.id}>
              {plan.name}
              {plan.revision ? ` (${plan.revision})` : ''}
            </option>
          ))}
        </select>
      </div>
      {activePlan?.composition && activePlan.composition.length > 0 && (
        <div className="plan-composition">
          {activePlan.composition.map((token, i) => (
            <span key={i} className="combo-token">
              {token}
            </span>
          ))}
          {comboMode && (
            <span className="chip violet" style={{ marginLeft: 4 }}>
              {comboMode === 'ALL' ? '交集' : '并集'}
            </span>
          )}
        </div>
      )}
      <div className="toolbar-spacer" />
      {onComboModeChange && comboMode && (
        <div className="segmented">
          <button
            className={clsx('segment', comboMode === 'ALL' && 'active')}
            onClick={() => onComboModeChange('ALL')}
          >
            ALL（交集）
          </button>
          <button
            className={clsx('segment', comboMode === 'ANY' && 'active')}
            onClick={() => onComboModeChange('ANY')}
          >
            ANY（并集）
          </button>
        </div>
      )}
    </div>
  )
}
