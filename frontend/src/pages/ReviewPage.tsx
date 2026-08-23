// [ReviewPage] - 描述: 复盘工作台主页面（canonical Scope-first，Slice D）
// 路由 /review；URL 参数（canonical）：date/family/scopeKey/view/tab/phase/readiness/sort/page/pageSize/q
// URL 是状态 SSOT（前进后退可恢复）；React Query 数据获取；禁止自由 AI 结论
// 只轮询 computing 状态；404/422/500 显示明确原因及 request_id
//
// [Slice D] 真实用户入口切换为 canonical Scope-first 研究终端：
//   - 不再渲染 legacy Discovery / Signal 五阶段 / Tracking runtime（组件文件仍物理存在，Slice F 删除）
//   - 无 fallback/debug 按钮进入已退休体验
import { useMemo, useEffect, useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { getReviewDates, getReviewOverview, extractReviewError } from '@/features/review/api'
import { reviewKeys } from '@/features/review/queryKeys'
import {
  decodeReviewUrl,
  encodeReviewUrl,
  withReviewDateChange,
  withReviewFamilyChange,
  withReviewFilterChange,
  withReviewPageChange,
  type ReviewUrlState,
} from '@/features/review/urlState'
import { COMPUTING_STATUSES, type ReviewScopeFamily } from '@/features/review/types'
import ReviewHeader from '@/features/review/ReviewHeader'
import ScopeExplorerWorkspace from '@/features/review/ScopeExplorerWorkspace'
import styles from '@/features/review/review.module.scss'

export default function ReviewPage() {
  const [searchParams, setSearchParams] = useSearchParams()

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

  // 3. 当日总览（仅 computing 时轮询）
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

  // 4. canonical URL 更新辅助
  const patchUrl = useCallback(
    (next: ReviewUrlState, replace = false) => {
      setSearchParams(encodeReviewUrl(next), { replace })
    },
    [setSearchParams],
  )

  const handleDateChange = useCallback(
    (date: string) => {
      patchUrl(withReviewDateChange(urlState, date))
    },
    [patchUrl, urlState],
  )

  const handleFamilyChange = useCallback(
    (family: ReviewScopeFamily) => {
      patchUrl(withReviewFamilyChange(urlState, family))
    },
    [patchUrl, urlState],
  )

  const handleFilterChange = useCallback(
    (patch: Partial<ReviewUrlState>) => {
      patchUrl(withReviewFilterChange(urlState, patch))
    },
    [patchUrl, urlState],
  )

  const handlePageChange = useCallback(
    (page: number) => {
      // 翻页走独立路径：只改 page，保留 q/phase/readiness/family/scopeKey/pageSize/view
      patchUrl(withReviewPageChange(urlState, page))
    },
    [patchUrl, urlState],
  )

  const handleViewChange = useCallback(
    (view: ReviewUrlState['view']) => {
      patchUrl({ ...urlState, view })
    },
    [patchUrl, urlState],
  )

  const handleSelectScope = useCallback(
    (scopeKey: string) => {
      patchUrl({ ...urlState, scopeKey })
    },
    [patchUrl, urlState],
  )

  // 5. 日期门
  const renderDatesGate = () => {
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

  // 6. canonical Scope-first 工作区
  const renderWorkspace = () => {
    if (!tradeDate) return renderDatesGate()

    // 总览加载
    if (overviewQuery.isLoading) {
      return <StateBox title="加载复盘总览" desc={`正在获取 ${tradeDate} 复盘数据...`} />
    }

    // 总览异常
    if (overviewQuery.isError) {
      const err = extractReviewError(overviewQuery.error)
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
    // partial / completed_with_errors 继续渲染 Scope Explorer

    return (
      <ScopeExplorerWorkspace
        tradeDate={tradeDate}
        urlState={urlState}
        onFamilyChange={handleFamilyChange}
        onFilterChange={handleFilterChange}
        onPageChange={handlePageChange}
        onViewChange={handleViewChange}
        onSelectScope={handleSelectScope}
      />
    )
  }

  return (
    <div className={styles.reviewPage}>
      <ReviewHeader
        overview={overviewQuery.data}
        tradeDate={tradeDate || '-'}
        availableDates={availableDates}
        onDateChange={handleDateChange}
      />
      <div className={styles.main}>
        <div className={styles.contentArea}>{renderWorkspace()}</div>
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
