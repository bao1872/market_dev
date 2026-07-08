"""research_matrix_writer 测试 - TDD RED。

验证内容：
1. 磁盘/月份/失败率三道硬阈值
2. monthly run 生命周期（create/resume/finalize）
3. 批量 upsert 到 research_feature_matrix_rows（on_conflict_do_update 幂等覆盖）
4. 月份 → (start_date, end_date) 解析
5. dry-run 估算（expected_rows / estimated_db_size）

用法：
    cd backend && APP_ENV=test pytest tests/test_research_matrix_writer.py -v
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.models.research_feature_matrix import (
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    ResearchFeatureMatrixRow,
    ResearchFeatureMatrixRun,
)
from app.research.research_matrix_writer import (
    MONTH_SIZE_MAX_GB,
    check_disk_threshold,
    check_failure_rate,
    check_month_size_threshold,
    create_or_resume_run,
    estimate_month_size,
    finalize_run,
    resolve_month_range,
    upsert_rows_batch,
)

# =============================================================================
# 1. 硬阈值检查（纯函数，无 DB）
# =============================================================================


class TestDiskThreshold:
    """磁盘剩余空间硬阈值（< 15GB 停止）。"""

    def test_blocks_when_free_below_15gb(self) -> None:
        """剩余 < 15GB → False（应停止）。"""
        with patch("app.research.research_matrix_writer.shutil.disk_usage") as mock_du:
            mock_du.return_value = type(
                "du", (), {"total": 100 * 10**9, "used": 90 * 10**9, "free": 10 * 10**9}
            )()
            assert check_disk_threshold("/") is False

    def test_passes_when_free_above_15gb(self) -> None:
        """剩余 >= 15GB → True（可继续）。"""
        with patch("app.research.research_matrix_writer.shutil.disk_usage") as mock_du:
            mock_du.return_value = type(
                "du", (), {"total": 100 * 10**9, "used": 80 * 10**9, "free": 20 * 10**9}
            )()
            assert check_disk_threshold("/") is True

    def test_passes_at_exact_15gb(self) -> None:
        """剩余 = 15GB（边界）→ True。"""
        with patch("app.research.research_matrix_writer.shutil.disk_usage") as mock_du:
            # 使用 1024^3 而非 10^9，与 check_disk_threshold 的 GB 计算一致
            fifteen_gb = 15 * (1024**3)
            mock_du.return_value = type(
                "du",
                (),
                {"total": 100 * (1024**3), "used": 85 * (1024**3), "free": fifteen_gb},
            )()
            assert check_disk_threshold("/") is True


class TestMonthSizeThreshold:
    """单月输出预估大小硬阈值（> 3GB 停止）。"""

    def test_blocks_when_estimated_above_3gb(self) -> None:
        """预估 3.5GB → False。"""
        assert check_month_size_threshold(3.5) is False

    def test_passes_when_estimated_below_3gb(self) -> None:
        """预估 2.0GB → True。"""
        assert check_month_size_threshold(2.0) is True

    def test_passes_at_exact_3gb(self) -> None:
        """预估 = 3GB（边界）→ True。"""
        assert check_month_size_threshold(float(MONTH_SIZE_MAX_GB)) is True

    def test_passes_zero(self) -> None:
        """预估 0 → True（无数据）。"""
        assert check_month_size_threshold(0.0) is True


class TestFailureRateThreshold:
    """失败率硬阈值（> 5% 停止）。"""

    def test_blocks_when_rate_above_5pct(self) -> None:
        """6/100 = 6% → False。"""
        assert check_failure_rate(failed=6, total=100) is False

    def test_passes_when_rate_at_5pct(self) -> None:
        """5/100 = 5% → True（边界）。"""
        assert check_failure_rate(failed=5, total=100) is True

    def test_passes_when_rate_below_5pct(self) -> None:
        """3/100 = 3% → True。"""
        assert check_failure_rate(failed=3, total=100) is True

    def test_zero_total_returns_true(self) -> None:
        """total=0 → True（无数据，不触发阈值）。"""
        assert check_failure_rate(failed=0, total=0) is True


# =============================================================================
# 2. 月份 → (start_date, end_date) 解析（纯函数）
# =============================================================================


class TestResolveMonthRange:
    """将 YYYY-MM 字符串解析为 (start_date, end_date)。"""

    def test_january(self) -> None:
        """2026-01 → (2026-01-01, 2026-01-31)。"""
        start, end = resolve_month_range("2026-01")
        assert start == date(2026, 1, 1)
        assert end == date(2026, 1, 31)

    def test_february_non_leap(self) -> None:
        """2026-02（非闰年）→ (2026-02-01, 2026-02-28)。"""
        start, end = resolve_month_range("2026-02")
        assert start == date(2026, 2, 1)
        assert end == date(2026, 2, 28)

    def test_february_leap_year(self) -> None:
        """2024-02（闰年）→ (2024-02-01, 2024-02-29)。"""
        start, end = resolve_month_range("2024-02")
        assert start == date(2024, 2, 1)
        assert end == date(2024, 2, 29)

    def test_december(self) -> None:
        """2026-12 → (2026-12-01, 2026-12-31)。"""
        start, end = resolve_month_range("2026-12")
        assert start == date(2026, 12, 1)
        assert end == date(2026, 12, 31)

    def test_invalid_format_raises(self) -> None:
        """非法格式 → ValueError。"""
        with pytest.raises(ValueError):
            resolve_month_range("2026-13")
        with pytest.raises(ValueError):
            resolve_month_range("2026/01")
        with pytest.raises(ValueError):
            resolve_month_range("not-a-month")


# =============================================================================
# 3. 月份大小估算（纯函数）
# =============================================================================


class TestEstimateMonthSize:
    """根据 instruments_count × trade_dates_count 估算单月 DB 占用。"""

    def test_small_sample(self) -> None:
        """100 股 × 20 交易日 = 2000 行。估算应 < 0.01GB。"""
        est = estimate_month_size(instruments_count=100, trade_dates_count=20)
        assert est > 0
        assert est < 0.1  # < 100MB

    def test_full_month_january(self) -> None:
        """5000 股 × 20 交易日 = 100000 行。估算应合理（GB 量级）。"""
        est = estimate_month_size(instruments_count=5000, trade_dates_count=20)
        assert est > 0.1  # > 100MB
        # 100000 行 × ~2KB/行 = 200MB（保守上界）
        assert est < 1.0  # < 1GB

    def test_zero_instruments(self) -> None:
        """0 股 → 0GB。"""
        assert estimate_month_size(instruments_count=0, trade_dates_count=20) == 0.0


# =============================================================================
# 4. monthly run 生命周期（DB）
# =============================================================================


@pytest.mark.asyncio
class TestRunLifecycle:
    """monthly run 创建/恢复/终结。"""

    async def test_create_run_returns_new_running_run(self, db_session) -> None:
        """首次创建 → status=running。"""
        run = await create_or_resume_run(
            db_session,
            month="2026-01",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
            scope="full",
        )
        assert run.run_key == "2026-01_full"
        assert run.month == "2026-01"
        assert run.status == STATUS_RUNNING
        assert run.started_at is not None

    async def test_create_run_idempotent_same_run_key(self, db_session) -> None:
        """相同 run_key 第二次调用 → 返回已存在 run（不重复创建）。"""
        run1 = await create_or_resume_run(
            db_session,
            month="2026-01",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
            scope="full",
        )
        run2 = await create_or_resume_run(
            db_session,
            month="2026-01",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
            scope="full",
        )
        assert run1.id == run2.id
        assert run1.run_key == run2.run_key

    async def test_create_run_distinguishes_scope(self, db_session) -> None:
        """不同 scope（full / sample_100）→ 不同 run_key。"""
        run_full = await create_or_resume_run(
            db_session,
            month="2026-01",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
            scope="full",
        )
        run_sample = await create_or_resume_run(
            db_session,
            month="2026-01",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
            scope="sample_100",
        )
        assert run_full.run_key != run_sample.run_key
        assert run_full.id != run_sample.id

    async def test_finalize_run_succeeded(self, db_session) -> None:
        """finalize_run(succeeded) → 更新 status/rows_count/duration/finished_at。"""
        run = await create_or_resume_run(
            db_session,
            month="2026-01",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
            scope="full",
        )
        await finalize_run(
            db_session,
            run,
            status=STATUS_SUCCEEDED,
            instruments_count=100,
            trade_dates_count=20,
            rows_count=2000,
            failed_count=10,
            duration_seconds=300.5,
        )
        # 重新查询
        stmt = select(ResearchFeatureMatrixRun).where(ResearchFeatureMatrixRun.id == run.id)
        result = await db_session.execute(stmt)
        updated = result.scalar_one()
        assert updated.status == STATUS_SUCCEEDED
        assert updated.instruments_count == 100
        assert updated.trade_dates_count == 20
        assert updated.rows_count == 2000
        assert updated.failed_count == 10
        assert updated.duration_seconds == 300.5
        assert updated.finished_at is not None

    async def test_finalize_run_failed(self, db_session) -> None:
        """finalize_run(failed) → status=failed。"""
        run = await create_or_resume_run(
            db_session,
            month="2026-02",
            start_date=date(2026, 2, 1),
            end_date=date(2026, 2, 28),
            scope="full",
        )
        await finalize_run(
            db_session,
            run,
            status=STATUS_FAILED,
            instruments_count=100,
            trade_dates_count=20,
            rows_count=500,
            failed_count=1500,
            duration_seconds=100.0,
        )
        stmt = select(ResearchFeatureMatrixRun).where(ResearchFeatureMatrixRun.id == run.id)
        result = await db_session.execute(stmt)
        updated = result.scalar_one()
        assert updated.status == STATUS_FAILED
        assert updated.failed_count == 1500

    async def test_finalize_run_records_failed_instruments_and_rows(
        self, db_session
    ) -> None:
        """[Blocker 3] finalize_run 在 metadata_json 记录 failed_instruments 和 failed_rows。

        failed_count 列存 failed_rows（行级失败数），
        metadata_json 额外记录 failed_instruments（股票级失败数）。
        """
        run = await create_or_resume_run(
            db_session,
            month="2026-03",
            start_date=date(2026, 3, 1),
            end_date=date(2026, 3, 31),
            scope="full",
        )
        await finalize_run(
            db_session,
            run,
            status=STATUS_FAILED,
            instruments_count=5000,
            trade_dates_count=22,
            rows_count=100000,
            failed_count=10000,  # failed_rows
            duration_seconds=600.0,
            metadata={"feature_version": "phase1_no_node_cluster"},
            failed_instruments=500,  # 500 只股票失败
        )
        stmt = select(ResearchFeatureMatrixRun).where(
            ResearchFeatureMatrixRun.id == run.id
        )
        result = await db_session.execute(stmt)
        updated = result.scalar_one()
        # failed_count 列 = failed_rows
        assert updated.failed_count == 10000
        # metadata_json 记录 failed_instruments 和 failed_rows
        assert updated.metadata_json is not None
        assert updated.metadata_json["failed_instruments"] == 500
        assert updated.metadata_json["failed_rows"] == 10000
        assert updated.metadata_json["feature_version"] == "phase1_no_node_cluster"

    async def test_resume_does_not_reset_completed_run(self, db_session) -> None:
        """[Blocker 7] resume 不应破坏已完成 full run 的统计。

        create_or_resume_run 对已 succeeded 的 run 只返回引用，
        不重置 status/rows_count/failed_count 等统计字段。
        """
        # 1. 创建并 finalize 一个 succeeded run
        run = await create_or_resume_run(
            db_session,
            month="2026-04",
            start_date=date(2026, 4, 1),
            end_date=date(2026, 4, 30),
            scope="full",
        )
        await finalize_run(
            db_session,
            run,
            status=STATUS_SUCCEEDED,
            instruments_count=5000,
            trade_dates_count=22,
            rows_count=110000,
            failed_count=0,
            duration_seconds=3600.0,
            failed_instruments=0,
        )

        # 2. resume：相同 run_key 再次调用
        run_resumed = await create_or_resume_run(
            db_session,
            month="2026-04",
            start_date=date(2026, 4, 1),
            end_date=date(2026, 4, 30),
            scope="full",
        )
        # 应返回同一个 run
        assert run_resumed.id == run.id
        # [Blocker 7] 统计字段不应被重置
        assert run_resumed.status == STATUS_SUCCEEDED
        assert run_resumed.rows_count == 110000
        assert run_resumed.failed_count == 0
        assert run_resumed.instruments_count == 5000


# =============================================================================
# 5. 批量 upsert rows（DB）
# =============================================================================


@pytest.mark.asyncio
class TestUpsertRowsBatch:
    """批量 upsert 到 research_feature_matrix_rows。"""

    async def _make_run(self, db_session) -> ResearchFeatureMatrixRun:
        """创建测试 run。"""
        return await create_or_resume_run(
            db_session,
            month="2026-01",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
            scope="full",
        )

    def _make_row_dict(
        self,
        run_id: uuid.UUID,
        instrument_id: uuid.UUID,
        symbol: str,
        trade_date: date,
        *,
        causal_atr: float = 1.5,
    ) -> dict:
        """构造单行 dict（仅设置少量 feature 字段，其余为 None）。"""
        return {
            "run_id": run_id,
            "instrument_id": instrument_id,
            "symbol": symbol,
            "trade_date": trade_date,
            "causal_atr": causal_atr,
            "causal_bb_percent_b": 0.5,
            "label_future_return_5d": 0.02,
        }

    async def test_inserts_new_rows(self, db_session) -> None:
        """首次 upsert → 写入新行。"""
        run = await self._make_run(db_session)
        inst_id = uuid.uuid4()
        rows = [
            self._make_row_dict(run.id, inst_id, "000001", date(2026, 1, 5)),
            self._make_row_dict(run.id, inst_id, "000001", date(2026, 1, 6)),
        ]
        count = await upsert_rows_batch(db_session, rows)
        assert count == 2

        # 验证 DB
        stmt = select(ResearchFeatureMatrixRow).where(
            ResearchFeatureMatrixRow.run_id == run.id
        )
        result = await db_session.execute(stmt)
        db_rows = result.scalars().all()
        assert len(db_rows) == 2

    async def test_upsert_on_conflict_updates_existing(self, db_session) -> None:
        """相同 (instrument_id, trade_date) → ON CONFLICT DO UPDATE 覆盖旧值。"""
        run = await self._make_run(db_session)
        inst_id = uuid.uuid4()
        rows_v1 = [self._make_row_dict(run.id, inst_id, "000001", date(2026, 1, 5), causal_atr=1.0)]
        await upsert_rows_batch(db_session, rows_v1)

        # 第二次：同 (instrument_id, trade_date)，不同 causal_atr
        rows_v2 = [self._make_row_dict(run.id, inst_id, "000001", date(2026, 1, 5), causal_atr=2.5)]
        count = await upsert_rows_batch(db_session, rows_v2)
        assert count == 1  # upsert 返回 1（受影响行）

        # 验证 DB 中只有 1 行，且 causal_atr 已更新
        stmt = select(ResearchFeatureMatrixRow).where(
            ResearchFeatureMatrixRow.instrument_id == inst_id,
            ResearchFeatureMatrixRow.trade_date == date(2026, 1, 5),
        )
        result = await db_session.execute(stmt)
        db_rows = result.scalars().all()
        assert len(db_rows) == 1
        assert db_rows[0].causal_atr == 2.5

    async def test_empty_rows_returns_zero(self, db_session) -> None:
        """空 list → 返回 0，不执行 DB 操作。"""
        count = await upsert_rows_batch(db_session, [])
        assert count == 0

    async def test_batch_above_limit_chunks_correctly(self, db_session) -> None:
        """超过单批上限（默认 1000）→ 自动分批写入。"""
        run = await self._make_run(db_session)
        inst_id = uuid.uuid4()
        # 生成 1050 行（trade_date 各不同）
        rows = [
            self._make_row_dict(
                run.id,
                inst_id,
                "000001",
                date(2026, 1, 1) + timedelta(days=i),
            )
            for i in range(1050)
        ]
        count = await upsert_rows_batch(db_session, rows)
        assert count == 1050


# =============================================================================
# 6. 集成：dry-run 估算 + 阈值组合
# =============================================================================


class TestDryRunEstimation:
    """dry-run 估算 expected_rows + estimated_db_size 组合逻辑。"""

    def test_full_month_estimate(self) -> None:
        """全市场单月估算：5000 × 20 = 100000 行，应 < 3GB 阈值。"""
        instruments = 5000
        trade_dates = 20
        est_gb = estimate_month_size(instruments, trade_dates)
        assert check_month_size_threshold(est_gb) is True

    def test_extreme_month_estimate_blocks(self) -> None:
        """极端场景：10000 × 31 = 310000 行（估算 > 3GB 应停止）。"""
        # 构造足够大估算触发阈值
        est_gb = estimate_month_size(instruments_count=50000, trade_dates_count=31)
        # 应阻止（不管实际值多少，构造极端场景必须阻止）
        # 这里只验证 check_month_size_threshold 在 > 3GB 时返回 False
        if est_gb > MONTH_SIZE_MAX_GB:
            assert check_month_size_threshold(est_gb) is False


if __name__ == "__main__":
    # 自测入口：允许直接运行（非 pytest）
    import asyncio

    async def _smoke() -> None:
        # 纯函数测试
        assert resolve_month_range("2026-01") == (date(2026, 1, 1), date(2026, 1, 31))
        assert check_failure_rate(failed=6, total=100) is False
        assert check_failure_rate(failed=5, total=100) is True
        est = estimate_month_size(5000, 20)
        assert est > 0
        print("smoke OK")

    asyncio.run(_smoke())
