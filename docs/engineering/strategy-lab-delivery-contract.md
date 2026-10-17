# 策略 lab 交付接口（实现合同）

2026-10-17，本地代码已实现，尚未部署或通过真实交付验收。生产 owner 为
`backend/app/strategy_integration/lab_delivery.py`；模型加载与冻结运行合同仍归
`fourteen_adapter.py`，队列归 `job_store.py`，激活归 `release_store.py`。

## 包结构

ZIP 根目录直接放置现有 `PANJI_FOURTEEN_PORTABLE_V1` 包，不增加外层目录。
另包含 `delivery.json`、`validation_report.json`、`golden_cases.json`。
压缩及展开均不超过 128 MiB，最多 100 个条目；拒绝越界路径、符号链接、重名条目、
加密条目及未声明文件。不在 API 进程执行上传代码。

原生 manifest 必须声明 `price_input_contract: QFQ_ASOF_V1`，以及
`split_windows: {train: [start,end], val: [start,end], test: [start,end]}`。
十四任务、131 维列序、标签/适用域、执行代码与依赖文件必须匹配当前批准运行合同。
更新这些合同需要新的代码变更，不能靠上传包覆盖。

`delivery.json` 字段：

- `schema`: `PANJI_LAB_DELIVERY_V1`。
- `manifest_sha256`: manifest 文件原始字节 SHA256，必须与管理员独立填写的摘要相同。
- `lab_revision`: lab 的 40 位 Git SHA 或 64 位内容身份。
- `approved_for_production`: 必须为 true；这是 lab 交付声明，不替代生产人工激活。
- `files`: `validation_report.json`、`golden_cases.json` 原始字节 SHA256。

同一 manifest 身份不能替换批准声明或证据；需要保留原交付，不能静默覆盖。

## 验证报告与黄金样例

`validation_report.json.windows` 按 train/val/test 提供 `{start,end}`，严格不重叠，
与 manifest 切割完全一致，测试截止不晚于 data_end。
`tasks` 必须包含十四个完整任务 ID，每个任务都含 train/val/test 的
`n/precision/recall/auc`。不可用指标须显式为 null 并给出 `unavailable_reason`，不填零掩盖缺样本。

`golden_cases.json` 是 1–100 个样例组成的列表，总历史不超过 100000 根。
每例包含：

- `as_of`: 决策截止日。
- `bars`: 同口径日线记录，datetime/open/high/low/close/volume；单例有效前缀 21–20000 根。
- `features`: 按 manifest 列序的二维单行特征数组，缺失值使用 JSON null。
- `expected`: lab 冻结的 runtime.score 输出，至少包含 status/signals。

全部样例合计覆盖十四任务。隔离 ML 子进程用正式运行时校验特征、信号集合、锚点、阶段、
标签/条件、阈值及分数；数值比较 rtol/atol 均为 1e-10。不由生产重新生成 expected
冒充独立 lab 对账。测试里由正式运行时生成的合成样例只验证校验器，不能作为真实交付证据。

## 执行和门禁

页面按 512 KiB 分片 POST `/v1/admin/strategy-research/zigzag/deliveries/chunks`，额外传
`chunk_index`（从 0 开始）和 `total_bytes`；不修改共享反向代理的请求限制。相同 manifest 的
原始 ZIP 分片不可替换，可重传相同分片。最多四个未完成上传；仅清理超过 24 小时且有本 owner
标记的未完成临时分片。完整包验证后才允许入队。

具备适当 HTTP 限制的受控调用方也可 POST `/v1/admin/strategy-research/zigzag/deliveries`，使用 multipart 字段
`file`、`manifest_sha256`、`trade_date`。成功接收后通过既有 EVALUATE 队列执行；相同交付/日期
使用同一任务身份，失败须走原任务重试。HTTP 202 不代表评分对账或全市场评价已成功。

交付证据对账成功后，既有 producer 生成独立结构成熟/20-bar 路径成熟评价，manifest
记录 lab 身份和验证证据。管理员仍须核对完整评价 SHA，人工审核，再显式激活。
过渡 TRAIN 及旧协议评价不具备此新门禁的交付证据，不能被本轮当成已批准生产模型。

不改变当前远程运行时，不自动训练、审核、激活或切换指针。实际 lab 交付与全市场
冷启动/日增量/补跑性能证据仍待真实运行，不能以本文或纯测试代替。
