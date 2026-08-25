# Round 2 Summary — 候选观察维度审计（DB-native / query-on-demand）

- EXP_SHA = ec78675bde74eb2ed0940bd039c86294b6a4968f
- DEV_BASE_SHA = 6fc7384228b2e51f13d3cf5af2a6b6a26b2837b0
- 窗口 = 2026-02-09 .. 2026-08-10（120 交易日, is_exact_target=True）

## A. State vs Transition

- regime_up_ratio vs t_regime_0_1_rate: spearman=0.39 → PARTIALLY_SUPPORTED
- regime_down_ratio vs t_regime_0_neg1_rate: spearman=0.346 → PARTIALLY_SUPPORTED
- swing_up_ratio vs t_swing_neg1_1_rate: spearman=0.496 → PARTIALLY_SUPPORTED
- momentum_expanding_ratio vs t_momdir_contract_expand_rate: spearman=0.326 → PARTIALLY_SUPPORTED

## B. Breadth vs Diffusion

- A_high_pos: 29 天
- B_high_neg: 31 天
- C_low_pos: 17 天
- D_low_neg: 43 天
（A=高breadth+正扩散 / B=高breadth+负扩散 / C=低breadth+正扩散 / D=低breadth+负扩散）

## C. Breadth vs Concentration

- top5_price_contribution__vs__regime_up_ratio: spearman=0.278
- member_change_hhi__vs__regime_up_ratio: spearman=0.245
- top5_amount_contribution__vs__regime_up_ratio: spearman=-0.776

## D. Participation

- volume_ratio20_median: vs_regime=-0.087 vs_mom=0.369 → NOT_REDUNDANT
- amount_ratio20_median: vs_regime=0.016 vs_mom=0.524 → NOT_REDUNDANT
- volume_above_1_ratio: vs_regime=-0.027 vs_mom=0.379 → NOT_REDUNDANT
- amount_above_1_ratio: vs_regime=0.065 vs_mom=0.548 → NOT_REDUNDANT

## E. Redundancy（|rho|>0.85）

- denom ~ regime_up_cnt: -0.8864 HIGH_REDUNDANCY_CANDIDATE
- denom ~ regime_down_cnt: 0.9347 HIGH_REDUNDANCY_CANDIDATE
- denom ~ swing_up_cnt: -0.913 HIGH_REDUNDANCY_CANDIDATE
- denom ~ swing_down_cnt: 0.9409 HIGH_REDUNDANCY_CANDIDATE
- denom ~ regime_up_ratio: -0.8906 HIGH_REDUNDANCY_CANDIDATE
- denom ~ regime_down_ratio: 0.9285 HIGH_REDUNDANCY_CANDIDATE
- denom ~ swing_up_ratio: -0.9279 HIGH_REDUNDANCY_CANDIDATE
- denom ~ swing_down_ratio: 0.9356 HIGH_REDUNDANCY_CANDIDATE
- denom ~ n_common: 0.9966 HIGH_REDUNDANCY_CANDIDATE
- denom ~ conc_denom: 0.9965 HIGH_REDUNDANCY_CANDIDATE
- regime_up_cnt ~ regime_down_cnt: -0.9062 HIGH_REDUNDANCY_CANDIDATE
- regime_up_cnt ~ swing_up_cnt: 0.9454 HIGH_REDUNDANCY_CANDIDATE
- regime_up_cnt ~ swing_down_cnt: -0.9383 HIGH_REDUNDANCY_CANDIDATE
- regime_up_cnt ~ regime_strength_median: 0.895 HIGH_REDUNDANCY_CANDIDATE
- regime_up_cnt ~ regime_up_ratio: 0.9998 HIGH_REDUNDANCY_CANDIDATE
- regime_up_cnt ~ regime_down_ratio: -0.9061 HIGH_REDUNDANCY_CANDIDATE
- regime_up_cnt ~ swing_up_ratio: 0.9418 HIGH_REDUNDANCY_CANDIDATE
- regime_up_cnt ~ swing_down_ratio: -0.9398 HIGH_REDUNDANCY_CANDIDATE
- regime_up_cnt ~ n_common: -0.882 HIGH_REDUNDANCY_CANDIDATE
- regime_up_cnt ~ conc_denom: -0.8795 HIGH_REDUNDANCY_CANDIDATE
- regime_neutral_cnt ~ regime_neutral_ratio: 0.9984 HIGH_REDUNDANCY_CANDIDATE
- regime_down_cnt ~ swing_up_cnt: -0.9555 HIGH_REDUNDANCY_CANDIDATE
- regime_down_cnt ~ swing_down_cnt: 0.9652 HIGH_REDUNDANCY_CANDIDATE
- regime_down_cnt ~ regime_strength_median: -0.8549 HIGH_REDUNDANCY_CANDIDATE
- regime_down_cnt ~ regime_up_ratio: -0.9069 HIGH_REDUNDANCY_CANDIDATE
- regime_down_cnt ~ regime_down_ratio: 0.9987 HIGH_REDUNDANCY_CANDIDATE
- regime_down_cnt ~ swing_up_ratio: -0.9599 HIGH_REDUNDANCY_CANDIDATE
- regime_down_cnt ~ swing_down_ratio: 0.9644 HIGH_REDUNDANCY_CANDIDATE
- regime_down_cnt ~ n_common: 0.9324 HIGH_REDUNDANCY_CANDIDATE
- regime_down_cnt ~ conc_denom: 0.9324 HIGH_REDUNDANCY_CANDIDATE
- swing_up_cnt ~ swing_down_cnt: -0.995 HIGH_REDUNDANCY_CANDIDATE
- swing_up_cnt ~ regime_strength_median: 0.9266 HIGH_REDUNDANCY_CANDIDATE
- swing_up_cnt ~ regime_up_ratio: 0.9451 HIGH_REDUNDANCY_CANDIDATE
- swing_up_cnt ~ regime_down_ratio: -0.9537 HIGH_REDUNDANCY_CANDIDATE
- swing_up_cnt ~ swing_up_ratio: 0.9984 HIGH_REDUNDANCY_CANDIDATE
- swing_up_cnt ~ swing_down_ratio: -0.9964 HIGH_REDUNDANCY_CANDIDATE
- swing_up_cnt ~ n_common: -0.9119 HIGH_REDUNDANCY_CANDIDATE
- swing_up_cnt ~ conc_denom: -0.9102 HIGH_REDUNDANCY_CANDIDATE
- swing_down_cnt ~ regime_strength_median: -0.9108 HIGH_REDUNDANCY_CANDIDATE
- swing_down_cnt ~ regime_up_ratio: -0.939 HIGH_REDUNDANCY_CANDIDATE

共 184 对

## F. Cross-horizon divergence

- weak_trend + internal/momentum improving: 5 天
- strong_trend + internal/momentum weakening: 8 天

## G. Archetype Days

- 2026-04-01 [max_regime_breadth_expansion] 最大 regime_up breadth 5日扩张
- 2026-03-24 [max_regime_breadth_contraction] 最大 regime_up breadth 5日收缩
- 2026-04-17 [max_momentum_expansion] 最大 momentum expanding 5日扩张
- 2026-03-23 [max_momentum_contraction] 最大 momentum expanding 5日收缩
- 2026-03-31 [max_positive_transition] 最大 regime 0->1 当日 transition rate
- 2026-06-05 [max_negative_transition] 最大 regime 0->-1 当日 transition rate
- 2026-05-13 [max_concentration] 成员变化 HHI 最高
- 2026-03-02 [max_participation_increase] 参与度(amount>1) 单日增幅最大
- 2026-03-03 [high_internal_transition_low_regime_change] 短周期内部 transition 大但长周期 regime 平稳

## H. Round 2 Verdict（按 candidate）

- State vs Transition information distinctness: PARTIALLY_SUPPORTED
- Breadth vs Diffusion independence: PARTIALLY_SUPPORTED
- Concentration independence from Breadth: PARTIALLY_SUPPORTED
- Participation distinctness: PARTIALLY_SUPPORTED

> 本结果仅用于候选维度判断，不构成 Review PRD 修改建议。