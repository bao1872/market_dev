"""[SLICE 4 / Price] Composition 历史（capital_tilt + leadership）published-run lineage PG closure.

真实 PostgreSQL 验证库（bz_stock_verify_<SHA>），需 PANJI_REMOTE_VERIFY_DB_TEST=1。
PURE_UNIT_TEST=1 下由 conftest 自动 skip。

本文件只为 SLICE 4 新增的 **DB Composition history read path** 钉死最高风险不变量
（spec §十四）：

1. 同日 A=formally published / B=后跑未发布，两者为同一 scope 写出截然不同的
   Composition → history.price 只能读到 A 的 capital_tilt / leadership，B 不得污染；
2. published 日期存在但该 scope 的 Composition 缺失 → date slot 保留为 null
   （不 forward-fill、不从当前 Composition 回推）。

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
    publish_review,
)
from app.services.review_scope_diagnostics_service import get_scope_diagnostics

pytestmark = pytest.mark.postgres

_VERIFY_DB_RE = re.compile(r"^bz_stock_verify_[0-9a-f]{40}$")

# 与其它 PG 用例隔离（不同于 diagnostics_lineage 的 2099-09-03），保证可重入。
T = date(2099, 9, 4)
SCOPE_TYPE = "industry_l1"
SCOPE_X = "pg_price_lineage_x"  # A / B 都写 Composition，值截然不同
SCOPE_Z = "pg_price_lineage_z"  # A 有 fact 但无 Composition -> 应为 null slot

# A / B 写出截然不同的 capital_tilt / leadership，便于机器判定到底取了谁。
A_TILT_X = 0.0040
A_JACCARD_X = 0.11
A_LEADER_IDS_X = ["aaaaaaaa-0000-4000-8000-000000000001"]
B_TILT_X = 0.9990  # 若 lineage 失效，history 会错误地取 0.9990
B_JACCARD_X = 0.99
B_LEADER_IDS_X = ["bbbbbbbb-0000-4000-8000-000000000002"]


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
    return MarketReviewRun(
        trade_date=td,
        source_core_run_id=core_id,
        source_board_run_id=None,
        source_chip_run_id=None,
        degraded_reasons=[],
        algorithm_version=REVIEW_ALGORITHM_VERSION,
        filter_version="filters-1.0.0",
        expected_scope_count=2,
        succeeded_scope_count=2,
        failed_scope_count=0,
        signal_count=0,
        coverage_ratio=__import__("decimal").Decimal("1.0"),
        status="signals_ready",
        metadata_json={
            "canonical_composition_readiness": {SCOPE_X: "ready", SCOPE_Z: "ready"}
        },
    )


def _make_fact(run_id: uuid.UUID, td: date, scope_key: str) -> ReviewScopeObservationFact:
    return ReviewScopeObservationFact(
        review_run_id=run_id,
        trade_date=td,
        scope_type=SCOPE_TYPE,
        scope_key=scope_key,
        pit_member_count=100,
        pit_member_count_t1=100,
        provided_member_count=100,
        t1_membership_available=True,
        pit_status_t="ready",
        pit_status_t1="ready",
        readiness="ready",
        observation_payload={
            "price": {
                "equal_weight_return": 0.01,
                "amount_weighted_return": 0.014,
                "breadth": {"advance_ratio": 0.5, "decline_ratio": 0.3, "unchanged_ratio": 0.2},
                "return_dispersion": 0.03,
            }
        },
        diagnostics=[],
        algorithm_version=REVIEW_ALGORITHM_VERSION,
    )


def _make_composition(
    run_id: uuid.UUID,
    td: date,
    scope_key: str,
    tilt: float,
    jaccard: float,
    leader_ids: list[str],
) -> ReviewScopeCompositionSnapshot:
    return ReviewScopeCompositionSnapshot(
        review_run_id=run_id,
        scope_type=SCOPE_TYPE,
        scope_key=scope_key,
        trade_date=td,
        algorithm_version=REVIEW_ALGORITHM_VERSION,
        composition_payload={
            "internal_structure_facts": {
                "capital_tilt": {
                    "equal_weight_return": 0.01,
                    "amount_weighted_return": 0.01 + tilt,
                    "capital_tilt": tilt,
                }
            },
            "leadership": {
                "status": "ready",
                "reason": None,
                "jaccard_stability": jaccard,
                "migration": 1.0 - jaccard,
                "current_leader_count": len(leader_ids),
                "current_leader_ids": leader_ids,
            },
        },
    )


async def _clean(db, td: date):
    await db.execute(
        text(
            "DELETE FROM factor_publications "
            "WHERE scope_type=:st AND scope_key=:sk AND trade_date=:d AND publication_kind=:pk"
        ),
        {
            "st": SCOPE_TYPE_REVIEW,
            "sk": SCOPE_KEY_REVIEW,
            "d": td,
            "pk": PUBLICATION_KIND_MARKET_REVIEW,
        },
    )
    await db.execute(
        text(
            "DELETE FROM review_scope_composition_snapshots "
            "WHERE trade_date=:d AND scope_type=:st AND scope_key IN (:x, :z)"
        ),
        {"d": td, "st": SCOPE_TYPE, "x": SCOPE_X, "z": SCOPE_Z},
    )
    await db.execute(
        text(
            "DELETE FROM review_scope_observation_facts "
            "WHERE trade_date=:d AND scope_type=:st AND scope_key IN (:x, :z)"
        ),
        {"d": td, "st": SCOPE_TYPE, "x": SCOPE_X, "z": SCOPE_Z},
    )


@pytest.mark.asyncio
async def test_price_composition_history_uses_published_run_only():
    """同日 A(published) / B(未发布) 写出不同 Composition：

    - history.price.capital_tilt[T] 取 A（不是 B）；
    - history.price.leadership[T] 取 A（jaccard / leader ids 都是 A 的）；
    - A 无 Composition 的 scope_z：date slot 保留 null（不是 0、不是回推）。
    """
    async with AsyncSessionLocal() as s:
        await _assert_verify_db(s)
        await _clean(s, T)
        await s.commit()

        # --- A: 正式发布（X 有 Composition；Z 故意不写 Composition）---
        core_a = _make_core_run(T)
        s.add(core_a)
        await s.flush()
        run_a = _make_review_run(core_a.id, T)
        s.add(run_a)
        await s.flush()
        s.add(_make_fact(run_a.id, T, SCOPE_X))
        s.add(_make_fact(run_a.id, T, SCOPE_Z))
        s.add(
            _make_composition(run_a.id, T, SCOPE_X, A_TILT_X, A_JACCARD_X, A_LEADER_IDS_X)
        )
        await s.flush()
        await publish_review(s, run_a)
        await s.commit()
        run_a_id = run_a.id

        # --- B: 后跑但未发布（无任何 publication pointer）---
        core_b = _make_core_run(T)
        s.add(core_b)
        await s.flush()
        run_b = _make_review_run(core_b.id, T)
        run_b.status = "signals_ready"  # 保持未发布
        s.add(run_b)
        await s.flush()
        s.add(_make_fact(run_b.id, T, SCOPE_X))
        s.add(
            _make_composition(run_b.id, T, SCOPE_X, B_TILT_X, B_JACCARD_X, B_LEADER_IDS_X)
        )
        await s.commit()
        run_b_id = run_b.id

        # 防御性确认：DB 里确实存在 A、B 两套 Composition（lineage 选错会读到 B）
        comps = (
            await s.execute(
                select(ReviewScopeCompositionSnapshot).where(
                    ReviewScopeCompositionSnapshot.trade_date == T,
                    ReviewScopeCompositionSnapshot.scope_type == SCOPE_TYPE,
                )
            )
        ).scalars().all()
        by_run: dict[uuid.UUID, list[ReviewScopeCompositionSnapshot]] = {}
        for c in comps:
            by_run.setdefault(c.review_run_id, []).append(c)
        assert run_a_id in by_run and run_b_id in by_run, "A/B 两套 Composition 必须都存在"
        b_x = next(
            (c for c in by_run[run_b_id] if c.scope_key == SCOPE_X),
            None,
        )
        assert b_x is not None, "B 的 Composition 必须存在（否则 lineage 测试无意义）"
        assert (
            b_x.composition_payload["internal_structure_facts"]["capital_tilt"]["capital_tilt"]
            == B_TILT_X
        ), "B 的 capital_tilt 必须存在且为 B 值"

        # === history.price 必须取 published run A 的值 ===
        history = await get_scope_diagnostics(
            s, trade_date=T, scope_type=SCOPE_TYPE, scope_key=SCOPE_X,
        )
        assert history["availability"]["status"] == "ready"
        assert history["dates"][-1] == T.isoformat()

        price = history["price"]
        assert price is not None, "activated scope_type 必须有 price 投影"
        assert price["capital_tilt"][-1] == A_TILT_X, (
            f"price.capital_tilt 必须取 published run A 的 {A_TILT_X}，"
            f"实际={price['capital_tilt'][-1]}（lineage 失效，误取未发布的 B）"
        )
        assert price["capital_tilt"][-1] != B_TILT_X, "price history 不得取未发布的 B"

        lead = price["leadership"][-1]
        assert lead is not None, "A 的 leadership 必须被投影出来"
        assert lead["jaccard_stability"] == A_JACCARD_X, (
            f"leadership 必须取 A 的 jaccard={A_JACCARD_X}，实际={lead['jaccard_stability']}"
        )
        assert lead["jaccard_stability"] != B_JACCARD_X, "leadership 不得取未发布的 B"
        assert lead["current_leader_ids"] == A_LEADER_IDS_X, "leader ids 必须取 A 的"
        assert lead["status"] == "ready"

        # === published 日期存在但该 scope Composition 缺失 -> date slot 为 null ===
        history_z = await get_scope_diagnostics(
            s, trade_date=T, scope_type=SCOPE_TYPE, scope_key=SCOPE_Z,
        )
        assert history_z["dates"][-1] == T.isoformat()
        price_z = history_z["price"]
        assert price_z is not None
        assert price_z["capital_tilt"][-1] is None, (
            "published run 无该 scope Composition 时必须为 null（不得 forward-fill / 回推）"
        )
        assert price_z["leadership"][-1] is None, "缺失 Composition 的 leadership 必须为 null"

        await _clean(s, T)
        await s.commit()
