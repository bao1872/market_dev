// [mapStructureStateLabel] - 描述: 形态状态 code → 中文标签纯函数
// API 返回稳定 code（between_nodes 等），前端必须映射为中文展示。
// 禁止在 DOM 中直接显示内部 code。
// DSA 状态同样映射为中文（上行/下行/震荡等）。

/**
 * 形态状态 code → 中文标签映射
 * API 保留稳定 code 供事件比较；前端展示必须经过此映射。
 */
export function mapStructureStateLabel(code: string | null | undefined): string {
  if (!code) return '数据不足'
  const map: Record<string, string> = {
    between_nodes: '节点之间',
    below_upper_node: '上方节点下方',
    above_lower_node: '下方节点上方',
    above_upper_node: '高于上方节点',
    below_lower_node: '低于下方节点',
  }
  return map[code] ?? code
}

/**
 * DSA 状态 code → 中文标签映射
 * 后端 _map_dsa_state 可能返回 "上行"/"下行"/"震荡" 或 code。
 * 若已是中文直接返回；若是 code 则映射。
 */
export function mapDsaStateLabel(code: string | null | undefined): string {
  if (!code) return '数据不足'
  const map: Record<string, string> = {
    up: '上行',
    down: '下行',
    sideways: '震荡',
    1: '上行',
    '0': '震荡',
    '-1': '下行',
  }
  return map[code] ?? code
}
