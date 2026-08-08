// [ReviewPage] - 描述: 复盘工作台主页面（PRD §3、§14、§15）
// 路由 /review；URL 参数：date/stage/scopeType/scopeKey/signalId/boardId/symbol/trackingTab
// URL 是状态 SSOT（前进后退可恢复）；React Query 数据获取；禁止自由 AI 结论
// 只轮询 computing 状态；404/422/500 显示明确原因及 request_id
// 阶段四个股跳转 /market 和 /stock/:symbol；保留 /boards/analysis
import { useMemo, useState, useEffect, useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { useToast } from '@/store/toast'
import { getReviewDates, getReviewOverview, extractReviewError } from '@/features/review/api'
import { reviewKeys } from '@/features/review/queryKeys'
import {
  decodeReviewUrl,
  encodeReviewUrl,
  patchReviewUrl,
  type ReviewUrlState,
} from '@/features/review/urlState'
import { COMPUTING_STATUSES } from '@/features/review/types'
import ReviewHeader from '@/features/review/ReviewHeader'
import ReviewStageNav from '@/features/review/ReviewStageNav'
import MarketScanPanel from '@/features/review/MarketScanPanel'
import FilterDiscoveryPanel from '@/features/review/FilterDiscoveryPanel'
import BoardAttributionPanel from '@/features/review/BoardAttributionPanel'
import StockValidationPanel from '@/features/review/StockValidationPanel'
import TrackingReviewPanel from '@/features/review/TrackingReviewPanel'
import AuctionBackflowPanel from '@/features/review/AuctionBackflowPanel'
import EvidenceDrawer, { type EvidenceTarget } from '@/features/review/EvidenceDrawer'
import type { ReviewScopeMetrics, ReviewSignal, ReviewAttribution, ReviewInstrument, ReviewStage } from '@/features/review/types'
import styles from '@/features/review/review.module.scss'

export default function ReviewPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const showToast = useToast((s) => s.show)

  // 1. 从 URL 解析状态（SSOT）
  const urlState = useMemo(() => decodeReviewUrl(searchParams), [searchParams])

  // 2. 可用日期（用于顶部前后切换 + 默认日期回填）
  const datesQuery = useQuery({
    queryKey: reviewKeys.dates(),
    queryFn: getReviewDates,
    staleTime: 5 * 60 * 1000,
  })
  const availableDates = datesQuery.data?.trade_dates ?? []

  // 首次加载无 date：回填最新已发布日期（replace，不污染历史）
  useEffect(() => {
    if (urlState.date) return
    const latest = datesQuery.data?.latest_trade_date
    if (!latest) return
    const params = encodeReviewUrl({ ...urlState, date: latest })
    setSearchParams(params, { replace: true })
  }, [urlState, datesQuery.data?.latest_trade_date, setSearchParams])

  const tradeDate = urlState.date ?? datesQuery.data?.latest_trade_date ?? ''

  // 3. 当日总览（仅 computing 时轮询，PRD §15）
  const overviewQuery = useQuery({
    queryKey: reviewKeys.overview(tradeDate),
    queryFn: () => getReviewOverview(tradeDate),
    enabled: !!tradeDate,
    staleTime: 30 * 1000,
    refetchInterval: (query) => {
      const status = query.state.data?.status
      return status && COMPUTING_STATUSES.has(status) ? 5000 : false
    },
  })

  // 4. URL 状态更新辅助
  const patchUrl = useCallback(
    (patch: Partial<ReviewUrlState>, replace = false) => {
      const next = patchReviewUrl(urlState, patch)
      setSearchParams(encodeReviewUrl(next), { replace })
    },
    [urlState, setSearchParams],
  )

  const handleDateChange = useCallback(
    (date: string) => {
      // 切换日期：清除 scope/signal/symbol（避免跨日残留）
      patchUrl({
        date,
        scopeType: null,
        scopeKey: null,
        scopeName: null,
        parentScopeType: null,
        parentScopeKey: null,
        signalId: null,
        symbol: null,
      })
    },
    [patchUrl],
  )

  const handleStageChange = useCallback(
    (stage: ReviewStage) => {
      patchUrl({ stage })
    },
    [patchUrl],
  )

  // 5. 证据抽屉目标（任意指标/信号/归因/股票可打开）
  const [evidenceTarget, setEvidenceTarget] = useState<EvidenceTarget | null>(null)

  // 阶段间联动
  const handleSelectScope = useCallback(
    (scope: ReviewScopeMetrics) => {
      patchUrl({
        scopeType: scope.scopeType,
        scopeKey: scope.scopeKey,
        scopeName: scope.scopeName,
        parentScopeType: scope.parentScopeType,
        parentScopeKey: scope.parentScopeKey,
        stage: 'signals',
      })
      setEvidenceTarget({ kind: 'metric', title: `${scope.scopeName} 范围指标`, payload: scope.p, meta: { sourceRunId: scope.reviewRunId } })
    },
    [patchUrl],
  )

  const handleSelectSignal = useCallback(
    (signal: ReviewSignal) => {
      patchUrl({ signalId: signal.id, stage: 'attribution' })
      setEvidenceTarget({ kind: 'signal', signal })
    },
    [patchUrl],
  )

  const handleViewAttribution = useCallback(
    (signal: ReviewSignal) => {
      patchUrl({ signalId: signal.id, stage: 'attribution' })
    },
    [patchUrl],
  )

  const handleViewHistory = useCallback(
    (signal: ReviewSignal) => {
      patchUrl({ signalId: signal.id, stage: 'tracking', trackingTab: 'history' })
    },
    [patchUrl],
  )

  const handleOpenInstrumentEvidence = useCallback((inst: ReviewInstrument) => {
    setEvidenceTarget({ kind: 'instrument', inst })
  }, [])

  const handleOpenAttributionEvidence = useCallback((attr: ReviewAttribution) => {
    setEvidenceTarget({ kind: 'attribution', attr })
  }, [])

  const handleSelectAttributionScope = useCallback(
    (attr: ReviewAttribution) => {
      patchUrl({
        parentScopeType: urlState.scopeType,
        parentScopeKey: urlState.scopeKey,
        scopeType: attr.childScopeType,
        scopeKey: attr.childScopeKey,
        scopeName: attr.childScopeName,
      })
    },
    [patchUrl, urlState.scopeType, urlState.scopeKey],
  )

  // 6. 面包屑（PRD §14.2：全市场 > 风格 > 行业 > 个股）
  const breadcrumb = useMemo(() => {
    const scopeTypeLabel: Record<string, string> = {
      market: '全市场',
      major_index: '主要指数',
      style: '风格',
      industry_l1: '一级行业',
      industry_l2: '二级行业',
      industry_l3: '三级行业',
      concept: '概念',
      instrument: '个股',
    }
    const parts: string[] = ['全市场']
    if (urlState.parentScopeType && urlState.parentScopeKey) {
      parts.push(`${scopeTypeLabel[urlState.parentScopeType] ?? urlState.parentScopeType} · ${urlState.parentScopeKey}`)
    }
    if (urlState.scopeType && urlState.scopeKey && urlState.scopeType !== 'market') {
      parts.push(`${scopeTypeLabel[urlState.scopeType] ?? urlState.scopeType} · ${urlState.scopeName ?? urlState.scopeKey}`)
    }
    if (urlState.symbol) {
      parts.push(urlState.symbol)
    }
    return parts
  }, [urlState.parentScopeType, urlState.parentScopeKey, urlState.scopeType, urlState.scopeKey, urlState.scopeName, urlState.symbol])

  // 7. 加载/异常态（PRD §17）
  const renderContent = () => {
    // 日期未确定
    if (!tradeDate) {
      if (datesQuery.isLoading) {
        return <StateBox title="加载复盘日期" desc="正在获取已发布复盘交易日..." />
      }
      if (datesQuery.isError) {
        const err = extractReviewError(datesQuery.error)
        return <StateBox title="复盘日期加载失败" desc={err.message} requestId={err.requestId} />
      }
      return (
        <StateBox
          title="尚无已发布复盘"
          desc="系统尚未发布任何复盘 run，盘后编排完成后将自动生成"
        />
      )
    }

    // 总览加载
    if (overviewQuery.isLoading) {
      return <StateBox title="加载复盘总览" desc={`正在获取 ${tradeDate} 复盘数据...`} />
    }

    // 总览异常
    if (overviewQuery.isError) {
      const err = extractReviewError(overviewQuery.error)
      // 404：当日尚未计算/未发布
      if (err.status === 404) {
        return (
          <StateBox
            title={`${tradeDate} 复盘尚未发布`}
            desc="当日复盘 run 尚未计算或未发布。请等待盘后编排完成或切换其他交易日"
          />
        )
      }
      return <StateBox title="复盘总览加载失败" desc={err.message} requestId={err.requestId} />
    }

    const status = overviewQuery.data?.status
    // 计算中（轮询）
    if (status && COMPUTING_STATUSES.has(status)) {
      return (
        <StateBox
          title="复盘计算中"
          desc={`当日复盘 run 状态：${status}。页面将每 5 秒自动刷新，发布完成后展示完整数据`}
        />
      )
    }
    // partial / completed_with_errors
    if (status === 'partial' || status === 'completed_with_errors') {
      // 仍展示各阶段数据（部分可用）
      // 不在此 return，继续渲染阶段内容
    }

    // 按阶段渲染
    switch (urlState.stage) {
      case 'scan':
        return (
          <MarketScanPanel
            tradeDate={tradeDate}
            activeScopeKey={urlState.scopeKey}
            onSelectScope={handleSelectScope}
          />
        )
      case 'signals':
        return (
          <FilterDiscoveryPanel
            tradeDate={tradeDate}
            scopeType={urlState.scopeType}
            scopeKey={urlState.scopeKey}
            activeSignalId={urlState.signalId}
            onSelectSignal={handleSelectSignal}
            onViewAttribution={handleViewAttribution}
            onViewHistory={handleViewHistory}
            onTrackingAdded={(s) => showToast('已加入追踪', `信号 ${s.signalType}`)}
          />
        )
      case 'attribution':
        return (
          <BoardAttributionPanel
            signalId={urlState.signalId}
            boardId={urlState.boardId}
            onOpenEvidence={(s) => setEvidenceTarget({ kind: 'signal', signal: s })}
            onOpenAttributionEvidence={handleOpenAttributionEvidence}
            onOpenInstrumentEvidence={handleOpenInstrumentEvidence}
            onSelectAttributionScope={handleSelectAttributionScope}
          />
        )
      case 'validation':
        return (
          <StockValidationPanel
            signalId={urlState.signalId}
            tradeDate={tradeDate}
            sourceCoreRunId={overviewQuery.data?.sourceCoreRunId ?? null}
            boardId={urlState.boardId}
            activeSymbol={urlState.symbol}
            onOpenInstrumentEvidence={handleOpenInstrumentEvidence}
            showToast={showToast}
          />
        )
      case 'tracking':
        return (
          <TrackingReviewPanel
            tradeDate={tradeDate}
            tab={urlState.trackingTab}
            onTabChange={(t) => patchUrl({ trackingTab: t })}
            showToast={showToast}
          />
        )
      case 'auction':
        // [P0-FE 2026-07-31] 第二金字塔 + 竞价事件回流（PRD75 §3）
        return <AuctionBackflowPanel tradeDate={tradeDate} />
      default:
        return null
    }
  }

  return (
    <div className={styles.reviewPage}>
      <ReviewHeader
        overview={overviewQuery.data}
        tradeDate={tradeDate || '-'}
        availableDates={availableDates}
        onDateChange={handleDateChange}
        onOpenDataQuality={() => {
          if (overviewQuery.data) {
            setEvidenceTarget({
              kind: 'metric',
              title: '复盘数据质量',
              payload: null,
              meta: {
                sourceRunId: overviewQuery.data.reviewRunId,
                algorithmVersion: overviewQuery.data.algorithmVersion,
              },
            })
          }
        }}
      />
      <ReviewStageNav stage={urlState.stage} onChange={handleStageChange} />
      {/* [Phase 5B.1 / C1] 竞价回流 auxiliary entry：复用既有 URL state（stage='auction'），
          不新增 route / 页面 / navigation architecture。 */}
      <div className={styles.auxEntry}>
        <button
          type="button"
          className={
            urlState.stage === 'auction'
              ? `${styles.auxEntryBtn} ${styles.auxEntryBtnActive}`
              : styles.auxEntryBtn
          }
          onClick={() => handleStageChange('auction')}
        >
          竞价回流
        </button>
      </div>
      {/* 面包屑 */}
      <div className={styles.breadcrumb}>
        {breadcrumb.map((part, i) => (
          <span key={i}>
            <span className={i === breadcrumb.length - 1 ? styles.breadcrumbCurrent : styles.breadcrumbItem}>
              {part}
            </span>
            {i < breadcrumb.length - 1 && <span className={styles.breadcrumbSep}> › </span>}
          </span>
        ))}
      </div>
      <div className={styles.main}>
        <div className={styles.contentArea}>{renderContent()}</div>
        <EvidenceDrawer target={evidenceTarget} onClose={() => setEvidenceTarget(null)} />
      </div>
    </div>
  )
}

/** 通用状态盒子（加载/空/异常） */
function StateBox({
  title,
  desc,
  requestId,
}: {
  title: string
  desc: string
  requestId?: string | null
}) {
  return (
    <div className={styles.stateBox}>
      <div className={styles.stateTitle}>{title}</div>
      <div className={styles.stateDesc}>{desc}</div>
      {requestId && <div className={styles.stateRequestId}>request_id={requestId}</div>}
    </div>
  )
}
