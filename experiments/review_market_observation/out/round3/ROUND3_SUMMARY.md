# ROUND 3 — Existing Review Alignment Audit — SUMMARY

**窗口**: 2026-02-09 .. 2026-08-10 | 120 trading dates

**Adjacency Conclusion**: **ROUND2_CONCLUSION_UNCHANGED**

## §2 Adjacency Micro-check 结果

- Transition eligible pairs: 620918
  - exact T-1: 620630
  - skipped: 288
  - skip ratio: 0.0004638293623312579
- Concentration close pairs: 620918
  - exact T-1: 620630
  - skipped: 288
  - skip ratio: 0.0004638293623312579

修正前后 rho 对比:
  - regime_up_ratio__vs__t_regime_0_1_rate: old=0.3904818709409718 corrected=None delta=None verdict_changed=False
  - regime_down_ratio__vs__t_regime_0_neg1_rate: old=0.3464680203088707 corrected=None delta=None verdict_changed=False
  - swing_up_ratio__vs__t_swing_neg1_1_rate: old=0.4958800114904972 corrected=None delta=None verdict_changed=False
  - momentum_expanding_ratio__vs__t_momdir_contract_expand_rate: old=0.3257305329205649 corrected=None delta=None verdict_changed=False
  - top5_price_contribution__vs__regime_up_ratio: old=0.27845491545154255 corrected=None delta=None verdict_changed=False
  - member_change_hhi__vs__regime_up_ratio: old=0.24463126560212628 corrected=None delta=None verdict_changed=False
  - top5_amount_contribution__vs__regime_up_ratio: old=-0.7755511164809047 corrected=None delta=None verdict_changed=False

## §7 Coverage Matrix Findings 分布

- **NO_DIRECT_COUNTERPART**: 10 components
- **COVERED**: 3 components
- **MIXED**: 5 components
- **REDUNDANT_CANDIDATE**: 7 components
- **PARTIAL**: 2 components

## §8 Primitive Coverage

- **STATE**: PARTIALLY_SUPPORTED
- **TRANSITION**: PARTIALLY_SUPPORTED
- **BREADTH**: MISSING_PRIMITIVE
- **DIFFUSION**: SUPPORTED_CURRENT_DESIGN
- **CONCENTRATION**: PARTIALLY_SUPPORTED
- **PARTICIPATION**: MISSING_PRIMITIVE
- **PRICE**: PARTIALLY_SUPPORTED
- **CROSS_HORIZON_DIVERGENCE**: MISSING_PRIMITIVE

## §9 P/Q/U/C/V Compression Audit Verdict

- **P**: RESTRUCTURE_CANDIDATE (spans 3 dims: ['BREADTH', 'MIXED', 'PRICE'])
- **Q**: RESTRUCTURE_CANDIDATE (spans 3 dims: ['DIFFUSION', 'STATE', 'TRANSITION'])
- **U**: RESTRUCTURE_CANDIDATE (spans 3 dims: ['DIFFUSION', 'PARTICIPATION', 'TRANSITION'])
- **C**: RESTRUCTURE_CANDIDATE (spans 1 dims: ['CONCENTRATION'])
- **V**: INCONCLUSIVE (spans 3 dims: ['MIXED', 'OTHER', 'PARTICIPATION'])

## §10 Archetype Day Clarity

- CLEAR: 9 days
- PARTIAL: 0 days
- OBSCURED: 0 days
- DATE_NOT_FOUND: 0 days

## §12 Specification Findings 总览

- SUPPORTED_CURRENT_DESIGN: 3
- PARTIALLY_SUPPORTED: 2
- SPECIFICATION_DEFECT_CANDIDATE: 5
- MISSING_PRIMITIVE: 1
- REDUNDANT_CANDIDATE: 7
- INCONCLUSIVE: 9

**Specification Defect candidates (混合多维度)**:
  - price_position_median (P, PRICE)
  - structure_net_event_rate (Q, TRANSITION)
  - structure_breakdown_diffusion (Q, DIFFUSION)
  - top5_amount_contribution (C, CONCENTRATION)
  - trend_segment_volume_improvement (V, MIXED)
**Missing primitives**: ['BREADTH', 'PARTICIPATION', 'CROSS_HORIZON_DIVERGENCE']
**Redundant candidates**: ['uptrend_member_ratio', 'main_structure_up_ratio', 'short_structure_up_ratio', 'volume_expansion_ratio', 'amount_expansion_ratio', 'volume_percentile20_median', 'amount_percentile200_median']

---

*详细数据文件见 round3_*.json/csv。*
