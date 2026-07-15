# SMC Pine Golden Fixture 目录

本目录存放从 TradingView LuxAlgo Smart Money Concepts 指标导出的 golden fixture，
用于验证 `smc_pine_core.py` 的 Pine 语义等价性。

## 文件结构

```
smc_pine/
  README.md                          ← 本文件（导出指南）
  ohlcv_603538_1d_1000.csv           ← 美诺华 603538 日线 1000 根 OHLCV（脱敏）
  ohlcv_603538_15m_1000.csv          ← 美诺华 603538 15m 1000 根 OHLCV（脱敏）
  pine_events_603538_1d.csv          ← Pine 导出的事件序列
  pine_order_blocks_603538_1d.csv    ← Pine 导出的 OB 序列
  pine_events_603538_15m.csv         ← Pine 导出的事件序列（15m）
  pine_order_blocks_603538_15m.csv   ← Pine 导出的 OB 序列（15m）
```

## 导出步骤

### 1. 在 TradingView 上加载 LuxAlgo SMC 指标

1. 打开 TradingView，选择 A 股标的（如美诺华 603538）。
2. 添加 "Smart Money Concepts" 指标（LuxAlgo 版本）。
3. 设置参数（必须与生产 DEFAULT_PARAMS 一致）：
   - Historical = true, Colored = true
   - Internal Structure = true, length=5, All, confluence=false, tiny
   - Swing Structure = true, length=50, All, small
   - Strong/Weak Highs/Lows = true
   - Internal Order Blocks = true, 5, ATR, High/Low
   - Swing Order Blocks = false
   - EQH/EQL = true, bars=3, threshold=0.1, tiny
   - Swing Points = false, MTF = false, Premium/Discount = false
   - FVG = false（生产排除 FVG）

### 2. 导出 OHLCV 数据

1. 在图表底部右键 → "Export data" 或使用 TV 的数据导出功能。
2. 导出日线 1000 根和 15m 1000 根。
3. 保存为 CSV，列：time, open, high, low, close, volume。
4. 脱敏处理：移除真实标的名称，保留 time/OHLCV 数值。

### 3. 导出 Pine 事件序列（通过隐藏 plot）

在 Pine Editor 中添加以下隐藏 plot 代码到 LuxAlgo SMC 指标末尾，
将事件序列导出为 CSV：

```pine
// ===== 隐藏 plot 用于导出事件序列 =====
// BOS/CHoCH 事件
plot(boschoc_event_code, "smc_event_code", display=display.data_window)
plot(boschoc_scope, "smc_event_scope", display=display.data_window)    // 0=internal, 1=swing
plot(boschoc_bias, "smc_event_bias", display=display.data_window)      // 1=bullish, -1=bearish
plot(boschoc_level, "smc_event_level", display=display.data_window)
plot(boschoc_anchor_bar, "smc_event_anchor_bar", display=display.data_window)
plot(boschoc_confirmed_bar, "smc_event_confirmed_bar", display=display.data_window)

// Order Blocks
plot(ob_top, "smc_ob_top", display=display.data_window)
plot(ob_bottom, "smc_ob_bottom", display=display.data_window)
plot(ob_bias, "smc_ob_bias", display=display.data_window)
plot(ob_anchor_bar, "smc_ob_anchor_bar", display=display.data_window)
plot(ob_mitigated, "smc_ob_mitigated", display=display.data_window)
```

然后通过 TV 的 "Data Window" 或 "Export" 功能导出 CSV。

### 4. CSV 格式

**事件 CSV 格式** (`pine_events_*.csv`)：
```
time,event_code,event_scope,event_bias,event_level,event_anchor_bar,event_confirmed_bar
2026-01-15,BOS,1,1,10.50,120,125
2026-01-16,CHoCH,0,-1,9.80,130,135
```

- `event_code`: BOS 或 CHoCH
- `event_scope`: 0=internal, 1=swing
- `event_bias`: 1=bullish, -1=bearish
- `event_level`: 事件价格水平
- `event_anchor_bar`: anchor bar 的 bar_index（从 0 开始）
- `event_confirmed_bar`: confirmed bar 的 bar_index

**OB CSV 格式** (`pine_order_blocks_*.csv`)：
```
time,ob_bias,ob_top,ob_bottom,ob_anchor_bar,ob_mitigated
2026-01-15,1,10.20,9.80,118,0
2026-01-16,-1,11.00,10.60,125,1
```

## Golden Test 使用方式

`test_smc_pine_parity.py` 会加载本目录下的 CSV fixture，
与 `smc_pine_core.compute_smc_pine()` 的输出进行逐事件比较。

比较元组：
- 事件：`(type, scope, bias, anchor, confirmed, level)`
- OB：`(bias, anchor, top, bottom, mitigated)`

浮点容差：1e-8。

**没有 Pine golden fixture 不得宣称"完全对齐"。**

## 当前状态

- [ ] ohlcv_603538_1d_1000.csv — 待导出
- [ ] ohlcv_603538_15m_1000.csv — 待导出
- [ ] pine_events_603538_1d.csv — 待导出
- [ ] pine_order_blocks_603538_1d.csv — 待导出
- [ ] pine_events_603538_15m.csv — 待导出
- [ ] pine_order_blocks_603538_15m.csv — 待导出

**Pine golden 对齐状态：PENDING（等待 TV 导出）**
