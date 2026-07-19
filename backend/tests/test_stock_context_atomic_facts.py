"""Atomic Fact Contract V1 - 后端 API / 集成测试（只读，不连生产库）。

测试数据库：conftest 强制 TEST_DATABASE_URL 指向 *_test 库（bz_stock_test），
绝不会触碰生产库。所有写操作经 savepoint 回滚，不污染测试库。

覆盖（用户要求的最小必要集，每组独立运行）：
1. 用户接口不返回 factId / sourcePath / formula / thresholdRef；分母固定 14
2. 缺失 Core 从 core 数组省略，分母仍 14，coreMissing 列 publicKey
3. M3 阈值未确认（thresholdEnabled=False）且不使用 1e-6 容差（1e-12 仍判增加）
4. M5 任一输入缺失时省略；双 true 进入 dataQuality.degradedReasons
5. S1 未知枚举不默认为区间内（直接缺失）
6. S3 越界（>1）省略
7. S7/S8 管理员 sourcePath 随趋势方向变化（dsa_dir>0→high，<0→low）
8. 新格式 summary payload 优先读取（persisted preferred）
9. summary 缺失 / 旧格式 / 版本不匹配 → fallback 重算
10. persisted 与 fallback 输出一致（共用同一纯函数）
11. as_of 条件必须在 SQL LIMIT 之前（12 快照 + as_of 早日期，证明 SQL 先过滤）
12. GET 零写入（请求前后 StockFeatureSnapshot 行数不变）
13. recentChanges 按展示精度过滤浮点噪声（1e-7 差异不产生变化记录）
14. admin 接口保留完整可追溯（factId/sourcePath/rawValue/thresholdRef/featureFlag）
15. 普通用户访问 admin 接口 403
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

# [CHANGE-20260719-001 §五-D] 使用生产者 _SCHEMA_VERSION 替代本地硬编码 = 1
# 与 stock_context.py / watchlist.py / market_stocks_service.py 保持一致
from app.services.feature_snapshot_service import _SCHEMA_VERSION, build_summary_payload
from tests.conftest import AsyncFactory

# factId -> publicKey 映射（用于断言）
_PK = {
    "T1_trend_direction": "trend_direction",
    "T2_aligned_slope": "aligned_slope",
    "T4_trend_age": "trend_duration",
    "T5_slope_ratio": "slope_ratio",
    "M1_momentum_alignment": "momentum_alignment",
    "M2_aligned_momentum": "aligned_momentum",
    "M3_aligned_momentum_delta": "momentum_delta",
    "M5_squeeze_state": "squeeze_state",
    "S1_confirmed_boundary_relation": "boundary_relation",
    "S2_active_dir_relation": "active_dir_relation",
    "S3_active_position": "active_position",
    "S7_dist_favorable_boundary": "dist_favorable",
    "S8_dist_adverse_boundary": "dist_adverse",
    "V3_avg_volume_ratio": "volume_ratio",
}


def _auth_headers(user_id: uuid.UUID) -> dict[str, str]:
    token = create_access_token(str(user_id))
    return {"Authorization": f"Bearer {token}"}


def _base_payload(
    dsa_overrides: dict | None = None,
    swing_overrides: dict | None = None,
    vol_overrides: dict | None = None,
) -> tuple[dict, dict]:
    """构造覆盖 14 Core + 10 Aux 的 payload（含 V1 原始值，验证其不进入 UI）。

    按子结构覆盖：dsa_segment / swing_position / volatility_momentum。
    """
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
                    # CHANGE-20260716-006: confirmed swing 字段（产品观察扩展）
                    "confirmed_swing_high": 10.5,
                    "confirmed_swing_low": 9.5,
                    "price_position_in_confirmed_swing_raw": 0.63,
                    "price_position_in_confirmed_swing_0_1": 0.63,
                },
            }
        }
    }
    tp = {"daily_context": {"daily_sqzmom_change_since_segment_start": 0.001}}
    if dsa_overrides:
        sp["primary"]["1d"]["dsa_segment"].update(dsa_overrides)
    if swing_overrides:
        sp["primary"]["1d"]["swing_position"].update(swing_overrides)
    if vol_overrides:
        sp["primary"]["1d"]["volatility_momentum"].update(vol_overrides)
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
        summary = build_summary_payload(sp, tp, trade_date)
    if summary_with_wrong_atomic:
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


def _all_core(body: dict) -> list[dict]:
    return [f for items in body["core"].values() for f in items]


def _core_pk_set(body: dict) -> set[str]:
    return {f["publicKey"] for f in _all_core(body)}


# =============================================================================
# 1. 用户接口不返回内部字段
# =============================================================================


@pytest.mark.asyncio
async def test_user_context_no_internal_fields_and_denominator_14(
    client: AsyncClient,
    db_session: AsyncSession,
    instrument_factory: AsyncFactory,
    member_with_sub: User,
) -> None:
    inst = await instrument_factory(symbol="ATOMICNOLEAK")
    sp, tp = _base_payload()
    await _make_published_run_and_snapshot(db_session, inst.id, date(2026, 7, 14), sp, tp)

    resp = await client.get(
        f"/api/v1/stocks/{inst.symbol}/context",
        headers=_auth_headers(member_with_sub.id),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 分母固定 14
    assert body["availability"]["coreDenominator"] == 14
    # 用户项不得含内部字段
    forbidden = {"factId", "sourcePath", "formula", "thresholdRef"}
    for item in _all_core(body) + body["auxiliary"]:
        keys = set(item.keys())
        assert forbidden.isdisjoint(keys), f"用户项泄露内部字段: {forbidden & keys}"
    # availability 不得含 factId
    assert "factId" not in body["availability"]


# =============================================================================
# 2. 缺失 Core 省略，分母 14
# =============================================================================


@pytest.mark.asyncio
async def test_missing_core_omitted_denominator_14(
    client: AsyncClient,
    db_session: AsyncSession,
    instrument_factory: AsyncFactory,
    member_with_sub: User,
) -> None:
    inst = await instrument_factory(symbol="ATOMICMISS")
    # 仅 S3 输入缺失（price_position 置 None），其余齐全
    sp, tp = _base_payload(swing_overrides={"price_position_in_active_swing_0_1": None})
    await _make_published_run_and_snapshot(db_session, inst.id, date(2026, 7, 14), sp, tp)

    resp = await client.get(
        f"/api/v1/stocks/{inst.symbol}/context",
        headers=_auth_headers(member_with_sub.id),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    pks = _core_pk_set(body)
    assert "active_position" not in pks, "缺失 S3 必须省略"
    assert body["availability"]["coreDenominator"] == 14
    assert "active_position" in body["availability"]["coreMissing"]
    assert body["availability"]["corePresent"] == 13


# =============================================================================
# 3. M3 阈值未确认，无 1e-6
# =============================================================================


@pytest.mark.asyncio
async def test_m3_threshold_not_confirmed_no_1e6(
    client: AsyncClient,
    db_session: AsyncSession,
    instrument_factory: AsyncFactory,
    member_with_sub: User,
) -> None:
    inst = await instrument_factory(symbol="ATOMICM3")
    # sqzmom_delta_1 = 1e-12（远小于旧 1e-6 容差）应判「增加」
    sp, tp = _base_payload(dsa_overrides={"sqzmom_delta_1": 1e-12})
    await _make_published_run_and_snapshot(db_session, inst.id, date(2026, 7, 14), sp, tp)

    resp = await client.get(
        f"/api/v1/stocks/{inst.symbol}/context",
        headers=_auth_headers(member_with_sub.id),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    m3 = next(f for f in _all_core(body) if f["publicKey"] == "momentum_delta")
    assert m3["categoryLabel"] == "增加", "1e-12 不应被 1e-6 容差吞掉"
    assert m3["thresholdEnabled"] is False, "M3 阈值未确认"


# =============================================================================
# 4. M5 缺失 / 双 true 数据质量
# =============================================================================


@pytest.mark.asyncio
async def test_m5_missing_omitted_and_double_true_in_data_quality(
    client: AsyncClient,
    db_session: AsyncSession,
    instrument_factory: AsyncFactory,
    member_with_sub: User,
) -> None:
    # 4a. 任一输入缺失 → M5 省略
    inst1 = await instrument_factory(symbol="ATOMICM5A")
    sp, tp = _base_payload(vol_overrides={"sqz_on": None, "sqz_off": None})
    await _make_published_run_and_snapshot(db_session, inst1.id, date(2026, 7, 14), sp, tp)
    r1 = await client.get(
        f"/api/v1/stocks/{inst1.symbol}/context",
        headers=_auth_headers(member_with_sub.id),
    )
    b1 = r1.json()
    assert "squeeze_state" not in _core_pk_set(b1), "M5 任一输入缺失必须省略"

    # 4b. 双 true → 省略 + dataQuality.degradedReasons 含 m5_inconsistent
    inst2 = await instrument_factory(symbol="ATOMICM5B")
    sp2, tp2 = _base_payload(vol_overrides={"sqz_on": True, "sqz_off": True})
    await _make_published_run_and_snapshot(db_session, inst2.id, date(2026, 7, 14), sp2, tp2)
    r2 = await client.get(
        f"/api/v1/stocks/{inst2.symbol}/context",
        headers=_auth_headers(member_with_sub.id),
    )
    b2 = r2.json()
    assert "squeeze_state" not in _core_pk_set(b2)
    assert "m5_inconsistent" in (b2["dataQuality"]["degradedReasons"] or [])


# =============================================================================
# 5. S1 未知枚举不默认为区间内
# =============================================================================


@pytest.mark.asyncio
async def test_s1_unknown_enum_not_inside(
    client: AsyncClient,
    db_session: AsyncSession,
    instrument_factory: AsyncFactory,
    member_with_sub: User,
) -> None:
    inst = await instrument_factory(symbol="ATOMICS1")
    sp, tp = _base_payload(swing_overrides={"confirmed_swing_breakout_state": "bogus_unknown_value"})
    await _make_published_run_and_snapshot(db_session, inst.id, date(2026, 7, 14), sp, tp)

    resp = await client.get(
        f"/api/v1/stocks/{inst.symbol}/context",
        headers=_auth_headers(member_with_sub.id),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "boundary_relation" not in _core_pk_set(body), "S1 未知枚举不得默认为区间内"


# =============================================================================
# 6. S3 越界省略
# =============================================================================


@pytest.mark.asyncio
async def test_s3_out_of_range_omitted(
    client: AsyncClient,
    db_session: AsyncSession,
    instrument_factory: AsyncFactory,
    member_with_sub: User,
) -> None:
    inst = await instrument_factory(symbol="ATOMICS3")
    sp, tp = _base_payload(swing_overrides={"price_position_in_active_swing_0_1": 1.5})
    await _make_published_run_and_snapshot(db_session, inst.id, date(2026, 7, 14), sp, tp)

    resp = await client.get(
        f"/api/v1/stocks/{inst.symbol}/context",
        headers=_auth_headers(member_with_sub.id),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "active_position" not in _core_pk_set(body), "S3 越界必须省略"


# =============================================================================
# 7. S7/S8 管理员 sourcePath 随趋势方向变化
# =============================================================================


@pytest.mark.asyncio
async def test_s7_s8_admin_sourcepath_by_trend(
    client: AsyncClient,
    db_session: AsyncSession,
    instrument_factory: AsyncFactory,
    admin_user: User,
) -> None:
    # 上行：S7→high, S8→low
    inst_up = await instrument_factory(symbol="ATOMICS7UP")
    sp_up, tp_up = _base_payload(dsa_overrides={"current_dsa_segment_dir": 1})
    await _make_published_run_and_snapshot(db_session, inst_up.id, date(2026, 7, 14), sp_up, tp_up)
    r_up = await client.get(
        f"/api/v1/admin/stocks/{inst_up.symbol}/debug",
        headers=_auth_headers(admin_user.id),
    )
    b_up = r_up.json()
    s7_up = next(d for d in b_up["atomicFactsDebug"] if d["factId"] == "S7_dist_favorable_boundary")
    s8_up = next(d for d in b_up["atomicFactsDebug"] if d["factId"] == "S8_dist_adverse_boundary")
    assert "distance_to_swing_high_atr" in s7_up["sourcePath"]
    assert "distance_to_swing_low_atr" in s8_up["sourcePath"]

    # 下行：S7→low, S8→high
    inst_dn = await instrument_factory(symbol="ATOMICS7DN")
    sp_dn, tp_dn = _base_payload(dsa_overrides={"current_dsa_segment_dir": -1})
    await _make_published_run_and_snapshot(db_session, inst_dn.id, date(2026, 7, 14), sp_dn, tp_dn)
    r_dn = await client.get(
        f"/api/v1/admin/stocks/{inst_dn.symbol}/debug",
        headers=_auth_headers(admin_user.id),
    )
    b_dn = r_dn.json()
    s7_dn = next(d for d in b_dn["atomicFactsDebug"] if d["factId"] == "S7_dist_favorable_boundary")
    s8_dn = next(d for d in b_dn["atomicFactsDebug"] if d["factId"] == "S8_dist_adverse_boundary")
    assert "distance_to_swing_low_atr" in s7_dn["sourcePath"]
    assert "distance_to_swing_high_atr" in s8_dn["sourcePath"]


# =============================================================================
# 8. 新格式 summary payload 优先读取
# =============================================================================


@pytest.mark.asyncio
async def test_summary_persisted_preferred(
    client: AsyncClient,
    db_session: AsyncSession,
    instrument_factory: AsyncFactory,
    member_with_sub: User,
) -> None:
    sp, tp = _base_payload()
    summary = build_summary_payload(sp, tp, date(2026, 7, 14))
    # 注入仅 stored 才有的标记，证明 context 读取的是持久化 summary 而非重算
    summary["atomic_fact_contract_v1"]["availability"]["warnings"].append("stored_preferred_marker")
    inst = await instrument_factory(symbol="ATOMICPREF")
    await _make_published_run_and_snapshot(
        db_session, inst.id, date(2026, 7, 14), sp, tp, summary_payload=summary,
    )
    resp = await client.get(
        f"/api/v1/stocks/{inst.symbol}/context",
        headers=_auth_headers(member_with_sub.id),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "stored_preferred_marker" in body["availability"]["warnings"]


# =============================================================================
# 9. summary 缺失 / 旧格式 → fallback
# =============================================================================


@pytest.mark.asyncio
async def test_summary_missing_or_old_fallback(
    client: AsyncClient,
    db_session: AsyncSession,
    instrument_factory: AsyncFactory,
    member_with_sub: User,
) -> None:
    sp, tp = _base_payload()

    # 9a. summary 缺失（None）→ fallback 重算
    inst_a = await instrument_factory(symbol="ATOMICFB1")
    await _make_published_run_and_snapshot(
        db_session, inst_a.id, date(2026, 7, 14), sp, tp, summary_payload=None,
    )
    r_a = await client.get(
        f"/api/v1/stocks/{inst_a.symbol}/context",
        headers=_auth_headers(member_with_sub.id),
    )
    b_a = r_a.json()
    s3 = next(f for f in b_a["core"]["structure"] if f["publicKey"] == "active_position")
    assert s3["categoryLabel"] == "中间"
    assert s3["value"] == 0.63

    # 9b. 旧格式 summary（无 atomic_fact_contract_v1）→ fallback
    inst_b = await instrument_factory(symbol="ATOMICFB2")
    await _make_published_run_and_snapshot(
        db_session, inst_b.id, date(2026, 7, 14), sp, tp,
        summary_payload={"legacy_field": "x"},
    )
    r_b = await client.get(
        f"/api/v1/stocks/{inst_b.symbol}/context",
        headers=_auth_headers(member_with_sub.id),
    )
    b_b = r_b.json()
    s3b = next(f for f in b_b["core"]["structure"] if f["publicKey"] == "active_position")
    assert s3b["categoryLabel"] == "中间"

    # 9c. 新键但结构错误（core 无 publicKey）→ fallback
    inst_c = await instrument_factory(symbol="ATOMICFB3")
    await _make_published_run_and_snapshot(
        db_session, inst_c.id, date(2026, 7, 14), sp, tp,
        summary_payload={"atomic_fact_contract_v1": {"availability": {"coreDenominator": 14}, "core": {}}},
    )
    r_c = await client.get(
        f"/api/v1/stocks/{inst_c.symbol}/context",
        headers=_auth_headers(member_with_sub.id),
    )
    assert r_c.status_code == 200, r_c.text


# =============================================================================
# 10. persisted 与 fallback 输出一致
# =============================================================================


@pytest.mark.asyncio
async def test_persisted_matches_fallback(
    client: AsyncClient,
    db_session: AsyncSession,
    instrument_factory: AsyncFactory,
    member_with_sub: User,
) -> None:
    sp, tp = _base_payload()
    summary = build_summary_payload(sp, tp, date(2026, 7, 14))
    inst = await instrument_factory(symbol="ATOMICCONSIST")
    await _make_published_run_and_snapshot(
        db_session, inst.id, date(2026, 7, 14), sp, tp, summary_payload=summary,
    )
    resp = await client.get(
        f"/api/v1/stocks/{inst.symbol}/context",
        headers=_auth_headers(member_with_sub.id),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # persisted summary 的 core 必须等于同一纯函数 recompute 的 core
    recompute = compute_atomic_facts(sp, tp)["core"]
    assert body["core"] == recompute


# =============================================================================
# 11. as_of 必须在 SQL LIMIT 之前
# =============================================================================


@pytest.mark.asyncio
async def test_as_of_sql_filter_before_limit(
    client: AsyncClient,
    db_session: AsyncSession,
    instrument_factory: AsyncFactory,
    member_with_sub: User,
) -> None:
    inst = await instrument_factory(symbol="ATOMICASOF")
    # 12 个连续交易日的已发布快照（07-01..07-12），每日 slope 不同以产生变化
    for i in range(12):
        d = date(2026, 7, 1) + timedelta(days=i)
        sp, tp = _base_payload(
            dsa_overrides={"current_dsa_segment_slope_atr_per_bar": round(0.0100 + i * 0.0005, 6)},
        )
        await _make_published_run_and_snapshot(db_session, inst.id, d, sp, tp)

    # CHANGE-20260716-006: as_of = 07-05，SQL 先过滤 trade_date<=07-05，再 DESC LIMIT 2
    # 正确实现应得到 07-05 和 07-04 两个快照 → 1 个过渡（asOf=07-05）
    # 旧实现（先取最新 10 个=07-03..07-12 再内存过滤）→ 会得到未来日期的快照
    resp = await client.get(
        f"/api/v1/stocks/{inst.symbol}/context?as_of=2026-07-05",
        headers=_auth_headers(member_with_sub.id),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["asOf"] == "2026-07-05"
    changes = body["recentChanges"]
    asof_set = {c["asOf"] for c in changes}
    for a in asof_set:
        assert a <= "2026-07-05", f"发现未来变化 {a}"
    # CHANGE-006: 仅 2 个快照（07-04, 07-05）→ 1 个过渡，asOf=07-05
    assert asof_set == {"2026-07-05"}, f"应有 1 个过渡日期 07-05，实际 {asof_set}"
    # latestChangesFrom/AsOf 标注正确
    assert body["latestChangesAsOf"] == "2026-07-05"
    assert body["latestChangesFrom"] == "2026-07-04"
    # productObservations 存在（confirmed_swing_position）
    assert "productObservations" in body
    assert "structure" in body["productObservations"]


# =============================================================================
# 12. GET 零写入
# =============================================================================


@pytest.mark.asyncio
async def test_get_context_writes_nothing(
    client: AsyncClient,
    db_session: AsyncSession,
    instrument_factory: AsyncFactory,
    member_with_sub: User,
) -> None:
    inst = await instrument_factory(symbol="ATOMICWRITE")
    sp, tp = _base_payload()
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
# 13. recentChanges 按展示精度过滤浮点噪声
# =============================================================================


@pytest.mark.asyncio
async def test_recent_changes_precision_filters_noise(
    client: AsyncClient,
    db_session: AsyncSession,
    instrument_factory: AsyncFactory,
    member_with_sub: User,
) -> None:
    inst = await instrument_factory(symbol="ATOMICNOISE")
    # 两天 payload 仅 T2 slope 相差 1e-7（展示 4 位小数下相等），其余完全相同
    sp1, tp1 = _base_payload(dsa_overrides={"current_dsa_segment_slope_atr_per_bar": 0.01230000})
    sp2, tp2 = _base_payload(dsa_overrides={"current_dsa_segment_slope_atr_per_bar": 0.01230001})
    await _make_published_run_and_snapshot(db_session, inst.id, date(2026, 7, 13), sp1, tp1)
    await _make_published_run_and_snapshot(db_session, inst.id, date(2026, 7, 14), sp2, tp2)

    resp = await client.get(
        f"/api/v1/stocks/{inst.symbol}/context",
        headers=_auth_headers(member_with_sub.id),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 1e-7 差异在 4 位小数下不可见 → 不应产生 aligned_slope 变化记录
    aligned_changes = [c for c in body["recentChanges"] if c["publicKey"] == "aligned_slope"]
    assert aligned_changes == [], f"浮点噪声不应产生变化: {aligned_changes}"


# =============================================================================
# 14. admin 接口完整可追溯
# =============================================================================


@pytest.mark.asyncio
async def test_admin_debug_full_traceability(
    client: AsyncClient,
    db_session: AsyncSession,
    instrument_factory: AsyncFactory,
    admin_user: User,
    member_user: User,
) -> None:
    inst = await instrument_factory(symbol="ATOMICADMIN")
    sp, tp = _base_payload()
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


# =============================================================================
# 15. 公共响应 meta 三版本字段
# =============================================================================


@pytest.mark.asyncio
async def test_response_meta_versions(
    client: AsyncClient,
    db_session: AsyncSession,
    instrument_factory: AsyncFactory,
    member_with_sub: User,
) -> None:
    """公共响应 meta 必须含三版本字段（前端禁止硬编码 V4.13）。"""
    inst = await instrument_factory(symbol="ATOMICMETA")
    sp, tp = _base_payload()
    await _make_published_run_and_snapshot(db_session, inst.id, date(2026, 7, 14), sp, tp)

    resp = await client.get(
        f"/api/v1/stocks/{inst.symbol}/context",
        headers=_auth_headers(member_with_sub.id),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "meta" in body
    meta = body["meta"]
    assert meta["payloadVersion"] == "1"
    assert meta["researchFreezeVersion"] == "V4.13"
    assert meta["presentationVersion"] == "Atomic Fact Presentation V1"

    # 空态响应也必须含 meta
    inst_empty = await instrument_factory(symbol="ATOMICMETA2")
    r_empty = await client.get(
        f"/api/v1/stocks/{inst_empty.symbol}/context",
        headers=_auth_headers(member_with_sub.id),
    )
    assert r_empty.status_code == 200
    b_empty = r_empty.json()
    assert b_empty["meta"]["researchFreezeVersion"] == "V4.13"
    assert b_empty["meta"]["payloadVersion"] == "1"


# =============================================================================
# 16. as_of 周末/节假日返回最近批次
# =============================================================================


@pytest.mark.asyncio
async def test_as_of_weekend_returns_previous_batch(
    client: AsyncClient,
    db_session: AsyncSession,
    instrument_factory: AsyncFactory,
    member_with_sub: User,
) -> None:
    """as_of 截止日期语义：周末/无批次日期返回之前最近一次已发布状态（非空态）。

    构造：07-14（周一）有批次；07-18（周六）无批次。
    as_of=07-18 应返回 07-14 的状态（asOf='2026-07-14'），而非空态。
    """
    inst = await instrument_factory(symbol="ATOMICASOFWK")
    sp, tp = _base_payload()
    await _make_published_run_and_snapshot(db_session, inst.id, date(2026, 7, 14), sp, tp)

    # as_of=2026-07-18（周六）应回退到 07-14
    resp = await client.get(
        f"/api/v1/stocks/{inst.symbol}/context?as_of=2026-07-18",
        headers=_auth_headers(member_with_sub.id),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["asOf"] == "2026-07-14", (
        f"as_of=07-18（周末）应回退到最近 07-14，实际 asOf={body['asOf']}"
    )
    assert body["dataQuality"]["hasSucceededRun"] is True
    assert body["dataQuality"]["hasSnapshot"] is True

    # as_of 早于所有批次（07-10）→ 空态
    resp_early = await client.get(
        f"/api/v1/stocks/{inst.symbol}/context?as_of=2026-07-10",
        headers=_auth_headers(member_with_sub.id),
    )
    body_early = resp_early.json()
    assert body_early["asOf"] is None
    assert body_early["dataQuality"]["hasSucceededRun"] is False


# =============================================================================
# 17. Legacy snapshot 降级原因进入 degradedReasons
# =============================================================================


@pytest.mark.asyncio
async def test_legacy_snapshot_reason_in_degraded_reasons(
    client: AsyncClient,
    db_session: AsyncSession,
    instrument_factory: AsyncFactory,
    member_with_sub: User,
) -> None:
    """Legacy snapshot 存在但 source_run_id 缺失/歧义时，原因进入 degradedReasons。

    构造：snapshot.source_run_id = NULL（snapshot_run_not_linked）。
    期望：dataQuality.hasSnapshot=True, reasonCode='snapshot_run_not_linked',
          degradedReasons 含 'snapshot_run_not_linked'。
    """
    inst = await instrument_factory(symbol="ATOMICLEGACY")
    sp, tp = _base_payload()
    # 正常构造 run + snapshot，但人为将 snapshot.source_run_id 置 NULL
    # legacy 回退查询按 (instrument_id, trade_date, schema_version,
    # primary_timeframe, secondary_timeframe, adj) 匹配，故 run 与 snapshot 必须一致
    now = datetime.now(UTC)
    run = StockFeatureSnapshotRun(
        schema_version=_SCHEMA_VERSION,
        status=STATUS_SUCCEEDED,
        run_type="scheduled",
        trade_date=date(2026, 7, 14),
        started_at=now,
        finished_at=now,
        published_at=now,
        metadata_={"scope": "full"},
        primary_timeframe="1d",
        secondary_timeframe="15m",
        adj="hfq",
    )
    db_session.add(run)
    await db_session.flush()

    snapshot = StockFeatureSnapshot(
        instrument_id=inst.id,
        trade_date=date(2026, 7, 14),
        primary_timeframe="1d",
        secondary_timeframe="15m",
        adj="hfq",
        schema_version=_SCHEMA_VERSION,
        source_run_id=None,  # legacy: 未关联 run
        structural_payload=sp,
        temporal_payload=tp,
        summary_payload=build_summary_payload(sp, tp, date(2026, 7, 14)),
        source_primary_bar_time=datetime(2026, 7, 14, 15, 0, tzinfo=UTC),
        source_secondary_bar_time=datetime(2026, 7, 14, 15, 0, tzinfo=UTC),
    )
    db_session.add(snapshot)
    await db_session.flush()

    resp = await client.get(
        f"/api/v1/stocks/{inst.symbol}/context",
        headers=_auth_headers(member_with_sub.id),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    dq = body["dataQuality"]
    assert dq["hasSnapshot"] is True, "legacy snapshot 存在应标记 hasSnapshot=True"
    assert dq["reasonCode"] == "snapshot_run_not_linked"
    assert "snapshot_run_not_linked" in dq["degradedReasons"], (
        f"legacy 原因必须进入 degradedReasons，实际 {dq['degradedReasons']}"
    )


# =============================================================================
# 18. 持久化 Payload 严格 schema fallback（损坏/未知key/重复key/错维度/错availability）
# =============================================================================


def _make_corrupt_summary(sp: dict, tp: dict, trade_date: date, *, corrupt_type: str) -> dict:
    """构造各类损坏的 summary_payload.atomic_fact_contract_v1 用于 fallback 测试。"""
    summary = build_summary_payload(sp, tp, trade_date)
    afc = summary["atomic_fact_contract_v1"]

    if corrupt_type == "second_item_corrupt":
        # 第二项损坏：core.trend[1] 缺 publicKey
        if len(afc["core"]["trend"]) >= 2:
            del afc["core"]["trend"][1]["publicKey"]
    elif corrupt_type == "unknown_public_key":
        # 未知 publicKey
        if afc["core"]["trend"]:
            afc["core"]["trend"][0]["publicKey"] = "unknown_key_xyz"
    elif corrupt_type == "duplicate_public_key":
        # 重复 publicKey
        if len(afc["core"]["trend"]) >= 2:
            afc["core"]["trend"][1]["publicKey"] = afc["core"]["trend"][0]["publicKey"]
    elif corrupt_type == "wrong_dimension":
        # 错维度：把 momentum 的 publicKey 放进 trend
        if afc["core"]["momentum"] and len(afc["core"]["trend"]) >= 1:
            afc["core"]["trend"][0]["publicKey"] = afc["core"]["momentum"][0]["publicKey"]
    elif corrupt_type == "wrong_availability":
        # 错误 availability：corePresent 与实际数组不一致
        afc["availability"]["corePresent"] = 99
    elif corrupt_type == "contains_debug":
        # 含 debug 字段（extra=forbid 拒绝）
        afc["debug"] = [{"factId": "leaked"}]
    elif corrupt_type == "forbidden_key_t3":
        # T3 混入（禁止的 publicKey）
        if afc["core"]["trend"]:
            afc["core"]["trend"][0]["publicKey"] = "trend_efficiency"

    return summary


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corrupt_type",
    [
        "second_item_corrupt",
        "unknown_public_key",
        "duplicate_public_key",
        "wrong_dimension",
        "wrong_availability",
        "contains_debug",
        "forbidden_key_t3",
    ],
)
async def test_persisted_schema_strict_fallback(
    client: AsyncClient,
    db_session: AsyncSession,
    instrument_factory: AsyncFactory,
    member_with_sub: User,
    corrupt_type: str,
) -> None:
    """PersistedAtomicFactsPayload 严格校验：任一损坏 → fallback 重算，不得 500。"""
    sp, tp = _base_payload()
    summary = _make_corrupt_summary(sp, tp, date(2026, 7, 14), corrupt_type=corrupt_type)
    inst = await instrument_factory(symbol=f"ATOMICFB{corrupt_type[:4].upper()}")
    await _make_published_run_and_snapshot(
        db_session, inst.id, date(2026, 7, 14), sp, tp, summary_payload=summary,
    )

    resp = await client.get(
        f"/api/v1/stocks/{inst.symbol}/context",
        headers=_auth_headers(member_with_sub.id),
    )
    assert resp.status_code == 200, (
        f"corrupt_type={corrupt_type} 应 fallback 重算而非 500: {resp.text}"
    )
    body = resp.json()
    # fallback 重算后 S3 仍可用（证明走的是纯函数）
    s3 = next(f for f in body["core"]["structure"] if f["publicKey"] == "active_position")
    assert s3["categoryLabel"] == "中间"


# =============================================================================
# 19. 全缺失 persisted payload 仍合法（修复旧 bug：全空 core 数组被错误拒绝）
# =============================================================================


@pytest.mark.asyncio
async def test_persisted_all_missing_is_valid(
    client: AsyncClient,
    db_session: AsyncSession,
    instrument_factory: AsyncFactory,
    member_with_sub: User,
) -> None:
    """全缺失 persisted payload（所有 core 数组为空）应被接受，不触发 fallback。

    旧 _is_valid_stored_afc 在所有 core 数组为空时返回 False（bug），新 schema 应接受。
    """
    # 构造全缺失的 sp（所有关键字段 None）
    sp, tp = _base_payload(
        dsa_overrides={
            "current_dsa_segment_dir": None,
            "current_dsa_segment_slope_atr_per_bar": None,
            "current_dsa_segment_age_bars": None,
            "prev_dsa_segment_slope_atr_per_bar": None,
            "current_segment_volume_sum": None,
            "prev_segment_volume_sum": None,
        },
        vol_overrides={"sqzmom_val": None, "sqzmom_delta_1": None, "sqz_on": None, "sqz_off": None},
        swing_overrides={
            "confirmed_swing_breakout_state": None,
            "active_swing_dir": None,
            "developing_swing_dir": None,
            "price_position_in_active_swing_0_1": None,
            "distance_to_swing_high_atr": None,
            "distance_to_swing_low_atr": None,
        },
    )
    summary = build_summary_payload(sp, tp, date(2026, 7, 14))
    inst = await instrument_factory(symbol="ATOMICALLEMPTY")
    await _make_published_run_and_snapshot(
        db_session, inst.id, date(2026, 7, 14), sp, tp, summary_payload=summary,
    )

    resp = await client.get(
        f"/api/v1/stocks/{inst.symbol}/context",
        headers=_auth_headers(member_with_sub.id),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 全缺失：corePresent=0，coreMissing 含全部 14 个 publicKey
    assert body["availability"]["corePresent"] == 0
    assert len(body["availability"]["coreMissing"]) == 14
    # all core arrays empty
    for dim in ("trend", "momentum", "structure", "volume"):
        assert body["core"][dim] == [], f"{dim} 应为空"
