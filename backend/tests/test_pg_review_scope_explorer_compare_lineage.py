"""[SLICE 5 / Explorer] compare-first read path published-run lineage + family cohort PG closure.

真实 PostgreSQL 验证库（bz_stock_verify_<SHA>），需 PANJI_REMOTE_VERIFY_DB_TEST=1。
PURE_UNIT_TEST=1 下由 conftest 自动 skip。

本文件钉死 SLICE 5 新增正式 read path 的最高风险不变量（spec §十 + §14）：

    ReviewScopeObservationFact
    LEFT JOIN ReviewScopeCompositionSnapshot
    （join key = review_run_id + trade_date + scope_type + scope_key）
    并把该 SQL 结果用于真实 Explorer compareFacts。

1. 同日 A=formally published / B=后跑未发布，两者为各自 scope 写出**截然不同**的
   regime_strength / equal_weight_return / capital_tilt / migration：
   - Explorer read 只解析到 A 的 review_run_id（FORMAL REVIEW READ OWNER）；
   - compareFacts 只来自 A，B 不得污染；
   - Composition LEFT JOIN 只匹配 A 的 review_run_id（B 的 composition 不得 JOIN 进来）。
2. family cohort：industry_l1 与 concept 同时存在时，peer percentile 分 family 计算，
   不得跨 family（industry_l1 的 scope 不得拿 concept 的 scope 当 peer）。
3. [SLICE 5 identity] 正式身份是复合 (scope_type, scope_key)：industry_l1 与 concept
   可共用同一 scope_key（shared_scope），两份 facts 必须完全独立、percentile 各自
   family 内计算，绝不互相覆盖。

DB identity（fail-closed）：APP_ENV==verification 且 current_database() 匹配
^bz_stock_verify_[0-9a-f]{40}$ 且 != bz_stock。

运行（远程验证运行时，非本地/CI）：
    panji-verify run --sha <FULL_SHA> --plan targeted-pg
"""
# ruff: noqa: UP017  (验证容器 Python<3.11，使用 timezone.utc)
from __future__ import annotations

import os
import re
import uuid
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select, text

from app.db import AsyncSessionLocal
from app.domain.review.analysis.cross_sectional import compute_cross_sectional
from app.domain.review.versions import REVIEW_ALGORITHM_VERSION
from app.models.market_review import (
    MarketReviewRun,
    ReviewScopeCompositionSnapshot,
    ReviewScopeObservationFact,
)
from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun
from app.services.review_publication_service import (
    PUBLICATION_KIND_MARKET_REVIEW,
    SCOPE_KEY_REVIEW,
    SCOPE_TYPE_REVIEW,
    get_published_review_run_id,
    is_formally_published_review_run,
    publish_review,
)
from app.services.review_scope_explorer_service import list_review_scope_compare

pytestmark = pytest.mark.postgres

_VERIFY_DB_RE = re.compile(r"^bz_stock_verify_[0-9a-f]{40}$")

# 与其它 PG 用例隔离（不同于 price-history 2099-09-03 / 2099-09-04）。
T = date(2099, 9, 6)
IND = "industry_l1"
CON = "concept"
SHARED = "shared_scope"  # 两 family 共用 scope_key，验证复合身份

# 6 个 / family（compute_cross_sectional 要求 valid_peer_count>=5，即排除自身后
# 至少 5 个 peer，故每 family 至少 6 个 scope 才能拿到 ready percentile）。
IND_KEYS = [f"exp_cmp_ind_{c}" for c in "abcdef"]
CON_KEYS = [f"exp_cmp_con_{c}" for c in "abcdef"]
# A 全量 scope 身份（含 shared_scope 两 family，共 14 个）
ALL_IDS = (
    {(IND, k) for k in IND_KEYS}
    | {(CON, k) for k in CON_KEYS}
    | {(IND, SHARED), (CON, SHARED)}
)

# A=published 的 regime_strength（两 family 明显分离，使"分 family"与"跨 family"
# 算出的 percentile 一定不同）。
IND_RS = [0.10, 0.20, 0.30, 0.40, 0.50, 0.60]
CON_RS = [0.70, 0.71, 0.72, 0.73, 0.74, 0.75]
# A=published 的 equal_weight_return（同理分离）。
IND_EWR = [0.010, 0.020, 0.030, 0.040, 0.050, 0.060]
CON_EWR = [0.110, 0.120, 0.130, 0.140, 0.150, 0.160]
# A=published 的 Composition（LEFT JOIN 取此）：capital_tilt / migration。
IND_CT = [0.004, 0.006, 0.008, 0.010, 0.012, 0.014]
CON_CT = [0.104, 0.106, 0.108, 0.110, 0.112, 0.114]
IND_MIG = [0.11, 0.13, 0.15, 0.17, 0.19, 0.21]
CON_MIG = [0.31, 0.33, 0.35, 0.37, 0.39, 0.41]
# A=published 的 shared_scope（落在各自 family 分布内，确保 ready percentile）
SHARED_RS_IND, SHARED_EWR_IND, SHARED_CT_IND, SHARED_MIG_IND = 0.25, 0.025, 0.007, 0.16
SHARED_RS_CON, SHARED_EWR_CON, SHARED_CT_CON, SHARED_MIG_CON = 0.72, 0.122, 0.107, 0.36

# B=未发布 的极端值：若 lineage 失效，Explorer 会误读这些 0.99。
B_RS = 0.99
B_EWR = 0.99
B_CT = 0.990
B_MIG = 0.99


async def _assert_verify_db(db):
    env = (os.environ.get("APP_ENV") or "").lower()
    assert env == "verification", f"APP_ENV 必须 verification, got {env!r}"
    name = (await db.execute(text("select current_database()"))).scalar_one()
    assert _VERIFY_DB_RE.match(name), f"非法验证数据库: {name!r}"
    assert name != "bz_stock"
    return name


def _make_core_run(td: date) -> StockFeatureSnapshotRun:
    return StockFeatureSnapshotRun(
        trade_date=td,
        run_type="after_close",
        status="succeeded",
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
    )


def _make_review_run(core_id: uuid.UUID, td: date) -> MarketReviewRun:
    # 发布门禁只消费 canonical composition readiness（空 dict = 空壳，禁止发布）。
    readiness = dict.fromkeys(
        (k for _, k in ALL_IDS), "ready"
    )
    return MarketReviewRun(
        trade_date=td,
        source_core_run_id=core_id,
        source_board_run_id=None,
        source_chip_run_id=None,
        degraded_reasons=[],
        algorithm_version=REVIEW_ALGORITHM_VERSION,
        filter_version="filters-1.0.0",
        expected_scope_count=14,
        succeeded_scope_count=14,
        failed_scope_count=0,
        signal_count=0,
        coverage_ratio=__import__("decimal").Decimal("1.0"),
        status="signals_ready",
        metadata_json={"canonical_composition_readiness": readiness},
    )


def _make_fact(
    run_id: uuid.UUID,
    td: date,
    scope_type: str,
    scope_key: str,
    *,
    regime_strength: float,
    equal_weight_return: float,
) -> ReviewScopeObservationFact:
    return ReviewScopeObservationFact(
        review_run_id=run_id,
        trade_date=td,
        scope_type=scope_type,
        scope_key=scope_key,
        pit_member_count=100,
        pit_member_count_t1=100,
        provided_member_count=100,
        t1_membership_available=True,
        pit_status_t="ready",
        pit_status_t1="ready",
        readiness="ready",
        observation_payload={
            "trend": {
                "continuous": {
                    "regime_strength": regime_strength,
                    "dsa_dir_bars": 5,
                    "dsa_vwap_dev_pct": 0.01,
                }
            },
            "structure": {"events": {"status": "unavailable", "reason": "EVENTS_UNAVAILABLE"}},
            "momentum": {"change": {"enhancing_ratio": 0.5, "weakening_ratio": 0.5, "denominator": 10}},
            "participation": {"volume": {"ratio20": {"p50": 1.0}}},
            "price": {
                "equal_weight_return": equal_weight_return,
                "amount_weight_return": equal_weight_return,
                "breadth": {"advance_ratio": 0.5, "decline_ratio": 0.3, "unchanged_ratio": 0.2},
            },
        },
        diagnostics=[],
        algorithm_version=REVIEW_ALGORITHM_VERSION,
    )


def _make_composition(
    run_id: uuid.UUID,
    td: date,
    scope_type: str,
    scope_key: str,
    *,
    capital_tilt: float,
    migration: float,
) -> ReviewScopeCompositionSnapshot:
    return ReviewScopeCompositionSnapshot(
        review_run_id=run_id,
        scope_type=scope_type,
        scope_key=scope_key,
        trade_date=td,
        algorithm_version=REVIEW_ALGORITHM_VERSION,
        composition_payload={
            "internal_structure_facts": {
                "capital_tilt": {
                    "equal_weight_return": 0.01,
                    "amount_weight_return": 0.01 + capital_tilt,
                    "capital_tilt": capital_tilt,
                }
            },
            "leadership": {
                "status": "ready",
                "reason": None,
                "jaccard_stability": 1.0 - migration,
                "migration": migration,
                "current_leader_count": 1,
                "current_leader_ids": ["aaaaaaaa-0000-4000-8000-00000000000a"],
            },
        },
    )


async def _clean(db, td: date) -> None:
    await db.execute(
        text(
            "DELETE FROM factor_publications "
            "WHERE publication_kind=:pk AND trade_date=:d "
            "AND scope_type=:st AND scope_key=:sk"
        ),
        {
            "pk": PUBLICATION_KIND_MARKET_REVIEW,
            "d": td,
            "st": SCOPE_TYPE_REVIEW,
            "sk": SCOPE_KEY_REVIEW,
        },
    )
    await db.execute(
        text(
            "DELETE FROM review_scope_composition_snapshots "
            "WHERE trade_date=:d AND scope_type IN (:ind, :con)"
        ),
        {"d": td, "ind": IND, "con": CON},
    )
    await db.execute(
        text(
            "DELETE FROM review_scope_observation_facts "
            "WHERE trade_date=:d AND scope_type IN (:ind, :con)"
        ),
        {"d": td, "ind": IND, "con": CON},
    )


def _stub(regime_strength: float, equal_weight_return: float) -> dict:
    return {
        "trend": {"continuous": {"regime_strength": regime_strength}},
        "price": {"equal_weight_return": equal_weight_return},
    }


def _regime_pct(payloads: dict, key: str) -> float | None:
    """直接调 canonical math owner，取某 scope 的 regime_strength peer percentile。"""
    res = compute_cross_sectional(
        current_payload=payloads[key],
        peer_payloads=payloads,
        current_scope_key=key,
    )
    field = next(
        f for f in res["fields"] if f["field"] == "trend.continuous.regime_strength"
    )
    return field["percentile"]


@pytest.mark.asyncio
async def test_explorer_compare_uses_published_run_only():
    """同日 A(published) / B(未发布) 写出不同值：

    - FORMAL REVIEW READ OWNER 只解析到 A（B 未发布）；
    - compareFacts 全部来自 A（regime_strength / equal_weight_return / capital_tilt
      / migration 都不是 B 的极端值）；
    - Composition LEFT JOIN 只匹配 A 的 review_run_id（B 的 composition 不得污染）；
    - 复合身份：industry_l1/shared_scope 与 concept/shared_scope 完全独立。
    """
    async with AsyncSessionLocal() as s:
        await _assert_verify_db(s)
        await _clean(s, T)
        await s.commit()

        # --- A: 正式发布（含 shared_scope 两 family，各写不同 A 值）---
        core_a = _make_core_run(T)
        s.add(core_a)
        await s.flush()
        run_a = _make_review_run(core_a.id, T)
        s.add(run_a)
        await s.flush()
        for i, k in enumerate(IND_KEYS):
            s.add(_make_fact(run_a.id, T, IND, k, regime_strength=IND_RS[i], equal_weight_return=IND_EWR[i]))
            s.add(_make_composition(run_a.id, T, IND, k, capital_tilt=IND_CT[i], migration=IND_MIG[i]))
        for i, k in enumerate(CON_KEYS):
            s.add(_make_fact(run_a.id, T, CON, k, regime_strength=CON_RS[i], equal_weight_return=CON_EWR[i]))
            s.add(_make_composition(run_a.id, T, CON, k, capital_tilt=CON_CT[i], migration=CON_MIG[i]))
        s.add(_make_fact(run_a.id, T, IND, SHARED, regime_strength=SHARED_RS_IND, equal_weight_return=SHARED_EWR_IND))
        s.add(_make_composition(run_a.id, T, IND, SHARED, capital_tilt=SHARED_CT_IND, migration=SHARED_MIG_IND))
        s.add(_make_fact(run_a.id, T, CON, SHARED, regime_strength=SHARED_RS_CON, equal_weight_return=SHARED_EWR_CON))
        s.add(_make_composition(run_a.id, T, CON, SHARED, capital_tilt=SHARED_CT_CON, migration=SHARED_MIG_CON))
        await s.flush()
        await publish_review(s, run_a)
        await s.commit()
        run_a_id = run_a.id

        # --- B: 后跑但未发布（值全部极端化）---
        core_b = _make_core_run(T)
        s.add(core_b)
        await s.flush()
        run_b = _make_review_run(core_b.id, T)
        run_b.status = "signals_ready"
        s.add(run_b)
        await s.flush()
        for k in IND_KEYS + CON_KEYS:
            fam = IND if k.startswith("exp_cmp_ind") else CON
            s.add(_make_fact(run_b.id, T, fam, k, regime_strength=B_RS, equal_weight_return=B_EWR))
            s.add(_make_composition(run_b.id, T, fam, k, capital_tilt=B_CT, migration=B_MIG))
        s.add(_make_fact(run_b.id, T, IND, SHARED, regime_strength=B_RS, equal_weight_return=B_EWR))
        s.add(_make_composition(run_b.id, T, IND, SHARED, capital_tilt=B_CT, migration=B_MIG))
        s.add(_make_fact(run_b.id, T, CON, SHARED, regime_strength=B_RS, equal_weight_return=B_EWR))
        s.add(_make_composition(run_b.id, T, CON, SHARED, capital_tilt=B_CT, migration=B_MIG))
        await s.commit()
        run_b_id = run_b.id

        # 防御性确认：B 的两套 fact/composition 确实存在于 DB（否则 lineage 测试无意义）
        b_facts = (
            await s.execute(
                select(ReviewScopeObservationFact).where(
                    ReviewScopeObservationFact.review_run_id == run_b_id,
                    ReviewScopeObservationFact.trade_date == T,
                )
            )
        ).scalars().all()
        assert len(b_facts) == 14, "B 必须写出 14 条 fact"
        b_comps = (
            await s.execute(
                select(ReviewScopeCompositionSnapshot).where(
                    ReviewScopeCompositionSnapshot.review_run_id == run_b_id,
                    ReviewScopeCompositionSnapshot.trade_date == T,
                )
            )
        ).scalars().all()
        assert len(b_comps) == 14, "B 必须写出 14 条 composition"

        # === FORMAL REVIEW READ OWNER 只解析到 A ===
        resolved_id = await get_published_review_run_id(s, T)
        assert resolved_id == run_a_id, "live pointer 必须指向已发布的 A"
        run = await s.get(MarketReviewRun, resolved_id)
        assert run is not None
        assert is_formally_published_review_run(run, resolved_id, expected_trade_date=T)

        # === Explorer read（真实 LEFT JOIN SQL，parameterized by A 的 run_id）===
        compare = await list_review_scope_compare(
            s, review_run_id=run_a_id, trade_date=T, scope_type=None, scope_keys=ALL_IDS,
        )
        assert set(compare.keys()) == ALL_IDS, "compareFacts 必须覆盖全部 14 个复合身份"

        # 每个 scope：只来自 A，B 不得污染
        plans = [
            (IND, IND_KEYS, IND_RS, IND_EWR, IND_CT, IND_MIG),
            (CON, CON_KEYS, CON_RS, CON_EWR, CON_CT, CON_MIG),
        ]
        for fam, keys, rs, ewr, ct, mig in plans:
            for i, k in enumerate(keys):
                f = compare[(fam, k)]
                assert f["dsa"]["regimeStrength"] == rs[i], (
                    f"{fam}/{k}.regimeStrength 必须取 A={rs[i]}，实际={f['dsa']['regimeStrength']}"
                    f"（lineage 失效，误取未发布的 B）"
                )
                assert f["dsa"]["regimeStrength"] != B_RS, f"{fam}/{k} 不得取未发布的 B"
                assert f["price"]["equalWeightReturn"] == ewr[i], f"{fam}/{k}.equalWeightReturn 必须取 A"
                assert f["price"]["equalWeightReturn"] != B_EWR, f"{fam}/{k} 不得取未发布的 B"
                # Composition LEFT JOIN 只匹配 A 的 review_run_id
                assert f["composition"]["capitalTilt"] == ct[i], (
                    f"{fam}/{k}.capitalTilt 必须取 A 的 composition={ct[i]}，"
                    f"实际={f['composition']['capitalTilt']}（JOIN 误匹配 B）"
                )
                assert f["composition"]["capitalTilt"] != B_CT, f"{fam}/{k} 不得 JOIN 到未发布的 B"
                assert f["composition"]["migration"] == mig[i], f"{fam}/{k}.migration 必须取 A"
                assert f["composition"]["migration"] != B_MIG, f"{fam}/{k} 不得取未发布的 B"

        # === 复合身份碰撞：industry_l1/shared_scope 与 concept/shared_scope 完全独立 ===
        ind_shared = compare[(IND, SHARED)]
        con_shared = compare[(CON, SHARED)]
        assert ind_shared["dsa"]["regimeStrength"] == SHARED_RS_IND
        assert con_shared["dsa"]["regimeStrength"] == SHARED_RS_CON
        assert ind_shared["dsa"]["regimeStrength"] != con_shared["dsa"]["regimeStrength"], (
            "两 family 的 shared_scope 必须互不串"
        )
        assert ind_shared["dsa"]["regimeStrength"] != B_RS
        assert con_shared["dsa"]["regimeStrength"] != B_RS
        assert ind_shared["price"]["equalWeightReturn"] == SHARED_EWR_IND
        assert con_shared["price"]["equalWeightReturn"] == SHARED_EWR_CON
        assert ind_shared["composition"]["capitalTilt"] == SHARED_CT_IND
        assert con_shared["composition"]["capitalTilt"] == SHARED_CT_CON
        assert ind_shared["composition"]["capitalTilt"] != con_shared["composition"]["capitalTilt"], (
            "两 family 的 shared_scope composition 必须互不串"
        )
        assert ind_shared["composition"]["capitalTilt"] != B_CT
        assert con_shared["composition"]["capitalTilt"] != B_CT
        assert ind_shared["composition"]["migration"] == SHARED_MIG_IND
        assert con_shared["composition"]["migration"] == SHARED_MIG_CON

        await _clean(s, T)
        await s.commit()


@pytest.mark.asyncio
async def test_explorer_compare_peer_percentile_per_family():
    """industry_l1 与 concept 同时存在：peer percentile 分 family 计算，不得跨 family。

    每 family 6 个 scope（valid_peer_count>=5 → ready）。用 canonical math owner 直接
    算"分 family"与"跨 family"两套 percentile，证明 Explorer 返回的是分 family 结果；
    对 shared_scope 同样要求分 family（不跨 family）。
    """
    async with AsyncSessionLocal() as s:
        await _assert_verify_db(s)
        await _clean(s, T)
        await s.commit()

        core_a = _make_core_run(T)
        s.add(core_a)
        await s.flush()
        run_a = _make_review_run(core_a.id, T)
        s.add(run_a)
        await s.flush()
        for i, k in enumerate(IND_KEYS):
            s.add(_make_fact(run_a.id, T, IND, k, regime_strength=IND_RS[i], equal_weight_return=IND_EWR[i]))
        for i, k in enumerate(CON_KEYS):
            s.add(_make_fact(run_a.id, T, CON, k, regime_strength=CON_RS[i], equal_weight_return=CON_EWR[i]))
        s.add(_make_fact(run_a.id, T, IND, SHARED, regime_strength=SHARED_RS_IND, equal_weight_return=SHARED_EWR_IND))
        s.add(_make_fact(run_a.id, T, CON, SHARED, regime_strength=SHARED_RS_CON, equal_weight_return=SHARED_EWR_CON))
        await s.flush()
        await publish_review(s, run_a)
        await s.commit()

        ind_stubs = {
            **{k: _stub(IND_RS[i], IND_EWR[i]) for i, k in enumerate(IND_KEYS)},
            SHARED: _stub(SHARED_RS_IND, SHARED_EWR_IND),
        }
        con_stubs = {
            **{k: _stub(CON_RS[i], CON_EWR[i]) for i, k in enumerate(CON_KEYS)},
            SHARED: _stub(SHARED_RS_CON, SHARED_EWR_CON),
        }
        # 跨 family：用前缀 key 避免 scope_key 碰撞，才能正确表达"混算"
        cross_stubs = {
            **{f"ind|{k}": v for k, v in ind_stubs.items()},
            **{f"con|{k}": v for k, v in con_stubs.items()},
        }
        all_stubs = {**ind_stubs, **con_stubs}  # 无碰撞 key（除 SHARED 被 con 覆盖，仅供IND非碰撞key用）

        # canonical owner 直接算：分 family vs 跨 family
        ind_a_per_family = _regime_pct(ind_stubs, IND_KEYS[0])
        ind_a_cross_family = _regime_pct(all_stubs, IND_KEYS[0])
        con_a_per_family = _regime_pct(con_stubs, CON_KEYS[0])
        con_a_cross_family = _regime_pct(all_stubs, CON_KEYS[0])
        shared_ind_per_family = _regime_pct(ind_stubs, SHARED)
        shared_ind_cross_family = _regime_pct(cross_stubs, f"ind|{SHARED}")
        assert ind_a_per_family is not None and ind_a_cross_family is not None
        assert con_a_per_family is not None and con_a_cross_family is not None
        # 两 family 分布明显分离 → 分 family 与跨 family percentile 必不同
        assert ind_a_per_family != ind_a_cross_family, "industry_l1 的 percentile 不应跨 family"
        assert con_a_per_family != con_a_cross_family, "concept 的 percentile 不应跨 family"

        # Explorer 返回的是分 family 结果
        compare = await list_review_scope_compare(
            s, review_run_id=run_a.id, trade_date=T, scope_type=None, scope_keys=ALL_IDS,
        )
        assert compare[(IND, IND_KEYS[0])]["dsa"]["regimeStrengthPeerPercentile"] == ind_a_per_family, (
            "Explorer 必须返回 industry_l1 分 family 的 peer percentile"
        )
        assert compare[(IND, IND_KEYS[0])]["dsa"]["regimeStrengthPeerPercentile"] != ind_a_cross_family, (
            "Explorer 不得把 concept 当 industry_l1 的 peer（跨 family 污染）"
        )
        assert compare[(CON, CON_KEYS[0])]["dsa"]["regimeStrengthPeerPercentile"] == con_a_per_family, (
            "Explorer 必须返回 concept 分 family 的 peer percentile"
        )
        assert compare[(CON, CON_KEYS[0])]["dsa"]["regimeStrengthPeerPercentile"] != con_a_cross_family

        # shared_scope 同样分 family：industry_l1/shared_scope 的 percentile 等于
        # 仅 industry_l1 cohort 的结果，且不等于跨族混算（证明 identity 隔离 + 不跨 family）
        assert compare[(IND, SHARED)]["dsa"]["regimeStrengthPeerPercentile"] == shared_ind_per_family, (
            "shared_scope 必须返回 industry_l1 分 family 的 peer percentile"
        )
        assert compare[(IND, SHARED)]["dsa"]["regimeStrengthPeerPercentile"] != shared_ind_cross_family, (
            "shared_scope 不得跨 family 与 concept 混算"
        )
        assert compare[(IND, SHARED)]["dsa"]["regimeStrengthPeerPercentile"] != (
            compare[(CON, SHARED)]["dsa"]["regimeStrengthPeerPercentile"]
        ), "两 family 的 shared_scope percentile 必须各自独立"

        await _clean(s, T)
        await s.commit()
