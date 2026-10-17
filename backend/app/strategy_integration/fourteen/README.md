# 盘迹 14 模型集成交付包 V1

此目录可整体复制到正式项目。包含14个正式模型、冻结特征引擎、日线评分入口与SHA清单，不依赖研究缓存、pickle或scikit-learn。模型版本 `fourteen-models-v1-20261016`，研究数据截至2026-09-30。

先读 [接入契约](INTEGRATION.md)。完整研究结论在仓库的 `reports/fourteen_models_v1/REPORT.md`。

## 快速运行

使用 Python 3.11+；本次验证环境为 Python 3.12.14。

```sh
python -m pip install -r integration/panji_fourteen_v1/requirements.txt
python integration/panji_fourteen_v1/runtime.py --input daily.parquet --as-of 2026-09-30 --output scores.json
```

也支持CSV。输入列为 `datetime,open,high,low,close,volume`。`as-of` 是含当日日线收盘的日期，不能传入盘中时刻；返回 `decision_dt` 是实际可用数据日，可能早于所请求日期。

应用内调用（从本仓库根目录）；复制到正式项目后按目标包路径调整导入，实例化一次并复用。

```python
from integration.panji_fourteen_v1 import FourteenModels

models = FourteenModels()
result = models.score(daily_dataframe, as_of="2026-09-30")
```

同一进程可复用实例。当前实现每次重放输入历史，未提供增量性能保证；正式项目应在保留前缀一致性的前提下另做缓存和延迟验证。

## 验证

从仓库根目录执行，不需要本地市场数据或研究模型pickle：

```sh
python scripts/test_panji_fourteen_integration.py
```

已验证14任务覆盖、180个合成历史前缀、过时候选排除、独立目录加载、非法输入拒绝及文件篡改检测。导出时另将278个合成任务分数逐项对比本地实验模型，容差 `1e-12`，阈值完全一致。记录见 `research_runs/fourteen_models_v1/integration_qa.json`。

## 目录

- `runtime.py`：仅对输入截止日的因果候选评分，不读取研究标签或样本缓存。
- `feature_engine.py`：原冻结参考模块原样复制，SHA与实验一致。模块含研究辅助函数，但运行时只调用 `replay/features/causal_candidates`，不调用其标签或训练流程。
- `models/*.txt`：14个正式LightGBM模型；没有研究对照模型。
- `manifest.json`：模型、阈值、目标含义、131维特征顺序及逐文件SHA。加载时检查引擎、入口和模型文件。
- `INTEGRATION.md`：方向、有效性、条件评分、接入步骤和验收契约。

修改运行时代码后，应通过导出脚本重新生成SHA并重跑验证，不能直接修改文件后绕过清单校验。
