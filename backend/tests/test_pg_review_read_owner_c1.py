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

PHASE C1 FINAL 追加（统一 FORMAL_REVIEW_READ_OWNER，§3/§4/§8）：
- CASE A broken live pointer（status=signals_ready / published_at=NULL）：
       /overview fail-closed、/latest fail-closed、/dates 不得把 T 标成正式已发布日期
- CASE B valid published run：/dates 包含 T、/latest 返回该正式 run
- CASE C 仅 superseded historical pointer：/dates 不包含 T、/latest 不得 resurrect
       historical run（返回次新的正式发布日）

PHASE C1 FINAL-IDENTITY 追加（pointer ↔ run 交易日 identity）：
- cross-date corruption：pointer(T_ALIAS) → ReviewRun(T_REAL) 且 T_ALIAS != T_REAL，
       ReviewRun 自身完全合法（status=published + published_at NOT NULL）；
       LIVE pointer 存在但 /dates 排除 T_ALIAS、/overview 500、/latest 500，
       而 T_REAL 自己的合法同日 pointer 不受影响

/latest 语义 = live pointer 中最大 trade_date，因此 CASE A/B/C 使用远大于本文件
其它用例（2026-08-xx）的日期（2099-12-31 / 2099-12-30），确保"该 T 必为 /latest
命中目标"这一前提确定成立，不依赖 pytest 执行顺序或其它测试文件残留。

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
from sqlalchemy import select, text

from app.api.review import get_latest_review, get_review_dates, get_review_overview
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
    list_formally_published_review_dates,
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
# PHASE C1 FINAL §8 CASE A/B/C：/latest 取 live pointer 的最大 trade_date，
# 故使用远大于上述日期的 sentinel 日期，保证命中确定、不依赖测试执行顺序。
T_MAX = date(2099, 12, 31)
T_PREV = date(2099, 12, 30)
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

        assert (
            is_formally_published_review_run(
                broken, broken.id, expected_trade_date=T8,
            )
            is False
        )
        # 用户正式 read path 必须 fail-closed（500 data-integrity），不得返回 200 正式 Review
        with pytest.raises(HTTPException) as exc:
            await get_review_overview(str(T8), include_partial=False, db=s, ctx=_ctx())
        assert exc.value.status_code == 500, "broken pointer 必须 fail-closed，不得返回正式 Review"


# ---------------------------------------------------------------------------
# PHASE C1 FINAL §8 CASE A — broken live pointer：三个用户端点同时 fail-closed/排除
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_c1_final_case_a_broken_pointer_all_user_endpoints():
    """CASE A：broken live pointer（status=signals_ready / published_at=NULL）
    → /overview fail-closed、/latest fail-closed、/dates 不得把 T 标成正式发布日期。

    /latest 必须 fail-closed（500），**不得**回退到同日其它 ReviewRun，
    也**不得**跳过到更早的交易日（§3 / §8 CASE A）。
    """
    async with AsyncSessionLocal() as s:
        await _assert_verify_db(s)
        await _clean_pointers(s, T_MAX)
        core = _make_core_run(T_MAX)
        s.add(core)
        await s.flush()
        broken = _make_publishable_review_run(core.id, T_MAX)
        broken.status = "signals_ready"  # 未正式发布，published_at 保持 NULL
        s.add(broken)
        await s.flush()
        # 手写 live pointer（异常 DB 状态，允许 fixture 直接插表）
        s.add(_insert_pointer(s, broken.id, T_MAX, superseded_by=None, published_at=None))
        await s.commit()

        # 1) /overview fail-closed
        with pytest.raises(HTTPException) as exc_o:
            await get_review_overview(str(T_MAX), include_partial=False, db=s, ctx=_ctx())
        assert exc_o.value.status_code == 500

        # 2) /latest fail-closed（复用统一 formal guard，不自行 pointer→get→return）
        with pytest.raises(HTTPException) as exc_l:
            await get_latest_review(db=s, ctx=_ctx())
        assert exc_l.value.status_code == 500, (
            f"/latest 对 broken pointer 必须 fail-closed，got {exc_l.value.status_code}"
        )

        # 3) /dates 不得把 T 标成正式已发布日期（DB 层 JOIN run formal state）
        dates_resp = await get_review_dates(db=s, ctx=_ctx())
        assert T_MAX.isoformat() not in dates_resp.trade_dates, (
            "broken pointer 的 T 不得列为正式已发布日期"
        )
        formal = await list_formally_published_review_dates(s, limit=500)
        assert T_MAX not in formal
        # LIVE POINTER OWNER 与 FORMAL OWNER 的区别：pointer 存在 ≠ 正式发布
        live = await list_published_review_dates(s, limit=500)
        assert T_MAX in live, "live pointer 仍然存在（证明 /dates 的排除来自 run formal state）"

        await _clean_pointers(s, T_MAX)
        await s.commit()


# ---------------------------------------------------------------------------
# PHASE C1 FINAL §8 CASE B — valid published run：/dates 含 T、/latest 返回该 run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_c1_final_case_b_valid_published_dates_and_latest():
    """CASE B：valid published run（生产 publish_review 路径）
    → /dates 包含 T、/latest 返回该正式 run。"""
    async with AsyncSessionLocal() as s:
        await _assert_verify_db(s)
        await _clean_pointers(s, T_MAX)
        core = _make_core_run(T_MAX)
        s.add(core)
        await s.flush()
        run = _make_publishable_review_run(core.id, T_MAX)
        s.add(run)
        await s.flush()
        s.add(_make_fact(run.id, T_MAX, 100, 80))
        await s.flush()
        await publish_review(s, run)  # 生产发布路径，绝不复制 SQL
        await s.commit()

        assert (
            is_formally_published_review_run(
                run, run.id, expected_trade_date=T_MAX,
            )
            is True
        )

        dates_resp = await get_review_dates(db=s, ctx=_ctx())
        assert T_MAX.isoformat() in dates_resp.trade_dates, (
            "正式发布日的 T 必须出现在 /dates"
        )
        assert dates_resp.latest_trade_date == T_MAX.isoformat()

        latest = await get_latest_review(db=s, ctx=_ctx())
        assert latest.review_run_id == str(run.id)
        assert latest.trade_date == T_MAX.isoformat()
        assert latest.status == "published"

        await _clean_pointers(s, T_MAX)
        await s.commit()


# ---------------------------------------------------------------------------
# PHASE C1 FINAL §8 CASE C — 仅 superseded historical：排除 T + /latest 不复活
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_c1_final_case_c_superseded_only_excluded():
    """CASE C：T 只有 superseded historical pointer
    → /dates 不包含 T、/latest 不得 resurrect 该 historical run。

    historical run 自身是正式发布态（status=published + published_at 非空），
    被排除的唯一原因是它的 pointer 已被 supersede —— 这正是"live pointer ≠
    formal read owner"的分界。为让 /latest 有确定性返回值，同时准备一个次新的
    正式发布日 T_PREV。
    """
    async with AsyncSessionLocal() as s:
        await _assert_verify_db(s)
        await _clean_pointers(s, T_MAX)
        await _clean_pointers(s, T_PREV)

        core_h = _make_core_run(T_MAX)
        s.add(core_h)
        await s.flush()
        run_h = _make_publishable_review_run(core_h.id, T_MAX)
        # 异常/历史状态由 fixture 直接制造：run 已正式发布，但 pointer 已被 supersede
        run_h.status = "published"
        run_h.published_at = datetime.now(timezone.utc)
        s.add(run_h)
        await s.flush()
        s.add(
            _insert_pointer(
                s,
                run_h.id,
                T_MAX,
                superseded_by=uuid.uuid4(),  # superseded_by 无 FK，直接制造 historical 语义
                published_at=datetime.now(timezone.utc),
            )
        )

        core_p = _make_core_run(T_PREV)
        s.add(core_p)
        await s.flush()
        run_p = _make_publishable_review_run(core_p.id, T_PREV)
        s.add(run_p)
        await s.flush()
        s.add(_make_fact(run_p.id, T_PREV, 100, 80))
        await s.flush()
        await publish_review(s, run_p)  # 生产发布路径
        await s.commit()

        dates_resp = await get_review_dates(db=s, ctx=_ctx())
        assert T_MAX.isoformat() not in dates_resp.trade_dates, (
            "superseded historical pointer 对应的 T 不得列为正式已发布日期"
        )
        assert T_PREV.isoformat() in dates_resp.trade_dates

        latest = await get_latest_review(db=s, ctx=_ctx())
        assert latest.review_run_id != str(run_h.id), "/latest 不得 resurrect historical run"
        assert latest.trade_date == T_PREV.isoformat()
        assert latest.review_run_id == str(run_p.id)

        await _clean_pointers(s, T_MAX)
        await _clean_pointers(s, T_PREV)
        await s.commit()


# ---------------------------------------------------------------------------
# PHASE C1 FINAL-IDENTITY §6-§10 — cross-date pointer corruption
# ---------------------------------------------------------------------------

# T_ALIAS 复用 sentinel 最大日期，确保 /latest 必定命中这个 cross-date pointer；
# T_REAL 是 ReviewRun 自己真正所属的交易日。
T_ALIAS = date(2099, 12, 31)
T_REAL = date(2020, 1, 2)


@pytest.mark.asyncio
async def test_c1_final_identity_cross_date_pointer_fail_closed():
    """cross-date corruption：pointer(T_ALIAS) → ReviewRun(T_REAL)，T_ALIAS != T_REAL。

    ReviewRun 自身完全合法（status=published + published_at NOT NULL），唯一异常是
    pointer 的交易日与 run 的交易日不一致。因此：

    - ``get_published_review_run_id(T_ALIAS) == R.id`` —— **LIVE pointer 确实存在**
      （§9：live pointer exists ≠ formal review exists，owner 分层保持清晰）；
    - ``list_formally_published_review_dates`` / ``/dates`` **不包含 T_ALIAS**（§3/§7）；
    - ``/overview/T_ALIAS`` → 500 fail-closed，**不得**返回 ``tradeDate=T_REAL`` 的
      200 payload（§8）；
    - ``/latest`` → 500 fail-closed（§6：T_ALIAS 为最大 live-pointer date），
      **不得**返回 T_REAL、不得跳过到更早日期、不得把 alias 当正式日期；
    - T_REAL 自己另有合法同日 pointer 时，**T_REAL 仍正常存在**（§7）。
    """
    async with AsyncSessionLocal() as s:
        await _assert_verify_db(s)
        await _clean_pointers(s, T_ALIAS)
        await _clean_pointers(s, T_REAL)

        core_a = _make_core_run(T_REAL)
        core_b = _make_core_run(T_REAL)
        s.add(core_a)
        s.add(core_b)
        await s.flush()

        # R：被 cross-date pointer 指向的 run。自身是正式发布态，只有 pointer 日期错位。
        run_r = _make_publishable_review_run(core_a.id, T_REAL)
        run_r.status = "published"
        run_r.published_at = datetime.now(timezone.utc)
        s.add(run_r)
        await s.flush()

        # V：T_REAL 自己的合法正式发布 run（生产 publish_review 路径）。
        run_v = _make_publishable_review_run(core_b.id, T_REAL)
        s.add(run_v)
        await s.flush()
        s.add(_make_fact(run_v.id, T_REAL, 100, 80))
        await s.flush()
        await publish_review(s, run_v)  # 生产发布路径，绝不复制 SQL

        # 人工插入 corrupt cross-date pointer（测试目标就是异常 DB 状态）
        s.add(
            _insert_pointer(
                s,
                run_r.id,
                T_ALIAS,
                superseded_by=None,
                published_at=datetime.now(timezone.utc),
            )
        )
        await s.commit()

        # ---- §9 LIVE POINTER LAYER：pointer 确实存在，但这不构成 formal review ----
        assert await get_published_review_run_id(s, T_ALIAS) == run_r.id, (
            "live pointer 存在（LIVE POINTER RESOLVER 仍可解析 run_id）"
        )
        assert await get_published_review_run_id(s, T_REAL) == run_v.id
        # 同一 run_r 在**自己**的交易日上下文里也满足 status/published_at，
        # 证明下面的排除确实来自 trade_date identity，而非 run 状态。
        assert run_r.status == "published" and run_r.published_at is not None

        # ---- §3/§7 /dates：T_ALIAS 不得列入，T_REAL 正常存在 ----
        formal = await list_formally_published_review_dates(s, limit=500)
        assert T_ALIAS not in formal, "cross-date pointer 的 T_ALIAS 不得列为正式已发布日期"
        assert T_REAL in formal, "T_REAL 自己的合法同日 pointer 不受影响"

        dates_resp = await get_review_dates(db=s, ctx=_ctx())
        assert T_ALIAS.isoformat() not in dates_resp.trade_dates
        assert T_REAL.isoformat() in dates_resp.trade_dates

        # ---- §8 /overview/T_ALIAS → 500 fail-closed ----
        with pytest.raises(HTTPException) as exc_o:
            await get_review_overview(str(T_ALIAS), include_partial=False, db=s, ctx=_ctx())
        assert exc_o.value.status_code == 500, (
            f"cross-date pointer 的 /overview 必须 fail-closed，got {exc_o.value.status_code}"
        )
        assert "不一致" in str(exc_o.value.detail), "detail 必须明确指出 trade_date 不一致"

        # ---- §6 /latest → 500 fail-closed（T_ALIAS 为最大 live-pointer date）----
        with pytest.raises(HTTPException) as exc_l:
            await get_latest_review(db=s, ctx=_ctx())
        assert exc_l.value.status_code == 500, (
            f"/latest 遇到 cross-date pointer 必须 fail-closed，got {exc_l.value.status_code}"
        )

        # ---- §7 正向对照：T_REAL 自己的 overview 仍是 200 且返回 V ----
        resp_real = await get_review_overview(
            str(T_REAL), include_partial=False, db=s, ctx=_ctx(),
        )
        assert resp_real.reviewRunId == str(run_v.id)
        assert resp_real.tradeDate == T_REAL.isoformat()

        await _clean_pointers(s, T_ALIAS)
        await _clean_pointers(s, T_REAL)
        await s.commit()


# ===========================================================================
# PHASE F1C — PG canary（附加在本已注册的 Review PG owner 文件内，
# 不修改受保护 verifier registry）
#
# 目的：在隔离 verification PostgreSQL 上证明 3 个合同：
#   1. 3-scope 记账：declared=3 / eligible=2 / succeeded=2 / skipped=1 / failed=0
#   2. publication：legal skip 不阻塞；real failure 阻塞（由真实 owner 判定）
#   3. cross-run：Y1/Y2 事实与组合不混串；formal pointer 指向 exact owner
#
# 与 PRODUCTION_OBSERVED（772/749/23）是**两个独立证据来源**，不得混写。
# ===========================================================================

F1C_T = date(2099, 12, 20)
F1C_SCOPE_A = "f1c_a"
F1C_SCOPE_B = "f1c_b"
F1C_SCOPE_C = "f1c_c_skipped"


async def _build_f1c_run(db, *, td: date, with_failure: bool = False):
    """Core X → Review Y + 3 个 metrics run item。

    A = succeeded（eligible，已落 canonical Fact/Composition）
    B = succeeded；with_failure 时为 failed
    C = **合法跳过**（诊断性终态，不得落 Fact/Composition）

    readiness 只覆盖真正 eligible 且已落 canonical fact 的 scope；
    skipped scope **不进** readiness（它没有 canonical fact）。
    """
    from app.models.market_review import MarketReviewRunItem
    from app.services.review_orchestrator_service import (
        ITEM_FAILED,
        ITEM_SKIPPED,
        ITEM_SUCCEEDED,
        PHASE_METRICS,
    )

    core = _make_core_run(td)
    db.add(core)
    await db.flush()

    run = _make_publishable_review_run(core.id, td)
    run.expected_scope_count = 3
    run.succeeded_scope_count = 1 if with_failure else 2
    run.failed_scope_count = 1 if with_failure else 0
    run.metadata_json = {
        "canonical_composition_readiness": {
            F1C_SCOPE_A: "ready",
            **({} if with_failure else {F1C_SCOPE_B: "ready"}),
        },
    }
    db.add(run)
    await db.flush()

    def _item(key: str, status: str) -> MarketReviewRunItem:
        return MarketReviewRunItem(
            review_run_id=run.id,
            scope_type="concept",
            scope_key=key,
            phase=PHASE_METRICS,
            status=status,
            attempt_count=1,
        )

    db.add(_item(F1C_SCOPE_A, ITEM_SUCCEEDED))
    db.add(_item(F1C_SCOPE_B, ITEM_FAILED if with_failure else ITEM_SUCCEEDED))
    db.add(_item(F1C_SCOPE_C, ITEM_SKIPPED))
    await db.flush()
    return core, run


@pytest.mark.asyncio
async def test_f1c_pg_three_scope_legal_skip_contract():
    """§8/§9 3-scope canary：真实 DB 上验证记账合同与发布许可。"""
    from app.services.review_orchestrator_service import (
        _count_scope_status,
        build_scope_execution_metadata,
    )

    async with AsyncSessionLocal() as s:
        await _assert_verify_db(s)
        await _clean_pointers(s, F1C_T)
        await s.commit()

        core, run = await _build_f1c_run(s, td=F1C_T)
        await s.commit()

        # --- 真实 DB 三态重算（不是 fake session）---
        succeeded, skipped, failed = await _count_scope_status(s, run.id)
        assert (succeeded, skipped, failed) == (2, 1, 0), (
            "legal skipped 不得被计入 succeeded"
        )

        # --- metadata 单一 owner 合同 ---
        meta = build_scope_execution_metadata(
            declared=run.expected_scope_count,
            succeeded=succeeded,
            skipped=skipped,
            failed=failed,
        )
        assert meta == {
            "declared": 3,
            "eligible": 2,
            "succeeded": 2,
            "skipped": 1,
            "failed": 0,
            "execution_success_ratio": 1.0,
        }

        # --- §10 formal publication：legal skip 不得阻塞 ---
        from app.services.review_publication_service import publish_review

        pub = await publish_review(s, run)
        assert pub is not None, "legal skip 不得阻塞正式发布"
        await s.commit()

        assert pub.data_run_id == run.id, "publication 必须精确指向 Review Y"
        assert pub.trade_date == F1C_T
        assert run.source_core_run_id == core.id, (
            "Review.source_core_run_id 必须精确绑定 Core X"
        )
        assert run.status == "published"

        # --- 清理 ---
        await _clean_pointers(s, F1C_T)
        await s.commit()


@pytest.mark.asyncio
async def test_f1c_pg_publication_real_failure_blocked():
    """§5/§6 负向对照：真实 failed item 必须被真实 owner 阻塞。

    预期**不是**测试发明的：evaluate_publish_gate 查询
    MarketReviewRunItem.status in (failed, pending, running)；
    skipped 被显式排除（诊断性终态，不阻塞）。
    """
    from app.services.review_publication_service import (
        ReviewPublishBlockError,
        publish_review,
    )

    async with AsyncSessionLocal() as s:
        await _assert_verify_db(s)
        await _clean_pointers(s, F1C_T)
        await s.commit()

        _core, run = await _build_f1c_run(s, td=F1C_T, with_failure=True)
        await s.commit()

        with pytest.raises(ReviewPublishBlockError) as exc:
            await publish_review(s, run)
        blockers = list(exc.value.blockers)
        assert blockers, "真实失败必须给出 blockers，不得 false-green 发布"
        assert any("failed" in str(b).lower() for b in blockers), (
            f"blockers 必须点明失败项: {blockers}"
        )
        await s.rollback()

        await _clean_pointers(s, F1C_T)
        await s.commit()


@pytest.mark.asyncio
async def test_f1c_pg_cross_run_isolation_and_pointer_owner():
    """§11-§14 cross-run：同 trade_date 两个 Review，事实不混串。

    Y1 的 created_at **刻意更晚**，但 formal pointer 必须由正式发布动作
    决定 —— 因此最终 owner 是 Y2（被正式发布的那一个），而不是 created_at
    最新的那一个（anti-latest negative control）。
    """
    from app.services.review_publication_service import publish_review

    async with AsyncSessionLocal() as s:
        await _assert_verify_db(s)
        await _clean_pointers(s, F1C_T)
        await s.commit()

        core1, run1 = await _build_f1c_run(s, td=F1C_T)
        await s.commit()
        core2, run2 = await _build_f1c_run(s, td=F1C_T)
        await s.commit()

        # --- 让两套数据可机器区分（§12）---
        # 每个 run 一条 canonical fact，用 pit_member_count / provided
        # 区分，混串可被直接检出。
        s.add(_make_fact(run1.id, F1C_T, 100, 80))
        s.add(_make_fact(run2.id, F1C_T, 200, 160))
        await s.commit()

        # Y1 created_at 更晚（易误判形状）
        run1.created_at = datetime.now(timezone.utc) + timedelta(hours=2)
        run2.created_at = datetime.now(timezone.utc) - timedelta(hours=2)
        await s.commit()

        # 只正式发布 Y2
        pub = await publish_review(s, run2)
        assert pub is not None
        await s.commit()

        # --- formal pointer 必须指向 Y2（不是 created_at 最新的 Y1）---
        live = await get_published_review_run_id(s, F1C_T)
        assert live == run2.id, "formal pointer 必须由发布动作决定，不是 created_at"
        assert live != run1.id

        # --- facts / compositions 不混串 ---
        f1 = (
            await s.execute(
                select(ReviewScopeObservationFact).where(
                    ReviewScopeObservationFact.review_run_id == run1.id
                )
            )
        ).scalars().all()
        f2 = (
            await s.execute(
                select(ReviewScopeObservationFact).where(
                    ReviewScopeObservationFact.review_run_id == run2.id
                )
            )
        ).scalars().all()

        assert len(f1) == 1 and len(f2) == 1, "每个 run 必须各有一条 canonical fact"
        assert all(f.review_run_id == run1.id for f in f1)
        assert all(f.review_run_id == run2.id for f in f2)
        assert not ({f.id for f in f1} & {f.id for f in f2}), "fact 不得跨 run 混串"
        # §12：数值可区分，证明读到的确实是各自 run 的数据
        assert f1[0].pit_member_count == 100
        assert f2[0].pit_member_count == 200

        # --- lineage 精确绑定各自 Core ---
        await s.refresh(run1)
        await s.refresh(run2)
        assert run1.source_core_run_id == core1.id
        assert run2.source_core_run_id == core2.id
        assert core1.id != core2.id

        # --- §15 不得引入 stock_core 作为 Review 前置 ---
        sc = (
            await s.execute(
                select(FactorPublication).where(
                    FactorPublication.trade_date == F1C_T,
                    FactorPublication.publication_kind == "stock_core",
                )
            )
        ).scalars().all()
        assert sc == [], "Review 成功不得依赖 stock_core publication"

        await _clean_pointers(s, F1C_T)
        await s.commit()
