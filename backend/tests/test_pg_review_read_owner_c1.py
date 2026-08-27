"""PHASE C1 — Review read-owner / API 合同契约测试（targeted PostgreSQL）。

自包含合成数据，全部写入验证库 bz_stock_verify_<SHA>（由 gate cleanup 丢弃），
不读不写生产 bz_stock。

覆盖任务 §15 要求：
- T1 modified-scope unit：_resolve_source_core_run_id 在 None 时 fail-closed。
- T2 API/schema contract：ReviewOverviewResponse 暴露 sourceCoreRunId 且 owner
  解析返回显式绑定的 CoreRun。
- 多 run 假绿（targeted PG，真实 SQL lineage）：同日 T，Core A / Core B +
  Review Y(A) / Review Z(B)；发布 Y 后 owner=Y（非后建 Z）；重发 Z 后 owner=Z
  （指针覆盖，非新增行）；Y.source_core_run_id == A.id；每 trade_date 仅一行
  market_review pointer。

DB identity（fail-closed，测试自身要求）：
- APP_ENV == "verification"
- current_database() 匹配 ^bz_stock_verify_[0-9a-f]{40}$ 且 != bz_stock
"""

import os
import re
import uuid
from datetime import date, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db import AsyncSessionLocal
from app.models.factor_publication import FactorPublication
from app.models.market_review import MarketReviewRun
from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun
from app.schemas.review import ReviewOverviewResponse
from app.services.review_orchestrator_service import _resolve_source_core_run_id
from app.services.review_publication_service import (
    PUBLICATION_KIND_MARKET_REVIEW,
    SCOPE_KEY_REVIEW,
    SCOPE_TYPE_REVIEW,
    get_published_review_run_id,
)

pytestmark = pytest.mark.postgres

T = date(2026, 8, 26)
_VERIFY_DB_RE = re.compile(r"^bz_stock_verify_[0-9a-f]{40}$")


async def _assert_verify_db(db):
    env = (os.environ.get("APP_ENV") or "").lower()
    assert env == "verification", f"APP_ENV 必须 verification, got {env!r}"
    name = (await db.execute(text("select current_database()"))).scalar_one()
    assert _VERIFY_DB_RE.match(name), f"非法验证数据库: {name!r}"
    assert name != "bz_stock"
    return name


def _make_core_run():
    return StockFeatureSnapshotRun(
        trade_date=T,
        run_type="after_close",
        status="succeeded",
        started_at=datetime.now(),
        finished_at=datetime.now(),
    )


def _make_review_run(core_id):
    return MarketReviewRun(
        trade_date=T,
        source_core_run_id=core_id,
        source_board_run_id=None,
        source_chip_run_id=None,
        degraded_reasons=[],
        algorithm_version="review-1.0.0",
        filter_version="filters-1.0.0",
        expected_scope_count=0,
        succeeded_scope_count=0,
        failed_scope_count=0,
        signal_count=0,
        coverage_ratio=1.0,
        status="published",
    )


async def _publish_pointer(db, run):
    """复用 review_publication_service.publish_review 的真实 upsert SQL lineage。

    不重算发布门禁（门禁依赖 scope readiness，非本测试目标）；仅验证指针
    唯一约束 + 覆盖语义（多 run 确定性）。
    """
    now = datetime.now()
    stmt = pg_insert(FactorPublication).values(
        scope_type=SCOPE_TYPE_REVIEW,
        scope_key=SCOPE_KEY_REVIEW,
        trade_date=run.trade_date,
        publication_kind=PUBLICATION_KIND_MARKET_REVIEW,
        algorithm_version=run.algorithm_version,
        data_run_id=run.id,
        coverage_ratio=float(run.coverage_ratio),
        published_at=now,
        metadata_json="{}",
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["scope_type", "scope_key", "trade_date", "publication_kind"],
        index_where=text("superseded_by IS NULL"),
        set_={
            "algorithm_version": stmt.excluded.algorithm_version,
            "data_run_id": stmt.excluded.data_run_id,
            "coverage_ratio": stmt.excluded.coverage_ratio,
            "published_at": stmt.excluded.published_at,
            "metadata_json": stmt.excluded.metadata_json,
        },
    )
    await db.execute(stmt)
    await db.commit()


# ---------------------------------------------------------------------------
# T1 — _resolve_source_core_run_id fail-closed（单元，无 DB 依赖可独立）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t1_resolve_source_core_run_id_fail_closed():
    async with AsyncSessionLocal() as s:
        await _assert_verify_db(s)
        # None → fail-closed，绝不回退 stock_core pointer
        raised = False
        try:
            await _resolve_source_core_run_id(s, T, source_core_run_id=None)
        except Exception as exc:  # noqa: BLE001
            raised = True
            assert "source_core_run_id" in str(exc)
        assert raised, "source_core_run_id=None 必须 fail-closed"
        # 显式传入 → 原样返回
        cid = uuid.uuid4()
        got = await _resolve_source_core_run_id(s, T, source_core_run_id=cid)
        assert got == cid


# ---------------------------------------------------------------------------
# T2 — API/schema 契约：overview 暴露 sourceCoreRunId + owner 解析 lineage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t2_overview_schema_lineage():
    async with AsyncSessionLocal() as s:
        await _assert_verify_db(s)
        core = _make_core_run()
        s.add(core)
        await s.flush()
        run = _make_review_run(core.id)
        s.add(run)
        await s.flush()
        await _publish_pointer(s, run)

        owner_id = await get_published_review_run_id(s, T)
        assert owner_id == run.id
        loaded = await s.get(MarketReviewRun, owner_id)
        # 显式 lineage 通过 owner 解析保持
        assert loaded.source_core_run_id == core.id

        # schema 合同：ReviewOverviewResponse 暴露 sourceCoreRunId
        assert "sourceCoreRunId" in ReviewOverviewResponse.model_fields
        assert "sourceBoardRunId" in ReviewOverviewResponse.model_fields


# ---------------------------------------------------------------------------
# 多 run 假绿（targeted PG，真实 SQL lineage）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pg_review_multi_run_false_green():
    async with AsyncSessionLocal() as s:
        await _assert_verify_db(s)
        core_a = _make_core_run()
        core_b = _make_core_run()
        s.add(core_a)
        s.add(core_b)
        await s.flush()

        # Review Y 绑定 Core A；Review Z 绑定 Core B（Z 后建）
        y = _make_review_run(core_a.id)
        z = _make_review_run(core_b.id)
        s.add(y)
        s.add(z)
        await s.flush()

        # 发布 Y
        await _publish_pointer(s, y)
        owner = await get_published_review_run_id(s, T)
        assert owner == y.id, "owner 必须等于已发布的 Y，而非后建的 Z（假绿防护）"

        # 同日重发 Z → 指针覆盖（非新增行）
        await _publish_pointer(s, z)
        owner2 = await get_published_review_run_id(s, T)
        assert owner2 == z.id, "重新发布必须覆盖指针，而非新增行"

        # lineage 保持
        yr = await s.get(MarketReviewRun, y.id)
        zr = await s.get(MarketReviewRun, z.id)
        assert yr.source_core_run_id == core_a.id
        assert zr.source_core_run_id == core_b.id

        # 唯一约束：每 trade_date 仅一行 market_review pointer
        rows = (
            await s.execute(
                select(FactorPublication).where(
                    FactorPublication.scope_type == SCOPE_TYPE_REVIEW,
                    FactorPublication.scope_key == SCOPE_KEY_REVIEW,
                    FactorPublication.trade_date == T,
                    FactorPublication.publication_kind == PUBLICATION_KIND_MARKET_REVIEW,
                )
            )
        ).scalars().all()
        assert len(rows) == 1, "多 run 不得产生多行 market_review pointer"
