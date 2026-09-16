// [USER-FIX-3 / A2] - 描述: /market 批次元数据（published-runs）与行情主数据的依赖解耦契约测试
// 用法：./node_modules/.bin/tsx --test src/features/market-workspace/__tests__/marketBatchMetaGate.test.ts
//
// 复现的生产缺陷（真实 dev runtime）：
//   普通 member（self_selection + market_data，无 research_replay）在 /market：
//     GET /v1/market/stocks?scope=market     → 200（行情行已就绪，total=5912）
//     GET /v1/strategies/.../published-runs  → 403「需要权限: research_replay」
//   但页面把 runsQuery.isLoading / runsQuery.isError 并入了表格 loading/error，
//   于是行情行被「运行批次加载失败」错误态整体遮断（表格 0 行）。
//
// 本文件用**行为断言**（不是字符串 grep）固定修复后的依赖关系：
//   A2-1  普通 member 不请求 published-runs（真实 usePublishedRuns 门控行为）
//   A2-2  watchlist scope 同样不受影响（scope 不参与 batch meta 门控）
//   A2-3  access 未 ready（idle/loading）时不偷跑 batch query
//   A2-4  admin 保留 batch meta（真实请求被启用，且能消费数据）
//   A2-5  辅助查询失败不得遮断主表（defense-in-depth）
//   A2-6  主数据失败仍必须显式失败（不得被"辅助不阻塞"吞掉）
//   A2-7  账号切换：门控关闭时不得消费上一会话残留的 published-runs 缓存

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import {
  describeMarketStocksError,
  resolveBatchMetaStrategyKey,
  resolveMarketTableState,
  selectBatchMetaItems,
} from '../marketBatchMetaGate.ts'
import { usePublishedRuns } from '../../../hooks/useApi.ts'

// useApi / apiClient 的拦截器会在真实请求时读取 window；SSR 下不发起请求，
// 这里仅按仓库既有 SSR 测试惯例打桩，避免任何意外访问。
;(globalThis as unknown as { window: unknown }).window = {
  location: { search: '', pathname: '/' },
  innerWidth: 1280,
  innerHeight: 800,
}

const DSA_STRATEGY_KEY = 'dsa_selector'
const BATCH_META_PARAMS = { limit: 1 }
const ADMIN_ITEMS = [{ id: 'run-from-admin-session', status: 'published' }]

type BatchMetaData = { items: { id: string; status: string }[] }

/**
 * 用**真实** usePublishedRuns 渲染一次门控后的 hook，返回
 * (a) 页面实际消费到的 batch meta 条目，(b) 真实创建出来的 query key。
 *
 * SSR 不执行 effect，因此不会真的发网络请求；这里断言的是**门控本身的行为**：
 * strategyKey=undefined ⇒ hook 内 enabled=false ⇒ 生成的 query key 与
 * `dsa_selector` 缓存条目**结构上不可达**（不会复用管理员会话的批次数据）。
 */
function renderBatchMetaProbe(input: {
  accessReady: boolean
  isAdmin: boolean
  seededAdminCache: boolean
}): { items: { id: string; status: string }[]; queryKeys: unknown[][] } {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  if (input.seededAdminCache) {
    // 模拟「管理员会话已经成功请求过 published-runs」留下的 Query 缓存
    qc.setQueryData<BatchMetaData>(
      ['strategies', DSA_STRATEGY_KEY, 'published-runs', BATCH_META_PARAMS],
      { items: ADMIN_ITEMS },
    )
  }

  let capturedItems: { id: string; status: string }[] = []

  function Probe() {
    const strategyKey = resolveBatchMetaStrategyKey({
      accessReady: input.accessReady,
      isAdmin: input.isAdmin,
      strategyKey: DSA_STRATEGY_KEY,
    })
    const runsQuery = usePublishedRuns(strategyKey, BATCH_META_PARAMS)
    capturedItems = selectBatchMetaItems({
      shouldLoadBatchMeta: strategyKey !== undefined,
      data: runsQuery.data as BatchMetaData | undefined,
    })
    return null
  }

  renderToStaticMarkup(
    createElement(
      QueryClientProvider as never,
      { client: qc },
      createElement(Probe as never, {}),
    ),
  )

  return {
    items: capturedItems,
    queryKeys: qc.getQueryCache().getAll().map((q) => q.queryKey as unknown[]),
  }
}

// ===== A2-1 普通 member 不请求 published-runs =====

test('A2-1: 普通 member（非 admin）门控为关闭，hook 收到的 strategyKey 为 undefined', () => {
  assert.equal(
    resolveBatchMetaStrategyKey({
      accessReady: true,
      isAdmin: false,
      strategyKey: DSA_STRATEGY_KEY,
    }),
    undefined,
    '非 admin 必须得不到 strategyKey ⇒ enabled=false ⇒ 不发起 published-runs 请求',
  )
})

test('A2-1: 普通 member 渲染时真实创建的是 null-strategy 查询，无法读到 dsa_selector 缓存条目', () => {
  const out = renderBatchMetaProbe({
    accessReady: true,
    isAdmin: false,
    seededAdminCache: false,
  })

  // 门控关闭 ⇒ 消费到 0 条 batch meta（行情表因此不会出现「运行批次加载失败」）
  assert.deepEqual(out.items, [], '非 admin 不得消费任何 batch meta')

  // 真实 hook 行为：key 的 strategy 槽位为 null，与 dsa_selector 条目结构不可达
  assert.equal(out.queryKeys.length, 1, '应只创建一个 published-runs query')
  assert.equal(
    out.queryKeys[0][1] ?? null,
    null,
    '非 admin 的 query key 必须落到 null strategy 槽位（不可达管理员缓存）',
  )
  assert.deepEqual(
    [out.queryKeys[0][0], out.queryKeys[0][2], out.queryKeys[0][3]],
    ['strategies', 'published-runs', BATCH_META_PARAMS],
    'query key 其余槽位保持既有合同',
  )
})

// ===== A2-2 watchlist 不受影响 =====

test('A2-2: scope 不参与 batch meta 门控 —— watchlist 用户同样不请求 published-runs', () => {
  // scope=watchlist 的 self_selection-only 用户：同样没有 research_replay
  assert.equal(
    resolveBatchMetaStrategyKey({
      accessReady: true,
      isAdmin: false,
      strategyKey: DSA_STRATEGY_KEY,
    }),
    undefined,
    'watchlist scope 下门控结论与 market scope 完全一致（scope 不是门控输入）',
  )

  const out = renderBatchMetaProbe({
    accessReady: true,
    isAdmin: false,
    seededAdminCache: false,
  })
  assert.deepEqual(out.items, [], 'watchlist 用户同样不得消费 batch meta')
})

// ===== A2-3 access 未 ready 时不偷跑 batch query =====

test('A2-3: accessStatus=idle/loading（accessReady=false）时，即使 admin 也不加载 batch meta', () => {
  for (const isAdmin of [true, false]) {
    assert.equal(
      resolveBatchMetaStrategyKey({
        accessReady: false,
        isAdmin,
        strategyKey: DSA_STRATEGY_KEY,
      }),
      undefined,
      `accessReady=false（isAdmin=${isAdmin}）必须在 capability 解析完成前保持关闭`,
    )
  }

  const out = renderBatchMetaProbe({
    accessReady: false,
    isAdmin: true,
    seededAdminCache: true,
  })
  assert.deepEqual(out.items, [], 'access 未 ready 时不得消费 batch meta')
})

// ===== A2-4 admin 保留 batch meta =====

test('A2-4: admin + access ready 时门控打开，真实 query 落在 dsa_selector 上并消费到数据', () => {
  assert.equal(
    resolveBatchMetaStrategyKey({
      accessReady: true,
      isAdmin: true,
      strategyKey: DSA_STRATEGY_KEY,
    }),
    DSA_STRATEGY_KEY,
    'admin 必须保留批次信息（不得因为修普通用户而删掉 admin 调试信息）',
  )

  const out = renderBatchMetaProbe({
    accessReady: true,
    isAdmin: true,
    seededAdminCache: true,
  })

  assert.deepEqual(
    out.queryKeys[0],
    ['strategies', DSA_STRATEGY_KEY, 'published-runs', BATCH_META_PARAMS],
    'admin 的 query key 必须落在真实 strategy key 上',
  )
  assert.deepEqual(out.items, ADMIN_ITEMS, 'admin 应能消费到 published-runs 数据')
})

// ===== A2-5 辅助查询失败不得遮断主表 =====

test('A2-5 [S2-A]: published-runs 状态根本不进入行情表状态（resolveMarketTableState 不接收 batchMeta*）', () => {
  // 主数据成功：有 rows（isLoading=false、isError=false）
  // [S2-A] 函数签名现在只有 marketStocksLoading / marketStocksError 两个输入字段；
  // 任何 batchMeta* 字段都不再是合法入参（类型层面拒绝），辅助查询不可能影响输出。
  const state = resolveMarketTableState({
    marketStocksLoading: false,
    marketStocksError: null,
  })
  assert.deepEqual(
    state,
    { loading: false, error: null },
    '主数据成功 ⇒ 表格 loading=false、error=null（辅助查询无从介入）',
  )
})

// ===== A2-6 primary failure 仍必须显式失败 =====

test('A2-6: 主数据失败必须显式失败，不得被"辅助不阻塞"吞掉', () => {
  const cases: { err: unknown; expect: string }[] = [
    {
      err: { response: { status: 422, data: { detail: 'fp_sort 无效' } } },
      expect: '筛选/排序参数无效：fp_sort 无效',
    },
    {
      err: {
        response: {
          status: 500,
          headers: { get: () => 'req-123' },
        },
      },
      expect: '服务器错误（request_id=req-123）',
    },
    {
      err: { response: { status: 500, headers: { get: () => null } } },
      expect: '服务器错误',
    },
    { err: { response: { status: 403 } }, expect: '行情列表加载失败：403' },
    { err: new Error('boom'), expect: '行情列表加载失败：网络错误' },
  ]

  for (const c of cases) {
    const text = describeMarketStocksError(c.err)
    assert.equal(text, c.expect)

    // [S2-A] 主数据错误必须原样透出（函数签名已不含 batchMeta*，辅助查询无从介入）
    const state = resolveMarketTableState({
      marketStocksLoading: false,
      marketStocksError: text,
    })
    assert.equal(state.error, text, '主数据失败必须显式失败')
  }
})

// ===== A2-7 account transition / stale cache =====

test('A2-7: 门控关闭时不得消费上一账号残留的 published-runs 缓存', () => {
  const staleAdminCache: BatchMetaData = { items: ADMIN_ITEMS }

  assert.deepEqual(
    selectBatchMetaItems({ shouldLoadBatchMeta: false, data: staleAdminCache }),
    [],
    'admin → logout → member：不得继续使用旧 cached activeRun',
  )

  // 门控打开（admin 自己）时才允许消费
  assert.deepEqual(
    selectBatchMetaItems({ shouldLoadBatchMeta: true, data: staleAdminCache }),
    ADMIN_ITEMS,
  )

  // data 缺失时保持安全空值
  assert.deepEqual(selectBatchMetaItems({ shouldLoadBatchMeta: true, data: undefined }), [])

  // 端到端：带管理员残留缓存渲染普通 member → 页面消费到 0 条
  const out = renderBatchMetaProbe({
    accessReady: true,
    isAdmin: false,
    seededAdminCache: true,
  })
  assert.deepEqual(out.items, [], '普通用户会话不得继承管理员批次信息')
})
