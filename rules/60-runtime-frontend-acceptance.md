# 60 Runtime、API 与前端技术闭环

## 1. 目的

本文件解决 Exploration 最容易被混淆的问题：

**“前端可见”不是“正确性验收”；但产品假设也不能只停留在后端测试。**

工程侧必须把正确结果送到前端消费层；用户负责最后的视觉与产品价值判断。

## 2. 工程技术闭环

当前 slice 涉及前端时，工程必须验证到：

`DB / source facts → service → persistence → API → frontend request → response schema → component binding`

至少确认：

- 真实数据已经产生；
- API 读取正确 canonical result；
- instrument / symbol / trade_date 一致；
- HTTP status 正确；
- response 字段存在且类型/nullability正确；
- 前端 hook/service 读取正确 endpoint；
- 前端组件消费正确字段；
- 不使用 mock 冒充真实数据；
- loading / unavailable / error 不会误显示为正常结果。

## 3. IDE 与用户责任边界

### IDE / 工程侧负责

- 业务逻辑；
- 代码逻辑；
- unit / contract / integration；
- Runtime；
- API；
- frontend 数据绑定；
- build/targeted frontend test；
- 提供访问路径与推荐样本。

### 用户负责

- 页面视觉是否合理；
- 信息密度；
- 文案是否符合研究习惯；
- 指标是否有信息增量；
- 产品/算法假设是否成立；
- 是否进入下一轮 PRD 调整。

IDE 不应把“用户先打开浏览器”作为完成工程技术验收的前置条件。

## 4. Exploration Runtime

Exploration Runtime 的目标是：

**证明当前 hypothesis slice 在真实环境和真实数据上工作。**

不自动要求：

- 全域 after-close；
- 九节点 fully_ready；
- 所有 Worker；
- 所有 API；
- 全市场；
- full release smoke。

## 5. 代表性样本

算法/状态逻辑迭代优先使用 25–60 个 intentional sample。

样本应覆盖适用状态，例如：

- strong trend；
- weak/down trend；
- range；
- high volatility；
- low volatility；
- BOS / CHoCH；
- squeeze / release；
- board leader / tail（若当前 slice 涉及 board）；
- insufficient history / edge；
- 用户熟悉的重点股票。

只有当方向被用户认可、需要验证市场覆盖或性能时，才升级全市场。

## 6. Runtime Success

对当前 slice 报告：

- exact SHA；
- target trade date；
- sample count / universe；
- success / failed / skipped；
- 关键字段 availability；
- API endpoint；
- frontend consumed fields；
- 已知异常。

不因为无关 enhancement 缺失把当前 slice 判失败。

例：

如果 H1 只验证 First Pyramid Core，则 Chip/Auction/Review 不应阻止 H1 Stock Detail 展示。

## 7. Frontend Acceptance Evidence

工程侧至少提供：

- 页面路由/访问方式；
- 推荐检查的 5–10 个代表股票；
- 对应 API；
- 关键字段；
- 前端 component/hook 绑定关系；
- build/test 结果。

如果已有浏览器自动化可低成本运行，可补充 Network/Console/route smoke；但 Exploration 不要求为了一个数据绑定任务搭建新的重型 E2E 框架。

## 8. Product Acceptance

用户确认后，hypothesis 可进入：

- REJECTED；
- ITERATE；
- VALIDATED。

只有 VALIDATED 才值得继续扩大该业务假设的工程投入。

## 9. STOP

当：

- 当前 slice correctness 通过；
- required tests 通过；
- 真实 runtime 通过；
- API/前端技术绑定通过；
- 用户已经可以开始产品判断；

立即 STOP。

不得自动进入下一个域。
