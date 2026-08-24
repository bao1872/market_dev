// [ScopeDetailWorkspace] - 描述: Canonical Scope Detail 工作区（Slice E）
//
// 布局：无选中 → 提示；选中 → header（scopeName/scopeKey/family/readiness/algorithmVersion）+ 五个子 Tab。
// 只有一个 detail owner：useReviewScopeDetail（scopeKey 为 null 不发请求）。
// 状态机（prompt §14）：无选中 / loading / API error / composition=null / layer unavailable / ready 混合 null facts 分开展示。
// 解析完全走 scopeDetailContract（唯一解析 owner），组件不散落 `as SomeType`。
import { useMemo } from 'react'
import { useReviewScopeDetail } from './useReviewScopeDetail'
import { extractReviewError } from './api'
import {
  parseDynamicsLayer,
  parseInternalStructure,
  parseLeadership,
  parseAttribution,
  parseCurrentSnapshot,
} from './scopeDetailContract'
import type { ReviewScopeCompositionDetailResponse } from './types'
import type { ReviewScopeListItem, ReviewScopeFamily } from './types'
import type { ReviewDetailTab } from './urlState'
import ScopeDetailTabs from './ScopeDetailTabs'
import ScopeDynamicsPanel from './ScopeDynamicsPanel'
import ScopeInternalStructurePanel from './ScopeInternalStructurePanel'
import ScopeLeadershipPanel from './ScopeLeadershipPanel'
import ScopeMemberAttributionPanel from './ScopeMemberAttributionPanel'
import ScopeRawFactsPanel from './ScopeRawFactsPanel'
import ScopeCurrentSnapshotPanel from './ScopeCurrentSnapshotPanel'
import { NULL_DISPLAY } from './reviewFormat'
import styles from './review.module.scss'

const FAMILY_LABEL: Record<ReviewScopeFamily, string> = {
  industry_l1: '一级行业',
  industry_l2: '二级行业',
  industry_l3: '三级行业',
  concept: '概念',
}

export interface ScopeDetailWorkspaceProps {
  tradeDate: string
  selectedScope: ReviewScopeListItem | undefined
  family: ReviewScopeFamily
  tab: ReviewDetailTab
  onTabChange: (tab: ReviewDetailTab) => void
}

function Header({
  data,
  family,
  readiness,
}: {
  data: ReviewScopeCompositionDetailResponse
  family: ReviewScopeFamily
  readiness: string
}) {
  return (
    <div className={styles.detailHeader}>
      <div className={styles.detailName}>{data.scopeName ?? NULL_DISPLAY}</div>
      <div className={styles.detailKeyline}>
        <span className={styles.detailKey} title={data.scopeKey}>
          {data.scopeKey}
        </span>
        <span className={styles.detailMeta}>{FAMILY_LABEL[family] ?? family}</span>
        <span className={styles.detailMeta}>readiness: {readiness}</span>
        <span className={styles.detailMeta}>algo: {data.algorithmVersion}</span>
      </div>
    </div>
  )
}

export default function ScopeDetailWorkspace({
  tradeDate,
  selectedScope,
  family,
  tab,
  onTabChange,
}: ScopeDetailWorkspaceProps) {
  const scopeType = selectedScope?.scopeType ?? family
  const detail = useReviewScopeDetail({
    tradeDate,
    scopeType,
    scopeKey: selectedScope ? selectedScope.scopeKey : null,
  })

  const panels = useMemo(() => {
    const c = detail.data?.composition ?? null
    // Current 身份来自已加载的 Scope list item（不发起新请求；无 N+1）。
    const currentIdentity = selectedScope
      ? {
          eligibleCount: selectedScope.eligibleCount,
          providedCount: selectedScope.providedCount,
          coverageRatio: selectedScope.coverageRatio,
        }
      : null
    return {
      dynamics: parseDynamicsLayer(c),
      internal: parseInternalStructure(c),
      leadership: parseLeadership(c),
      attribution: parseAttribution(c),
      // [R1] Current Snapshot：projection only，复用单一解析 owner；不重算。
      current: parseCurrentSnapshot({
        composition: c,
        observation: detail.data?.observation ?? null,
        identity: currentIdentity,
      }),
    }
  }, [detail.data, selectedScope])

  const noSelection = (
    <div className={styles.detailEmpty}>选择一个 Scope 查看详细分析</div>
  )

  if (!selectedScope) return noSelection

  if (detail.isLoading) {
    return (
      <div className={styles.detailWorkspace}>
        <div className={styles.stateTitle}>加载详细分析</div>
        <div className={styles.stateDesc}>正在获取 {selectedScope.scopeKey} 的 Canonical Composition...</div>
      </div>
    )
  }

  if (detail.isError) {
    const err = extractReviewError(detail.error)
    return (
      <div className={styles.detailWorkspace}>
        <div className={styles.stateTitle}>详情加载失败</div>
        <div className={styles.stateDesc}>
          {err.message}
          {err.requestId ? `（request_id=${err.requestId}）` : ''}
        </div>
      </div>
    )
  }

  if (!detail.data) {
    return (
      <div className={styles.detailWorkspace}>
        <div className={styles.stateTitle}>暂无详情数据</div>
        <div className={styles.stateDesc}>detail 未返回 payload</div>
      </div>
    )
  }

  const readiness = detail.data.composition?.composition_readiness ??
    selectedScope.readiness ??
    'unavailable_current'

  if (!detail.data.composition) {
    return (
      <div className={styles.detailWorkspace}>
        <Header data={detail.data} family={family} readiness={readiness} />
        <div className={styles.detailHeaderNote}>该 Scope 当前没有 Canonical Composition</div>
        <ScopeDetailTabs tab={tab} onTabChange={onTabChange} />
      </div>
    )
  }

  return (
    <div className={styles.detailWorkspace} data-detail-workspace>
      <Header data={detail.data} family={family} readiness={readiness} />
      <ScopeDetailTabs tab={tab} onTabChange={onTabChange} />

      <div className={styles.detailContent}>
        {tab === 'current' && <ScopeCurrentSnapshotPanel current={panels.current} />}
        {tab === 'dynamics' && <ScopeDynamicsPanel dynamics={panels.dynamics} />}
        {tab === 'internal' && <ScopeInternalStructurePanel internal={panels.internal} />}
        {tab === 'leadership' && <ScopeLeadershipPanel leadership={panels.leadership} />}
        {tab === 'attribution' && <ScopeMemberAttributionPanel attr={panels.attribution} />}
        {tab === 'facts' && <ScopeRawFactsPanel observation={detail.data.observation} />}
      </div>
    </div>
  )
}

export type { ReviewDetailTab }