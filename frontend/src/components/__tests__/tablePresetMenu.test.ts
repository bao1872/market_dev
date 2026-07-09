// [TablePresetMenu] - 描述: TablePresetMenu 保存逻辑契约测试
// 用法：node --experimental-strip-types --test src/components/__tests__/tablePresetMenu.test.ts
//
// 覆盖：
//   1. savePreset 空名称时提示并直接返回
//   2. savePreset 成功时清空输入、清除错误、toast 成功、并调用 presetsQuery.refetch()
//   3. savePreset 失败时在下拉内显示错误并 toast 后端 detail
//
// 设计说明：
// - TablePresetMenu.tsx 将保存逻辑抽出为纯 async 函数 savePreset，便于不依赖 DOM 快速验证。
// - 真实列表刷新由 presetsQuery.refetch() 保证；调用它即代表保存后列表会重新拉取。

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { savePreset, type SavePresetContext } from '../tablePresetMenuLogic.ts'
import type { TableViewPresetCreateRequest } from '@/api/endpoints'

function buildCtx(overrides: Partial<SavePresetContext> = {}): SavePresetContext {
  return {
    name: '测试配置',
    tableId: 'screener',
    strategyKey: 'dsa_selector',
    payload: { keyword: '新能源', pageSize: 50 },
    isDefault: false,
    createMutation: {
      mutateAsync: async () => ({ id: 'preset-1', name: '测试配置' }),
    },
    presetsQuery: { refetch: async () => ({ data: { items: [] }, error: null }) },
    toast: { show: () => {} },
    setEditingName: () => {},
    setSaveError: () => {},
    ...overrides,
  }
}

test('savePreset: 空名称时提示输入名称并直接返回', async () => {
  const calls: string[] = []
  const ctx = buildCtx({
    name: '   ',
    toast: { show: (title: string, msg: string) => { calls.push(`${title}:${msg}`) } },
    createMutation: {
      mutateAsync: async () => { throw new Error('不应被调用') },
    },
  })

  await savePreset(ctx)
  assert.deepStrictEqual(calls, ['保存配置:请输入配置名称'])
})

test('savePreset: 成功时清空输入、清除错误、toast 成功并 refetch 列表', async () => {
  const toastCalls: string[] = []
  let editingName = '测试配置'
  let saveError: string | null = 'previous-error'
  let refetchCalled = false
  let mutatePayload: TableViewPresetCreateRequest | null = null

  const ctx = buildCtx({
    name: '  测试配置  ',
    isDefault: true,
    createMutation: {
      mutateAsync: async (payload: TableViewPresetCreateRequest) => {
        mutatePayload = payload
        return { id: 'preset-123', name: '测试配置' }
      },
    },
    presetsQuery: {
      refetch: async () => {
        refetchCalled = true
        return { data: { items: [{ id: 'preset-123', name: '测试配置' }] }, error: null }
      },
    },
    toast: { show: (title: string, msg: string) => { toastCalls.push(`${title}:${msg}`) } },
    setEditingName: (value: string) => { editingName = value },
    setSaveError: (msg: string | null) => { saveError = msg },
  })

  await savePreset(ctx)

  assert.deepStrictEqual(mutatePayload, {
    table_id: 'screener',
    strategy_key: 'dsa_selector',
    name: '测试配置',
    config: { keyword: '新能源', pageSize: 50 },
    is_default: true,
  })
  assert.equal(refetchCalled, true, '成功后必须调用 presetsQuery.refetch() 刷新列表')
  assert.equal(editingName, '', '成功后必须清空输入框')
  assert.equal(saveError, null, '成功后必须清除下拉错误')
  assert.deepStrictEqual(toastCalls, ['保存配置:已保存「测试配置」'])
})

test('savePreset: 失败时在下拉内显示后端 detail 并 toast 错误', async () => {
  const toastCalls: string[] = []
  let saveError: string | null = null
  let refetchCalled = false

  const ctx = buildCtx({
    name: '重复名称',
    createMutation: {
      mutateAsync: async () => {
        const err = new Error('conflict') as Error & { response?: { data?: { detail?: string } } }
        err.response = { data: { detail: '同维度下已存在同名 preset' } }
        throw err
      },
    },
    presetsQuery: {
      refetch: async () => { refetchCalled = true; return { data: undefined, error: null } },
    },
    toast: { show: (title: string, msg: string) => { toastCalls.push(`${title}:${msg}`) } },
    setSaveError: (msg: string | null) => { saveError = msg },
  })

  await savePreset(ctx)

  assert.equal(saveError, '同维度下已存在同名 preset', '失败后必须在下拉内显示后端错误')
  assert.equal(refetchCalled, false, '失败时不应调用 refetch')
  assert.deepStrictEqual(toastCalls, ['保存配置失败:同维度下已存在同名 preset'])
})

test('savePreset: 失败且无 detail 时使用默认错误文案', async () => {
  let saveError: string | null = null

  const ctx = buildCtx({
    name: 'fail',
    createMutation: {
      mutateAsync: async () => { throw new Error('network error') },
    },
    presetsQuery: { refetch: async () => ({ data: undefined, error: null }) },
    toast: { show: () => {} },
    setSaveError: (msg: string | null) => { saveError = msg },
  })

  await savePreset(ctx)
  assert.equal(saveError, '保存失败')
})
