// [AtomicFactsPanel] - 描述: Atomic Fact Contract V1 用户侧状态观察面板（compact /market 右栏；expanded /stock Drawer）
// 复用 StockContext 单一接口（GET /api/v1/stocks/{symbol}/context），market/detail 共用 query key。
// 四组固定：趋势运行(info) / 动量配合(brand) / 结构位置(purple) / 成交参与(warning)。
// 事实行按 visualKind 渲染（metric/value_with_category/relation/position/distance/ratio/confirmed_position），
// 禁止解析中文推断类型/状态；事实行非卡片（CSS Grid 透明行，仅底部分隔线）。
// S3/S6 用完整轨道（低位/0.33/0.67/高位 + 圆点 + `0.63 · 中间`）。
// CHANGE-20260716-006: confirmed_swing_position（产品观察，不计入 14/14）插入结构组 S1 之后，
// 范围内 0–1 全宽轨道；<0 显示「低于确认区间」；>1 显示「高于确认区间」。
// Auxiliary 按 动量补充/结构补充/成交补充 分组，默认收起。
// 近期变化仅显示最近一个交易日（latestChangesAsOf），无变化显示「最近交易日无状态变化」。
// 面板关闭时由父组件不挂载，useStockContext enabled=false（0 请求）。
// 普通用户 DOM 不得出现内部研究字段或英文术语（如字段路径、研究内部代号等）。
import { useState } from 'react'
import { useStockContext } from '@/hooks/useApi'
import type {
  AtomicFactItem,
  AtomicFactsContextResponse,
  AtomicFactChange,
  ProductObservationItem,
} from '@/api/endpoints'
import { getReasonCodeMessage } from './reasonCodeMessages'
import styles from './AtomicFactsPanel.module.scss'

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

// Auxiliary 分组（按 dimension 归类，展示中文组标题）
const AUX_GROUP_DEFS: { key: string; title: string }[] = [
  { key: 'momentum', title: '动量补充' },
  { key: 'structure', title: '结构补充' },
  { key: 'volume', title: '成交补充' },
]

/** 中性关系徽章（不表达涨跌、利好利空） */
export function RelationBadge({ text, tone }: { text: string | null; tone?: 'neutral' | 'warn' }) {
  if (!text) return null
  return (
    <span className={`${styles.badge} ${tone === 'warn' ? styles.badgeWarn : styles.badgeNeutral}`}>
      {text}
    </span>
  )
}

/**
 * 按 visualKind 渲染单个事实行。
 * - metric: 数值（mono + 高字重）+ secondaryText
 * - value_with_category: 数值 + categoryLabel 徽章
 * - relation: 仅 categoryLabel 徽章（一次，不重复）
 * - position: 完整轨道（PositionRail）
 * - distance: categoryLabel 徽章 + 数值（各一次）
 * - ratio: 数值 + secondaryText（未启用分类提示由后端置入，仅一次）
 */
export function FactRow({ fact }: { fact: AtomicFactItem }) {
  if (fact.visualKind === 'position') return <PositionRail fact={fact} />
  if (fact.visualKind === 'distance') {
    return (
      <div className={styles.factRow}>
        <span className={styles.factLabel}>{fact.label}</span>
        <span className={styles.factValue}>
          <RelationBadge text={fact.categoryLabel} tone={fact.categoryLabel === '已越过' ? 'warn' : 'neutral'} />
          <span className={styles.factValueMetric}>{fact.valueText}</span>
        </span>
      </div>
    )
  }
  if (fact.visualKind === 'relation') {
    return (
      <div className={styles.factRow}>
        <span className={styles.factLabel}>{fact.label}</span>
        <span className={styles.factValue}>
          <RelationBadge text={fact.categoryLabel} />
        </span>
      </div>
    )
  }
  if (fact.visualKind === 'value_with_category') {
    return (
      <div className={styles.factRow}>
        <span className={styles.factLabel}>{fact.label}</span>
        <span className={styles.factValue}>
          <span className={styles.factValueMetric}>{fact.valueText}</span>
          {fact.categoryLabel && <RelationBadge text={fact.categoryLabel} />}
        </span>
      </div>
    )
  }
  // metric / ratio：数值 + secondaryText（ratio 的未启用分类提示由后端置入 secondaryText）
  return (
    <div className={styles.factRow}>
      <span className={styles.factLabel}>{fact.label}</span>
      <span className={styles.factValue}>
        <span className={styles.factValueMetric}>{fact.valueText}</span>
      </span>
      {fact.secondaryText && <span className={styles.factSecondary}>{fact.secondaryText}</span>}
    </div>
  )
}

/** S3/S6 位置轨道：第一行 label 左 / `0.63 · 中间` 右；第二行轨道横跨整组宽度
 *  轨道显示 低位 / 0.33 / 0.67 / 高位 四个刻度，预留刻度高度，禁止刻度与 caption 重叠 */
export function PositionRail({ fact }: { fact: AtomicFactItem }) {
  const v = fact.value
  const pct = v == null ? 0 : Math.max(0, Math.min(1, v)) * 100
  const valText = fact.valueText ?? '—'
  return (
    <div className={styles.positionRow}>
      <span className={styles.factLabel}>{fact.label}</span>
      <span className={styles.railCaption}>
        {valText}
        {fact.categoryLabel ? ` · ${fact.categoryLabel}` : ''}
      </span>
      <div className={styles.railTrackWrap}>
        <div
          className={styles.railTrack}
          role="img"
          aria-label={`${fact.label}：${valText} · ${fact.categoryLabel ?? ''}`}
        >
          <span className={styles.railTick} style={{ left: '33%' }} />
          <span className={styles.railTick} style={{ left: '67%' }} />
          <span className={styles.railKnob} style={{ left: `${pct}%` }} />
        </div>
        <div className={styles.railScale}>
          <span>低位</span>
          <span>0.33</span>
          <span>0.67</span>
          <span>高位</span>
        </div>
      </div>
    </div>
  )
}

/**
 * CHANGE-20260716-006: 最近确认区间位置（产品观察，不计入 Core 14/14）。
 *
 * 渲染规则：
 * - 范围内（value != null，0≤原始值≤1）：0–1 全宽轨道 + 0.33/0.67 边界 + 圆点 + `value · categoryLabel`
 * - 原始值 < 0：badge「低于确认区间」（不静默 clip 到 0）
 * - 原始值 > 1：badge「高于确认区间」（不静默 clip 到 1）
 */
export function ConfirmedPositionRow({ obs }: { obs: ProductObservationItem }) {
  // 范围外：badge 行（label 左 / badge 右）
  if (obs.value == null) {
    return (
      <div className={styles.factRow}>
        <span className={styles.factLabel}>{obs.label}</span>
        <span className={styles.factValue}>
          <RelationBadge text={obs.categoryLabel} />
        </span>
      </div>
    )
  }
  // 范围内：0–1 全宽轨道（与 S3 一致的 0.33/0.67 边界）
  const pct = Math.max(0, Math.min(1, obs.value)) * 100
  const valText = obs.valueText ?? '—'
  return (
    <div className={styles.positionRow}>
      <span className={styles.factLabel}>{obs.label}</span>
      <span className={styles.railCaption}>
        {valText}
        {obs.categoryLabel ? ` · ${obs.categoryLabel}` : ''}
      </span>
      <div className={styles.railTrackWrap}>
        <div
          className={styles.railTrack}
          role="img"
          aria-label={`${obs.label}：${valText} · ${obs.categoryLabel ?? ''}`}
        >
          <span className={styles.railTick} style={{ left: '33%' }} />
          <span className={styles.railTick} style={{ left: '67%' }} />
          <span className={styles.railKnob} style={{ left: `${pct}%` }} />
        </div>
        <div className={styles.railScale}>
          <span>低位</span>
          <span>0.33</span>
          <span>0.67</span>
          <span>高位</span>
        </div>
      </div>
    </div>
  )
}

/** 单个 Core 组卡（固定四组之一）；structure 组可追加产品观察项（插入 S1 之后） */
export function CoreFactGroup({
  dimension,
  items,
  productItems,
  insertAfterPublicKey,
}: {
  dimension: string
  items: AtomicFactItem[]
  /** 产品观察项（CHANGE-20260716-006，仅 structure 组使用） */
  productItems?: ProductObservationItem[]
  /** 产品观察项插入位置：指定 publicKey 之后；未匹配时追加到末尾 */
  insertAfterPublicKey?: string
}) {
  if (items.length === 0 && (!productItems || productItems.length === 0)) return null
  // 计算插入点：匹配 insertAfterPublicKey 之后；未匹配时追加到末尾
  const insertIdx =
    insertAfterPublicKey != null && productItems && productItems.length > 0
      ? items.findIndex((f) => f.publicKey === insertAfterPublicKey)
      : -1
  const before = insertIdx >= 0 ? items.slice(0, insertIdx + 1) : items
  const after = insertIdx >= 0 ? items.slice(insertIdx + 1) : []
  return (
    <section className={`${styles.group} ${DIMENSION_GROUP_CLASS[dimension] ?? ''}`}>
      <h4 className={styles.groupTitle}>{DIMENSION_LABEL[dimension] ?? dimension}</h4>
      <div className={styles.factList}>
        {before.map((fact) => (
          <FactRow key={fact.publicKey} fact={fact} />
        ))}
        {productItems?.map((obs) => (
          <ConfirmedPositionRow key={`product-${obs.publicKey}`} obs={obs} />
        ))}
        {after.map((fact) => (
          <FactRow key={fact.publicKey} fact={fact} />
        ))}
      </div>
    </section>
  )
}

/**
 * 近期变化（CHANGE-20260716-006：仅最近一个交易日发生变化的项）。
 *
 * 显示规则：
 * - latestChangesAsOf 存在 + changes 非空：标题「最近交易日变化 · {date}」+ 变化列表
 * - latestChangesAsOf 存在 + changes 空：「最近交易日（{date}）无状态变化」
 * - latestChangesAsOf 为 null：「暂无近期变化」（无已发布快照）
 */
export function RecentChangesStrip({
  changes,
  latestChangesAsOf,
}: {
  changes: AtomicFactChange[]
  latestChangesAsOf: string | null
}) {
  if (changes.length === 0) {
    const emptyText = latestChangesAsOf
      ? `最近交易日（${latestChangesAsOf}）无状态变化`
      : '暂无近期变化'
    return <div className={styles.changesEmpty}>{emptyText}</div>
  }
  return (
    <ul className={styles.changeList}>
      {changes.map((c, i) => (
        <li key={`${c.label}-${c.asOf}-${i}`} className={styles.changeItem}>
          <span className={styles.changeLabel}>{c.label}</span>
          <span className={styles.changeArrow}>
            {c.fromText ?? '—'} → {c.toText ?? '—'}
          </span>
          {c.deltaText && <span className={styles.changeDelta}>{c.deltaText}</span>}
          <span className={styles.changeAsOf}>{c.asOf}</span>
        </li>
      ))}
    </ul>
  )
}

/** 更多观察（Auxiliary 按 动量补充/结构补充/成交补充 分组，默认收起） */
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
        <>
          {auxiliary.length === 0 ? (
            <div className={styles.changesEmpty}>暂无更多观察</div>
          ) : (
            AUX_GROUP_DEFS.map((grp) => {
              const items = auxiliary.filter((f) => f.dimension === grp.key)
              if (items.length === 0) return null
              return (
                <div key={grp.key} className={styles.auxGroup}>
                  <h5 className={styles.auxGroupTitle}>{grp.title}</h5>
                  <div className={styles.auxList}>
                    {items.map((fact) => (
                      <FactRow key={fact.publicKey} fact={fact} />
                    ))}
                  </div>
                </div>
              )
            })
          )}
        </>
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
      <div className={styles.headerRow}>
        <div className={styles.headerTitle}>个股状态观察</div>
        <div className={styles.headerMeta}>
          <span className={styles.headerVersion}>日线 · {data.meta.researchFreezeVersion}</span>
          <span className={styles.headerDenom}>
            {data.availability.corePresent}/{data.availability.coreDenominator}
          </span>
        </div>
      </div>
      {data.asOf && <div className={styles.headerAsOf}>观察日期 {data.asOf}</div>}
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
      {DIMENSION_ORDER.map((dim) => {
        if (dim === 'structure') {
          // CHANGE-20260716-006: 结构组在 S1 (boundary_relation) 之后插入产品观察项
          return (
            <CoreFactGroup
              key={dim}
              dimension={dim}
              items={data.core[dim] ?? []}
              productItems={data.productObservations?.structure ?? []}
              insertAfterPublicKey="boundary_relation"
            />
          )
        }
        return <CoreFactGroup key={dim} dimension={dim} items={data.core[dim] ?? []} />
      })}
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
  const { availability, recentChanges, auxiliary, latestChangesAsOf } = data
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
              <h4 className={styles.recentTitle}>
                {latestChangesAsOf ? `最近交易日变化 · ${latestChangesAsOf}` : '近期变化'}
              </h4>
              <RecentChangesStrip
                changes={recentChanges}
                latestChangesAsOf={latestChangesAsOf}
              />
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
