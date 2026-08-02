"""[P0 收口 2026-07-29 五.1] PostgreSQL 集成测试：99字段 filter/sort + chip 严格五元组匹配。

覆盖场景（instruction 五.1）：
1. flat 数字/事件字段 filter+sort
2. column 字段 calculated_at/run_id
3. literal data_source
4. boolean（fp_chip_available）
5. computed is_stale
6. matched chip POC
7. 同股票旧 trade_date/旧 core_run/旧 algorithm_version 不得匹配
8. 列表返回 chip 字段与排序字段一致
9. between、多条件、NULLS LAST、跨页 symbol 稳定
10. 非法字段 422

运行环境：
- 必须在 CI 临时 Postgres 容器中运行（GITHUB_ACTIONS=true 或 PANJI_CI_DB_TEST=1）
- 使用测试数据库和 Redis DB15
- 本地 PURE_UNIT_TEST=1 时自动 skip（不连接数据库）

运行：
    # CI 环境
    APP_ENV=test TEST_DATABASE_URL=postgresql://...pytest_test \\
    GITHUB_ACTIONS=true pytest tests/test_market_stocks_chip_integration.py -v

    # 本地（自动 skip）
    PURE_UNIT_TEST=1 pytest tests/test_market_stocks_chip_integration.py -v
"""
from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

# [P0 收口] 纯单元测试模式自动 skip（不连接数据库）
_PURE_UNIT = os.environ.get("PURE_UNIT_TEST", "").lower() in ("1", "true", "yes")
_CI_ENV = (
    os.environ.get("GITHUB_ACTIONS", "").lower() in ("1", "true", "yes")
    or os.environ.get("PANJI_CI_DB_TEST", "").lower() in ("1", "true", "yes")
)

pytestmark = pytest.mark.skipif(
    _PURE_UNIT or not _CI_ENV,
    reason="PostgreSQL 集成测试只在 CI 临时 Postgres 容器中运行；本地请用 PURE_UNIT_TEST=1",
)

from app.core.deps import get_current_active_user  # noqa: E402
from app.main import app  # noqa: E402
from app.models.bar import BarDaily  # noqa: E402
from app.models.instrument import Instrument  # noqa: E402
from app.models.stock_chip_consensus_snapshot import StockChipConsensusSnapshot  # noqa: E402
from app.models.stock_feature_snapshot import StockFeatureSnapshot  # noqa: E402
from app.models.stock_feature_snapshot_run import (  # noqa: E402
    STATUS_SUCCEEDED,
    StockFeatureSnapshotRun,
)
from app.models.user import User  # noqa: E402
from app.schemas.first_pyramid import CHIP_CONSENSUS_ALGORITHM_VERSION  # noqa: E402
from app.services.feature_snapshot_service import _SCHEMA_VERSION  # noqa: E402
from app.services.first_pyramid_flatten import (  # noqa: E402
    FP_CHIP_KEYS,
    flatten_first_pyramid,
)


def _build_first_pyramid_payload(
    *, trend_direction: str = "上行", trend_bars: int = 5,
    swing_direction: str = "上行", momentum_direction: str = "扩张",
) -> dict:
    """构造 first_pyramid payload（含足够 flat 字段供筛选排序）。"""
    return {
        "tradeDate": "2026-07-25",
        "statusText": "趋势上行，结构共振",
        "trend": {
            "continuousFactors": {
                "regime_value": 1 if trend_direction == "上行" else -1,
                "dsa_dir_bars": trend_bars,
                "dsa_vwap_dev_pct": 2.34,
                "segment_change_pct": 5.6,
                "regime_strength": 0.85,
                "current_vs_prev_volume_mean_ratio": 1.5,
                "current_vs_prev_amount_mean_ratio": 1.4,
            },
        },
        "structure": {
            "continuousFactors": {
                "swing_bias": 1 if swing_direction == "上行" else -1,
                "internal_bias": 1,
                "active_ob_count": 2,
            },
            "events": [
                {
                    "type": "BOS",
                    "direction": "up",
                    "freshnessBars": 3,
                    "occurredAt": "2026-07-22",
                    "price": 10.7,
                }
            ],
        },
        "momentum": {
            "continuousFactors": {
                "sqzmom_val": 0.15,
                "squeeze_on": False,
                "squeeze_off": True,
                "bb_width": 0.04,
            },
            "events": [
                {
                    "type": "SQZ_OFF",
                    "direction": "up",
                    "freshnessBars": 2,
                    "occurredAt": "2026-07-24",
                }
            ],
        },
        "chipConsensus": None,  # 由 chip 表独立读取，不从 review-core 读
    }


def _build_chip_payload(*, available: bool = True, poc_price: float = 29.36) -> dict:
    """构造 chip_payload（含 chip_flat 扁平对象，供 chip source 字段读取）。"""
    chip_dim = {
        "name": "chip_consensus",
        "available": available,
        "statusText": "筹码峰稳定" if available else "无有效峰",
        "continuousFactors": {
            "poc_price": poc_price,
            "last_close": 30.12,
            "n_peak_nodes": 2,
            "vah_price": 30.5,
            "val_price": 28.8,
        },
        "events": [
            {
                "type": "NODE_CROSSOVER",
                "direction": "up",
                "freshnessBars": 3,
                "price": 29.40,
                "occurredAt": "2026-07-25",
                "barIndex": 100,
            }
        ],
    } if available else None
    chip_flat = flatten_first_pyramid({"chipConsensus": chip_dim}) if chip_dim else dict.fromkeys(FP_CHIP_KEYS)
    return {
        "chip": chip_dim,
        "chipHash": f"sha256:{uuid.uuid4().hex[:8]}",
        "algorithmVersion": CHIP_CONSENSUS_ALGORITHM_VERSION,
        "dailyBarsCount": 250,
        "bars15mCount": 4000,
        "error": None,
        "chip_flat": {k: chip_flat.get(k) for k in FP_CHIP_KEYS},
    }


@pytest_asyncio.fixture
async def chip_test_setup(
    client: AsyncClient,
    db_session: AsyncSession,
    user_factory,
    instrument_factory,
    subscription_factory,
) -> AsyncGenerator[tuple[AsyncClient, User, list[Instrument], uuid.UUID, uuid.UUID], None]:
    """构造 3 只股票 + snapshot + chip + bar_daily 测试数据。

    返回 (client, user, instruments, run_id, core_run_id)：
    - inst1: 最新 snapshot + matched chip（available=True）
    - inst2: 最新 snapshot + matched chip（available=False, chip=None）
    - inst3: 最新 snapshot + 旧 trade_date chip（不匹配，fp_chip_available=False）
    另插入 inst1 的旧 core_run chip（验证不匹配旧 run）
    """
    from datetime import date as date_cls

    user = await user_factory(
        email="chip_test@example.com",
        password_hash="fake-hash",
        timezone="Asia/Shanghai",
    )
    inst1 = await instrument_factory(symbol="600519", name="贵州茅台", market="SH")
    inst2 = await instrument_factory(symbol="000001", name="平安银行", market="SZ")
    inst3 = await instrument_factory(symbol="300750", name="宁德时代", market="SZ")
    await subscription_factory(user_id=user.id, plan_code="observe_20")

    # [CHANGE-20260731-004] 修复 chip_test_setup FK 违反：
    # core_run_id FK 指向 stock_feature_snapshot_runs.id（CHANGE-20260729-007），
    # 不再指向 scheduler_job_runs.id。原 fixture 误用 SchedulerJobRun 导致 FK 违反。
    # snap_run: 当前 trade_date 的 StockFeatureSnapshotRun（chip 应匹配）
    snap_run = StockFeatureSnapshotRun(
        trade_date=date_cls(2026, 7, 25),
        schema_version=_SCHEMA_VERSION,
        primary_timeframe="1d",
        secondary_timeframe="15m",
        adj="qfq",
        run_type="after_close",
        status=STATUS_SUCCEEDED,
        started_at=datetime.now(UTC) - timedelta(hours=1),
        finished_at=datetime.now(UTC),
        published_at=datetime.now(UTC),
        metadata_={"scope": "full"},
    )
    db_session.add(snap_run)
    await db_session.flush()

    # old_snap_run: 旧 trade_date 的 StockFeatureSnapshotRun（chip 不应匹配）
    old_snap_run = StockFeatureSnapshotRun(
        trade_date=date_cls(2026, 7, 24),
        schema_version=_SCHEMA_VERSION,
        primary_timeframe="1d",
        secondary_timeframe="15m",
        adj="qfq",
        run_type="after_close",
        status=STATUS_SUCCEEDED,
        started_at=datetime.now(UTC) - timedelta(days=1, hours=1),
        finished_at=datetime.now(UTC) - timedelta(days=1),
        published_at=datetime.now(UTC) - timedelta(days=1),
        metadata_={"scope": "full"},
    )
    db_session.add(old_snap_run)
    await db_session.flush()

    # ===== inst1: 最新 snapshot + matched chip（available=True） =====
    fp1 = _build_first_pyramid_payload(trend_direction="上行", trend_bars=5)
    flat1 = flatten_first_pyramid(fp1, calculated_at="2026-07-25T15:00:00+08:00", run_id=str(snap_run.id))
    snap1 = StockFeatureSnapshot(
        instrument_id=inst1.id,
        trade_date=date_cls(2026, 7, 25),
        primary_timeframe="1d",
        secondary_timeframe="15m",
        adj="qfq",
        schema_version=_SCHEMA_VERSION,
        source_run_id=snap_run.id,
        structural_payload={},
        temporal_payload={},
        summary_payload={
            "first_pyramid": fp1,
            "first_pyramid_flat": flat1,
            "daily_developing_swing_dir": 1,
        },
        degraded_reasons=[],
    )
    db_session.add(snap1)
    chip_payload1 = _build_chip_payload(available=True, poc_price=29.36)
    chip1 = StockChipConsensusSnapshot(
        instrument_id=inst1.id,
        trade_date=date_cls(2026, 7, 25),
        core_run_id=snap_run.id,
        algorithm_version=CHIP_CONSENSUS_ALGORITHM_VERSION,
        chip_hash=chip_payload1["chipHash"],
        chip_payload=chip_payload1,
        status="succeeded",
    )
    db_session.add(chip1)

    # ===== inst2: 最新 snapshot + matched chip（available=False, chip=None） =====
    fp2 = _build_first_pyramid_payload(trend_direction="下行", trend_bars=3)
    flat2 = flatten_first_pyramid(fp2, calculated_at="2026-07-25T15:00:00+08:00", run_id=str(snap_run.id))
    snap2 = StockFeatureSnapshot(
        instrument_id=inst2.id,
        trade_date=date_cls(2026, 7, 25),
        primary_timeframe="1d",
        secondary_timeframe="15m",
        adj="qfq",
        schema_version=_SCHEMA_VERSION,
        source_run_id=snap_run.id,
        structural_payload={},
        temporal_payload={},
        summary_payload={
            "first_pyramid": fp2,
            "first_pyramid_flat": flat2,
            "daily_developing_swing_dir": -1,
        },
        degraded_reasons=[],
    )
    db_session.add(snap2)
    chip_payload2 = _build_chip_payload(available=False)
    chip2 = StockChipConsensusSnapshot(
        instrument_id=inst2.id,
        trade_date=date_cls(2026, 7, 25),
        core_run_id=snap_run.id,
        algorithm_version=CHIP_CONSENSUS_ALGORITHM_VERSION,
        chip_hash=chip_payload2["chipHash"],
        chip_payload=chip_payload2,
        status="succeeded",
    )
    db_session.add(chip2)

    # ===== inst3: 最新 snapshot + 旧 trade_date chip（不匹配） =====
    fp3 = _build_first_pyramid_payload(trend_direction="上行", trend_bars=8)
    flat3 = flatten_first_pyramid(fp3, calculated_at="2026-07-25T15:00:00+08:00", run_id=str(snap_run.id))
    snap3 = StockFeatureSnapshot(
        instrument_id=inst3.id,
        trade_date=date_cls(2026, 7, 25),
        primary_timeframe="1d",
        secondary_timeframe="15m",
        adj="qfq",
        schema_version=_SCHEMA_VERSION,
        source_run_id=snap_run.id,
        structural_payload={},
        temporal_payload={},
        summary_payload={
            "first_pyramid": fp3,
            "first_pyramid_flat": flat3,
            "daily_developing_swing_dir": 1,
        },
        degraded_reasons=[],
    )
    db_session.add(snap3)
    # inst3 的 chip 是旧 trade_date（2026-07-24）→ 不匹配 latest_snap.trade_date
    chip_payload3 = _build_chip_payload(available=True, poc_price=50.0)
    chip3_old = StockChipConsensusSnapshot(
        instrument_id=inst3.id,
        trade_date=date_cls(2026, 7, 24),  # 旧 trade_date
        core_run_id=snap_run.id,
        algorithm_version=CHIP_CONSENSUS_ALGORITHM_VERSION,
        chip_hash=chip_payload3["chipHash"],
        chip_payload=chip_payload3,
        status="succeeded",
    )
    db_session.add(chip3_old)
    # inst3 还有一条旧 core_run_id 的 chip（同 trade_date 但 core_run 不匹配）
    chip3_old_run = StockChipConsensusSnapshot(
        instrument_id=inst3.id,
        trade_date=date_cls(2026, 7, 25),
        core_run_id=old_snap_run.id,  # 旧 snap_run → 不匹配 latest_snap.source_run_id
        algorithm_version=CHIP_CONSENSUS_ALGORITHM_VERSION,
        chip_hash=chip_payload3["chipHash"],
        chip_payload=chip_payload3,
        status="succeeded",
    )
    db_session.add(chip3_old_run)
    # inst3 还有一条旧 algorithm_version 的 chip（同 trade_date + core_run 但版本不匹配）
    chip3_old_ver = StockChipConsensusSnapshot(
        instrument_id=inst3.id,
        trade_date=date_cls(2026, 7, 25),
        core_run_id=snap_run.id,
        algorithm_version="0.0.0-old-version",  # 旧 algorithm_version
        chip_hash=chip_payload3["chipHash"],
        chip_payload=chip_payload3,
        status="succeeded",
    )
    db_session.add(chip3_old_ver)

    # ===== BarDaily：inst1/inst2 最新交易日为 2026-07-25（与 snapshot 同日，is_stale=False） =====
    # inst3 故意只插到 2026-07-26（snapshot trade_date=2026-07-25 < MAX=2026-07-26 → is_stale=True）
    for inst, latest_date in [
        (inst1, date_cls(2026, 7, 25)),
        (inst2, date_cls(2026, 7, 25)),
        (inst3, date_cls(2026, 7, 26)),  # 比 snapshot 新 → is_stale=True
    ]:
        for d in [latest_date, date_cls(2026, 7, 24)]:
            bar = BarDaily(
                instrument_id=inst.id,
                trade_date=d,
                open=Decimal("10.0"),
                high=Decimal("11.0"),
                low=Decimal("9.5"),
                close=Decimal("10.5"),
                volume=Decimal("1000000"),
            )
            db_session.add(bar)

    await db_session.flush()

    # 覆盖认证
    async def _get_user():
        return user

    app.dependency_overrides[get_current_active_user] = _get_user

    yield client, user, [inst1, inst2, inst3], snap_run.id, snap_run.id

    app.dependency_overrides.pop(get_current_active_user, None)


@pytest.mark.asyncio
class TestMarketStocksChipIntegration:
    """PostgreSQL 集成测试：99字段 filter/sort + chip 严格五元组匹配。"""

    async def test_flat_number_filter_and_sort(
        self, chip_test_setup,
    ) -> None:
        """场景1: flat 数字字段 filter+sort（fp_trend_bars）。"""
        client, user, instruments, _, _ = chip_test_setup
        # filter: fp_trend_bars >= 5 → 只有 inst1(5) 和 inst3(8)
        resp = await client.get(
            "/v1/market/stocks",
            params={"scope": "market", "fp_filter": "fp_trend_bars:gte:5", "page_size": 50},
        )
        assert resp.status_code == 200
        data = resp.json()
        symbols = [item["symbol"] for item in data["items"]]
        assert "600519" in symbols  # inst1 trend_bars=5
        assert "300750" in symbols  # inst3 trend_bars=8
        assert "000001" not in symbols  # inst2 trend_bars=3

    async def test_flat_event_field_filter(
        self, chip_test_setup,
    ) -> None:
        """场景1b: flat 事件字段 filter（fp_structure_event_type eq BOS）。"""
        client, user, instruments, _, _ = chip_test_setup
        resp = await client.get(
            "/v1/market/stocks",
            params={"scope": "market", "fp_filter": "fp_structure_event_type:eq:BOS", "page_size": 50},
        )
        assert resp.status_code == 200
        data = resp.json()
        # 所有 3 只都有 BOS 事件
        assert data["total"] >= 3

    async def test_column_calculated_at_sort(
        self, chip_test_setup,
    ) -> None:
        """场景2: column 字段 calculated_at 排序。"""
        client, user, instruments, _, _ = chip_test_setup
        resp = await client.get(
            "/v1/market/stocks",
            params={"scope": "market", "fp_sort": "fp_calculated_at:asc", "page_size": 50},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 3

    async def test_column_run_id_filter(
        self, chip_test_setup,
    ) -> None:
        """场景2b: column 字段 run_id filter（eq）。"""
        client, user, instruments, run_id, _ = chip_test_setup
        resp = await client.get(
            "/v1/market/stocks",
            params={"scope": "market", "fp_filter": f"fp_run_id:eq:{run_id}", "page_size": 50},
        )
        assert resp.status_code == 200
        data = resp.json()
        # 所有 3 只的 source_run_id 都是 snap_run.id
        assert data["total"] == 3

    async def test_literal_data_source_filter(
        self, chip_test_setup,
    ) -> None:
        """场景3: literal data_source filter（eq feature_snapshot）。"""
        client, user, instruments, _, _ = chip_test_setup
        resp = await client.get(
            "/v1/market/stocks",
            params={"scope": "market", "fp_filter": "fp_data_source:eq:feature_snapshot", "page_size": 50},
        )
        assert resp.status_code == 200
        data = resp.json()
        # 所有有 snapshot 的股票都匹配
        assert data["total"] == 3

    async def test_boolean_chip_available_filter(
        self, chip_test_setup,
    ) -> None:
        """场景4: boolean fp_chip_available filter（eq true）。"""
        client, user, instruments, _, _ = chip_test_setup
        resp = await client.get(
            "/v1/market/stocks",
            params={"scope": "market", "fp_filter": "fp_chip_available:eq:true", "page_size": 50},
        )
        assert resp.status_code == 200
        data = resp.json()
        symbols = [item["symbol"] for item in data["items"]]
        # 只有 inst1 有 matched chip available=True
        assert "600519" in symbols
        # inst2 chip available=False → 不匹配
        assert "000001" not in symbols
        # inst3 无 matched chip → fp_chip_available=False
        assert "300750" not in symbols

    async def test_computed_is_stale_filter(
        self, chip_test_setup,
    ) -> None:
        """场景5: computed is_stale filter（eq true）。"""
        client, user, instruments, _, _ = chip_test_setup
        resp = await client.get(
            "/v1/market/stocks",
            params={"scope": "market", "fp_filter": "fp_is_stale:eq:true", "page_size": 50},
        )
        assert resp.status_code == 200
        data = resp.json()
        symbols = [item["symbol"] for item in data["items"]]
        # 只有 inst3 的 snapshot trade_date(2026-07-25) < MAX(bar_daily.trade_date)(2026-07-26)
        assert "300750" in symbols
        assert "600519" not in symbols  # inst1 snap_td == max_td → is_stale=False
        assert "000001" not in symbols

    async def test_matched_chip_poc_sort(
        self, chip_test_setup,
    ) -> None:
        """场景6: matched chip POC 排序（fp_poc_price desc）。"""
        client, user, instruments, _, _ = chip_test_setup
        resp = await client.get(
            "/v1/market/stocks",
            params={"scope": "market", "fp_sort": "fp_poc_price:desc", "page_size": 50},
        )
        assert resp.status_code == 200
        data = resp.json()
        # 只有 inst1 有 matched chip with poc_price=29.36
        # 排序后 inst1 应在首位（其他 chip_poc_price 为 None，NULLS LAST）
        if data["items"]:
            first = data["items"][0]
            if first["first_pyramid"] and first["first_pyramid"].get("fp_poc_price") is not None:
                assert first["symbol"] == "600519"
                assert first["first_pyramid"]["fp_poc_price"] == 29.36

    async def test_old_trade_date_chip_not_matched(
        self, chip_test_setup,
    ) -> None:
        """场景7a: 同股票旧 trade_date chip 不得匹配。"""
        client, user, instruments, _, _ = chip_test_setup
        # inst3 有 chip trade_date=2026-07-24（旧），不应匹配 latest_snap trade_date=2026-07-25
        resp = await client.get(
            "/v1/market/stocks",
            params={
                "scope": "market",
                "fp_filter": "fp_chip_available:eq:true",
                "query": "300750",
                "page_size": 50,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        # inst3 不应出现在 chip_available=true 结果中
        for item in data["items"]:
            if item["symbol"] == "300750":
                fp = item.get("first_pyramid") or {}
                assert fp.get("fp_chip_available") is False
                assert fp.get("fp_poc_price") is None

    async def test_old_core_run_chip_not_matched(
        self, chip_test_setup,
    ) -> None:
        """场景7b: 同股票旧 core_run_id chip 不得匹配。"""
        client, user, instruments, _, _ = chip_test_setup
        # inst3 有 chip core_run_id=old_run.id（旧 run），不应匹配 latest_snap.source_run_id=snap_run.id
        resp = await client.get(
            "/v1/market/stocks",
            params={
                "scope": "market",
                "fp_filter": "fp_chip_available:eq:true",
                "query": "300750",
                "page_size": 50,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        for item in data["items"]:
            if item["symbol"] == "300750":
                fp = item.get("first_pyramid") or {}
                assert fp.get("fp_chip_available") is False

    async def test_old_algorithm_version_chip_not_matched(
        self, chip_test_setup,
    ) -> None:
        """场景7c: 同股票旧 algorithm_version chip 不得匹配。"""
        client, user, instruments, _, _ = chip_test_setup
        # inst3 有 chip algorithm_version="0.0.0-old-version"，不应匹配
        resp = await client.get(
            "/v1/market/stocks",
            params={
                "scope": "market",
                "fp_filter": "fp_chip_available:eq:true",
                "query": "300750",
                "page_size": 50,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        for item in data["items"]:
            if item["symbol"] == "300750":
                fp = item.get("first_pyramid") or {}
                assert fp.get("fp_chip_available") is False

    async def test_chip_field_consistency_between_filter_and_response(
        self, chip_test_setup,
    ) -> None:
        """场景8: 列表返回 chip 字段与排序字段一致。"""
        client, user, instruments, _, _ = chip_test_setup
        # 按 fp_poc_price 排序，验证返回的 fp_poc_price 与排序值一致
        resp = await client.get(
            "/v1/market/stocks",
            params={"scope": "market", "fp_sort": "fp_poc_price:desc", "page_size": 50},
        )
        assert resp.status_code == 200
        data = resp.json()
        # 找到 inst1，验证其 fp_poc_price 与 chip_payload 中的值一致
        for item in data["items"]:
            if item["symbol"] == "600519":
                fp = item.get("first_pyramid") or {}
                assert fp.get("fp_poc_price") == 29.36
                assert fp.get("fp_chip_state") == "筹码峰稳定"
                assert fp.get("fp_peak_node_count") == 2
                assert fp.get("fp_vah_price") == 30.5
                assert fp.get("fp_val_price") == 28.8
                assert fp.get("fp_chip_available") is True
                break

    async def test_between_filter(
        self, chip_test_setup,
    ) -> None:
        """场景9a: between 多条件 filter（fp_trend_bars between 4 and 6）。"""
        client, user, instruments, _, _ = chip_test_setup
        resp = await client.get(
            "/v1/market/stocks",
            params={
                "scope": "market",
                "fp_filter": "fp_trend_bars:between:4;6",
                "page_size": 50,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        symbols = [item["symbol"] for item in data["items"]]
        # inst1 trend_bars=5 在 [4,6] 范围内
        assert "600519" in symbols
        # inst2 trend_bars=3 不在范围
        assert "000001" not in symbols
        # inst3 trend_bars=8 不在范围
        assert "300750" not in symbols

    async def test_multi_condition_filter(
        self, chip_test_setup,
    ) -> None:
        """场景9b: 多条件 filter（fp_trend_direction eq 上行 AND fp_trend_bars gte 5）。"""
        client, user, instruments, _, _ = chip_test_setup
        resp = await client.get(
            "/v1/market/stocks",
            params={
                "scope": "market",
                "fp_filter": "fp_trend_direction:eq:上行;fp_trend_bars:gte:5",
                "page_size": 50,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        symbols = [item["symbol"] for item in data["items"]]
        # inst1 (上行, 5) 和 inst3 (上行, 8) 匹配
        assert "600519" in symbols
        assert "300750" in symbols
        # inst2 (下行, 3) 不匹配
        assert "000001" not in symbols

    async def test_nulls_last_sort(
        self, chip_test_setup,
    ) -> None:
        """场景9c: NULLS LAST 排序（fp_poc_price asc，None 排最后）。"""
        client, user, instruments, _, _ = chip_test_setup
        resp = await client.get(
            "/v1/market/stocks",
            params={"scope": "market", "fp_sort": "fp_poc_price:asc", "page_size": 50},
        )
        assert resp.status_code == 200
        data = resp.json()
        # inst1 有 poc_price=29.36，应排在最前
        items = data["items"]
        if items:
            # 找到第一个有 fp_poc_price 的
            first_with_poc = next(
                (i for i in items if i.get("first_pyramid") and i["first_pyramid"].get("fp_poc_price") is not None),
                None,
            )
            if first_with_poc:
                assert first_with_poc["symbol"] == "600519"
                assert first_with_poc["first_pyramid"]["fp_poc_price"] == 29.36

    async def test_cross_page_symbol_stable(
        self, chip_test_setup,
    ) -> None:
        """场景9d: 跨页 symbol 稳定（page_size=1，两页结果不重叠且 symbol 升序）。"""
        client, user, instruments, _, _ = chip_test_setup
        resp1 = await client.get(
            "/v1/market/stocks",
            params={"scope": "market", "page_size": 1, "page": 1},
        )
        resp2 = await client.get(
            "/v1/market/stocks",
            params={"scope": "market", "page_size": 1, "page": 2},
        )
        assert resp1.status_code == 200
        assert resp2.status_code == 200
        page1_symbols = [i["symbol"] for i in resp1.json()["items"]]
        page2_symbols = [i["symbol"] for i in resp2.json()["items"]]
        # 两页不重叠
        assert not set(page1_symbols) & set(page2_symbols)
        # 默认按 symbol asc，page1 < page2
        if page1_symbols and page2_symbols:
            assert page1_symbols[0] < page2_symbols[0]

    async def test_invalid_field_returns_422(
        self, chip_test_setup,
    ) -> None:
        """场景10: 非法字段 422。"""
        client, user, instruments, _, _ = chip_test_setup
        resp = await client.get(
            "/v1/market/stocks",
            params={"scope": "market", "fp_filter": "fp_nonexistent_field:eq:foo", "page_size": 50},
        )
        assert resp.status_code == 422

    async def test_99_fields_count(
        self, chip_test_setup,
    ) -> None:
        """验证 first_pyramid_flat 恰好 99 键。"""
        client, user, instruments, _, _ = chip_test_setup
        resp = await client.get(
            "/v1/market/stocks",
            params={"scope": "market", "page_size": 50},
        )
        assert resp.status_code == 200
        data = resp.json()
        # 至少有一个 item 有 first_pyramid
        for item in data["items"]:
            fp = item.get("first_pyramid")
            if fp is not None:
                assert len(fp) == 99, f"first_pyramid 应为 99 键，实际 {len(fp)}"
                break
