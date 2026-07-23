# 公开静态门户来源记录（SOURCE）

> 本文件记录 `frontend/public/portal/` 静态门户的来源与导入后调整，供审计与回滚使用。
> 对应变更：`docs/changes/records/CHANGE-20260723-006.md`

## 1. 源附件

| 项目 | 值 |
|---|---|
| 源 zip 名称 | `盘迹门户_完整版_Logo与需求摘要修正版.zip` |
| 源 zip 路径 | `ref/盘迹门户_完整版_Logo与需求摘要修正版.zip`（仅供人工阅读，禁止运行时依赖，见 AGENTS §七.8） |
| SHA256 | `4e89469330e0d5050a87a37305e7571bc67c401156c155bbebf0e1654192f523` |
| 附件版本 | 0.5.1 |
| 导入日期 | 2026-07-23 |

## 2. 导入文件范围（仅运行文件）

已导入：
- `index.html`（门户首页）
- `pages/*.html`（10 个说明页：quick-start / market / watchlist / stock-detail / alerts / messages / data / faq / boundaries / customization）
- `assets/css/site.css`
- `assets/js/site.js`
- `assets/data/factors.public.js`
- `assets/data/factors.public.json`
- `assets/images/wechat-qr-placeholder.svg`（占位图）
- `assets/images/logo_symbol_128.png`（从仓库批准资产 `frontend/src/assets/brand/logo_symbol_128.png` 复制，非附件资产）
- `content/site.json`
- `SOURCE.md`（本文件）

未导入（按 PRD §7 排除）：
- `QA_REPORT*` / `QA_STATIC.txt` / `BROWSER_QA.json`
- `README.md` / `ARCHITECTURE.md`
- `QUANT_CUSTOMIZATION_INTEGRATION.md`
- `scripts/patch_portal_nav.py`
- `CHANGELOG-0.5.1.txt`
- `assets/images/panji-logo.svg`（附件重绘资产，不作为运行品牌标识）
- 源 zip 本体与临时解压目录

## 3. 导入后调整

### 3.1 真实业务链接修正（清零死链接）
全站将 `href="#"` 死链接替换为真实系统路由：
- 「返回系统」→ `/market`
- 「登录盘迹」→ `/login`
- `alerts.html` / `messages.html` 的 product-nav「行情」→ `/market`，并移除「复盘」链接（复盘未正式上线，不得作为公开入口）

### 3.2 移除未批准死链接
- 移除 footer 中无批准文案的「隐私说明」链接（`<a href="#">隐私说明</a>`）

### 3.3 品牌资产替换
- 附件 `assets/images/panji-logo.svg` 是重绘资产，不作为运行品牌标识（PRD §6）。
- 11 个 HTML 页面统一引用批准资产 `assets/images/logo_symbol_128.png`（从 `frontend/src/assets/brand/logo_symbol_128.png` 复制），并保留相邻「盘迹」文字。

### 3.4 首页 base 标签
- 仅 `index.html` 的 `<head>` 增加 `<base href="/portal/">`，使首页相对路径（`assets/css/site.css`、`pages/*.html`、`index.html`）解析到 `/portal/` 下。
- 子页 `/portal/pages/*.html` 保持原有相对路径（`../assets/...`、`../pages/...`），不加 base 标签。
- 业务绝对路径（`/market`、`/login`）以根为基准，不受 base 影响。

### 3.5 内容真实性调整
- 文案符合当前系统事实：`/market` 统一行情工作区；自选为 `/market?scope=watchlist`；`/stock/:symbol` 唯一正式 K 线详情；`/messages` 消息历史。
- 权限/额度/监控资格由后端决定；飞书仅消息投递渠道；不荐股、不提供买卖时点、自动交易或收益保证。
- 附件已移除公开价格、名额和内测申请入口；`customization.html` / `data.html` 中出现的「价格」均为技术术语（价格结构、价格位置），非商业定价。
- `alerts.html` 是帮助说明页，不是新业务路由；提醒实际查看入口为 `/messages` 或 `/market?scope=watchlist`，未创建 `/alerts` 业务路由。
- 复盘 `/replay` 路由在代码中存在但仅在 SubscriberRoute 守卫内，门户不公开 `/replay` 入口。

## 4. 因子目录合同

`assets/data/factors.public.json` 与 `assets/data/factors.public.js` 关键字段一致：

| 字段 | 期望 | 实际 |
|---|---|---|
| version | `public-catalog-v1` | `public-catalog-v1` |
| generated_at | `2026-07-22` | `2026-07-22` |
| total | 240 | 240 |
| verified_current_core（PRD 称 current_core） | 24 | 24 |
| extended_catalog | 216 | 216 |
| categories | 15 | 15 |
| 唯一因子 ID | 240 | 240 |

- 24 条为已核验现有核心；216 条为扩展需求目录，是否可直接使用需项目评估。
- 仅展示名称、分类、状态、观察内容、原理、解释、用途和限制；不展示完整公式、源码、参数阈值、客户权重、买卖信号或收益承诺。

## 5. 需求摘要合同

`customization.html` 内联脚本实现：
- 已选因子：`state.selected` + `selectedList` 渲染
- 输出选项：6 个 checkbox（筛选列表 / 综合评分 / 历史回测 / 自选监控 / 网页图表 / 消息提醒）
- 新因子六项：`newObserve` / `newOutput` / `newLogic` / `newData` / `newExample` / `newScope`，空值显示「未填写」
- 复制：`navigator.clipboard.writeText` 不可用时回退 `document.execCommand('copy')`

## 6. 上线阻塞

**BLOCKED_EXTERNAL_ASSET：管理员真实微信二维码**

当前 `assets/images/wechat-qr-placeholder.svg` 为占位图。真实管理员微信二维码尚未提供。
- 允许使用占位图完成开发和测试。
- 未替换真实二维码前，PR 可以完成，但禁止生产上线。
