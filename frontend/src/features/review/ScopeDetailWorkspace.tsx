// [ScopeDetailWorkspace] - 描述: Canonical Scope Detail 工作区（Slice E）
//
// 布局：无选中 → 提示；选中 → header（scopeName/family/readiness/algorithmVersion）+ 五个子 Tab。
// [Slice A] header 已不再展示内部 scopeKey/UUID；scopeKey 仅作为 URL / routing / API 身份。
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
import ScopeCurrentObservationWorkspace from './ScopeCurrentObservationWorkspace'
import ScopeDsaPanel from './ScopeDsaPanel'
import ScopeSmcPanel from './ScopeSmcPanel'
import ScopeMomentumVolumePanel from './ScopeMomentumVolumePanel'
import ScopePriceAnalysisPanel from './ScopePriceAnalysisPanel'
import { NULL_DISPLAY, UNNAMED_SCOPE_LABEL, formatReadiness } from './reviewFormat'
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
        <span className={styles.detailMeta}>{FAMILY_LABEL[family] ?? family}</span>
        <span className={styles.detailMeta}>数据状态：{formatReadiness(readiness)}</span>
      </div>
      {/* [Slice A] Detail header 不再展示 Scope UUID；身份仍由 scopeKey 承载于
          URL / routing / API 请求。Raw Facts 面板保留必要 identity。 */}
      <div className={styles.detailTechnical} data-tech-line>
        <span className={styles.detailMeta}>算法版本：{data.algorithmVersion}</span>
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
    return {
      dynamics: parseDynamicsLayer(c),
      internal: parseInternalStructure(c),
      leadership: parseLeadership(c),
      attribution: parseAttribution(c),
      // [R3B] Current 由 Canonical Observation 拥有（L2 observationGroups + L1 observation）。
      // 不再由混合 ScopeCurrentSnapshot（parseCurrentSnapshot）拥有。
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
        <div className={styles.stateDesc}>正在获取 {selectedScope.scopeName ?? UNNAMED_SCOPE_LABEL} 的 Canonical Composition...</div>
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
        {/* [PHASE D1 §9] 明确错误态 + retry；不得把其它 Scope / 其它日期的缓存
            详情继续当作当前 Scope 的正式数据展示（queryKey 含 tradeDate+scopeKey）。 */}
        <button type="button" className={styles.btn} onClick={() => void detail.refetch()}>
          重试
        </button>
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

  const compositionMissing = !detail.data.composition

  return (
    <div className={styles.detailWorkspace} data-detail-workspace>
      <Header data={detail.data} family={family} readiness={readiness} />
      {compositionMissing && (
        // [R3A] Fact-only detail：Objective Observation 仍可用，不把缺失 Composition
        // 说成 failed/broken/error（除非发生实际 API error，已在上方处理）。
        <div className={styles.detailHeaderNote}>
          Canonical Composition 不可用；Objective Observation 仍可用
        </div>
      )}
      <ScopeDetailTabs tab={tab} onTabChange={onTabChange} />

      <div className={styles.detailContent}>
        {tab === 'dsa' && (
          <ScopeDsaPanel
            observation={detail.data?.observation ?? null}
            history={detail.data?.history ?? null}
            crossSection={detail.data?.crossSection ?? null}
            memberDirectory={detail.data?.memberDirectory}
          />
        )}
        {tab === 'smc' && (
          <ScopeSmcPanel
            observation={detail.data?.observation ?? null}
            history={detail.data?.history ?? null}
            memberDirectory={detail.data?.memberDirectory ?? null}
          />
        )}
        {tab === 'momentum' && (
          <ScopeMomentumVolumePanel
            observation={detail.data?.observation ?? null}
            history={detail.data?.history ?? null}
            crossSection={detail.data?.crossSection ?? null}
          />
        )}
        {tab === 'price' && (
          <ScopePriceAnalysisPanel
            observation={detail.data?.observation ?? null}
            history={detail.data?.history ?? null}
            dynamics={panels.dynamics}
            internal={panels.internal}
            crossSection={detail.data?.crossSection ?? null}
            memberDirectory={detail.data?.memberDirectory ?? null}
          />
        )}
        {tab === 'current' && (
          <ScopeCurrentObservationWorkspace
            observationGroups={detail.data?.observationGroups ?? null}
            observation={detail.data?.observation ?? null}
          />
        )}
        {tab === 'dynamics' && <ScopeDynamicsPanel dynamics={panels.dynamics} />}
        {tab === 'internal' && <ScopeInternalStructurePanel internal={panels.internal} />}
        {tab === 'leadership' && <ScopeLeadershipPanel leadership={panels.leadership} memberDirectory={detail.data?.memberDirectory} />}
        {tab === 'attribution' && <ScopeMemberAttributionPanel attr={panels.attribution} memberDirectory={detail.data?.memberDirectory} />}
        {tab === 'facts' && <ScopeRawFactsPanel observation={detail.data.observation} />}
      </div>
    </div>
  )
}

export type { ReviewDetailTab }