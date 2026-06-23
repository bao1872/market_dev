// 策略目录页（受保护路由，admin only）
// 对应原型：admin/strategies.html (V1.6.3)
//
// 用法：
// 1. 路由 /admin/strategies，受保护路由（经 ProtectedLayout + AdminRoute 包裹）
// 2. 分段按钮：全部/Selector/Monitor（按 kind 筛选）+ 状态下拉（Active/Shadow/Draft）
// 3. split-even 策略卡网格：每张含类型/名称/meta（key·version·build）/状态pill/三stat/chip-row/操作按钮
// 4. 发布弹窗 publishModal：策略类型/版本/Manifest文件/发布说明
// 5. 灰度发布弹窗 rolloutModal：流量比例滑块（1-100%）+ 回滚提示
//
// 依赖 hooks：
// - useStrategies：获取全部策略列表（客户端按 kind 筛选，保证分段计数准确）
// - useQueries + api.getStrategyVersions：批量获取每个策略的版本信息
// - api.createStrategy：发布弹窗提交时创建策略定义 + 草稿版本
// - useToast：操作反馈

import { useState, useMemo, useCallback } from 'react'
import { useQueries, useQueryClient } from '@tanstack/react-query'
import clsx from 'clsx'
import { useStrategies } from '@/hooks/useApi'
import * as api from '@/api/endpoints'
import { useToast } from '@/store/toast'

// ===== 类型定义 =====

/** 策略卡数据（策略定义 + 版本信息合并，每张卡对应一个策略版本） */
interface StrategyCardData {
  strategyKey: string
  kind: string
  displayName: string
  version: string
  status: string
  buildHash: string
  releasedAt: string | null
  manifest: Record<string, unknown>
}

// ===== 工具函数 =====

/** 格式化 ISO 日期为 YYYY-MM-DD，无效时返回 '—' */
function formatDate(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

/** 策略类型显示文本（对应原型 .strategy-type） */
function getKindTypeText(kind: string): string {
  switch (kind) {
    case 'selector':
      return 'Selector strategy'
    case 'monitor':
      return 'Monitor strategy'
    default:
      return kind
  }
}

/** 版本状态 pill 映射：released→Active(ok) / shadow→Shadow(warn) / draft→Draft(off) / archived→Archived(off) */
function getStatusPill(status: string): { label: string; pill: string } {
  switch (status) {
    case 'released':
      return { label: 'Active', pill: 'ok' }
    case 'shadow':
      return { label: 'Shadow', pill: 'warn' }
    case 'draft':
      return { label: 'Draft', pill: 'off' }
    case 'archived':
      return { label: 'Archived', pill: 'off' }
    default:
      return { label: status, pill: 'off' }
  }
}

/** 从 manifest 中安全提取数组字段 */
function tryGetManifestArray(manifest: Record<string, unknown>, key: string): unknown[] | null {
  const val = manifest[key]
  return Array.isArray(val) ? val : null
}

/**
 * 策略卡三 stat 网格（按状态展示不同指标）
 * 值优先从 manifest 提取，API 未提供的运行时指标显示 '—'
 */
function getStrategyStats(card: StrategyCardData): Array<{ label: string; value: string; className?: string }> {
  const manifest = card.manifest || {}

  if (card.status === 'released') {
    // Active：输出指标 / 黄金测试 / 最近运行
    const outputs = tryGetManifestArray(manifest, 'outputs')
    return [
      { label: '输出指标', value: outputs ? String(outputs.length) : '—' },
      { label: '黄金测试', value: '—' },
      { label: '最近运行', value: card.releasedAt ? formatDate(card.releasedAt) : '—' },
    ]
  }

  if (card.status === 'shadow') {
    // Shadow：事件一致率 / 延迟 P95 / 异常差异（运行时指标，API 未提供）
    return [
      { label: '事件一致率', value: '—' },
      { label: '延迟 P95', value: '—' },
      { label: '异常差异', value: '—' },
    ]
  }

  if (card.status === 'draft') {
    // Draft：Manifest / 黄金数据 / 契约测试
    return [
      { label: 'Manifest', value: '待校验' },
      { label: '黄金数据', value: '未提交', className: 'neg' },
      { label: '契约测试', value: '—' },
    ]
  }

  // Archived 或其他：版本 / 构建 / 归档时间
  return [
    { label: '版本', value: card.version },
    { label: '构建', value: card.buildHash ? card.buildHash.slice(0, 7) : '—' },
    { label: '归档时间', value: card.releasedAt ? formatDate(card.releasedAt) : '—' },
  ]
}

/** 策略卡 chip 标签（按 kind + status 派生） */
function getStrategyChips(card: StrategyCardData): Array<{ label: string; className?: string }> {
  const chips: Array<{ label: string; className?: string }> = []

  // kind 标签
  if (card.kind === 'selector') {
    chips.push({ label: '选股策略', className: 'blue' })
  } else if (card.kind === 'monitor') {
    chips.push({ label: '监控策略', className: 'blue' })
  }

  // status 标签
  switch (card.status) {
    case 'released':
      chips.push({ label: '已发布', className: 'green' })
      break
    case 'shadow':
      chips.push({ label: '影子运行', className: 'orange' })
      chips.push({ label: '不向用户分发' })
      break
    case 'draft':
      chips.push({ label: '待接入', className: 'orange' })
      chips.push({ label: '不可发布', className: 'red' })
      break
    case 'archived':
      chips.push({ label: '已归档' })
      break
  }

  return chips
}

// ===== 主页面 =====

export default function AdminStrategiesPage() {
  const toast = useToast()
  const queryClient = useQueryClient()

  // ===== 筛选状态 =====
  const [kindFilter, setKindFilter] = useState<string>('all')
  const [statusFilter, setStatusFilter] = useState<string>('all')

  // ===== 发布弹窗状态 =====
  const [publishModalOpen, setPublishModalOpen] = useState(false)
  const [publishKind, setPublishKind] = useState('selector')
  const [publishVersion, setPublishVersion] = useState('')
  const [publishFile, setPublishFile] = useState<File | null>(null)
  const [publishNotes, setPublishNotes] = useState('')
  const [publishing, setPublishing] = useState(false)

  // ===== 灰度发布弹窗状态 =====
  const [rolloutModalOpen, setRolloutModalOpen] = useState(false)
  const [rolloutTarget, setRolloutTarget] = useState<StrategyCardData | null>(null)
  const [rolloutPercent, setRolloutPercent] = useState(10)

  // ===== 数据查询 =====

  // 获取全部策略（客户端按 kind 筛选，保证分段计数始终准确）
  const strategiesQuery = useStrategies()
  const strategies = strategiesQuery.data?.items ?? []

  // 批量查询每个策略的版本（queryKey 与 useStrategyVersions hook 一致，共享缓存）
  const versionQueries = useQueries({
    queries: strategies.map((s) => ({
      queryKey: ['strategies', s.strategy_key, 'versions'],
      queryFn: () => api.getStrategyVersions(s.strategy_key),
      staleTime: 5 * 60 * 1000,
    })),
  })

  // 合并策略定义 + 版本为卡片数据（每张卡 = 一个策略版本）
  const allCards = useMemo(() => {
    const cards: StrategyCardData[] = []
    strategies.forEach((s, i) => {
      const versions = versionQueries[i]?.data?.items ?? []
      versions.forEach((v) => {
        cards.push({
          strategyKey: s.strategy_key,
          kind: s.kind,
          displayName: s.display_name,
          version: v.version,
          status: v.status,
          buildHash: v.build_hash,
          releasedAt: v.released_at,
          manifest: v.manifest,
        })
      })
    })
    return cards
  }, [strategies, versionQueries])

  // 按 kind 筛选
  const kindFilteredCards = useMemo(() => {
    if (kindFilter === 'all') return allCards
    return allCards.filter((c) => c.kind === kindFilter)
  }, [allCards, kindFilter])

  // 按 status 筛选
  const filteredCards = useMemo(() => {
    if (statusFilter === 'all') return kindFilteredCards
    return kindFilteredCards.filter((c) => c.status === statusFilter)
  }, [kindFilteredCards, statusFilter])

  // 分段计数（基于 allCards，不受当前 kind 筛选影响）
  const counts = useMemo(
    () => ({
      all: allCards.length,
      selector: allCards.filter((c) => c.kind === 'selector').length,
      monitor: allCards.filter((c) => c.kind === 'monitor').length,
    }),
    [allCards],
  )

  // 版本查询是否加载中
  const versionsLoading = versionQueries.some((q) => q.isLoading)

  // ===== 事件处理 =====

  /** 打开发布弹窗，重置表单 */
  const handleOpenPublishModal = useCallback(() => {
    setPublishKind('selector')
    setPublishVersion('')
    setPublishFile(null)
    setPublishNotes('')
    setPublishModalOpen(true)
  }, [])

  /** 关闭发布弹窗 */
  const handleClosePublishModal = useCallback(() => {
    setPublishModalOpen(false)
  }, [])

  /** 提交发布：读取 Manifest 文件 → JSON 解析 → 调用 createStrategy → 失效缓存 */
  const handlePublish = useCallback(async () => {
    if (publishing) return
    if (!publishFile) {
      toast.show('提示', '请选择 Manifest 文件')
      return
    }
    setPublishing(true)
    try {
      const text = await publishFile.text()
      let manifest: Record<string, unknown>
      try {
        manifest = JSON.parse(text) as Record<string, unknown>
      } catch {
        toast.show('上传失败', 'Manifest JSON 解析失败，请检查文件格式')
        return
      }
      await api.createStrategy(manifest)
      toast.show('策略检查任务已创建', `${publishFile.name} 已上传，正在执行校验`)
      queryClient.invalidateQueries({ queryKey: ['strategies'] })
      setPublishModalOpen(false)
    } catch (err) {
      const axiosErr = err as { response?: { data?: { detail?: string } } }
      const message = axiosErr.response?.data?.detail ?? '上传失败'
      toast.show('上传失败', message)
    } finally {
      setPublishing(false)
    }
  }, [publishing, publishFile, toast, queryClient])

  /** 打开灰度发布弹窗 */
  const handleOpenRolloutModal = useCallback((card: StrategyCardData) => {
    setRolloutTarget(card)
    setRolloutPercent(10)
    setRolloutModalOpen(true)
  }, [])

  /** 关闭灰度发布弹窗 */
  const handleCloseRolloutModal = useCallback(() => {
    setRolloutModalOpen(false)
    setRolloutTarget(null)
  }, [])

  /** 开始灰度（当前无后端接口，显示 toast 反馈） */
  const handleStartRollout = useCallback(() => {
    if (!rolloutTarget) return
    toast.show(
      '灰度计划已启动',
      `${rolloutTarget.displayName} v${rolloutTarget.version} · 流量 ${rolloutPercent}%`,
    )
    setRolloutModalOpen(false)
  }, [rolloutTarget, rolloutPercent, toast])

  /** 停止影子（当前无后端接口，显示 toast 反馈） */
  const handleStopShadow = useCallback(
    (card: StrategyCardData) => {
      toast.show('影子已停止', `${card.displayName} v${card.version} 已停止影子运行`)
    },
    [toast],
  )

  /** 查看清单（当前无后端接口，显示 toast 反馈） */
  const handleViewManifest = useCallback(
    (card: StrategyCardData) => {
      toast.show('Manifest', `${card.strategyKey} v${card.version} 清单查看功能开发中`)
    },
    [toast],
  )

  /** 测试报告 / 差异报告（当前无后端接口，显示 toast 反馈） */
  const handleViewTestReport = useCallback(
    (card: StrategyCardData) => {
      toast.show('测试报告', `${card.strategyKey} v${card.version} 测试报告功能开发中`)
    },
    [toast],
  )

  /** 版本历史（当前无后端接口，显示 toast 反馈） */
  const handleViewVersionHistory = useCallback(
    (card: StrategyCardData) => {
      toast.show('版本历史', `${card.strategyKey} 版本历史功能开发中`)
    },
    [toast],
  )

  /** 查看缺失项（当前无后端接口，显示 toast 反馈） */
  const handleViewMissing = useCallback(
    (card: StrategyCardData) => {
      toast.show('缺失项', `${card.strategyKey} v${card.version} 缺失项检查功能开发中`)
    },
    [toast],
  )

  /** 查看接入规范（当前无后端接口，显示 toast 反馈） */
  const handleViewSpec = useCallback(() => {
    toast.show('接入规范', '策略接入规范文档功能开发中')
  }, [toast])

  // ===== 渲染 =====

  return (
    <>
      {/* 页头 */}
      <div className="page-head">
        <div>
          <h1 className="page-title">策略目录</h1>
          <div className="page-desc">选择器与监控器通过统一 Manifest、版本和契约测试接入平台</div>
        </div>
        <div className="actions">
          <button className="btn" onClick={handleViewSpec}>
            查看接入规范
          </button>
          <button className="btn primary" onClick={handleOpenPublishModal}>
            ＋ 发布策略版本
          </button>
        </div>
      </div>

      {/* 工具栏：分段按钮 + 状态下拉 */}
      <div className="toolbar">
        <div className="segmented">
          <button
            className={clsx('segment', kindFilter === 'all' && 'active')}
            onClick={() => setKindFilter('all')}
          >
            全部 {counts.all}
          </button>
          <button
            className={clsx('segment', kindFilter === 'selector' && 'active')}
            onClick={() => setKindFilter('selector')}
          >
            Selector {counts.selector}
          </button>
          <button
            className={clsx('segment', kindFilter === 'monitor' && 'active')}
            onClick={() => setKindFilter('monitor')}
          >
            Monitor {counts.monitor}
          </button>
        </div>
        <div className="toolbar-spacer" />
        <select
          className="select"
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
        >
          <option value="all">全部状态</option>
          <option value="released">Active</option>
          <option value="shadow">Shadow</option>
          <option value="draft">Draft</option>
        </select>
      </div>

      {/* 策略卡网格 */}
      {strategiesQuery.isLoading || versionsLoading ? (
        <div className="empty">加载策略目录中…</div>
      ) : filteredCards.length === 0 ? (
        <div className="empty">暂无策略版本</div>
      ) : (
        <div className="grid split-even">
          {filteredCards.map((card) => {
            const statusPill = getStatusPill(card.status)
            const stats = getStrategyStats(card)
            const chips = getStrategyChips(card)
            const shortBuild = card.buildHash ? card.buildHash.slice(0, 7) : '—'
            return (
              <div key={`${card.strategyKey}-${card.version}`} className="card strategy-card">
                {/* 头部：类型 + 名称 + meta + 状态pill */}
                <div className="strategy-head">
                  <div>
                    <div className="strategy-type">{getKindTypeText(card.kind)}</div>
                    <div className="strategy-name">{card.displayName}</div>
                    <div className="strategy-meta">
                      {card.strategyKey} · v{card.version} · build {shortBuild}
                    </div>
                  </div>
                  <span className={`status-pill ${statusPill.pill}`}>{statusPill.label}</span>
                </div>

                {/* 三 stat 网格 */}
                <div className="strategy-grid">
                  {stats.map((stat) => (
                    <div key={stat.label} className="strategy-stat">
                      <span>{stat.label}</span>
                      <b className={stat.className}>{stat.value}</b>
                    </div>
                  ))}
                </div>

                {/* chip-row 标签 */}
                <div className="chip-row">
                  {chips.map((chip, idx) => (
                    <span key={idx} className={`chip ${chip.className ?? ''}`}>
                      {chip.label}
                    </span>
                  ))}
                </div>

                {/* 操作按钮（按状态显示不同操作） */}
                <div className="actions">
                  {/* Active：Manifest / 测试报告 / 版本历史 */}
                  {card.status === 'released' && (
                    <>
                      <button className="btn small" onClick={() => handleViewManifest(card)}>
                        Manifest
                      </button>
                      <button className="btn small" onClick={() => handleViewTestReport(card)}>
                        测试报告
                      </button>
                      <button className="btn small" onClick={() => handleViewVersionHistory(card)}>
                        版本历史
                      </button>
                    </>
                  )}
                  {/* Shadow：差异报告 / 灰度发布 / 停止影子 */}
                  {card.status === 'shadow' && (
                    <>
                      <button className="btn small" onClick={() => handleViewTestReport(card)}>
                        差异报告
                      </button>
                      <button className="btn small" onClick={() => handleOpenRolloutModal(card)}>
                        灰度发布
                      </button>
                      <button className="btn small danger" onClick={() => handleStopShadow(card)}>
                        停止影子
                      </button>
                    </>
                  )}
                  {/* Draft：Manifest / 查看缺失项 */}
                  {card.status === 'draft' && (
                    <>
                      <button className="btn small" onClick={() => handleViewManifest(card)}>
                        Manifest
                      </button>
                      <button className="btn small" onClick={() => handleViewMissing(card)}>
                        查看缺失项
                      </button>
                    </>
                  )}
                  {/* Archived：Manifest / 版本历史 */}
                  {card.status === 'archived' && (
                    <>
                      <button className="btn small" onClick={() => handleViewManifest(card)}>
                        Manifest
                      </button>
                      <button className="btn small" onClick={() => handleViewVersionHistory(card)}>
                        版本历史
                      </button>
                    </>
                  )}
                </div>
              </div>
            )
          })}
        </div>
      )}

      {/* 发布弹窗 publishModal */}
      {publishModalOpen && (
        <div className="modal-backdrop open" onClick={handleClosePublishModal}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-head">
              <b>发布策略版本</b>
              <button className="icon-btn" onClick={handleClosePublishModal}>
                ×
              </button>
            </div>
            <div className="modal-body">
              <div className="notice">
                上传 Manifest 后将执行 Schema、契约、黄金数据与资源预算检查。Active 版本不可直接修改。
              </div>
              <div className="form-grid modal-form-grid">
                <div className="form-row">
                  <label className="form-label">策略类型</label>
                  <select
                    className="select"
                    value={publishKind}
                    onChange={(e) => setPublishKind(e.target.value)}
                  >
                    <option value="selector">Selector</option>
                    <option value="monitor">Monitor</option>
                  </select>
                </div>
                <div className="form-row">
                  <label className="form-label">版本</label>
                  <input
                    className="input"
                    placeholder="例如 1.4.0"
                    value={publishVersion}
                    onChange={(e) => setPublishVersion(e.target.value)}
                  />
                </div>
                <div className="form-row full">
                  <label className="form-label">Manifest 文件</label>
                  <input
                    className="input"
                    type="file"
                    onChange={(e) => setPublishFile(e.target.files?.[0] ?? null)}
                  />
                </div>
                <div className="form-row full">
                  <label className="form-label">发布说明</label>
                  <textarea
                    className="input"
                    rows={3}
                    value={publishNotes}
                    onChange={(e) => setPublishNotes(e.target.value)}
                  />
                </div>
              </div>
            </div>
            <div className="modal-foot">
              <button className="btn" onClick={handleClosePublishModal}>
                取消
              </button>
              <button className="btn primary" onClick={handlePublish} disabled={publishing}>
                {publishing ? '上传中...' : '上传并校验'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 灰度发布弹窗 rolloutModal */}
      {rolloutModalOpen && rolloutTarget && (
        <div className="modal-backdrop open" onClick={handleCloseRolloutModal}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-head">
              <b>灰度发布 v{rolloutTarget.version}</b>
              <button className="icon-btn" onClick={handleCloseRolloutModal}>
                ×
              </button>
            </div>
            <div className="modal-body">
              <label className="form-label">
                流量比例：<b>{rolloutPercent}%</b>
              </label>
              <input
                className="rollout-range"
                type="range"
                min={1}
                max={100}
                value={rolloutPercent}
                onChange={(e) => setRolloutPercent(Number(e.target.value))}
              />
              <div className="notice warn modal-notice">
                灰度期间新旧版本均保留事件快照；发生异常可立即回滚到上一版本。
              </div>
            </div>
            <div className="modal-foot">
              <button className="btn" onClick={handleCloseRolloutModal}>
                取消
              </button>
              <button className="btn primary" onClick={handleStartRollout}>
                开始灰度
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  )
}
