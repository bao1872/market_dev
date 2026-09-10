// StrategyLab（Full Alignment V1.5 · 真实玩法案例）：
// 4 个玩法 tab（道氏123风格 / 趋势跟踪 / 双底支撑 / 更多玩法）。
// 前三为「真实产品截图 + 文字说明」的大案例舞台（桌面截图约 68% + 解释约 32%），
// 第四为「更多玩法待你探索」的探索板（不伪造第四张截图、不显示空白图片区）。
// 边界（owner 裁决）：统一用「道氏123风格 / 思路」，不宣称精确实现经典定义；
// 不虚构候选数量漏斗；盘迹是一套状态语言，不是一套固定策略。
import { useState } from 'react'
import clsx from 'clsx'
import ScrollReveal from '../components/ScrollReveal'
import SectionHeading from '../components/SectionHeading'
import { STRATEGY_LAB, type RealStrategyCase, type ExploreStrategyCase } from '../data/copy'
import styles from '../marketing.module.scss'

export default function StrategyLab() {
  const [activeId, setActiveId] = useState<string>(STRATEGY_LAB.cases[0].id)
  const active: RealStrategyCase | ExploreStrategyCase =
    STRATEGY_LAB.cases.find((item) => item.id === activeId) ?? STRATEGY_LAB.cases[0]

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

        {/* 玩法 tab 行 */}
        <div className={styles.caseTabs} role="tablist" aria-label="盘迹真实玩法案例">
          {STRATEGY_LAB.cases.map((item) => (
            <button
              key={item.id}
              type="button"
              role="tab"
              aria-selected={active.id === item.id}
              className={clsx(styles.caseTab, active.id === item.id && styles.caseTabActive)}
              onClick={() => setActiveId(item.id)}
            >
              {item.tabLabel}
            </button>
          ))}
        </div>

        <ScrollReveal>
          {active.kind === 'case' ? (
            <RealCaseStage data={active} />
          ) : (
            <ExploreStage data={active} />
          )}
        </ScrollReveal>
      </div>
    </section>
  )
}

// 真实产品截图案例：桌面截图 68% + 解释 32%。
function RealCaseStage({ data }: { data: RealStrategyCase }) {
  return (
    <article className={styles.caseStage}>
      <figure className={styles.caseScreenshot}>
        <div className={styles.caseImageMeta}>
          <span>真实产品截图</span>
          <span>历史案例</span>
        </div>
        <img src={data.imageSrc} alt={data.imageAlt} loading="lazy" />
      </figure>

      <aside className={styles.caseExplanation}>
        <span className={styles.caseStock}>
          {data.stock} · {data.symbol}
        </span>
        <h3 className={styles.casePlaybook}>{data.playbook}</h3>
        <p className={styles.caseLead}>{data.lead}</p>

        <div className={styles.caseExplainBlock}>
          <span>这个案例在看什么</span>
          <p>{data.what}</p>
        </div>

        <div className={styles.caseExplainBlock}>
          <span>盘迹怎么参与</span>
          <p>{data.panji}</p>
        </div>

        <p className={styles.caseDisclaimer}>{data.note}</p>
      </aside>
    </article>
  )
}

// 更多玩法探索板：不显示空白图片区，直接给出状态语言组合。
function ExploreStage({ data }: { data: ExploreStrategyCase }) {
  return (
    <article className={styles.exploreStage}>
      <div className={styles.exploreFormula}>
        <span>趋势</span>
        <b>×</b>
        <span>结构</span>
        <b>×</b>
        <span>动量</span>
        <b>×</b>
        <span>成交量</span>
        <b>×</b>
        <span>筹码</span>
        <b>×</b>
        <span>事件</span>
      </div>

      <h3>{data.playbook}</h3>
      <p className={styles.exploreLead}>{data.lead}</p>
      <p className={styles.exploreText}>{data.text}</p>

      <div className={styles.exploreExamples}>
        {data.examples.map((example) => (
          <span key={example}>{example}</span>
        ))}
      </div>
    </article>
  )
}