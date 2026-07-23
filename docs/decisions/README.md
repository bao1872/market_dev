# 架构决策记录（ADR）

本目录存放架构决策记录（Architecture Decision Records）。

格式：`ADR-NNNN-short-title.md`，包含 Context / Decision / Consequences 三段。

PRD V2.0 §7.1 定义。最后更新：CP-19。

## ADR 列表

- [ADR-0001-atomic-snapshot-single-mdas-read.md](ADR-0001-atomic-snapshot-single-mdas-read.md) — Atomic Chart Snapshot 单 MDAS 读取（CP-16 / SNAP-01）
- [ADR-0002-node-cluster-input-contract-isolation.md](ADR-0002-node-cluster-input-contract-isolation.md) — Node Cluster 输入契约隔离（CHANGE-20260720-001）

## ADR 模板

```markdown
# ADR-NNNN: <标题>

- **状态**: proposed | accepted | deprecated | superseded by ADR-XXXX
- **日期**: YYYY-MM-DD
- **关联**: CHANGE-YYYYMMDD-NNN / CP-XX / PR #XX

## Context（背景）

<为什么需要做这个决策？当前问题是什么？涉及哪些约束？>

## Decision（决策）

<做出了什么决策？关键点是什么？引用真实代码路径作为真源。>

## Consequences（后果）

- 正面影响：<...>
- 负面影响：<...>
- 风险与缓解：<...>
- 后续约束：<写入 AGENTS.md 哪条硬规则？>
```

## 规则

- 每个 ADR 唯一编号，不重用
- ADR 一旦 accepted 不可修改，只能被新 ADR superseded
- ADR 必须引用真实代码路径（如 `backend/app/api/chart_snapshot.py`），禁止引用 `ref/`
- ADR 决策必须转化为 AGENTS.md 硬规则或 docs/current/*.md 事实源
