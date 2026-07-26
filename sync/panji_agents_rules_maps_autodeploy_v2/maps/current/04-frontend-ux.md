# 前端和 UX

## 路由

```text
/                       静态门户
/login
/market                 无主 K 线工作区
/stock/:symbol          唯一主 K 线详情
/messages
/settings
/admin/*
/capture/stock/:symbol
```

## 原则

- 前端不重算权限和指标；
- `/market` 负责筛选和列表；
- `/stock` 负责详情；
- ChartSnapshot 是详情行情真源；
- market/watchlist 来源列表保持同源同序；
- direct 模式才是单列。
