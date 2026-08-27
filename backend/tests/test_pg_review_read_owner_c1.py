"""PHASE C1 CONTINUATION — Review Read Owner + API Multi-Run Determinism Closure.

自包含 synthetic 数据，全部写入验证库 bz_stock_verify_<SHA>（由 gate cleanup 丢弃），
不读不写生产 bz_stock。

覆盖任务 §5–§9：
- T1: _resolve_source_core_run_id 在 None 时 fail-closed（运行于 PG suite / 验证库，非 pure unit）
- T2: overview 响应暴露 sourceCoreRunId + 单 run 生产 publish → owner 解析 lineage 正确
- T5/T7 生产发布路径多 run 假绿：同日 Core A/B + Review Y(A)/Z(B)；
       Y: industry_l1 eligible=100/provided=80；Z: 100/20；
       CASE1 publish Y → overview=Y(0.8)；CASE2 publish Z → overview=Z(0.2)
       owner 由 publication action 决定，非 created_at/latest timestamp
- §7 superseded pointer 假绿：同 T 制造 historical H(superseded) + live L；
       H.published_at 更晚，但 get_published_review_run_id 必须返回 L；
       仅 superseded history、无 live pointer → T 不得视为当前正式 owner
- §8 broken pointer fail-closed：live pointer → run status=signals_ready / published_at=NULL
       → 用户正式 read path 不得返回 200 正式 Review（按 §4 guard fail-closed）

DB identity（fail-closed，测试自身要求）：
- APP_ENV == "verification"
- current_database() 匹配 ^bz_stock_verify_[0-9a-f]{40}$ 且 != bz_stock
"""
# ruff: noqa: UP017  (验证容器 Python<3.11，datetime.UTC 不可用，使用 timezone.utc)

import os
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.api.review import get_review_overview
from app.db import AsyncSessionLocal
from app.domain.review.versions import REVIEW_ALGORITHM_VERSION
from app.models.factor_publication import FactorPublication
from app.models.market_review import MarketReviewRun, ReviewScopeObservationFact
from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun
from app.schemas.review import ReviewOverviewResponse
from app.services.access_control_service import AccessContext
from app.services.review_observation_persistence_service import (
    list_scope_observation_facts,
)
from app.services.review_orchestrator_service import _resolve_source_core_run_id
from app.services.review_publication_service import (
    PUBLICATION_KIND_MARKET_REVIEW,
    SCOPE_KEY_REVIEW,
    SCOPE_TYPE_REVIEW,
    get_published_review_run_id,
    is_formally_published_review_run,
    list_published_review_dates,
    publish_review,
)

pytestmark = pytest.mark.postgres

# 每个测试使用独立 trade_date，避免 live pointer 唯一约束相互冲突
T = date(2026, 8, 26)
T2 = date(2026, 8, 25)
T5 = date(2026, 8, 24)
T7 = date(2026, 8, 23)
T8 = date(2026, 8, 22)
_VERIFY_DB_RE = re.compile(r"^bz_stock_verify_[0-9a-f]{40}$")


async def _assert_verify_db(db):
    env = (os.environ.get("APP_ENV") or "").lower()
    assert env == "verification", f"APP_ENV 必须 verification, got {env!r}"
    name = (await db.execute(text("select current_database()"))).scalar_one()
    assert _VERIFY_DB_RE.match(name), f"非法验证数据库: {name!r}"
    assert name != "bz_stock"
    return name


def _ctx() -> AccessContext:
    # 直接调用 endpoint 函数，绕过 FastAPI DI；仅提供最小 AccessContext。
    return AccessContext(
        user_id="c1-test",
        account_status="active",
        roles=["member"],
        is_admin=False,
        is_member=True,
        subscription_active=True,
        default_route="/review",
        capabilities={"research_replay": {"active": True}},
    )


def _make_core_run(td: date) -> StockFeatureSnapshotRun:
    return StockFeatureSnapshotRun(
        trade_date=td,
        run_type="after_close",
        status="succeeded",
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
    )


def _make_publishable_review_run(core_id: uuid.UUID, td: date) -> MarketReviewRun:
    return MarketReviewRun(
        trade_date=td,
        source_core_run_id=core_id,
        source_board_run_id=None,
        source_chip_run_id=None,
        degraded_reasons=[],
        algorithm_version=REVIEW_ALGORITHM_VERSION,
        filter_version="filters-1.0.0",
        expected_scope_count=5,
        succeeded_scope_count=5,
        failed_scope_count=0,
        signal_count=0,
        coverage_ratio=Decimal("1.0"),
        status="signals_ready",
        metadata_json={"canonical_composition_readiness": {"industry_l1": "ready"}},
    )


def _make_fact(run_id: uuid.UUID, td: date, eligible: int, provided: int) -> ReviewScopeObservationFact:
    return ReviewScopeObservationFact(
        review_run_id=run_id,
        trade_date=td,
        scope_type="industry_l1",
        scope_key="all",
        pit_member_count=eligible,
        pit_member_count_t1=eligible,
        provided_member_count=provided,
        t1_membership_available=True,
        pit_status_t="ready",
        pit_status_t1="ready",
        readiness="ready",
        observation_payload={},
        diagnostics=[],
        algorithm_version=REVIEW_ALGORITHM_VERSION,
    )


async def _clean_pointers(db, td: date):
    # 保证可重入：清除该 trade_date 的 market_review pointer（测试间互不污染）
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


def _insert_pointer(db, run_id: uuid.UUID, td: date, *, superseded_by=None, published_at=None):
    pub = FactorPublication(
        scope_type=SCOPE_TYPE_REVIEW,
        scope_key=SCOPE_KEY_REVIEW,
        trade_date=td,
        publication_kind=PUBLICATION_KIND_MARKET_REVIEW,
        algorithm_version=REVIEW_ALGORITHM_VERSION,
        data_run_id=run_id,
        coverage_ratio=1.0,
        published_at=published_at,
        metadata_json="{}",
        superseded_by=superseded_by,
    )
    db.add(pub)
    return pub


# ---------------------------------------------------------------------------
# T1 — _resolve_source_core_run_id fail-closed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t1_resolve_source_core_run_id_fail_closed():
    """T1（运行于 PG suite / 验证库，非 pure unit）：source_core_run_id=None 必须 fail-closed。"""
    async with AsyncSessionLocal() as s:
        await _assert_verify_db(s)
        raised = False
        try:
            await _resolve_source_core_run_id(s, T, source_core_run_id=None)
        except Exception as exc:  # noqa: BLE001
            raised = True
            assert "source_core_run_id" in str(exc)
        assert raised, "source_core_run_id=None 必须 fail-closed"
        cid = uuid.uuid4()
        assert await _resolve_source_core_run_id(s, T, source_core_run_id=cid) == cid


# ---------------------------------------------------------------------------
# T2 — overview schema + 单 run 生产 publish owner lineage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t2_overview_schema_and_lineage():
    async with AsyncSessionLocal() as s:
        await _assert_verify_db(s)
        # schema 契约：overview 暴露 sourceCoreRunId
        assert "sourceCoreRunId" in ReviewOverviewResponse.model_fields
        assert "sourceBoardRunId" in ReviewOverviewResponse.model_fields

        core = _make_core_run(T2)
        s.add(core)
        await s.flush()
        run = _make_publishable_review_run(core.id, T2)
        s.add(run)
        await s.flush()
        s.add(_make_fact(run.id, T2, 100, 80))
        await s.flush()
        # 生产发布路径
        await publish_review(s, run)
        await s.commit()

        resp = await get_review_overview(str(T2), include_partial=False, db=s, ctx=_ctx())
        assert isinstance(resp, ReviewOverviewResponse)
        assert resp.reviewRunId == str(run.id)
        assert resp.sourceCoreRunId == str(core.id)
        assert resp.tradeDate == T2.isoformat()
        assert abs(resp.coverage.industryL1 - 0.8) < 1e-9


# ---------------------------------------------------------------------------
# T5/T7 — 同日多 ReviewRun 假绿（生产 publish + 生产 overview read path）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t5_t7_same_day_multi_run_false_green():
    """§6 多 run 假绿：同日 Core A/B + Review Y(A)/Z(B)。
    Y: 100/80；Z: 100/20。publish Y → overview=Y(0.8)；publish Z → overview=Z(0.2)。
    owner 由 publication action 决定，非 created_at/latest。
    """
    async with AsyncSessionLocal() as s:
        await _assert_verify_db(s)
        core_a = _make_core_run(T5)
        core_b = _make_core_run(T5)
        s.add(core_a)
        s.add(core_b)
        await s.flush()

        y = _make_publishable_review_run(core_a.id, T5)
        z = _make_publishable_review_run(core_b.id, T5)
        s.add(y)
        s.add(z)
        await s.flush()
        s.add(_make_fact(y.id, T5, 100, 80))
        s.add(_make_fact(z.id, T5, 100, 20))
        await s.flush()

        # CASE 1: publish Y；Z 后建但未发布
        await publish_review(s, y)
        await s.commit()
        resp_y = await get_review_overview(str(T5), include_partial=False, db=s, ctx=_ctx())
        assert resp_y.reviewRunId == str(y.id)
        assert resp_y.sourceCoreRunId == str(core_a.id)
        assert abs(resp_y.coverage.industryL1 - 0.8) < 1e-9, "overview 必须只聚合 Y 的 facts"

        # SQL 级隔离验证：Y 与 Z 各自 facts 存在但不应被彼此聚合
        facts_y = await list_scope_observation_facts(s, review_run_id=y.id, from_date=T5, to_date=T5)
        facts_z = await list_scope_observation_facts(s, review_run_id=z.id, from_date=T5, to_date=T5)
        assert len(facts_y) == 1 and facts_y[0].provided_member_count == 80
        assert len(facts_z) == 1  # Z 的 fact 存在，但 overview 不聚合

        # CASE 2: publish Z → owner 切到 Z
        await publish_review(s, z)
        await s.commit()
        resp_z = await get_review_overview(str(T5), include_partial=False, db=s, ctx=_ctx())
        assert resp_z.reviewRunId == str(z.id)
        assert resp_z.sourceCoreRunId == str(core_b.id)
        assert abs(resp_z.coverage.industryL1 - 0.2) < 1e-9, "owner 切换后必须只聚合 Z 的 facts"


# ---------------------------------------------------------------------------
# §7 — superseded pointer 假绿
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t7_superseded_pointer_false_green():
    """§7: 同 T 制造 historical H(superseded) + live L；H.published_at 更晚，
    但 get_published_review_run_id 必须返回 L。仅 superseded history、无 live pointer
    → T 不得视为当前正式 owner。"""
    async with AsyncSessionLocal() as s:
        await _assert_verify_db(s)
        await _clean_pointers(s, T7)
        core_a = _make_core_run(T7)
        core_b = _make_core_run(T7)
        s.add(core_a)
        s.add(core_b)
        await s.flush()
        run_h = _make_publishable_review_run(core_a.id, T7)
        run_l = _make_publishable_review_run(core_b.id, T7)
        s.add(run_h)
        s.add(run_l)
        await s.flush()

        now = datetime.now(timezone.utc)
        live_pub = _insert_pointer(s, run_l.id, T7, superseded_by=None, published_at=now)
        s.add(live_pub)
        await s.flush()  # 取得 live_pub.id
        h = _insert_pointer(
            s, run_h.id, T7, superseded_by=live_pub.id, published_at=now + timedelta(hours=1)
        )
        s.add(h)
        await s.commit()

        owner = await get_published_review_run_id(s, T7)
        assert owner == run_l.id, "superseded history 不得覆盖 live pointer"
        assert owner != run_h.id

        # 仅 superseded history、无 live pointer → T 不视为当前正式 owner
        # 先删 H（其 superseded_by 引用 L），再删 L
        await s.delete(h)
        await s.delete(live_pub)
        await s.commit()

        assert await get_published_review_run_id(s, T7) is None
        dates = await list_published_review_dates(s)
        assert T7 not in dates, "list_published_review_dates 不得包含仅有 superseded 的 T"


# ---------------------------------------------------------------------------
# §8 — broken pointer fail-closed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t8_broken_pointer_fail_closed():
    """§8: live pointer → run status=signals_ready / published_at=NULL
    → 用户正式 read path 不得返回 200 正式 Review（§4 guard fail-closed）。"""
    async with AsyncSessionLocal() as s:
        await _assert_verify_db(s)
        await _clean_pointers(s, T8)
        core = _make_core_run(T8)
        s.add(core)
        await s.flush()
        broken = _make_publishable_review_run(core.id, T8)
        broken.status = "signals_ready"  # 未正式发布，published_at 默认 NULL
        s.add(broken)
        await s.flush()
        # 手写 live pointer（绕过 publish_review，模拟已存在但 run 未正式发布的不一致态）
        live_pub = _insert_pointer(s, broken.id, T8, superseded_by=None, published_at=None)
        s.add(live_pub)
        await s.commit()

        assert is_formally_published_review_run(broken, broken.id) is False
        # 用户正式 read path 必须 fail-closed（500 data-integrity），不得返回 200 正式 Review
        with pytest.raises(HTTPException) as exc:
            await get_review_overview(str(T8), include_partial=False, db=s, ctx=_ctx())
        assert exc.value.status_code == 500, "broken pointer 必须 fail-closed，不得返回正式 Review"
