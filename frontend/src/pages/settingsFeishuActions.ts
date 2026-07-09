// [SettingsPage] - 描述: 飞书渠道卡片可执行动作的计算逻辑
// 用途：将权限/状态判断抽为纯函数，供 node test 直接验证

export type FeishuActionId = 'test' | 'latest-event' | 'edit' | 'delete'

export interface FeishuChannelAction {
  id: FeishuActionId
  label: string
  adminOnly: boolean
}

/** 根据用户权限与渠道状态返回应展示的渠道操作按钮列表（顺序即渲染顺序） */
export function getFeishuChannelActions(
  isAdmin: boolean,
  channelStatus: string,
): FeishuChannelAction[] {
  const actions: FeishuChannelAction[] = [
    { id: 'edit', label: '编辑', adminOnly: false },
    {
      id: 'test',
      label: channelStatus === 'active' ? '发送测试消息' : '测试并启用',
      adminOnly: false,
    },
  ]
  if (isAdmin) {
    actions.push({ id: 'latest-event', label: '管理员实测最近事件', adminOnly: true })
  }
  actions.push({ id: 'delete', label: '删除', adminOnly: false })
  return actions
}
