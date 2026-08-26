"""第一金字塔非筹码历史回补服务（[CHANGE-20260729-003] 核心与筹码解耦 - P0-11）。

设计目标（ref/instruction.md §三.11）：
1. **按个股为外层**：每只股票一次读完整可用日线，一次调用 history SSOT
2. **一次调用 history SSOT**：`compute_first_pyramid_history` 一次计算多日
   daily_state + 不可变 events，禁止逐日调用 snapshot
3. **保存最近 250 日 daily state 与不可变 events**：
   - daily_state: upsert 到 first_pyramid_history_daily_state（幂等）
   - events: insert on_conflict_do_nothing（不可变，重跑不覆盖）
4. **分批 25—50 股**：默认 batch_size=25，每批一个事务（commit + checkpoint）
5. **幂等重跑**：相同 (instrument_id, trade_date, algorithm_version) 重复执行
   只更新 daily_state 内容，events 不重复插入
6. **禁止回补 chip**：chip 由独立 after_close_chip_consensus job 异步处理

调用链：
    backfill_first_pyramid_history_batch
      └─ for each instrument:
           ├─ bar_repository.get_bars(1d, qfq, completed_only=True)
           ├─ compute_first_pyramid_history(bars, include_chip=False)
           ├─ upsert daily_state rows (on_conflict_do_update)
           └─ insert events (on_conflict_do_nothing)

约束：
- 本服务不读取/写入 chip 相关数据
- 本服务不调用 compute_first_pyramid_snapshot（逐日）
- 单股失败不阻塞其他股票，写入 failed_instruments 列表

模块自测：
    python -m app.services.first_pyramid_history_service
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.first_pyramid_history import (
    FirstPyramidHistoryDailyState,
    FirstPyramidHistoryEvent,
)
from app.models.first_pyramid_history_run import (
    HISTORY_RUN_FAILED,
    HISTORY_RUN_PARTIAL,
    HISTORY_RUN_RUNNING,
    HISTORY_RUN_SUCCEEDED,
    SCOPE_ALL_A_SHARE,
    FirstPyramidHistoryRun,
)
from app.models.first_pyramid_history_run_item import FirstPyramidHistoryRunItem
from app.schemas.first_pyramid import FIRST_PYRAMID_CORE_ALGORITHM_VERSION

logger = logging.getLogger(__name__)

# =============================================================================
# 常量
# =============================================================================

# 默认回补输出天数（与第一金字塔合同对齐）
_DEFAULT_OUTPUT_BARS = 250

# 默认批量大小（instruction 要求 25—50 股）
_DEFAULT_BATCH_SIZE = 25

# 单股失败阈值：超过则整体标 partial
_FAILURE_THRESHOLD = 0.3


def _apply_pit_normalization(bars: pd.DataFrame, history: dict[str, Any]) -> None:
    """[HISTORY-BACKFILL-PIT-01] 将 one-pass history 归一化到 date-specific PIT。

    从 bars.adj_factor 列提取因子序列，计算 K_t = anchor_factor / factor(t)，
    对 LINEAR_SCALE_COVARIANT 字段应用 post-rescale。

    anchor_factor = 全局 one-pass 的复权分母 = bars 中最新 adj_factor（与
    _fetch_db_only_daily_bars 的 adjustment_as_of=None 语义一致：MDAS 用
    latest_adj 做分母）。

    factor(t) = adj_factor 序列中 trade_date <= t 的最后一个（ffill，与
    adj_factor.py _compute_denominator_factor 同语义）。

    如果 bars 无 adj_factor 列或全 null → 无公司行为 → 跳过归一化。
    """
    from app.services.first_pyramid_service import normalize_history_result_to_pit

    if "adj_factor" not in bars.columns:
        return

    factor_series = bars["adj_factor"].dropna()
    if factor_series.empty:
        return

    # anchor_factor = 全局锚点的复权分母 = 最新 adj_factor
    anchor_factor = float(factor_series.iloc[-1])

    normalize_history_result_to_pit(
        history,
        factor_series=factor_series,
        anchor_factor=anchor_factor,
    )


# =============================================================================
# 主入口
# =============================================================================


async def backfill_first_pyramid_history_batch(
    session: AsyncSession,
    instrument_ids: Sequence[uuid.UUID],
    *,
    batch_size: int = _DEFAULT_BATCH_SIZE,
    output_bars: int = _DEFAULT_OUTPUT_BARS,
    progress_callback: Callable[..., Awaitable[None]] | None = None,
    _fetch_bars_func: Callable[..., Awaitable[pd.DataFrame | None]] | None = None,
    source_history_run_id: uuid.UUID | None = None,
    history_contract_version: str | None = None,
) -> dict[str, Any]:
    """[P0-11] 第一金字塔非筹码历史回补批量入口。

    按"个股为外层，一次调用 history SSOT"模式回补：
    1. 每只股票读取完整可用日线（point-in-time <= 今日，qfq 复权）
    2. 一次调用 compute_first_pyramid_history(bars, include_chip=False)
    3. 持久化最近 output_bars 日 daily_state（upsert）+ events（on_conflict_do_nothing）
    4. 分批提交事务，每批后回调 progress_callback（checkpoint）

    Args:
        session: 异步 DB 会话（由 caller 控制 commit/rollback 边界）
        instrument_ids: 待回补 instrument ID 列表
        batch_size: 每批 instrument 数（默认 25）
        output_bars: 输出最近 N 个有效日的 daily state（默认 250）
        progress_callback: 进度回调，接收 processed/total/succeeded/failed
        _fetch_bars_func: 测试注入的 bars 获取函数（生产留空，使用 bar_repository）

    Returns:
        统计信息 dict：
        {
            "total_count": int,
            "succeeded_count": int,
            "failed_count": int,
            "skipped_count": int,  # bars 不足或为空
            "status": "succeeded" | "failed" | "partial",
            "failed_instruments": list[dict],  # 失败详情
            "algorithm_version": str,
            "output_bars": int,
        }
    """
    # 延迟导入避免循环依赖
    from app.services.first_pyramid_service import compute_first_pyramid_history

    total = len(instrument_ids)
    succeeded_count = 0
    failed_count = 0
    skipped_count = 0
    failed_instruments: list[dict[str, Any]] = []

    for batch_start in range(0, total, batch_size):
        batch = list(instrument_ids[batch_start:batch_start + batch_size])
        for instrument_id in batch:
            try:
                # 1. 读取完整可用日线
                if _fetch_bars_func is not None:
                    bars = await _fetch_bars_func(instrument_id)
                else:
                    bars = await _fetch_history_daily_bars(instrument_id)

                if bars is None or bars.empty:
                    skipped_count += 1
                    failed_instruments.append({
                        "instrument_id": str(instrument_id),
                        "error": "daily bars 为空",
                    })
                    continue

                # 2. 一次调用 history SSOT（include_chip=False）
                history = compute_first_pyramid_history(
                    bars=bars,
                    symbol=str(instrument_id),
                    output_bars=output_bars,
                    include_chip=False,
                )

                # 2.2 PIT normalization（HISTORY-BACKFILL-PIT-01）
                _apply_pit_normalization(bars, history)

                # 3. 持久化 daily_state + events
                persisted = await _persist_history_result(
                    session=session,
                    instrument_id=instrument_id,
                    history=history,
                    algorithm_version=FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
                    source_history_run_id=source_history_run_id,
                    history_contract_version=history_contract_version,
                )

                if persisted["daily_state_count"] == 0:
                    skipped_count += 1
                    failed_instruments.append({
                        "instrument_id": str(instrument_id),
                        "error": "history daily_state 为空（可能 bars 长度不足）",
                    })
                    continue

                succeeded_count += 1
            except Exception as exc:
                failed_count += 1
                failed_instruments.append({
                    "instrument_id": str(instrument_id),
                    "error": str(exc)[:500],
                })
                logger.error(
                    "[HistoryBackfill] instrument_id=%s 回补失败: %s",
                    instrument_id, exc, exc_info=True,
                )

        # 每批 commit + checkpoint
        try:
            await session.commit()
        except Exception as exc:
            await session.rollback()
            logger.error(
                "[HistoryBackfill] batch commit 失败 batch_start=%s: %s",
                batch_start, exc, exc_info=True,
            )
            raise

        if progress_callback is not None:
            try:
                await progress_callback(
                    processed=min(batch_start + len(batch), total),
                    total=total,
                    succeeded=succeeded_count,
                    failed=failed_count,
                    skipped=skipped_count,
                )
            except Exception as exc:
                logger.warning(
                    "[HistoryBackfill] progress_callback 失败: %s", exc,
                )

    # 统计状态
    if failed_count == 0 and succeeded_count > 0:
        status = "succeeded"
    elif succeeded_count == 0 and failed_count > 0:
        status = "failed"
    elif succeeded_count > 0 and failed_count > 0:
        status = "partial"
    else:
        status = "failed"  # 全部 skipped

    result = {
        "total_count": total,
        "succeeded_count": succeeded_count,
        "failed_count": failed_count,
        "skipped_count": skipped_count,
        "status": status,
        "failed_instruments": failed_instruments,
        "algorithm_version": FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
        "output_bars": output_bars,
    }

    logger.info(
        "[HistoryBackfill] 批量回补完成: total=%d, succeeded=%d, failed=%d, skipped=%d, status=%s",
        total, succeeded_count, failed_count, skipped_count, status,
    )

    return result


# =============================================================================
# Run/Item 接入版（CHANGE-20260729-008）
# =============================================================================


_HISTORY_ITEM_PENDING = "pending"
_HISTORY_ITEM_RUNNING = "running"
_HISTORY_ITEM_SUCCEEDED = "succeeded"
_HISTORY_ITEM_FAILED = "failed"
_HISTORY_ITEM_SKIPPED = "skipped"

# 默认 lease 时长（秒），单股 history 计算通常 < 60s
_HISTORY_ITEM_LEASE_SECONDS = 300

# 最大重试次数
_HISTORY_MAX_ATTEMPT_COUNT = 3


# =============================================================================
# Skip reason contract（CHANGE-20260809 / Phase 4B.1）
# =============================================================================
#
# Stage A 的 skipped 是 execution outcome 的一部分：某些 instrument 天然无法产出
# canonical history（历史 bar 不足 / provider 完全无日线数据）。这些 skip 不损害
# 已产出 canonical state 的 PIT 正确性，因此对 Stage B 是 non-blocking 的。
#
# 但「skip 是 non-blocking」必须是**显式白名单**，不能因为 failed==0 就默认放行：
# 未知 skip 原因可能代表 systemic data gap 或未预期排除，此时 run 不得作为
# canonical source 被消费。
#
# 分类结果只有三种，UNKNOWN 一律视为 canonical-blocking。

HISTORY_SKIP_INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
HISTORY_SKIP_NO_DAILY_BARS = "NO_DAILY_BARS"
HISTORY_SKIP_UNKNOWN = "UNKNOWN"

# 已知 non-blocking skip category（显式白名单）
ALLOWED_NON_BLOCKING_SKIP_CATEGORIES: frozenset[str] = frozenset(
    {
        HISTORY_SKIP_INSUFFICIENT_HISTORY,
        HISTORY_SKIP_NO_DAILY_BARS,
    }
)

# legacy reason 文本（历史 run 已经写入 DB，无法重写）→ 显式兼容读取。
# 注意：这是**精确前缀匹配**，不是「包含任意子串即通过」。
_LEGACY_NO_DAILY_BARS_REASONS: tuple[str, ...] = (
    "daily bars 为空（DB-only）",
    "daily bars 为空",
)


def classify_history_skip_reason(reason: str | None) -> str:
    """将 Stage A run item 的 skip reason 归类为已知 category。

    [CHANGE-20260809] Phase 4B.1 canonical readiness contract 的组成部分。

    只识别显式已知形态；任何无法识别的字符串（含 None / 空串）都返回 UNKNOWN，
    由调用方 fail closed。禁止 hardcode 具体 symbol。
    """
    if reason is None:
        return HISTORY_SKIP_UNKNOWN
    text = reason.strip()
    if not text:
        return HISTORY_SKIP_UNKNOWN
    if text.startswith(HISTORY_SKIP_INSUFFICIENT_HISTORY):
        return HISTORY_SKIP_INSUFFICIENT_HISTORY
    if text.startswith(HISTORY_SKIP_NO_DAILY_BARS):
        return HISTORY_SKIP_NO_DAILY_BARS
    for legacy in _LEGACY_NO_DAILY_BARS_REASONS:
        if text.startswith(legacy):
            return HISTORY_SKIP_NO_DAILY_BARS
    return HISTORY_SKIP_UNKNOWN


def _compute_parameter_hash(
    output_bars: int,
    include_chip: bool,
    history_contract_version: str,
) -> str:
    """计算历史回补参数 hash。

    [CHANGE-20260808] 必须纳入 history_contract_version，使 review-history-v1 与
    review-history-v2 不能复用同一个 succeeded history run（parameter_hash 不同）。
    """
    raw = (
        f"output_bars={output_bars};include_chip={include_chip};"
        f"history_contract_version={history_contract_version}"
    )
    return hashlib.md5(raw.encode()).hexdigest()[:16]


async def create_history_run(
    session: AsyncSession,
    *,
    algorithm_version: str,
    output_bars: int,
    scope: str,
    instrument_ids: Sequence[uuid.UUID],
    scheduler_job_run_id: uuid.UUID | None = None,
    include_chip: bool = False,
) -> tuple[FirstPyramidHistoryRun, bool]:
    """创建历史回补 run（幂等：相同 algorithm_version + parameter_hash + scope 已有 running/succeeded 则返回已有）。

    Args:
        session: 异步 DB 会话（caller 控制 commit）
        algorithm_version: 算法版本
        output_bars: 输出最近 N 日
        scope: 范围标识
        instrument_ids: eligible universe（用于 expected_count）
        scheduler_job_run_id: 关联 SchedulerJobRun（可选）
        include_chip: 是否含 chip（默认 False）

    Returns:
        (FirstPyramidHistoryRun, is_new)
    """
    # [CHANGE-20260808] 局部 import 避免循环依赖；HISTORY_CONTRACT_VERSION 进入 run identity
    from app.services.first_pyramid_service import HISTORY_CONTRACT_VERSION

    parameter_hash = _compute_parameter_hash(
        output_bars, include_chip, HISTORY_CONTRACT_VERSION,
    )

    # 幂等查找：同 algorithm_version + parameter_hash + scope 的活跃 run
    existing_stmt = (
        select(FirstPyramidHistoryRun)
        .where(
            FirstPyramidHistoryRun.algorithm_version == algorithm_version,
            FirstPyramidHistoryRun.parameter_hash == parameter_hash,
            FirstPyramidHistoryRun.scope == scope,
            FirstPyramidHistoryRun.status.in_(
                (HISTORY_RUN_RUNNING, HISTORY_RUN_PARTIAL, HISTORY_RUN_SUCCEEDED),
            ),
        )
        .order_by(FirstPyramidHistoryRun.created_at.desc())
        .limit(1)
    )
    existing = (await session.execute(existing_stmt)).scalar_one_or_none()
    if existing is not None:
        return existing, False

    run = FirstPyramidHistoryRun(
        scheduler_job_run_id=scheduler_job_run_id,
        algorithm_version=algorithm_version,
        parameter_hash=parameter_hash,
        output_bars=output_bars,
        scope=scope,
        expected_count=len(instrument_ids),
        succeeded_count=0,
        failed_count=0,
        skipped_count=0,
        status=HISTORY_RUN_RUNNING,
        started_at=datetime.now(UTC),
        # [CHANGE-20260808] history_contract_version 进入 run metadata
        metadata_json=json.dumps(
            {"history_contract_version": HISTORY_CONTRACT_VERSION},
            sort_keys=True,
        ),
    )
    session.add(run)
    await session.flush()
    return run, True


async def ensure_current_first_pyramid_history_run(
    session: AsyncSession,
    *,
    algorithm_version: str | None = None,
    scope: str = SCOPE_ALL_A_SHARE,
    output_bars: int = 250,
    include_chip: bool = False,
    instrument_ids: Sequence[uuid.UUID] | None = None,
    scheduler_job_run_id: uuid.UUID | None = None,
) -> tuple[FirstPyramidHistoryRun, bool]:
    """[CHANGE-20260821-001 Phase 1] 解析或创建「今天盘后」当前 canonical FirstPyramidHistoryRun。

    PRODUCER CURRENT-RUN RESOLVER —— 与 Review consumer resolver 完全独立。

    确定当前 canonical 配置：algorithm_version（默认核心版本
    ``FIRST_PYRAMID_CORE_ALGORITHM_VERSION``）、parameter_hash（由 output_bars +
    include_chip + HISTORY_CONTRACT_VERSION 经 ``_compute_parameter_hash`` 计算，
    contract 进入 identity）、scope（默认 ``all_a_share``）。

    通过既有合法创建契约 ``create_history_run`` 幂等 resolve-or-create：
    - 相同 (algorithm_version, parameter_hash, scope) 已存在 running/partial/succeeded
      run → 返回已有 run（is_new=False，resume，不新建、不硬编码任何 run id）。
    - 不存在 → 创建新 run（is_new=True），identity 由上述三元组决定，绝不硬编码 UUID。

    硬边界（见 PRD 80 / CHANGE-20260821-001，REVIEW_CODE_FREEZE=TRUE）：
    - **不调用** Review 的 ``validate_canonical_history_run_readiness`` /
      ``_resolve_canonical_history_source``；producer 仅确保 canonical run 行存在，
      readiness 由 Review 自行判定（FIX_DIRECTION=UPSTREAM_ONLY）。
    - algorithm_version / HISTORY_CONTRACT_VERSION / output_bars 任一变化 →
      resolve key 改变 → 旧 run 不被复用（rollover 不复用旧 run）。
    - 本阶段**不计算、不修改** participating set（membership reconciliation 属
      Phase 2）；instrument_ids 仅用于 expected_count 进度展示，不构成 membership
      定义，默认空（caller 在 Phase 2+ 才传入真实 eligible universe）。

    Args:
        session: 异步 DB 会话（caller 控制 commit）
        algorithm_version: 算法版本（默认 FIRST_PYRAMID_CORE_ALGORITHM_VERSION）
        scope: 范围标识
        output_bars: 输出最近 N 日（默认 250）
        include_chip: 是否含 chip（默认 False）
        instrument_ids: eligible universe（可选，仅用于 expected_count；membership 不在此解决）
        scheduler_job_run_id: 关联 SchedulerJobRun（可选）

    Returns:
        (FirstPyramidHistoryRun, is_new)
    """
    if algorithm_version is None:
        algorithm_version = FIRST_PYRAMID_CORE_ALGORITHM_VERSION

    run, is_new = await create_history_run(
        session,
        algorithm_version=algorithm_version,
        output_bars=output_bars,
        scope=scope,
        instrument_ids=instrument_ids or (),
        scheduler_job_run_id=scheduler_job_run_id,
        include_chip=include_chip,
    )
    return run, is_new


async def create_history_run_items(
    session: AsyncSession,
    history_run_id: uuid.UUID,
    instrument_ids: Sequence[uuid.UUID],
    *,
    input_hash: str | None = None,
) -> int:
    """为 eligible universe 创建 history/pending items（幂等）。

    使用 INSERT ON CONFLICT DO NOTHING 保证并发安全。
    """
    if not instrument_ids:
        return 0

    # 查找已存在的 items
    existing_stmt = (
        select(FirstPyramidHistoryRunItem.instrument_id)
        .where(
            FirstPyramidHistoryRunItem.history_run_id == history_run_id,
            FirstPyramidHistoryRunItem.instrument_id.in_(instrument_ids),
        )
    )
    existing_ids = {
        row[0] for row in (await session.execute(existing_stmt))
    }

    new_items = []
    for instrument_id in instrument_ids:
        if instrument_id in existing_ids:
            continue
        new_items.append(FirstPyramidHistoryRunItem(
            history_run_id=history_run_id,
            instrument_id=instrument_id,
            status=_HISTORY_ITEM_PENDING,
            input_hash=input_hash,
        ))

    if new_items:
        session.add_all(new_items)
        await session.flush()

    return len(new_items)


# =============================================================================
# [CHANGE-20260821-001 Phase 2] Membership reconciliation
# =============================================================================
# 不新增任何 schema / 字段 / enum：membership 完全由「当前 eligible universe 查询」
# 与「canonical run 已有 run_items」派生。no-longer-current 通过从派生参与集排除表达，
# 其历史 item 保留，禁止物理删除。
# RUN-LEVEL COUNTER INVARIANT：reconcile 后 run.expected_count = 该 run 累计 run_item 总数
# （不更新 succeeded_count/skipped_count，后者属 Phase 3）。REVIEW_CODE_FREEZE=TRUE：本区与 Review 完全独立。

@dataclass
class MembershipPartition:
    """[CHANGE-20260821-001 Phase 2] 纯分区结果（不依赖 DB / ORM）。

    输入为普通 dict/set，便于纯单元测试覆盖全部 membership 生命周期场景。
    """

    retained: list[uuid.UUID]                 # 仍在 universe 且已有 item（保留 lineage）
    added: list[uuid.UUID]                     # 新成员：应建 pending item（NOT daily-ready）
    no_longer_current: list[uuid.UUID]         # 退出 universe：保留历史 item，排除出参与集（不删除）
    no_longer_current_nonterminal: list[uuid.UUID]  # 退出 universe 且仍为非终态(pending/failed/running)：
                                                    # 须进入 RUN_TERMINALIZATION_SET，不得静默遗弃；本阶段不改其 status
    reevaluation_candidates: list[uuid.UUID]   # failed / skipped 且仍在 universe → 可复评（候选识别）
    skipped_reevaluation_candidates: list[uuid.UUID]  # 仅 skipped 且仍在 universe（复评候选，独立于 rearm 策略）
    rearmed_skipped: list[uuid.UUID]           # 实际重置为 pending 的 skipped（= 候选 ∩ 授权集；默认空）
    daily_ready: list[uuid.UUID]               # 在 universe 且 status==succeeded（bootstrap 完成）
    not_daily_ready: list[uuid.UUID]           # 在 universe 但非 succeeded（含新增 pending / failed / 复评）
    current_expected_participating_set: list[uuid.UUID]  # = retained + added（daily advance 参与集）


def compute_membership_partition(
    existing: Mapping[uuid.UUID, str],
    eligible_instrument_ids: Iterable[uuid.UUID],
    *,
    rearm_skipped: bool = False,
    reevaluate_instrument_ids: Iterable[uuid.UUID] | None = None,
) -> MembershipPartition:
    """纯函数：给定 (instrument_id -> status) 与 eligible universe，计算 membership 分区。

    所有成员状态均由现有 status enum 派生，不引入 inactive / daily_ready 等 schema 变化。

    Args:
        existing: history_run 已有 run_items 的 (instrument_id -> status)
        eligible_instrument_ids: 当前 eligible A-share universe
        rearm_skipped: 是否将**全部** in-universe skipped 重置为 pending（复评入口）。
            默认 False：不无条件全量复评（避免每天无意义 churn）。
        reevaluate_instrument_ids: 仅重置这些 instrument 的 skipped（候选 ∩ 本集合）。
            与 rearm_skipped 互斥优先级：本参数非空时仅按本集合；否则看 rearm_skipped。

    Returns:
        MembershipPartition
    """
    eligible_set = set(eligible_instrument_ids)

    retained: list[uuid.UUID] = []
    added: list[uuid.UUID] = []
    no_longer_current: list[uuid.UUID] = []
    for iid in eligible_set:
        if iid in existing:
            retained.append(iid)
        else:
            added.append(iid)
    for iid in existing:
        if iid not in eligible_set:
            no_longer_current.append(iid)

    # 退出 universe 且非终态：必须进入 RUN_TERMINALIZATION_SET，不能静默遗弃。
    # 本阶段仅**暴露**（不改 status）。三个集合必须分清，绝不混用：
    #   - cumulative lineage membership  = 全部 run_items            → expected_count（run-level counter）
    #   - current daily eligible set     = eligible ∩ 有 item         → current_expected_participating_set（今天需 T-state）
    #   - run terminalization set        = no_longer_current_nonterminal（含已退市但仍 pending/failed/running 的历史成员）
    # 不发明新的 skip reason（如 NO_LONGER_ELIGIBLE）：Review readiness 对 skip reason 有白名单，
    # 新增 reason 会改变 consumer eligibility contract，违反 REVIEW_CODE_FREEZE。
    # 这些历史成员应随真实历史数据被 Phase 3 处理到合法 succeeded / 既有允许 skipped，而非为 membership 管理伪造 skip 原因。
    _NON_TERMINAL = frozenset(
        {_HISTORY_ITEM_PENDING, _HISTORY_ITEM_FAILED, _HISTORY_ITEM_RUNNING}
    )
    no_longer_current_nonterminal = [
        iid for iid in no_longer_current if existing.get(iid) in _NON_TERMINAL
    ]

    # 候选识别（始终报告，不依赖 rearm 策略）：failed / skipped 且仍在 universe
    # （退出 universe 的不复评、不重置）
    reevaluation_candidates = [
        iid for iid, st in existing.items()
        if st in (_HISTORY_ITEM_FAILED, _HISTORY_ITEM_SKIPPED) and iid in eligible_set
    ]
    # 仅 skipped 且仍在 universe 的复评候选（独立于 rearm 策略，便于上层识别可复评集合）
    skipped_reevaluation_candidates = [
        iid for iid, st in existing.items()
        if st == _HISTORY_ITEM_SKIPPED and iid in eligible_set
    ]

    # 实际 rearm（候选 ∩ 授权集）：
    # - reevaluate_instrument_ids 非空 → 仅重置其中的 skipped（精确授权）
    # - 否则 rearm_skipped=True → 重置全部 in-universe skipped
    # - 默认（rearm_skipped=False 且无 reevaluate 列表）→ 不重置任何 skipped
    # （避免每天把 skipped 全量重开 → Review 临时 not_ready → 重算仍 skipped 的无意义 churn）
    skip_candidates = set(skipped_reevaluation_candidates)
    if reevaluate_instrument_ids is not None:
        rearm_targets = skip_candidates & set(reevaluate_instrument_ids)
    elif rearm_skipped:
        rearm_targets = skip_candidates
    else:
        rearm_targets = set()
    rearmed_skipped = sorted(rearm_targets, key=str)

    # 派生最终 status（模拟 rearm 后）用于 daily_ready 判定
    final_status: dict[uuid.UUID, str] = dict(existing)
    for iid in rearmed_skipped:
        final_status[iid] = _HISTORY_ITEM_PENDING

    daily_ready = [
        iid for iid in eligible_set
        if final_status.get(iid) == _HISTORY_ITEM_SUCCEEDED
    ]
    not_daily_ready = [
        iid for iid in eligible_set
        if final_status.get(iid) != _HISTORY_ITEM_SUCCEEDED
    ]

    return MembershipPartition(
        retained=sorted(retained, key=str),
        added=sorted(added, key=str),
        no_longer_current=sorted(no_longer_current, key=str),
        no_longer_current_nonterminal=sorted(no_longer_current_nonterminal, key=str),
        reevaluation_candidates=sorted(reevaluation_candidates, key=str),
        skipped_reevaluation_candidates=sorted(skipped_reevaluation_candidates, key=str),
        rearmed_skipped=sorted(rearmed_skipped, key=str),
        daily_ready=sorted(daily_ready, key=str),
        not_daily_ready=sorted(not_daily_ready, key=str),
        current_expected_participating_set=sorted(retained + added, key=str),
    )


@dataclass
class MembershipReconciliationResult:
    """[CHANGE-20260821-001 Phase 2] reconcile_first_pyramid_history_membership 返回值。"""

    history_run_id: uuid.UUID
    partition: MembershipPartition
    expected_count: int = 0  # reconcile 后该 run 的累计 lineage membership 计数（RUN-LEVEL COUNTER INVARIANT）


async def reconcile_first_pyramid_history_membership(
    session: AsyncSession,
    *,
    history_run_id: uuid.UUID,
    eligible_instrument_ids: Sequence[uuid.UUID],
    rearm_skipped: bool = False,
    reevaluate_instrument_ids: Sequence[uuid.UUID] | None = None,
) -> MembershipReconciliationResult:
    """[CHANGE-20260821-001 Phase 2] 计算并落地 canonical run 的 membership partition。

    MEMBERSHIP RECONCILIATION OWNER —— 与 Review 完全独立（REVIEW_CODE_FREEZE=TRUE）。

    落地动作（全部 scope 到 history_run_id，不串其它 run lineage）：
    - added → 委托既有 ``create_history_run_items`` 幂等建 pending item（NOT daily-ready）。
    - rearmed_skipped → UPDATE skipped→pending 仅限 in-universe（复评入口）。
    - no_longer_current → 不删除、不改 status，仅由派生参与集排除。
    - no_longer_current_nonterminal → partition 中暴露退出 universe 且仍非终态(pending/failed/running)
      的历史成员（RUN_TERMINALIZATION_SET）；本阶段仅暴露、不改 status，供 Phase 3 terminalize。
      绝不发明新 skip reason（如 NO_LONGER_ELIGIBLE）来"管理" membership，那会改 Review consumer
      eligibility contract（违反 REVIEW_CODE_FREEZE）。

    硬边界（REVIEW_CODE_FREEZE / FIX_DIRECTION=UPSTREAM_ONLY）：
    - **不修改** Review 任何代码 / contract；不调用 ``validate_canonical_history_run_readiness``。
    - **RUN-LEVEL COUNTER INVARIANT（被你纠正，本阶段必须维护）**：reconcile 后
      ``run.expected_count == 该 canonical run 已纳入 lineage 的 run_item 总数``（累计 membership，
      非“今天仍上市的股票数”）。新增成员使 count 增加；退出 universe 不删 item → count 不降。
      Review readiness 依赖 ``expected_count == succeeded_count + skipped_count``；REVIEW_CODE_FREEZE
      仅禁止修改 Review 的判定逻辑，**不禁止 producer 正确维护 Review 依赖的数据**，否则新 run 会
      永久 ``expected_count=0`` 而永远过不了现有 readiness（属 Phase 1/2 上游 bug，不是 Review bug）。
      **本阶段不更新** ``succeeded_count`` / ``skipped_count`` —— 它们随 Phase 3 bootstrap/process
      完成后的真实 item 状态刷新（run-progress lifecycle，属 Phase 3 关注）。
    - **不 bootstrap / 不推进到 T**：new member 仅建 pending item，NOT daily-ready，直到
      Phase 3 required lookback bootstrap + canonical lineage persistence 成功。

    Args:
        session: 异步 DB 会话（caller 控制 commit）
        history_run_id: 目标 canonical FirstPyramidHistoryRun.id
        eligible_instrument_ids: 当前 eligible A-share universe（caller 提供，本函数不查询 universe）
        rearm_skipped: 是否将**全部** in-universe skipped 重置为 pending（复评入口），默认 False。
        reevaluate_instrument_ids: 仅重置这些 instrument 的 skipped（精确授权）；非空时优先于
            rearm_skipped。Phase 2 提供复评**能力**，但不把“能力”变成“每天无条件全量复评”。

    Returns:
        MembershipReconciliationResult
    """
    # 1. 载入本 run 已有 items（scope 到 history_run_id，不串其它 run lineage）
    existing_rows = (
        await session.execute(
            select(
                FirstPyramidHistoryRunItem.instrument_id,
                FirstPyramidHistoryRunItem.status,
            ).where(FirstPyramidHistoryRunItem.history_run_id == history_run_id)
        )
    ).all()
    existing: dict[uuid.UUID, str] = {row[0]: row[1] for row in existing_rows}

    # 2. 纯分区
    partition = compute_membership_partition(
        existing, eligible_instrument_ids,
        rearm_skipped=rearm_skipped,
        reevaluate_instrument_ids=reevaluate_instrument_ids,
    )

    # 3. 新成员：幂等建 pending item（委托既有契约，不硬编码任何 id）
    if partition.added:
        await create_history_run_items(session, history_run_id, partition.added)

    # 4. 复评入口：in-universe skipped → pending（仅 UPDATE，不删不改其它字段）
    if partition.rearmed_skipped:
        await session.execute(
            update(FirstPyramidHistoryRunItem)
            .where(
                FirstPyramidHistoryRunItem.history_run_id == history_run_id,
                FirstPyramidHistoryRunItem.instrument_id.in_(partition.rearmed_skipped),
                FirstPyramidHistoryRunItem.status == _HISTORY_ITEM_SKIPPED,
            )
            .values(status=_HISTORY_ITEM_PENDING, updated_at=datetime.now(UTC))
        )
        await session.flush()

    # 5. RUN-LEVEL COUNTER INVARIANT：expected_count = 累计 lineage membership 计数
    #    （新增 item 使 count 增加；no-longer-current 不删 item → count 不降）。
    #    不碰 succeeded_count / skipped_count（属 Phase 3 run-progress lifecycle）。
    total_items = (
        await session.execute(
            select(func.count())
            .select_from(FirstPyramidHistoryRunItem)
            .where(FirstPyramidHistoryRunItem.history_run_id == history_run_id)
        )
    ).scalar_one()
    run = (
        await session.execute(
            select(FirstPyramidHistoryRun)
            .where(FirstPyramidHistoryRun.id == history_run_id)
        )
    ).scalar_one()
    run.expected_count = total_items
    session.add(run)
    await session.flush()

    return MembershipReconciliationResult(
        history_run_id=history_run_id, partition=partition,
        expected_count=total_items,
    )


async def claim_history_items(
    session: AsyncSession,
    history_run_id: uuid.UUID,
    *,
    worker_instance_id: str,
    batch_size: int = 25,
    lease_seconds: int = _HISTORY_ITEM_LEASE_SECONDS,
    max_attempt_count: int = _HISTORY_MAX_ATTEMPT_COUNT,
) -> list[FirstPyramidHistoryRunItem]:
    """Worker 原子领取一批 pending/可恢复 history items。

    使用 UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED) RETURNING。
    """
    now = datetime.now(UTC)
    lease_expires_at = now + timedelta(seconds=lease_seconds)

    claim_sql = text(
        """
        UPDATE first_pyramid_history_run_items
        SET status = 'running',
            attempt_count = attempt_count + 1,
            lease_epoch = lease_epoch + 1,
            worker_instance_id = :worker_id,
            started_at = COALESCE(started_at, :now),
            heartbeat_at = :now,
            lease_expires_at = :lease_expires,
            updated_at = :now
        WHERE id IN (
            SELECT id FROM first_pyramid_history_run_items
            WHERE history_run_id = :history_run_id
              AND (
                status = 'pending'
                OR (status = 'failed' AND attempt_count < :max_attempts)
                OR (status = 'running' AND lease_expires_at < :now)
              )
            ORDER BY created_at
            LIMIT :batch_size
            FOR UPDATE SKIP LOCKED
        )
        RETURNING id, history_run_id, instrument_id, status, attempt_count,
                  input_hash, worker_instance_id, lease_epoch, lease_expires_at,
                  daily_state_count, event_count, last_error, started_at,
                  heartbeat_at, completed_at, created_at, updated_at
        """
    )
    result = await session.execute(claim_sql, {
        "worker_id": worker_instance_id,
        "now": now,
        "lease_expires": lease_expires_at,
        "history_run_id": history_run_id,
        "max_attempts": max_attempt_count,
        "batch_size": batch_size,
    })
    rows = result.fetchall()
    if not rows:
        return []

    items: list[FirstPyramidHistoryRunItem] = []
    for row in rows:
        item = FirstPyramidHistoryRunItem(
            id=row[0],
            history_run_id=row[1],
            instrument_id=row[2],
            status=row[3],
            attempt_count=row[4],
            input_hash=row[5],
            worker_instance_id=row[6],
            lease_epoch=row[7],
            lease_expires_at=row[8],
            daily_state_count=row[9],
            event_count=row[10],
            last_error=row[11],
            started_at=row[12],
            heartbeat_at=row[13],
            completed_at=row[14],
            created_at=row[15],
            updated_at=row[16],
        )
        items.append(item)
    return items


async def mark_history_item_succeeded(
    session: AsyncSession,
    item_id: uuid.UUID,
    *,
    daily_state_count: int | None = None,
    event_count: int | None = None,
    lease_epoch: int | None = None,
) -> bool:
    """标记单股 history item 成功（带 lease_epoch fencing）。"""
    now = datetime.now(UTC)
    conditions = [
        FirstPyramidHistoryRunItem.id == item_id,
        FirstPyramidHistoryRunItem.status == _HISTORY_ITEM_RUNNING,
    ]
    if lease_epoch is not None:
        conditions.append(FirstPyramidHistoryRunItem.lease_epoch == lease_epoch)
    stmt = (
        update(FirstPyramidHistoryRunItem)
        .where(*conditions)
        .values(
            status=_HISTORY_ITEM_SUCCEEDED,
            daily_state_count=daily_state_count,
            event_count=event_count,
            completed_at=now,
            updated_at=now,
        )
    )
    result = await session.execute(stmt)
    return result.rowcount > 0  # type: ignore[union-attr]


async def mark_history_item_failed(
    session: AsyncSession,
    item_id: uuid.UUID,
    error: str,
    *,
    lease_epoch: int | None = None,
) -> bool:
    """标记单股 history item 失败（带 lease_epoch fencing）。"""
    now = datetime.now(UTC)
    conditions = [
        FirstPyramidHistoryRunItem.id == item_id,
        FirstPyramidHistoryRunItem.status == _HISTORY_ITEM_RUNNING,
    ]
    if lease_epoch is not None:
        conditions.append(FirstPyramidHistoryRunItem.lease_epoch == lease_epoch)
    stmt = (
        update(FirstPyramidHistoryRunItem)
        .where(*conditions)
        .values(
            status=_HISTORY_ITEM_FAILED,
            last_error=error[:1000],
            completed_at=now,
            updated_at=now,
        )
    )
    result = await session.execute(stmt)
    return result.rowcount > 0  # type: ignore[union-attr]


async def mark_history_item_skipped(
    session: AsyncSession,
    item_id: uuid.UUID,
    reason: str,
    *,
    lease_epoch: int | None = None,
) -> bool:
    """标记单股 history item 跳过（数据不足等，带 lease_epoch fencing）。"""
    now = datetime.now(UTC)
    conditions = [
        FirstPyramidHistoryRunItem.id == item_id,
        FirstPyramidHistoryRunItem.status == _HISTORY_ITEM_RUNNING,
    ]
    if lease_epoch is not None:
        conditions.append(FirstPyramidHistoryRunItem.lease_epoch == lease_epoch)
    stmt = (
        update(FirstPyramidHistoryRunItem)
        .where(*conditions)
        .values(
            status=_HISTORY_ITEM_SKIPPED,
            last_error=reason[:1000],
            completed_at=now,
            updated_at=now,
        )
    )
    result = await session.execute(stmt)
    return result.rowcount > 0  # type: ignore[union-attr]


def _classify_history_zero_output(
    n_input: int,
    daily_state_rows: Sequence[Any],
    required_bars: int,
    meta_error: str | None = None,
) -> tuple[str, str]:
    """分类 history compute 的 zero-output 语义（避免 false-success）。

    返回 (decision, reason)：
    - ("process", "")：有输出，正常持久化
    - ("skip", reason)：INSUFFICIENT_HISTORY —— input bars 不足阈值，合法 zero-output
    - ("fail", reason)：COMPUTE_EMPTY_UNEXPECTED —— bars 足够但 compute 异常返回空，
      fail closed（不得 silently skipped，避免算法 bug 被掩盖）
    """
    if not daily_state_rows and n_input < required_bars:
        return (
            "skip",
            f"INSUFFICIENT_HISTORY: input_bars={n_input} required_bars={required_bars}",
        )
    if not daily_state_rows:
        return (
            "fail",
            "COMPUTE_EMPTY_UNEXPECTED: "
            f"input_bars={n_input} >= {required_bars} "
            f"但 daily_state 为空; meta={meta_error}",
        )
    return ("process", "")


async def requeue_history_items(
    session: AsyncSession,
    history_run_id: uuid.UUID,
    instrument_ids: Sequence[uuid.UUID],
) -> int:
    """把指定 instrument 的 history run items 重新置为 pending（targeted requeue）。

    [Phase 3D] 用于：
    - 原 skipped（如 BJ 无 bars，补齐 bars 后重新处理）
    - 原 false-success（旧语义标 succeeded 但零输出，需按新语义重新分类）

    只处理该 run 内、命中 instrument_ids 的 skipped/succeeded/failed/pending 项，
    不新建 run、不触碰其他 run、不重置已完成且有真实输出的 succeeded item。

    语义：
    - 保留 canonical history_run_id / input_hash（仅清 status/last_error/lease）
    - 保持 attempt/lease 字段可被 claim_history_items 正常接管
    - 不允许跨 run 重排
    """
    if not instrument_ids:
        return 0
    now = datetime.now(UTC)
    stmt = (
        update(FirstPyramidHistoryRunItem)
        .where(
            FirstPyramidHistoryRunItem.history_run_id == history_run_id,
            FirstPyramidHistoryRunItem.instrument_id.in_(list(instrument_ids)),
        )
        .values(
            status="pending",
            last_error=None,
            attempt_count=0,
            lease_epoch=0,
            worker_instance_id=None,
            started_at=None,
            heartbeat_at=None,
            lease_expires_at=None,
            updated_at=now,
        )
    )
    result = await session.execute(stmt)
    return result.rowcount or 0


async def get_history_run_progress(
    session: AsyncSession,
    history_run_id: uuid.UUID,
) -> dict[str, Any]:
    """获取 history run 级进度统计。"""
    stmt = (
        select(
            FirstPyramidHistoryRunItem.status,
            func.count(FirstPyramidHistoryRunItem.id).label("cnt"),
        )
        .where(FirstPyramidHistoryRunItem.history_run_id == history_run_id)
        .group_by(FirstPyramidHistoryRunItem.status)
    )
    rows = (await session.execute(stmt)).all()
    counts = {row.status: row.cnt for row in rows}

    succeeded = counts.get(_HISTORY_ITEM_SUCCEEDED, 0)
    failed = counts.get(_HISTORY_ITEM_FAILED, 0)
    pending = counts.get(_HISTORY_ITEM_PENDING, 0)
    running = counts.get(_HISTORY_ITEM_RUNNING, 0)
    skipped = counts.get(_HISTORY_ITEM_SKIPPED, 0)
    total = succeeded + failed + pending + running + skipped
    coverage = succeeded / total if total > 0 else 0.0

    return {
        "succeeded": succeeded,
        "failed": failed,
        "pending": pending,
        "running": running,
        "skipped": skipped,
        "total": total,
        "coverage": coverage,
    }


async def finish_history_run(
    session: AsyncSession,
    history_run_id: uuid.UUID,
    *,
    status: str,
) -> None:
    """更新 history run 的最终状态。"""
    progress = await get_history_run_progress(session, history_run_id)
    now = datetime.now(UTC)
    stmt = (
        update(FirstPyramidHistoryRun)
        .where(FirstPyramidHistoryRun.id == history_run_id)
        .values(
            status=status,
            succeeded_count=progress["succeeded"],
            failed_count=progress["failed"],
            skipped_count=progress["skipped"],
            completed_at=now,
            updated_at=now,
        )
    )
    await session.execute(stmt)


async def _fetch_db_only_daily_bars(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    *,
    output_bars: int,
) -> pd.DataFrame | None:
    """[CHANGE-20260731-003 / 20260808] SSOT 合规 strict DB-only 读日线行情。

    原实现直接调用 bar_repository._query_daily_bars 违反 SSOT 架构，
    改为通过 MarketDataAggregationService (MDAS) 统一出口。
    completed_only=True 保证只读取已完成 bar；include_realtime=False 禁用实时补充。
    [CHANGE-20260808] allow_backfill=False 强制 strict DB-only：即使 DB 缺尾也绝不调用
    external provider / realtime / 15m。DB 无 completed qfq bars → 返回 None（caller 标 skipped）。
    production history replay / canary 必须使用 strict DB-only。
    """
    from app.services.market_data_aggregation_service import MarketDataAggregationService

    mdas = MarketDataAggregationService()
    agg = await mdas.get_bars(
        session,
        instrument_id,
        timeframe="1d",
        adj="qfq",
        include_realtime=False,
        completed_only=True,
        allow_backfill=False,
        limit=output_bars * 2,  # 留余量，history SSOT 内部会截取 output_bars
    )
    df = agg.bars
    if df is None or df.empty:
        return None
    return df


async def _fetch_pit_daily_bars_for_target(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    *,
    output_bars: int,
    target_trade_date: date,
) -> pd.DataFrame | None:
    """[HISTORY-CURRENT-DATE-LIFECYCLE-01 §5] target-aware PIT strict DB-only 读日线。

    与 ``_fetch_db_only_daily_bars`` 的唯一差异：显式传 MDAS 已有的 PIT 参数
    ``end_date`` / ``adjustment_as_of``，保证：

    - ``max(bars.trade_date) <= target_trade_date``（不读未来 bar）
    - 复权锚点固定在 target_trade_date（禁止未来除权事件泄漏）

    其余契约与 backfill 完全一致（同一 MDAS 出口、strict DB-only、completed_only），
    因此 target-date state 与既有历史 state 由同一数据口径产出。
    """
    from app.services.market_data_aggregation_service import MarketDataAggregationService

    mdas = MarketDataAggregationService()
    agg = await mdas.get_bars(
        session,
        instrument_id,
        timeframe="1d",
        adj="qfq",
        include_realtime=False,
        completed_only=True,
        allow_backfill=False,
        end_date=target_trade_date,
        adjustment_as_of=target_trade_date,
        limit=output_bars * 2,
    )
    df = agg.bars
    if df is None or df.empty:
        return None
    return df


async def _fetch_pit_daily_bars_batch(
    session: AsyncSession,
    instrument_ids: Iterable[uuid.UUID],
    *,
    output_bars: int,
    target_trade_date: date,
) -> dict[uuid.UUID, BarAggregationResult | Exception]:
    """[CHANGE-20260821-001 Phase 3.3] PIT strict DB-only 批量读日线。

    对整批只发起 ~3 次 repository 查询（1×bars_daily + 1×adj_factor + 1×预期完成日），
    替代旧单股 ``_fetch_pit_daily_bars_for_target`` 的每股 1 次 ``get_bars`` 往返。
    与单股 helper 共用同一 MDAS 出口与一致 PIT 合同：

    - ``end_date=target_trade_date``：max(bars.trade_date) <= target，不读未来 bar；
    - ``adjustment_as_of=target_trade_date``：复权锚点固定，禁止未来除权泄漏；
    - strict DB-only（``allow_backfill=False``）+ ``completed_only=True``。

    返回 **key=instrument_id → BarAggregationResult | Exception**，保留完整
    BarAggregationResult（source_bar_hash / adj_factor_hash / completed_through 供 single-vs-batch
    parity 审计），caller 只消费 ``.bars``。

    BATCH CONTRACT 严格校验（fail visible，绝不静默降级）：
    - ``get_bars_batch`` 整批抛异常 → 为 batch 每股返回同一明确 Exception（不整批退出，
      也不静默 fallback 回单股 N+1，避免掩盖性能回归/生产问题）；
    - batch result 缺失某 instrument（key 不存在）→ 该 instrument 视为
      BATCH CONTRACT VIOLATION → RuntimeError（这绝不是 ``no_bar``）；
    - 值既非 BarAggregationResult 也非 Exception → RuntimeError。
    """
    from app.services.market_data_aggregation_service import (
        BarAggregationResult,
        MarketDataAggregationService,
    )

    ids = list(instrument_ids)
    mdas = MarketDataAggregationService()
    try:
        agg_map = await mdas.get_bars_batch(
            session,
            ids,
            timeframe="1d",
            adj="qfq",
            include_realtime=False,
            completed_only=True,
            allow_backfill=False,
            end_date=target_trade_date,
            adjustment_as_of=target_trade_date,
            limit=output_bars * 2,  # 留余量，history SSOT 内部会截取 output_bars
        )
    except Exception as exc:  # noqa: BLE001 - 整批失败对 batch 每股可见，不静默 fallback
        return {iid: RuntimeError(f"MDAS batch fetch failed: {exc}") for iid in ids}

    result: dict[uuid.UUID, BarAggregationResult | Exception] = {}
    for iid in ids:
        value = agg_map.get(iid)
        if isinstance(value, BarAggregationResult):
            result[iid] = value
        elif isinstance(value, Exception):
            result[iid] = value
        elif value is None:
            result[iid] = RuntimeError(
                f"MDAS batch result missing instrument: {iid} (BATCH CONTRACT VIOLATION)"
            )
        else:
            result[iid] = RuntimeError(
                f"unexpected MDAS batch result type for {iid}: {type(value).__name__}"
            )
    return result


async def backfill_history_with_run_items(
    *,
    history_run_id: uuid.UUID,
    algorithm_version: str,
    output_bars: int,
    worker_id: str = "history_worker",
    batch_size: int = 25,
    progress_callback: Callable[..., Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """[CHANGE-20260729-008] Run/Item 接入版历史回补（单股×检查点）。

    与 backfill_first_pyramid_history_batch 关键差异：
    - 使用 first_pyramid_history_run_items 表做单股 claim/lease/commit
    - 每只股票在独立短事务中计算并 commit（失败只回滚该股）
    - coverage 从 run_items 实时统计
    - 恢复只领 pending/可重试 failed/过期 running
    - **DB-only 取数**：直接调 _query_daily_bars，禁止自动 pytdx 拉取
    - 支持 resume：重启只处理 pending/failed/过期 running items

    流程：
    1. claim_history_items 领取一批
    2. 逐股：独立事务读 bars → 计算 history SSOT → 持久化 → commit → mark_item_succeeded
    3. 失败：mark_item_failed，继续下一股
    4. 无可领取 items 时结束

    Args:
        history_run_id: FirstPyramidHistoryRun.id
        algorithm_version: 算法版本
        output_bars: 输出最近 N 日
        worker_id: Worker 标识
        batch_size: claim 批次大小
        progress_callback: 进度回调

    Returns:
        统计 dict
    """
    from app.db import AsyncSessionLocal
    from app.services.first_pyramid_service import (
        _MIN_BARS_FOR_REQUIRED_DIMS,
        HISTORY_CONTRACT_VERSION,
        compute_first_pyramid_history,
    )

    total_processed = 0
    succeeded_count = 0
    failed_count = 0
    skipped_count = 0

    while True:
        # 1. claim 一批 items
        async with AsyncSessionLocal() as db:
            items = await claim_history_items(
                db, history_run_id,
                worker_instance_id=worker_id,
                batch_size=batch_size,
            )
            await db.commit()

        if not items:
            break

        # 2. 逐股计算（每股独立事务）
        for item in items:
            total_processed += 1
            try:
                # 2.1 DB-only 读取 bars（独立短事务）
                async with AsyncSessionLocal() as bars_db:
                    bars = await _fetch_db_only_daily_bars(
                        bars_db, item.instrument_id, output_bars=output_bars,
                    )

                if bars is None or bars.empty:
                    # 无任何 daily bars → skipped（NO_DAILY_BARS 类别）
                    async with AsyncSessionLocal() as skip_db:
                        await mark_history_item_skipped(
                            skip_db, item.id, "daily bars 为空（DB-only）",
                            lease_epoch=item.lease_epoch,
                        )
                        await skip_db.commit()
                    skipped_count += 1
                    continue

                # 2.2 计算 history SSOT（include_chip=False，禁止 chip）
                history = compute_first_pyramid_history(
                    bars=bars,
                    symbol=str(item.instrument_id),
                    output_bars=output_bars,
                    include_chip=False,
                )

                # 2.2.1 zero-output 分类（INSUFFICIENT_HISTORY vs COMPUTE_EMPTY_UNEXPECTED）
                decision, zr_reason = _classify_history_zero_output(
                    len(bars),
                    history.get("daily_state") or [],
                    required_bars=_MIN_BARS_FOR_REQUIRED_DIMS,
                    meta_error=history.get("meta", {}).get("error"),
                )
                if decision == "skip":
                    # 数据量不足 → skipped（明确原因，非失败、非 NO_DAILY_BARS）
                    async with AsyncSessionLocal() as skip_db:
                        await mark_history_item_skipped(
                            skip_db, item.id, zr_reason,
                            lease_epoch=item.lease_epoch,
                        )
                        await skip_db.commit()
                    skipped_count += 1
                    continue
                if decision == "fail":
                    # bars 足够但 compute 异常返回空 → fail closed（不得 silently skipped）
                    async with AsyncSessionLocal() as fail_db:
                        await mark_history_item_failed(
                            fail_db, item.id, zr_reason,
                            lease_epoch=item.lease_epoch,
                        )
                        await fail_db.commit()
                    failed_count += 1
                    continue

                # 2.2.2 PIT normalization：将全局锚点结果归一化到 date-specific PIT
                # bars 来自 _fetch_db_only_daily_bars，adjustment_as_of 未显式传
                # → MDAS 使用 latest_adj 作为复权分母。
                # 对 scale-covariant 字段（sqzmom_val/delta + event prices）
                # 乘以 K_t = anchor_factor / factor(t) 恢复严格 date-specific PIT。
                _apply_pit_normalization(bars, history)

                # 2.3 持久化（独立短事务）
                async with AsyncSessionLocal() as persist_db:
                    persisted = await _persist_history_result(
                        session=persist_db,
                        instrument_id=item.instrument_id,
                        history=history,
                        algorithm_version=algorithm_version,
                        source_history_run_id=history_run_id,
                        history_contract_version=HISTORY_CONTRACT_VERSION,
                    )
                    await persist_db.commit()

                # 2.4 标记 succeeded
                async with AsyncSessionLocal() as mark_db:
                    ok = await mark_history_item_succeeded(
                        mark_db, item.id,
                        daily_state_count=persisted["daily_state_count"],
                        event_count=persisted["events_count"],
                        lease_epoch=item.lease_epoch,
                    )
                    await mark_db.commit()

                if ok:
                    succeeded_count += 1
                else:
                    logger.warning(
                        "[HistoryBackfill] item %s lease_epoch 不匹配，已被接管",
                        item.id,
                    )

            except Exception as exc:
                failed_count += 1
                logger.error(
                    "[HistoryBackfill] instrument_id=%s 回补失败: %s",
                    item.instrument_id, exc, exc_info=True,
                )
                try:
                    async with AsyncSessionLocal() as fail_db:
                        await mark_history_item_failed(
                            fail_db, item.id, str(exc),
                            lease_epoch=item.lease_epoch,
                        )
                        await fail_db.commit()
                except Exception as mark_exc:
                    logger.error(
                        "mark_history_item_failed 失败 item_id=%s: %s",
                        item.id, mark_exc,
                    )

            # 2.5 进度回调
            if progress_callback is not None:
                try:
                    await progress_callback(
                        processed=total_processed,
                        succeeded=succeeded_count,
                        failed=failed_count,
                        skipped=skipped_count,
                    )
                except Exception as cb_exc:
                    logger.warning("progress_callback 失败: %s", cb_exc)

    # 3. 从 DB 统计最终进度（canonical source of truth）
    async with AsyncSessionLocal() as db:
        progress = await get_history_run_progress(db, history_run_id)

    # 4. 更新 run 最终状态 —— 必须由 DB canonical progress 决定，不得用本
    #    worker invocation 的 local counters（并发 worker 下会误判）：
    #     - pending > 0 或 running > 0：其他 worker 仍在 running，不得 finalize succeeded
    #     - 全部成功（succeeded==total 且无 failed/skipped/pending/running）：succeeded
    #     - 有成功且有 failed/skipped/pending/running 残留：partial
    #     - 无成功（全 failed/skipped）：failed
    final_status = _derive_run_final_status(progress)
    async with AsyncSessionLocal() as db:
        await finish_history_run(db, history_run_id, status=final_status)
        await db.commit()

    logger.info(
        "[HistoryBackfill] run=%s 完成: status=%s, succeeded=%d, failed=%d, skipped=%d",
        history_run_id, final_status, progress["succeeded"], progress["failed"],
        progress["skipped"],
    )

    return {
        "history_run_id": str(history_run_id),
        "algorithm_version": algorithm_version,
        "output_bars": output_bars,
        "status": final_status,
        "succeeded_count": progress["succeeded"],
        "failed_count": progress["failed"],
        "skipped_count": progress["skipped"],
        "total_processed": total_processed,
        "progress": progress,
    }


def _derive_run_final_status(progress: dict[str, Any]) -> str:
    """[CHANGE-20260808] 由 DB canonical progress 决定 run final status。

    HistoryRun model 语义：
      succeeded = 全部成功
      partial   = 部分成功 / failed / skipped
      failed    = 无成功

    并发安全：pending > 0 或 running > 0 时其他 worker 可能仍在处理，不得 finalize
    succeeded（也避免把仍在跑标记为完成）。
    """
    total = progress.get("total", 0)
    succeeded = progress.get("succeeded", 0)
    failed = progress.get("failed", 0)
    pending = progress.get("pending", 0)
    running = progress.get("running", 0)
    skipped = progress.get("skipped", 0)

    if pending > 0 or running > 0:
        # 有其他 worker 仍在处理 / 还有未领取 item → 不得视为 succeeded
        return HISTORY_RUN_PARTIAL

    if (
        total > 0
        and succeeded == total
        and failed == 0
        and skipped == 0
        and pending == 0
        and running == 0
    ):
        return HISTORY_RUN_SUCCEEDED

    if succeeded > 0:
        return HISTORY_RUN_PARTIAL

    # 无成功：全部 failed / skipped（按 model contract：failed）
    return HISTORY_RUN_FAILED


# =============================================================================
# 内部辅助
# =============================================================================


async def _fetch_history_daily_bars(
    instrument_id: uuid.UUID,
) -> pd.DataFrame | None:
    """获取完整可用日线（point-in-time，qfq 复权，已完成 bar）。

    [CHANGE-20260731-003] SSOT 合规：通过 MarketDataAggregationService (MDAS) 读取行情，
    不再调用 bar_repository.get_bars（已在 SSOT 黑名单中）。
    断点：依赖 MDAS 真实接口，本地纯单元测试由 caller 注入 _fetch_bars_func mock。
    """
    try:
        from app.db import AsyncSessionLocal
        from app.services.market_data_aggregation_service import MarketDataAggregationService
    except ImportError:
        logger.warning(
            "[HistoryBackfill] MDAS 或 AsyncSessionLocal 不可用，返回空 bars",
        )
        return None

    mdas = MarketDataAggregationService()
    async with AsyncSessionLocal() as db:
        result = await mdas.get_bars(
            db,
            instrument_id,
            timeframe="1d",
            adj="qfq",
            include_realtime=False,
            completed_only=True,
        )
        return result.bars


async def _persist_history_result(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    history: dict[str, Any],
    algorithm_version: str,
    *,
    source_history_run_id: uuid.UUID | None = None,
    history_contract_version: str | None = None,
) -> dict[str, int]:
    """持久化 history SSOT 结果到两张表。

    - daily_state: upsert（on_conflict_do_update），更新 state_payload + lineage
    - events: insert on_conflict_do_nothing（不可变，重跑不覆盖）

    [CHANGE-20260808] Historical Lineage（M2）：source_history_run_id + history_contract_version
    写入 daily_state 显式列（新 review-history-v2 replay 必须）。从 run.id 传，不从 metadata 猜。

    Args:
        session: 异步 DB 会话（不 commit，由 caller 控制）
        instrument_id: 股票 ID
        history: compute_first_pyramid_history 返回的 dict
        algorithm_version: 算法版本
        source_history_run_id: 来源 HistoryRun ID（M2）
        history_contract_version: history payload contract version（M2）

    Returns:
        {"daily_state_count": int, "events_count": int}
    """
    daily_state_list = history.get("daily_state") or []
    events_list = history.get("events") or []
    meta = history.get("meta") or {}
    input_hash = meta.get("input_hash") or ""

    daily_state_count = 0
    events_count = 0

    # 1. upsert daily_state
    for state in daily_state_list:
        time_str = state.get("time")
        if not time_str:
            continue
        try:
            trade_date_val = pd.to_datetime(time_str).date()
        except (ValueError, TypeError):
            continue

        stmt = pg_insert(FirstPyramidHistoryDailyState).values(
            instrument_id=instrument_id,
            trade_date=trade_date_val,
            algorithm_version=algorithm_version,
            input_hash=input_hash,
            source_history_run_id=source_history_run_id,
            history_contract_version=history_contract_version,
            state_payload=state,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_first_pyramid_history_daily_state_instr_date_ver",
            set_={
                "input_hash": stmt.excluded.input_hash,
                "source_history_run_id": stmt.excluded.source_history_run_id,
                "history_contract_version": stmt.excluded.history_contract_version,
                "state_payload": stmt.excluded.state_payload,
                "updated_at": func.now(),
            },
        )
        await session.execute(stmt)
        daily_state_count += 1

    # 2. insert events (on_conflict_do_nothing - immutable)
    for evt in events_list:
        event_type = evt.get("type") or evt.get("event_type") or "UNKNOWN"
        # 构造稳定 event_id：优先用 event 自带的 id，其次 bar_index+type，最后 time+type
        event_id = (
            evt.get("event_id")
            or evt.get("id")
            or _build_event_id(evt, event_type)
        )
        if not event_id:
            continue

        event_time = evt.get("time") or evt.get("anchor_time")

        stmt = pg_insert(FirstPyramidHistoryEvent).values(
            instrument_id=instrument_id,
            algorithm_version=algorithm_version,
            event_type=event_type,
            event_id=str(event_id),
            event_time=str(event_time) if event_time else None,
            history_contract_version=history_contract_version,
            event_payload=evt,
        )
        # [CHANGE-20260808] 事件唯一性 contract-aware：普通 UNIQUE 约束已拆成两个 partial
        # unique index（legacy WHERE history_contract_version IS NULL / versioned WHERE
        # history_contract_version IS NOT NULL）。on_conflict 必须用 index_elements +
        # index_where 做 index inference，禁止 ON CONFLICT ON CONSTRAINT <partial-index-name>，
        # 保证旧 NULL X + v2 X 可共存、v2 X 重跑仍幂等。
        if history_contract_version is not None:
            stmt = stmt.on_conflict_do_nothing(
                index_elements=[
                    FirstPyramidHistoryEvent.instrument_id,
                    FirstPyramidHistoryEvent.algorithm_version,
                    FirstPyramidHistoryEvent.history_contract_version,
                    FirstPyramidHistoryEvent.event_id,
                ],
                index_where=text("history_contract_version IS NOT NULL"),
            )
        else:
            stmt = stmt.on_conflict_do_nothing(
                index_elements=[
                    FirstPyramidHistoryEvent.instrument_id,
                    FirstPyramidHistoryEvent.algorithm_version,
                    FirstPyramidHistoryEvent.event_id,
                ],
                index_where=text("history_contract_version IS NULL"),
            )
        await session.execute(stmt)
        events_count += 1

    await session.flush()

    return {
        "daily_state_count": daily_state_count,
        "events_count": events_count,
    }


# =============================================================================
# [CHANGE-20260826-001 History-v3] Pure projection materializer
# =============================================================================
async def materialize_history_v3_from_core(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    trade_date: date,
    core_flat: dict[str, Any],
    *,
    core_run_id: uuid.UUID | None = None,
    algorithm_version: str | None = None,
) -> dict[str, int]:
    """[CHANGE-20260826-001] 从 durable Core artifact 投影并物化 review-history-v3。

    这是 Daily AfterClose 的 canonical History(T) 生产 owner。

    **禁止**：compute_first_pyramid_history / advance_history_to_trade_date /
    compute_dsa_bundle / compute_smc_pine / compute_sqzmom_lb / VolumeContext compute /
    bars reload for Core recompute。History-v3 只投影、不运行 kernel。

    Args:
        session: 异步 DB 会话（不 commit，由 caller 控制）
        instrument_id: 股票 ID
        trade_date: 业务交易日
        core_flat: StockFeatureSnapshot.summary_payload["first_pyramid_flat"]
                   （Core 已计算一次的事实；作为唯一投影输入）
        core_run_id: 来源 Core run id（lineage）
        algorithm_version: 算法版本（默认 FIRST_PYRAMID_CORE_ALGORITHM_VERSION）

    Returns:
        {"daily_state_count": int, "events_count": int}
    """
    from app.services.history_v3_projection import (
        REVIEW_HISTORY_V3_CONTRACT_VERSION,
        build_history_v3_projection,
        to_history_result_shape,
    )

    if not isinstance(core_flat, dict) or not core_flat:
        # 投影语义：Core 无事实 → 合法空投影（不崩溃、不重算）
        return {"daily_state_count": 0, "events_count": 0}

    projection = build_history_v3_projection(
        core_flat=core_flat,
        instrument_id=str(instrument_id),
        trade_date=trade_date,
        core_run_id=str(core_run_id) if core_run_id else None,
    )
    history_result = to_history_result_shape(projection)

    # 复用既有 _persist_history_result（state upsert + events 不可变 insert），
    # 仅 history_contract_version 指向 review-history-v3。
    return await _persist_history_result(
        session,
        instrument_id,
        history_result,
        algorithm_version or FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
        source_history_run_id=core_run_id,
        history_contract_version=REVIEW_HISTORY_V3_CONTRACT_VERSION,
    )


def _max_bar_trade_date(bars: pd.DataFrame) -> date | None:
    """[HISTORY-CURRENT-DATE-LIFECYCLE-01 §5] 取 bars 最大交易日（PIT 断言用）。

    history SSOT 的 bars 契约是 DatetimeIndex；兼容退化的 ``time`` 列形态。
    """
    if bars is None or bars.empty:
        return None
    try:
        if isinstance(bars.index, pd.DatetimeIndex):
            return bars.index.max().date()
        if "time" in bars.columns:
            return pd.to_datetime(bars["time"]).max().date()
    except (ValueError, TypeError, AttributeError):
        return None
    return None


def _target_date_events(history: dict[str, Any], trade_date: date) -> list[dict[str, Any]]:
    """PURE ADAPTER: slice the canonical events whose event date == ``trade_date``.

    ROUND-2.2A: the exact-T canonical calculation must, in the same lifecycle, form
    the T-day State AND the T-day Structure Event stream.  This helper only does
    ``event_time -> date`` filtering — it does NOT re-judge BOS / re-decide Structure
    Level / recompute Direction / aggregate member ratio / drop "less important"
    events.  Every event whose date equals ``trade_date`` is kept with its full
    canonical payload.

    ROUND-2.2A-1 FAIL-CLOSED (F1): a canonical event that cannot be assigned an
    event date is a lifecycle failure, NOT a zero-event.  ``events == []`` is a legal
    zero-event (member had no event on T); but an event with missing/invalid
    ``time``, or with a future date (> trade_date), must NOT be silently dropped —
    otherwise a corrupted event stream would be misrepresented as "a clean zero-event
    lifecycle", systematically lowering the event denominator.  These are raised so
    ``advance_history_to_trade_date`` fails the instrument and does NOT call
    persistence (fail closed).  ``event_date < trade_date`` is a history-window
    legacy event and is legitimately ignored.

    The event ``time`` field is the canonical occurrence/confirmation date (ISO date)
    normalized by ``compute_first_pyramid_history``; ``anchor_time`` is a different
    semantic (anchor/pivot/OB bar) and is NEVER used as the event date.  Input bars
    are hard-limited to ``max_bar_date <= trade_date``, so a future event date
    implies timestamp-mapping error / canonical compute leakage / date-semantics bug.
    """
    raw_events = history.get("events") or []
    if not raw_events:
        # history.events == [] -> legitimate zero-event; the lifecycle completed.
        return []
    out: list[dict[str, Any]] = []
    for evt in raw_events:
        # 2.2A-1 AUDIT FIX (F1): canonical target-date SSOT = evt["time"] ONLY.
        # compute_first_pyramid_history normalizes every event's canonical occurrence /
        # confirmation date into "time" (BOS/CHoCH->confirmed_time, OB_CREATED->
        # confirmed_time, OB_ENTERED->enter_time, OB_MITIGATED->mitigated_time,
        # EQH/EQL->confirmed_time, SQZ_RELEASE/ZERO_CROSS->times[i]).
        # ``anchor_time`` is a DIFFERENT semantic (anchor/pivot/OB bar), NOT the
        # event occurrence date — it must NEVER fallback-infer the event date
        # (that would attribute an event to the wrong trade day).  Missing time =>
        # CONTRACT CORRUPTION, fail closed.
        time_str = evt.get("time")
        if not time_str:
            raise ValueError(
                "event 缺少 canonical time，无法确定其 event date："
                f"type={evt.get('type') or '?'} — 这是 lifecycle failure，"
                "不是 zero-event；anchor_time 不是 event occurrence date，"
                "不允许 fallback 推断"
            )
        try:
            evt_date = pd.to_datetime(time_str).date()
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError(
                f"event time 无法解析（invalid timestamp）：{time_str!r} "
                f"type={evt.get('type') or '?'} — lifecycle failure，fail closed"
            ) from exc
        if evt_date > trade_date:
            # Input bars 已限制 max_bar_date <= trade_date；未来事件 = leakage /
            # 时间语义错误 / timestamp-mapping 错误 -> PIT violation，fail closed。
            raise ValueError(
                f"event date {evt_date.isoformat()} > trade_date "
                f"{trade_date.isoformat()}（type={evt.get('type') or '?'}）— "
                "PIT violation / canonical compute leakage，fail closed"
            )
        if evt_date == trade_date:
            out.append(evt)
        # evt_date < trade_date -> history-window legacy event, legitimately ignored.
    return out


async def advance_history_to_trade_date(
    session: AsyncSession,
    history_run_id: uuid.UUID,
    trade_date: date,
    *,
    output_bars: int = 250,
    batch_size: int = 25,
    progress_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """[HISTORY-CURRENT-DATE-LIFECYCLE-01 §4] 把 canonical history dataset 推进到 target trade date。

    语义（**canonical dataset advancement，不是重跑 backfill run**）：

    - HistoryRun 是「一个 algorithm+contract+scope 的 canonical dataset lineage 身份」
      （create_history_run 幂等复用 + daily_state upsert 覆盖 lineage 共同证明），
      因此推进数据集 = 在**同一个 run id** 下补齐 target-date state，
      而不是新建 run X 全量 replay（那会把 ~1.32M 历史行 lineage 改写成 X）。
    - ROUND-2.2A：exact-T State 与 exact-T Events 从**同一次** canonical calculation
      一起持久化（复用 ``_persist_history_result`` 单一 owner：State upsert +
      Event immutable insert + contract-aware uniqueness + lineage）。
      target-event slicing 是纯 date adapter（``_target_date_events``），非新业务算法。
      **零事件是一个合法、可证明的结果**（该 member T 日无事件 = 生命周期完整完成，
      而非 "no coverage"）。
    - 只写 target_trade_date 一行/instrument + 该日 events（1x write amplification，
      非 250x history rewrite；events 用不可变 insert-on-conflict-nothing，重跑幂等）
    - 不 claim / 不修改任何 run item（5283 succeeded + 10 skipped 的 execution history 冻结）
    - PIT：bars 经 MDAS ``end_date=trade_date`` + ``adjustment_as_of=trade_date``

    participating set = run 现有 succeeded run-item set（§8），
    不把历史 skipped instrument 拉进来（HISTORY-SKIP-REEVALUATION-01 已 DEFER）。

    Args:
        session: 异步 DB 会话
        history_run_id: canonical history run id（lineage 保持不变）
        trade_date: target trade date
        output_bars: history SSOT window（与 backfill 保持一致，默认 250）
        batch_size: 每批 commit 的 instrument 数（顺序执行，不并发）
        progress_callback: 可选进度回调

    Returns:
        {"run_id", "trade_date", "processed", "target_state_count",
         "no_bar", "no_target_state", "failed", "failed_instruments"}

    Raises:
        ValueError: run 不存在 / scope 不兼容 / contract 版本不匹配
    """
    from app.services.first_pyramid_service import (
        HISTORY_CONTRACT_VERSION,
        compute_first_pyramid_history,
    )

    run = await session.get(FirstPyramidHistoryRun, history_run_id)
    if run is None:
        raise ValueError(f"history run not found: {history_run_id}")

    # metadata_json 是 Text 列（既有 schema），可能是 JSON 字符串或已解析 dict；
    # 与 validate_canonical_history_run_readiness 使用同一解析口径。
    run_meta: dict[str, Any] = {}
    if isinstance(run.metadata_json, str) and run.metadata_json:
        try:
            parsed = json.loads(run.metadata_json)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            run_meta = parsed
    elif isinstance(run.metadata_json, dict):
        run_meta = run.metadata_json
    run_contract = run_meta.get("history_contract_version")
    if run_contract != HISTORY_CONTRACT_VERSION:
        raise ValueError(
            "history run contract mismatch: "
            f"run={run_contract!r} required={HISTORY_CONTRACT_VERSION!r}"
        )
    if run.scope != "all_a_share":
        raise ValueError(
            f"history run scope not canonical: {run.scope!r} (required 'all_a_share')"
        )

    algorithm_version = run.algorithm_version

    # §8 participating set = 现有 succeeded run items（不含 skipped）
    item_rows = await session.execute(
        select(FirstPyramidHistoryRunItem.instrument_id)
        .where(
            FirstPyramidHistoryRunItem.history_run_id == history_run_id,
            FirstPyramidHistoryRunItem.status == "succeeded",
        )
        .order_by(FirstPyramidHistoryRunItem.instrument_id)
    )
    instrument_ids = [row[0] for row in item_rows.all()]

    # [Phase 3.2] 性能埋点累加器（纯观测；不改变业务行为；仅 additive 写入返回 perf）
    _t_run_start = time.perf_counter()
    _run_perf: dict[str, Any] = {
        "stock_records": [],
        # [Phase 3.3] mdas_request_count 保持原语义=「逐股单读 MDAS 调用次数」；
        # 批读模式不再逐股调用，故保持 0（不换名换义）。批读计数见
        # mdas_batch_request_count；本轮请求股票数见 mdas_instrument_count。
        "mdas_request_count": 0,
        "mdas_batch_request_count": 0,
        "mdas_instrument_count": len(instrument_ids),
        "bars_count": 0,
        "daily_state_output_count": 0,
        "event_count": 0,
    }

    processed = 0
    target_state_count = 0
    no_bar = 0
    no_target_state = 0
    failed = 0
    failed_instruments: list[dict[str, Any]] = []

    for offset in range(0, len(instrument_ids), batch_size):
        batch = instrument_ids[offset : offset + batch_size]

        # [CHANGE-20260821-001 Phase 3.3] 整批一次 MDAS 批读（~3 次 repository SQL，
        # 替代旧单股 get_bars 的每股 1 次往返）。逐股失败隔离保留；整批异常对每股可见，
        # 绝不静默 fallback 回单股（NO_SECOND_ALGORITHM_IMPLEMENTATION / fail visible）。
        _t_batch = time.perf_counter()
        bars_by_id = await _fetch_pit_daily_bars_batch(
            session,
            batch,
            output_bars=output_bars,
            target_trade_date=trade_date,
        )
        _batch_bars_fetch_ms = (time.perf_counter() - _t_batch) * 1000.0
        _run_perf["mdas_batch_request_count"] += 1

        for instrument_id in batch:
            processed += 1
            _stock_perf: dict[str, float] = {}
            # 批读模式已无逐股 MDAS 调用；bars_fetch_ms 表示该股所属整批的批读耗时
            # （同一 batch 内每股一致），不逐股计时以免产生误导性的 0 值。
            _bars_fetch_ms = _batch_bars_fetch_ms
            _history_total_ms = 0.0
            _event_slice_ms = 0.0
            _persist_ms = 0.0
            try:
                entry = bars_by_id[instrument_id]
                if isinstance(entry, Exception):
                    # MDAS batch contract：整批异常/缺失/未知类型在 helper 已归一为
                    # Exception；此处每股 fail visible，不中断其余股票。
                    raise RuntimeError(f"MDAS batch fetch failed: {entry}")
                bars = entry.bars
                if bars is None or bars.empty:
                    no_bar += 1
                    _run_perf["stock_records"].append({
                        "instrument_id": str(instrument_id),
                        "outcome": "no_bar",
                        "bars_fetch_ms": _bars_fetch_ms,
                        "history_total_ms": 0.0,
                        "event_slice_ms": 0.0,
                        "persist_ms": 0.0,
                        "dsa_ms": 0.0, "smc_ms": 0.0,
                        "sqzmom_ms": 0.0, "volume_context_ms": 0.0,
                        "history_assembly_ms": 0.0, "bars_count": 0,
                    })
                    continue

                # §5 PIT 硬断言：绝不允许未来 bar 进入 compute input
                max_bar_date = _max_bar_trade_date(bars)
                if max_bar_date is not None and max_bar_date > trade_date:
                    raise ValueError(
                        f"PIT violation: max bar date {max_bar_date} > target {trade_date}"
                    )

                # §6 唯一 SSOT，算法不改
                _t = time.perf_counter()
                history = compute_first_pyramid_history(
                    bars=bars,
                    symbol=str(instrument_id),
                    output_bars=output_bars,
                    include_chip=False,
                    perf=_stock_perf,
                )
                _history_total_ms = (time.perf_counter() - _t) * 1000.0
                meta = history.get("meta") or {}

                target_state = None
                for state in history.get("daily_state") or []:
                    time_str = state.get("time")
                    if not time_str:
                        continue
                    try:
                        state_date = pd.to_datetime(time_str).date()
                    except (ValueError, TypeError):
                        continue
                    if state_date == trade_date:
                        target_state = state
                        break

                if target_state is None:
                    # instrument 在 target date 无 completed bar（停牌/退市/未上市）
                    no_target_state += 1
                    _run_perf["stock_records"].append({
                        "instrument_id": str(instrument_id),
                        "outcome": "no_target_state",
                        "bars_fetch_ms": _bars_fetch_ms,
                        "history_total_ms": _history_total_ms,
                        "event_slice_ms": 0.0,
                        "persist_ms": 0.0,
                        "dsa_ms": _stock_perf.get("dsa_ms", 0.0),
                        "smc_ms": _stock_perf.get("smc_ms", 0.0),
                        "sqzmom_ms": _stock_perf.get("sqzmom_ms", 0.0),
                        "volume_context_ms": _stock_perf.get("volume_context_ms", 0.0),
                        "history_assembly_ms": _stock_perf.get("history_assembly_ms", 0.0),
                        "bars_count": _stock_perf.get("bars_count", 0),
                    })
                    continue

                # ROUND-2.2A: exact-T State + exact-T Events come from the SAME
                # canonical calculation and are persisted TOGETHER via the single
                # ``_persist_history_result`` owner (State upsert + Event immutable
                # insert + contract-aware event uniqueness + lineage semantics).
                # Only the T-date state (1 row) and the T-date events are written —
                # no 250-day history rewrite.  target-event slicing is a pure date
                # adapter (``_target_date_events``), NOT a new business algorithm.
                _t = time.perf_counter()
                target_events = _target_date_events(history, trade_date)
                _event_slice_ms = (time.perf_counter() - _t) * 1000.0
                target_result: dict[str, Any] = {
                    "daily_state": [target_state],
                    "events": target_events,
                    "meta": meta,
                }
                _t = time.perf_counter()
                await _persist_history_result(
                    session,
                    instrument_id,
                    target_result,
                    algorithm_version,
                    source_history_run_id=history_run_id,
                    history_contract_version=HISTORY_CONTRACT_VERSION,
                )
                _persist_ms = (time.perf_counter() - _t) * 1000.0
                target_state_count += 1
                _run_perf["bars_count"] += _stock_perf.get("bars_count", 0)
                _run_perf["daily_state_output_count"] += 1
                _run_perf["event_count"] += len(target_events)
                _run_perf["stock_records"].append({
                    "instrument_id": str(instrument_id),
                    "outcome": "ok",
                    "bars_fetch_ms": _bars_fetch_ms,
                    "history_total_ms": _history_total_ms,
                    "event_slice_ms": _event_slice_ms,
                    "persist_ms": _persist_ms,
                    "dsa_ms": _stock_perf.get("dsa_ms", 0.0),
                    "smc_ms": _stock_perf.get("smc_ms", 0.0),
                    "sqzmom_ms": _stock_perf.get("sqzmom_ms", 0.0),
                    "volume_context_ms": _stock_perf.get("volume_context_ms", 0.0),
                    "history_assembly_ms": _stock_perf.get("history_assembly_ms", 0.0),
                    "bars_count": _stock_perf.get("bars_count", 0),
                })
            except Exception as exc:  # noqa: BLE001 - 单股失败不阻塞整体
                failed += 1
                failed_instruments.append(
                    {"instrument_id": str(instrument_id), "error": str(exc)}
                )
                logger.warning(
                    "advance_history_to_trade_date instrument failed: %s (%s)",
                    instrument_id,
                    exc,
                )

        await session.commit()
        if progress_callback is not None:
            await progress_callback(
                {
                    "processed": processed,
                    "total": len(instrument_ids),
                    "target_state_count": target_state_count,
                }
            )

    _wall_clock_ms = (time.perf_counter() - _t_run_start) * 1000.0
    _records = _run_perf["stock_records"]

    def _pctl(values: list[float], q: float) -> float:
        if not values:
            return 0.0
        s = sorted(values)
        if len(s) == 1:
            return s[0]
        idx = max(0, min(len(s) - 1, int(round((q / 100.0) * (len(s) - 1)))))
        return s[idx]

    _compute_vals = [r["history_total_ms"] for r in _records]
    _perf_summary: dict[str, Any] = {
        "instrument_count": len(instrument_ids),
        "processed": processed,
        "target_state_count": target_state_count,
        "no_bar": no_bar,
        "no_target_state": no_target_state,
        "failed": failed,
        "fail_count": failed,
        "skip_count": 0,
        "mdas_request_count": _run_perf["mdas_request_count"],
        "mdas_batch_request_count": _run_perf["mdas_batch_request_count"],
        "mdas_instrument_count": _run_perf["mdas_instrument_count"],
        "bars_count": _run_perf["bars_count"],
        "daily_state_output_count": _run_perf["daily_state_output_count"],
        "event_count": _run_perf["event_count"],
        "wall_clock_ms": _wall_clock_ms,
        "p50_stock_ms": _pctl(_compute_vals, 50),
        "p90_stock_ms": _pctl(_compute_vals, 90),
        "p95_stock_ms": _pctl(_compute_vals, 95),
        "p99_stock_ms": _pctl(_compute_vals, 99),
        "bars_fetch_p50_ms": _pctl([r["bars_fetch_ms"] for r in _records], 50),
        "bars_fetch_p95_ms": _pctl([r["bars_fetch_ms"] for r in _records], 95),
        "persist_p50_ms": _pctl([r["persist_ms"] for r in _records], 50),
        "persist_p95_ms": _pctl([r["persist_ms"] for r in _records], 95),
        "kernel": {
            "dsa_ms_p50": _pctl([r["dsa_ms"] for r in _records], 50),
            "dsa_ms_p95": _pctl([r["dsa_ms"] for r in _records], 95),
            "smc_ms_p50": _pctl([r["smc_ms"] for r in _records], 50),
            "smc_ms_p95": _pctl([r["smc_ms"] for r in _records], 95),
            "sqzmom_ms_p50": _pctl([r["sqzmom_ms"] for r in _records], 50),
            "sqzmom_ms_p95": _pctl([r["sqzmom_ms"] for r in _records], 95),
            "volume_context_ms_p50": _pctl([r["volume_context_ms"] for r in _records], 50),
            "volume_context_ms_p95": _pctl([r["volume_context_ms"] for r in _records], 95),
            "history_assembly_ms_p50": _pctl([r["history_assembly_ms"] for r in _records], 50),
            "history_assembly_ms_p95": _pctl([r["history_assembly_ms"] for r in _records], 95),
        },
        # 注意：bollinger 在 history 路径内嵌于 DSA bundle（compute_dsa_bundle），
        # 无法在不改动 kernel 内部的前提下单独计时；dsa_ms 已含 Bollinger 子步骤。
        # 若基准显示 DSA 为热点，可在 Phase 3.2.1 对 compute_dsa_bundle 单独插桩。
        "notes": {
            "bollinger_ms": "subsumed_in_dsa_ms",
        },
    }

    return {
        "run_id": str(history_run_id),
        "trade_date": trade_date.isoformat(),
        "total": len(instrument_ids),
        "processed": processed,
        "target_state_count": target_state_count,
        "no_bar": no_bar,
        "no_target_state": no_target_state,
        "failed": failed,
        "failed_instruments": failed_instruments,
        "perf": _perf_summary,
    }


# =============================================================================
# Phase 3 — canonical history terminalization + daily advancement
# (CHANGE-20260821-001) PRODUCER lifecycle owner；与 Review 完全独立（REVIEW_CODE_FREEZE=TRUE）
# =============================================================================

_NON_TERMINAL_TERMINALIZE_STATUSES = (
    _HISTORY_ITEM_PENDING,
    _HISTORY_ITEM_RUNNING,
    _HISTORY_ITEM_FAILED,
)


async def get_history_run_nonterminal_instruments(
    session: AsyncSession,
    history_run_id: uuid.UUID,
) -> list[uuid.UUID]:
    """列出该 canonical run 仍处非终态(pending/running/failed)的历史成员。

    这是 RUN_TERMINALIZATION_SET 的精确 instrument 列表（含已退出 universe 但仍
    pending/failed/running 的历史成员）。用于 Phase 3 结构化结果陈述「仍须 terminalize 的工作」，
    不依赖 current eligible universe（universe 由 Phase 2/Phase 4 提供）。
    """
    rows = (
        await session.execute(
            select(FirstPyramidHistoryRunItem.instrument_id).where(
                FirstPyramidHistoryRunItem.history_run_id == history_run_id,
                FirstPyramidHistoryRunItem.status.in_(_NON_TERMINAL_TERMINALIZE_STATUSES),
            )
        )
    ).all()
    return [row[0] for row in rows]


@dataclass
class HistoryRunClaimabilityReport:
    """[CHANGE-20260821-001 Phase 3.1] RUN_TERMINALIZATION_SET 的 claimability 分区。

    把 run 内仍非终态(pending/running/failed)成员，严格按既有 ``claim_history_items``
    的领取规则分成两类，供 Phase 3 决定是否 dispatch backfill：

    - CLAIMABLE_TERMINALIZATION_SET: pending / retryable-failed(attempt_count<max) /
      expired-running(lease 过期) —— claim_history_items 能领取，dispatch backfill 能真正推进
    - BLOCKING_BUT_NOT_CLAIMABLE: active-lease-running(lease 未过期) /
      exhausted-failed(attempt_count>=max) —— claim_history_items 领取不到；
      dispatch backfill 只会空转并触发其内部的 finish_history_run(重写 status/completed_at)，
      故必须禁止 dispatch，并在结构化结果中暴露 blocker 类别让 caller 知悉原因与可解性。
    """

    history_run_id: uuid.UUID
    claimable_count: int
    claimable_instruments: list[uuid.UUID]
    pending_nonterminal_instruments: list[uuid.UUID]   # 全部仍非终态成员（RUN_TERMINALIZATION_SET 剩余）
    active_lease_instruments: list[uuid.UUID]           # running 且 lease 未过期：lease 过期后可自动领取
    retry_exhausted_instruments: list[uuid.UUID]        # failed 且 attempt_count>=max：非自动可解，需手动 requeue


async def analyze_history_run_claimability(
    session: AsyncSession,
    history_run_id: uuid.UUID,
) -> HistoryRunClaimabilityReport:
    """[CHANGE-20260821-001 Phase 3.1] 计算 run 非终态成员的 claimability 分区。

    严格复刻 ``claim_history_items`` 的 WHERE 语义（见其 SQL），保证「可 dispatch 的
    terminalization work」与 worker 实际能领取的集合一致：

        claimable := status='pending'
                     OR (status='failed' AND attempt_count < max_attempts)
                     OR (status='running' AND lease_expires_at < now)

    其余非终态成员归入 BLOCKING_BUT_NOT_CLAIMABLE：
        - running 且 lease_expires_at >= now            → active_lease（将来可自动领取）
        - failed 且 attempt_count >= max_attempts       → retry_exhausted（需人工 requeue）

    caller 据此仅在有 claimable work 时 dispatch backfill，避免对不可领取项空转 finalization
    （无意义重写 run.status/completed_at）。
    """
    now = datetime.now(UTC)
    max_attempts = _HISTORY_MAX_ATTEMPT_COUNT
    rows = (
        await session.execute(
            select(
                FirstPyramidHistoryRunItem.instrument_id,
                FirstPyramidHistoryRunItem.status,
                FirstPyramidHistoryRunItem.attempt_count,
                FirstPyramidHistoryRunItem.lease_expires_at,
            ).where(
                FirstPyramidHistoryRunItem.history_run_id == history_run_id,
                FirstPyramidHistoryRunItem.status.in_(_NON_TERMINAL_TERMINALIZE_STATUSES),
            )
        )
    ).all()

    claimable: list[uuid.UUID] = []
    active_lease: list[uuid.UUID] = []
    retry_exhausted: list[uuid.UUID] = []
    all_nonterminal: list[uuid.UUID] = []

    for instrument_id, status, attempt_count, lease_expires_at in rows:
        all_nonterminal.append(instrument_id)
        if status == _HISTORY_ITEM_PENDING:
            claimable.append(instrument_id)
        elif status == _HISTORY_ITEM_RUNNING:
            if lease_expires_at is not None and lease_expires_at < now:
                claimable.append(instrument_id)
            else:
                active_lease.append(instrument_id)
        elif status == _HISTORY_ITEM_FAILED:
            if attempt_count is not None and attempt_count < max_attempts:
                claimable.append(instrument_id)
            else:
                retry_exhausted.append(instrument_id)

    return HistoryRunClaimabilityReport(
        history_run_id=history_run_id,
        claimable_count=len(claimable),
        claimable_instruments=claimable,
        pending_nonterminal_instruments=all_nonterminal,
        active_lease_instruments=active_lease,
        retry_exhausted_instruments=retry_exhausted,
    )


async def refresh_history_run_progress_counters(
    session: AsyncSession,
    history_run_id: uuid.UUID,
) -> None:
    """[CHANGE-20260821-001 Phase 3] 窄 progress-counter refresh owner。

    只把 ``run.succeeded_count`` / ``failed_count`` / ``skipped_count`` 同步为真实 run-item 状态计数，
    **不碰** ``run.status`` / ``completed_at`` / ``expected_count``（expected_count 由 Phase 2 reconcile 维护）。

    REVIEW_CODE_FREEZE 背景：现有 ``finish_history_run`` 在写 counters 的同时还会改写 status + completed_at。
    对一个已经 partial/succeeded、需长期 daily advance 的 canonical run，每天机械调用
    ``finish_history_run`` 会错误重置 execution lifecycle（completed_at 被刷新、status 被重钉为终态）。
    本 owner 仅同步 counters，是 producer 正确维护 Review 依赖数据（而非改 Review 判定）的安全路径；
    run status 的最终化只由 INITIAL_BOOTSTRAP_FINALIZATION 经 ``backfill_history_with_run_items``
    一次性完成，NORMAL_DAILY_ADVANCEMENT 不重做 finalization。
    """
    progress = await get_history_run_progress(session, history_run_id)
    run = await session.get(FirstPyramidHistoryRun, history_run_id)
    if run is None:
        return
    run.succeeded_count = progress["succeeded"]
    run.failed_count = progress["failed"]
    run.skipped_count = progress["skipped"]
    run.updated_at = datetime.now(UTC)
    session.add(run)
    await session.flush()


@dataclass
class CanonicalHistoryDailyAdvanceResult:
    """[CHANGE-20260821-001 Phase 3] ``advance_canonical_history_run_to_trade_date`` 结构化结果。

    不重新定义 Review readiness（READY 由现有 Review contract 判）；本结果只陈述 producer 做好的事实。
    [Phase 3.1] 新增 active_lease_instruments / retry_exhausted_instruments：当 result 为 incomplete 时，
    caller 据此知道「为什么 incomplete、以及它现在能不能靠再次自动跑解决」——
    active_lease 将来 lease 过期后可自动领取；retry_exhausted 需人工 requeue（非自动可解）。
    """

    history_run_id: uuid.UUID
    target_trade_date: date
    mode: str  # INITIAL_BOOTSTRAP_FINALIZATION | NORMAL_DAILY_ADVANCEMENT
    terminalization_dispatched: bool
    terminalization_summary: dict[str, Any] | None
    advance_summary: dict[str, Any]
    expected_count: int
    succeeded_count: int
    failed_count: int
    skipped_count: int
    pending_count: int
    running_count: int
    pending_nonterminal_instruments: list[uuid.UUID]
    active_lease_instruments: list[uuid.UUID]   # [Phase 3.1] running 且 lease 未过期：将来可自动领取
    retry_exhausted_instruments: list[uuid.UUID]  # [Phase 3.1] failed 且 attempt>=max：需人工 requeue
    failed_instruments: list[dict[str, Any]]
    status: str  # complete | incomplete（producer 事实，非 Review readiness）
    reason: str | None


async def advance_canonical_history_run_to_trade_date(
    session: AsyncSession,
    *,
    history_run_id: uuid.UUID,
    target_trade_date: date,
    output_bars: int = 250,
    bootstrap_worker_id: str = "history_producer",
    algorithm_version: str | None = None,
) -> CanonicalHistoryDailyAdvanceResult:
    """[CHANGE-20260821-001 Phase 3] 把已选定的 canonical HistoryRun 推进到目标交易日 T。

    固定流程（与 Review 完全独立；REVIEW_CODE_FREEZE=TRUE）：
      A. terminalize outstanding run_items（整个 run，不限 current eligible universe，
         故含 no_longer_current_nonterminal）→ 复用 ``backfill_history_with_run_items``
      B. 刷新 run-level progress counters（窄 owner，不碰 status/completed_at）
      C. advance canonical history → target T（复用 ``advance_history_to_trade_date``，
         仅处理 succeeded items；不重算 compute、不反推 snapshot、不绕过既有 canonical 持久化路径）

    硬边界：
    - 不调用 ``validate_canonical_history_run_readiness`` / ``_resolve_canonical_history_source``
      （producer 做事实，Review 判 readiness，职责分开）。
    - 不复写 ``compute_first_pyramid_history``；不从 snapshot summary 反推 history；
      不调用私有 ``_persist_history_result`` 直接写（一律经 advance_history_to_trade_date 既有 canonical 路径）。
    - 不谎报 success：任何 incomplete / failed → 返回 status='incomplete' + reason + failed_instruments。
    - INITIAL_BOOTSTRAP_FINALIZATION vs NORMAL_DAILY_ADVANCEMENT：
        run.status == running → 初始 bootstrap，terminalization 经 backfill 一次性 finalize
        （running → succeeded/partial）；NORMAL_DAILY 仅在确有「可自动领取(claimable)的 terminalization
        work」（pending / retryable-failed / expired-running）时才 dispatch backfill，且窄 counter owner
        不重钉 status/completed_at（避免每天把长期 canonical run 当 backfill 重做 finalization）。
        [Phase 3.1] 非过期 running、attempt 已达上限的 failed 属 BLOCKING_BUT_NOT_CLAIMABLE，worker 领取不到，
        此时不 dispatch backfill（否则其内部 finish_history_run 会无意义重写 status/completed_at），result 标记
        incomplete 并暴露 active_lease / retry_exhausted blocker 类别。

    Args:
        session: 异步 DB 会话（caller 控制 commit/transaction）
        history_run_id: 已 resolve 的 canonical FirstPyramidHistoryRun.id
        target_trade_date: 目标交易日
        output_bars: history SSOT window（与 backfill/advance 保持一致，默认 250）
        bootstrap_worker_id: terminalization worker 标识
        algorithm_version: 算法版本（默认取 run.algorithm_version）

    Returns:
        CanonicalHistoryDailyAdvanceResult
    """
    run = await session.get(FirstPyramidHistoryRun, history_run_id)
    if run is None:
        raise ValueError(f"history run not found: {history_run_id}")

    mode = (
        "INITIAL_BOOTSTRAP_FINALIZATION"
        if run.status == HISTORY_RUN_RUNNING
        else "NORMAL_DAILY_ADVANCEMENT"
    )
    algo = algorithm_version or run.algorithm_version

    # ---- A. terminalization（整个 run 的 outstanding 历史债务）----
    # 复用既有 whole-run bootstrap worker：claim pending/retryable-failed/expired-running →
    # 计算 → 持久化 → mark（覆盖所有 run items，不限 current universe，故含退出 universe 的非终态历史成员）。
    # [Phase 3.1] dispatch 门槛从「outstanding > 0」收紧为「存在可自动领取(claimable)的 terminalization work」：
    #   claimable = pending / retryable-failed(attempt_count<max) / expired-running(lease 过期)
    #             —— 与 claim_history_items 的 WHERE 语义严格一致，worker 真能领取并推进。
    # 非过期 running、attempt 已达上限的 failed 属 BLOCKING_BUT_NOT_CLAIMABLE，worker 领取不到；
    # 若仍 dispatch，backfill 内部 finish_history_run 会重写 status/completed_at（无谓 finalization churn），
    # 故此时不 dispatch，result 标记 incomplete 并暴露 blocker 类别（active_lease / retry_exhausted）。
    pre_claim = await analyze_history_run_claimability(session, history_run_id)
    terminalization_dispatched = False
    terminalization_summary: dict[str, Any] | None = None
    if pre_claim.claimable_count > 0:
        terminalization_summary = await backfill_history_with_run_items(
            history_run_id=history_run_id,
            algorithm_version=algo,
            output_bars=output_bars,
            worker_id=bootstrap_worker_id,
        )
        terminalization_dispatched = True

    # ---- B. 窄 counter refresh（只同步 succeeded/failed/skipped；不碰 status/completed_at/expected_count）----
    await refresh_history_run_progress_counters(session, history_run_id)

    # ---- C. advance canonical history → target T（复用既有 canonical 路径）----
    advance_summary = await advance_history_to_trade_date(
        session, history_run_id, target_trade_date, output_bars=output_bars,
    )

    # ---- 结构化事实（不重定义 readiness）----
    progress = await get_history_run_progress(session, history_run_id)
    run = await session.get(FirstPyramidHistoryRun, history_run_id)
    post_claim = await analyze_history_run_claimability(session, history_run_id)
    pending_nonterminal = post_claim.pending_nonterminal_instruments
    advance_failed = int(advance_summary.get("failed", 0) or 0)
    failed_instruments = list(advance_summary.get("failed_instruments", []) or [])

    reasons: list[str] = []
    if progress["pending"] > 0:
        reasons.append(f"pending={progress['pending']}")
    if progress["running"] > 0:
        reasons.append(f"running={progress['running']}")
    if progress["failed"] > 0:
        reasons.append(f"failed={progress['failed']}")
    if advance_failed > 0:
        reasons.append(f"advance_failed={advance_failed}")
    # [Phase 3.1] 暴露不可自动领取的 blocker 类别，让 caller 知道：
    #   active_lease = 将来 lease 过期后可由再次自动跑解决；retry_exhausted = 需人工 requeue（非自动可解）。
    if post_claim.active_lease_instruments:
        reasons.append(f"active_lease={len(post_claim.active_lease_instruments)}")
    if post_claim.retry_exhausted_instruments:
        reasons.append(f"retry_exhausted={len(post_claim.retry_exhausted_instruments)}")

    if reasons:
        status = "incomplete"
        reason = ";".join(reasons)
    else:
        status = "complete"
        reason = None

    return CanonicalHistoryDailyAdvanceResult(
        history_run_id=history_run_id,
        target_trade_date=target_trade_date,
        mode=mode,
        terminalization_dispatched=terminalization_dispatched,
        terminalization_summary=terminalization_summary,
        advance_summary=advance_summary,
        expected_count=int(run.expected_count or 0),
        succeeded_count=int(run.succeeded_count or 0),
        failed_count=int(run.failed_count or 0),
        skipped_count=int(run.skipped_count or 0),
        pending_count=progress["pending"],
        running_count=progress["running"],
        pending_nonterminal_instruments=pending_nonterminal,
        active_lease_instruments=post_claim.active_lease_instruments,
        retry_exhausted_instruments=post_claim.retry_exhausted_instruments,
        failed_instruments=failed_instruments,
        status=status,
        reason=reason,
    )


def _build_event_id(evt: dict[str, Any], event_type: str) -> str:
    """构造事件稳定 ID（无自带 id 时使用）。

    [CHANGE-20260808] 修复同 bar 多事件碰撞：event_id 不能仅用 {type}_{bar_index}，
    否则同 bar 的 internal BOS 与 swing BOS、同 bar 多个 OB 生命周期事件会互相覆盖
    （persistence ON CONFLICT 吃掉一行）。

    稳定 identity 纳入：
        event_type + bar_index + internal + structure_level/anchor_index + direction/bias
    保证同 bar 不同 internal/level/anchor 的事件生成不同 event_id。

    优先级：bar_index+type+内部字段 > time+type > payload hash
    """
    import hashlib
    import json

    bar_index = evt.get("bar_index")
    if bar_index is not None:
        # 区分字段：internal（internal vs swing）、anchor_index、structure_level、direction
        internal = "int" if evt.get("internal") else "swg"
        anchor = evt.get("anchor_index")
        anchor_s = f"_a{anchor}" if anchor is not None else ""
        level = evt.get("structure_level") or evt.get("level")
        level_s = f"_l{level}" if level is not None else ""
        direction = evt.get("direction") or evt.get("bias")
        dir_s = f"_d{direction}" if direction is not None else ""
        return f"{event_type}_{bar_index}_{internal}{anchor_s}{level_s}{dir_s}"

    time_val = evt.get("time") or evt.get("anchor_time")
    if time_val:
        return f"{event_type}_{time_val}"

    # fallback: hash of payload
    payload_str = json.dumps(evt, sort_keys=True, default=str)
    return f"{event_type}_{hashlib.md5(payload_str.encode()).hexdigest()[:12]}"


# =============================================================================
# 模块自测
# =============================================================================


if __name__ == "__main__":
    # 纯静态自测：验证 _build_event_id 稳定性
    evt1 = {"type": "BOS", "bar_index": 50, "time": "2026-07-01"}
    evt2 = {"type": "OB_CREATED", "anchor_time": "2026-07-01", "ob_id": "abc"}
    evt3 = {"type": "SQZ_RELEASE", "time": "2026-07-01", "direction": "up"}

    id1 = _build_event_id(evt1, "BOS")
    id2 = _build_event_id(evt2, "OB_CREATED")
    id3 = _build_event_id(evt3, "SQZ_RELEASE")

    assert id1 == "BOS_50", f"id1={id1}"
    assert id2 == "OB_CREATED_2026-07-01", f"id2={id2}"
    assert id3 == "SQZ_RELEASE_2026-07-01", f"id3={id3}"

    # 验证 model 字段
    from app.models.first_pyramid_history import (
        FirstPyramidHistoryDailyState,
        FirstPyramidHistoryEvent,
    )
    ds_cols = {c.name for c in FirstPyramidHistoryDailyState.__table__.columns}
    ev_cols = {c.name for c in FirstPyramidHistoryEvent.__table__.columns}
    assert "state_payload" in ds_cols
    assert "event_payload" in ev_cols

    print("OK: first_pyramid_history_service 自测通过")
    print(f"  daily_state cols: {sorted(ds_cols)}")
    print(f"  events cols: {sorted(ev_cols)}")
    print(f"  event_id samples: {id1}, {id2}, {id3}")
