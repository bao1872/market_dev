# SMC 用户 Pine 源码逐项 Parity 文档

本文件逐项记录用户 Pine SMC 源码（`ref/smc_user_source.pine`）与生产 `backend/app/strategy_assets/algorithms/features/smc_pine_core.py` 的函数、状态、初始值、NA 规则、执行顺序、DTO 和测试映射。

## 参考来源（唯一真源）

| 来源 | 路径 | SHA256 | 行数 | 角色 |
|---|---|---|---|---|
| **用户 Pine 源码** | `ref/smc_user_source.pine` | `0bd3d2ad8819f2dc7a9399f0e869ca3c9eced8100f190aa131aac5fe8191988f` | 843 | **SMC 算法唯一真源**（用户原创，授权盘迹商业项目使用） |
| 同一份内容副本 | `ref/smc_ref.txt` | 同上 | 843 | 历史路径别名，内容与 `smc_user_source.pine` 完全相同 |
| 生产 Python 核心 | `backend/app/strategy_assets/algorithms/features/smc_pine_core.py` | — | 852 | 唯一 Pine 语义核心（生产+测试共用） |
| 生产薄包装 | `backend/app/strategy_assets/algorithms/features/smc_indicator.py` | — | — | 委托 `compute_smc_pine`，签名不变 |

**禁止**：读取或引用任何第三方 LuxAlgo Pine 源码；维护第二套近似算法；以"clean-room 翻译"为名绕过 parity 验证。

---

## 一、Pine 默认参数（严格匹配）

来源：`ref/smc_user_source.pine` lines 72-130 的 `input.*` 调用。

| Pine 输入 | 默认值 | Python `DEFAULT_PARAMS` 字段 | 说明 |
|---|---|---|---|
| `modeInput` | `'Historical'` | —（生产始终 Historical 语义） | 保留全部历史事件 |
| `styleInput` | `'Colored'` | —（前端配色） | 多头红空头绿 |
| `showTrendInput` | `false` | — | Color Candles 关闭 |
| `showInternalsInput` | `true` | —（始终启用 internal structure） | Internal 结构 |
| `showInternalBullInput` | `'All'` | — | 显示所有 BOS/CHoCH |
| `showInternalBearInput` | `'All'` | — | 同上 |
| `internalFilterConfluenceInput` | `false` | `internal_filter_confluence: False` | Confluence Filter 关闭 |
| `internalStructureSize` | `size.tiny` | —（前端 8px） | Internal 标签尺寸 |
| `showStructureInput` | `true` | —（始终启用 swing structure） | Swing 结构 |
| `showSwingBullInput` | `'All'` | — | 显示所有 BOS/CHoCH |
| `showSwingBearInput` | `'All'` | — | 同上 |
| `swingStructureSize` | `size.small` | —（前端 11px） | Swing 标签尺寸 |
| `showSwingsInput` | `false` | `show_swings: False` | Swing Points 标签关闭 |
| `swingsLengthInput` | `50` | `swings_length: 50` | Swing pivot 长度 |
| `showHighLowSwingsInput` | `true` | `show_high_low_swings: True` | Strong/Weak High/Low |
| `showInternalsInput` | `true` | `show_internals: True` | **[CHANGE-20260717-001]** Internal structure gate（Pine L76） |
| `showStructureInput` | `true` | `show_structure: True` | **[CHANGE-20260717-001]** Swing structure gate（Pine L84） |
| `showTrendInput` | `false` | `show_trend: True` | **[CHANGE-20260717-001]** Trend gate（Pine L74；Python 默认 True 以保持 gate 开启，Color Candles 仍由前端独立控制） |
| `showInternalOrderBlocksInput` | `true` | `show_internal_order_blocks: True` | Internal OB |
| `internalOrderBlocksSizeInput` | `5` | `internal_ob_size: 5` | 显示最近 5 个 |
| `showSwingOrderBlocksInput` | `false` | `show_swing_order_blocks: False` | Swing OB 关闭 |
| `swingOrderBlocksSizeInput` | `5` | `swing_ob_size: 5` | （关闭时不消费） |
| `orderBlockFilterInput` | `'Atr'` | `order_block_filter: 'Atr'` | ATR 过滤 |
| `orderBlockMitigationInput` | `'High/Low'` | `order_block_mitigation: 'High/Low'` | High/Low 穿越 |
| `showEqualHighsLowsInput` | `true` | `show_equal_hl: True` | EQH/EQL |
| `equalHighsLowsLengthInput` | `3` | `equal_length: 3` | 确认 bar 数 |
| `equalHighsLowsThresholdInput` | `0.1` | `equal_threshold: 0.1` | 阈值（× ATR200） |
| `equalHighsLowsSizeInput` | `size.tiny` | —（前端 8px） | EQH/EQL 标签尺寸 |
| `showFairValueGapsInput` | `false` | **完全排除**（不提供开关） | FVG 不计算/不返回/不缓存/不渲染 |
| `showDailyLevelsInput` | `false` | —（不实现） | MTF levels 关闭 |
| `showWeeklyLevelsInput` | `false` | —（不实现） | 同上 |
| `showMonthlyLevelsInput` | `false` | —（不实现） | 同上 |
| `showPremiumDiscountZonesInput` | `false` | —（不实现） | Premium/Discount 关闭 |

---

## 二、Pine 语义原语 → Python 实现

### 2.1 原语映射表

| Pine 函数/表达式 | Python 实现 | 默认 length | NA 初始行为 | 验证测试 |
|---|---|---|---|---|
| `ta.rma(src, length)` | `pine_rma(src, length)` | 200（ATR） | 前 length-1 根：逐步 SMA（min_periods）；第 length-1 根：完整 SMA 种子；之后：Wilder 递推 `(prev*(length-1)+src)/length` | `test_rma_wilder_recurrence`、`test_rma_min_periods` |
| `ta.tr` | `pine_true_range(highs, lows, closes)` | — | bar 0 = `highs[0]-lows[0]`（无前 close）；之后 `max(h-l, |h-prev_close|, |l-prev_close|)` | `test_atr_equals_rma_of_tr` |
| `ta.atr(n)` | `pine_atr(highs, lows, closes, n)` | 200 | = `pine_rma(pine_true_range, 200)` | `test_atr_equals_rma_of_tr` |
| `ta.cum(ta.tr) / bar_index` | `pine_cumulative_mean_range(highs, lows, closes)` | — | **bar 0 = NaN（除零）**；bar i (i>0) = `sum(tr[0..i]) / i` | `test_cmr_bar0_nan` |
| `ta.highest(src, length)` | `pine_highest(src, length, ref_i)` | 50/5/3 | 窗口 `[ref_i+1, ref_i+length]`，**不含 ref_i 本身** | `test_highest_excludes_ref` |
| `ta.lowest(src, length)` | `pine_lowest(src, length, ref_i)` | 50/5/3 | 同上 | `test_lowest_excludes_ref` |
| `ta.crossover(a, b)` | `pine_crossover(a_curr, a_prev, b_curr, b_prev)` | — | `a_curr > b_curr and a_prev <= b_prev` | `test_crossover` |
| `ta.crossunder(a, b)` | `pine_crossunder(a_curr, a_prev, b_curr, b_prev)` | — | `a_curr < b_curr and a_prev >= b_prev` | `test_crossunder` |
| `ta.change(x)` | 直接 `x[i] - x[i-1]` | — | i=0 时返回 na | `test_leg_change_semantics` |
| `nz(x, 0)` | `x if x == x else 0.0`（NaN 检查） | — | NaN → 0 | （隐式覆盖） |
| `math.max(a, b)` | `max(a, b)` | — | **Pine: 任一为 na → na**；Python: 需显式 NaN 检查 | （trailing 处理） |
| `math.min(a, b)` | `min(a, b)` | — | 同上 | 同上 |
| `math.abs(x)` | `abs(x)` | — | na → na | （隐式） |
| `math.round(x)` | `round(x)` | — | na → na | （标签位置） |
| `array.push(arr, 0, item)` | `arr.insert(0, item)` | — | 头部插入 | `test_ob_unshift_and_pop` |
| `array.pop(arr)` | `arr.pop()` | — | 删除尾部（最旧） | 同上 |
| `array.slice(arr, start, end)` | `arr[start:end]` | — | end-exclusive | `test_ob_slice_end_exclusive` |
| `array.indexof(arr, val)` | `arr.index(val)` | — | 返回首个匹配 | `test_ob_argmax_first_match` |
| `array.size(arr)` | `len(arr)` | — | — | — |

### 2.2 关键 Pine 语义说明

#### `ta.rma` vs SMA（CRITICAL）
Pine 的 `ta.rma(src, length)` 是 Wilder's Running Moving Average：
- 前 `length-1` 根：逐步 SMA（`min_periods` 行为，用可用数据计算）
- 第 `length-1` 根（index=length-1）：完整 SMA 作为种子
- 之后：递推 `rma[i] = (rma[i-1] * (length-1) + src[i]) / length`

**禁止**：用 `rolling().mean()`（SMA）代替 RMA。SMA 和 RMA 在稳定后数值趋近，但前 200-400 根差异显著，直接影响 `parsedHigh`/`parsedLow` 互换、OB 极值选择和 EQH/EQL 触发。

#### `ta.cum(ta.tr) / bar_index`（CMR）
Pine `bar_index` 从 0 开始：
- bar 0: `tr[0] / 0 = na`（除零）
- bar i (i>0): `sum(tr[0..i]) / i`

**禁止**：用 `arange(1, n+1)`（即 `i+1`）作为除数，这会让 bar 0 有值且所有 bar 除数比 Pine 大 1。

#### `ta.highest/lowest` 窗口
Pine `ta.highest(high, size)` 在 `leg(size)` 中调用时，`high[size]` 是 `i-size` 那根 bar 的 high，`ta.highest(high, size)` 是 `max(high[i-size+1..i])`（**不含** `high[size]` 本身）。

Python `pine_highest(src, length, ref_i)` 实现：窗口 `[ref_i+1, ref_i+length]` 即 `[i-size+1, i]`，与 Pine 一致。

---

## 三、Pine 状态变量 → Python 状态

### 3.1 UDT 映射

| Pine UDT (`type`) | Python `@dataclass` | 字段对应 | 初始值 |
|---|---|---|---|
| `pivot` | `_Pivot` | `currentLevel`→`current_level`, `lastLevel`→`last_level`, `crossed`→`crossed`, `barTime`→`bar_time`, `barIndex`→`bar_index` | `current_level=NaN`, `last_level=NaN`, `crossed=False`, `bar_time=None`, `bar_index=None` |
| `trend` | `_Trend` | `bias`→`bias` | `bias=0`（未定） |
| `trailingExtremes` | `_TrailingExtremes` | `top`→`top`, `bottom`→`bottom`, `barTime`→`bar_time`, `barIndex`→`bar_index`, `lastTopTime`→`last_top_time`, `lastBottomTime`→`last_bottom_time` | `top=NaN`, `bottom=NaN`, 其余 `None` |
| `orderBlock` | `_OrderBlock` | `barHigh`→`bar_high`, `barLow`→`bar_low`, `barTime`→`bar_time`, `bias`→`bias` | 创建时填入；额外：`bar_index`, `confirmed_index`, `confirmed_time`, `mitigated=False`, `mitigated_index=None`, `mitigated_time=None` |
| `alerts` | （不实现） | — | 生产不消费 alertcondition |
| `equalDisplay` | （不实现） | — | 前端直接渲染，无需 line/label 对象 |
| `fairValueGap` | **完全排除** | — | FVG 不计算、不返回、不缓存 |

### 3.2 状态变量映射

| Pine `var` 变量 | Python 字段 | 初始值 | 说明 |
|---|---|---|---|
| `var pivot swingHigh` | `self.swing_high: _Pivot` | currentLevel=na, lastLevel=na, crossed=false, barTime=time, barIndex=bar_index | swing 高点 pivot |
| `var pivot swingLow` | `self.swing_low: _Pivot` | 同上 | swing 低点 pivot |
| `var pivot internalHigh` | `self.internal_high: _Pivot` | 同上 | internal 高点 pivot |
| `var pivot internalLow` | `self.internal_low: _Pivot` | 同上 | internal 低点 pivot |
| `var pivot equalHigh` | `self.equal_high: _Pivot` | 同上 | EQH pivot |
| `var pivot equalLow` | `self.equal_low: _Pivot` | 同上 | EQL pivot |
| `var trend swingTrend` | `self.swing_trend: _Trend` | bias=0 | swing 趋势（BULLISH=1/BEARISH=-1/0=未定） |
| `var trend internalTrend` | `self.internal_trend: _Trend` | bias=0 | internal 趋势 |
| `var trailingExtremes trailing` | `self.trailing: _TrailingExtremes` | top=na, bottom=na | Strong/Weak High/Low 极值 |
| `var array<orderBlock> swingOrderBlocks` | `self.swing_order_blocks: list[_OrderBlock]` | `[]` | swing OB 列表（默认不显示） |
| `var array<orderBlock> internalOrderBlocks` | `self.internal_order_blocks: list[_OrderBlock]` | `[]` | internal OB 列表（默认显示最近 5 个） |
| `var array<float> parsedHighs` | `self.parsed_highs: list[float]` | — | 高波动 bar 互换后的 highs |
| `var array<float> parsedLows` | `self.parsed_lows: list[float]` | — | 高波动 bar 互换后的 lows |
| `var array<float> highs` | `self.highs: list[float]` | — | 原始 highs（leg 检测用） |
| `var array<float> lows` | `self.lows: list[float]` | — | 原始 lows |
| `var array<int> times` | `self.times: list[str]` | — | bar 时间序列 |
| `var array<fairValueGap> fairValueGaps` | **完全排除** | — | FVG 不实现 |
| `var array<box> swingOrderBlocksBoxes` | （不实现） | — | 前端 canvas 渲染，无需 box 对象 |
| `var array<box> internalOrderBlocksBoxes` | （不实现） | — | 同上 |
| `varip int currentBarIndex` | （隐式：循环变量 `i`） | — | Python 用 for 循环，无需显式跟踪 |
| `varip int lastBarIndex` | （隐式） | — | 同上 |
| `alerts currentAlerts` | （不实现） | — | 生产不消费 alertcondition |
| `var initialTime` | （不实现） | — | 仅用于 MTF levels，已排除 |

---

## 四、Pine 函数 → Python 方法映射

### 4.1 函数映射表

| Pine 函数（行号） | Python 方法 | 调用时机 | 说明 |
|---|---|---|---|
| `leg(int size)` (L333-342) | `_SMCPineState.leg(i, size, lane)` | 每 bar | leg 检测：`newLegHigh = high[size] > highest(high, size)` 等 |
| `startOfNewLeg(int leg)` (L347) | `start_of_new_leg(i, size, lane)` | 每 bar | `ta.change(leg) != 0` |
| `startOfBearishLeg(int leg)` (L352) | `start_of_bearish_leg(i, size, lane)` | 每 bar | `ta.change(leg) == -1` |
| `startOfBullishLeg(int leg)` (L357) | `start_of_bullish_leg(i, size, lane)` | 每 bar | `ta.change(leg) == +1` |
| `drawLabel(...)` (L366-372) | （不实现） | — | 前端 canvas 渲染，无需 label 对象 |
| `drawEqualHighLow(...)` (L380-398) | （不实现，直接 append 到 `equal_highs_lows`） | EQH/EQL 检测时 | 前端从 DTO 渲染 |
| `getCurrentStructure(size, equalHighLow, internal)` (L405-453) | `get_current_structure(i, size, equal_high_low, internal)` | 每 bar（3 次：swing/internal/equal） | pivot 检测 + EQH/EQL 生成 |
| `drawStructure(...)` (L463-472) | （不实现，直接 append 到 `events`） | BOS/CHoCH 检测时 | 前端从 DTO 渲染 |
| `deleteOrderBlocks(bool internal)` (L477-496) | `delete_order_blocks(i, internal)` | 每 bar（internal + swing） | OB mitigation |
| `storeOrdeBlock(pivot, internal, bias)` (L503-521) | `store_order_block(piv, current_i, internal, bias)` | BOS/CHoCH 检测时 | OB 创建 |
| `drawOrderBlocks(bool internal)` (L526-542) | （不实现，输出时限制最近 N 个） | Pine 仅最后一根 bar | Python 在 DTO 输出时由前端限制显示数量 |
| `displayStructure(bool internal)` (L547-608) | `display_structure(i, internal)` | 每 bar（internal + swing） | BOS/CHoCH 检测 + OB 创建 |
| `fairValueGapBox(...)` (L617) | **完全排除** | — | FVG 不实现 |
| `deleteFairValueGaps()` (L621-626) | **完全排除** | — | FVG 不实现 |
| `drawFairValueGaps()` (L630-645) | **完全排除** | — | FVG 不实现 |
| `getStyle(string style)` (L650-654) | （不实现） | — | 前端处理线型 |
| `drawLevels(...)` (L662-696) | （不实现） | — | MTF levels 默认关闭 |
| `higherTimeframe(string timeframe)` (L701) | （不实现） | — | MTF levels 默认关闭 |
| `updateTrailingExtremes()` (L705-709) | `update_trailing_extremes(i)` | 每 bar（trailing 初始化后） | Strong/Weak High/Low 极值更新 |
| `drawHighLowSwings()` (L713-729) | （不实现，输出 trailing dict） | Pine 每根 bar | 前端从 DTO 渲染 |
| `drawZone(...)` (L740-747) | （不实现） | — | Premium/Discount 默认关闭 |
| `drawPremiumDiscountZones()` (L751-757) | （不实现） | — | Premium/Discount 默认关闭 |

### 4.2 `getCurrentStructure` 详细映射

Pine lines 405-453：

```pine
getCurrentStructure(int size, bool equalHighLow = false, bool internal = false) =>
    currentLeg = leg(size)
    newPivot = startOfNewLeg(currentLeg)
    pivotLow = startOfBullishLeg(currentLeg)
    pivotHigh = startOfBearishLeg(currentLeg)

    if newPivot
        if pivotLow
            pivot p_ivot = equalHighLow ? equalLow : internal ? internalLow : swingLow
            if equalHighLow and math.abs(p_ivot.currentLevel - low[size]) < equalHighsLowsThresholdInput * atrMeasure
                drawEqualHighLow(p_ivot, low[size], size, false)
                currentAlerts.equalLows := true
            p_ivot.lastLevel := p_ivot.currentLevel
            p_ivot.currentLevel := low[size]
            p_ivot.crossed := false
            p_ivot.barTime := time[size]
            p_ivot.barIndex := bar_index[size]
            if not equalHighLow and not internal
                trailing.bottom := p_ivot.currentLevel
                trailing.barTime := p_ivot.barTime
                trailing.barIndex := p_ivot.barIndex
                trailing.lastBottomTime := p_ivot.barTime
            if showSwingsInput and not internal and not equalHighLow
                drawLabel(time[size], p_ivot.currentLevel, p_ivot.currentLevel < p_ivot.lastLevel ? 'LL' : 'HL', swingBullishColor, label.style_label_up)
        else
            pivot p_ivot = equalHighLow ? equalHigh : internal ? internalHigh : swingHigh
            if equalHighLow and math.abs(p_ivot.currentLevel - high[size]) < equalHighsLowsThresholdInput * atrMeasure
                drawEqualHighLow(p_ivot, high[size], size, true)
                currentAlerts.equalHighs := true
            p_ivot.lastLevel := p_ivot.currentLevel
            p_ivot.currentLevel := high[size]
            p_ivot.crossed := false
            p_ivot.barTime := time[size]
            p_ivot.barIndex := bar_index[size]
            if not equalHighLow and not internal
                trailing.top := p_ivot.currentLevel
                trailing.barTime := p_ivot.barTime
                trailing.barIndex := p_ivot.barIndex
                trailing.lastTopTime := p_ivot.barTime
            if showSwingsInput and not internal and not equalHighLow
                drawLabel(time[size], p_ivot.currentLevel, p_ivot.currentLevel > p_ivot.lastLevel ? 'HH' : 'LH', swingBearishColor, label.style_label_down)
```

Python 实现（`smc_pine_core.py:384-470`）等价映射：
- `low[size]` → `self.lows[i-size]`（即 `self.lows[ref_i]`）
- `high[size]` → `self.highs[ref_i]`
- `time[size]` → `self.times[ref_i]`
- `bar_index[size]` → `ref_i`（Python 用数组索引，Pine 用 bar_index）
- `atrMeasure` → `self.atr200[i]`
- EQH/EQL 阈值：`abs(piv.current_level - level) < threshold * atr_measure`（使用 `<`，非 `<=`）
- pivot 更新顺序：`lastLevel = currentLevel` → `currentLevel = new level` → `crossed = false` → `barTime/barIndex = ref_i`
- trailing 更新条件：`not equal_high_low and not internal`（仅 swing pivot 更新 trailing）

### 4.3 `displayStructure` 详细映射

Pine lines 547-608：

```pine
displayStructure(bool internal = false) =>
    var bullishBar = true
    var bearishBar = true
    if internalFilterConfluenceInput
        bullishBar := high - math.max(close, open) > math.min(close, open - low)
        bearishBar := high - math.max(close, open) < math.min(close, open - low)
    pivot p_ivot = internal ? internalHigh : swingHigh
    trend t_rend = internal ? internalTrend : swingTrend
    lineStyle = internal ? line.style_dashed : line.style_solid
    labelSize = internal ? internalStructureSize : swingStructureSize
    extraCondition = internal ? internalHigh.currentLevel != swingHigh.currentLevel and bullishBar : true
    bullishColor = styleInput == MONOCHROME ? MONO_BULLISH : internal ? internalBullColorInput : swingBullColorInput

    if ta.crossover(close, p_ivot.currentLevel) and not p_ivot.crossed and extraCondition
        string tag = t_rend.bias == BEARISH ? CHOCH : BOS  // 先用旧 bias 判定
        // ... alerts ...
        p_ivot.crossed := true  // 再设 crossed
        t_rend.bias := BULLISH  // 最后更新 bias
        // ... drawStructure if displayCondition ...
        if (internal and showInternalOrderBlocksInput) or (not internal and showSwingOrderBlocksInput)
            storeOrdeBlock(p_ivot, internal, BULLISH)

    p_ivot := internal ? internalLow : swingLow
    extraCondition := internal ? internalLow.currentLevel != swingLow.currentLevel and bearishBar : true
    bearishColor = ...
    if ta.crossunder(close, p_ivot.currentLevel) and not p_ivot.crossed and extraCondition
        string tag = t_rend.bias == BULLISH ? CHOCH : BOS  // 先用旧 bias 判定
        // ... alerts ...
        p_ivot.crossed := true  // 再设 crossed
        t_rend.bias := BEARISH  // 最后更新 bias
        // ... drawStructure if displayCondition ...
        if (internal and showInternalOrderBlocksInput) or (not internal and showSwingOrderBlocksInput)
            storeOrdeBlock(p_ivot, internal, BEARISH)
```

**关键顺序**（Pine parity 必须遵守）：
1. **先用旧 `t_rend.bias` 判定** tag（CHOCH 或 BOS）
2. **再设 `p_ivot.crossed = true`**
3. **最后更新 `t_rend.bias`**（BULLISH 或 BEARISH）

Python `display_structure` (`smc_pine_core.py:518-602`) 已正确实现此顺序。

### 4.4 `storeOrdeBlock` 详细映射

Pine lines 503-521：

```pine
storeOrdeBlock(pivot p_ivot, bool internal = false, int bias) =>
    if (not internal and showSwingOrderBlocksInput) or (internal and showInternalOrderBlocksInput)
        array<float> a_rray = na
        int parsedIndex = na
        if bias == BEARISH
            a_rray := parsedHighs.slice(p_ivot.barIndex, bar_index)  // [start, end)
            parsedIndex := p_ivot.barIndex + a_rray.indexof(a_rray.max())  // 首个最大值
        else
            a_rray := parsedLows.slice(p_ivot.barIndex, bar_index)
            parsedIndex := p_ivot.barIndex + a_rray.indexof(a_rray.min())  // 首个最小值
        orderBlock o_rderBlock = orderBlock.new(parsedHighs.get(parsedIndex), parsedLows.get(parsedIndex), times.get(parsedIndex), bias)
        array<orderBlock> orderBlocks = internal ? internalOrderBlocks : swingOrderBlocks
        if orderBlocks.size() >= 100
            orderBlocks.pop()  // 删除尾部（最旧）
        orderBlocks.unshift(o_rderBlock)  // 头部插入（最新在最前）
```

Python `store_order_block` (`smc_pine_core.py:605-668`) 等价映射：
- `parsedHighs.slice(start, end)` → `self.parsed_highs[start:end]`（end-exclusive）
- `a_rray.max()` → `max(arr)`
- `a_rray.indexof(max)` → `arr.index(max(arr))`（**首个匹配**）
- `array.pop()` → `list.pop()`（删除尾部）
- `array.unshift(item)` → `list.insert(0, item)`（头部插入）

**OB 创建参与当前 bar mitigation**：Python `store_order_block` 在 `display_structure` 中调用，`delete_order_blocks` 在之后调用，因此新建 OB 会被当前 bar 的 mitigation 检查（但通常不会立即 mitigated，因为 OB 创建 bar 的 high/low 就是 OB 自身的 bar_high/bar_low）。

### 4.5 `deleteOrderBlocks` 详细映射

Pine lines 477-496：

```pine
deleteOrderBlocks(bool internal = false) =>
    array<orderBlock> orderBlocks = internal ? internalOrderBlocks : swingOrderBlocks
    for [index, eachOrderBlock] in orderBlocks
        bool crossedOderBlock = false
        if bearishOrderBlockMitigationSource > eachOrderBlock.barHigh and eachOrderBlock.bias == BEARISH
            crossedOderBlock := true
        else if bullishOrderBlockMitigationSource < eachOrderBlock.barLow and eachOrderBlock.bias == BULLISH
            crossedOderBlock := true
        if crossedOderBlock
            orderBlocks.remove(index)  // Pine 数组 remove by index
```

其中：
- `bearishOrderBlockMitigationSource = orderBlockMitigationInput == CLOSE ? close : high`（默认 High/Low → `high`）
- `bullishOrderBlockMitigationSource = orderBlockMitigationInput == CLOSE ? close : low`（默认 High/Low → `low`）

Python `delete_order_blocks` (`smc_pine_core.py:670-707`) 等价映射：
- mitigation_src_high = `closes[i]` if CLOSE else `highs[i]`
- mitigation_src_low = `closes[i]` if CLOSE else `lows[i]`
- BEARISH OB mitigated if `mitigation_src_high > ob.bar_high`
- BULLISH OB mitigated if `mitigation_src_low < ob.bar_low`
- mitigated OB 标记 `mitigated=True`、`mitigated_index=i`、`mitigated_time=times[i]`，并从列表移除

### 4.6 `updateTrailingExtremes` 详细映射

Pine lines 705-709：

```pine
updateTrailingExtremes() =>
    trailing.top := math.max(high, trailing.top)  // Pine: na 传播
    trailing.lastTopTime := trailing.top == high ? time : trailing.lastTopTime
    trailing.bottom := math.min(low, trailing.bottom)  // Pine: na 传播
    trailing.lastBottomTime := trailing.bottom == low ? time : trailing.lastBottomTime
```

Python `update_trailing_extremes` (`smc_pine_core.py:711-725`)：
- 若 `trailing.top` 为 NaN 或 `highs[i] >= trailing.top`：`trailing.top = highs[i]`
- `lastTopTime` 在 top 更新时同步
- 同理 bottom

**Pine na 传播**：`math.max(high, na) = na`，因此 Pine 在 trailing 未初始化时（top=na），`updateTrailingExtremes` 不会改变 top。Python 用显式 NaN 检查模拟此行为，且额外用 `trailing.bar_index is not None` 守卫（仅 swing pivot 首次出现后才更新）。

---

## 五、执行顺序（Pine 逐 bar 主循环）

### 5.1 Pine 主循环（lines 760-807）

```pine
// 每 bar 执行：
parsedOpen = showTrendInput ? open : na
candleColor = internalTrend.bias == BULLISH ? swingBullishColor : swingBearishColor
plotcandle(parsedOpen, high, low, close, color=candleColor, ...)

if showHighLowSwingsInput or showPremiumDiscountZonesInput
    updateTrailingExtremes()                    // 1. trailing 极值更新（FIRST）
    if showHighLowSwingsInput
        drawHighLowSwings()
    if showPremiumDiscountZonesInput
        drawPremiumDiscountZones()

if showFairValueGapsInput                        // FVG 排除，不执行
    deleteFairValueGaps()

getCurrentStructure(swingsLengthInput, false)    // 2. swing pivot (size=50)
getCurrentStructure(5, false, true)              // 3. internal pivot (size=5)

if showEqualHighsLowsInput
    getCurrentStructure(equalHighsLowsLengthInput, true)  // 4. EQH/EQL (size=3)

if showInternalsInput or showInternalOrderBlocksInput or showTrendInput
    displayStructure(true)                       // 5. internal BOS/CHoCH + OB 创建

if showStructureInput or showSwingOrderBlocksInput or showHighLowSwingsInput
    displayStructure()                           // 6. swing BOS/CHoCH + OB 创建

if showInternalOrderBlocksInput
    deleteOrderBlocks(true)                      // 7. internal OB mitigation

if showSwingOrderBlocksInput
    deleteOrderBlocks()                          // 8. swing OB mitigation

if showFairValueGapsInput                        // FVG 排除，不执行
    drawFairValueGaps()

if barstate.islastconfirmedhistory or barstate.islast
    if showInternalOrderBlocksInput
        drawOrderBlocks(true)                    // 9. 仅最后一根 bar：绘制 internal OB
    if showSwingOrderBlocksInput
        drawOrderBlocks()                        // 10. 仅最后一根 bar：绘制 swing OB

lastBarIndex := currentBarIndex                  // 11. 更新 barIndex
currentBarIndex := bar_index
```

### 5.2 Python `run()` 实现（`smc_pine_core.py:727-769`）

```python
for i in range(self.n):
    # 1. swing pivot
    self.get_current_structure(i, swings_length, False, False)
    # 2. internal pivot (size=5)
    self.get_current_structure(i, 5, False, True)
    # 3. equal H/L pivot
    if show_equal_hl:
        self.get_current_structure(i, equal_length, True, False)

    # 4. internal BOS/CHoCH
    if show_internal_order_blocks:
        self.display_structure(i, True)
    # 5. swing BOS/CHoCH
    self.display_structure(i, False)

    # 6. trailing
    if self.trailing.bar_index is not None:
        self.update_trailing_extremes(i)

    # 7. internal OB mitigation
    if show_internal_order_blocks:
        self.delete_order_blocks(i, True)
    # 8. swing OB mitigation
    if show_swing_order_blocks:
        self.delete_order_blocks(i, False)
```

### 5.3 Parity Divergence — `updateTrailingExtremes` 顺序（已修复）

**Pine 顺序**：`updateTrailingExtremes` 在 **最前面**（step 1），在任何 `getCurrentStructure` 之前。
**Python 旧顺序**：`update_trailing_extremes` 在 `display_structure` 之后（step 6）。

**影响**：
- 在 Pine 中，bar i 的 trailing.top 先用 `max(high[i], 旧 trailing.top)` 更新，然后如果 `getCurrentStructure` 检测到新 swing pivot，会**覆盖** trailing.top 为新 pivot level（即 `high[i-50]`）。
- 在 Python 旧顺序中，如果 bar i 检测到新 swing pivot，trailing.top 先被设为新 pivot level，然后 `update_trailing_extremes` 会用 `max(high[i], 新 pivot level)` 更新——若 `high[i] > high[i-50]`，Python 给出 `high[i]`，Pine 给出 `high[i-50]`。

**修复**：将 `update_trailing_extremes` 移到循环最前面（step 1），与 Pine 一致。详见 CHANGE-20260715-003。

### 5.4 Parity Divergence — `displayStructure(internal)` 条件（**CHANGE-20260717-001 已修复**）

**Pine 条件**（L784）：`if showInternalsInput or showInternalOrderBlocksInput or showTrendInput`
**Python 旧条件**：`if show_internal_order_blocks`（仅由 OB 开关门控）

**修复**（CHANGE-20260717-001）：新增 `show_internals`/`show_trend` 参数（默认 True），Python 条件改为 `if internal_gate:` 其中 `internal_gate = show_internals or show_internal_order_blocks or show_trend`，严格复刻 Pine L784。

### 5.5 Parity Divergence — `displayStructure(swing)` 条件（**CHANGE-20260717-001 已修复**）

**Pine 条件**（L787）：`if showStructureInput or showSwingOrderBlocksInput or showHighLowSwingsInput`
**Python 旧条件**：`（始终执行）`（无门控）

**修复**（CHANGE-20260717-001）：新增 `show_structure` 参数（默认 True），Python 条件改为 `if swing_gate:` 其中 `swing_gate = show_structure or show_swing_order_blocks or show_high_low_swings`，严格复刻 Pine L787。

---

## 六、anchor/confirmed 因果契约

每个事件/PB 同时返回 `anchor_index`/`anchor_time` 与 `confirmed_index`/`confirmed_time`。

| 事件类型 | anchor | confirmed | 不可变契约 |
|---|---|---|---|
| pivot | `ref_i` (i-size) | `i` (leg change 确认 bar) | pivot 写入后 currentLevel/lastLevel/crossed 可更新，但 pivot 事件记录不可变 |
| BOS | `pivot.barIndex` (被穿越的 pivot bar) | `i` (close 穿越 pivot 的 bar) | 事件一旦写入不可变 |
| CHoCH | `pivot.barIndex` (被穿越的 pivot bar) | `i` (close 穿越 pivot 的 bar) | 同上 |
| Order Block | `parsed_index` (OB bar) | `current_i` (触发 OB 创建的 BOS/CHoCH bar) | OB 创建后 top/bottom/bias 不可变 |
| EQH/EQL | `prev piv.barIndex` (前一 pivot) | `i-size` (新 pivot bar) | 同上 |
| Mitigation | OB.anchor | `i` (close/high/low 穿越 OB 的 bar) | mitigated=True 后不可逆 |

**API 事件时间使用 confirmed**；**可视化从 anchor 画到 confirmed**；**未来 bar 不得修改已确认事件**（事件一旦写入即不可变）。

---

## 七、输出 DTO

`compute_smc_pine()` 返回 `dict[str, Any]`，包含以下字段：

| 输出字段 | 类型 | 来源 | 说明 |
|---|---|---|---|
| `events` | `list[dict]` | BOS/CHoCH | 每个事件含 `type`/`anchor_index`/`anchor_time`/`confirmed_index`/`confirmed_time`/`level`/`bias`/`internal` |
| `order_blocks` | `list[dict]` | OB 列表 | 每个 OB 含 `bias`/`anchor_index`/`anchor_time`/`confirmed_index`/`confirmed_time`/`bar_high`/`bar_low`/`mitigated`/`mitigated_index`/`mitigated_time`/`internal`；**[CHANGE-20260717-001] 顺序为 newest-first**（`insert(0, ...)`，与 Pine `array.unshift` 一致），前端 `slice(0,5)` 取最新 5 个 active internal OB |
| `equal_highs_lows` | `list[dict]` | EQH/EQL | 每个含 `type`/`anchor_index`/`anchor_time`/`confirmed_index`/`confirmed_time`/`level`/`prev_level`（**[CHANGE-20260717-001]** `type` 直接为 `"EQH"`/`"EQL"`，前端两端点线用 `prev_level`→`level`） |
| `pivots` | `list[dict]` | pivot 记录 | 每个含 `type`/`level`/`bar_index`/`bar_time`/`internal` |
| `trailing` | `dict` | Strong/Weak | `top`/`bottom`/`bar_time`/`bar_index`/`last_top_time`/`last_bottom_time`（**[CHANGE-20260717-001]** `last_top_time`/`last_bottom_time` 为前端 Strong/Weak 线起点；`top`/`bottom` 在首个 swing pivot 前为 NaN，严格复刻 Pine `math.max(high, na)=na`） |
| `time` | `list[str]` | 完整时间序列 | 与输入 bars 等长（**不截断**），用于 anchor/confirmed 索引对齐 |
| `params` | `dict` | DEFAULT_PARAMS | 实际使用的参数快照 |
| **FVG** | **不存在** | — | **完全排除**，输出中无 `fvg`/`fair_value_gap` 字段 |

**SMC 输出不截断**：`_truncate_lists` 不调用 SMC 输出。前端 `smcToDisplay` 通过时间匹配自动过滤展示区外事件。

---

## 八、warmup 契约（**CHANGE-20260717-001 计算历史与展示窗口分离**）

Pine 使用全历史计算 SMC；项目必须分离**计算历史**与**展示窗口**：SMC 计算完整历史，view adapter 裁成展示窗口 DTO。

| 周期 | SMC 计算输入 | 计算根数 | 展示根数 | 说明 |
|---|---|---|---|---|
| 1d | `full_daily_bars`（DB 全量日线） | 完整历史 | 4000 | ≥500 warmup；在 `daily_bars.tail(daily_count)` 截断前保存 |
| 15m | 独立查询 `bars + _SMC_WARMUP_BARS` | 5000 | 4000 | **[CHANGE-20260717-001]** 独立查询 5000 根（4000 展示 + 1000 warmup），计算后 adapter 裁成 4000；前复权与主 15m 路径一致；查询不足回退 `macd_bars` |
| 1h | `macd_bars` | 完整历史 | 4000 | 可获得完整历史 |
| 1w | `macd_bars` | 完整历史 | 4000 | 同上 |
| 1mo | `macd_bars` 或扩展回看 | ≥200 | 4000 | **[CHANGE-20260717-001]** 若 `len(macd_bars) < _SMC_MONTHLY_MIN_BARS(200)` 则扩展回看到 `_SMC_MONTHLY_LOOKBACK_DAYS(7000)` 天（≈233 月），确保 ATR200 可初始化；EQH/EQL 阈值（× ATR200）不再全 NaN |

**常量**（`indicator_service.py` L89-94）：
- `_SMC_WARMUP_BARS = 1000`（15m 专用 warmup）
- `_SMC_MONTHLY_MIN_BARS = 200`（1mo 最少 bar 数）
- `_SMC_MONTHLY_LOOKBACK_DAYS = 7000`（1mo 扩展回看）

**禁止**：
- 只用当前可见 bars 初始化状态
- 用展示区可见 bars 初始化 SMC 状态机
- 调用 `_truncate_lists` 截断 SMC 输出
- 15m 只计算 4000 根（无 warmup）— 窗口左缘 pivot/BOS/CHoCH 会丢失
- 1mo 不足 200 根 — ATR200 无法初始化，EQH/EQL 阈值全 NaN

---

## 九、缓存隔离

| 项目 | 值 | 说明 |
|---|---|---|
| `indicator_cache.ALGORITHM_VERSION` | `v10` | **[CHANGE-20260717-001]** 从 v9 bump；SMC warmup/gate/trailing/OB 逻辑变更，旧 v9 缓存强制失效 |
| `:smc` 后缀 | `include_smc=True` 时缓存键追加 | SMC 与非 SMC 结果独立缓存 |
| 默认缓存键 | 不带 `:smc` 后缀 | `include_smc=False`（默认）时使用 |
| Redis 操作 | 仅允许精确 DEL 测试键 | **禁止** `FLUSHDB`/`FLUSHALL` |

---

## 十、API 契约

| 端点 | 参数 | 默认 | 响应 |
|---|---|---|---|
| `GET /api/v1/instruments/{id}/indicators` | `include_smc` (bool) | `false` | 缺省/false 时响应无 smc layer 且不计算；true 时返回 smc layer（含 renderer/direction_colored/颜色/data） |

**SMC 默认关闭**：
- `include_smc=False` 时核心函数调用次数为 0
- `/market` 右栏小 K 线（`MiniKlineCard`）不请求 SMC
- SMC 只进入 `/stock` 指标链

---

## 十一、前端渲染契约

### 11.1 SMC Canvas 渲染（`StrategyChart`）

| 元素 | internal | swing | 说明 |
|---|---|---|---|
| 线型 | 虚线 `[4, 3]` | 实线 | Pine: `line.style_dashed` vs `line.style_solid` |
| 标签尺寸 | tiny（8px） | small（11px） | Pine: `size.tiny` vs `size.small` |
| 标签位置 | pivot 与 confirmed 中点 | 同上 | `math.round(0.5*(pivot.barIndex+confirmed_index))` |
| 颜色（多头） | `#FF4D4F` | `#FF4D4F` | A 股红涨 |
| 颜色（空头） | `#22C55E` | `#22C55E` | A 股绿跌 |
| OB box alpha | active 0.12 | active 0.12 | 半透明 |
| OB box alpha（mitigated） | 0.05 | 0.05 | 更低透明度 |
| **EQH/EQL 线** | — | 两端点 `prev_level`→`level` | **[CHANGE-20260717-001]** Pine L396 两端点线（可能不水平）；EQH=`SMC_BEAR_COLOR` 绿 + label_down，EQL=`SMC_BULL_COLOR` 红 + label_up；标签位于两 pivot 中点（Pine L397） |
| **Strong/Weak 线起点** | — | `trailing.last_top_time`/`last_bottom_time` | **[CHANGE-20260717-001]** Pine L721-727 线起点为 trailing 时间（非最后可见 bar）；新增 `timeToDisplayIdx` 辅助（ISO 时间→display index，缺失/找不到 clamp 到窗口左端）；终点延伸到 `plotRight`（约 20 bar）；颜色按 strong/weak 区分（**有意视觉差异**，Pine 用固定 bearish/bullish 色） |

### 11.2 Historical 模式

- 绘制全部事件（**不因标签碰撞删除**，只允许调整标签偏移）
- internal OB 默认显示最近 5 个有效区域（`internalOrderBlocksSizeInput=5`）
- OB 从创建 bar（anchor）延伸到 mitigation 或当前最右端

### 11.3 viewport 同步

- 拖拽/缩放/复位/周期切换后，所有 SMC 线/标签/OB 与 K 线共用相同 viewport 映射
- 使用 Pointer Events（`pointerdown`/`pointermove`/`pointerup`/`pointercancel`）+ `setPointerCapture`

### 11.4 FVG 不存在

- FVG 类型、DOM、box 和开关**不存在**
- 前端不得渲染 FVG 相关元素
- `CHART_LAYER_MANIFEST` 8 条目（trend/node/boll/volume/macd/sqzmom/breakout/smc），无 fvg

---

## 十二、测试覆盖

### 12.1 Pine 语义原语测试（`TestPineSemantics`）

| 测试 | 验证内容 | 状态 |
|---|---|---|
| `test_rma_wilder_recurrence` | Wilder 递推公式 `(prev*(length-1)+src)/length` | PASS |
| `test_rma_min_periods` | 前 length-1 根逐步 SMA | PASS |
| `test_cmr_bar0_nan` | bar 0 = NaN（除零） | PASS |
| `test_atr_equals_rma_of_tr` | `ta.atr = ta.rma(ta.tr)` | PASS |
| `test_crossover` | `a_curr > b_curr and a_prev <= b_prev` | PASS |
| `test_crossunder` | `a_curr < b_curr and a_prev >= b_prev` | PASS |
| `test_highest_excludes_ref` | 窗口不含 ref_i 本身 | PASS |
| `test_lowest_excludes_ref` | 同上 | PASS |

### 12.2 FVG 排除测试（输出级断言）

| 测试 | 验证内容 | 状态 |
|---|---|---|
| `test_output_keys_no_fvg` | result keys 不含 `fvg`/`fair_value_gap` | PASS |
| `test_events_no_fvg` | events 列表无 FVG 类型 | PASS |
| `test_order_blocks_no_fvg` | order_blocks 无 FVG 字段 | PASS |
| `test_equal_highs_lows_no_fvg` | equal_highs_lows 无 FVG | PASS |
| `test_params_no_fvg` | params 无 FVG 开关 | PASS |
| `test_state_no_fvg` | state 无 FVG 状态 | PASS |

**FVG 验收方式**：输出级别断言（检查 result keys/events/order_blocks/equal_highs_lows/params/state 不含 FVG），**不是源码字符串扫描**。

### 12.3 Pine Golden Fixture（PENDING — **PINE_PARITY_PENDING**）

| fixture | 状态 | 说明 |
|---|---|---|
| Pine golden CSV | **PENDING** | 等待用户从 TradingView 导出事件/OB CSV（使用 `ref/smc_user_export.pine` 的 26 个隐藏 plot） |
| `backend/tests/fixtures/smc_pine/README.md` | 已创建 | TV 导出步骤、隐藏 plot 代码、CSV 格式规范 |
| `TestPineGoldenFixture` / `test_smc_tv_parity.py` | skip（无 fixture 时） | 无 fixture 时跳过（`PINE_PARITY_PENDING`），不得宣称"完全对齐" |
| 美诺华 603538 日线 1000 根 | 待用户提供 | 用于 golden 测试 |
| 15m 样本 | 待用户提供 | 用于 golden 测试 |

**PINE_PARITY_PENDING**：当前无 Pine 导出的 golden CSV，无法进行输出级完全一致断言。**[CHANGE-20260717-001]** golden 测试本身已修复（EQH 类型直接用 core 输出不误映射、日内时间戳用 `isoformat()` 不压缩、容差 0 严格逐 bar、新增 OB/EQ 端点/全链 3 个测试），但无真实 TV fixture 时仍 skip。代码级修复通过，输出级 parity pending。不伪造 fixture，不声称"完全对齐"。

### 12.4 SMC 集成测试

| 测试 | 验证内容 | 状态 |
|---|---|---|
| `test_smc_default_off_zero_computation` | `include_smc=False` 时核心函数调用 0 次 | PASS |
| `test_smc_cache_isolation` | true/false 缓存键独立，切换不返回旧缓存 | PASS |
| `test_smc_output_structure` | 输出含 events/order_blocks/equal_highs_lows/pivots/trailing/time/params | PASS |
| `test_anchor_confirmed_immutability` | 事件一旦写入不可变 | PASS |

### 12.5 前端测试

| 测试 | 验证内容 | 状态 |
|---|---|---|
| `chartDrag.test.ts` | Pointer Events 拖拽契约 | PASS |
| `chartLabels.test.ts` | 文案契约（POC→核心共识价等） | PASS |
| `columnAlignment.test.ts` | 列对齐纯函数 + 源码契约 | PASS |
| SMC renderer 组件测试 | internal 虚线、swing 实线、OB box、Historical 全量 | 待补 |

### 12.6 SMC Pine 确定性测试（**CHANGE-20260717-001 新增**，`test_smc_pine_deterministic.py`）

不依赖 TV CSV fixture，使用合成 OHLC 数据验证 SMC 核心逻辑的 Pine 语义正确性。

| 测试类 | 验证内容 | 状态 |
|---|---|---|
| `TestChoCHRules` | CHoCH 规则（bearish after bullish + tag before bias update） | PASS |
| `TestBOSRules` | BOS（bias 延续时非 CHoCH） | PASS |
| `TestWarmupConsistency` | 5000 计算/4000 展示 vs 4000 计算/4000 展示（重叠窗口一致） | PASS |
| `TestOrderBlockOrder` | OB newest-first（`insert(0, ...)`，Pine `array.unshift`） | PASS |
| `TestOrderBlockChain` | core→adapter 字段完整性（顺序、anchor、high/low、mitigation） | PASS |
| `TestTrailingNaN` | trailing NaN + `last_top_time`/`last_bottom_time`（首个 swing pivot 前为 NaN） | PASS |
| `TestExecutionGate` | internal/swing gate 关闭→事件为空（Pine L784/L787） | PASS |
| `TestEqualHighLowGeometry` | EQ 两端点 `prev_level`/`level` + anchor→second_pivot 区间 | PASS |

---

## 十三、Known Gap

| # | 差异 | 影响 | 修复状态 |
|---|---|---|---|
| 1 | `updateTrailingExtremes` 顺序：Python 在 step 6，Pine 在 step 1 | trailing.top/bottom 在新 pivot 检测 bar 与 Pine 不一致 | **CHANGE-20260715-003 修复** |
| 2 | `displayStructure(internal)` 条件：Python 用 `show_internal_order_blocks`，Pine 用 `showInternalsInput or showInternalOrderBlocksInput or showTrendInput` | 默认值下行为一致；用户自定义参数时会分歧 | **CHANGE-20260717-001 修复**（新增 `show_internals`/`show_trend`，`internal_gate` 门控） |
| 3 | `displayStructure(swing)` 条件：Python 始终执行，Pine 用 `showStructureInput or showSwingOrderBlocksInput or showHighLowSwingsInput` | 同上 | **CHANGE-20260717-001 修复**（新增 `show_structure`，`swing_gate` 门控） |
| 4 | Pine golden fixture 未提供 | 无法进行输出级完全一致断言 | **PINE_PARITY_PENDING**（等待用户 TV 导出；代码级修复通过，输出级 parity pending） |
| 5 | SMC renderer 组件测试未补 | 前端渲染契约未在测试中固化 | 待补 |
| 6 | **[CHANGE-20260717-001]** Strong/Weak 颜色按 strong/weak 区分 | Pine 用固定 bearish/bullish 色；前端按 strong/weak 区分（强高红/弱高绿、强低绿/弱低红） | **有意视觉差异**（不影响数据与几何） |
| 7 | **[CHANGE-20260717-001]** trailing NaN 旧实现凭空初始化 | `math.max(high, na)=na`，旧 `or` 分支用 high/low 初始化 | **CHANGE-20260717-001 修复**（仅非 NaN 时更新） |
| 8 | **[CHANGE-20260717-001]** OB 顺序旧实现 oldest-first | Pine `array.unshift` 为 newest-first；前端 `slice(0,5)` 取最旧 5 个 | **CHANGE-20260717-001 修复**（`append`→`insert(0,...)`） |
| 9 | **[CHANGE-20260717-001]** EQH/EQL 旧实现水平线 | Pine L396 两端点线（`prev_level`→`level`） | **CHANGE-20260717-001 修复**（两端点 + EQH 绿/EQL 红 + 中点标签） |
| 10 | **[CHANGE-20260717-001]** Strong/Weak 旧实现从最后 bar 起画 | Pine L721-727 线起点 `trailing.lastTopTime/lastBottomTime` | **CHANGE-20260717-001 修复**（`timeToDisplayIdx` + `last_top_time`/`last_bottom_time`） |
| 11 | **[CHANGE-20260717-001]** 15m warmup 不足 | 窗口左缘 pivot/BOS/CHoCH 丢失；1mo <200 ATR200 全 NaN | **CHANGE-20260717-001 修复**（15m 5000 计算/4000 展示、1mo ≥200） |

---

## 十四、Parity 状态总结

| 维度 | 状态 | 说明 |
|---|---|---|
| Pine 语义原语 | ✅ 单元对齐 | RMA/ATR/CMR/highest/lowest/crossover/crossunder 8 项测试 PASS（**注意**：CHANGE-20260716-001 修正了 `displayStructure` 中 crossover/crossunder 的 level_curr/level_prev 快照语义，旧实现错误地将 `current_level` 同时作为 curr 和 prev） |
| 默认参数 | ✅ 单元对齐 | 逐项匹配 Pine input 默认值；**[CHANGE-20260717-001]** 新增 `show_internals`/`show_structure`/`show_trend` 三参数对应 Pine L76/L84/L74 |
| 状态变量 | ✅ 结构对齐 | 6 个 pivot + 2 个 trend + trailing + OB 列表 |
| 执行顺序 | ✅ 已修复 | `updateTrailingExtremes` 顺序（CHANGE-20260715-003 修复） |
| execution gate | ✅ 已修复 | **[CHANGE-20260717-001]** internal/swing gate 严格复刻 Pine L784/L787（`internal_gate`/`swing_gate`） |
| trailing NaN | ✅ 已修复 | **[CHANGE-20260717-001]** `math.max(high, na)=na`，trailing 仅由 swing pivot 初始化 |
| OB 顺序 | ✅ 已修复 | **[CHANGE-20260717-001]** `insert(0, ...)` newest-first（Pine `array.unshift`）；前端 `slice(0,5)` 取最新 5 个 |
| anchor/confirmed 契约 | ✅ 结构对齐 | 6 类事件均含 anchor/confirmed；CHANGE-20260716-001 统一 EQH/EQL 为 anchor/second_pivot/confirmed 三时间点（second_pivot 为视觉线端点） |
| FVG 排除 | ✅ 完全排除 | 6 项输出级断言 PASS |
| warmup | ✅ 满足 | **[CHANGE-20260717-001]** 计算历史与展示窗口分离：15m 5000 计算/4000 展示（1000 warmup）、1d 完整日线、1h/1w 完整历史、1mo ≥200（扩展回看 7000 天） |
| 缓存隔离 | ✅ 完全隔离 | **[CHANGE-20260717-001]** ALGORITHM_VERSION v10（从 v9 bump，旧 SMC 缓存强制失效）+ `:smc` 后缀 |
| 前端渲染 | ✅ 代码级修复完成 | **[CHANGE-20260717-001]** EQH/EQL 两端点线（`prev_level`→`level`，EQH 绿/EQL 红）、Strong/Weak 线起点 `trailing.last_top_time`/`last_bottom_time`（`timeToDisplayIdx` 辅助）；CHANGE-20260716-001 已修正 anchor_index 统一、viewport 区间求交、slice(0,5)、标签不加 `·I`、纵轴候选完整、Canvas mock 测试；需 golden fixture 输出级验证 |
| 确定性测试 | ✅ 新增 | **[CHANGE-20260717-001]** `test_smc_pine_deterministic.py` 8 个测试类（不依赖 TV fixture，合成 OHLC 验证 Pine 语义） |
| Pine golden fixture | ⏳ PENDING | **PINE_PARITY_PENDING**：等待用户 TV 导出；CHANGE-20260717-001 已修复 golden 测试本身（EQH 类型/时间戳/容差 0/全链测试）；无 fixture 时 skip |

**结论**：Pine 语义原语、默认参数、执行顺序、execution gate、trailing NaN、OB 顺序、warmup、前端渲染几何均已代码级修复完成（CHANGE-20260715-003 + CHANGE-20260716-001 + CHANGE-20260717-001）；确定性测试 8 个测试类覆盖 CHoCH/BOS/warmup/OB/trailing/gate/EQ 几何；**PINE_PARITY_PENDING：无真实 TV CSV fixture，代码级修复通过，输出级 parity pending**。不伪造 fixture，不声称"完全对齐"。
