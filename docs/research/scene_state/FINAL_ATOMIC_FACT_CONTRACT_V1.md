# Atomic Fact Contract V1 — Final Research Handover

## 0. 文档元信息

| 字段 | 值 |
|------|-----|
| 契约版本 | Atomic Fact Contract V1 |
| 研究等级 | B-FINAL |
| 研究状态 | 正式关闭 |
| 实验分支 | experiment/hierarchical-scene-state-v3 |
| 数据范围 | 2026-07-07、08、09、10、13、14 |
| 排除日期 | 2026-07-06（永久排除） |
| 样本总量 | 31,758 条 (symbol, trade_date) 唯一记录 |
| 每日预期 | 5,293 条 |
| 覆盖率阈值 | 95% |
| 公式一致率目标 | 100% |
| 逻辑冲突目标 | 0 |
| 上游实验报告 | `/home/ubuntu/panji_research_outputs/scene_state_v3/final_v4_12_atomic_fact_contract_closure.md` |
| 上游实验报告 | `/home/ubuntu/panji_research_outputs/scene_state_v3/final_v4_13_atomic_fact_contract_freeze.md` |
| 上游脚本 | `research/experiments/v4_12_atomic_fact_contract_closure.py` |
| 上游脚本 | `research/experiments/v4_13_atomic_fact_contract_freeze.py` |

> 本文件仅冻结研究结论与计算契约，不修改 `main`，不实现列表页，不修改生产后端，不部署，不创建 PR。

---

## 1. 研究背景与最终结论

### 1.1 背景

日线市场状态不再采用 Pattern、聚类标签、综合分数或反转概率，而采用：

```text
DSA 趋势主干
＋动量修饰
＋Confirmed/Active 结构修饰
＋Segment 平均量能修饰
```

DSA 是唯一具有“趋势方向”定义权的字段。Active Swing 只描述当前主 Swing 结构；Developing Swing 只描述更局部的 Swing 变化。任何 Swing 方向变化均不能单独称为趋势形成或趋势反转。

### 1.2 最终研究结论

```text
B-FINAL
```

| 层级 | 数量 |
|------|------|
| Core Facts | 14 |
| Auxiliary Facts | 10 |
| Rejected / UI 禁用 | 1 |
| 概念禁用 | 多项（综合分数、反转概率、买卖建议等） |

### 1.3 关键门禁（六日 31,758 条样本）

| 检查项 | 期望 | 实际 | 结果 |
|--------|------|------|------|
| 六日总记录数 | 31,758 | 31,758 | PASS |
| 公式一致率 | 100% | 100% | PASS |
| 逻辑冲突 | 0 | 0 | PASS |
| 效率越界记录 | 137 | 137 | PASS |
| 累计量比 ↔ 年龄比 Pearson | — | 0.6405 | 高污染 |
| 均量比 ↔ 年龄比 Pearson | — | -0.0149 | 低污染 |
| Shadow 效率有限值 ∈ [0,1] | 是 | violations=0 | PASS |
| 越界记录修复 | 137/137 | 137/137 | PASS |
| 逐日 computable coverage ≥ 95% | 是 | 0.9734–0.9960 | PASS |
| 模板禁用词命中 | 0 | 0 | PASS |
| 模板逻辑矛盾 | 0 | 0 | PASS |
| Rejected 字段出现次数 | 0 | 0 | PASS |
| Peak RSS | < 400MB | 233.5MB | PASS |

### 1.4 研究阶段关闭声明

```text
原子事实研究阶段正式关闭。
后续不再开展 V4.14 或新的原子事实实验。
后续工作全部转入工程实现、生产效率修复和回归测试。
```

---

## 2. Core Facts（14 项）

进入 main 列表视图右侧默认状态区域。

### 2.1 趋势 Trend

#### T1_trend_direction

| 字段 | 值 |
|------|-----|
| ID | T1_trend_direction |
| 中文名 | DSA 方向 |
| 维度 | trend |
| 层级 | core |
| 真实路径 | `structural_payload.primary.1d.dsa_segment.current_dsa_segment_dir` |
| 公式 | 直接取值（1 → UP, -1 → DOWN, 0 → NONE） |
| raw_type | categorical |
| NULL 规则 | 字段缺失 → MISSING → 省略该行 |
| 展示模板 | DSA 当前趋势方向为 {上行/下行/中性} |
| 展示顺序 | 1 |
| 禁止解释 | 买卖方向、未来涨跌预测 |
| 旧字段别名 | — |

#### T2_aligned_slope

| 字段 | 值 |
|------|-----|
| ID | T2_aligned_slope |
| 中文名 | 方向对齐斜率 |
| 维度 | trend |
| 层级 | core |
| 真实路径 | `structural_payload.primary.1d.dsa_segment.current_dsa_segment_slope_atr_per_bar` |
| 公式 | `dsa_dir × current_slope_atr_per_bar` |
| raw_type | continuous |
| 单位 | ATR/bar |
| NULL 规则 | dsa_dir 缺失或=0 → MISSING；cur_slope_atr 缺失 → MISSING → 省略 |
| 展示模板 | 方向对齐斜率为 {value:.4f} ATR/bar |
| 展示顺序 | 2 |
| 禁止解释 | 斜率大等于确定延续 |
| 旧字段别名 | — |

#### T4_trend_age

| 字段 | 值 |
|------|-----|
| ID | T4_trend_age |
| 中文名 | 当前 Segment 年龄 |
| 维度 | trend |
| 层级 | core |
| 真实路径 | `structural_payload.primary.1d.dsa_segment.current_dsa_segment_age_bars` |
| 公式 | 直接取值 |
| raw_type | int |
| NULL 规则 | 字段缺失 → MISSING → 省略 |
| 展示模板 | 当前 Segment 已持续 {value} 根 bar |
| 展示顺序 | 3 |
| 禁止解释 | 成熟、衰竭、即将反转 |
| 旧字段别名 | — |

#### T5_slope_ratio

| 字段 | 值 |
|------|-----|
| ID | T5_slope_ratio |
| 中文名 | 当前段相对前段的速度关系 |
| 维度 | trend |
| 层级 | core |
| 真实路径 | `current_dsa_segment_slope_atr_per_bar` 与 `prev_dsa_segment_slope_atr_per_bar` |
| 公式 | `abs(current_slope_atr) / abs(previous_slope_atr)`，>上阈值为加速，<下阈值为减速，其余为相近 |
| raw_type | continuous → categorical |
| NULL 规则 | prev_slope_atr 缺失或=0 → MISSING → 省略 |
| 展示模板 | 斜率相对前段：{加速/减速/相近} |
| 展示顺序 | 4 |
| 阈值 | 由 `thresholds.t5_slope_ratio` 统一配置；当前实验未硬编码上下阈值，工程化时需根据 V4.12 q25/q75 候选确认 |
| 禁止解释 | 加速等于买入，减速等于卖出 |
| 旧字段别名 | — |

### 2.2 动量 Momentum

#### M1_momentum_alignment

| 字段 | 值 |
|------|-----|
| ID | M1_momentum_alignment |
| 中文名 | 动量与趋势关系 |
| 维度 | momentum |
| 层级 | core |
| 真实路径 | `volatility_momentum.sqzmom_val` 与 `dsa_segment.current_dsa_segment_dir` |
| 公式 | 比较 `sign(sqzmom_val)` 与 dsa_dir；同向→ALIGNED，反向→COUNTER，其余→ZERO |
| raw_type | categorical |
| NULL 规则 | 任一字段缺失或 dsa_dir=0 → MISSING → 省略 |
| 展示模板 | SQZMOM 动量与趋势 {同向/逆向/中性} |
| 展示顺序 | 5 |
| 禁止解释 | 同向等于上涨信号 |
| 旧字段别名 | — |

#### M2_aligned_momentum

| 字段 | 值 |
|------|-----|
| ID | M2_aligned_momentum |
| 中文名 | 方向对齐动量 |
| 维度 | momentum |
| 层级 | core |
| 真实路径 | `volatility_momentum.sqzmom_val` 与 `dsa_segment.current_dsa_segment_dir` |
| 公式 | `dsa_dir × sqzmom_val` |
| raw_type | continuous |
| NULL 规则 | 任一字段缺失或 dsa_dir=0 → MISSING → 省略 |
| 展示模板 | 方向对齐动量值为 {value:.4f} |
| 展示顺序 | 6 |
| 禁止解释 | 正值等于上涨信号 |
| 旧字段别名 | — |

#### M3_aligned_momentum_delta

| 字段 | 值 |
|------|-----|
| ID | M3_aligned_momentum_delta |
| 中文名 | 最近一 Bar 方向对齐动量变化 |
| 维度 | momentum |
| 层级 | core |
| 真实路径 | `volatility_momentum.sqzmom_delta_1` 与 `dsa_segment.current_dsa_segment_dir` |
| 公式 | `dsa_dir × sqzmom_delta_1`；输出 Raw 值 + POSITIVE/NEGATIVE/ZERO 三态 |
| raw_type | continuous + categorical |
| NULL 规则 | 任一字段缺失或 dsa_dir=0 → MISSING → 省略 |
| 展示模板 | 最近一 Bar 对齐动量变化：{正/负/零}（raw={value:.6f}） |
| 展示顺序 | 7 |
| 零值容差 | 由字段存储精度决定，配置在 `thresholds.m3_zero_tolerance`；**禁止使用六日中位数或分位数进行产品分类**；当前实验未硬编码阈值，工程化时需根据 `sqzmom_delta_1` 存储精度确认 |
| 禁止解释 | 正变化等于买入信号 |
| 旧字段别名 | — |

#### M5_squeeze_state

| 字段 | 值 |
|------|-----|
| ID | M5_squeeze_state |
| 中文名 | Squeeze 状态 |
| 维度 | momentum |
| 层级 | core |
| 真实路径 | `volatility_momentum.sqz_on` 与 `volatility_momentum.sqz_off` |
| 公式 | sqz_on=true → 挤压中；sqz_off=true → 释放中；均为 false → 正常；均为 true → INCONSISTENT |
| raw_type | categorical |
| NULL 规则 | 任一字段缺失 → MISSING → 省略 |
| 展示模板 | 波动率挤压状态：{挤压中/释放中/正常} |
| 展示顺序 | 8 |
| 禁止解释 | 挤压等于即将上涨或下跌；不解释突破方向 |
| 旧字段别名 | — |

### 2.3 结构 Structure

#### S1_confirmed_boundary_relation

| 字段 | 值 |
|------|-----|
| ID | S1_confirmed_boundary_relation |
| 中文名 | Confirmed 边界关系 |
| 维度 | structure |
| 层级 | core |
| 真实路径 | `swing_position.confirmed_swing_breakout_state` 与 `dsa_segment.current_dsa_segment_dir` |
| 公式 | 按 dsa_dir 与 breakout_state 组合映射为：顺 DSA 方向越界 / 区间内 / 逆 DSA 方向越界 |
| raw_type | categorical |
| NULL 规则 | 任一字段缺失或 dsa_dir=0 → MISSING → 省略 |
| 展示模板 | {顺 DSA 方向突破确认边界/价格在区间内/逆 DSA 方向边界破坏} |
| 展示顺序 | 9 |
| 禁止解释 | 越界等于交易确认 |
| 旧字段别名 | — |

#### S2_active_dir_relation

| 字段 | 值 |
|------|-----|
| ID | S2_active_dir_relation |
| 中文名 | Active Swing 与 DSA 方向关系 |
| 维度 | structure |
| 层级 | core |
| 真实路径 | `swing_position.active_swing_dir` 与 `dsa_segment.current_dsa_segment_dir` |
| 公式 | 同向 → ALIGNED；反向 → COUNTER |
| raw_type | categorical |
| NULL 规则 | 任一字段缺失或 dsa_dir=0 → MISSING → 省略 |
| 展示模板 | Active Swing 方向与 DSA {一致/相反} |
| 展示顺序 | 10 |
| 禁止解释 | Active 方向变化称为新趋势 |
| 旧字段别名 | — |

#### S3_active_position

| 字段 | 值 |
|------|-----|
| ID | S3_active_position |
| 中文名 | 价格在 Active Swing 中的位置 |
| 维度 | structure |
| 层级 | core |
| 真实路径 | `swing_position.price_position_in_active_swing_0_1` |
| 公式 | 0–0.33 → 偏低区间；0.33–0.67 → 中间区间；0.67–1.0 → 偏高区间 |
| raw_type | continuous → categorical |
| NULL 规则 | 字段缺失或不在 [0,1] → MISSING → 省略 |
| 展示模板 | 价格在 Active Swing 区间内位置：{偏低/中/高} |
| 展示顺序 | 11 |
| 阈值 | `thresholds.s3_position_lower=0.33`, `thresholds.s3_position_upper=0.67` |
| 禁止解释 | 便宜、昂贵、买点、卖点 |
| 旧字段别名 | — |

#### S7_dist_favorable_boundary

| 字段 | 值 |
|------|-----|
| ID | S7_dist_favorable_boundary |
| 中文名 | 距顺 DSA 方向 Confirmed 边界距离 |
| 维度 | structure |
| 层级 | core |
| 真实路径 | dsa_dir>0 → `swing_position.distance_to_swing_high_atr`；dsa_dir<0 → `swing_position.distance_to_swing_low_atr` |
| 公式 | 按 dsa_dir 选择对应边界距离（有符号 ATR） |
| raw_type | continuous |
| NULL 规则 | dsa_dir=0 或对应距离缺失 → MISSING → 省略 |
| 展示模板 | 距顺 DSA 方向确认边界：{尚未到达 X ATR / 已越过 X ATR} |
| 展示顺序 | 12 |
| UI 规则 | **禁止显示负距离**；统一转换为"尚未到达 |d| ATR"（d≥0）或"已越过 |d| ATR"（d<0） |
| 禁止解释 | 止损、安全距离 |
| 旧字段别名 | — |

#### S8_dist_adverse_boundary

| 字段 | 值 |
|------|-----|
| ID | S8_dist_adverse_boundary |
| 中文名 | 距逆 DSA 方向 Confirmed 边界距离 |
| 维度 | structure |
| 层级 | core |
| 真实路径 | dsa_dir>0 → `swing_position.distance_to_swing_low_atr`；dsa_dir<0 → `swing_position.distance_to_swing_high_atr` |
| 公式 | 按 dsa_dir 选择对应边界距离（有符号 ATR） |
| raw_type | continuous |
| NULL 规则 | dsa_dir=0 或对应距离缺失 → MISSING → 省略 |
| 展示模板 | 距逆 DSA 方向确认边界：{尚未到达 X ATR / 已越过 X ATR} |
| 展示顺序 | 13 |
| UI 规则 | **禁止显示负距离**；统一转换为"尚未到达 |d| ATR"（d≥0）或"已越过 |d| ATR"（d<0） |
| 禁止解释 | 止损、安全距离 |
| 旧字段别名 | — |

### 2.4 成交量 Volume

#### V3_avg_volume_ratio

| 字段 | 值 |
|------|-----|
| ID | V3_avg_volume_ratio |
| 中文名 | 当前 Segment 相对前一 Segment 的平均每 Bar 量比 |
| 维度 | volume |
| 层级 | core |
| 真实路径 | `dsa_segment.current_segment_volume_sum`、`current_dsa_segment_age_bars`、`prev_segment_volume_sum`、`prev_dsa_segment_age_bars` |
| 公式 | `(cur_vol_sum / cur_age) / (prev_vol_sum / prev_age)` |
| raw_type | continuous |
| NULL 规则 | 任一字段缺失、age≤0、prev_avg=0 → MISSING → 省略 |
| 展示模板 | Segment 均量与前段 {高/低/相近}（ratio={value:.4f}） |
| 展示顺序 | 14 |
| 阈值 | `thresholds.v3_ratio_lower`, `thresholds.v3_ratio_upper`；当前实验未硬编码上下阈值，工程化时需根据 V4.12 q25/q75 候选确认 |
| 禁止解释 | 放量、缩量；不得用累计 Segment 量比代替 |
| 旧字段别名 | — |

---

## 3. Auxiliary Facts（10 项）

不进入右侧默认列表，可供未来详情展开、调试或生产修复后升级。

### 3.1 趋势效率（工程欠账）

#### T3_trend_efficiency

| 字段 | 值 |
|------|-----|
| ID | T3_trend_efficiency |
| 中文名 | 当前 Segment 趋势效率 |
| 维度 | trend |
| 层级 | auxiliary |
| 默认 UI 启用 | false |
| 真实路径 | `structural_payload.primary.1d.dsa_segment.current_dsa_segment_efficiency_0_1` |
| 公式 | 生产公式：`abs(last_close - cur_start_price) / np.nansum(np.abs(np.diff(seg_closes)))`；Shadow 公式：`abs(close_end - close_start) / sum(abs(diff(close_path)))`（禁止 nansum、禁止 clip） |
| raw_type | continuous |
| NULL 规则 | 路径含 NaN、长度<2、分母≤0 → NULL |
| 展示模板 | 趋势效率为 {value:.4f}（仅修复后启用） |
| 禁止解释 | 效率高等于确定延续 |
| 工程欠账 | 见 §5 |

#### T6_efficiency_delta

| 字段 | 值 |
|------|-----|
| ID | T6_efficiency_delta |
| 中文名 | 当前段相对前段效率差 |
| 维度 | trend |
| 层级 | auxiliary |
| 默认 UI 启用 | false |
| 真实路径 | `current_dsa_segment_efficiency_0_1` 与 `prev_dsa_segment_efficiency_0_1` |
| 公式 | `cur_efficiency - prev_efficiency` |
| raw_type | continuous |
| NULL 规则 | T3 或 prev_efficiency 为 NULL → 传播 NULL |
| 展示模板 | 效率差为 {value:.4f}（仅修复后启用） |
| 禁止解释 | 效率差扩大等于趋势加强 |
| 工程欠账 | 见 §5 |

### 3.2 动量辅助

#### M4_segment_momentum_change

| 字段 | 值 |
|------|-----|
| ID | M4_segment_momentum_change |
| 中文名 | Segment 起点至当前的动量变化 |
| 维度 | momentum |
| 层级 | auxiliary |
| 默认 UI 启用 | false |
| 真实路径 | `temporal_payload.daily_context.daily_sqzmom_change_since_segment_start` 与 `dsa_segment.current_dsa_segment_dir` |
| 公式 | `dsa_dir × daily_sqzmom_change_since_segment_start` |
| raw_type | continuous |
| NULL 规则 | 任一字段缺失或 dsa_dir=0 → MISSING → 省略 |
| 展示模板 | Segment 起点动量变化：{value:.4f} |
| 禁止解释 | 段内动量变化直接预测延续 |

### 3.3 结构辅助（Developing Swing 定位）

#### S4_developing_dir_relation

| 字段 | 值 |
|------|-----|
| ID | S4_developing_dir_relation |
| 中文名 | Developing Swing 与 DSA 方向关系 |
| 维度 | structure |
| 层级 | auxiliary |
| 默认 UI 启用 | false |
| 真实路径 | `swing_position.developing_swing_dir` 与 `dsa_segment.current_dsa_segment_dir` |
| 公式 | 同向 → ALIGNED；反向 → COUNTER |
| raw_type | categorical |
| NULL 规则 | 任一字段缺失或 dsa_dir=0 → MISSING → 省略 |
| 展示模板 | Developing Swing 方向与 DSA {一致/相反} |

#### S5_active_vs_developing

| 字段 | 值 |
|------|-----|
| ID | S5_active_vs_developing |
| 中文名 | Active 与 Developing 关系 |
| 维度 | structure |
| 层级 | auxiliary |
| 默认 UI 启用 | false |
| 真实路径 | `swing_position.active_swing_dir` 与 `swing_position.developing_swing_dir` |
| 公式 | 同向 → ALIGNED；反向 → COUNTER |
| raw_type | categorical |
| NULL 规则 | 任一字段缺失 → MISSING → 省略 |
| 展示模板 | Active 与 Developing {一致/相反} |

#### S6_developing_position

| 字段 | 值 |
|------|-----|
| ID | S6_developing_position |
| 中文名 | 价格在 Developing Swing 中的位置 |
| 维度 | structure |
| 层级 | auxiliary |
| 默认 UI 启用 | false |
| 真实路径 | `swing_position.price_position_in_developing_swing_0_1` |
| 公式 | 0–0.33 → 偏低区间；0.33–0.67 → 中间区间；0.67–1.0 → 偏高区间 |
| raw_type | continuous → categorical |
| NULL 规则 | 字段缺失或不在 [0,1] → MISSING → 省略 |
| 展示模板 | 价格在 Developing 区间位置：{偏低/中/高} |

### 3.4 成交量辅助

#### V2_current_avg_volume

| 字段 | 值 |
|------|-----|
| ID | V2_current_avg_volume |
| 中文名 | 当前 Segment 平均每 Bar 成交量 |
| 维度 | volume |
| 层级 | auxiliary |
| 默认 UI 启用 | false |
| 真实路径 | `dsa_segment.current_segment_volume_sum` 与 `current_dsa_segment_age_bars` |
| 公式 | `current_segment_volume_sum / current_dsa_segment_age_bars` |
| raw_type | continuous |
| NULL 规则 | 任一字段缺失或 age≤0 → MISSING → 省略 |
| 展示模板 | 当前 Segment 平均每 Bar 量：{value:.2f} |

#### V4_age_ratio_raw

| 字段 | 值 |
|------|-----|
| ID | V4_age_ratio_raw |
| 中文名 | 当前段相对前段年龄比 |
| 维度 | volume |
| 层级 | auxiliary |
| 默认 UI 启用 | false |
| 真实路径 | `current_dsa_segment_age_bars` 与 `prev_dsa_segment_age_bars` |
| 公式 | `cur_age / prev_age` |
| raw_type | continuous |
| NULL 规则 | 任一字段缺失或 prev_age≤0 → MISSING → 省略 |
| 展示模板 | 当前段相对前段年龄比：{value:.4f} |
| 备注 | 仅作辅助参考；不进入产品摘要 |

#### V5_return_per_volume

| 字段 | 值 |
|------|-----|
| ID | V5_return_per_volume |
| 中文名 | 当前 Segment 收益率/成交量 |
| 维度 | volume |
| 层级 | auxiliary |
| 默认 UI 启用 | false |
| 真实路径 | `dsa_segment.current_segment_return_per_volume` |
| 公式 | 直接取值 |
| raw_type | continuous |
| NULL 规则 | 字段缺失 → MISSING → 省略 |
| 展示模板 | 当前 Segment 收益率/成交量：{value:.6f} |

#### V5_return_per_volume_ratio

| 字段 | 值 |
|------|-----|
| ID | V5_return_per_volume_ratio |
| 中文名 | 当前段相对前段收益率/成交量比 |
| 维度 | volume |
| 层级 | auxiliary |
| 默认 UI 启用 | false |
| 真实路径 | `dsa_segment.return_per_volume_ratio` |
| 公式 | 直接取值 |
| raw_type | continuous |
| NULL 规则 | 字段缺失 → MISSING → 省略 |
| 展示模板 | 收益率/量比：{value:.4f} |

### 3.5 Developing Swing 辅助定位

Developing Swing 具有独立信息，但固定为辅助层：

- 不定义趋势；
- 不触发趋势转向；
- 不进入右侧默认主状态；
- 可在"更多结构信息"中展示。

---

## 4. Rejected / UI 禁用

### 4.1 正式 Rejected 事实

#### V1_cumulative_volume_ratio

| 字段 | 值 |
|------|-----|
| ID | V1_cumulative_volume_ratio |
| 中文名 | 当前段相对前段累计成交量比 |
| 维度 | volume |
| 层级 | rejected |
| UI 启用 | **false（UI 永久禁用）** |
| 真实路径 | `dsa_segment.current_vs_prev_volume_ratio` |
| 公式 | `current_segment_volume_sum / previous_segment_volume_sum` |
| raw_type | continuous |
| NULL 规则 | — |
| 展示模板 | — |
| Rejected 原因 | **累计成交量比与 Segment 年龄比高度相关**（六日 Pearson=0.6405），主要受 Segment 长度影响，不能直接描述放量或缩量 |
| 处置 | 该值可继续保留在数据库或调试工具中，但不得进入列表右侧、摘要文本或用户状态卡 |

### 4.2 概念禁用（不在 FACT_RAW_DEPS 中但产品 UI 永久禁用）

- 综合趋势分数
- 综合动量分数
- 综合结构分数
- 综合成交量分数
- 趋势健康度
- 衰竭分数
- 反转概率
- 买点
- 卖点
- 加仓
- 减仓
- 持仓建议
- 新旧重复 Alignment 别名
- 累计成交量比（V1 同义概念）
- "趋势形成" / "趋势反转" 表述
- "成熟" / "衰竭" 表述
- "便宜" / "昂贵" 表述
- "止损" / "安全" 表述
- "突破方向" 解释
- "放量" / "缩量" 表述

---

## 5. 趋势效率工程欠账（T3/T6）

### 5.1 越界记录

六日数据共发现 **137 条** 当前 Segment 效率 > 1 的越界记录，前段效率越界 0 条。

### 5.2 生产代码定位

- **文件**：`backend/app/services/structural_factor_service.py`
- **函数**：`_compute_dsa_segment_factors`
- **当前段效率行**：约 858–864
- **前段效率行**：约 903–910

### 5.3 生产 Bug 1：`np.nansum` 跳过 NaN

```python
# 生产代码 (BUG)
diffs = np.abs(np.diff(seg_closes))
path_sum = float(np.nansum(diffs))  # 跳过 NaN，导致 path_sum 偏小
```

**影响**：当 close 路径中存在 NaN 时，`nansum` 跳过 NaN 使 path_sum 偏小，导致 `efficiency = net/path_sum > 1`。

### 5.4 生产 Bug 2：`net_move` 使用 DSA 线值而非收盘价

```python
# 生产代码 (BUG)
cur_start_price = float(cur_points[0]['value'])  # DSA 线值，非收盘价
net = abs(last_close - cur_start_price)  # 混用收盘价和 DSA 线值
```

**影响**：DSA 线值在 pivot 点可能不等于收盘价，导致 net_move 计算偏差。对于大多数记录（DSA 线值 = 收盘价），此 Bug 不显现。

### 5.5 Shadow 公式（独立实现）

```text
net_move = abs(close_end - close_start)
path_length = sum(abs(close[t] - close[t-1]))
efficiency = net_move / path_length
```

**规则**：
- close 序列必须来自同一 DSA Segment；
- 所有 Bar 必须有限（None/NaN → 返回 NULL）；
- 路径 ≥ 2 点；
- path_length > 0；
- **禁止 nansum，禁止 clip 到 1**。

### 5.6 V4.13 Shadow 验证结果

| 门禁 | 描述 | 结果 |
|------|------|------|
| Gate 1 | 所有有限 Shadow 效率 ∈ [0,1] | PASS (violations=0) |
| Gate 2 | 正常记录与生产值误差 ≤ 1e-9 | FAIL (match=40/30771) |
| Gate 3 | 越界记录变为合法值或 NULL | PASS (cur=137/137, prev=0/0) |
| Gate 4 | 逐日 computable coverage ≥ 95% | PASS (0.9734–0.9960) |
| Gate 5 | T6 正确传播 NULL | PASS |

**Gate 2 失败原因**：Bug 2（DSA 线值 vs 收盘价）影响大多数记录，不仅仅是 137 条越界记录，属于因子语义重定义而非局部补丁。

### 5.7 修复期间处置

在生产公式完成统一、回归和历史影响评估前：

- T3 保持 Auxiliary；
- T6 保持 Auxiliary；
- 默认右侧列表不得展示 T3/T6；
- T3/T6 必须有 feature flag，默认关闭；
- 不影响其余 14 项 Core 进入工程实现。

### 5.8 生产修复建议（不直接修改生产服务）

#### EFF-001：替换 `np.nansum` 为 `np.sum` 并增加有限性检查

```python
if not np.all(np.isfinite(seg_closes)):
    result['current_dsa_segment_efficiency_0_1'] = None
else:
    diffs = np.abs(np.diff(seg_closes))
    path_sum = float(np.sum(diffs))
    # ...
```

#### EFF-002：将 `cur_start_price` 改为收盘价

```python
close_start = float(closes[cur_start_bar_idx])
net = abs(last_close - close_start)
# 同理修改 prev_start_price 和 prev_end_price
```

---

## 6. V1 长度污染与 V3 平均量比结论

### 6.1 V1 长度污染

`V1_cumulative_volume_ratio = current_segment_volume_sum / previous_segment_volume_sum` 与 `Segment 年龄比 = cur_age / prev_age` 的六日 Pearson 相关系数为 **0.6405**，表明 V1 主要受 Segment 长度影响，无法直接描述放量或缩量。

### 6.2 V3 平均量比结论

`V3_avg_volume_ratio = (cur_vol_sum / cur_age) / (prev_vol_sum / prev_age)` 与年龄比的 Pearson 相关系数为 **-0.0149**，几乎无相关性，可独立描述 Segment 间平均每 Bar 量能关系。

**结论**：V3 进入 Core，V1 进入 Rejected。

---

## 7. main 列表视图右侧四组映射

### 7.1 默认顺序固定

```text
趋势
动量
结构
成交量
```

### 7.2 趋势组（默认 4 行）

| 顺序 | Fact | 展示 |
|------|------|------|
| 1 | T1_trend_direction | DSA 当前趋势方向为 {上行/下行/中性} |
| 2 | T2_aligned_slope | 方向对齐斜率为 {value:.4f} ATR/bar |
| 3 | T4_trend_age | 当前 Segment 已持续 {value} 根 bar |
| 4 | T5_slope_ratio | 斜率相对前段：{加速/减速/相近} |

### 7.3 动量组（默认 4 行）

| 顺序 | Fact | 展示 |
|------|------|------|
| 5 | M1_momentum_alignment | SQZMOM 动量与趋势 {同向/逆向/中性} |
| 6 | M2_aligned_momentum | 方向对齐动量值为 {value:.4f} |
| 7 | M3_aligned_momentum_delta | 最近一 Bar 对齐动量变化：{正/负/零}（raw={value:.6f}） |
| 8 | M5_squeeze_state | 波动率挤压状态：{挤压中/释放中/正常} |

### 7.4 结构组（默认 5 行）

| 顺序 | Fact | 展示 |
|------|------|------|
| 9 | S1_confirmed_boundary_relation | {顺 DSA 方向突破确认边界/价格在区间内/逆 DSA 方向边界破坏} |
| 10 | S2_active_dir_relation | Active Swing 方向与 DSA {一致/相反} |
| 11 | S3_active_position | 价格在 Active Swing 区间内位置：{偏低/中/高} |
| 12 | S7_dist_favorable_boundary | 距顺 DSA 方向确认边界：{尚未到达 X ATR / 已越过 X ATR} |
| 13 | S8_dist_adverse_boundary | 距逆 DSA 方向确认边界：{尚未到达 X ATR / 已越过 X ATR} |

### 7.5 成交量组（默认 1 行）

| 顺序 | Fact | 展示 |
|------|------|------|
| 14 | V3_avg_volume_ratio | Segment 均量与前段 {高/低/相近}（ratio={value:.4f}） |

---

## 8. 全局 UI 规则

### 8.1 缺失值处理

- 缺失事实直接 **省略该行**；
- 不显示 `MISSING`、`UNCALCULABLE`、`NOT_COMPUTABLE` 等内部枚举；
- 某一分组全部缺失时，**隐藏该分组** 或统一显示"数据不足"，两种方案只能选择一种；
- Auxiliary 默认不展示；
- 不展示任何概率、综合分数或操作建议；
- 右侧状态只描述当前客观事实；
- 顶部统一标注"以上为状态描述，不构成买卖建议"。

### 8.2 S7/S8 负距离处理

S7/S8 原始值为有符号 ATR 相对位置，**前台不得显示负距离**。统一转换为：

```text
尚未到达该边界，距离 X ATR     (原始值 d ≥ 0)
已越过该边界 X ATR             (原始值 d < 0)
```

### 8.3 禁用词列表

```text
买入、卖出、加仓、减仓、止损、安全、买点、卖点、持仓、
趋势形成、趋势反转、成熟、衰竭、便宜、昂贵、
突破方向、放量、缩量、累计成交量比
```

### 8.4 阈值集中配置

- 阈值不得散落在前后端；
- 所有阈值统一来自 `thresholds` 节点（见 `atomic_fact_contract_v1.json`）；
- 无法从 V4.12/V4.13 脚本中确认的阈值，标记 `engineering_confirmation_required=true`，工程化时根据 V4.12 q20/q80、q25/q75、q33/q67 候选确认。

---

## 9. 后续工程提取步骤

V4.13 完成后不再开展 V4.14 或新原子事实实验。后续仅允许进入工程阶段：

1. **修复生产效率计算**：实施 EFF-001 + EFF-002，覆盖 137 条越界记录和 1000+ 正常记录的回归测试；
2. **建立唯一 Canonical Fact Registry**：Fact ID、路径、公式、NULL 规则和文案模板集中配置；
3. **后端实现事实生成 API**：输出 Raw 值、标准枚举和 display payload；
4. **前端实现事实卡组件**：从 main 列表视图右侧默认状态区域入手，遵循四组映射；
5. **增加回归测试**：覆盖 137 条越界记录和 1000+ 正常记录，验证 T3/T6 NULL 传播；
6. **T3/T6 feature flag**：默认关闭，待生产修复后开启；
7. **V1 永久禁用**：不得出现在正式 API 的默认 UI payload；
8. **Developing 字段**：放入 `auxiliary` 节点，不在默认主状态展示；
9. **阈值单一配置源**：所有阈值来自 `thresholds` 节点；
10. **更新项目文档**：`AGENTS.md` + `docs/current/` + `docs/maps/` 添加 Atomic Fact Contract V1 条目；
11. **建立 Missing、零分母和有符号边界关系回归测试**。

---

## 10. 关闭声明

```text
原子事实研究阶段正式关闭。

后续工作全部转入工程实现、生产效率修复和回归测试。
不再开展 V4.14 或新的原子事实实验。

研究分支 experiment/hierarchical-scene-state-v3 仅保留：
- 研究脚本（V4.12 / V4.13）
- 最终研究契约（本文件 + atomic_fact_contract_v1.json）
- 上游 MD 报告（位于 research_outputs/，不入库）

不修改 main。
不创建 PR。
不部署。
```
