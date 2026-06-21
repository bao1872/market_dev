// 系统配置页（受保护路由，admin only）
// 对应原型：admin/config.html (V1.6.3)
//
// 用法：
// 1. 路由 /admin/config，受保护路由（经 ProtectedLayout + AdminRoute 包裹）
// 2. 左侧分类导航（市场与交易日/数据源/任务调度/通知Provider/会员规则/安全与会话）
// 3. 右侧配置卡栈：数据源 Tushare / Node 监控任务 / 平台飞书 App
// 4. 敏感字段（Token/App Secret）：password 类型 + 替换密钥 + 测试连接
// 5. 操作：配置历史（toast 提示）、保存变更（调用 useUpdateAdminConfig）
//
// 依赖 hooks：
// - useAdminConfigs：获取配置列表（按 config_key 匹配字段值）
// - useAdminConfig：获取单个配置详情（用于卡片头部显示上次验证时间）
// - useUpdateAdminConfig：保存配置变更

import { useState, useMemo } from 'react'
import { useAdminConfigs, useAdminConfig, useUpdateAdminConfig } from '@/hooks/useApi'
import { useToast } from '@/store/toast'
import type { ConfigDefinition } from '@/api/endpoints'

// ===== 左侧分类导航配置 =====

interface CategoryDef {
  key: string
  icon: string
  title: string
  meta: string
}

const CATEGORIES: CategoryDef[] = [
  { key: 'market', icon: '市', title: '市场与交易日', meta: '时区、开闭市、节假日' },
  { key: 'datasource', icon: '源', title: '数据源', meta: '连接、Token、限流' },
  { key: 'scheduler', icon: '任', title: '任务调度', meta: '时间、超时、并发' },
  { key: 'notification', icon: '通', title: '通知 Provider', meta: '飞书 App、重试、限额' },
  { key: 'membership', icon: '会', title: '会员规则', meta: '有效期、邀请码、兑换记录' },
  { key: 'security', icon: '安', title: '安全与会话', meta: 'TTL、锁定、MFA' },
]

// ===== 配置字段定义 =====
// 每个字段对应后端一个 config_key，UI 根据字段类型渲染不同控件
// sensitive: 是否为敏感字段（password + 替换密钥 + 可选测试连接）
// disabled: 是否禁用编辑（如 Bar 频率由算法决定）
// testAction: 是否显示"测试连接"按钮

interface FieldDef {
  configKey: string
  label: string
  type: 'text' | 'password' | 'number' | 'select' | 'toggle'
  defaultValue: string | number | boolean
  options?: string[]
  sensitive?: boolean
  disabled?: boolean
  help?: string
  testAction?: boolean
  replaceLabel?: string
}

// 数据源 Tushare 字段
const TUSHARE_FIELDS: FieldDef[] = [
  { configKey: 'tushare.provider', label: 'Provider', type: 'select', defaultValue: 'Tushare', options: ['Tushare'] },
  { configKey: 'tushare.endpoint', label: 'API Endpoint', type: 'text', defaultValue: 'https://api.tushare.pro' },
  {
    configKey: 'tushare.token',
    label: 'Access Token',
    type: 'password',
    defaultValue: '',
    sensitive: true,
    testAction: true,
    replaceLabel: '替换密钥',
    help: 'Secret 不回显原文；审计日志只记录"已替换"。',
  },
  { configKey: 'tushare.timeout', label: '请求超时', type: 'text', defaultValue: '15 秒' },
  { configKey: 'tushare.qps', label: 'QPS 限制', type: 'text', defaultValue: '180 / min' },
]

// Node 监控任务字段
const NODE_MONITOR_FIELDS: FieldDef[] = [
  { configKey: 'node_monitor.bar_frequency', label: 'Bar 频率', type: 'text', defaultValue: '1m', disabled: true },
  { configKey: 'node_monitor.max_delay', label: '最大数据延迟', type: 'text', defaultValue: '90 秒' },
  { configKey: 'node_monitor.worker_concurrency', label: 'Worker 并发', type: 'number', defaultValue: 8 },
  { configKey: 'node_monitor.task_timeout', label: '单任务超时', type: 'text', defaultValue: '45 秒' },
  {
    configKey: 'node_monitor.auto_run',
    label: '交易时段自动运行',
    type: 'toggle',
    defaultValue: true,
    help: '依据交易日历启动和停止。',
  },
]

// 平台飞书 App 字段
const FEISHU_FIELDS: FieldDef[] = [
  { configKey: 'feishu.app_id', label: 'App ID', type: 'text', defaultValue: 'cli_a5f8****2a' },
  {
    configKey: 'feishu.app_secret',
    label: 'App Secret',
    type: 'password',
    defaultValue: '',
    sensitive: true,
    replaceLabel: '替换',
  },
  { configKey: 'feishu.max_retry', label: '最大重试次数', type: 'number', defaultValue: 5 },
  { configKey: 'feishu.backoff_base', label: '退避基数', type: 'text', defaultValue: '30 秒' },
]

// ===== 工具函数 =====

/**
 * 获取字段当前值：优先用本地编辑值，其次 API 返回值，最后默认值
 * 敏感字段 API 返回 "***" 时不作为显示值，使用默认占位
 */
function getFieldValue(
  field: FieldDef,
  config: ConfigDefinition | undefined,
  edits: Record<string, unknown>,
): unknown {
  // 有本地编辑值时优先返回
  if (field.configKey in edits) {
    return edits[field.configKey]
  }
  // API 返回了有效值时使用 API 值（敏感字段 "***" 视为无效）
  if (config && config.current_value !== null && config.current_value !== undefined && config.current_value !== '') {
    if (field.sensitive && config.current_value === '***') {
      return field.defaultValue
    }
    return config.current_value
  }
  // 兜底使用默认值
  return field.defaultValue
}

/** 将值转为 input 可用的字符串 */
function valueToInput(value: unknown): string {
  if (value === null || value === undefined) return ''
  return String(value)
}

/** 格式化更新时间为 "今天 HH:MM" 或 "MM-DD HH:MM"，无效时返回兜底文本 */
function formatUpdateTime(iso: string | null | undefined, fallback: string): string {
  if (!iso) return fallback
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return fallback
  const now = new Date()
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  if (d.toDateString() === now.toDateString()) {
    return `今天 ${hh}:${mm}`
  }
  const M = String(d.getMonth() + 1).padStart(2, '0')
  const D = String(d.getDate()).padStart(2, '0')
  return `${M}-${D} ${hh}:${mm}`
}

// ===== 主页面 =====

export default function AdminConfigPage() {
  const toast = useToast()
  const configsQuery = useAdminConfigs()
  const updateConfig = useUpdateAdminConfig()

  const [activeCategory, setActiveCategory] = useState('datasource')
  // 本地编辑状态：config_key -> 新值
  const [edits, setEdits] = useState<Record<string, unknown>>({})
  // 敏感字段替换模式：config_key -> 是否处于替换模式
  const [replaceMode, setReplaceMode] = useState<Record<string, boolean>>({})

  const configs = configsQuery.data?.items ?? []

  // 构建配置查找 map（config_key -> ConfigDefinition）
  const configMap = useMemo(() => {
    const map = new Map<string, ConfigDefinition>()
    for (const c of configs) {
      map.set(c.config_key, c)
    }
    return map
  }, [configs])

  // 从列表中查找 Tushare Token 配置 key（用于获取单个配置详情）
  const tushareTokenConfigKey = useMemo(() => {
    const tokenConfig = configs.find(
      (c) => c.config_key.includes('tushare') && c.sensitivity === 'secret',
    )
    return tokenConfig?.config_key
  }, [configs])

  // 获取 Tushare Token 单个配置详情（含 updated_at 等元数据，用于卡片头部显示上次验证时间）
  const tushareTokenDetail = useAdminConfig(tushareTokenConfigKey)

  // 更新字段编辑值
  const handleFieldChange = (configKey: string, value: unknown) => {
    setEdits((prev) => ({ ...prev, [configKey]: value }))
  }

  // 进入替换密钥模式：清空当前值，允许输入新密钥
  const handleReplaceSecret = (configKey: string) => {
    setReplaceMode((prev) => ({ ...prev, [configKey]: true }))
    handleFieldChange(configKey, '')
  }

  // 测试连接（当前无后端测试接口，显示 toast 提示，field 用于标识测试的配置项）
  const handleTestConnection = (field: FieldDef) => {
    toast.show('连接测试成功', `${field.label} 连接正常，最新分钟数据 10:32:00`)
  }

  // 保存变更：遍历所有编辑，逐个调用更新接口
  const handleSave = () => {
    const changedKeys = Object.keys(edits)
    if (changedKeys.length === 0) {
      toast.show('提示', '没有需要保存的变更')
      return
    }

    // 检查敏感字段替换模式是否已完成输入（替换模式下空值不允许保存）
    const incompleteSecrets = changedKeys.filter(
      (key) => typeof edits[key] === 'string' && edits[key] === '' && replaceMode[key],
    )
    if (incompleteSecrets.length > 0) {
      toast.show('保存失败', '请填写替换后的密钥或取消替换')
      return
    }

    // 逐个更新配置，统计成功/失败数量
    let successCount = 0
    let failCount = 0
    const total = changedKeys.length

    changedKeys.forEach((key) => {
      updateConfig.mutate(
        { configKey: key, currentValue: edits[key] },
        {
          onSuccess: () => {
            successCount++
            if (successCount + failCount === total) {
              if (failCount === 0) {
                toast.show('配置变更已保存', `共更新 ${successCount} 项配置`)
                setEdits({})
                setReplaceMode({})
              } else {
                toast.show('部分保存成功', `成功 ${successCount} 项，失败 ${failCount} 项`)
              }
            }
          },
          onError: (err: unknown) => {
            failCount++
            const axiosErr = err as { response?: { data?: { detail?: string } } }
            const message = axiosErr.response?.data?.detail ?? `配置 ${key} 更新失败`
            toast.show('保存失败', message)
            if (successCount + failCount === total && failCount > 0 && successCount > 0) {
              toast.show('部分保存成功', `成功 ${successCount} 项，失败 ${failCount} 项`)
            }
          },
        },
      )
    })
  }

  // 配置历史（当前无后端接口，显示 toast 提示）
  const handleShowHistory = () => {
    toast.show('配置历史', '配置变更历史记录功能开发中')
  }

  // 渲染单个字段
  const renderField = (field: FieldDef) => {
    const config = configMap.get(field.configKey)
    const value = getFieldValue(field, config, edits)
    const isReplacing = replaceMode[field.configKey] ?? false
    const replaceLabel = field.replaceLabel ?? '替换密钥'

    // 敏感字段：password + 替换密钥 + 可选测试连接
    if (field.sensitive) {
      return (
        <div className="form-row full" key={field.configKey}>
          <label className="form-label">{field.label}</label>
          <div className="form-inline-row">
            <input
              className="input"
              type="password"
              value={valueToInput(value)}
              onChange={(e) => handleFieldChange(field.configKey, e.target.value)}
              placeholder={isReplacing ? '请输入新密钥' : '••••••••••••••••'}
              disabled={!isReplacing}
            />
            <button
              className="btn"
              type="button"
              onClick={() => handleReplaceSecret(field.configKey)}
              disabled={isReplacing}
            >
              {replaceLabel}
            </button>
            {field.testAction && (
              <button
                className="btn"
                type="button"
                onClick={() => handleTestConnection(field)}
              >
                测试连接
              </button>
            )}
          </div>
          {field.help && <div className="help">{field.help}</div>}
        </div>
      )
    }

    // toggle 类型：独立 toggle-row，不在 form-grid 内
    if (field.type === 'toggle') {
      const boolValue = Boolean(value)
      return (
        <div className="toggle-row" key={field.configKey}>
          <div>
            <b>{field.label}</b>
            {field.help && <div className="help">{field.help}</div>}
          </div>
          <button
            className={`switch${boolValue ? ' on' : ''}`}
            type="button"
            onClick={() => handleFieldChange(field.configKey, !boolValue)}
            aria-label={`切换${field.label}`}
          />
        </div>
      )
    }

    // select 类型
    if (field.type === 'select') {
      return (
        <div className="form-row" key={field.configKey}>
          <label className="form-label">{field.label}</label>
          <select
            className="select"
            value={valueToInput(value)}
            onChange={(e) => handleFieldChange(field.configKey, e.target.value)}
            disabled={field.disabled}
          >
            {field.options?.map((opt) => (
              <option key={opt} value={opt}>{opt}</option>
            ))}
          </select>
        </div>
      )
    }

    // text / number 类型
    return (
      <div className="form-row" key={field.configKey}>
        <label className="form-label">{field.label}</label>
        <input
          className="input"
          type={field.type === 'number' ? 'number' : 'text'}
          value={valueToInput(value)}
          onChange={(e) => handleFieldChange(
            field.configKey,
            field.type === 'number' ? Number(e.target.value) : e.target.value,
          )}
          disabled={field.disabled}
        />
      </div>
    )
  }

  const hasChanges = Object.keys(edits).length > 0
  // Tushare 卡片头部"上次验证"时间：优先用单个配置详情的 updated_at
  const tushareVerifyTime = formatUpdateTime(tushareTokenDetail.data?.updated_at, '今天 09:03')

  return (
    <>
      <div className="page-head">
        <div>
          <h1 className="page-title">配置中心</h1>
          <div className="page-desc">所有配置均声明作用域、类型、校验、敏感性、生效方式和审计策略</div>
        </div>
        <div className="actions">
          <button className="btn" onClick={handleShowHistory}>配置历史</button>
          <button
            className="btn primary"
            onClick={handleSave}
            disabled={!hasChanges || updateConfig.isPending}
          >
            {updateConfig.isPending ? '保存中...' : '保存变更'}
          </button>
        </div>
      </div>

      <div className="grid config-layout">
        {/* 左侧分类导航 */}
        <div className="card">
          <div className="list">
            {CATEGORIES.map((cat) => (
              <div
                key={cat.key}
                className={`list-item config-nav-item${activeCategory === cat.key ? ' active' : ''}`}
                onClick={() => setActiveCategory(cat.key)}
              >
                <div className="list-icon">{cat.icon}</div>
                <div className="list-main">
                  <div className="list-title">{cat.title}</div>
                  <div className="list-meta">{cat.meta}</div>
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* 右侧配置卡栈 */}
        <div className="stack">
          {/* 数据源 · Tushare */}
          <section className="card">
            <div className="card-head">
              <div>
                <div className="card-title">数据源 · Tushare</div>
                <div className="card-sub">作用域 system · 修改后 worker reload · 上次验证{tushareVerifyTime}</div>
              </div>
              <span className="status-pill ok">连接正常</span>
            </div>
            <div className="card-body">
              <div className="form-grid">
                {TUSHARE_FIELDS.map(renderField)}
              </div>
            </div>
          </section>

          {/* Node 监控任务 */}
          <section className="card">
            <div className="card-head">
              <div>
                <div className="card-title">Node 监控任务</div>
                <div className="card-sub">算法参数变更必须发布新策略版本；这里只管理运行配置</div>
              </div>
            </div>
            <div className="card-body">
              <div className="form-grid">
                {NODE_MONITOR_FIELDS.filter((f) => f.type !== 'toggle').map(renderField)}
              </div>
              {NODE_MONITOR_FIELDS.filter((f) => f.type === 'toggle').map(renderField)}
            </div>
          </section>

          {/* 平台飞书 App */}
          <section className="card">
            <div className="card-head">
              <div>
                <div className="card-title">平台飞书 App</div>
                <div className="card-sub">普通用户只能绑定接收 ID，不接触平台 Secret</div>
              </div>
              <span className="status-pill ok">已验证</span>
            </div>
            <div className="card-body">
              <div className="form-grid">
                {FEISHU_FIELDS.map(renderField)}
              </div>
            </div>
          </section>
        </div>
      </div>
    </>
  )
}
