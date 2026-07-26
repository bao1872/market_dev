# 20 行情、复权和指标

## MDAS

`MarketDataAggregationService` 是行情读取和复权唯一出口。

禁止业务层：

- 直接 repository 查询行情；
- 直接应用复权；
- 自行周/月聚合；
- 创建第二套行情出口。

## 复权

- raw bar 不复权落库；
- qfq 在 MDAS 应用一次；
- `adjustment_as_of` 防止未来公司行为；
- 周/月在日线复权后聚合。

## ChartSnapshot

详情 quote、K 线和指标必须同一快照。

禁止：

- `/quote` + `/chart-snapshot` 双源；
- quote→bar 覆盖；
- 生产中 1m 合成 1d/15m/60m。

## Canonical 四链

详情、盘后、盘中、Capture 经 Canonical Service 和 Registry。

相同五维输入必须有相同 result hash。

## Node

```text
1d 250
15m 4000
1m 2（只用于穿越）
```

VA 外 Peak 有效，nearest node 来自全部 Peak。

## SMC

- FVG 排除；
- strict time key；
- 生产 Kernel 唯一真源；
- `ref/` 仅人工参考。

## AFC

Core 14 数量、分组、顺序和分母不得无版本升级修改。
