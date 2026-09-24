// 板块 / 概念数据（本地手动同步状态）—— admin「数据生产中心 → 板块」Tab。
//
// [BOARD-LOCAL-OWNERSHIP-01] 板块/概念同步已迁出盘后 DAG：
// 改为本地 Mac 手动 `scripts/ops/panji-board-sync` → SSH stdin → 生产 importer。
// 本组件只展示**只读**状态：
// - 更新方式 / 数据源 / 最后成功更新 / 计数 / 最近一次尝试；
// - 服务端不提供任何抓取问财的动作按钮；
// - 不做任何基于数据年龄的可用性判定（age 仅作信息展示）。

import type { ReactNode } from 'react'
import { useAdminBoardSyncStatus } from '@/hooks/useApi'
import { buildBoardSyncDisplay } from './adminBoardDataStatusHelpers'

function Row({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="toggle-row">
      <span>{label}</span>
      <b className="num">{value}</b>
    </div>
  )
}

export default function AdminBoardDataStatus() {
  const query = useAdminBoardSyncStatus(true)
  const status = query.data
  const display = status ? buildBoardSyncDisplay(status) : null

  return (
    <section className="card section-gap">
      <div className="card-head">
        <div>
          <div className="card-title">板块 / 概念数据</div>
          <div className="card-sub">本地手动同步 · 只读状态</div>
        </div>
      </div>
      <div className="card-body">
        {query.isLoading ? (
          <div className="notice">加载中…</div>
        ) : query.isError ? (
          <div className="notice warn">板块同步状态查询失败，请稍后重试</div>
        ) : !display ? (
          <div className="notice">暂无数据</div>
        ) : (
          <>
            <Row label="更新方式" value={display.updateMode} />
            <Row label="数据源" value={display.source} />
            <Row
              label="最后成功更新"
              value={
                <span>
                  {display.lastSuccessText}
                  {!display.available && (
                    <span className="muted" style={{ marginLeft: 8 }}>
                      （暂无可用板块数据）
                    </span>
                  )}
                </span>
              }
            />
            <Row label="行业" value={display.industryCount} />
            <Row label="概念" value={display.conceptCount} />
            <Row label="板块合计" value={display.boardCount} />
            <Row label="成分关系" value={display.membershipCount} />
            <Row label="覆盖股票" value={display.stockCount} />

            {display.recentAttempt && (
              <div
                className="section-gap"
                style={{ marginTop: 12, borderTop: '1px solid var(--border, #2a2f3a)', paddingTop: 12 }}
              >
                <div className="toggle-row">
                  <span>最近一次同步尝试</span>
                  <b>
                    <span
                      className={`status-pill ${display.recentAttempt.isFailed ? 'off' : 'ok'}`}
                    >
                      {display.recentAttempt.statusText}
                    </span>
                  </b>
                </div>
                <Row label="时间" value={display.recentAttempt.timeText} />
                {display.recentAttempt.isFailed && display.recentAttempt.errorCode && (
                  <Row label="错误码" value={display.recentAttempt.errorCode} />
                )}
                {display.dataNotice && (
                  <div className="notice warn" style={{ marginTop: 8 }}>
                    {display.dataNotice}
                  </div>
                )}
              </div>
            )}

            <div className="notice" style={{ marginTop: 12 }}>
              {display.helpText}
            </div>
          </>
        )}
      </div>
    </section>
  )
}
