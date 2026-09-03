"""[CHANGE-20260903] R3 History / Cross-sectional published-run lineage PG closure.

真实 PostgreSQL 验证库（bz_stock_verify_<SHA>），需 PANJI_REMOTE_VERIFY_DB_TEST=1。
PURE_UNIT_TEST=1 下由 conftest 自动 skip。

目的（用户指定的最高风险不变量钉死）：
- history 是否真的按 published review_run_id 跨日选 canonical fact；
- crossSection 是否真的不会被同日后跑的非 published run 污染。

场景（同一交易日 T，两个 ReviewRun）：
- A = published（正式发布，FactorPublication 指向 A）；
- B = 后跑但未发布（status=signals_ready，无任何 publication pointer）；
- A 与 B 为同一 scope_type 的相同 scope 写出**截然不同**的 observation 值。

断言：
- history[T] 取 A 的值（而非 B）；
- crossSection 的 peer_count == 2（仅 A 的 X/Y 两个 scope），证明 B 的 fact
  被 review_run_id 门控排除，未污染 cohort。若 lineage 失效，B 混入会让
  peer_count 变 4。

DB identity（fail-closed）：APP_ENV==verification 且 current_database() 匹配
^bz_stock_verify_[0-9a-f]{40}$ 且 != bz_stock。

运行（远程验证运行时，非本地/CI）：
    panji-verify run --sha <FULL_SHA> --plan targeted-pg
"""
# ruff: noqa: UP017  (验证容器 Python<3.11，datetime.UTC 不可用，使用 timezone.utc)
from __future__ import annotations

import os
import re
import uuid
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import text

from app.db import AsyncSessionLocal
from app.domain.review.versions import REVIEW_ALGORITHM_VERSION
from app.models.market_review import MarketReviewRun, ReviewScopeObservationFact
from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun
from app.services.review_cross_sectional_service import get_cross_sectional
from app.services.review_observation_persistence_service import (
    ACTIVATED_OBSERVATION_PERSISTENCE_SCOPE_TYPES,
)
from app.services.review_publication_service import (
    PUBLICATION_KIND_MARKET_REVIEW,
    SCOPE_KEY_REVIEW,
    SCOPE_TYPE_REVIEW,
    publish_review,
)
from app.services.review_scope_diagnostics_service import get_scope_diagnostics

pytestmark = pytest.mark.postgres

_VERIFY_DB_RE = re.compile(r"^bz_stock_verify_[0-9a-f]{40}$")

# 隔离日期：远大于其它 PG 用例，保证 /latest 等不受影响、可重入。
T = date(2099, 9, 3)
SCOPE_TYPE = "industry_l1"
SCOPE_X = "pg_diag_lineage_x"
SCOPE_Y = "pg_diag_lineage_y"

# A / B 写出截然不同的值，便于机器判定 history/crossSection 到底取了谁。
A_REGIME_X = 0.90
A_REGIME_Y = 0.50
B_REGIME_X = 0.10  # 若 lineage 失效，history[T] 会错误地取 0.10
B_REGIME_Y = 0.30


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
        metadata_json={"canonical_composition_readiness": {SCOPE_X: "ready", SCOPE_Y: "ready"}},
    )


def _make_fact(run_id: uuid.UUID, td: date, scope_key: str, regime: float) -> ReviewScopeObservationFact:
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
        observation_payload={"trend": {"continuous": {"regime_strength": regime}}},
        diagnostics=[],
        algorithm_version=REVIEW_ALGORITHM_VERSION,
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
            "DELETE FROM review_scope_observation_facts "
            "WHERE trade_date=:d AND scope_type=:st AND scope_key IN (:x, :y)"
        ),
        {"d": td, "st": SCOPE_TYPE, "x": SCOPE_X, "y": SCOPE_Y},
    )


@pytest.mark.asyncio
async def test_history_and_cross_section_use_published_run_only():
    """同日 A(published) / B(未发布) 写出不同值：
    - history[T] 取 A；
    - crossSection peer_count == 2（B 被 review_run_id 门控排除）。
    """
    assert SCOPE_TYPE in ACTIVATED_OBSERVATION_PERSISTENCE_SCOPE_TYPES

    async with AsyncSessionLocal() as s:
        await _assert_verify_db(s)
        await _clean(s, T)
        await s.commit()

        # --- A: 正式发布 ---
        core_a = _make_core_run(T)
        s.add(core_a)
        await s.flush()
        run_a = _make_review_run(core_a.id, T)
        s.add(run_a)
        await s.flush()
        s.add(_make_fact(run_a.id, T, SCOPE_X, A_REGIME_X))
        s.add(_make_fact(run_a.id, T, SCOPE_Y, A_REGIME_Y))
        await s.flush()
        await publish_review(s, run_a)  # 生产发布路径：写入 FactorPublication(kind=market_review)
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
        s.add(_make_fact(run_b.id, T, SCOPE_X, B_REGIME_X))
        s.add(_make_fact(run_b.id, T, SCOPE_Y, B_REGIME_Y))
        await s.commit()
        run_b_id = run_b.id

        # 防御性确认：DB 里确实存在 A、B 两套 fact（lineage 选错会读到 B）
        from sqlalchemy import select

        all_facts = (
            await s.execute(
                select(ReviewScopeObservationFact).where(
                    ReviewScopeObservationFact.trade_date == T,
                    ReviewScopeObservationFact.scope_type == SCOPE_TYPE,
                )
            )
        ).scalars().all()
        by_run = {}
        for f in all_facts:
            by_run.setdefault(f.review_run_id, []).append(f)
        assert run_a_id in by_run and run_b_id in by_run, "A/B 两套 fact 必须都存在"
        assert any(f.scope_key == SCOPE_X and f.observation_payload["trend"]["continuous"]["regime_strength"] == B_REGIME_X for f in by_run[run_b_id]), "B 的 X fact 必须存在（否则 lineage 测试无意义）"

        # === history: 必须取 published run A 的值 ===
        history = await get_scope_diagnostics(
            s, trade_date=T, scope_type=SCOPE_TYPE, scope_key=SCOPE_X,
        )
        assert history["availability"]["status"] == "ready"
        assert history["dates"] == [T.isoformat()]
        regime_series = history["fields"]["regime_strength"]["series"]
        assert regime_series[-1] == A_REGIME_X, (
            f"history 必须取 published run A 的 regime_strength={A_REGIME_X}，"
            f"实际={regime_series[-1]}（lineage 失效，误取 B）"
        )
        assert regime_series[-1] != B_REGIME_X, "history 不得取未发布的 B"

        # === crossSection: cohort 必须只含 A（peer_count == 2，排除 B）===
        cs = await get_cross_sectional(s, T, SCOPE_TYPE, SCOPE_X)
        assert cs is not None, "published run 存在时 crossSection 不得为 None"
        regime_field = next(
            (f for f in cs["fields"] if f["field_key"] == "trend.continuous.regime_strength"),
            None,
        )
        assert regime_field is not None
        assert regime_field["peer_count"] == 2, (
            f"crossSection cohort 必须只含 published run A 的 2 个 scope，"
            f"实际 peer_count={regime_field['peer_count']}（B 被 review_run_id 门控排除）"
        )
        assert regime_field["peer_count"] != 4, "B 的 fact 不得污染 cohort"
        # current 必须是 A 的 X
        assert regime_field["value"] == A_REGIME_X, (
            f"current 必须取 A 的 X={A_REGIME_X}，实际={regime_field['value']}"
        )

        # 清理
        await _clean(s, T)
        await s.commit()
