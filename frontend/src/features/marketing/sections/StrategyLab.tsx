// StrategyLab（Full Alignment V1 · 新增核心交互）：
// 4 个经典场景 tab；选中场景展示：
//   左：逻辑说明
//   中：实际 filter chips（绿色 = 已选）
//   右：候选数量漏斗
// 约束：不宣称「精确实现经典道氏123」，统一写「道氏123风格」（字段为可编程近似）。
import { useState } from 'react'
import clsx from 'clsx'
import ScrollReveal from '../components/ScrollReveal'
import SectionHeading from '../components/SectionHeading'
import { SCENARIO_ICONS, type ScenarioIconKey } from '../components/MarketingIcons'
import { STRATEGY_LAB } from '../data/copy'
import styles from '../marketing.module.scss'

export default function StrategyLab() {
  const [activeId, setActiveId] = useState(STRATEGY_LAB.scenarios[0].id)
  const active =
    STRATEGY_LAB.scenarios.find((s) => s.id === activeId) ?? STRATEGY_LAB.scenarios[0]
  const maxCount = active.funnel[0]?.count ?? 1

  return (
    <section
      className={styles.section}
      id="strategy-lab"
      data-testid="marketing-strategy-lab"
    >
      <div className={styles.container}>
        <SectionHeading
          index={STRATEGY_LAB.index}
          eyebrow={STRATEGY_LAB.eyebrow}
          title={STRATEGY_LAB.title}
          subtitle={STRATEGY_LAB.subtitle}
        />

        {/* 场景 tab 行 */}
        <div className={styles.labTabs} role="tablist" aria-label="经典场景">
          {STRATEGY_LAB.scenarios.map((sc) => {
            const Icon = SCENARIO_ICONS[sc.icon as ScenarioIconKey]
            return (
              <button
                key={sc.id}
                type="button"
                role="tab"
                aria-selected={sc.id === activeId}
                className={clsx(styles.labTab, sc.id === activeId && styles.labTabActive)}
                onClick={() => setActiveId(sc.id)}
              >
                {Icon ? <Icon width={18} height={18} /> : null}
                <span>{sc.name}</span>
              </button>
            )
          })}
        </div>

        <ScrollReveal>
          <div className={styles.labWorkspace}>
            {/* 左：逻辑说明 */}
            <div className={styles.labLogic}>
              <h3 className={styles.labLogicTitle}>{active.name}</h3>
              <p className={styles.labLogicText}>{active.logic}</p>
            </div>

            {/* 中：filter chips */}
            <div className={styles.labChips}>
              <span className={styles.labChipsLabel}>筛选条件</span>
              <div className={styles.labChipRow}>
                {active.chips.map((chip) => (
                  <span
                    key={chip.label}
                    className={clsx(
                      styles.labChip,
                      chip.active && styles.labChipActive,
                    )}
                  >
                    {chip.active ? '✓ ' : ''}
                    {chip.label}
                  </span>
                ))}
              </div>
            </div>

            {/* 右：候选数量漏斗 */}
            <div className={styles.labFunnel}>
              <span className={styles.labFunnelLabel}>候选数量</span>
              <ul className={styles.labFunnelList}>
                {active.funnel.map((row) => (
                  <li key={row.label} className={styles.labFunnelRow}>
                    <span className={styles.labFunnelName}>{row.label}</span>
                    <span className={styles.labFunnelBarWrap}>
                      <span
                        className={styles.labFunnelBar}
                        style={{
                          width: `${Math.max(6, (row.count / maxCount) * 100)}%`,
                        }}
                      />
                    </span>
                    <span className={styles.labFunnelCount}>{row.count.toLocaleString()}</span>
                  </li>
                ))}
              </ul>
            </div>
          </div>
        </ScrollReveal>

        <p className={styles.labNote}>{STRATEGY_LAB.note}</p>
      </div>
    </section>
  )
}
