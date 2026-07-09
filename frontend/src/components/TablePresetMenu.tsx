// [Presets] - 描述: 表格视图配置菜单（保存/应用/删除/设为默认）
// 用法：在 StrategyDataTable 元信息栏渲染，需要 tableId + strategyKey
// config 仅保存 keyword/sort/filters/hiddenColumns/pageSize，禁止保存 selectedKeys/page/activeRunId
import { useState, useRef, useEffect } from 'react'
import clsx from 'clsx'
import { useToast } from '@/store/toast'
import {
  useTableViewPresets,
  useCreateTableViewPreset,
  useUpdateTableViewPreset,
  useDeleteTableViewPreset,
} from '@/hooks/useApi'
import type { TableViewPresetConfig } from '@/api/endpoints'
import { savePreset } from './tablePresetMenuLogic'

export interface TablePresetMenuProps {
  tableId: string
  strategyKey?: string | null
  /** 当前表格配置（由 StrategyDataTable 从内部 state 构建） */
  currentConfig: TableViewPresetConfig
  /** 应用 preset 时回调（StrategyDataTable 重置内部 state） */
  onApply: (config: TableViewPresetConfig) => void
}

/** 将 TableViewPresetConfig 转为后端 config dict（过滤 null/undefined） */
function configToPayload(config: TableViewPresetConfig): Record<string, unknown> {
  const payload: Record<string, unknown> = {}
  if (config.keyword != null && config.keyword !== '') payload.keyword = config.keyword
  if (config.sort != null) payload.sort = config.sort
  if (config.filters != null) payload.filters = config.filters
  if (config.hiddenColumns != null) payload.hiddenColumns = config.hiddenColumns
  if (config.pageSize != null) payload.pageSize = config.pageSize
  return payload
}

/** 从后端 preset.config 提取 TableViewPresetConfig */
function payloadToConfig(config: Record<string, unknown>): TableViewPresetConfig {
  return {
    keyword: (config.keyword as string | null | undefined) ?? null,
    sort: (config.sort as TableViewPresetConfig['sort']) ?? null,
    filters: (config.filters as TableViewPresetConfig['filters']) ?? null,
    hiddenColumns: (config.hiddenColumns as string[] | null | undefined) ?? null,
    pageSize: (config.pageSize as number | null | undefined) ?? null,
  }
}

export function TablePresetMenu({ tableId, strategyKey, currentConfig, onApply }: TablePresetMenuProps) {
  const toast = useToast.getState()
  const [open, setOpen] = useState(false)
  const [editingName, setEditingName] = useState('')
  const [saveError, setSaveError] = useState<string | null>(null)
  const [renamingId, setRenamingId] = useState<string | null>(null)
  const [renameValue, setRenameValue] = useState('')
  const containerRef = useRef<HTMLDivElement>(null)

  const presetsQuery = useTableViewPresets(tableId, strategyKey ?? undefined)
  const createMutation = useCreateTableViewPreset()
  const updateMutation = useUpdateTableViewPreset()
  const deleteMutation = useDeleteTableViewPreset()

  const presets = presetsQuery.data?.items ?? []

  // [Presets] - 描述: 点击外部关闭下拉
  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false)
        setRenamingId(null)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  /** 保存当前配置为新 preset */
  const handleSave = () => {
    savePreset({
      name: editingName,
      tableId,
      strategyKey,
      payload: configToPayload(currentConfig),
      isDefault: presets.length === 0,
      createMutation,
      presetsQuery,
      toast,
      setEditingName,
      setSaveError,
    })
  }

  /** 覆盖已有 preset 的 config */
  const handleOverwrite = async (id: string, name: string) => {
    try {
      await updateMutation.mutateAsync({
        id,
        payload: { config: configToPayload(currentConfig) },
      })
      toast.show('覆盖配置', `已更新「${name}」`)
    } catch (e: unknown) {
      const msg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? '覆盖失败'
      toast.show('覆盖配置失败', msg)
    }
  }

  /** 应用 preset */
  const handleApply = (id: string, name: string) => {
    const preset = presets.find((p) => p.id === id)
    if (!preset) return
    onApply(payloadToConfig(preset.config))
    toast.show('应用配置', `已应用「${name}」`)
    setOpen(false)
  }

  /** 设为默认 */
  const handleSetDefault = async (id: string, isDefault: boolean) => {
    try {
      await updateMutation.mutateAsync({ id, payload: { is_default: !isDefault } })
      toast.show('默认配置', !isDefault ? '已设为默认' : '已取消默认')
    } catch (e: unknown) {
      const msg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? '操作失败'
      toast.show('默认配置失败', msg)
    }
  }

  /** 重命名 */
  const handleRename = async (id: string) => {
    const name = renameValue.trim()
    if (!name) return
    try {
      await updateMutation.mutateAsync({ id, payload: { name } })
      toast.show('重命名', `已重命名为「${name}」`)
      setRenamingId(null)
      setRenameValue('')
    } catch (e: unknown) {
      const msg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? '重命名失败'
      toast.show('重命名失败', msg)
    }
  }

  /** 删除 */
  const handleDelete = async (id: string, name: string) => {
    try {
      await deleteMutation.mutateAsync(id)
      toast.show('删除配置', `已删除「${name}」`)
    } catch (e: unknown) {
      const msg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? '删除失败'
      toast.show('删除配置失败', msg)
    }
  }

  return (
    <div className="table-preset-menu" ref={containerRef}>
      <button
        className="table-columns-btn"
        onClick={() => setOpen(!open)}
        disabled={presetsQuery.isLoading}
      >
        配置
      </button>
      {open && (
        <div className="table-preset-dropdown">
          {/* 保存当前配置 */}
          <div className="table-preset-save-row">
            <input
              className="input"
              placeholder="新配置名称"
              value={editingName}
              onChange={(e) => setEditingName(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') handleSave() }}
            />
            <button
              className="btn small"
              onClick={handleSave}
              disabled={createMutation.isPending || !editingName.trim()}
            >
              保存
            </button>
          </div>
          {saveError && (
            <div className="table-preset-error">{saveError}</div>
          )}

          {/* 已保存配置列表 */}
          {presets.length === 0 ? (
            <div className="table-preset-empty">暂无已保存配置</div>
          ) : (
            <div className="table-preset-list">
              {presets.map((preset) => (
                <div key={preset.id} className={clsx('table-preset-item', preset.is_default && 'default')}>
                  {renamingId === preset.id ? (
                    <div className="table-preset-rename-row">
                      <input
                        className="input"
                        value={renameValue}
                        onChange={(e) => setRenameValue(e.target.value)}
                        onKeyDown={(e) => { if (e.key === 'Enter') handleRename(preset.id) }}
                      />
                      <button className="btn small" onClick={() => handleRename(preset.id)}>确定</button>
                      <button className="btn small" onClick={() => setRenamingId(null)}>取消</button>
                    </div>
                  ) : (
                    <>
                      <div className="table-preset-item-info">
                        <span
                          className="table-preset-item-name"
                          onClick={() => handleApply(preset.id, preset.name)}
                        >
                          {preset.name}
                          {preset.is_default && <span className="tag info" style={{ marginLeft: 6 }}>默认</span>}
                        </span>
                      </div>
                      <div className="table-preset-item-actions">
                        <button className="btn small" onClick={() => handleApply(preset.id, preset.name)}>应用</button>
                        <button className="btn small" onClick={() => handleOverwrite(preset.id, preset.name)}>覆盖</button>
                        <button
                          className="btn small"
                          onClick={() => {
                            setRenamingId(preset.id)
                            setRenameValue(preset.name)
                          }}
                        >
                          重命名
                        </button>
                        <button
                          className="btn small"
                          onClick={() => handleSetDefault(preset.id, preset.is_default)}
                        >
                          {preset.is_default ? '取消默认' : '设默认'}
                        </button>
                        <button
                          className="btn small danger"
                          onClick={() => handleDelete(preset.id, preset.name)}
                        >
                          删除
                        </button>
                      </div>
                    </>
                  )}
                </div>
              ))}
            </div>
          )}
          {presetsQuery.error && (
            <div className="table-preset-error">配置加载失败</div>
          )}
        </div>
      )}
    </div>
  )
}
