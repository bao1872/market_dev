# 第一金字塔离线验证 Runbook

**用途**：在不启动 Scheduler/Worker、不执行盘后发布的前提下，验证第一金字塔（趋势→结构→动量→筹码共识）计算正确性。

**适用场景**：
- 本地开发验证
- 算法或契约变更后回归
- 跨入口一致性核验

**前置约束**：
- 不 Docker、不 Migration、不回填、不全市场任务
- 不启动 Scheduler/Worker
- 不写共享 PostgreSQL/Redis
- 测试库连接可用（或使用 `SKIP_ALEMBIC_UPGRADE=1` 跳过迁移）

## 1. 验证入口

### 1.1 模块自测（最快）

```bash
cd backend
.venv/bin/python -m app.services.first_pyramid_service
```

**预期输出**：
```
OK: TEST.MOCK 2026-04-24
  ordered: ['trend', 'structure', 'momentum', 'chip_consensus']
  algo: 1.0.0-phase-5b-1
  inputHash: sha256:...
  parameterHash: sha256:...
  trend.available: True
  structure.available: True
  momentum.available: True
  chipConsensus: ...
  statusText: DSA ... | Swing ... | Squeeze ... | Node ...
```

### 1.2 契约测试套件

```bash
cd backend
PURE_UNIT_TEST=1 \
.venv/bin/python -m pytest tests/test_first_pyramid_contract.py -v
```

**预期**：38 测试全部通过，覆盖：
- DTO 契约（ordered_dimensions、必选维度、chip_consensus 可选）
- 端到端（4 维度 available、段内成交量、事件新鲜度、状态文本顺序）
- 跨入口一致性（同输入同 hash、不同输入不同 hash、JSON 可序列化）
- 不变量（无 NaN/Inf、freshness >= 0、事件时间升序、确定性输出）
- golden fixture（上涨/下跌/横盘三种典型行情）
- 错误处理（空 bars、数据不足、None）
- PRD20 QM 映射（QM-01/02/12/40/60/61/62）

## 2. 自定义 fixture 验证

### 2.1 构造固定 fixture

```python
import numpy as np
import pandas as pd
from app.services.first_pyramid_service import compute_first_pyramid_snapshot

np.random.seed(42)
n = 100
dates = pd.date_range("2026-01-01", periods=n, freq="B")
close = 10.0 + np.cumsum(np.random.randn(n) * 0.1 + 0.08)  # 上涨
bars = pd.DataFrame({
    "open": close - np.random.rand(n) * 0.05,
    "high": close + np.random.rand(n) * 0.2,
    "low": close - np.random.rand(n) * 0.2,
    "close": close,
    "volume": np.random.randint(100000, 500000, n).astype(float),
    "amount": close * np.random.randint(100000, 500000, n).astype(float),
}, index=dates)

snap = compute_first_pyramid_snapshot(bars, symbol="TEST.UP", trade_date="2026-05-26")
print(snap.to_dict())
```

### 2.2 跨入口一致性验证

```python
# 同一输入两次调用，结果必须完全一致
s1 = compute_first_pyramid_snapshot(bars, symbol="TEST.UP")
s2 = compute_first_pyramid_snapshot(bars, symbol="TEST.UP")
assert s1.to_dict() == s2.to_dict()
assert s1.inputHash == s2.inputHash
assert s1.parameterHash == s2.parameterHash

# 不同输入必须产生不同 inputHash
s_down = compute_first_pyramid_snapshot(down_bars, symbol="TEST.DOWN")
assert s1.inputHash != s_down.inputHash
```

## 3. 只读真实样本验证（可选）

**约束**：最多 3 只股票、2 个区间；数据读入内存计算；不写库、不缓存。

```python
# 通过 SSH 隧道读取远程只读行情（不写入数据库）
from app.services.market_data_aggregation_service import MarketDataAggregationService

# 1. 建立 SSH 隧道（见 local-development.md）
# 2. 读取 3 只股票最近 250 根日线
# 3. 调用 compute_first_pyramid_snapshot
# 4. 检查输出合理性（不替代 golden 测试）
```

## 4. 验证清单

完成离线验证后，确认以下门槛：

- [ ] 模块自测输出 4 维度 available=True
- [ ] orderedDimensions = ['trend', 'structure', 'momentum', 'chip_consensus']
- [ ] statusText 包含 trend/structure/momentum 关键词，顺序正确
- [ ] 38 契约测试全部通过
- [ ] 同一输入两次调用 to_dict() 完全一致
- [ ] 不同输入产生不同 inputHash
- [ ] 趋势维度 continuousFactors 含 current_segment_volume_mean
- [ ] 结构维度 events 含 type/direction/freshnessBars
- [ ] 动量维度 continuousFactors 含 squeeze_on/sqzmom_val/bb_width
- [ ] chip_consensus 可为 None 或 DimensionResult（不阻塞前三维）
- [ ] 数据不足（< 60 根）抛 ValueError，不静默伪造

## 5. 故障排查

### 5.1 ImportError: cannot import name 'BBconfig'
- 原因：类名是 `BBcfg`（非 `BBconfig`）
- 解决：检查 `bollinger_features_plotly.py` 实际导出的类名

### 5.2 RuntimeError: 测试必须在 APP_ENV=test 下运行
- 原因：conftest.py 强制要求测试模式
- 解决：使用 `PURE_UNIT_TEST=1` 纯单元模式（或经 SSH 隧道连共享开发业务数据库的 `PANJI_SHARED_DEV_DB_TEST=1` 目标测试）

### 5.3 FileNotFoundError: alembic
- 原因：conftest.py 默认执行 alembic 迁移
- 解决：`export SKIP_ALEMBIC_UPGRADE=1` 跳过迁移（纯计算测试不需要）

### 5.4 Node Cluster 计算失败
- 现象：`chipConsensus` 为 None
- 原因：数据不足或 15m bars 缺失
- 解决：检查日志 `Node Cluster 计算失败`；前三维不受影响

## 6. 相关文档

- 设计契约：`docs/prd/20-quant-model.md`
- 实现 Map：`docs/maps/20-quant-model.md` §9.5
- 变更记录：`docs/changes/2026/CHANGE-20260727-004-first-pyramid-local-root.md`
- 本地开发：`docs/runbooks/local-development.md`
