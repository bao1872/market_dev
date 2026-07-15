// [AtomicFactsPanel] - 描述: Atomic Fact Contract V1 用户侧状态观察面板
// 复用 StockContext 单一接口（GET /api/v1/stocks/{symbol}/context），market/detail 共用 query key
// variant='compact'（/market 右栏）：固定四组顺序 趋势4/动量4/结构5/成交1，分母14，面板内滚动
// variant='expanded'（/stock/:symbol 详情）：概览 + 近期变化 + 默认收起更多
// 面板关闭时由父组件不挂载本组件，useStockContext enabled=false（0 请求）
// 通俗中文；禁综合分/反转概率/买卖/成熟衰竭/便宜昂贵/止损/安全/放量缩量
import { useState } from 'react'
import { useStockContext } from '@/hooks/useApi'
import type {
  AtomicFactItem,
  AtomicFactChange,
  AtomicFactsContextResponse,
} from '@/api/endpoints'
import { getReasonCodeMessage } from './reasonCodeMessages'
import styles from './AtomicFactsPanel.module.scss'

const DIMENSION_ORDER = ['trend', 'momentum', 'structure', 'volume'] as const
type Dimension = (typeof DIMENSION_ORDER)[number]
const DIMENSION_LABEL: Record<Dimension, string> = {
  trend: '趋势',
  momentum: '动量',
  structure: '结构',
  volume: '成交量',
}

interface AtomicFactsPanelProps {
  /** 股票代码（symbol），用于调用 StockContext API */
  symbol: string | undefined
  /** 历史查询日期（可选，不传则查最新） */
  asOf?: string | null
  /** compact=右栏小卡下；expanded=详情页右面板（含近期变化与更多） */
  variant?: 'compact' | 'expanded'
}

function buildFactLabelMap(data: AtomicFactsContextResponse): Record<string, string> {
  const map: Record<string, string> = {}
  for (const dim of DIMENSION_ORDER) {
    for (const it of data.core[dim] ?? []) {
      map[it.factId] = it.label
    }
  }
  return map
}

function changeText(category: string | null, value: number | null): string {
  if (category != null) return category
  if (value != null) return String(value)
  return '—'
}

/** 数据质量简报（更多区块内复用） */
function DataQualityBlock({
  dataQuality,
}: {
  dataQuality: AtomicFactsContextResponse['dataQuality']
}) {
  const msg = getReasonCodeMessage(dataQuality.reasonCode, dataQuality.runTradeDate)
  return (
    <div className={styles.moreRow}>
      <span>数据质量</span>
      <span>
        {dataQuality.hasSucceededRun ? '有发布批次' : '无发布批次'}
        {dataQuality.hasSnapshot ? ' · 有快照' : ' · 无快照'}
        {msg ? ` · ${msg.title}` : ''}
      </span>
    </div>
  )
}

/** 空态：Core 全缺失时展示 reasonCode 文案 */
function NoStateBlock({
  dataQuality,
}: {
  dataQuality: AtomicFactsContextResponse['dataQuality']
}) {
  const msg = getReasonCodeMessage(dataQuality.reasonCode, dataQuality.runTradeDate)
  if (!msg) return null
  return (
    <div className={styles.noStateInner}>
      <div>{msg.title}</div>
      {msg.meta && <div className={styles.noStateMeta}>{msg.meta}</div>}
    </div>
  )
}

export function AtomicFactsPanel({
  symbol,
  asOf,
  variant = 'compact',
}: AtomicFactsPanelProps) {
  const query = useStockContext(symbol, asOf ? { as_of: asOf } : undefined, {
    enabled: true, // 本组件只在面板打开时渲染，故始终 enabled
  })
  const [moreOpen, setMoreOpen] = useState(false)

  if (!symbol) {
    return (
      <div className={styles.empty}>
        <div className={styles.emptyText}>请选择一只股票查看状态观察</div>
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
        <button className={styles.retryBtn} onClick={() => query.refetch()}>
          重试
        </button>
      </div>
    )
  }

  const data = query.data
  const { core, availability, recentChanges, dataQuality, contractVersion, asOf: asOfDate } = data
  const labels = buildFactLabelMap(data)
  const groups = DIMENSION_ORDER.map((dim) => ({ dim, items: core[dim] ?? [] }))
  const hasFacts = availability.corePresent > 0

  return (
    <div className={`${styles.panel} ${variant === 'compact' ? styles.panelCompact : ''}`}>
      <div className={styles.header}>
        <span className={styles.contractVersion}>{contractVersion}</span>
        <span className={styles.denominator}>
          {availability.corePresent}/{availability.coreDenominator}
        </span>
      </div>
      {asOfDate && <div className={styles.asOf}>状态截止：{asOfDate}</div>}

      {hasFacts ? (
        <>
          {groups.map(({ dim, items }) => (
            <section key={dim} className={styles.group}>
              <h4 className={styles.groupTitle}>{DIMENSION_LABEL[dim]}</h4>
              <div className={styles.factList}>
                {items.map((fact: AtomicFactItem) => (
                  <div
                    key={fact.factId}
                    className={`${styles.factRow} ${fact.missing ? styles.factMissing : ''}`}
                  >
                    <span className={styles.factLabel}>{fact.label}</span>
                    <span className={styles.factValue}>
                      {fact.displayText}
                      {!fact.missing && fact.category != null && (
                        <span className={styles.factCategory}>{fact.category}</span>
                      )}
                    </span>
                  </div>
                ))}
              </div>
            </section>
          ))}

          {variant === 'expanded' && (
            <section className={styles.group}>
              <h4 className={styles.groupTitle}>近期变化</h4>
              {recentChanges.length === 0 ? (
                <div className={styles.groupEmpty}>暂无近期变化</div>
              ) : (
                <ul className={styles.changeList}>
                  {recentChanges.map((ch: AtomicFactChange, i: number) => (
                    <li key={`${ch.factId}-${i}`} className={styles.changeItem}>
                      <span className={styles.changeFact}>{labels[ch.factId] ?? ch.factId}</span>
                      <span className={styles.changeArrow}>
                        {changeText(ch.fromCategory, ch.fromValue)} →{' '}
                        {changeText(ch.toCategory, ch.toValue)}
                      </span>
                      <span className={styles.changeAsOf}>{ch.asOf}</span>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          )}

          {variant === 'expanded' && (
            <section className={styles.group}>
              <button
                className={styles.moreToggle}
                onClick={() => setMoreOpen((v) => !v)}
                aria-expanded={moreOpen}
              >
                {moreOpen ? '收起更多信息' : '查看更多信息'}
              </button>
              {moreOpen && (
                <div className={styles.moreContent}>
                  <div className={styles.moreRow}>
                    <span>合同版本</span>
                    <span>{contractVersion}</span>
                  </div>
                  <div className={styles.moreRow}>
                    <span>Core 可用 / 分母</span>
                    <span>
                      {availability.corePresent} / {availability.coreDenominator}
                    </span>
                  </div>
                  {availability.coreMissing.length > 0 && (
                    <div className={styles.moreRow}>
                      <span>缺失事实</span>
                      <span>{availability.coreMissing.join('、')}</span>
                    </div>
                  )}
                  {availability.auxiliaryHidden.length > 0 && (
                    <div className={styles.moreRow}>
                      <span>默认隐藏</span>
                      <span>{availability.auxiliaryHidden.join('、')}</span>
                    </div>
                  )}
                  <DataQualityBlock dataQuality={dataQuality} />
                </div>
              )}
            </section>
          )}
        </>
      ) : (
        <div className={styles.noState}>
          <NoStateBlock dataQuality={dataQuality} />
        </div>
      )}
    </div>
  )
}
