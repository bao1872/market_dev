# 盘迹 PRD

## 1. 定位

`docs/prd/` 是盘迹“系统应该怎样工作”的需求与设计意图唯一事实源。

PRD 负责定义：

- 产品目标与边界；
- 功能行为；
- 业务规则；
- 数据口径；
- 指标和因子定义；
- 运行方式；
- 状态语义；
- 验收标准；
- 已确认但尚未实现的目标。

PRD 不记录当前代码文件、函数、表、队列和服务位置；这些由对应 Map 记录。

## 2. PRD 与 Maps 的配合

每份领域 PRD 与一份同编号 Map 对应：

| PRD | 对应 Map |
|---|---|
| `00-product-scope.md` | `../maps/00-system-overview.md` |
| `10-market-data.md` | `../maps/10-market-data.md` |
| `20-quant-model.md` | `../maps/20-quant-model.md` |
| `30-after-close.md` | `../maps/30-after-close.md` |
| `40-market-stock-experience.md` | `../maps/40-market-stock-experience.md` |
| `50-watchlist-intraday.md` | `../maps/50-watchlist-intraday.md` |
| `60-permissions-admin.md` | `../maps/60-permissions-admin.md` |
| `70-review.md` | `../maps/70-review.md` |
| `80-system-runtime.md` | `../maps/80-system-runtime.md` |
| `90-system-wide-requirements.md` | `../maps/90-system-wide-implementation.md` |

配合原则：

1. PRD 保存“目标要求”。
2. Map 保存“要求目前怎样落地”。
3. 关键需求使用稳定条款编号。
4. Map 使用相同编号维护实现状态、入口和证据。
5. Map 不复制 PRD 全文。
6. 代码与 PRD 不一致时，Map 如实记录偏差，不得修改 PRD 迎合错误实现。

## 3. 状态

PRD 只使用：

- `草案`：仍在讨论，不作为完整开发依据；
- `已确认`：可以作为开发和验收依据。

实现进度不写入 PRD。

## 4. 文件职责

| 文件 | 需求所有权 |
|---|---|
| `00-product-scope.md` | 产品定位、目标用户、功能边界和核心术语 |
| `10-market-data.md` | 行情、参考数据、数据来源、数据口径和 readiness |
| `20-quant-model.md` | 趋势、结构、动量、筹码和板块模型 |
| `30-after-close.md` | 盘后触发、编排、计算、校验、发布和补跑 |
| `40-market-stock-experience.md` | 行情页、个股详情、图层和导航上下文 |
| `50-watchlist-intraday.md` | 自选、盘中监控和异常信息 |
| `60-permissions-admin.md` | 邀请码、权限和管理后台 |
| `70-review.md` | 复盘模块目标与当前边界 |
| `80-system-runtime.md` | 本地/远程、Git、数据库、Redis、Scheduler 和部署边界 |
| `90-system-wide-requirements.md` | 跨模块统一时间、状态、标识、来源和非功能要求 |

## 5. 新需求流程

```text
提出需求
→ 找到唯一所属 PRD
→ 更新需求条款和验收标准
→ 确认 PRD
→ 开发与验证
→ 更新对应 Map
→ 重要变化写入 Changes
```

## 6. 小 Bug 流程

```text
读取 PRD 确认正确行为
→ 读取 Map 找到实现入口
→ 复现和修复
→ 最小有效验证
→ 入口或关系变化时更新 Map
```

普通 Bug 默认不修改 PRD。

## 7. 条款编号

每份 PRD 使用自己的前缀：

| 文件 | 前缀 |
|---|---|
| 产品范围 | `PS` |
| 市场数据 | `MD` |
| 量化模型 | `QM` |
| 盘后任务 | `AC` |
| 行情与个股体验 | `MX` |
| 自选与盘中 | `WI` |
| 权限与管理 | `PA` |
| 复盘 | `RV` |
| 运行体系 | `SR` |
| 跨系统要求 | `SW` |

条款编号用于 Map、测试和 Change 引用，不要求给每句话编号。
