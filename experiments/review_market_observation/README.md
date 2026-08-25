# 盘迹实验：Review Market Observation（复盘视角行情观察）

> 参考 PRD: `ref/panji_market_observation_experiment_prd_v1.0.md` (main repo)
> 参考 Prompt: `ref/prompt.md` Round 1 (main repo)
> 工作分支: `exp/review-market-observation-v1` (独立 worktree)
> 治理模式: Exploration（Fast Iteration，不代表盘迹正式发布）

## 实验目标

本轮只验证「**第一金字塔历史行情质量是否足够支撑复盘视角的行情观察**」，
**不做预测、不建立正式研究工具 UI、不改变任何生产代码/数据**。

**Round 2 scope/detail TBD after Round 1 audit.**
**No Round 2 implementation in this commit.**

## 工作区

```
experiments/review_market_observation/
├── round1/
│   ├── dataset_schema.py        冻结数据集 schema 定义（69 列）
│   ├── round1_extract.py        只读 DB 提取 frozen dataset + manifest（--end-date fail-closed）
│   └── round1_analyze.py        完整性 + 原语 + 过渡审计 + verdict + public summary
├── tests/
│   └── test_round1.py           21 个单元测试（PURE UNIT，不连 DB）
├── run_round1.sh                一键执行入口（diagnose/extract/analyze/all）
├── dataset_manifest_public.json §9 真实执行后生成：脱敏公开 manifest
├── ROUND1_SUMMARY.md            §16 真实执行后生成：人类可读摘要
├── ROUND1_SUMMARY_TEMPLATE.md   模板（未真实执行前的占位）
└── README.md                    本文件
```

大型真实产物 **不提交** Git（通过 `.gitignore` 忽略）：

```
_run/        EXP_OUTPUT_ROOT 默认目录：frozen parquet / checksums / audit JSON
data/        旧版 run_round1.sh 本地 data 输出位置
audit/       旧版 run_round1.sh 本地 audit 输出位置
```

公开小型证据 **必须提交** Git（§9 prompt）：
- `dataset_manifest_public.json`（SHA / 行×列 / 日期范围 / 版本 / 覆盖率 / verdict / 压缩后原语 & 过渡统计，无任何凭据）
- `ROUND1_SUMMARY.md`（分节的人类可读报告：Baseline→Frozen→Integrity→Primitive→Transition→Unexpected→Verdict）

## Round 1：原始数据 & 原语审计

目标：**把「发生了什么」拆成可重复、可溯源的原子事实**。

### 修正后关键执行原则（按 prompt.md）

§2.1 执行入口
```
python -m experiments.review_market_observation.round1.round1_extract \
  --data-dir <DIR> --dev-base-sha 6fc73842... --exp-sha <EXP_SHA> \
  --end-date YYYY-MM-DD          # §5 fail-closed，不默认 max(date)
```

§3 Universe（**禁止 survivorship bias**）
- 不使用 `instruments.status='listed'` 过滤历史样本
- Universe SSOT = `first_pyramid_history_daily_state` 在目标版本 × 目标日期窗口实际出现过的行
- `instruments` 表只 LEFT JOIN 补 symbol/name，不做样本裁剪

§4 Canonical version pin
- `EXPECTED_ALGORITHM_VERSION = 1.0.0-core-split`（WHERE 写死）
- `EXPECTED_HISTORY_CONTRACT_VERSION = review-history-v2`（WHERE 写死）
- **不自动回退旧版本**；120 日目标行数不足 → PARTIAL / INVALID 报告事实

§5 完整交易日选择 fail-closed
- **禁止**默认取 `max(trade_date)` 当 completed；必须显式 `--end-date`
- `--diagnose-recent=N` 先打印最近 N 天 row_count/valid_count/algo_match/hc_match，用于人工选 end-date
- 不发明 90%/95% 等完成阈值；未通过的日期就是 incomplete

§6 Manifest（14 字段 + 防 drift）
- `DEV_BASE_SHA EXP_SHA DATASET_ID TRADE_DATE_START TRADE_DATE_END TRADE_DATE_COUNT`
  `ROW_COUNT INSTRUMENT_COUNT ALGORITHM_VERSION HISTORY_CONTRACT_VERSION`
  `EXTRACTED_AT SCHEMA_HASH DATA_HASH`
- 提取器在每次运行时会 `git rev-parse HEAD` 并与 `--exp-sha` 对比；**不一致=STOP**
- DEV_BASE_SHA 固定为 `6fc7384228b2e51f13d3cf5af2a6b6a26b2837b0`；未显式授权不得 rebase

§7 只读证据（真实）
- 每个 DB 事务开头执行 `SET TRANSACTION READ ONLY` + `SHOW transaction_read_only`
- **硬断言 `transaction_read_only == 'on'`**；否则抛出 `ReadOnlyAssertionError` 终止
- 只使用 SELECT；无 INSERT/UPDATE/DELETE/CREATE/ALTER/DROP/migration

## 执行命令

### A. 本地 Mac：纯验证（不连 DB）

```bash
# (1) 21 pure unit
cd <worktree>/experiments
python -m pytest review_market_observation/tests/test_round1.py -v

# (2) Extractor dry-run（不连 DB，校验 SHA/参数/schema_hash）
cd <worktree>/experiments
python -m experiments.review_market_observation.round1.round1_extract \
  --data-dir /tmp/r1-tmp \
  --dev-base-sha 6fc7384228b2e51f13d3cf5af2a6b6a26b2837b0 \
  --exp-sha "$(git -C <worktree> rev-parse HEAD)" \
  --dry-run

# (3) Analyzer CLI help
python -m experiments.review_market_observation.round1.round1_analyze --help

# (4) Shell syntax
bash -n <worktree>/experiments/review_market_observation/run_round1.sh
```

### B. 远程服务器：真实 Round 1

```bash
# (0) 准备独立 worktree（不得触碰 /root/web_dev）
mkdir -p /root/.panji-experiments
cd /root/.panji-experiments
git -C /root/web_dev worktree add -f \
    /root/.panji-experiments/wt-r1-<PREEXEC_SHA> \
    <PREEXEC_SHA>
cd /root/.panji-experiments/wt-r1-<PREEXEC_SHA>
[[ "$(git rev-parse HEAD)" == "<PREEXEC_SHA>" ]] || { echo "SHA 不一致，STOP" >&2; exit 3; }

# (1) Diagnose — 先看最近 30 天完整度，人工选 end-date
cd experiments/review_market_observation
DATABASE_URL=postgresql://readonly-user:***@host:5432/bz_stock \
EXP_SHA=<PREEXEC_SHA> \
bash run_round1.sh diagnose
# 读取 recent_trade_dates_diagnose.json → 选一个已完整交易日

# (2) Extract + Analyze — 冻结 120 交易日 + 审计 + 写入 public summary
END_DATE=YYYY-MM-DD \
EXP_SHA=<PREEXEC_SHA> \
DATABASE_URL=postgresql://readonly-user:***@host:5432/bz_stock \
bash run_round1.sh all

# 审阅 audit dir + ROUND1_SUMMARY.md
cat /root/.panji-experiments/wt-r1-<SHA>/_run/r1/<SHA>/audit/round1_summary.json
cat /root/.panji-experiments/wt-r1-<SHA>/experiments/review_market_observation/ROUND1_SUMMARY.md
```

## 最低验证覆盖（§10 Prompt 要求）

| 测试 | 位置 |
|---|---|
| 21 pure unit（trade date / T-1 / dup / denominator / lookahead / canonical / hash / verdict） | `tests/test_round1.py` |
| Extractor CLI dry-run（含 DEV_BASE / EXP_SHA 校验 + schema hash 输出） | `round1_extract --dry-run` |
| Analyzer CLI help | `round1_analyze --help` |
| Shell `bash -n run_round1.sh` 无语法错 | `run_round1.sh` |

## 数据安全 & 可溯源

- **只读执行**：每次事务 `SET TRANSACTION READ ONLY`，并硬断言 `SHOW transaction_read_only=on`。
- **Frozen 不可更改**：`extracted_manifest.json` 记录 14 字段 lineage；`checksums.sha256` 存 parquet+manifest 校验；输出目录存在 frozen 文件时拒绝 overwrite（Clean state）。
- **不写回生产**：输出仅限 RUN_DIR（默认 `review_market_observation/_run/r1/<SHA>/` 或 `$EXP_OUTPUT_ROOT/...`）。
- **Public 证据无秘密**：`dataset_manifest_public.json` 只保留 SHA/日期/行数/版本/统计/verdict，绝不含 DATABASE_URL/凭据。
- **不影响主运行栈**：远程实验路径独立于 `/root/web_dev`；主分支/服务不改、不重启。
