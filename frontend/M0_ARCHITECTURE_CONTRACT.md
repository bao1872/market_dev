# M0 Architecture Contract — 盘迹营销门户

> 状态：**PASS（Accept with amendments）** — 第 0 号问题裁定为 Route D，M1 已授权开工。
> 版本：v2（2026-09-09）。v1 的两项事实错误已在 §1 修正，本轮补充 §2 的生产路由事实。
> 原则：本文件只记录**已核实事实**与**裁定结论**。未决项标注 `⛔`，延期项标注 `DEFERRED`。

---

## 1. v1 事实勘误（保留，作为决策依据）

| 评审断言 | 核实 | 证据 |
|---|---|---|
| 已有 `features/first-pyramid/presentation.ts` | ❌ 不存在 | 实际散落 `stock-research/firstPyramidViewModel.ts`(410行)、`FirstPyramidPanel.tsx`(544行) |
| 已有 `ResearchPoster` 可复用 | ❌ 不存在 | 实际是 `components/MobileIndicatorStage.tsx`（1440×2560 9:16） |
| "Backend API 已齐" | ⚠️ 收窄 | `bars.py` L49 有 `require_instrument_market_access`，非匿名 |

**修正**：FirstPyramid 与海报组件的共享源为「**待新建**」，非「已有可复用」。

---

## 2. 生产路由事实（v2 新增，已核实）

### 2.1 Nginx 直接服务静态门户

`frontend/nginx.conf` L100-106：

```nginx
# ===== 公开产品官网（PANJI PUBLIC SITE V2, 2026-09-08）=====
# [CHANGE-20260908-001] 公开根路径 / 由 Help Center 改为产品官网
location = / {
    add_header Cache-Control "no-store, no-cache, must-revalidate";
    try_files /landing/index.html =404;
}
```

✅ **确认**：生产访问 `/` 时请求**不进入 React SPA**，Nginx 直接返回 `/landing/index.html`。
因此**仅修改 `App.tsx` 无法改变生产首页**——`frontend/nginx.conf` 必须纳入 CUTOVER touchpoint。

### 2.2 SPA fallback 天然支持 preview 路由

`nginx.conf` L137-140：

```nginx
location / {
    try_files $uri $uri/ /index.html;
}
```

✅ **确认**：`/marketing-preview` 等路径天然落入 SPA，无需改 Nginx 即可开发。

### 2.3 现有静态门户 SEO 状态（已核实）

`public/landing/index.html` 现有 meta：

| 项 | 状态 |
|---|---|
| `description` | ✅ `盘迹：从全市场发现变化，理解个股状态…` |
| `og:type` / `og:title` / `og:description` | ✅ |
| `og:image` | ✅ `/landing/assets/images/poster_img1.webp` |
| `twitter:card` | ✅ `summary_large_image` |
| `theme-color` | ✅ `#0A0F14` |
| **`canonical`** | ❌ **缺失** |
| **`og:url`** | ❌ **缺失** |

⚠️ CUTOVER 时需补 `canonical` 与 `og:url`（评审称"已有完整 SEO"，实际缺这两项）。

### 2.4 ⚠️ CUTOVER 风险点（评审未提及）

`nginx.conf` L86-92 的 Umami 注入仅作用于 `location = /index.html`：

```nginx
location = /index.html {
    sub_filter '</head>' '<script async src="/umami/script.js" ...></script></head>';
}
```

CUTOVER 后若 `/` 改为 `try_files /index.html`，**内部跳转可能不触发 `location = /index.html` 的 sub_filter**，导致官网首页丢失 Umami 统计。CUTOVER 时必须验证 Umami 是否仍注入，或改用其他注入方式。

---

## 3. 裁定：Route D — Shadow Migration + Controlled Cutover

**不采用 A（改造静态页）/ B（big-bang React 重写）/ C（永久双门户）。**

理由：C 会制造「两个同时公开、内容不同的官网」，导致 SEO authority 分裂、OG 不一致、Umami 数据拆散、内容长期漂移——这正是本契约要避免的「两个 Source of Truth」，只是从字段层面扩大到整个官网层面。

### Phase 1 — 生产零改动
`/` 继续由 Nginx 服务 `/landing/index.html`。现有官网继续提供正式服务。

### Phase 2 — 影子开发
新增 `/marketing-preview`（或 `/__marketing`）路由进入 `MarketingPage`，通过 SPA fallback 天然可达。完成 M1–M4，**不碰正式首页**。

### Phase 3 — Parity Gate（M4.5）
| Gate | 要求 |
|---|---|
| 内容 | 原有重要内容无丢失 |
| 移动端 | 375 / 430 可用 |
| Desktop | 1440 / 1920 正常 |
| 动画 | `reduced-motion` 正常 |
| CTA | 登录 / QQ / 雪球链接正常 |
| SEO | title / description / OG 完整（**含 canonical 与 og:url**） |
| 性能 | 首屏 bundle 可接受 |
| 视觉 | Playwright baseline PASS |
| 产品语义 | 无 BOS/CHoCH 等旧语义回流 |

### Phase 4 — 一次性切换（CUTOVER）
- 修改 `frontend/nginx.conf`：`/` → SPA；`/landing/index.html` → **301 → /**（不是继续公开）
- 验证 Umami sub_filter 仍生效（见 §2.4）
- 旧静态代码保留一个 release 周期作为 **rollback asset**，但不再作为第二官网公开
- 出问题回滚 Nginx / Docker image

---

## 4. 分项裁定

| 项 | 裁定 |
|---|---|
| 教学动画（StructureStory / ChipConsensusStory / StrategyLab / XueqiuToPanji） | ✅ **批准纯 deterministic**，且 demo 数据进 contract test 防视觉改动破坏逻辑 |
| First Pyramid 共享源 | ✅ 授权抽取，但**独立为 M3.5 refactor**；**M1 只做 Drawer shell，禁止复制 99 字段第二套定义** |
| `CaptureStockPage.tsx` / `MobileIndicatorStage.tsx` | ❌ **不授权修改**。M4 第一版直接用真实 capture 产出的静态图（`poster_img1.webp` / `poster_img2.webp` 已存在）。未来若确需组件复用，另立 refactor contract（含 1440×2560 像素回归） |
| 真实行情快照（原 M5） | `DEFERRED` — 默认不做，不阻塞 M0。仅当"Hero 无实时数字确实降低理解/转化"时才做 `/v1/public/marketing/snapshot` |
| SEO | 开发阶段 preview 不承担正式 SEO；CUTOVER 阶段完善 SPA root SEO + canonical + 301 |

---

## 5. 技术栈（锁死，零新增依赖）

React 18 + TypeScript + Vite 5 + **SCSS Modules** + lightweight-charts@^4.2 + React Query + `publicApiClient`（复用，不建第 4 个 axios 实例）。

设计 token 全部复用现有：`--bg #0A0F14` / `--panel` / `--brand #00F6C2` / `--up #FF4D4F` / `--down #22C55E` / `--text` / `--muted` / `--border`。

---

## 6. 目标 IA

| Section | 目的 | 数据 |
|---|---|---|
| `Nav` | 导航 | 静态 |
| `Hero` | 一句话解释盘迹 | 静态文案 |
| `Discovery` | 全市场 + 小Z说事两种机会入口 | deterministic |
| `Workflow` | 发现→筛选→理解→自选→跟踪 | deterministic |
| `StructureStory` | K 线推进产生结构变化动画 | `demo/structureStory.ts` |
| `ChipConsensusStory` | 成交累积产生筹码共识动画 | `demo/chipConsensusStory.ts` |
| `MarketLanguage` | 趋势/结构/动量/成交量/筹码/事件 | deterministic |
| `FirstPyramidDrawer` | 99 字段查询，默认隐藏 | **M1 shell / M3.5 接 registry** |
| `StrategyLab` | 道氏123 / 趋势跟踪 / 新趋势+异常放量 / 板块机会 | `demo/strategyScenarios.ts` |
| `XueqiuToPanji` | 小Z说事→板块→盘迹筛选 | `data/reviewExamples.ts` |
| `WatchAndNotify` | 自选→变化→飞书图片 | **静态 poster 图** |
| `Invitation` / `Footer` | QQ 邀请码 + 雪球 | 静态 |

### 6.1 公开门户边界（M1.1 修订）

- ❌ footer / nav 一律不得暴露 `/review`、`/auction`（不对外开放，门户不宣传）
- ❌ 不得再链回 `/portal/index.html`——使用说明已裁定融合进门户
- ✅ 产品入口收敛为：`行情 /market`、`自选 /market?scope=watchlist`
- ✅ 内容入口：雪球搜索「小Z说事」+ 「关注每日早晚复盘」文案（「复盘」仅作内容语境，不作为产品路由）
- 「第一金字塔」为**渐进披露**：不在主导航、不占 section index，仅在 MarketLanguage 底部保留低调入口 → 右侧 Drawer

---

## 7. 里程碑（v2 重排）

| 阶段 | 内容 | 出口 |
|---|---|---|
| **M0** | 本契约 + Route D 冻结 | ✅ 已完成 |
| **M1** | `/marketing-preview` 骨架：Nav / Hero / Discovery / Workflow / MarketLanguage / **Drawer shell** / Footer | `tsc -b` PASS |
| **M1.1** | 公开边界修正（去掉 `/review`、`/auction`、`/portal/index.html`）+ 第一金字塔渐进披露（去主导航、去 numbered section、改为右侧 Drawer）+ marketing contract 接入 `test:contract` | `npm run test:contract` PASS |
| **M2** | StructureStory + ChipConsensusStory（自动播放/上一步/下一步/暂停/reduced-motion/每阶段中文解释） | `tsc -b` + contract test |
| **M3** | StrategyLab + 小Z说事→板块→盘迹 | `tsc -b` + demo 数据 contract |
| **M3.5** | First Pyramid presentation registry 抽取（**Refactor only**：禁止改字段名/排序/分组/语义），原 contract 全 PASS 后 Marketing Drawer 接入 | 原契约全 PASS |
| **M4** | 自选→变化→飞书；**用真实 poster 静态图，不改 Capture** | `tsc -b` |
| **M4.5** | Parity Gate（视觉/响应式/性能/内容） | 全 gate PASS |
| **CUTOVER** | 改 `nginx.conf`；`/` → React；`/landing/index.html` → 301 `/`；补 canonical/og:url；验证 Umami | 生产验证 |
| **M5** | `DEFERRED` | — |

---

## 8. 改动边界（v2 修订）

**M1–M4 允许**：
1. 新增 `src/features/marketing/**`
2. `src/App.tsx` — **仅新增 `/marketing-preview` 公开路由**，不动 `/`、不动任何产品路由
3. `package.json` — **仅允许**向现有 `test:contract` 显式枚举**追加** marketing contract tests；禁止依赖变化、禁止其它 script 语义变化（M1.1 已追加 `src/features/marketing/__tests__/marketingCopy.test.ts`）

**M3.5 允许**：新增 `src/features/first-pyramid/presentation.ts`；改动 `stock-research/` 相关文件（refactor only）

**CUTOVER 允许**：`frontend/nginx.conf`、`index.html`（SEO meta）

**全程禁止**：
- 改 `/` 的现有行为（CUTOVER 前）
- 改 `CaptureStockPage.tsx` / `MobileIndicatorStage.tsx`
- backend 改动（M5 解除前）
- `docs/prd` / `docs/maps` / `docs/runbooks` / `rules/` 改动
- migration、bz_stock 写入、新建 branch、force push
- 新增 npm 依赖
- M1 复制 99 字段定义

---

## 8.5 KNOWN BASELINE TEST DEBT

**baseline SHA**：`879a042186ec2a2f592a57b1642e622a4aca5f56`

以下 2 项为**继承失败**（inherited failures），在 baseline SHA 上即已存在，与 Marketing 无关：

1. `P0-5: StrategyChart range/reset 按钮以 calc.length 为右边界`
2. `P0-5: StrategyChart 无 viewportProp 时回退到 createDefaultViewport(calc.length)`

成因：`scripts/contract-tests/viewport-reset.test.ts` 仍断言 `src/components/StrategyChart.tsx` 中的旧源码字符串（`createDefaultViewport(calc.length, initialVisibleBars)`、`if (viewportProp) return clampViewport(...)`），而该组件已演进；属 **STALE_TEST**，不是 RUNTIME_BUG。

规则：
- M2 及后续里程碑**禁止顺手修**这两项（不改 `StrategyChart.tsx`、不改 `viewport-reset.test.ts`）
- full `test:contract` **不得出现第三个失败**
- Marketing 自身 contract 必须全绿
- 该 stale contract 由**单独治理任务**处理

---

## 9. 已关闭的待办

- ✅ 第 0 号问题 → Route D
- ✅ 教学动画 deterministic → 批准
- ✅ First Pyramid 抽取 → 授权，独立 M3.5
- ✅ CaptureStockPage → 不授权，改用静态 poster
- ✅ M5 → DEFERRED，移出阻塞

**M1 已授权开工。**

---

## 10. Marketing Preview 轻部署通道（Preview Lane）

2026-09-09 用户裁定：门户影子页（`/marketing-preview`）迭代**不再走 `panji-test-deploy` whole-system runner**，改为独立轻通道，push 后直接部署供 Owner 视觉验收，不再等 ChatGPT UI PASS。

### 10.1 架构事实
- 生产 frontend 静态根：`/opt/panji-live/frontend/dist` 经 live bind mount → 容器 `/usr/share/nginx/html`。
- 因此 Preview 只需写入独立子目录：`/opt/panji-live/frontend/dist/marketing-preview/`，Nginx 经 live mount 立即可见，**无需 restart frontend、无需碰 backend/worker/database**。
- 正式 `/` 仍由 Nginx `location = /` 服务现有静态门户（`/landing/index.html`），CUTOVER 前不变。

### 10.2 构建入口
- 新增独立 root：`frontend/marketing-preview/index.html`（entry = `../src/features/marketing/preview.tsx`）。
- `frontend/src/features/marketing/preview.tsx`：仅 `createRoot(<MarketingPage/>)`，无 RouterProvider / QueryClientProvider / Toast / App.tsx（MarketingPage 为 fully deterministic 静态页，零产品运行时依赖）。
- `package.json` 脚本 `build:marketing-preview`：`vite build marketing-preview --config ./vite.config.ts --base /marketing-preview/ --outDir ../dist-marketing-preview`（复用现有 vite 配置，不重建整站 SPA，产物落 `frontend/dist-marketing-preview/`）。

### 10.3 部署入口
- `scripts/ops/panji-marketing-preview-deploy <FULL_SHA>`：唯一允许的 Preview 部署器。
- 服务器用独立 git worktree `/opt/panji-marketing-preview-src`（从目标 SHA `--detach`）构建，**不改 `/root/web_dev` 当前 production deployment state**。
- node_modules 走 Docker named volume `panji-marketing-preview-node-modules` 缓存；仅当 `package-lock.json` 哈希变化才 `npm ci`（node:20-alpine 容器内）。
- 部署顺序：先 `rsync` assets 再 `install` index（`preview-build.json` 记录 git_sha + scope）。
- 硬断言：主 SPA `index.html` sha256 与 `trading-frontend` 容器 `StartedAt` 部署前后一致（证明无改主 SPA、无重启）。
- 禁止：sccp/docker cp、改 backend/DB/RUNTIME_SHA/market.env/nginx.conf、restart 容器、执行 `panji-deploy.sh`。

### 10.4 保留预部署 Gate 的三类改动（不属 Preview Lane）
1. **涉及产品共用代码**（如 M3.5 `firstPyramidViewModel`/`FirstPyramidPanel`/共享 presentation registry）→ IDE push → 用户审 diff → 再部署。
2. **CUTOVER / backend / nginx / Capture**（如 `/` 正式切门户、`nginx.conf`、后端 API、数据库、`CaptureStockPage`、飞书真实产出链）→ 继续严格 Gate。
3. 普通 Marketing 改动（Hero/文案/StructureStory/ChipConsensusStory/StrategyLab/CSS/deterministic 数据）→ push 后直接部署 preview，Owner 先看。

### 10.5 版本编号
`Preview M2 (SHA f650c61d)` → Owner 视觉反馈 → `Preview M2.1 (SHA ...)` → ... → `Owner Visual PASS` → 才进入 M3。
