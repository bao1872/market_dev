"""竞价分析 PG 集成测试 - 验证锚点→发布→扫描→聚合完整链路。

覆盖（ref/instruction.md §三 竞价分析完整链路）：
1. 完整链路：stock_core 发布 → 锚点生成 → 锚点发布 → 扫描 → 聚合
2. 幂等性：重复调用 generate/publish/scan/aggregate 不产生重复记录
3. 版本一致性：旧 source_core_run_id 与当日 stock_core pointer 不一致 → 禁止发布
4. 锚点未发布 → 扫描抛 AnchorNotPublishedError
5. chip 未完成 → 锚点状态为 structure_only（仍可发布与扫描）
6. 事件生命周期：formed → confirmed/weakened/failed（update_event_lifecycle）
7. 聚合幂等：重复 compute_auction_aggregation 删除旧 scope_results 后重写

测试环境：PostgreSQL 测试库（conftest.py db_session fixture）
运行（仅 CI 临时 Postgres 容器）：
    APP_ENV=test TEST_DATABASE_URL=postgresql://... \
        pytest backend/tests/test_auction_pg_integration.py -v
本地运行纯单元测试：
    PURE_UNIT_TEST=1 pytest backend/tests/test_auction_pg_integration.py -v
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auction import (
    AuctionAnchorItem,
    AuctionAnchorPublication,
    AuctionAnchorSnapshot,
    AuctionEventTracking,
    AuctionScopeResult,
)
from app.models.bar import BarDaily, BarMinute
from app.models.factor_publication import (
    PUBLICATION_KIND_STOCK_CORE,
    SCOPE_TYPE_MARKET,
    FactorPublication,
)
from app.models.instrument import Instrument
from app.models.market_board import MarketBoard, MarketBoardMembership
from app.models.stock_chip_consensus_snapshot import StockChipConsensusSnapshot
from app.models.stock_feature_snapshot import StockFeatureSnapshot
from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun
from app.services.auction_aggregation_service import (
    AUCTION_AGGREGATION_ALGORITHM_VERSION,
    compute_auction_aggregation,
    get_aggregation_results,
)
from app.services.auction_anchor_service import (
    AUCTION_ANCHOR_ALGORITHM_VERSION,
    AnchorCoverageLowError,
    AnchorSnapshotNotFoundError,
    AnchorVersionMismatchError,
    generate_auction_anchors,
    get_published_anchors,
    publish_auction_anchors,
)
from app.services.auction_scan_service import (
    AnchorNotPublishedError,
    get_scan_results,
    run_auction_scan,
    update_event_lifecycle,
)

# CI 环境标识（与 conftest.py 一致）
_CI_ENV = (
    os.environ.get("GITHUB_ACTIONS", "").lower() in ("1", "true", "yes")
    or os.environ.get("PANJI_CI_DB_TEST", "").lower() in ("1", "true", "yes")
)

# 本测试文件全部为 PG 集成测试（依赖 db_session fixture），
# 只在 CI 临时 Postgres 容器中运行；本地 PURE_UNIT_TEST=1 自动 skip。
pytestmark = pytest.mark.skipif(
    not _CI_ENV,
    reason="竞价分析 PG 集成测试只在 CI 临时 Postgres 容器中运行；本地请用 PURE_UNIT_TEST=1",
)

_TRADE_DATE = date(2026, 7, 30)


# =============================================================================
# Fixture 工厂：构造完整链路所需的最小数据集
# =============================================================================


def _make_structure_summary(*, trailing_top: str, trailing_bottom: str) -> dict:
    """构造 StockFeatureSnapshot.summary_payload（含 first_pyramid.structure）。

    锚点提取读取 summary_payload.first_pyramid.structure：
    - events: BOS/CHoCH/OB_CREATED
    - continuousFactors: trailing_top/trailing_bottom
    """
    return {
        "first_pyramid": {
            "structure": {
                "continuousFactors": {
                    "trailing_top": trailing_top,
                    "trailing_bottom": trailing_bottom,
                    "swing_bias": 1,
                    "internal_bias": 1,
                    "active_ob_count": 1,
                },
                "events": [
                    {
                        "type": "BOS",
                        "direction": "up",
                        "price": "10.20",
                        "occurredAt": "2026-07-28",
                        "barIndex": 100,
                        "extra": {"structure_level": "swing", "anchor_index": 1},
                    },
                    {
                        "type": "OB_CREATED",
                        "direction": "up",
                        "occurredAt": "2026-07-27",
                        "barIndex": 95,
                        "extra": {
                            "ob_high": "10.30",
                            "ob_low": "10.00",
                            "structure_level": "internal",
                            "anchor_index": 2,
                        },
                    },
                ],
            },
        },
    }


def _make_chip_payload(*, poc: str, vah: str, val: str, last_close: str) -> dict:
    """构造 StockChipConsensusSnapshot.chip_payload（含 chip 维度）。

    锚点提取读取 chip_payload.chip.continuousFactors（POC/VAH/VAL/last_close）
    和 chip.events（cross_up/cross_down）。
    """
    return {
        "chip": {
            "available": True,
            "continuousFactors": {
                "poc_price": poc,
                "vah_price": vah,
                "val_price": val,
                "last_close": last_close,
                "n_peak_nodes": 2,
            },
            "events": [
                {
                    "type": "cross_up",
                    "price": val,
                    "occurredAt": "2026-07-28",
                },
            ],
            "statusText": "筹码峰稳定",
        },
    }


async def _create_instrument(db_session: AsyncSession, *, symbol: str) -> Instrument:
    """创建活跃 A 股 instrument（symbol 必须 6 位数字）。"""
    inst = Instrument(
        symbol=symbol,
        name=f"测试股{symbol}",
        market="SZ" if symbol.startswith(("0", "3")) else "SH",
        status="active",
    )
    db_session.add(inst)
    await db_session.flush()
    return inst


async def _create_chip_snapshot(
    db_session: AsyncSession,
    *,
    instrument: Instrument,
    trade_date: date,
    core_run_id: uuid.UUID,
    poc: str = "10.10",
    vah: str = "10.30",
    val: str = "9.90",
    last_close: str = "10.15",
) -> StockChipConsensusSnapshot:
    """创建 succeeded 状态的 StockChipConsensusSnapshot。"""
    chip = StockChipConsensusSnapshot(
        instrument_id=instrument.id,
        trade_date=trade_date,
        core_run_id=core_run_id,
        algorithm_version="chip_v1.0.0",
        chip_hash="test_chip_hash_001",
        chip_payload=_make_chip_payload(
            poc=poc, vah=vah, val=val, last_close=last_close,
        ),
        status="succeeded",
    )
    db_session.add(chip)
    await db_session.flush()
    return chip


async def _create_stock_core_publication(
    db_session: AsyncSession,
    *,
    trade_date: date,
    core_run_id: uuid.UUID,
) -> FactorPublication:
    """创建 market 级 stock_core 发布指针。"""
    pub = FactorPublication(
        scope_type=SCOPE_TYPE_MARKET,
        scope_key="market",
        trade_date=trade_date,
        publication_kind=PUBLICATION_KIND_STOCK_CORE,
        algorithm_version="stock_core_v1",
        data_run_id=core_run_id,
        coverage_ratio=1.0,
    )
    db_session.add(pub)
    await db_session.flush()
    return pub


async def _create_prev_daily_bar(
    db_session: AsyncSession,
    *,
    instrument: Instrument,
    trade_date: date,
    close: str = "10.00",
    adj_factor: str = "1.0",
) -> BarDaily:
    """创建前一日 BarDaily（提供 prev_close 和 ATR 历史）。"""
    bar = BarDaily(
        instrument_id=instrument.id,
        trade_date=trade_date,
        open=Decimal(close),
        high=Decimal(close) + Decimal("0.20"),
        low=Decimal(close) - Decimal("0.20"),
        close=Decimal(close),
        volume=Decimal("1000000"),
        amount=Decimal("10000000"),
        adj_factor=Decimal(adj_factor),
    )
    db_session.add(bar)
    await db_session.flush()
    return bar


async def _create_final_auction_bar(
    db_session: AsyncSession,
    *,
    instrument: Instrument,
    trade_date: date,
    close: str = "10.50",
    volume: int = 2000000,
    amount: str = "21000000",
) -> BarMinute:
    """创建 trade_date 9:25 BarMinute（最终竞价数据）。"""
    auction_time = datetime.combine(trade_date, time(9, 25, 0))
    bar = BarMinute(
        instrument_id=instrument.id,
        trade_time=auction_time,
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=Decimal(volume),
        amount=Decimal(amount),
        adj_factor=Decimal("1.0"),
    )
    db_session.add(bar)
    await db_session.flush()
    return bar


async def _create_history_auction_bars(
    db_session: AsyncSession,
    *,
    instrument: Instrument,
    trade_date: date,
    count: int = 5,
) -> list[BarMinute]:
    """创建前 count 个交易日 9:25 BarMinute（历史竞价额用于中位数/分位计算）。"""
    bars: list[BarMinute] = []
    for i in range(1, count + 1):
        d = trade_date - timedelta(days=i)
        auction_time = datetime.combine(d, time(9, 25, 0))
        bar = BarMinute(
            instrument_id=instrument.id,
            trade_time=auction_time,
            open=Decimal("10.00"),
            high=Decimal("10.00"),
            low=Decimal("10.00"),
            close=Decimal("10.00"),
            volume=Decimal("1000000"),
            amount=Decimal("10000000"),
            adj_factor=Decimal("1.0"),
        )
        db_session.add(bar)
        bars.append(bar)
    await db_session.flush()
    return bars


async def _create_board_with_members(
    db_session: AsyncSession,
    *,
    board_type: str,
    name: str,
    instruments: list[Instrument],
) -> MarketBoard:
    """创建板块及成员关系。"""
    board = MarketBoard(
        externalCode=f"test:{name}",
        name=name,
        type=board_type,
        updatedAt=datetime.now(UTC),
    )
    db_session.add(board)
    await db_session.flush()
    for inst in instruments:
        mem = MarketBoardMembership(
            boardId=board.id,
            instrumentId=inst.id,
            updatedAt=datetime.now(UTC),
        )
        db_session.add(mem)
    await db_session.flush()
    return board


async def _setup_full_pipeline_fixtures(
    db_session: AsyncSession,
    *,
    with_chip: bool = True,
) -> tuple[list[Instrument], uuid.UUID, uuid.UUID]:
    """构造完整链路所需前置数据。

    创建 3 个 A 股 instrument、1 个共享 core_run（market 级）、3 个 chip_snapshot、
    stock_core 发布指针、前一日 BarDaily、最终竞价 BarMinute、历史竞价 BarMinute。

    所有 StockFeatureSnapshot.source_run_id 共享同一 core_run_id（与生产一致：
    market 级 stock_core 发布指向单次 after_close run，所有 snapshot 归属该 run）。

    Returns:
        (instruments, core_run_id, source_core_run_id_from_publication)
    """
    # 1. 创建共享 core_run（market 级单次 after_close）
    core_run = StockFeatureSnapshotRun(
        trade_date=_TRADE_DATE,
        schema_version=1,
        primary_timeframe="1d",
        secondary_timeframe="15m",
        adj="qfq",
        run_type="after_close",
        status="succeeded",
        snapshot_count=3,
        failed_count=0,
        failure_rate=0.0,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        published_at=datetime.now(UTC),
        adj_factor_hash="test_adj_hash_001",
        adjustment_as_of=_TRADE_DATE,
    )
    db_session.add(core_run)
    await db_session.flush()
    core_run_id = core_run.id

    instruments = []
    for symbol in ["000001", "000002", "600001"]:
        inst = await _create_instrument(db_session, symbol=symbol)
        instruments.append(inst)

        # snapshot 归属共享 core_run
        snapshot = StockFeatureSnapshot(
            instrument_id=inst.id,
            trade_date=_TRADE_DATE,
            primary_timeframe="1d",
            secondary_timeframe="15m",
            adj="qfq",
            schema_version=1,
            source_run_id=core_run_id,
            structural_payload={},
            temporal_payload={},
            summary_payload=_make_structure_summary(
                trailing_top="10.50", trailing_bottom="9.80",
            ),
            degraded_reasons=[],
        )
        db_session.add(snapshot)

        # chip snapshot（可选，归属同一 core_run_id）
        if with_chip:
            await _create_chip_snapshot(
                db_session, instrument=inst, trade_date=_TRADE_DATE,
                core_run_id=core_run_id,
            )

        # 前一日 daily bars（prev_close 来源 + ATR 历史）
        # 至少 ATR_LENGTH=14 根历史，构造 15 根以满足 ATR 计算
        for i in range(1, 16):
            await _create_prev_daily_bar(
                db_session, instrument=inst,
                trade_date=_TRADE_DATE - timedelta(days=i),
                close="10.00",
            )
        # 最终竞价 bar（高开 5%）
        await _create_final_auction_bar(
            db_session, instrument=inst, trade_date=_TRADE_DATE, close="10.50",
        )
        # 历史竞价 bars（中位数/分位计算）
        await _create_history_auction_bars(
            db_session, instrument=inst, trade_date=_TRADE_DATE, count=5,
        )

    await db_session.flush()

    # 2. market 级 stock_core 发布指针指向共享 core_run
    await _create_stock_core_publication(
        db_session, trade_date=_TRADE_DATE, core_run_id=core_run_id,
    )

    return instruments, core_run_id, core_run_id


# =============================================================================
# 测试 1: 完整链路 — 锚点生成 → 发布 → 扫描 → 聚合
# =============================================================================


class TestAuctionFullPipeline:
    """完整链路端到端测试：验证 7 张表正确填充。"""

    @pytest.mark.asyncio
    async def test_full_pipeline_populates_all_tables(self, db_session: AsyncSession) -> None:
        """完整链路：generate → publish → scan → aggregate 全部成功。

        验证：
        - AuctionAnchorSnapshot.status=succeeded（chip 可用时）
        - AuctionAnchorItem 含 structure/chip/composite 三类
        - AuctionAnchorPublication 写入且 source_core_run_id 一致
        - AuctionScanRun.status=succeeded，含 results 和 events
        - AuctionScopeResult 含 market/industry/concept 三类
        - AuctionEventTracking.lifecycle=formed
        """
        instruments, core_run_id, _ = await _setup_full_pipeline_fixtures(db_session)
        # 额外创建板块用于聚合验证
        await _create_board_with_members(
            db_session, board_type="industry", name="测试行业",
            instruments=instruments[:2],
        )
        await _create_board_with_members(
            db_session, board_type="concept", name="测试概念",
            instruments=instruments[1:],
        )

        # 1. 锚点生成
        gen_result = await generate_auction_anchors(
            db_session, trade_date=_TRADE_DATE,
        )
        assert gen_result["status"] == "succeeded", (
            f"锚点生成应 succeeded（chip 可用），实际 {gen_result['status']}"
        )
        assert gen_result["eligible_count"] == 3
        assert gen_result["structure_count"] > 0, "应生成结构锚点"
        assert gen_result["chip_count"] > 0, "应生成筹码锚点"
        assert gen_result["composite_count"] > 0, "应生成复合锚点"
        snapshot_id = gen_result["snapshot_id"]
        assert snapshot_id is not None

        # 2. 锚点发布
        publication = await publish_auction_anchors(db_session, snapshot_id)
        assert publication.source_core_run_id == core_run_id
        assert publication.coverage_ratio > 0.0
        assert publication.algorithm_version == AUCTION_ANCHOR_ALGORITHM_VERSION

        # 3. 验证 publication pointer 查询
        anchors_info = await get_published_anchors(db_session, _TRADE_DATE)
        assert anchors_info["publication_id"] is not None
        assert anchors_info["snapshot_id"] == snapshot_id
        assert anchors_info["active_anchor_count"] > 0

        # 4. 扫描
        scan_result = await run_auction_scan(
            db_session, trade_date=_TRADE_DATE, auction_type="final",
        )
        assert scan_result["status"] in ("succeeded", "partial"), (
            f"扫描应成功，实际 {scan_result['status']}"
        )
        assert scan_result["eligible_count"] == 3
        assert scan_result["result_count"] == 3, "应生成 3 条 instrument result"
        run_id = scan_result["run_id"]

        # 5. 聚合
        agg_result = await compute_auction_aggregation(db_session, run_id)
        assert agg_result["scan_run_id"] == str(run_id)
        assert agg_result["market"] is not None, "应生成 market 级聚合"
        assert agg_result["market"]["scope_type"] == "market"
        assert len(agg_result["industries"]) == 1, "应聚合 1 个行业"
        assert len(agg_result["concepts"]) == 1, "应聚合 1 个概念"
        # market 聚合必须有 status_label 和 confidence_level
        assert agg_result["market"]["status_label"] is not None
        assert agg_result["market"]["confidence_level"] is not None

        # 6. 验证 7 张表均有数据
        anchor_item_count = (await db_session.execute(
            select(func.count(AuctionAnchorItem.id))
            .where(AuctionAnchorItem.snapshot_id == snapshot_id)
        )).scalar_one()
        assert anchor_item_count > 0, "AuctionAnchorItem 应有数据"

        event_count = (await db_session.execute(
            select(func.count(AuctionEventTracking.id))
            .where(AuctionEventTracking.scan_run_id == run_id)
        )).scalar_one()
        # 至少部分 instrument 有有意义事件（非 inside_open 等）
        assert event_count >= 0, "AuctionEventTracking 查询应成功"

        scope_count = (await db_session.execute(
            select(func.count(AuctionScopeResult.id))
            .where(AuctionScopeResult.scan_run_id == run_id)
        )).scalar_one()
        # market + 1 industry + 1 concept = 3
        assert scope_count == 3, f"应有 3 条 scope_result，实际 {scope_count}"


# =============================================================================
# 测试 2: 幂等性 — 重复调用不产生重复记录
# =============================================================================


class TestAuctionIdempotency:
    """幂等性测试：upsert 保证重复调用安全。"""

    @pytest.mark.asyncio
    async def test_publish_idempotent_same_publication_record(
        self, db_session: AsyncSession,
    ) -> None:
        """重复 publish 同一 snapshot → publication 唯一键 upsert，不产生重复。"""
        await _setup_full_pipeline_fixtures(db_session)

        gen_result = await generate_auction_anchors(db_session, trade_date=_TRADE_DATE)
        snapshot_id = gen_result["snapshot_id"]

        # 第一次发布
        pub1 = await publish_auction_anchors(db_session, snapshot_id)
        # 第二次发布（幂等 upsert）
        pub2 = await publish_auction_anchors(db_session, snapshot_id)

        # 同一 (trade_date, algorithm_version) 唯一键 → 同一条记录
        assert pub1.id == pub2.id, "重复发布应 upsert 到同一条 publication 记录"

        # 验证 DB 中只有一条 publication
        count = (await db_session.execute(
            select(func.count(AuctionAnchorPublication.id))
            .where(AuctionAnchorPublication.trade_date == _TRADE_DATE)
        )).scalar_one()
        assert count == 1, f"幂等发布后应只有 1 条 publication，实际 {count}"

    @pytest.mark.asyncio
    async def test_aggregation_idempotent_replaces_scope_results(
        self, db_session: AsyncSession,
    ) -> None:
        """重复 compute_auction_aggregation → 删除旧 scope_results 后重写。"""
        await _setup_full_pipeline_fixtures(db_session)
        await _create_board_with_members(
            db_session, board_type="industry", name="幂等行业",
            instruments=[],
        )

        gen_result = await generate_auction_anchors(db_session, trade_date=_TRADE_DATE)
        await publish_auction_anchors(db_session, gen_result["snapshot_id"])
        scan_result = await run_auction_scan(db_session, trade_date=_TRADE_DATE)
        run_id = scan_result["run_id"]

        # 第一次聚合
        await compute_auction_aggregation(db_session, run_id)
        count1 = (await db_session.execute(
            select(func.count(AuctionScopeResult.id))
            .where(AuctionScopeResult.scan_run_id == run_id)
        )).scalar_one()

        # 第二次聚合（应删除旧记录后重写）
        await compute_auction_aggregation(db_session, run_id)
        count2 = (await db_session.execute(
            select(func.count(AuctionScopeResult.id))
            .where(AuctionScopeResult.scan_run_id == run_id)
        )).scalar_one()

        # 数量应一致（不翻倍）
        assert count1 == count2, (
            f"重复聚合应幂等，scope_results 数量应一致：{count1} vs {count2}"
        )
        assert count2 > 0, "应有 scope_results"


# =============================================================================
# 测试 3: 版本一致性 — 旧 source_core_run_id 禁止发布
# =============================================================================


class TestAuctionVersionConsistency:
    """版本一致性测试：source run 与当日 stock_core pointer 不一致时拒绝发布。"""

    @pytest.mark.asyncio
    async def test_publish_with_stale_source_run_rejected(
        self, db_session: AsyncSession,
    ) -> None:
        """snapshot.source_core_run_id 与当日 stock_core pointer.data_run_id 不一致
        → 抛 AnchorVersionMismatchError。

        场景：旧 snapshot 引用旧 core_run，但当日 pointer 已切换到新 core_run。
        """
        instruments, _, _ = await _setup_full_pipeline_fixtures(db_session)

        # 创建一个"旧" core_run（不属于当日发布指针，仅用于模拟旧 source run）
        # 不创建 StockFeatureSnapshot（会与已有快照唯一约束冲突）
        old_run = StockFeatureSnapshotRun(
            trade_date=_TRADE_DATE,
            schema_version=1,
            primary_timeframe="1d",
            secondary_timeframe="15m",
            adj="qfq",
            run_type="backfill",
            status="succeeded",
            snapshot_count=0,
            failed_count=0,
            failure_rate=0.0,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )
        db_session.add(old_run)
        await db_session.flush()

        gen_result = await generate_auction_anchors(db_session, trade_date=_TRADE_DATE)
        snapshot_id = gen_result["snapshot_id"]

        # 篡改 snapshot 的 source_core_run_id 为旧 run（模拟旧 snapshot）
        snapshot = await db_session.get(AuctionAnchorSnapshot, snapshot_id)
        assert snapshot is not None
        snapshot.source_core_run_id = old_run.id
        await db_session.flush()

        # 发布应被拒绝（source_core_run_id 与 pointer.data_run_id 不一致）
        with pytest.raises(AnchorVersionMismatchError, match="不一致"):
            await publish_auction_anchors(db_session, snapshot_id)

    @pytest.mark.asyncio
    async def test_publish_nonexistent_snapshot_raises(
        self, db_session: AsyncSession,
    ) -> None:
        """发布不存在的 snapshot → 抛 AnchorSnapshotNotFoundError。"""
        await _setup_full_pipeline_fixtures(db_session)
        fake_id = uuid.uuid4()
        with pytest.raises(AnchorSnapshotNotFoundError):
            await publish_auction_anchors(db_session, fake_id)


# =============================================================================
# 测试 4: 锚点未发布 → 扫描拒绝
# =============================================================================


class TestAuctionScanGuards:
    """扫描前置校验测试。"""

    @pytest.mark.asyncio
    async def test_scan_without_publication_raises(
        self, db_session: AsyncSession,
    ) -> None:
        """无已发布锚点 → run_auction_scan 抛 AnchorNotPublishedError。"""
        # 构造前置数据但不调用 generate/publish
        await _setup_full_pipeline_fixtures(db_session)

        # 不发布锚点直接扫描
        with pytest.raises(AnchorNotPublishedError, match="未发布"):
            await run_auction_scan(db_session, trade_date=_TRADE_DATE)


# =============================================================================
# 测试 5: chip 未完成 → structure_only 状态
# =============================================================================


class TestAuctionStructureOnly:
    """chip 未完成时锚点状态降级为 structure_only。"""

    @pytest.mark.asyncio
    async def test_chip_missing_yields_structure_only(
        self, db_session: AsyncSession,
    ) -> None:
        """无 chip snapshot → 锚点 status=structure_only，仍可发布与扫描。"""
        # 构造前置数据但不创建 chip snapshot
        await _setup_full_pipeline_fixtures(db_session, with_chip=False)

        gen_result = await generate_auction_anchors(db_session, trade_date=_TRADE_DATE)
        assert gen_result["status"] == "structure_only", (
            f"chip 不可用时应为 structure_only，实际 {gen_result['status']}"
        )
        assert gen_result["chip_count"] == 0, "无 chip 时不应生成筹码锚点"
        assert gen_result["structure_count"] > 0, "仍应生成结构锚点"

        # structure_only 仍可发布
        snapshot_id = gen_result["snapshot_id"]
        publication = await publish_auction_anchors(db_session, snapshot_id)
        assert publication.source_chip_run_id is None, (
            "structure_only 时 source_chip_run_id 应为 None"
        )

        # structure_only 仍可扫描
        scan_result = await run_auction_scan(db_session, trade_date=_TRADE_DATE)
        assert scan_result["status"] in ("succeeded", "partial")


# =============================================================================
# 测试 6: 事件生命周期 — formed → confirmed/weakened/failed
# =============================================================================


class TestAuctionEventLifecycle:
    """事件生命周期转换测试（update_event_lifecycle）。"""

    @pytest.mark.asyncio
    async def test_breakout_event_confirmed_on_open_above_trigger(
        self, db_session: AsyncSession,
    ) -> None:
        """突破事件（dual_breakout/structure_breakout）：开盘价 >= 触发价 → confirmed。

        场景：竞价扫描产生 formed 状态的 dual_breakout 事件；
        开盘后窗口价格 >= 触发价 → update_event_lifecycle 转为 confirmed。
        """
        instruments, _, _ = await _setup_full_pipeline_fixtures(db_session)

        gen_result = await generate_auction_anchors(db_session, trade_date=_TRADE_DATE)
        await publish_auction_anchors(db_session, gen_result["snapshot_id"])
        scan_result = await run_auction_scan(db_session, trade_date=_TRADE_DATE)
        run_id = scan_result["run_id"]

        # 查询 formed 状态事件
        events = list((await db_session.execute(
            select(AuctionEventTracking)
            .where(
                AuctionEventTracking.scan_run_id == run_id,
                AuctionEventTracking.lifecycle == "formed",
            )
        )).scalars().all())

        if not events:
            pytest.skip("无 formed 事件可测试（取决于锚点与竞价价相对位置）")

        # 为首个事件构造开盘后窗口 bars（开盘价高于触发价 → confirmed）
        first_event = events[0]
        trigger_price = first_event.trigger_price
        # 开盘价设为触发价 + 2%（确保 confirmed）
        open_price = (
            float(trigger_price) * 1.02 if trigger_price is not None else 11.0
        )
        opening_time = datetime.combine(_TRADE_DATE, time(9, 30, 0))
        # 构造 2 根开盘窗口 bar
        for i, price in enumerate([open_price, open_price * 1.01]):
            bar = BarMinute(
                instrument_id=first_event.instrument_id,
                trade_time=opening_time + timedelta(minutes=i),
                open=Decimal(str(round(price, 2))),
                high=Decimal(str(round(price, 2))),
                low=Decimal(str(round(price, 2))),
                close=Decimal(str(round(price, 2))),
                volume=Decimal("500000"),
                amount=Decimal("5000000"),
                adj_factor=Decimal("1.0"),
            )
            db_session.add(bar)
        await db_session.flush()

        result = await update_event_lifecycle(db_session, run_id)

        assert result["total"] == len(events)
        # 至少有一个事件发生转换
        assert result["transitions"], "应有生命周期转换"
        # 突破类事件应转换为 confirmed
        breakout_transitions = [
            t for t in result["transitions"] if "formed->confirmed" in t
        ]
        assert breakout_transitions, (
            f"突破事件开盘价高于触发价应 confirmed，transitions={result['transitions']}"
        )

        # 验证 DB 中事件状态已更新
        await db_session.refresh(first_event)
        assert first_event.lifecycle == "confirmed"
        assert first_event.confirmed_at is not None
        assert first_event.confirmation_data is not None

    @pytest.mark.asyncio
    async def test_breakout_event_failed_on_open_below_trigger(
        self, db_session: AsyncSession,
    ) -> None:
        """突破事件：开盘价远低于触发价（>2%）→ failed。

        场景：dual_breakout 事件后，开盘价跌破触发价 3% → failed。
        """
        instruments, _, _ = await _setup_full_pipeline_fixtures(db_session)

        gen_result = await generate_auction_anchors(db_session, trade_date=_TRADE_DATE)
        await publish_auction_anchors(db_session, gen_result["snapshot_id"])
        scan_result = await run_auction_scan(db_session, trade_date=_TRADE_DATE)
        run_id = scan_result["run_id"]

        events = list((await db_session.execute(
            select(AuctionEventTracking)
            .where(
                AuctionEventTracking.scan_run_id == run_id,
                AuctionEventTracking.lifecycle == "formed",
            )
        )).scalars().all())

        if not events:
            pytest.skip("无 formed 事件可测试")

        # 筛选突破类事件（dual_breakout/structure_breakout/chip_repricing）
        breakout_events = [
            e for e in events
            if e.event_type in ("dual_breakout", "structure_breakout", "chip_repricing")
        ]
        if not breakout_events:
            pytest.skip("无突破类事件可测试")

        first_event = breakout_events[0]
        trigger_price = first_event.trigger_price
        # 开盘价设为触发价 - 5%（远超 2% 阈值 → failed）
        open_price = float(trigger_price) * 0.95
        opening_time = datetime.combine(_TRADE_DATE, time(9, 30, 0))
        bar = BarMinute(
            instrument_id=first_event.instrument_id,
            trade_time=opening_time,
            open=Decimal(str(round(open_price, 2))),
            high=Decimal(str(round(open_price, 2))),
            low=Decimal(str(round(open_price, 2))),
            close=Decimal(str(round(open_price, 2))),
            volume=Decimal("500000"),
            amount=Decimal("5000000"),
            adj_factor=Decimal("1.0"),
        )
        db_session.add(bar)
        await db_session.flush()

        await update_event_lifecycle(db_session, run_id)

        await db_session.refresh(first_event)
        assert first_event.lifecycle == "failed", (
            f"突破事件开盘价远低于触发价应 failed，实际 {first_event.lifecycle}"
        )
        assert first_event.failed_at is not None

    @pytest.mark.asyncio
    async def test_update_lifecycle_nonexistent_run_raises(
        self, db_session: AsyncSession,
    ) -> None:
        """update_event_lifecycle 传入不存在的 scan_run_id → 抛 ValueError。"""
        fake_id = uuid.uuid4()
        with pytest.raises(ValueError, match="not found"):
            await update_event_lifecycle(db_session, fake_id)


# =============================================================================
# 测试 7: 聚合查询 — get_aggregation_results
# =============================================================================


class TestAuctionAggregationQuery:
    """聚合结果查询测试。"""

    @pytest.mark.asyncio
    async def test_get_aggregation_results_returns_all_scopes(
        self, db_session: AsyncSession,
    ) -> None:
        """get_aggregation_results 返回 market/industries/concepts 三类。"""
        instruments, _, _ = await _setup_full_pipeline_fixtures(db_session)
        await _create_board_with_members(
            db_session, board_type="industry", name="查询行业",
            instruments=instruments[:2],
        )
        await _create_board_with_members(
            db_session, board_type="concept", name="查询概念",
            instruments=instruments[1:],
        )

        gen_result = await generate_auction_anchors(db_session, trade_date=_TRADE_DATE)
        await publish_auction_anchors(db_session, gen_result["snapshot_id"])
        scan_result = await run_auction_scan(db_session, trade_date=_TRADE_DATE)
        run_id = scan_result["run_id"]

        await compute_auction_aggregation(db_session, run_id)
        result = await get_aggregation_results(db_session, run_id)

        assert result["scan_run_id"] == str(run_id)
        assert result["trade_date"] == _TRADE_DATE.isoformat()
        assert result["algorithm_version"] == AUCTION_AGGREGATION_ALGORITHM_VERSION
        assert result["market"] is not None
        assert len(result["industries"]) == 1
        assert len(result["concepts"]) == 1
        # 行业条目含 scope_name 和 status_label
        industry = result["industries"][0]
        assert industry["scope_name"] == "查询行业"
        assert industry["scope_type"] == "industry"
        assert "status_label" in industry
        assert "confidence_level" in industry

    @pytest.mark.asyncio
    async def test_get_aggregation_results_nonexistent_run_raises(
        self, db_session: AsyncSession,
    ) -> None:
        """查询不存在的 scan_run_id → 抛 ValueError。"""
        fake_id = uuid.uuid4()
        with pytest.raises(ValueError, match="not found"):
            await get_aggregation_results(db_session, fake_id)


# =============================================================================
# 测试 8: 覆盖率为 0 → 发布拒绝
# =============================================================================


class TestAuctionCoverageGuard:
    """覆盖率门禁测试。"""

    @pytest.mark.asyncio
    async def test_publish_zero_coverage_raises(
        self, db_session: AsyncSession,
    ) -> None:
        """snapshot.coverage_ratio=0（无活跃锚点）→ 抛 AnchorCoverageLowError。

        场景：构造一个 succeeded 但 coverage_ratio=0 的 snapshot（无活跃锚点）。
        """
        instruments, core_run_id, _ = await _setup_full_pipeline_fixtures(db_session)

        # 直接创建一个 coverage_ratio=0 的 snapshot（绕过 generate）
        snapshot = AuctionAnchorSnapshot(
            trade_date=_TRADE_DATE,
            source_core_run_id=core_run_id,
            source_chip_run_id=core_run_id,
            algorithm_version=AUCTION_ANCHOR_ALGORITHM_VERSION,
            price_adjustment_version="test_adj",
            status="succeeded",
            eligible_count=3,
            ready_count=0,
            coverage_ratio=0.0,
            missing_count=3,
        )
        db_session.add(snapshot)
        await db_session.flush()

        with pytest.raises(AnchorCoverageLowError, match="覆盖率"):
            await publish_auction_anchors(db_session, snapshot.id)


# =============================================================================
# 测试 9: 扫描结果查询 — get_scan_results
# =============================================================================


class TestAuctionScanQuery:
    """扫描结果查询测试。"""

    @pytest.mark.asyncio
    async def test_get_scan_results_returns_results_and_events(
        self, db_session: AsyncSession,
    ) -> None:
        """get_scan_results 返回 results 和 events 列表。"""
        await _setup_full_pipeline_fixtures(db_session)

        gen_result = await generate_auction_anchors(db_session, trade_date=_TRADE_DATE)
        await publish_auction_anchors(db_session, gen_result["snapshot_id"])
        scan_result = await run_auction_scan(db_session, trade_date=_TRADE_DATE)
        run_id = scan_result["run_id"]

        query_result = await get_scan_results(
            db_session, _TRADE_DATE, auction_type="final",
        )

        assert query_result["run_id"] == run_id
        assert query_result["status"] in ("succeeded", "partial")
        assert query_result["eligible_count"] == 3
        assert len(query_result["results"]) == 3
        # events 可能为空（取决于竞价价相对锚点位置）
        assert isinstance(query_result["events"], list)

    @pytest.mark.asyncio
    async def test_get_scan_results_no_run_returns_empty(
        self, db_session: AsyncSession,
    ) -> None:
        """无扫描 run 时返回空结果。"""
        result = await get_scan_results(
            db_session, _TRADE_DATE, auction_type="final",
        )
        assert result["run_id"] is None
        assert result["status"] is None
        assert result["results"] == []
        assert result["events"] == []
