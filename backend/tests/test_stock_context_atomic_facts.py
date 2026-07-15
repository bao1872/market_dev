"""Atomic Fact Contract V1 - 后端 API / 集成测试（只读，不连生产库）。

测试数据库：conftest 强制 TEST_DATABASE_URL 指向 *_test 库（bz_stock_test），
绝不会触碰生产库。所有写操作经 savepoint 回滚，不污染测试库。

覆盖（用户要求的最小必要集，每组独立运行）：
1. context 空态：无已发布 run → core 分母 14 全缺失 + reasonCode
2. context 有快照：返回 14 Core + Auxiliary，S2 存在，V1 不出现
3. GET 零写入：请求前后 StockFeatureSnapshot 行数不变
4. as_of 无未来：未来 as_of → 空态；as_of 精确匹配；recentChanges 不含未来
5. feature snapshot summary 持久化 + 旧快照 fallback（同一纯函数，禁止两套公式）
6. admin debug 权限（member 403）+ 字段可追溯（Fact ID/rawValue/path/thresholdRef/flag）
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.models.stock_feature_snapshot import StockFeatureSnapshot
from app.models.stock_feature_snapshot_run import (
    STATUS_SUCCEEDED,
    StockFeatureSnapshotRun,
)
from app.models.user import User
from app.services.atomic_fact_contract_service import compute_atomic_facts
from app.services.feature_snapshot_service import build_summary_payload
from tests.conftest import AsyncFactory

_SCHEMA_VERSION = 1


def _auth_headers(user_id: uuid.UUID) -> dict[str, str]:
    token = create_access_token(str(user_id))
    return {"Authorization": f"Bearer {token}"}


def _sample_payloads() -> tuple[dict, dict]:
    """构造覆盖 14 Core + 10 Aux 的 payload（含 V1 原始值，验证其不进入 UI）。"""
    sp = {
        "primary": {
            "1d": {
                "dsa_segment": {
                    "current_dsa_segment_dir": 1,
                    "current_dsa_segment_slope_atr_per_bar": 0.0123,
                    "prev_dsa_segment_slope_atr_per_bar": 0.0100,
                    "current_dsa_segment_age_bars": 12,
                    "prev_dsa_segment_age_bars": 10,
                    "current_segment_volume_sum": 1200000.0,
                    "prev_segment_volume_sum": 900000.0,
                    "current_dsa_segment_efficiency_0_1": 0.7,
                    "prev_dsa_segment_efficiency_0_1": 0.6,
                    "current_segment_return_per_volume": 0.000123,
                    "return_per_volume_ratio": 0.5,
                    "current_vs_prev_volume_ratio": 1.33,  # V1 原始值（仅 DB 调试）
                },
                "volatility_momentum": {
                    "sqzmom_val": 0.002,
                    "sqzmom_delta_1": 0.0003,
                    "sqz_on": False,
                    "sqz_off": True,
                },
                "swing_position": {
                    "confirmed_swing_breakout_state": "inside",
                    "active_swing_dir": 1,
                    "developing_swing_dir": 1,
                    "price_position_in_active_swing_0_1": 0.63,
                    "price_position_in_developing_swing_0_1": 0.5,
                    "distance_to_swing_high_atr": 2.5,
                    "distance_to_swing_low_atr": -1.2,
                },
            }
        }
    }
    tp = {"daily_context": {"daily_sqzmom_change_since_segment_start": 0.001}}
    return sp, tp


@pytest_asyncio.fixture
async def admin_user(user_factory: AsyncFactory[User]) -> User:
    return await user_factory(email="admin-atomic@example.com", roles=["admin"])


@pytest_asyncio.fixture
async def member_user(user_factory: AsyncFactory[User]) -> User:
    return await user_factory(email="member-atomic@example.com", roles=["member"])


@pytest_asyncio.fixture
async def member_with_sub(
    user_factory: AsyncFactory[User],
    make_user_eligible: AsyncFactory[User],
) -> User:
    user = await user_factory(email="member-sub-atomic@example.com")
    await make_user_eligible(user)
    return user


async def _make_published_run_and_snapshot(
    db_session: AsyncSession,
    instrument_id: uuid.UUID,
    trade_date: date,
    sp: dict,
    tp: dict,
    summary_payload: dict | None = None,
    summary_with_wrong_atomic: bool = False,
) -> None:
    """构造一个 succeeded + published + full scope 的 run 及其快照（source_run_id 直连）。"""
    now = datetime.now(UTC)
    run = StockFeatureSnapshotRun(
        schema_version=_SCHEMA_VERSION,
        status=STATUS_SUCCEEDED,
        run_type="scheduled",
        trade_date=trade_date,
        started_at=now,
        finished_at=now,
        published_at=now,
        metadata_={"scope": "full"},
    )
    db_session.add(run)
    await db_session.flush()

    summary = summary_payload
    if summary is None and not summary_with_wrong_atomic:
        # 新快照真实写入 summary（含 atomic_fact_contract_v1）
        summary = build_summary_payload(sp, tp, trade_date)
    if summary_with_wrong_atomic:
        # 故意写入错误/陈旧的 atomic_fact_contract_v1，验证 context 从 payload 重算而非信任 summary
        summary = {"atomic_fact_contract_v1": {"__corrupt__": True, "core": {}}}

    snapshot = StockFeatureSnapshot(
        instrument_id=instrument_id,
        trade_date=trade_date,
        primary_timeframe="1d",
        secondary_timeframe="15m",
        adj="hfq",
        schema_version=_SCHEMA_VERSION,
        source_run_id=run.id,
        structural_payload=sp,
        temporal_payload=tp,
        summary_payload=summary,
        source_primary_bar_time=datetime(
            trade_date.year, trade_date.month, trade_date.day, 15, 0, tzinfo=UTC
        ),
        source_secondary_bar_time=datetime(
            trade_date.year, trade_date.month, trade_date.day, 15, 0, tzinfo=UTC
        ),
    )
    db_session.add(snapshot)
    await db_session.flush()


# =============================================================================
# 1. context 空态
# =============================================================================


@pytest.mark.asyncio
async def test_context_empty_when_no_published_run(
    client: AsyncClient,
    db_session: AsyncSession,
    instrument_factory: AsyncFactory,
    member_with_sub: User,
) -> None:
    inst = await instrument_factory(symbol="ATOMICEMPTY")
    resp = await client.get(
        f"/api/v1/stocks/{inst.symbol}/context",
        headers=_auth_headers(member_with_sub.id),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["contractVersion"] == "Atomic Fact Contract V1"
    assert body["asOf"] is None
    assert body["availability"]["coreDenominator"] == 14
    assert body["availability"]["corePresent"] == 0
    assert len(body["availability"]["coreMissing"]) == 14
    assert body["dataQuality"]["reasonCode"] == "no_published_full_run"


# =============================================================================
# 2. context 有快照
# =============================================================================


@pytest.mark.asyncio
async def test_context_with_snapshot_returns_core_and_aux(
    client: AsyncClient,
    db_session: AsyncSession,
    instrument_factory: AsyncFactory,
    member_with_sub: User,
) -> None:
    inst = await instrument_factory(symbol="ATOMICOK")
    sp, tp = _sample_payloads()
    await _make_published_run_and_snapshot(db_session, inst.id, date(2026, 7, 14), sp, tp)

    resp = await client.get(
        f"/api/v1/stocks/{inst.symbol}/context",
        headers=_auth_headers(member_with_sub.id),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 四组 Core 齐全
    structure = {dim: body["core"].get(dim, []) for dim in ("trend", "momentum", "structure", "volume")}
    all_core = [f for items in structure.values() for f in items]
    assert len(all_core) == 14
    assert body["availability"]["coreDenominator"] == 14
    assert body["availability"]["corePresent"] == 14
    # S2 存在
    ids = [f["factId"] for f in all_core]
    assert "S2_active_dir_relation" in ids
    # V1（被拒事实 V1_cumulative_volume_ratio）永不进入用户 payload
    assert "V1_cumulative_volume_ratio" not in str(body)
    # Auxiliary 存在且默认隐藏，T3/T6 关闭
    aux_ids = [f["factId"] for f in body["auxiliary"]]
    assert "T3_trend_efficiency" not in aux_ids
    assert "T6_efficiency_delta" not in aux_ids
    assert body["availability"]["v1Present"] is False
    assert body["availability"]["rejectedPresent"] is False


# =============================================================================
# 3. GET 零写入
# =============================================================================


@pytest.mark.asyncio
async def test_get_context_writes_nothing(
    client: AsyncClient,
    db_session: AsyncSession,
    instrument_factory: AsyncFactory,
    member_with_sub: User,
) -> None:
    inst = await instrument_factory(symbol="ATOMICWRITE")
    sp, tp = _sample_payloads()
    await _make_published_run_and_snapshot(db_session, inst.id, date(2026, 7, 14), sp, tp)

    before = (
        await db_session.execute(select(func.count()).select_from(StockFeatureSnapshot))
    ).scalar_one()

    resp = await client.get(
        f"/api/v1/stocks/{inst.symbol}/context",
        headers=_auth_headers(member_with_sub.id),
    )
    assert resp.status_code == 200, resp.text

    after = (
        await db_session.execute(select(func.count()).select_from(StockFeatureSnapshot))
    ).scalar_one()
    assert before == after, "GET /context 不得写入 StockFeatureSnapshot"


# =============================================================================
# 4. as_of 无未来信息
# =============================================================================


@pytest.mark.asyncio
async def test_as_of_future_returns_empty_and_no_future_changes(
    client: AsyncClient,
    db_session: AsyncSession,
    instrument_factory: AsyncFactory,
    member_with_sub: User,
) -> None:
    inst = await instrument_factory(symbol="ATOMICFUTURE")
    sp, tp = _sample_payloads()
    # 三个连续交易日的已发布快照
    for d in (date(2026, 7, 12), date(2026, 7, 13), date(2026, 7, 14)):
        await _make_published_run_and_snapshot(db_session, inst.id, d, sp, tp)

    # 未来 as_of（无 run）→ 空态
    resp = await client.get(
        f"/api/v1/stocks/{inst.symbol}/context?as_of=2026-07-30",
        headers=_auth_headers(member_with_sub.id),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["dataQuality"]["reasonCode"] == "no_published_full_run"

    # as_of = 07-13：recentChanges 不得包含 07-14 的未来变化
    resp = await client.get(
        f"/api/v1/stocks/{inst.symbol}/context?as_of=2026-07-13",
        headers=_auth_headers(member_with_sub.id),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["asOf"] == "2026-07-13"
    for ch in body["recentChanges"]:
        assert ch["asOf"] <= "2026-07-13", f"发现未来变化 {ch['asOf']}"


# =============================================================================
# 5. summary 持久化 + 旧快照 fallback（单公式）
# =============================================================================


@pytest.mark.asyncio
async def test_summary_persists_atomic_facts_and_context_recomputes_from_payload(
    client: AsyncClient,
    db_session: AsyncSession,
    instrument_factory: AsyncFactory,
    member_with_sub: User,
) -> None:
    sp, tp = _sample_payloads()
    # build_summary_payload 必须包含 atomic_fact_contract_v1 且等于纯函数结果
    summary = build_summary_payload(sp, tp, date(2026, 7, 14))
    assert "atomic_fact_contract_v1" in summary
    assert summary["atomic_fact_contract_v1"] == compute_atomic_facts(sp, tp)

    # 旧快照：summary 中写入错误 atomic_fact_contract_v1，context 必须从 payload 重算
    inst = await instrument_factory(symbol="ATOMICFALLBACK")
    await _make_published_run_and_snapshot(
        db_session, inst.id, date(2026, 7, 14), sp, tp,
        summary_with_wrong_atomic=True,
    )
    resp = await client.get(
        f"/api/v1/stocks/{inst.symbol}/context",
        headers=_auth_headers(member_with_sub.id),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 从 payload 重算得到正确值（S3=0.63 → MIDDLE），而非错误的 summary
    s3 = next(
        f for f in body["core"]["structure"] if f["factId"] == "S3_active_position"
    )
    assert s3["category"] == "MIDDLE"
    assert s3["value"] == 0.63


# =============================================================================
# 6. admin debug 权限 + 字段可追溯
# =============================================================================


@pytest.mark.asyncio
async def test_admin_debug_fields_and_member_forbidden(
    client: AsyncClient,
    db_session: AsyncSession,
    instrument_factory: AsyncFactory,
    admin_user: User,
    member_user: User,
) -> None:
    inst = await instrument_factory(symbol="ATOMICADMIN")
    sp, tp = _sample_payloads()
    await _make_published_run_and_snapshot(db_session, inst.id, date(2026, 7, 14), sp, tp)

    # member 访问 admin debug → 403
    r_member = await client.get(
        f"/api/v1/admin/stocks/{inst.symbol}/debug",
        headers=_auth_headers(member_user.id),
    )
    assert r_member.status_code == 403, r_member.text

    # admin 访问 → 200 + rawDebug + atomicFactsDebug 可追溯
    r_admin = await client.get(
        f"/api/v1/admin/stocks/{inst.symbol}/debug",
        headers=_auth_headers(admin_user.id),
    )
    assert r_admin.status_code == 200, r_admin.text
    body = r_admin.json()
    assert body["rawDebug"] is not None
    assert body["rawDebug"]["structuralPayload"] is not None
    assert body["rawDebug"]["summaryPayload"] is not None
    debug = body.get("atomicFactsDebug", [])
    assert len(debug) > 0
    # 每个事实可追溯 Fact ID / rawValue / path / thresholdRef / featureFlag
    for item in debug:
        assert item["factId"]
        assert "rawValue" in item
        assert "sourcePath" in item
        assert "thresholdRef" in item
        assert "thresholdEnabled" in item
        assert "featureFlag" in item
    # V1（被拒事实）不出现在 debug（仅 core+aux）
    debug_ids = [d["factId"] for d in debug]
    assert "V1_cumulative_volume_ratio" not in debug_ids
    # T3/T6 featureFlag=false
    t3 = [d for d in debug if d["factId"] == "T3_trend_efficiency"]
    if t3:
        assert t3[0]["featureFlag"] is False
