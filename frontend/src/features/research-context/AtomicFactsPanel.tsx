// [AtomicFactsPanel] - 描述: Atomic Fact Contract V1 用户侧状态观察面板（compact /market 右栏；expanded /stock Drawer）
// 复用 StockContext 单一接口（GET /api/v1/stocks/{symbol}/context），market/detail 共用 query key。
// 四组固定：趋势运行(info) / 动量配合(brand) / 结构位置(purple) / 成交参与(warning)。
// 每项一行：标签左、主值右、弱说明在下；S3 用 0–1 轨道（0.33/0.67），T5/V3 显示「分类未启用」，
// S7/S8 显示「尚未到达/已越过」。内部滚动，不改变左侧列表与小 K 线高度。
// 面板关闭时由父组件不挂载，useStockContext enabled=false（0 请求）。
// 普通用户 DOM 不得出现内部研究字段或英文术语（如字段路径、研究内部代号等）。
import { useState } from 'react'
import { useStockContext } from '@/hooks/useApi'
import type {
  AtomicFactItem,
  AtomicFactsContextResponse,
  AtomicFactChange,
} from '@/api/endpoints'
import { getReasonCodeMessage } from './reasonCodeMessages'
import styles from './AtomicFactsPanel.module.scss'

// 冻结研究合同版本（V4.13），仅用于 UI 标注，与后端 contractVersion 解耦。
const AFC_RESEARCH_VERSION = 'V4.13'

const DIMENSION_LABEL: Record<string, string> = {
  trend: '趋势运行',
  momentum: '动量配合',
  structure: '结构位置',
  volume: '成交参与',
}
const DIMENSION_GROUP_CLASS: Record<string, string> = {
  trend: styles.groupTrend,
  momentum: styles.groupMomentum,
  structure: styles.groupStructure,
  volume: styles.groupVolume,
}
const DIMENSION_ORDER = ['trend', 'momentum', 'structure', 'volume'] as const

const DISCLAIMER = '以上为状态描述，不构成买卖建议'

/** 关系/状态中性徽章（不表达涨跌、利好利空） */
export function RelationBadge({ text, tone }: { text: string | null; tone?: 'neutral' | 'warn' }) {
  if (!text) return null
  return (
    <span className={`${styles.badge} ${tone === 'warn' ? styles.badgeWarn : styles.badgeNeutral}`}>
      {text}
    </span>
  )
}

/** 通用事实行：标签左 / 主值右 / 弱说明下；relation 用中性徽章，ratio 未启用时附「分类未启用」 */
export function FactMetricRow({ fact }: { fact: AtomicFactItem }) {
  const isRatio = fact.visualKind === 'ratio'
  const showUnclassified = isRatio && fact.thresholdEnabled === false
  return (
    <div className={styles.factRow}>
      <span className={styles.factLabel}>{fact.label}</span>
      <span className={styles.factValue}>
        {fact.valueText}
        {fact.categoryLabel && fact.visualKind === 'relation' && (
          <RelationBadge text={fact.categoryLabel} />
        )}
        {showUnclassified && <span className={styles.unclassified}>分类未启用</span>}
      </span>
      {fact.secondaryText && <span className={styles.factSecondary}>{fact.secondaryText}</span>}
    </div>
  )
}

/** S3/S6 位置轨道：0–1 真实轨道，标注 0.33 / 0.67 边界 */
export function PositionRail({ fact }: { fact: AtomicFactItem }) {
  const v = fact.value
  const pct = v == null ? 0 : Math.max(0, Math.min(1, v)) * 100
  return (
    <div className={styles.factRow}>
      <span className={styles.factLabel}>{fact.label}</span>
      <div className={styles.railTrack} role="img" aria-label={`${fact.label}：${fact.valueText}`}>
        <span className={styles.railTick} style={{ left: '33%' }} />
        <span className={styles.railTick} style={{ left: '67%' }} />
        <span className={styles.railKnob} style={{ left: `${pct}%` }} />
      </div>
      <span className={styles.factSecondary}>
        {fact.categoryLabel ? `${fact.categoryLabel} · ` : ''}
        {fact.valueText}
      </span>
    </div>
  )
}

/** S7/S8 边界距离：显示「尚未到达 / 已越过」 */
export function BoundaryRow({ fact }: { fact: AtomicFactItem }) {
  const crossed = fact.valueText.includes('已越过')
  return (
    <div className={styles.factRow}>
      <span className={styles.factLabel}>{fact.label}</span>
      <span className={styles.factValue}>
        {fact.valueText}
        <RelationBadge text={crossed ? '已越过' : '尚未到达'} tone={crossed ? 'warn' : 'neutral'} />
      </span>
    </div>
  )
}

/** 单个 Core 组卡（固定四组之一） */
export function CoreFactGroup({ dimension, items }: { dimension: string; items: AtomicFactItem[] }) {
  if (items.length === 0) return null
  return (
    <section className={`${styles.group} ${DIMENSION_GROUP_CLASS[dimension] ?? ''}`}>
      <h4 className={styles.groupTitle}>{DIMENSION_LABEL[dimension] ?? dimension}</h4>
      <div className={styles.factList}>
        {items.map((fact) => {
          if (fact.visualKind === 'position') return <PositionRail key={fact.publicKey} fact={fact} />
          if (fact.visualKind === 'distance') return <BoundaryRow key={fact.publicKey} fact={fact} />
          return <FactMetricRow key={fact.publicKey} fact={fact} />
        })}
      </div>
    </section>
  )
}

/** 近期变化（全宽） */
export function RecentChangesStrip({ changes }: { changes: AtomicFactChange[] }) {
  if (changes.length === 0) {
    return <div className={styles.changesEmpty}>暂无近期变化</div>
  }
  return (
    <ul className={styles.changeList}>
      {changes.map((c, i) => (
        <li key={`${c.publicKey}-${i}`} className={styles.changeItem}>
          <span className={styles.changeFact}>{c.publicKey}</span>
          <span className={styles.changeArrow}>
            {c.fromText ?? '—'} → {c.toText ?? '—'}
          </span>
          <span className={styles.changeDelta}>{c.deltaText}</span>
          <span className={styles.changeAsOf}>{c.asOf}</span>
        </li>
      ))}
    </ul>
  )
}

/** 更多观察（Auxiliary 默认收起，展开真实渲染 8 项；T3/T6/V1 永不出现） */
export function AuxiliaryAccordion({ auxiliary }: { auxiliary: AtomicFactItem[] }) {
  const [open, setOpen] = useState(false)
  return (
    <section className={styles.auxSection}>
      <button
        type="button"
        className={styles.auxToggle}
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        {open ? '收起更多观察' : '更多观察'}
      </button>
      {open && (
        <div className={styles.auxList}>
          {auxiliary.length === 0 ? (
            <div className={styles.changesEmpty}>暂无更多观察</div>
          ) : (
            auxiliary.map((fact) => {
              if (fact.visualKind === 'position') return <PositionRail key={fact.publicKey} fact={fact} />
              if (fact.visualKind === 'distance') return <BoundaryRow key={fact.publicKey} fact={fact} />
              return <FactMetricRow key={fact.publicKey} fact={fact} />
            })
          )}
        </div>
      )}
    </section>
  )
}

interface AtomicFactsPanelProps {
  symbol: string | undefined
  asOf?: string | null
  variant?: 'compact' | 'expanded'
}

function Header({ data }: { data: AtomicFactsContextResponse }) {
  return (
    <div className={styles.header}>
      <div className={styles.headerTitle}>个股状态观察</div>
      <div className={styles.headerMeta}>
        <span className={styles.headerVersion}>日线 · {AFC_RESEARCH_VERSION}</span>
        {data.asOf && <span className={styles.headerAsOf}>观察日期 {data.asOf}</span>}
        <span className={styles.headerDenom}>
          {data.availability.corePresent}/{data.availability.coreDenominator}
        </span>
      </div>
    </div>
  )
}

function EmptyState({ data }: { data: AtomicFactsContextResponse }) {
  const msg = getReasonCodeMessage(data.dataQuality.reasonCode, data.dataQuality.runTradeDate)
  if (!msg) return null
  return (
    <div className={styles.empty}>
      <div className={styles.errorText}>{msg.title}</div>
      {msg.meta && <div className={styles.loadingText}>{msg.meta}</div>}
    </div>
  )
}

/** 共享：四组卡（compact 纵向 / expanded 网格由父容器 className 控制） */
function GroupCards({ data }: { data: AtomicFactsContextResponse }) {
  return (
    <>
      {DIMENSION_ORDER.map((dim) => (
        <CoreFactGroup key={dim} dimension={dim} items={data.core[dim] ?? []} />
      ))}
    </>
  )
}

/** AtomicFactsPanel — compact（/market 右栏）或 expanded（/stock Drawer 内） */
export function AtomicFactsPanel({
  symbol,
  asOf,
  variant = 'compact',
}: AtomicFactsPanelProps) {
  const query = useStockContext(symbol, asOf ? { as_of: asOf } : undefined, { enabled: true })

  if (!symbol) {
    return (
      <div className={styles.empty}>
        <div className={styles.loadingText}>请选择一只股票查看状态观察</div>
      </div>
    )
  }
  if (query.isLoading) {
    return (
      <div className={styles.loading}>
        <div className={styles.loadingText}>加载中…</div>
      </div>
    )
  }
  if (query.isError || !query.data) {
    return (
      <div className={styles.error}>
        <div className={styles.errorText}>数据加载失败</div>
        <button type="button" className={styles.retryBtn} onClick={() => query.refetch()}>
          重试
        </button>
      </div>
    )
  }

  const data = query.data
  const { availability, recentChanges, auxiliary } = data
  const hasFacts = availability.corePresent > 0

  if (variant === 'expanded') {
    return (
      <div className={styles.panel}>
        <Header data={data} />
        {hasFacts ? (
          <>
            <div className={styles.groupGrid}>
              <GroupCards data={data} />
            </div>
            <section className={styles.recentSection}>
              <h4 className={styles.recentTitle}>近期变化</h4>
              <RecentChangesStrip changes={recentChanges} />
            </section>
            <AuxiliaryAccordion auxiliary={auxiliary} />
          </>
        ) : (
          <EmptyState data={data} />
        )}
        <div className={styles.disclaimer}>{DISCLAIMER}</div>
      </div>
    )
  }

  return (
    <div className={`${styles.panel} ${styles.panelCompact}`}>
      <Header data={data} />
      {hasFacts ? <GroupCards data={data} /> : <EmptyState data={data} />}
      <div className={styles.disclaimer}>{DISCLAIMER}</div>
    </div>
  )
}

export default AtomicFactsPanel
