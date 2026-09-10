// 第一金字塔 public adapter（Marketing V1.5.2 / M3.5 接线）。
// 只负责「公开叫法」，不重新定义字段。
// 数据源必须来自产品 SSOT 派生的 FIRST_PYRAMID_FIELD_DICTIONARY；
// 禁止把 99 条字段定义复制到 marketing/data。
// 目标：最终公开 title/description 不得出现内部技术词（见 MARKETING_FIRST_PYRAMID_BANNED_TOKENS），
// 违规字段通过 PUBLIC_OVERRIDES 只覆盖公开 title/description，不改 canonical field key。
import {
  FIRST_PYRAMID_FIELD_DICTIONARY,
  type FirstPyramidDictionaryField,
} from '../../market-workspace/firstPyramidColumns'

type PublicOverride = {
  title?: string
  description?: string
}

// 仅内部技术词：这些词只会出现在产品内部字段名/短标题/helpText 里，官网不允许暴露。
// 如果本 adapter 输出仍含这些词，说明某个字段漏了 override，测试会截停。
export const MARKETING_FIRST_PYRAMID_BANNED_TOKENS: readonly string[] = [
  'fp_',
  'DSA',
  'VWAP',
  'BOS',
  'CHoCH',
  'POC',
  'VAH',
  'VAL',
  'SQZ',
  'feature_snapshot',
  'Run ID',
]

const PUBLIC_OVERRIDES: Record<string, PublicOverride> = {
  // 快照
  fp_run_id: {
    title: '数据批次',
    description: '这组字段所属的数据计算批次。',
  },
  fp_data_source: {
    title: '数据来源',
    description: '这组字段所属的数据快照。',
  },

  // 趋势
  fp_dsa_vwap_dev_pct: {
    title: '距趋势参考线',
    description: '当前价格相对趋势参考位置的偏离幅度。',
  },
  fp_vwap_ret_total: {
    title: '本轮趋势收益',
    description: '本轮趋势相对参考线累计收益率。',
  },

  // 结构事件
  fp_latest_bos_direction: {
    title: '最近结构突破方向',
    description: '最近一次主要结构突破的方向。',
  },
  fp_latest_choch_direction: {
    title: '最近结构转折方向',
    description: '最近一次结构出现转折信号的方向。',
  },
  fp_structure_event_freshness: {
    title: '结构事件新鲜度',
    description: '距离最近一次结构事件过去多少个周期，越小越新。',
  },

  // 动量事件
  fp_latest_sqz_off_freshness: {
    title: '距最近动量释放周期',
    description: '距离最近一次动量从挤压状态释放过去多少个周期。',
  },

  // 筹码
  fp_poc_price: {
    title: '主要成交密集价',
    description: '历史成交最集中的价格位置，不代表股东真实持仓成本。',
  },

  // 量能关键定义锁定
  fp_volume_ratio20: {
    description: '当前成交量 ÷ 过去20日平均成交量。',
  },
  fp_volume_percentile20: {
    description: '当前成交量在近20日成交量中的相对位置，范围 0–1。',
  },
  fp_volume_zscore20: {
    description: '当前成交量相对近20日均值偏离多少个标准差。',
  },
}

export type FirstPyramidPublicField = {
  key: string
  group: FirstPyramidDictionaryField['group']
  title: string
  description: string
}

export const MARKETING_FIRST_PYRAMID_FIELDS: readonly FirstPyramidPublicField[] =
  FIRST_PYRAMID_FIELD_DICTIONARY.map((field) => {
    const override = PUBLIC_OVERRIDES[field.key]
    return {
      key: field.key,
      group: field.group,
      title: override?.title ?? field.title,
      description: override?.description ?? field.helpText,
    }
  })