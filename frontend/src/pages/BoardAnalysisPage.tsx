// [CHANGE-20260730-011] 板块分析 V1 页面
// 路由 /boards（列表）和 /boards/:boardId（详情）
//
// 任何 market_data 用户可读；触发计算（admin only）通过单独的按钮调用 admin API。
// 设计：
// 1. 顶部：行业/概念切换 + 排序 + 日期
// 2. 列表表格：板块名/类型/coverage/ready/missing/状态/过期/已发布
// 3. 点击行进入详情（路由切换）
// 4. 详情页：四维分布（趋势/结构/动量/量能）+ 结构事件率 + payload JSON
// 5. Admin 用户：显示"触发计算"按钮（canary + 全量）

import { useState, useMemo } from 'react'
import { useParams, useNavigate, useSearchParams } from 'react-router-dom'
import {
  useBoardAnalysisList,
  useBoardAnalysisDetail,
  useTriggerComputeAllBoards,
} from '@/hooks/useApi'
import type {
  BoardAnalysisSnapshotDTO,
  BoardAnalysisListParams,
} from '@/api/endpoints'
import { formatShanghaiTime } from '@/utils/datetime'
import { useAuthStore } from '@/store/auth'
import { useToast } from '@/store/toast'

type BoardType = 'industry' | 'concept'
type SortKey = NonNullable<BoardAnalysisListParams['sort']>

const BOARD_TYPE_LABEL: Record<BoardType, string> = {
  industry: '行业',
  concept: '概念',
}

const SORT_OPTIONS: Array<{ value: SortKey; label: string }> = [
  { value: 'coverage_desc', label: '覆盖率↓' },
  { value: 'coverage_asc', label: '覆盖率↑' },
  { value: 'name_asc', label: '名称' },
  { value: 'ready_desc', label: '有效数↓' },
]

/** 覆盖率颜色徽标 */
function CoverageBadge({ ratio }: { ratio: number }) {
  const pct = (ratio * 100).toFixed(1) + '%'
  let cls = 'chip-success'
  if (ratio < 0.95) cls = 'chip-warning'
  if (ratio < 0.80) cls = 'chip-danger'
  return <span className={`chip ${cls}`}>{pct}</span>
}

/** 状态徽标 */
function StatusBadge({ status }: { status: string }) {
  const map: Record<string, string> = {
    succeeded: 'chip-success',
    partial: 'chip-warning',
    failed: 'chip-danger',
    running: 'chip-info',
    pending: 'chip-default',
  }
  return (
    <span className={`chip ${map[status] || 'chip-default'}`}>{status}</span>
  )
}

/** 列表视图 */
function BoardAnalysisListView() {
  const [searchParams, setSearchParams] = useSearchParams()
  const navigate = useNavigate()
  const { user } = useAuthStore()
  const isAdmin = user?.is_admin ?? false

  const [boardType, setBoardType] = useState<BoardType>(
    (searchParams.get('type') as BoardType) || 'industry',
  )
  const [sort, setSort] = useState<SortKey>(
    (searchParams.get('sort') as SortKey) || 'coverage_desc',
  )
  const [page, setPage] = useState(Number(searchParams.get('page') || 1))

  const listQuery = useBoardAnalysisList({
    type: boardType,
    sort,
    page,
    page_size: 20,
  })

  const computeAllMutation = useTriggerComputeAllBoards()
  const showToast = useToast((s) => s.show)

  const items: BoardAnalysisSnapshotDTO[] = listQuery.data?.items ?? []
  const total = listQuery.data?.total ?? 0
  const hasMore = listQuery.data?.has_more ?? false

  const handleTypeChange = (t: BoardType) => {
    setBoardType(t)
    setPage(1)
    const params = new URLSearchParams(searchParams)
    params.set('type', t)
    params.set('page', '1')
    setSearchParams(params, { replace: true })
  }

  const handleSortChange = (s: SortKey) => {
    setSort(s)
    setPage(1)
    const params = new URLSearchParams(searchParams)
    params.set('sort', s)
    params.set('page', '1')
    setSearchParams(params, { replace: true })
  }

  const handleTriggerCanary = async () => {
    try {
      const result = await computeAllMutation.mutateAsync({
        board_type: boardType,
        limit: 5,
        publish: true,
      })
      showToast(
        'Canary 完成',
        `成功 ${result.succeeded}，失败 ${result.failed}，已发布 ${result.published}`,
      )
    } catch (e) {
      showToast('Canary 失败', (e as Error).message)
    }
  }

  const handleTriggerAll = async () => {
    if (!confirm(`确定全量计算所有${BOARD_TYPE_LABEL[boardType]}板块？可能耗时较长。`)) {
      return
    }
    try {
      const result = await computeAllMutation.mutateAsync({
        board_type: boardType,
        publish: true,
      })
      showToast(
        '全量完成',
        `成功 ${result.succeeded}，失败 ${result.failed}，已发布 ${result.published}`,
      )
    } catch (e) {
      showToast('全量失败', (e as Error).message)
    }
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1 className="page-title">板块分析</h1>
          <div className="page-desc">
            [CHANGE-20260730-011] V1 · 趋势/结构/动量/量能 + 事件率 · coverage ≥ 0.95 才发布
          </div>
        </div>
        {isAdmin && (
          <div className="actions">
            <button
              className="btn btn-secondary"
              onClick={handleTriggerCanary}
              disabled={computeAllMutation.isPending}
            >
              Canary (5)
            </button>
            <button
              className="btn btn-primary"
              onClick={handleTriggerAll}
              disabled={computeAllMutation.isPending}
            >
              全量计算
            </button>
          </div>
        )}
      </div>

      {/* 类型切换 */}
      <div className="tab-row section-gap">
        {(['industry', 'concept'] as BoardType[]).map((t) => (
          <button
            key={t}
            type="button"
            className={`jobs-tab ${boardType === t ? 'active' : ''}`}
            onClick={() => handleTypeChange(t)}
            aria-selected={boardType === t}
          >
            {BOARD_TYPE_LABEL[t]}
          </button>
        ))}
      </div>

      {/* 排序 */}
      <div className="filter-row section-gap">
        {SORT_OPTIONS.map((opt) => (
          <button
            key={opt.value}
            type="button"
            className={`btn btn-secondary btn-sm ${sort === opt.value ? 'active' : ''}`}
            onClick={() => handleSortChange(opt.value)}
          >
            {opt.label}
          </button>
        ))}
      </div>

      {/* Loading */}
      {listQuery.isLoading && (
        <div className="card section-gap">
          <div className="empty-state">加载中...</div>
        </div>
      )}

      {/* Error */}
      {listQuery.isError && (
        <div className="card section-gap">
          <div className="empty-state">
            板块分析查询失败
            <div className="hint">
              {(listQuery.error as Error)?.message || '请稍后重试'}
            </div>
          </div>
        </div>
      )}

      {/* Empty */}
      {!listQuery.isLoading && !listQuery.isError && items.length === 0 && (
        <div className="card section-gap">
          <div className="empty-state">
            暂无板块分析数据
            <div className="hint">
              请等待盘后计算完成或联系管理员触发计算
            </div>
          </div>
        </div>
      )}

      {/* 列表表格 */}
      {items.length > 0 && (
        <div className="card section-gap">
          <table className="data-table">
            <thead>
              <tr>
                <th>板块名</th>
                <th>类型</th>
                <th>交易日</th>
                <th>覆盖率</th>
                <th>有效/总数</th>
                <th>缺失</th>
                <th>状态</th>
                <th>过期</th>
                <th>已发布</th>
                <th>计算完成</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr
                  key={item.id}
                  onClick={() => navigate(`/boards/${item.board_id}`)}
                  style={{ cursor: 'pointer' }}
                >
                  <td>{item.board_name}</td>
                  <td>{BOARD_TYPE_LABEL[item.board_type as BoardType] || item.board_type}</td>
                  <td className="num">{item.trade_date}</td>
                  <td>
                    <CoverageBadge ratio={item.coverage_ratio} />
                  </td>
                  <td className="num">
                    {item.ready_count}/{item.eligible_count}
                  </td>
                  <td className="num">{item.missing_count}</td>
                  <td>
                    <StatusBadge status={item.status} />
                  </td>
                  <td>{item.is_stale ? '⚠️' : '✓'}</td>
                  <td>{item.is_published ? '✓' : '—'}</td>
                  <td className="num">
                    {item.finished_at ? formatShanghaiTime(item.finished_at) : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* 分页 */}
      {total > 20 && (
        <div className="pagination section-gap">
          <button
            className="btn btn-secondary btn-sm"
            onClick={() => setPage(Math.max(1, page - 1))}
            disabled={page === 1}
          >
            上一页
          </button>
          <span>
            第 {page} 页 · 共 {Math.ceil(total / 20)} 页 · {total} 条
          </span>
          <button
            className="btn btn-secondary btn-sm"
            onClick={() => setPage(page + 1)}
            disabled={!hasMore}
          >
            下一页
          </button>
        </div>
      )}
    </>
  )
}

/** 详情视图 */
function BoardAnalysisDetailView({ boardId }: { boardId: string }) {
  const navigate = useNavigate()
  const detailQuery = useBoardAnalysisDetail(boardId)
  const snap = detailQuery.data?.snapshot

  if (detailQuery.isLoading) {
    return (
      <div className="card section-gap">
        <div className="empty-state">加载中...</div>
      </div>
    )
  }
  if (detailQuery.isError || !snap) {
    return (
      <div className="card section-gap">
        <div className="empty-state">
          板块分析不存在
          <div className="hint">
            {(detailQuery.error as Error)?.message || `board_id=${boardId} 未找到`}
          </div>
        </div>
        <div className="actions">
          <button
            className="btn btn-secondary"
            onClick={() => navigate('/boards')}
          >
            返回列表
          </button>
        </div>
      </div>
    )
  }

  const payload = snap.payload || {}
  const trendDist = (payload.trend_dist as { up: number; down: number; neutral: number }) || { up: 0, down: 0, neutral: 0 }
  const trendStrength = (payload.trend_strength as { avg: number | null; p25: number | null; p50: number | null; p75: number | null }) || {}
  const vwapDev = (payload.vwap_dev_pct as { avg: number | null; p25: number | null; p50: number | null; p75: number | null }) || {}
  const structure = (payload.structure as Record<string, number | null>) || {}
  const structureEvents = (payload.structure_events as Record<string, number | null>) || {}
  const momentum = (payload.momentum as Record<string, number | null>) || {}
  const volume = (payload.volume as Record<string, unknown>) || {}
  const missingReasons = snap.missing_reasons || {}

  return (
    <>
      <div className="page-head">
        <div>
          <h1 className="page-title">{snap.board_name}</h1>
          <div className="page-desc">
            {BOARD_TYPE_LABEL[snap.board_type as BoardType] || snap.board_type} ·
            交易日 {snap.trade_date} ·
            算法版本 {snap.algorithm_version}
          </div>
        </div>
        <div className="actions">
          <button
            className="btn btn-secondary"
            onClick={() => navigate('/boards')}
          >
            返回列表
          </button>
        </div>
      </div>

      {/* KPI */}
      <div className="grid split-4-even section-gap">
        <section className="card">
          <div className="card-head">
            <div className="card-title">覆盖率</div>
          </div>
          <div className="kpi-row">
            <div className="kpi-item">
              <div className="kpi-label">比例</div>
              <div className="kpi-value">
                <CoverageBadge ratio={snap.coverage_ratio} />
              </div>
            </div>
            <div className="kpi-item">
              <div className="kpi-label">有效/总数</div>
              <div className="kpi-value">
                {snap.ready_count}/{snap.eligible_count}
              </div>
            </div>
          </div>
        </section>

        <section className="card">
          <div className="card-head">
            <div className="card-title">状态</div>
          </div>
          <div className="kpi-row">
            <div className="kpi-item">
              <div className="kpi-label">计算状态</div>
              <div className="kpi-value">
                <StatusBadge status={snap.status} />
              </div>
            </div>
            <div className="kpi-item">
              <div className="kpi-label">发布</div>
              <div className="kpi-value">{snap.is_published ? '✓ 已发布' : '未发布'}</div>
            </div>
            <div className="kpi-item">
              <div className="kpi-label">过期</div>
              <div className="kpi-value">{snap.is_stale ? '⚠️ 过期' : '✓ 最新'}</div>
            </div>
          </div>
        </section>

        <section className="card">
          <div className="card-head">
            <div className="card-title">来源</div>
          </div>
          <div className="kpi-row">
            <div className="kpi-item">
              <div className="kpi-label">core_run_id</div>
              <div className="kpi-value" style={{ fontSize: '0.75rem' }}>
                {snap.source_core_run_id.slice(0, 8)}...
              </div>
            </div>
            <div className="kpi-item">
              <div className="kpi-label">完成时间</div>
              <div className="kpi-value" style={{ fontSize: '0.75rem' }}>
                {snap.finished_at ? formatShanghaiTime(snap.finished_at) : '—'}
              </div>
            </div>
          </div>
        </section>

        <section className="card">
          <div className="card-head">
            <div className="card-title">缺失原因</div>
          </div>
          {Object.keys(missingReasons).length === 0 ? (
            <div className="empty-state">无缺失</div>
          ) : (
            <table className="data-table">
              <thead>
                <tr>
                  <th>原因</th>
                  <th>数量</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(missingReasons).map(([k, v]) => (
                  <tr key={k}>
                    <td>{k}</td>
                    <td className="num">{v as number}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      </div>

      {/* 四维分布 */}
      <div className="grid split-2-even section-gap">
        {/* 趋势 */}
        <section className="card">
          <div className="card-head">
            <div className="card-title">趋势分布</div>
          </div>
          <table className="data-table">
            <tbody>
              <tr>
                <td>上涨</td>
                <td className="num">{trendDist.up}</td>
              </tr>
              <tr>
                <td>下跌</td>
                <td className="num">{trendDist.down}</td>
              </tr>
              <tr>
                <td>中性</td>
                <td className="num">{trendDist.neutral}</td>
              </tr>
              <tr>
                <td>平均强度</td>
                <td className="num">
                  {trendStrength.avg != null ? trendStrength.avg.toFixed(4) : '—'}
                </td>
              </tr>
              <tr>
                <td>VWAP偏离均值</td>
                <td className="num">
                  {vwapDev.avg != null ? `${(vwapDev.avg * 100).toFixed(2)}%` : '—'}
                </td>
              </tr>
              <tr>
                <td>强度中位 (P50)</td>
                <td className="num">
                  {trendStrength.p50 != null ? trendStrength.p50.toFixed(4) : '—'}
                </td>
              </tr>
            </tbody>
          </table>
        </section>

        {/* 结构 */}
        <section className="card">
          <div className="card-head">
            <div className="card-title">结构分布</div>
          </div>
          <table className="data-table">
            <tbody>
              <tr>
                <td>Swing Up</td>
                <td className="num">{structure.swing_up ?? 0}</td>
              </tr>
              <tr>
                <td>Swing Down</td>
                <td className="num">{structure.swing_down ?? 0}</td>
              </tr>
              <tr>
                <td>Swing Neutral</td>
                <td className="num">{structure.swing_neutral ?? 0}</td>
              </tr>
              <tr>
                <td>对齐</td>
                <td className="num">{structure.alignment_aligned ?? 0}</td>
              </tr>
              <tr>
                <td>未对齐</td>
                <td className="num">{structure.alignment_misaligned ?? 0}</td>
              </tr>
              <tr>
                <td>平均 OB 数</td>
                <td className="num">
                  {structure.avg_active_ob_count != null
                    ? structure.avg_active_ob_count.toFixed(2)
                    : '—'}
                </td>
              </tr>
            </tbody>
          </table>
        </section>

        {/* 结构事件 */}
        <section className="card">
          <div className="card-head">
            <div className="card-title">结构事件</div>
          </div>
          <table className="data-table">
            <tbody>
              <tr>
                <td>BOS Up / Down</td>
                <td className="num">
                  {structureEvents.bos_up ?? 0} / {structureEvents.bos_down ?? 0}
                </td>
              </tr>
              <tr>
                <td>CHoCH Up / Down</td>
                <td className="num">
                  {structureEvents.choch_up ?? 0} / {structureEvents.choch_down ?? 0}
                </td>
              </tr>
              <tr>
                <td>OB Up / Down</td>
                <td className="num">
                  {structureEvents.ob_up ?? 0} / {structureEvents.ob_down ?? 0}
                </td>
              </tr>
              <tr>
                <td>EQH / EQL 存在</td>
                <td className="num">
                  {structureEvents.eqh_present ?? 0} / {structureEvents.eql_present ?? 0}
                </td>
              </tr>
              <tr>
                <td>BOS 事件率</td>
                <td className="num">
                  {structureEvents.bos_rate != null
                    ? `${(structureEvents.bos_rate * 100).toFixed(1)}%`
                    : '—'}
                </td>
              </tr>
              <tr>
                <td>CHoCH 事件率</td>
                <td className="num">
                  {structureEvents.choch_rate != null
                    ? `${(structureEvents.choch_rate * 100).toFixed(1)}%`
                    : '—'}
                </td>
              </tr>
            </tbody>
          </table>
        </section>

        {/* 动量 */}
        <section className="card">
          <div className="card-head">
            <div className="card-title">动量分布</div>
          </div>
          <table className="data-table">
            <tbody>
              <tr>
                <td>正/负/中性</td>
                <td className="num">
                  {momentum.positive ?? 0} / {momentum.negative ?? 0} / {momentum.neutral ?? 0}
                </td>
              </tr>
              <tr>
                <td>挤压/释放/正常</td>
                <td className="num">
                  {momentum.squeeze ?? 0} / {momentum.released ?? 0} / {momentum.normal ?? 0}
                </td>
              </tr>
              <tr>
                <td>增强/减弱/平</td>
                <td className="num">
                  {momentum.enhancing ?? 0} / {momentum.fading ?? 0} / {momentum.flat ?? 0}
                </td>
              </tr>
              <tr>
                <td>平均 SQZMOM</td>
                <td className="num">
                  {momentum.avg_sqzmom != null
                    ? momentum.avg_sqzmom.toFixed(4)
                    : '—'}
                </td>
              </tr>
            </tbody>
          </table>
        </section>

        {/* 量能 */}
        <section className="card">
          <div className="card-head">
            <div className="card-title">量能分布</div>
          </div>
          <table className="data-table">
            <tbody>
              <tr>
                <td>放量/缩量/正常/未知</td>
                <td className="num">
                  {volume.high as number ?? 0} / {volume.low as number ?? 0} /{' '}
                  {volume.normal as number ?? 0} / {volume.unknown as number ?? 0}
                </td>
              </tr>
              <tr>
                <td>平均量比 20d</td>
                <td className="num">
                  {volume.avg_volume_ratio20 != null
                    ? (volume.avg_volume_ratio20 as number).toFixed(2)
                    : '—'}
                </td>
              </tr>
              <tr>
                <td>平均量比 200d</td>
                <td className="num">
                  {volume.avg_volume_ratio200 != null
                    ? (volume.avg_volume_ratio200 as number).toFixed(2)
                    : '—'}
                </td>
              </tr>
            </tbody>
          </table>
        </section>

        {/* 原始 JSON */}
        <section className="card">
          <div className="card-head">
            <div className="card-title">原始 Payload</div>
          </div>
          <pre style={{
            padding: '12px',
            background: 'var(--bg-muted, #f5f5f5)',
            borderRadius: '6px',
            fontSize: '0.75rem',
            overflowX: 'auto',
            maxHeight: '400px',
          }}>
            {JSON.stringify(payload, null, 2)}
          </pre>
        </section>
      </div>
    </>
  )
}

export default function BoardAnalysisPage() {
  const { boardId } = useParams<{ boardId?: string }>()
  const view = useMemo(() => (boardId ? 'detail' : 'list'), [boardId])

  return view === 'detail' && boardId ? (
    <BoardAnalysisDetailView boardId={boardId} />
  ) : (
    <BoardAnalysisListView />
  )
}
