# 盘迹项目文档

## 1. 文档结构

```text
docs/
├── README.md
├── prd/
├── maps/
└── changes/
```

当前文档体系负责：

| 目录 | 回答的问题 |
|---|---|
| `prd/` | 系统应该怎样工作 |
| `maps/` | 系统现在实际上怎样实现 |
| `changes/` | 为什么从原状态变化为当前状态 |

代码实现规则位于仓库根目录的 `rules/`，AI/IDE 工作协议位于 `AGENTS.md`。

## 2. 基本关系

```text
PRD
定义目标要求
    ↓
开发与验证
    ↓
Maps
记录当前实现
    ↓
Changes
保留重要变化的原因、迁移和证据
```

实际工作中：

- 新需求先修改 PRD；
- 开发并验证后更新对应 Map；
- 行为、契约、架构、运行方式或重要数据发生实质变化时创建 Change；
- 普通小 Bug 由 Git 历史记录，不强制创建 Change。

## 3. 领域对齐

PRD 与 Maps 使用相同编号对齐：

| 领域 | PRD | Map |
|---|---|---|
| 产品与系统全貌 | `prd/00-product-scope.md` | `maps/00-system-overview.md` |
| 市场数据 | `prd/10-market-data.md` | `maps/10-market-data.md` |
| 量化模型 | `prd/20-quant-model.md` | `maps/20-quant-model.md` |
| 盘后任务 | `prd/30-after-close.md` | `maps/30-after-close.md` |
| 行情与个股体验 | `prd/40-market-stock-experience.md` | `maps/40-market-stock-experience.md` |
| 自选与盘中 | `prd/50-watchlist-intraday.md` | `maps/50-watchlist-intraday.md` |
| 权限与管理 | `prd/60-permissions-admin.md` | `maps/60-permissions-admin.md` |
| 复盘 | `prd/70-review.md` | `maps/70-review.md` |
| 运行体系 | `prd/80-system-runtime.md` | `maps/80-system-runtime.md` |
| 跨系统要求 | `prd/90-system-wide-requirements.md` | `maps/90-system-wide-implementation.md` |

## 4. 当前状态

- PRD 包含已经确认的目标以及仍需确认的草案。
- Maps 统一以真实代码和运行证据为准；尚未核验的内容明确标为待重建或未核验。
- Changes 只记录重要变化，不作为普通开发流水账。
- 不再使用 `docs/current/` 作为另一套事实源。
