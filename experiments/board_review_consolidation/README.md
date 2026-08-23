# Board → Review Consolidation — Slice 1 (Design / Audit)

**状态：DESIGN / AUDIT ONLY。未修改任何生产代码、未做 DB migration、未 DROP 表、未删 Board 数据、未部署。**

## 目标
建立不可遗漏的迁移合同：Board Analysis 全部有效功能一个都不能静默丢失；重复功能归并到唯一 Review owner；冲突算法明确选 owner；Review 对 Board runtime 的依赖全部找出来。

## 文件
| 文件 | 内容 |
|---|---|
| `board_review_ownership_matrix.json` | 字段级 ownership matrix（V1+V2 全展开），含 classification + target_disposition + `information_parity` 块 |
| `board_consumer_graph.json` | 全仓 consumer 分类（analytics payload vs identity/lineage vs publication pointer） |
| `source_board_run_dependency.json` | `source_board_run_id` 全依赖链 + Slice 2 替代身份方案（不迁 schema） |
| `scope_discovery_target.json` | Review scope discovery 脱离 BoardAnalysisRun 的设计（保持相同 universe + PIT lineage） |
| `feature_preservation_manifest.json` | 全功能保留清单 + 目标架构（retire target 仅定义不执行） |

## 关键发现（纠正预设）
- **Review 对 Board 的真实依赖是 identity/lineage（`source_board_run_id` / `BoardAnalysisRun` 存在性），不是 Board analytics payload。**
- Review scope discovery 对 industry/concept 实际直接查 `MarketBoard` + `resolve_board_membership_at`（PIT membership），**不读 `BoardAnalysisSnapshot` analytics**。
- `pyramid_v2` 不是 Review consumer；Review `filter_engine` 用自身 canonical `state_transitions` 派生 diffusion。
- 真正消费 Board ANALYTICS payload 的路径只有 `review_attribution_service`（member attribution 读 snapshot payload）。

## classification 词汇
`EXACT_DUPLICATE` / `REVIEW_SUPERSET` / `SEMANTIC_OVERLAP_DIFFERENT_FORMULA` / `SEMANTIC_OVERLAP_DIFFERENT_AGGREGATION` / `SEMANTIC_OVERLAP_DIFFERENT_TIME_SEMANTICS` / `BOARD_UNIQUE` / `INFRASTRUCTURE_NOT_ANALYTICS` / `LEGACY_UNUSED` / `UNKNOWN_NEEDS_DECISION`

## target_disposition 词汇
`USE_REVIEW_OWNER` / `MIGRATE_TO_REVIEW` / `DERIVE_FROM_REVIEW_FACTS` / `DERIVE_FROM_MEMBER_FACTS` / `RENAME_AND_MIGRATE` / `KEEP_INFRASTRUCTURE_ONLY` / `RETIRE` / `NEEDS_PRODUCT_DECISION`

## information_parity 词汇（Slice 1S 新增）
每个非基础设施字段带 `information_parity` 块：
- `EXACT`：Board 信息从 Review payload 逐字段一致可得。
- `DERIVABLE`：Board 信息可由 Review 当前 persisted payload 完整恢复（如分类计数）。
- `LOSSY`：Board 信息**不能**从 Review 当前 payload 恢复（如 avg/P25/P50/P75 vs 单 median；badge 计数；histogram）。
- `DIFFERENT_SEMANTICS`：Board 与 Review 语义本质不同（DIFFERENT_TIME_SEMANTICS 等），不能相互替代。

**硬规则**：
- `information_parity ∈ {LOSSY, DIFFERENT_SEMANTICS}` → `target_disposition` **不得**为 `USE_REVIEW_OWNER`，必须是 `MIGRATE_TO_REVIEW` / `RENAME_AND_MIGRATE` / `DERIVE_FROM_MEMBER_FACTS`。
- `target_disposition == USE_REVIEW_OWNER` → 要求 `information_parity ∈ {EXACT, DERIVABLE}`。
- `DERIVE_FROM_MEMBER_FACTS`：不重复保存基础事实；统一 Review batch-prepare 一次消费同一批 member facts 产生所有需要的 aggregate。

## Slice 1S 信息无损审计结果（7 组）
| 组 | Board 信息 | Review 当前 | parity | disposition |
|---|---|---|---|---|
| `trend_strength` | avg/P25/P50/P75 | regime_strength = median only | LOSSY | MIGRATE_TO_REVIEW |
| `vwap_dev_pct` | avg/P25/P50/P75 | dsa_vwap_dev_pct = median only | LOSSY | MIGRATE_TO_REVIEW |
| `momentum.enhancing/fading/flat` | change 维度计数 | state(方向)/transition(迁移) | DIFFERENT_SEMANTICS | MIGRATE_TO_REVIEW |
| `momentum.avg_sqzmom` | mean of fp_sqzmom | bb_position/width（不同振荡器） | DIFFERENT_SEMANTICS | MIGRATE_TO_REVIEW |
| `volume.high/low/normal/unknown` | badge 计数 | p25/p50/p75（无计数） | LOSSY | MIGRATE_TO_REVIEW |
| `volume.avg_volume_ratio20/200` | mean | p25/p50/p75（无 mean） | LOSSY | MIGRATE_TO_REVIEW |
| `volume.percentile_20/200_dist` | 5-bin histogram | p25/p50/p75 | LOSSY | MIGRATE_TO_REVIEW |
| `structure_events.*` | latest-event snapshot state | exact-T event stream | DIFFERENT_TIME_SEMANTICS | MIGRATE_TO_REVIEW |
| `structure.avg_active_ob_count` | 独有 capability | v2.3 已移除 | DIFFERENT_SEMANTICS | MIGRATE_TO_REVIEW |

以上均不得静默 `USE_REVIEW_OWNER` / `RETIRE`。

## 测试
`tests/test_board_review_consolidation_contract.py`（纯单元，不连库）：
- 100% Board capability 在 matrix 有条目
- BOARD_UNIQUE 不得 RETIRE
- SEMANTIC_OVERLAP 不得无说明映射 USE_REVIEW_OWNER
- Infrastructure (taxonomy/PIT/lineage) 不被分析层退役误伤
- **(Slice 1S) `information_parity` 硬门**：LOSSY/DIFFERENT_SEMANTICS 不得 USE_REVIEW_OWNER；USE_REVIEW_OWNER 要求 DERIVABLE/EXACT；不可恢复字段须带 `required_preservation_action`。

运行：`PURE_UNIT_TEST=1 pytest tests/test_board_review_consolidation_contract.py`

## Slice 2 第一刀（待授权）
先让 Review Scope Discovery 脱离 `BoardAnalysisSnapshot` / `BoardAnalysisRun`，保持相同 scope universe + PIT lineage；再决定物理退役 `BoardAnalysisRun` / `BoardAnalysisSnapshot` / `market_aggregation` 历史行。
