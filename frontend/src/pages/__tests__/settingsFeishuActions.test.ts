// [SettingsPage] - 描述: 飞书渠道操作按钮权限契约测试
// 用法：node --experimental-strip-types --test src/pages/__tests__/settingsFeishuActions.test.ts
//
// 覆盖：
//   1. member 不显示 admin 最近事件实测按钮
//   2. member 渠道卡显示"发送测试消息"或"测试并启用"
//   3. admin 才显示"管理员实测最近事件"

import { strict as assert } from 'node:assert'
import { test } from 'node:test'
import { getFeishuChannelActions } from '../settingsFeishuActions.ts'

function actionLabels(actions: ReturnType<typeof getFeishuChannelActions>): string[] {
  return actions.map((a) => a.label)
}

function findAction(
  actions: ReturnType<typeof getFeishuChannelActions>,
  id: string,
) {
  return actions.find((a) => a.id === id)
}

test('member 不显示 admin 最近事件实测按钮', () => {
  const actions = getFeishuChannelActions(false, 'pending')
  const labels = actionLabels(actions)
  assert.equal(labels.includes('管理员实测最近事件'), false)
  assert.equal(labels.includes('发送最近事件实测'), false)
})

test('member 渠道卡显示"测试并启用"（pending 状态）', () => {
  const actions = getFeishuChannelActions(false, 'pending')
  const testAction = findAction(actions, 'test')
  assert.ok(testAction)
  assert.equal(testAction!.adminOnly, false)
  assert.equal(testAction!.label, '测试并启用')
})

test('member 渠道卡显示"发送测试消息"（active 状态）', () => {
  const actions = getFeishuChannelActions(false, 'active')
  const testAction = findAction(actions, 'test')
  assert.ok(testAction)
  assert.equal(testAction!.adminOnly, false)
  assert.equal(testAction!.label, '发送测试消息')
})

test('admin 才显示"管理员实测最近事件"', () => {
  const memberActions = getFeishuChannelActions(false, 'active')
  const adminActions = getFeishuChannelActions(true, 'active')

  assert.equal(findAction(memberActions, 'latest-event'), undefined)

  const adminLatest = findAction(adminActions, 'latest-event')
  assert.ok(adminLatest)
  assert.equal(adminLatest!.label, '管理员实测最近事件')
  assert.equal(adminLatest!.adminOnly, true)
})

test('编辑与删除按钮对 member/admin 都可见', () => {
  for (const isAdmin of [false, true]) {
    const actions = getFeishuChannelActions(isAdmin, 'active')
    assert.ok(findAction(actions, 'edit'), `isAdmin=${isAdmin} 应有编辑按钮`)
    assert.ok(findAction(actions, 'delete'), `isAdmin=${isAdmin} 应有删除按钮`)
  }
})
