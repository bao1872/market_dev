"""无监督候选状态发现模块 — 分布审计、聚类、稳定性检验。

与生产完全隔离：只读 research_feature_matrix_rows 和 bars_daily，
不修改 API/前端/Worker/migration/snapshot。

子模块：
- data_access: 只读 SQL、分层抽样、分块迭代
- feature_builder: 基础归一化与时序派生特征
- distribution_audit: 分布、缺失、尾部、漂移、相关性审计
- preprocessing: winsorize、RobustScaler、横截面 rank、PCA
- models: MiniBatchKMeans、GMM、k 候选评估
- stability: bootstrap、时间切分、transition/dwell
- reporting: 小型 CSV/JSON/Markdown 输出 + 50MB 门禁
"""
