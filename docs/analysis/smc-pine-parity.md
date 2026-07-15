# SMC Pine 等价基准分析

本文件逐函数对比用户 Pine SMC 源码（`ref/smc_ref.txt`，用户原创）、ref/smc.py（Python 重写版）和当前生产 smc_pine_core.py 三者的差异，作为 Pine parity 实现的基准。

## 参考来源

- **Pine 原始源码**：`ref/smc_ref.txt`（用户原创，SHA256 0bd3d2ad，843 行，授权盘迹商业项目使用），为 SMC 算法唯一真源
- **ref/smc.py**：早期 Python 重写版，包含 pytdx 数据源、argparse、Plotly 输出（已被 `ref/smc_ref.txt` Pine 源码取代为算法真源）
- **生产 smc_pine_core.py**：当前 backend/app/strategy_assets/algorithms/features/smc_pine_core.py，唯一 Pine 语义核心（生产+测试共用）

---

## 一、ta.atr / RMA（CRITICAL）

### Pine 语义
```
// Pine 的 ta.rma(src, length) 是 Wilder's Running Moving Average
rma[0] = sma(src, length)  // 首个值用 SMA 播种
rma[i] = (rma[i-1] * (length-1) + src[i]) / length  // 递推

// Pine 的 ta.atr(n) = ta.rma(ta.tr, n)
// ta.tr = true range
```

### ref/smc.py（第 158-159 行）
```python
def atr(df, n):
    return true_range(df).rolling(n, min_periods=1).mean()  # SMA，不是 RMA
```
**差异**：使用 Simple Moving Average（`rolling().mean()`），不是 Pine 的 Wilder's RMA。`min_periods=1` 在前 n-1 根用部分窗口 SMA。

### 生产 smc_indicator.py（第 240-252 行）
```python
def _compute_atr(tr, n):
    # 也是 SMA：window_sum / count
```
**差异**：与 ref/smc.py 相同的 SMA 实现。

### 影响
ATR200 用于：
1. `volatilityMeasure`（当 order_block_filter="Atr" 时）
2. `highVolatilityBar` 判断（`high-low >= 2*atr`）
3. EQH/EQL 阈值（`equal_threshold * atr`）

SMA 和 RMA 在稳定后数值会趋近，但前 200-400 根差异显著，直接影响 parsedHigh/parsedLow 互换、OB 极值选择和 EQH/EQL 触发。

### 修复目标
smc_pine_core.py 实现 `ta.rma`：
- 前 length-1 根：逐步 SMA（min_periods 行为）
- 第 length-1 根：完整 SMA 作为种子
- 之后：Wilder 递推 `(prev * (length-1) + src) / length`

---

## 二、累计区间（Cumulative Mean Range）

### Pine 语义
```
// bar_index 从 0 开始
cumulativeMeanRange = ta.cum(ta.tr) / bar_index
// bar 0: tr[0] / 0 = na（除零）
// bar 1: (tr[0]+tr[1]) / 1
// bar n: sum(tr[0..n]) / n
```

### ref/smc.py（第 162-165 行）
```python
def cumulative_mean_range(df):
    tr = true_range(df)
    idx = np.arange(1, len(df) + 1)  # 1, 2, 3, ...
    return tr.cumsum() / idx
# bar 0: tr[0] / 1
# bar 1: (tr[0]+tr[1]) / 2
# bar n: sum(tr[0..n]) / (n+1)
```
**差异**：除数用 `arange(1, n+1)`，即 `i+1`，而 Pine 用 `bar_index`，即 `i`。ref 在 bar 0 有值，Pine 在 bar 0 为 na。所有 bar 的除数比 Pine 大 1。

### 生产 smc_indicator.py（第 255-263 行）
与 ref/smc.py 相同，除数用 `i+1`。

### 修复目标
- bar 0: NaN
- bar i (i>0): `sum(tr[0..i]) / i`

---

## 三、leg / pivot 确认

### Pine 语义
```
newLegHigh = high[size] > ta.highest(high, size)
newLegLow = low[size] < ta.lowest(low, size)
// high[size] = i-size 那根 bar 的 high
// ta.highest(high, size) = max(high[i-size+1..i])（不含 high[size]）
```

### ref/smc.py（第 412-442 行）
```python
def leg(self, i, size, lane):
    ref_i = i - size
    new_leg_high = self.highs[ref_i] > self._highest_after_ref_window(ref_i, size)
    # _highest_after_ref_window: max(highs[ref_i+1 : ref_i+size+1])
```
**等价**：窗口 `[ref_i+1, ref_i+size]` 即 `[i-size+1, i]`，与 Pine `ta.highest(high, size)` 一致。

### 生产 smc_indicator.py（第 288-331 行）
与 ref/smc.py 相同实现。

### 结论
leg/pivot 确认逻辑三者一致，无需修改。

---

## 四、crossover / crossunder

### Pine 语义
```
ta.crossover(a, b) = a[0] > b[0] and a[1] <= b[1]
ta.crossunder(a, b) = a[0] < b[0] and a[1] >= b[1]
```

### ref/smc.py（第 615-629 行）
```python
def _crossover_close(level):
    return i > 0 and closes[i-1] <= level and closes[i] > level
def _crossunder_close(level):
    return i > 0 and closes[i-1] >= level and closes[i] < level
```
**等价**。

### 生产 smc_indicator.py（第 512-517 行）
与 ref/smc.py 相同。

### 结论
一致，无需修改。

---

## 五、执行顺序

### Pine / ref/smc.py / 生产 三者一致
```
for i in range(n):
    1. get_current_structure(i, swings_length=50, swing)      # swing pivot
    2. get_current_structure(i, 5, internal)                   # internal pivot
    3. get_current_structure(i, equal_length=3, equal)         # EQH/EQL（if show_equal_hl）
    4. display_structure(i, internal=True)                     # internal BOS/CHoCH
    5. display_structure(i, internal=False)                    # swing BOS/CHoCH
    6. update_trailing_extremes(i)                             # trailing strong/weak
    7. delete_order_blocks(i, internal=True)                   # internal OB mitigation
    8. delete_order_blocks(i, internal=False)                  # swing OB mitigation
```

---

## 六、OB 存储 slice

### Pine 语义
```
// Bearish OB: 在 [pivot.barIndex, bar_index) 区间找 parsedHighs 最大值
// Bullish OB: 在 [pivot.barIndex, bar_index) 区间找 parsedLows 最小值
array.push(orderBlocks, 0, OrderBlock(...))
if array.size(orderBlocks) > 100
    array.pop(orderBlocks)  // 删除最后一个（最旧）
```

### ref/smc.py（第 769-805 行）
```python
start = piv.barIndex
end = current_i  # end-exclusive
if bias == BEARISH:
    local_idx = np.argmax(parsedHighs[start:end])
else:
    local_idx = np.argmin(parsedLows[start:end])
parsed_index = start + local_idx
target.insert(0, ob)  # 头部插入
if len(target) >= 100:
    target.pop()  # 删除尾部（最旧）
```
**等价**。

### 生产 smc_indicator.py（第 574-638 行）
与 ref/smc.py 相同，用 `list.index(max(arr))` 代替 `np.argmax`。

### 结论
一致。

---

## 七、OB mitigation

### Pine 语义
```
if orderBlockMitigation == "Close"
    bearish_src = close, bullish_src = close
else  // "High/Low"
    bearish_src = high, bullish_src = low

if bearish_src > ob.top and ob.bias == BEARISH → mitigated
if bullish_src < ob.bottom and ob.bias == BULLISH → mitigated
```

### ref/smc.py（第 739-767 行）
与 Pine 相同。

### 生产 smc_indicator.py（第 640-679 行）
与 ref/smc.py 相同。

### 结论
一致。

---

## 八、EQH/EQL

### Pine 语义
```
equalThreshold = 0.1 * atr  // 默认 equal_threshold=0.1
if abs(piv.currentLevel - level) < equalThreshold * atr
    → EQH/EQL
```

### ref/smc.py（第 521-527 行）
```python
if abs(piv.currentLevel - level) < self.args.equal_threshold * atr_measure
```
**等价**。

### 生产 smc_indicator.py（第 379-394 行）
与 ref/smc.py 相同。

### 结论
一致。但注意：ATR 值不同会导致 EQH/EQL 触发不同。

---

## 九、Historical / Present 模式

### Pine 语义
- Historical：保留所有历史事件（不删除旧线/标签）
- Present：只显示最新事件（删除旧线/标签）

### ref/smc.py
- `mode` 参数默认 `HISTORICAL`
- Present 模式下 `draw_structure` 和 `draw_order_blocks` 会先 delete 旧 prefix

### 生产 smc_indicator.py
- 不区分 Historical/Present，所有事件都保留在输出列表中
- 前端负责渲染所有事件

### 修复目标
生产保持 Historical 语义（所有事件保留），前端不得因标签碰撞删除事件，只允许调整标签偏移。

---

## 十、Strong/Weak High/Low

### Pine 语义
```
trailing.top / trailing.bottom 在 swing pivot 更新时设置
update_trailing_extremes 在每个 bar 更新极值

"Strong High" if swingTrend.bias == BEARISH else "Weak High"
"Strong Low" if swingTrend.bias == BULLISH else "Weak Low"
```

### ref/smc.py（第 843-855, 1070-1113 行）
与 Pine 相同。

### 生产 smc_indicator.py（第 683-700 行）
与 ref/smc.py 相同。输出 trailing dict 包含 top/bottom/bar_time/last_top_time/last_bottom_time。

### 结论
一致。分类逻辑由前端根据 swingTrend.bias 判断。

---

## 十一、预热长度（Warmup）

### Pine
- 在 TradingView 上，指标可访问图表上的所有历史 bar
- 默认 ATR200 需要 200 根预热
- swings_length=50 需要 50 根预热
- 实际有效事件从第 200+ 根开始

### ref/smc.py
- 默认获取 1000 根 bar（`--bars 1000`）
- 足够的预热空间

### 生产 smc_indicator.py
- 当前使用 API 传入的 250 根日线
- 250 根中前 200 根用于 ATR200 预热，仅最后 50 根有完整 ATR
- swings_length=50 需要 50 根预热，但 pivot 确认在 i > 50 后才开始

### 修复目标
- 计算必须包含足够 warmup，至少展示区之前 500 根
- 可获得时使用完整历史计算后裁剪输出
- 不得只用当前可见 bars 初始化状态

---

## 十二、数据复权

### Pine
- 使用图表上的复权数据（通常前复权）
- SMC 计算基于复权后的 OHLC

### ref/smc.py
- 从 pytdx 获取数据，默认不复权

### 生产 smc_indicator.py
- 使用 indicator_service 传入的 bars，复权方式由调用方决定
- 当前 API 使用 qfq（前复权）

### 修复目标
输入 bars 必须与主 K 线完全相同：相同 symbol/timeframe/复权/时间戳。

---

## 十三、FVG 排除

### Pine
- FVG 是可切换功能，默认关闭
- 实现包含 bullish/bearish FVG 检测、mitigation、延伸

### ref/smc.py
- FVG 代码存在但默认 `show_fvg=False`
- compute_fvg/draw_fvg 在 show_fvg=False 时不执行

### 生产 smc_indicator.py
- FVG 完全排除：不计算、不返回、不缓存、不渲染
- 生产计算路径无 FVG 函数或状态

### 修复目标
smc_pine_core.py 继续 FVG 完全排除，不提供 FVG 开关。FVG 排除不得改变其他逻辑的索引、执行顺序和右侧延伸。

---

## 十四、前端渲染

### Pine
- internal 线为虚线（dash），标签 tiny（10px）
- swing 线为实线（solid），标签 small（11px）
- 标签位于结构线中点
- OB 为半透明 box，从创建 bar 延伸到 mitigation 或右端
- Historical 模式绘制全部事件

### 生产 StrategyChart
- 当前已有 SMC Canvas 渲染
- 需验证：internal 虚线、swing 实线、标签中点、OB box、Historical 全量

### 修复目标
- internal 线为虚线、tiny 标签
- swing 线为实线、small 标签
- 标签位于结构线中点
- Historical 模式绘制全部事件
- internal OB 默认显示最近 5 个有效区域
- API 有 OB 而 DOM/canvas 绘制数为 0 时测试失败
- 多头红 #FF4D4F、空头绿 #22C55E
- 品牌绿只用于 SMC 开关

---

## 十五、总结：必须修复的差异

| # | 差异 | 优先级 | 影响范围 |
|---|------|--------|----------|
| 1 | ATR: SMA → RMA（Wilder's） | CRITICAL | parsedHigh/Low、OB 极值、EQH/EQL 阈值 |
| 2 | CMR: 除数 i+1 → i，bar 0 = NaN | HIGH | CMR 模式下的 volatility |
| 3 | Warmup: 需 ≥500 根预热 | HIGH | 事件数量和位置 |
| 4 | 新建 smc_pine_core.py 统一核心 | HIGH | 架构 |
| 5 | 前端渲染对齐（虚线/实线/OB box） | MEDIUM | 视觉一致性 |
| 6 | Pine golden fixture 导出指南 | MEDIUM | 验证基准 |

---

## 十六、Pine/Python 逐函数映射对照表（CHANGE-20260715-002 新增）

本表为用户 Pine 源码（`ref/smc_ref.txt`）与生产 `smc_pine_core.py` 的逐函数/状态变量映射。
所有 Pine 语义原语由 `smc_pine_core.py` 唯一实现，`smc_indicator.py` 仅作薄包装委托。

### 16.1 Pine 原语 → Python 函数

| Pine 函数/表达式 | Python 实现 | 默认值 | NA 初始行为 |
|---|---|---|---|
| `ta.rma(src, length)` | `pine_rma(src, length)` | length=200（ATR） | 前 length-1 根：逐步 SMA；第 length-1 根：完整 SMA 种子 |
| `ta.tr` | `pine_true_range(highs, lows, closes)` | — | bar 0 = high-low（无前 close） |
| `ta.atr(n)` | `pine_atr(highs, lows, closes, n)` | n=200 | = `pine_rma(pine_true_range, 200)` |
| `ta.cum(ta.tr) / bar_index` | `pine_cumulative_mean_range(highs, lows, closes)` | — | bar 0 = NaN（除零）；bar i = cumsum(tr[0..i]) / i |
| `ta.highest(src, length)` | `pine_highest(src, length, ref_i)` | length=swings_length=50 / internal=5 / equal=3 | 窗口 `[ref_i+1, ref_i+length]`，不含 ref_i 本身 |
| `ta.lowest(src, length)` | `pine_lowest(src, length, ref_i)` | 同上 | 同上 |
| `ta.crossover(a, b)` | `pine_crossover(a_curr, a_prev, b_curr, b_prev)` | — | `a_curr > b_curr and a_prev <= b_prev` |
| `ta.crossunder(a, b)` | `pine_crossunder(a_curr, a_prev, b_curr, b_prev)` | — | `a_curr < b_curr and a_prev >= b_prev` |
| `ta.change(x)` | 直接用 `x[i] - x[i-1]` | — | i=0 时返回 na |
| `nz(x, 0)` | `x if x == x else 0.0`（NaN 检查） | — | NaN → 0 |

### 16.2 Pine 状态变量 → Python 状态

| Pine 状态变量 | Python 字段 | 默认值 | 说明 |
|---|---|---|---|
| `var pivot swingHigh` | `self.swing_high: _Pivot` | currentLevel=na, lastLevel=na, crossed=false | swing 高点 pivot |
| `var pivot swingLow` | `self.swing_low: _Pivot` | 同上 | swing 低点 pivot |
| `var pivot internalHigh` | `self.internal_high: _Pivot` | 同上 | internal 高点 pivot |
| `var pivot internalLow` | `self.internal_low: _Pivot` | 同上 | internal 低点 pivot |
| `var pivot equalHigh` | `self.equal_high: _Pivot` | 同上 | EQH pivot |
| `var pivot equalLow` | `self.equal_low: _Pivot` | 同上 | EQL pivot |
| `var trend swingTrend` | `self.swing_trend: _Trend` | bias=0 | swing 趋势（BULLISH=1/BEARISH=-1/0=未定） |
| `var trend internalTrend` | `self.internal_trend: _Trend` | bias=0 | internal 趋势 |
| `var trailingExtremes trailing` | `self.trailing: _TrailingExtremes` | top=na, bottom=na | Strong/Weak High/Low 极值 |
| `var array<orderBlock> swingOrderBlocks` | `self.swing_order_blocks: list[_OrderBlock]` | [] | swing OB 列表（默认不显示） |
| `var array<orderBlock> internalOrderBlocks` | `self.internal_order_blocks: list[_OrderBlock]` | [] | internal OB 列表（默认显示最近 5 个） |
| `var array<float> parsedHighs` | `self.parsed_highs: list[float]` | — | 高波动 bar 互换后的 highs |
| `var array<float> parsedLows` | `self.parsed_lows: list[float]` | — | 高波动 bar 互换后的 lows |
| `var array<fairValueGap> fairValueGaps` | **完全排除**（不实现） | — | FVG 不计算、不返回、不缓存 |

### 16.3 Pine 默认参数 → Python DEFAULT_PARAMS

| Pine 输入 | 默认值 | Python 字段 |
|---|---|---|
| `showSwingsInput` | false | `show_swings` |
| `swingsLengthInput` | 50 | `swings_length` |
| `showStructureInput` | true | —（始终启用 swing structure） |
| `showInternalsInput` | true | —（始终启用 internal structure） |
| `internalStructureSize` | TINY | —（前端标签尺寸） |
| `swingStructureSize` | SMALL | —（前端标签尺寸） |
| `internalFilterConfluenceInput` | false | `internal_filter_confluence` |
| `internalOrderBlocksSizeInput` | 5 | `internal_ob_size` |
| `swingOrderBlocksSizeInput` | 5 | `swing_ob_size` |
| `showInternalOrderBlocksInput` | true | `show_internal_order_blocks` |
| `showSwingOrderBlocksInput` | false | `show_swing_order_blocks` |
| `orderBlockFilterInput` | 'Atr' | `order_block_filter` |
| `orderBlockMitigationInput` | 'High/Low' | `order_block_mitigation` |
| `showEqualHighsLowsInput` | true | `show_equal_hl` |
| `equalHighsLowsLengthInput` | 3 | `equal_length` |
| `equalHighsLowsThresholdInput` | 0.1 | `equal_threshold` |
| `showHighLowSwingsInput` | true | `show_high_low_swings` |
| `showFairValueGapsInput` | false | **完全排除**（不提供开关） |

### 16.4 执行顺序（Pine 逐 bar 主循环 → Python `_SMCPineState.run()`）

| 步骤 | Pine 函数 | Python 方法 | 说明 |
|---|---|---|---|
| 1 | `get_current_structure(i, swings_length, swing)` | `get_current_structure(i, swings_length, internal=False, equal_high_low=False)` | swing pivot 检测 |
| 2 | `get_current_structure(i, 5, internal)` | `get_current_structure(i, 5, internal=True, equal_high_low=False)` | internal pivot 检测 |
| 3 | `get_current_structure(i, equal_length, equal)` | `get_current_structure(i, equal_length, equal_high_low=True)` | EQH/EQL 检测（if show_equal_hl） |
| 4 | `display_structure(i, internal=True)` | `display_structure(i, internal=True)` | internal BOS/CHoCH |
| 5 | `display_structure(i, internal=False)` | `display_structure(i, internal=False)` | swing BOS/CHoCH |
| 6 | `update_trailing_extremes(i)` | `update_trailing_extremes(i)` | trailing strong/weak high/low |
| 7 | `delete_order_blocks(i, internal=True)` | `delete_order_blocks(i, internal=True)` | internal OB mitigation |
| 8 | `delete_order_blocks(i, internal=False)` | `delete_order_blocks(i, internal=False)` | swing OB mitigation |

### 16.5 anchor/confirmed 因果契约

| 事件类型 | anchor | confirmed | 不可变契约 |
|---|---|---|---|
| pivot | `ref_i` (i-size) | `i` (leg change 确认 bar) | pivot 写入后 currentLevel/lastLevel/crossed 可更新，但 pivot 事件记录不可变 |
| BOS | `pivot.barIndex` (被穿越的 pivot bar) | `i` (close 穿越 pivot 的 bar) | 事件一旦写入不可变 |
| CHoCH | `pivot.barIndex` (被穿越的 pivot bar) | `i` (close 穿越 pivot 的 bar) | 同上 |
| Order Block | `parsed_index` (OB bar) | `current_i` (触发 OB 创建的 BOS/CHoCH bar) | OB 创建后 top/bottom/bias 不可变 |
| EQH/EQL | `prev piv.barIndex` (前一 pivot) | `i-size` (新 pivot bar) | 同上 |
| Mitigation | OB.anchor | `i` (close/high/low 穿越 OB 的 bar) | mitigated=True 后不可逆 |

### 16.6 输出 DTO

| 输出字段 | 类型 | 来源 | 说明 |
|---|---|---|---|
| `events` | `list[dict]` | BOS/CHoCH | 每个事件含 type/anchor_index/anchor_time/confirmed_index/confirmed_time/level/bias/internal |
| `order_blocks` | `list[dict]` | OB 列表 | 每个 OB 含 bias/anchor_index/anchor_time/confirmed_index/confirmed_time/top/bottom/mitigated/mitigated_index/mitigated_time/internal |
| `equal_highs_lows` | `list[dict]` | EQH/EQL | 每个含 type/anchor_index/anchor_time/confirmed_index/confirmed_time/level/prev_level |
| `pivots` | `list[dict]` | pivot 记录 | 每个含 type/level/bar_index/bar_time/internal |
| `trailing` | `dict` | Strong/Weak | top/bottom/bar_time/bar_index/last_top_time/last_bottom_time |
| `params` | `dict` | DEFAULT_PARAMS | 实际使用的参数快照 |
| `state` | `dict` | 最终状态 | swing_trend/internal_trend/trailing 摘要 |
| `time` | `list[str]` | 完整时间序列 | 与输入 bars 等长（不截断），用于 anchor/confirmed 索引对齐 |
| **FVG** | **不存在** | — | 完全排除，输出中无 fvg/fair_value_gap 字段 |

### 16.7 测试 fixture 状态

| fixture | 状态 | 说明 |
|---|---|---|
| Pine golden CSV | **PENDING** | 等待用户从 TradingView 导出事件/OB CSV |
| `backend/tests/fixtures/smc_pine/README.md` | 已创建 | TV 导出步骤、隐藏 plot 代码、CSV 格式规范 |
| `TestPineGoldenFixture` | skip（无 fixture 时） | 无 fixture 时跳过，不得宣称"完全对齐" |
| `TestPineSemantics` | 8 项 PASS | RMA Wilder 递推、RMA min_periods、CMR bar0=NaN、ATR=RMA(TR)、crossover、crossunder、highest、lowest |
| 美诺华 603538 日线 1000 根 | 待用户提供 | 用于 golden 测试 |
| 15m 样本 | 待用户提供 | 用于 golden 测试 |

**PINE_GOLDEN_NOT_PROVIDED**：当前无 Pine 导出的 golden CSV，无法进行输出级完全一致断言。
Python 单元测试已覆盖 Pine 语义原语（8 项），但不得声称"已完全对齐 Pine"。
用户授权的 SMC 实现保留，不因缺少 golden fixture 而撤销。

### 16.8 关键差异修复状态

| 差异 | 修复状态 | 验证方式 |
|---|---|---|
| ATR: SMA → RMA（Wilder's） | ✅ 已修复 | `pine_rma` 实现 + `TestPineSemantics.test_rma_wilder_recurrence` |
| CMR: 除数 i+1 → i，bar 0 = NaN | ✅ 已修复 | `pine_cumulative_mean_range` + `TestPineSemantics.test_cmr_bar0_nan` |
| Warmup: ≥500 根预热 | ✅ 已修复 | `indicator_service.py` 1d 使用 `full_daily_bars`（DB 全量日线） |
| smc_pine_core.py 统一核心 | ✅ 已创建 | 852 行，唯一 Pine 语义核心 |
| FVG 完全排除 | ✅ 已实现 | 输出级别断言（keys/events/OB/EQH/EQL/params/state 6 项） |
| 前端渲染对齐 | ✅ 已实现 | internal 虚线 [4,3]/tiny 8px，swing 实线/small 11px，OB box 半透明 |
| 缓存隔离 | ✅ 已实现 | ALGORITHM_VERSION v6→v7，`:smc` 后缀 |
| Pine golden fixture | ⏳ PENDING | 等待用户 TV 导出，README 已就绪 |
